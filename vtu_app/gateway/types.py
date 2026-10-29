"""Normalized, provider-independent types shared by the engine and adapters."""
import re
from dataclasses import dataclass, field
from decimal import Decimal


class Service:
    DATA = "DATA"
    AIRTIME = "AIRTIME"
    CABLE = "CABLE"
    ELECTRICITY = "ELECTRICITY"
    ALL = (DATA, AIRTIME, CABLE, ELECTRICITY)

    # wallet.Transaction.trans_type for each service
    TRANSACTION_TYPE = {DATA: "DATA", AIRTIME: "AIRTIME", CABLE: "UTILITY", ELECTRICITY: "UTILITY"}


class Outcome:
    """What happened to a *submission* - drives the failover decision."""
    ACCEPTED = "ACCEPTED"       # provider took the order; final state arrives later
    REJECTED = "REJECTED"       # provider confirmed it did NOT process the order
    AMBIGUOUS = "AMBIGUOUS"     # timeout / dropped / unparseable: it MAY have been processed


class ErrorCategory:
    NONE = ""
    AUTH = "auth"                           # bad/expired credentials            (provider-specific)
    PROVIDER_BALANCE = "provider_balance"   # our float at the provider is low   (provider-specific)
    MAINTENANCE = "maintenance"             # provider down / maintenance        (provider-specific)
    RATE_LIMIT = "rate_limit"               # throttled                          (provider-specific)
    UNSUPPORTED = "unsupported"             # this provider lacks the network/plan (provider-specific)
    INVALID_REQUEST = "invalid_request"     # bad number/meter/amount - every provider will refuse
    TIMEOUT = "timeout"
    CONNECTION = "connection"
    BAD_RESPONSE = "bad_response"
    UNKNOWN = "unknown"

    # Confirmed rejections for these reasons say nothing about the customer's
    # request, so trying the next provider is safe and sensible.
    FAILOVER_SAFE = frozenset({AUTH, PROVIDER_BALANCE, MAINTENANCE, RATE_LIMIT, UNSUPPORTED})
    # Failures that count against a provider's health (circuit breaker).
    HEALTH_AFFECTING = frozenset({AUTH, PROVIDER_BALANCE, MAINTENANCE, RATE_LIMIT,
                                  TIMEOUT, CONNECTION, BAD_RESPONSE})


class OrderStatus:
    """Normalized status of an order already accepted by a provider."""
    SUCCESSFUL = "SUCCESSFUL"
    FAILED = "FAILED"       # provider confirmed it failed / was reversed
    PENDING = "PENDING"     # still processing
    UNKNOWN = "UNKNOWN"     # unrecognized / not found - never auto-resolved


@dataclass
class PurchaseRequest:
    service: str
    reference: str                      # unique per attempt; sent as the provider's request id
    callback_url: str = ""
    amount: Decimal | int | None = None  # face amount sent to the provider
    network: str = ""                   # MTN/GLO/... | DSTV/GOTV/... | disco code
    plan_id: str = ""                   # the plan's provider-side code (DataPlan.dataplan_id / CablePlan.plan_id)
    plan_pk: int | None = None          # DB pk of OUR plan row; plan ownership (source) is decided from it
    target: str = ""                    # phone / meter / smartcard
    phone: str = ""                     # contact phone (cable / electricity)
    meter_type: str = "PREPAID"


@dataclass
class ProviderResult:
    outcome: str
    category: str = ErrorCategory.NONE
    code: str = ""                      # provider's raw status string (no secrets)
    message: str = ""                   # safe to show/log; kept equal to the legacy message
    provider_order_id: str = ""
    extra: dict = field(default_factory=dict)   # e.g. electricity token, passed to the client
    raw: dict = field(default_factory=dict)     # redacted provider body for the audit trail
    elapsed_ms: int = 0

    @property
    def failover_safe(self):
        return self.outcome == Outcome.REJECTED and self.category in ErrorCategory.FAILOVER_SAFE


@dataclass
class StatusResult:
    status: str
    code: str = ""
    raw: dict = field(default_factory=dict)


@dataclass
class WebhookEvent:
    provider_order_id: str = ""
    reference: str = ""
    claimed_status: str | None = None   # trusted only when ``verified``
    verified: bool = False              # signature/authenticity proven by the adapter


# --- redaction ---------------------------------------------------------------

_SECRET_KEYS = re.compile(r"(key|secret|password|pass|auth|userid|user_id|signature|token)", re.I)
_PII_KEYS = re.compile(r"(mobile|phone|meter|smartcard|card|account)", re.I)


def redact(value, _depth=0):
    """Recursively strip credentials and mask recipient numbers for logs/DB."""
    if _depth > 5:
        return "..."
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if _SECRET_KEYS.search(str(k)):
                out[k] = "***"
            elif _PII_KEYS.search(str(k)) and isinstance(v, (str, int)):
                s = str(v)
                out[k] = ("*" * max(len(s) - 4, 0)) + s[-4:]
            else:
                out[k] = redact(v, _depth + 1)
        return out
    if isinstance(value, list):
        return [redact(v, _depth + 1) for v in value[:20]]
    if isinstance(value, str) and len(value) > 500:
        return value[:500] + "..."
    return value


_CREDENTIAL_KEYS = re.compile(r"(key|secret|password|pass|auth|userid|user_id|signature)", re.I)


def strip_credentials(data):
    """Drop credential-looking fields from provider data that is returned to the client."""
    return {k: v for k, v in data.items() if not _CREDENTIAL_KEYS.search(str(k))}
