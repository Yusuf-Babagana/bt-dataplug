"""
Gongoz Concept adapter (https://www.gongozconcept.com/documentation/).

Documented: Token auth header, endpoints, request bodies, network/cable/disco id tables.
NOT documented (so handled defensively, see docs/VTU_PROVIDERS.md): response bodies, the
order-status vocabulary, any reference/idempotency field, and the webhook payload.

Consequences, all deliberate:
* An order is ACCEPTED only when the provider answers 200/201 with an order ``id``. Whether it
  later succeeded is decided by ``query_status`` using the status words you configure in
  ``VTU_GONGOZ_SUCCESS_STATUSES`` / ``VTU_GONGOZ_FAILED_STATUSES`` (empty by default => orders
  stay PENDING, never auto-completed or auto-refunded on a guess).
* Gongoz can only be queried by ITS id, never by our reference. A submission whose reply never
  arrived (timeout) therefore cannot be looked up: it stays PENDING for manual resolution.
* The order id is stored as ``<endpoint>:<id>`` (e.g. ``data:58``) so ``query_status`` knows which
  endpoint to ask.
"""
import logging
import time

import requests

from vtu_app.models import CablePlan, DataPlan

from ..base import CustomerVerificationError, ProviderAdapter, WebhookRejected
from ..config import ConfigSpec
from ..types import (
    ErrorCategory, OrderStatus, Outcome, ProviderResult, PurchaseRequest, Service, StatusResult, redact,
)

logger = logging.getLogger("vtu_app.gateway")

SPEC = ConfigSpec(
    required=("API_KEY",),
    optional={
        "BASE_URL": "https://www.gongozconcept.com/api",
        "TIMEOUT": "30",
        "PORTED_NUMBER": "true",
        "SUCCESS_STATUSES": "",     # comma separated, case-insensitive, e.g. "successful"
        "FAILED_STATUSES": "",      # e.g. "failed"
    },
    secrets=("API_KEY",),
)

NETWORK_IDS = {"MTN": 1, "GLO": 2, "9MOBILE": 3, "AIRTEL": 4, "SMILE": 6}
AIRTIME_NETWORKS = {"MTN", "GLO", "9MOBILE", "AIRTEL"}
CABLE_IDS = {"GOTV": 1, "DSTV": 2, "STARTIMES": 3}
# This app's disco codes (what the mobile app / web forms send; labels as shown to customers) -> Gongoz disco id.
# Gongoz: 1 Ikeja, 2 Eko, 3 Abuja, 4 Kano, 5 Enugu, 6 Port Harcourt, 7 Ibadan, 8 Kaduna, 9 Jos, 10 Benin, 11 Yola.
DISCO_IDS = {
    "01": 2,    # EKEDC (Eko)
    "02": 1,    # IKEDC (Ikeja)
    "03": 3,    # AEDC (Abuja)
    "04": 4,    # KEDCO (Kano)
    "05": 6,    # PHEDC (Port Harcourt)
    "06": 9,    # JEDC (Jos)
    "07": 7,    # IBEDC (Ibadan)
    "08": 5,    # EEDC (Enugu)
    "09": 8,    # KAEDCO (Kaduna)
    "10": 10,   # BEDC (Benin)
    "11": 11,   # YEDC (Yola)
}
METER_TYPE_IDS = {"01": 1, "02": 2, "PREPAID": 1, "POSTPAID": 2}

ENDPOINT = {
    Service.DATA: "data", Service.AIRTIME: "topup",
    Service.ELECTRICITY: "billpayment", Service.CABLE: "cablesub",
}
_QUERY_ENDPOINTS = set(ENDPOINT.values())


def _words(csv):
    return {w.strip().lower() for w in (csv or "").split(",") if w.strip()}


def _body_message(body):
    """Best-effort human text from an error body (DRF: {"detail"|"error"|"message": ..} or {field: [..]})."""
    if isinstance(body, dict):
        for k in ("detail", "error", "message", "msg"):
            if body.get(k):
                return str(body[k])[:200]
        for v in body.values():
            v = v[0] if isinstance(v, list) and v else v
            if isinstance(v, str):
                return v[:200]
    return ""


class GongozAdapter(ProviderAdapter):
    name = "gongoz"
    label = "Gongoz Concept"
    services = (Service.DATA, Service.AIRTIME, Service.CABLE, Service.ELECTRICITY)
    config_spec = SPEC
    default_enabled = False
    default_priority = 20
    webhook_verified = False

    GENERIC = "Service temporarily unavailable. Please try again."

    # -- capability ---------------------------------------------------------
    def _code(self, r: PurchaseRequest):
        """Gongoz's own plan id, only for plans imported from Gongoz (source='gongoz'); else None."""
        if not r.plan_pk:
            return None
        model, field = (DataPlan, "dataplan_id") if r.service == Service.DATA else (CablePlan, "plan_id")
        return model.objects.filter(pk=r.plan_pk, source=self.name).values_list(field, flat=True).first()

    def supports(self, r: PurchaseRequest) -> bool:
        net = (r.network or "").upper()
        if r.service == Service.AIRTIME:
            return net in AIRTIME_NETWORKS
        if r.service == Service.ELECTRICITY:
            return net in DISCO_IDS
        if r.service == Service.DATA:
            return net in NETWORK_IDS and self._code(r) is not None
        if r.service == Service.CABLE:
            return net in CABLE_IDS and self._code(r) is not None
        return False

    # -- http ---------------------------------------------------------------
    def fetch_catalog(self):
        """GET /user/ (also carries the plan catalogue). Returns the dict or None; never raises."""
        try:
            resp = self._call("GET", "user/")
            body = self._json(resp)
        except requests.RequestException:
            return None
        return body if resp.status_code == 200 and isinstance(body, dict) else None

    def _headers(self):
        return {"Authorization": f"Token {self.config.get('API_KEY')}", "Content-Type": "application/json"}

    def _timeout(self):
        try:
            return float(self.config.get("TIMEOUT", "30"))
        except ValueError:
            return 30.0

    def _url(self, path):
        return f"{self.config.get('BASE_URL').rstrip('/')}/{path.lstrip('/')}"

    def _call(self, method, path, **kw):
        # Exception text from requests can embed the URL; never propagate it.
        return requests.request(method, self._url(path), headers=self._headers(), timeout=self._timeout(), **kw)

    @staticmethod
    def _json(resp):
        try:
            return resp.json()
        except ValueError:
            return None

    # -- purchase -----------------------------------------------------------
    @staticmethod
    def _whole(amount):
        if int(amount) != amount:
            raise ValueError("fractional amount")
        return int(amount)

    def _payload(self, r: PurchaseRequest):
        ported = self.config.get("PORTED_NUMBER", "true").lower() in ("1", "true", "yes", "on")
        net = (r.network or "").upper()
        if r.service == Service.DATA:
            return {"network": NETWORK_IDS[net], "mobile_number": r.target, "plan": int(self._code(r)), "Ported_number": ported}
        if r.service == Service.AIRTIME:
            return {"network": NETWORK_IDS[net], "amount": self._whole(r.amount), "mobile_number": r.target,
                    "Ported_number": ported, "airtime_type": "VTU"}
        if r.service == Service.ELECTRICITY:
            return {"disco_name": DISCO_IDS[net], "amount": self._whole(r.amount), "meter_number": r.target,
                    "MeterType": METER_TYPE_IDS.get((r.meter_type or "PREPAID").upper(), 1)}
        return {"cablename": CABLE_IDS[net], "cableplan": int(self._code(r)), "smart_card_number": r.target}

    def purchase(self, r: PurchaseRequest) -> ProviderResult:
        started = time.monotonic()
        ms = lambda: int((time.monotonic() - started) * 1000)  # noqa: E731
        try:
            payload = self._payload(r)
        except (KeyError, TypeError, ValueError):
            return ProviderResult(Outcome.REJECTED, ErrorCategory.UNSUPPORTED, "UNSUPPORTED", "Unsupported request")

        endpoint = ENDPOINT[r.service]
        try:
            resp = self._call("POST", f"{endpoint}/", json=payload)
        except requests.ConnectTimeout:
            return ProviderResult(Outcome.REJECTED, ErrorCategory.MAINTENANCE, "CONNECT_TIMEOUT", self.GENERIC, elapsed_ms=ms())
        except requests.Timeout:
            return ProviderResult(Outcome.AMBIGUOUS, ErrorCategory.TIMEOUT, "TIMEOUT", "Provider request timed out", elapsed_ms=ms())
        except requests.RequestException:
            return ProviderResult(Outcome.AMBIGUOUS, ErrorCategory.CONNECTION, "CONNECTION", "Provider connection failed", elapsed_ms=ms())

        body = self._json(resp)
        code, raw = resp.status_code, redact(body) if isinstance(body, (dict, list)) else {}
        base = dict(raw=raw, elapsed_ms=ms())

        if code in (200, 201) and isinstance(body, dict) and body.get("id") not in (None, ""):
            return ProviderResult(Outcome.ACCEPTED, code=f"HTTP {code}", provider_order_id=f"{endpoint}:{body['id']}", **base)

        # Client/auth errors are answered before any order is created.
        if code in (401, 403):
            return ProviderResult(Outcome.REJECTED, ErrorCategory.AUTH, f"HTTP {code}", self.GENERIC, **base)
        if code == 429:
            return ProviderResult(Outcome.REJECTED, ErrorCategory.RATE_LIMIT, "HTTP 429", self.GENERIC, **base)
        if code in (400, 404, 405, 422):
            text = _body_message(body)
            low = text.lower()
            if "balance" in low or "insufficient" in low or "fund" in low:
                return ProviderResult(Outcome.REJECTED, ErrorCategory.PROVIDER_BALANCE, f"HTTP {code}", self.GENERIC, **base)
            if code in (404, 405):
                return ProviderResult(Outcome.REJECTED, ErrorCategory.MAINTENANCE, f"HTTP {code}", self.GENERIC, **base)
            return ProviderResult(Outcome.REJECTED, ErrorCategory.INVALID_REQUEST, f"HTTP {code}", text or "Invalid request", **base)

        # 5xx, 200 without an id, non-JSON, anything else: the order may exist.
        return ProviderResult(Outcome.AMBIGUOUS, ErrorCategory.BAD_RESPONSE, f"HTTP {code}", "Provider reply unclear", **base)

    # -- status -------------------------------------------------------------
    def query_status(self, provider_order_id="", reference=""):
        endpoint, _, oid = (provider_order_id or "").partition(":")
        if endpoint not in _QUERY_ENDPOINTS or not oid.isdigit():
            return StatusResult(OrderStatus.UNKNOWN, "NO_PROVIDER_ID")      # cannot query by our reference
        try:
            resp = self._call("GET", f"{endpoint}/{oid}")
        except requests.RequestException:
            return StatusResult(OrderStatus.UNKNOWN, "QUERY_FAILED")
        body = self._json(resp)
        if resp.status_code != 200 or not isinstance(body, dict):
            return StatusResult(OrderStatus.UNKNOWN, f"HTTP {resp.status_code}")

        value = next((str(v) for k, v in body.items() if k.lower() == "status" and v is not None), "")
        norm = value.strip().lower()
        if norm and norm in _words(self.config.get("SUCCESS_STATUSES")):
            status = OrderStatus.SUCCESSFUL
        elif norm and norm in _words(self.config.get("FAILED_STATUSES")):
            status = OrderStatus.FAILED
        else:
            status = OrderStatus.UNKNOWN
            logger.info("gongoz order %s has unconfigured status %r - left unresolved "
                        "(add it to VTU_GONGOZ_SUCCESS_STATUSES / FAILED_STATUSES once confirmed)", provider_order_id, value)
        return StatusResult(status, code=value[:50], raw=redact(body))

    def check_connectivity(self):
        started = time.monotonic()
        if self.config.errors():
            return {"ok": False, "detail": "; ".join(self.config.errors()), "elapsed_ms": 0}
        try:
            resp = self._call("GET", "user/")
        except requests.RequestException:
            return {"ok": False, "detail": "connection failed or timed out", "elapsed_ms": int((time.monotonic() - started) * 1000)}
        ok = resp.status_code == 200
        # Deliberately not echoing the body: it holds account details.
        return {"ok": ok, "detail": "authenticated" if ok else f"HTTP {resp.status_code}",
                "elapsed_ms": int((time.monotonic() - started) * 1000)}

    def parse_webhook(self, request):
        # Payload/signature format is undocumented; accepting unverified bodies would be guessing.
        # Orders are resolved by polling (status endpoint / reconcile command) instead.
        raise WebhookRejected("gongoz webhooks are not supported")

    # -- customer lookup ----------------------------------------------------
    def verify_customer(self, service, network, target, meter_type="PREPAID"):
        net = (network or "").upper()
        if service == Service.CABLE and net in CABLE_IDS:
            path, params = "validateiuc/", {"smart_card_number": target, "cablename": CABLE_IDS[net]}
        elif service == Service.ELECTRICITY and net in DISCO_IDS:
            path, params = "validatemeter/", {"meternumber": target, "disconame": DISCO_IDS[net],
                                             "mtype": METER_TYPE_IDS.get((meter_type or "PREPAID").upper(), 1)}
        else:
            raise NotImplementedError
        try:
            resp = self._call("GET", path, params=params)
        except requests.RequestException:
            raise CustomerVerificationError("Verification is temporarily unavailable. Please try again shortly.")
        body = self._json(resp)
        name = None
        if resp.status_code == 200 and isinstance(body, dict):
            name = next((str(v) for k, v in body.items()
                         if k.lower() in ("customer_name", "name", "customername", "customer") and v), None)
        if not name:
            # Unknown reply shape: refuse rather than invent a name.
            raise CustomerVerificationError("Could not verify these details. Please check and try again.")
        return name
