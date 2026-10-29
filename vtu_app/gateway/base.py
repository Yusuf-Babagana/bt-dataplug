from django.urls import reverse

from .config import ConfigSpec, ProviderConfig
from .types import ProviderResult, PurchaseRequest, StatusResult, WebhookEvent


class WebhookRejected(Exception):
    """Raised by ``parse_webhook`` when a callback fails authenticity checks."""


class CustomerVerificationError(Exception):
    """
    Smartcard/meter lookup failed. ``invalid`` is True only when the provider definitively said the
    number does not exist; False means "could not confirm" (down, unknown reply shape).
    """

    def __init__(self, message="", invalid=False):
        super().__init__(message)
        self.invalid = invalid


class ProviderAdapter:
    """
    Contract every VTU provider implements. Translate to/from the provider's
    wire format *here* and nowhere else.

    Class attributes to set:
        name              unique lowercase slug; also the env prefix (VTU_<NAME>_*)
        label             human name for the admin
        services          iterable of ``Service`` values it can fulfil
        config_spec       ``ConfigSpec`` of required/optional env settings
        default_enabled   True only for a provider that was live before this system existed
        default_priority  lower = tried first

    Rules for ``purchase``:
        * Return REJECTED only when the provider has definitely NOT created an
          order (it answered, and said no). Anything doubtful - timeout, dropped
          connection, unparseable body - must be AMBIGUOUS; the engine will then
          refuse to fail over and will reconcile instead.
        * Never raise for provider problems, never put credentials in the result.
    """

    name = ""
    label = ""
    services = ()
    config_spec = ConfigSpec()
    default_enabled = False
    default_priority = 100
    webhook_verified = False    # True if parse_webhook cryptographically proves authenticity

    def __init__(self, config: ProviderConfig):
        self.config = config

    # -- capability ---------------------------------------------------------
    def supports(self, request: PurchaseRequest) -> bool:
        """Service + network/plan compatibility. Override for finer checks."""
        return request.service in self.services

    # -- required -----------------------------------------------------------
    def purchase(self, request: PurchaseRequest) -> ProviderResult:
        raise NotImplementedError

    def query_status(self, provider_order_id: str = "", reference: str = "") -> StatusResult:
        """Look up an order. Must return UNKNOWN (not FAILED) if it cannot tell."""
        raise NotImplementedError

    def check_connectivity(self) -> dict:
        """Read-only probe (auth + reachability). Must NEVER place an order."""
        raise NotImplementedError

    # -- pricing (used by the lowest-cost routing policy and the cost refresher) ----------------
    def quote(self, request: PurchaseRequest):
        """Our estimated cost of fulfilling ``request`` through this provider; None when unknown."""
        from . import offers
        return offers.quote(self.name, request)

    def catalog_plans(self):
        """The provider's current data/cable plans as ``catalog.CatalogPlan`` list; None when unsupported/unreachable."""
        return None

    def fetch_airtime_rates(self):
        """{'MTN': Decimal('97'), ...} - percent of face value the provider charges us; {} when unpublished."""
        return {}

    # -- optional -----------------------------------------------------------
    def parse_webhook(self, request) -> WebhookEvent:
        raise NotImplementedError

    def callback_url_path(self) -> str:
        """Path (not host) the provider should call back; default is the generic route."""
        return reverse("vtu_provider_webhook", kwargs={"provider": self.name})

    def verify_customer(self, service, network, target, meter_type="PREPAID"):
        raise NotImplementedError(f"{self.name} does not support customer verification")
