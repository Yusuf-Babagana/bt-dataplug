"""Test helpers for the VTU gateway: isolated provider env, a scriptable fake adapter, and a Nellobytes HTTP stub."""
import json
import os
from unittest import mock
from urllib.parse import urlparse

import requests
from django.test import TestCase

from vtu_app.gateway import registry
from vtu_app.gateway.base import ProviderAdapter
from vtu_app.gateway.config import ConfigSpec
from vtu_app.gateway.types import (
    ErrorCategory, OrderStatus, Outcome, ProviderResult, PurchaseRequest, Service, StatusResult, WebhookEvent,
)

NELLO_ENV = {
    "VTU_NELLOBYTES_ENABLED": "true",
    "VTU_NELLOBYTES_USER_ID": "test-user",
    "VTU_NELLOBYTES_API_KEY": "SUPER-SECRET-KEY-123",
}
# Env vars that would leak a developer's real .env into tests.
_SCRUB = ["NELLOBYTE_USER_ID", "NELLOBYTE_API_KEY", "NELLOBYTE_BASE_URL", "CK_USER_ID", "CK_API_KEY",
          "VTU_NELLOBYTES_BASE_URL", "VTU_NELLOBYTES_PRIORITY", "VTU_FAILURE_THRESHOLD", "VTU_SUSPEND_MINUTES",
          "VTU_ROUTING_POLICY", "VTU_GONGOZ_ENABLED", "VTU_GONGOZ_API_KEY", "VTU_GONGOZ_PRIORITY",
          "VTU_GONGOZ_SUCCESS_STATUSES", "VTU_GONGOZ_FAILED_STATUSES"]


class FakeAdapter(ProviderAdapter):
    """
    Scriptable provider. ``script`` is a list of ProviderResults handed out one per ``purchase``;
    ``statuses`` maps reference/order id -> OrderStatus for ``query_status``. State is class-level because the
    registry builds a fresh adapter per call.
    """
    label = "Fake"
    services = Service.ALL
    config_spec = ConfigSpec()
    default_enabled = False
    script = None
    calls = None
    statuses = None
    network_ok = True

    @classmethod
    def reset(cls):
        cls.script, cls.calls, cls.statuses = [], [], {}

    def supports(self, request):
        return self.network_ok and request.service in self.services

    def purchase(self, request: PurchaseRequest):
        type(self).calls.append(request)
        return type(self).script.pop(0)

    def query_status(self, provider_order_id="", reference=""):
        s = type(self).statuses
        return StatusResult(s.get(provider_order_id) or s.get(reference) or OrderStatus.UNKNOWN)

    def check_connectivity(self):
        return {"ok": True, "detail": "fake", "elapsed_ms": 1}

    def parse_webhook(self, request):
        d = request.query_params if request.method == "GET" else request.data
        return WebhookEvent(provider_order_id=d.get("orderid", ""), reference=d.get("requestid", ""))


class FakeA(FakeAdapter):
    name = "fakea"
    default_priority = 10


class FakeB(FakeAdapter):
    name = "fakeb"
    default_priority = 20


def accepted(order_id="ORD-1", **extra):
    return ProviderResult(Outcome.ACCEPTED, code="ORDER_RECEIVED", provider_order_id=order_id, extra=extra)


def rejected(category=ErrorCategory.MAINTENANCE, code="PROVIDER_DOWN"):
    return ProviderResult(Outcome.REJECTED, category, code, code)


def ambiguous(category=ErrorCategory.TIMEOUT):
    return ProviderResult(Outcome.AMBIGUOUS, category, "TIMEOUT", "Provider request timed out")


def http_json(body, status=200):
    resp = mock.Mock()
    resp.status_code = status
    resp.json.return_value = body
    resp.text = json.dumps(body)
    return resp


class SupplierStub:
    """
    Stands in for the Nellobytes HTTP API at the ``requests.get`` boundary, with the interface the old tests used
    for the supplier client: configure ``stub.buy_data.return_value = (response_dict, rid)`` (or ``side_effect``),
    then assert on ``stub.buy_data.call_args``. The real adapter runs in between.
    """
    ROUTES = {
        "/APIDatabundleV1.asp": ("buy_data", ("MobileNetwork", "DataPlan", "MobileNumber")),
        "/APIAirtimeV1.asp": ("buy_airtime", ("MobileNetwork", "Amount", "MobileNumber")),
        "/APICableTVV1.asp": ("buy_cable", ("CableTV", "Package", "SmartCardNo", "PhoneNo")),
        "/APIElectricityV1.asp": ("pay_electricity", ("ElectricCompany", "MeterNo", "MeterType", "Amount", "PhoneNo")),
    }

    def __init__(self):
        self.buy_data = mock.Mock()
        self.buy_airtime = mock.Mock()
        self.buy_cable = mock.Mock()
        self.pay_electricity = mock.Mock()
        self.query_order = mock.Mock(return_value={"status": "ORDER_RECEIVED"})
        self.balance = mock.Mock(return_value={"balance": "1000"})
        self.verify = mock.Mock(return_value={"customer_name": "JOHN DOE"})

    def __call__(self, url, params=None, **kw):
        path = urlparse(url).path
        params = dict(params or {})
        if path in self.ROUTES:
            name, keys = self.ROUTES[path]
            out = getattr(self, name)(*[params.get(k) for k in keys])
            body = out[0] if isinstance(out, tuple) else out
        elif path == "/APIQueryV1.asp":
            body = self.query_order(params.get("RequestID") or params.get("OrderID"))
        elif path == "/APIWalletBalanceV1.asp":
            body = self.balance()
        else:
            body = self.verify(path, params)
        return http_json(body)


class GatewayTestCase(TestCase):
    """Fake providers fakea (prio 10) and fakeb (prio 20) on; Nellobytes off unless ``nello = True``."""
    nello = False
    fakes = True
    env = {}

    def setUp(self):
        for cls in (FakeA, FakeB):
            cls.reset()
            registry.register(cls)
        self.addCleanup(registry.unregister, "fakea")
        self.addCleanup(registry.unregister, "fakeb")

        env = {k: "" for k in _SCRUB}
        env.update(NELLO_ENV if self.nello else {"VTU_NELLOBYTES_ENABLED": "false"})
        flag = "true" if self.fakes else "false"
        env.update({"VTU_FAKEA_ENABLED": flag, "VTU_FAKEB_ENABLED": flag})
        env.update(self.env)
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)

        # No test may touch the network: any HTTP call not explicitly mocked is recorded and fails the test
        # in cleanup (a test's own patch of requests.get / request overrides this).
        self.unmocked_http_calls = []

        def _blocked(*args, **kwargs):
            self.unmocked_http_calls.append(str(args[1] if len(args) > 1 else "?")[:80])
            raise requests.ConnectionError("network disabled in tests")

        net = mock.patch("requests.sessions.Session.request", side_effect=_blocked)
        net.start()
        self.addCleanup(net.stop)
        self.addCleanup(lambda: self.assertEqual(self.unmocked_http_calls, [], "test made an unmocked HTTP call"))
