"""
Nellobytes (ClubKonnect) adapter - the supplier this app has always used.

Wire format: GET with credentials in the query string; an order is accepted only when the reply's
``status`` is ``ORDER_RECEIVED`` *and* it carries an ``orderid``. Any other JSON reply without an order id
is a definite rejection; a timeout, dropped connection or unrecognised body is ambiguous (the order may
exist), and so is any reply that carries an order id but is not ORDER_RECEIVED (e.g. ORDER_ONHOLD).

Exception text from ``requests`` embeds the full URL (credentials are in the query string), so it is never
logged or re-raised: only fixed messages leave this module.
"""
import logging
import re
import time
from decimal import Decimal

import requests

from ..base import CustomerVerificationError, ProviderAdapter, WebhookRejected  # noqa: F401
from .. import offers
from ..catalog import CatalogPlan
from ..config import ConfigSpec, ProviderConfig
from ..types import (
    ErrorCategory, OrderStatus, Outcome, ProviderResult, PurchaseRequest, Service, StatusResult,
    WebhookEvent, redact, strip_credentials,
)

logger = logging.getLogger("vtu_app.gateway")

DEFAULT_BASE = "https://www.nellobytesystems.com"
SPEC = ConfigSpec(
    required=("USER_ID", "API_KEY"),
    optional={"BASE_URL": DEFAULT_BASE, "TIMEOUT": "30"},
    secrets=("USER_ID", "API_KEY"),
    # Names this app used before the multi-provider gateway - still honoured.
    legacy_env={"USER_ID": ("NELLOBYTE_USER_ID", "CK_USER_ID"), "API_KEY": ("NELLOBYTE_API_KEY", "CK_API_KEY"),
                "BASE_URL": ("NELLOBYTE_BASE_URL",)},
)

NETWORK_CODES = {"MTN": "01", "GLO": "02", "9MOBILE": "03", "T2MOBILE": "03", "AIRTEL": "04"}
CABLE_CODES = {"DSTV": "dstv", "GOTV": "gotv", "STARTIMES": "startimes"}
# This app's disco codes ARE Nellobytes' ElectricCompany codes (the mobile app and web forms send them as-is).
DISCO_CODES = {f"{n:02d}" for n in range(1, 13)}
METER_TYPES = {"01": "01", "02": "02", "PREPAID": "01", "POSTPAID": "02"}
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) BT-DataPlug/1.0"}   # the supplier blocks bare clients

_PENDING_STATUSES = {"ORDER_RECEIVED", "ORDER_ONHOLD", "ORDER_PROCESSING", "ORDER_PENDING"}
_FAILED_STATUSES = {"ORDER_ERROR"}
_TOKEN = re.compile(r"^[A-Z0-9_]{3,60}$")
# Supplier answers that mean "rejected, nothing was delivered". Anything else that is not an accepted order is
# treated as UNCLEAR (stays Pending for reconciliation) - never refunded on a guess.
_REJECTION_PREFIXES = ("INVALID_", "MISSING_", "INSUFFICIENT_", "MINIMUM_", "MAXIMUM_", "ORDER_CANCELLED", "ORDER_FAILED",
                       "NOT_AVAILABLE", "DUPLICATE_ORDER", "AUTHENTICATION_FAILED")


def is_definite_rejection(status):
    value = str(status or "").strip().upper()
    return value.startswith(_REJECTION_PREFIXES) or "CURRENTLY UNAVAILABLE" in value


def load_config():
    return ProviderConfig("nellobytes", SPEC, default_enabled=True)


def normalize_order_status(orderstatus, statuscode=None):
    """
    Map Nellobytes' order status to ``OrderStatus``. Success needs ORDER_COMPLETED (or SUCCESSFUL) and,
    when present, statuscode 200; anything unrecognised is UNKNOWN and is never auto-resolved.
    """
    value = str(orderstatus or "").strip().upper()
    code = str(statuscode or "").strip()
    if value in ("ORDER_COMPLETED", "SUCCESSFUL"):
        return OrderStatus.SUCCESSFUL if code in ("", "200") else OrderStatus.PENDING
    if value in _FAILED_STATUSES or any(w in value for w in ("FAILED", "CANCEL", "REFUND")):
        return OrderStatus.FAILED
    if value in _PENDING_STATUSES:
        return OrderStatus.PENDING
    return OrderStatus.UNKNOWN


def classify_rejection(code):
    c = (code or "").upper()
    if c == "UNSUPPORTED":
        return ErrorCategory.UNSUPPORTED
    if c == "INVALID_DATAPLAN":
        return ErrorCategory.UNSUPPORTED        # this supplier does not know that plan code
    if c == "INVALID_CREDENTIALS" or "AUTHENTICATION" in c or "IPADDRESS" in c or "RESTRICTED" in c:
        return ErrorCategory.AUTH
    if "INSUFFICIENT" in c:
        return ErrorCategory.PROVIDER_BALANCE
    if c.startswith("INVALID_API_ERROR") or "MAINTENANCE" in c or "NOT_AVAILABLE" in c or "UNAVAILABLE" in c:
        return ErrorCategory.MAINTENANCE
    if "LIMIT" in c or "TOO_MANY" in c:
        return ErrorCategory.RATE_LIMIT
    if c.startswith(("INVALID", "MINIMUM", "MAXIMUM", "MISSING", "DUPLICATE")):
        return ErrorCategory.INVALID_REQUEST
    return ErrorCategory.UNKNOWN


class _NotSent(Exception):
    """The request never reached the supplier (connect failure), so no order can exist."""


class _Unclear(Exception):
    """The request may have reached the supplier; the outcome is unknown."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


class NellobytesAdapter(ProviderAdapter):
    name = "nellobytes"
    label = "Nellobytes (ClubKonnect)"
    services = (Service.DATA, Service.AIRTIME, Service.CABLE, Service.ELECTRICITY)
    config_spec = SPEC
    default_enabled = True
    default_priority = 10
    webhook_verified = False    # callbacks are unsigned - always confirmed via query_status

    # -- capability ---------------------------------------------------------
    def supports(self, r: PurchaseRequest) -> bool:
        net = (r.network or "").upper()
        # DATA/CABLE: only a plan Nellobytes owns, or one with a verified offer from it (see offers.py)
        if r.service == Service.DATA:
            return net in NETWORK_CODES and offers.offer(self.name, r) is not None
        if r.service == Service.AIRTIME:
            return net in NETWORK_CODES
        if r.service == Service.CABLE:
            return net in CABLE_CODES and offers.offer(self.name, r) is not None
        if r.service == Service.ELECTRICITY:
            return net in DISCO_CODES
        return False

    # -- http ---------------------------------------------------------------
    def _creds(self):
        return {"UserID": self.config.get("USER_ID"), "APIKey": self.config.get("API_KEY")}

    def _timeout(self):
        try:
            return float(self.config.get("TIMEOUT", "30"))
        except ValueError:
            return 30.0

    def _get(self, endpoint, params, timeout=None, plain_text_ok=False):
        url = f"{self.config.get('BASE_URL', DEFAULT_BASE).rstrip('/')}{endpoint}"
        try:
            resp = requests.get(url, params={**self._creds(), **params}, headers=HEADERS, timeout=timeout or self._timeout())
        except requests.ConnectTimeout:
            raise _NotSent()
        except requests.Timeout:
            raise _Unclear("TIMEOUT")
        except requests.RequestException:
            raise _Unclear("CONNECTION")
        try:
            body = resp.json()
        except ValueError:
            text = resp.text.strip()
            # The supplier sometimes answers a bare status token (e.g. INSUFFICIENT_BALANCE) instead of JSON.
            if plain_text_ok and _TOKEN.match(text) and not text.startswith("ORDER_"):
                return {"status": text}
            logger.error("Nellobytes non-JSON response: endpoint=%s http=%s", endpoint, resp.status_code)
            raise _Unclear("BAD_RESPONSE")
        if not isinstance(body, dict):
            raise _Unclear("BAD_RESPONSE")
        return body

    # -- purchase -----------------------------------------------------------
    def _request(self, r: PurchaseRequest):
        base = {"RequestID": r.reference}
        if r.callback_url:
            base["CallBackURL"] = r.callback_url
        net = (r.network or "").upper()
        if r.service == Service.DATA:
            return "/APIDatabundleV1.asp", {**base, "MobileNetwork": NETWORK_CODES[net], "DataPlan": r.plan_id, "MobileNumber": r.target}
        if r.service == Service.AIRTIME:
            return "/APIAirtimeV1.asp", {**base, "MobileNetwork": NETWORK_CODES[net], "Amount": r.amount, "MobileNumber": r.target}
        if r.service == Service.CABLE:
            return "/APICableTVV1.asp", {**base, "CableTV": CABLE_CODES[net], "Package": r.plan_id,
                                         "SmartCardNo": r.target, "PhoneNo": r.phone}
        return "/APIElectricityV1.asp", {**base, "ElectricCompany": net, "MeterType": METER_TYPES[(r.meter_type or "01").upper()],
                                         "MeterNo": r.target, "Amount": r.amount, "PhoneNo": r.phone}

    def purchase(self, r: PurchaseRequest) -> ProviderResult:
        started = time.monotonic()
        ms = lambda: int((time.monotonic() - started) * 1000)  # noqa: E731
        try:
            endpoint, params = self._request(r)
        except (KeyError, TypeError):
            return ProviderResult(Outcome.REJECTED, ErrorCategory.UNSUPPORTED, "UNSUPPORTED", "Unsupported request")
        try:
            body = self._get(endpoint, params, plain_text_ok=True)
        except _NotSent:
            return ProviderResult(Outcome.REJECTED, ErrorCategory.MAINTENANCE, "CONNECT_TIMEOUT",
                                  "Nellobytes request timed out", elapsed_ms=ms())
        except _Unclear as e:
            category = {"TIMEOUT": ErrorCategory.TIMEOUT, "CONNECTION": ErrorCategory.CONNECTION}.get(e.code, ErrorCategory.BAD_RESPONSE)
            return ProviderResult(Outcome.AMBIGUOUS, category, e.code, "Provider reply unclear", elapsed_ms=ms())

        status = str(body.get("status") or "").strip()
        order_id = str(body.get("orderid") or "")
        raw = redact(body)
        if status == "ORDER_RECEIVED":
            # The order id is optional here: this app can always look the order up by its RequestID.
            reserved = {"orderid", "statuscode", "status", "order_id", "status_code"}
            return ProviderResult(Outcome.ACCEPTED, code=status, provider_order_id=order_id,
                                  extra=strip_credentials({k: v for k, v in body.items() if k not in reserved}),
                                  raw=raw, elapsed_ms=ms())
        if order_id or status.startswith("ORDER_"):
            # An order may exist (ONHOLD, an order id with another status, ...): not a rejection - reconcile, never refund blindly.
            return ProviderResult(Outcome.AMBIGUOUS, ErrorCategory.BAD_RESPONSE, status or "NO_STATUS",
                                  "Provider reply unclear", provider_order_id=order_id, raw=raw, elapsed_ms=ms())
        if is_definite_rejection(status):
            logger.error("Nellobytes order rejected: endpoint=%s status=%s", endpoint, status)
            return ProviderResult(Outcome.REJECTED, classify_rejection(status), status, status or "Nellobytes request failed",
                                  raw=raw, elapsed_ms=ms())
        logger.warning("Nellobytes unrecognised reply treated as unclear: endpoint=%s status=%s", endpoint, status[:60])
        return ProviderResult(Outcome.AMBIGUOUS, ErrorCategory.UNKNOWN, status[:100] or "NO_STATUS",
                              "Provider reply unclear", raw=raw, elapsed_ms=ms())

    # -- status -------------------------------------------------------------
    def query_status(self, provider_order_id="", reference=""):
        params = {"OrderID": provider_order_id} if provider_order_id else {"RequestID": reference}
        try:
            body = self._get("/APIQueryV1.asp", params, timeout=20)
        except (_NotSent, _Unclear):
            return StatusResult(OrderStatus.UNKNOWN, "QUERY_FAILED")
        raw_status = body.get("orderstatus") or body.get("status")
        return StatusResult(normalize_order_status(raw_status, body.get("statuscode")),
                            code=str(raw_status or "")[:50], raw=redact(body))

    def check_connectivity(self):
        """Wallet-balance read: proves credentials + reachability, places no order."""
        started = time.monotonic()
        if self.config.errors():
            return {"ok": False, "detail": "; ".join(self.config.errors()), "elapsed_ms": 0}
        try:
            body = self._get("/APIWalletBalanceV1.asp", {}, timeout=15)
        except (_NotSent, _Unclear) as e:
            return {"ok": False, "detail": getattr(e, "code", "timed out"), "elapsed_ms": int((time.monotonic() - started) * 1000)}
        ok = body.get("balance") is not None
        return {"ok": ok, "detail": f"balance {body.get('balance')}" if ok else str(body.get("status") or "UNKNOWN_ERROR"),
                "elapsed_ms": int((time.monotonic() - started) * 1000)}

    def catalog_plans(self):
        """Nellobytes' data plan list (public endpoint: needs only the user id). None if unreachable."""
        try:
            resp = requests.get(f"{self.config.get('BASE_URL', DEFAULT_BASE).rstrip('/')}/APIDatabundlePlansV2.asp",
                                params={"UserID": self.config.get("USER_ID")}, headers=HEADERS, timeout=30)
            data = resp.json().get("MOBILE_NETWORK", {})
        except (requests.RequestException, ValueError, AttributeError):
            return None
        keys = {"MTN": "01", "Glo": "02", "m_9mobile": "03", "Airtel": "04"}
        skip = {("01", c) for c in ("38", "39", "40", "41")}        # reported as duplicated by the supplier
        out = []
        for key, net in keys.items():
            for block in data.get(key, []):
                for p in block.get("PRODUCT", []):
                    try:
                        code = str(p["PRODUCT_CODE"]).strip()
                        cost = Decimal(str(p["PRODUCT_AMOUNT"]))
                    except (KeyError, ArithmeticError):
                        continue
                    if (net, code) not in skip and cost > 0:
                        name = str(p.get("PRODUCT_NAME", "")).strip()
                        out.append(CatalogPlan("data", net, code, cost, name, name))
        return out or None

    def get_balance(self):
        """Supplier wallet balance for the admin dashboards: a string, or an error label (legacy behaviour)."""
        try:
            body = self._get("/APIWalletBalanceV1.asp", {}, timeout=15)
        except (_NotSent, _Unclear):
            return "Conn Error"
        return body["balance"] if "balance" in body else "Auth Error"

    def parse_webhook(self, request):
        data = request.query_params if request.method == "GET" or not isinstance(request.data, dict) else request.data
        return WebhookEvent(provider_order_id=str(data.get("orderid") or ""), reference=str(data.get("requestid") or ""),
                            claimed_status=data.get("orderstatus") or data.get("status"), verified=False)

    # -- customer lookup ----------------------------------------------------
    def verify_customer(self, service, network, target, meter_type="01"):
        net = (network or "").upper()
        if service == Service.CABLE and net in CABLE_CODES:
            endpoint, params, token = "/APIVerifyCableTVV1.0.asp", {"CableTV": CABLE_CODES[net], "SmartCardNo": target}, "INVALID_SMARTCARDNO"
        elif service == Service.ELECTRICITY and net in DISCO_CODES:
            endpoint = "/APIVerifyElectricityV1.asp"
            params = {"ElectricCompany": net, "MeterNo": target, "MeterType": METER_TYPES.get(str(meter_type).upper(), "01")}
            token = "INVALID_METERNO"
        else:
            raise NotImplementedError
        try:
            body = self._get(endpoint, params, timeout=20)
        except (_NotSent, _Unclear):
            raise CustomerVerificationError("Verification is temporarily unavailable.")
        name = body.get("customer_name")
        if not name or str(name).upper().startswith("INVALID"):
            raise CustomerVerificationError(token, invalid=True)
        return str(name)
