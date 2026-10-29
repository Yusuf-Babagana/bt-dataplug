"""
Gateway tests. Providers are fakes or a mocked HTTP layer - no network, no real money.
Note: SQLite ignores select_for_update, so true parallel-thread races are NOT exercised here; the lock-dependent
guarantees are tested via the state guards + unique constraints instead.
"""
import os
from datetime import timedelta
from decimal import Decimal
from unittest import mock

import requests
from django.contrib.auth.models import User
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from vtu_app.gateway import engine, flows, health, ledger, registry, router
from vtu_app.gateway.adapters.nellobytes import normalize_order_status
from vtu_app.gateway.base import CustomerVerificationError, WebhookRejected
from vtu_app.gateway.types import (
    ErrorCategory, OrderStatus, Outcome, PurchaseRequest, Service, WebhookEvent, redact,
)
from vtu_app.models import DataPlan, Profile, ProviderState, PurchaseAttempt, Transaction, TransactionEvent, WalletTransaction
from vtu_app.services.transaction_service import TransactionService
from vtu_app.testing import FakeA, FakeB, GatewayTestCase, accepted, ambiguous, http_json, rejected

FAST_HASH = ['django.contrib.auth.hashers.MD5PasswordHasher']  # Profile hashes the PIN on every save


def make_user(balance="5000", name="buyer"):
    user = User.objects.create_user(name, f"{name}@example.com", "pw12345")
    profile, _ = Profile.objects.get_or_create(user=user)
    profile.wallet_balance = Decimal(balance)
    profile.save()
    return user


def balance(user):
    return Profile.objects.get(user=user).wallet_balance


def airtime_req(network="MTN", amount=500):
    return PurchaseRequest(service=Service.AIRTIME, reference="", network=network, amount=amount, target="08030000000")


def place(user, amount="500", key=None, prefix="ATM", service_type="Airtime Purchase (Mobile)"):
    """Debit the wallet exactly like the real flows do and return the Pending Transaction."""
    ok, tx = TransactionService.process_debit(
        user=user, amount=Decimal(amount), service_type=service_type, plan_name="MTN Airtime", recipient="08030000000",
        reference=flows.new_reference(prefix), description="test", cost_price=Decimal(amount) * Decimal("0.97"),
        idempotency_key=key)
    assert ok, tx
    return tx


def buy(user, template=None, amount="500", **kw):
    tx = place(user, amount)
    return tx, engine.dispatch(tx, template or airtime_req(amount=Decimal(amount)), **kw)


# ---------------------------------------------------------------------------
@override_settings(PASSWORD_HASHERS=FAST_HASH)
class NellobytesAdapterTests(GatewayTestCase):
    nello = True
    fakes = False

    def adapter(self):
        return registry.get_adapter("nellobytes")

    def buy(self, service=Service.AIRTIME, **kw):
        req = PurchaseRequest(service=service, reference="REF1", callback_url="http://cb", network=kw.pop("network", "MTN"),
                              amount=kw.pop("amount", 500), target="08030000000", **kw)
        return self.adapter().purchase(req)

    @mock.patch("vtu_app.gateway.adapters.nellobytes.requests.get")
    def test_accepted_request_matches_the_supplier_contract(self, get):
        get.return_value = http_json({"status": "ORDER_RECEIVED", "orderid": "77", "statuscode": "100", "metertoken": "1234-5678"})
        r = self.buy()
        self.assertEqual((r.outcome, r.provider_order_id, r.extra), (Outcome.ACCEPTED, "77", {"metertoken": "1234-5678"}))
        params, kw = get.call_args.kwargs["params"], get.call_args.kwargs
        self.assertEqual((params["RequestID"], params["MobileNetwork"], params["Amount"], params["CallBackURL"]), ("REF1", "01", 500, "http://cb"))
        self.assertEqual((params["UserID"], params["APIKey"]), ("test-user", "SUPER-SECRET-KEY-123"))
        self.assertIn("User-Agent", kw["headers"])      # the supplier blocks bare clients (kept from the old integration)
        self.assertTrue(get.call_args.args[0].endswith("/APIAirtimeV1.asp"))

    @mock.patch("vtu_app.gateway.adapters.nellobytes.requests.get")
    def test_order_received_without_id_is_still_accepted_because_we_reconcile_by_request_id(self, get):
        get.return_value = http_json({"status": "ORDER_RECEIVED"})
        r = self.buy()
        self.assertEqual((r.outcome, r.provider_order_id), (Outcome.ACCEPTED, ""))

    @mock.patch("vtu_app.gateway.adapters.nellobytes.requests.get")
    def test_other_services_hit_the_right_endpoints_with_the_right_codes(self, get):
        get.return_value = http_json({"status": "ORDER_RECEIVED", "orderid": "1"})
        self.buy(Service.DATA, plan_id="1000.00", network="9MOBILE")
        self.assertTrue(get.call_args.args[0].endswith("/APIDatabundleV1.asp"))
        self.assertEqual((get.call_args.kwargs["params"]["MobileNetwork"], get.call_args.kwargs["params"]["DataPlan"]), ("03", "1000.00"))
        self.buy(Service.CABLE, plan_id="gotv-jolli", network="GOTV", phone="08011111111")
        self.assertEqual(get.call_args.kwargs["params"]["CableTV"], "gotv")
        self.assertTrue(get.call_args.args[0].endswith("/APICableTVV1.asp"))
        self.buy(Service.ELECTRICITY, network="08", phone="08011111111", meter_type="02")
        p = get.call_args.kwargs["params"]
        self.assertEqual((p["ElectricCompany"], p["MeterType"]), ("08", "02"))      # the app's codes ARE the supplier's codes

    @mock.patch("vtu_app.gateway.adapters.nellobytes.requests.get")
    def test_secrets_never_reach_result_or_audit_payload(self, get):
        get.return_value = http_json({"status": "ORDER_RECEIVED", "orderid": "77", "APIKey": "SUPER-SECRET-KEY-123", "mobilenumber": "08030000000"})
        r = self.buy()
        self.assertNotIn("SUPER-SECRET-KEY-123", repr(r))
        self.assertEqual(r.raw["mobilenumber"], "*******0000")

    @mock.patch("vtu_app.gateway.adapters.nellobytes.requests.get")
    def test_confirmed_rejections_are_classified(self, get):
        cases = {
            "INVALID_RECIPIENT": (ErrorCategory.INVALID_REQUEST, False),
            "MINIMUM_50": (ErrorCategory.INVALID_REQUEST, False),
            "INSUFFICIENT_APIBALANCE": (ErrorCategory.PROVIDER_BALANCE, True),
            "INSUFFICIENT_BALANCE": (ErrorCategory.PROVIDER_BALANCE, True),
            "INVALID_CREDENTIALS": (ErrorCategory.AUTH, True),
            "INVALID_DATAPLAN": (ErrorCategory.UNSUPPORTED, True),
            "MTN SME data service is currently unavailable, pls try again later": (ErrorCategory.MAINTENANCE, True),
        }
        for status, (category, failover_safe) in cases.items():
            get.return_value = http_json({"status": status})
            r = self.buy()
            self.assertEqual((r.outcome, r.category, r.failover_safe), (Outcome.REJECTED, category, failover_safe), status)
            self.assertEqual(r.message, status)

    @mock.patch("vtu_app.gateway.adapters.nellobytes.requests.get")
    def test_unrecognised_reply_is_unclear_not_a_failure(self, get):
        for body in ({"status": "ERROR", "remark": "Connection Timeout"}, {"status": "WHO_KNOWS"}, {}):
            get.return_value = http_json(body)
            r = self.buy()
            self.assertEqual((r.outcome, r.failover_safe), (Outcome.AMBIGUOUS, False), body)

    @mock.patch("vtu_app.gateway.adapters.nellobytes.requests.get")
    def test_order_state_other_than_received_is_not_a_rejection(self, get):
        get.return_value = http_json({"status": "ORDER_ONHOLD", "orderid": "88"})
        r = self.buy()
        self.assertEqual((r.outcome, r.provider_order_id), (Outcome.AMBIGUOUS, "88"))

    @mock.patch("vtu_app.gateway.adapters.nellobytes.requests.get")
    def test_timeout_dropped_connection_and_garbage_are_ambiguous(self, get):
        get.side_effect = requests.ReadTimeout("https://x/?APIKey=SUPER-SECRET-KEY-123")
        r = self.buy()
        self.assertEqual((r.outcome, r.category), (Outcome.AMBIGUOUS, ErrorCategory.TIMEOUT))
        self.assertNotIn("SUPER-SECRET", repr(r))
        get.side_effect = requests.ConnectionError("reset")
        self.assertEqual(self.buy().outcome, Outcome.AMBIGUOUS)
        bad = mock.Mock(status_code=200, text="<html>oops</html>")
        bad.json.side_effect = ValueError()
        get.side_effect, get.return_value = None, bad
        self.assertEqual(self.buy().outcome, Outcome.AMBIGUOUS)

    @mock.patch("vtu_app.gateway.adapters.nellobytes.requests.get")
    def test_bare_status_token_reply_is_read_as_that_status(self, get):
        plain = mock.Mock(status_code=200, text="INSUFFICIENT_BALANCE")
        plain.json.side_effect = ValueError()
        get.return_value = plain
        r = self.buy()
        self.assertEqual((r.outcome, r.category), (Outcome.REJECTED, ErrorCategory.PROVIDER_BALANCE))

    @mock.patch("vtu_app.gateway.adapters.nellobytes.requests.get")
    def test_connect_timeout_means_nothing_was_sent(self, get):
        get.side_effect = requests.ConnectTimeout()
        r = self.buy()
        self.assertEqual((r.outcome, r.category), (Outcome.REJECTED, ErrorCategory.MAINTENANCE))

    def test_status_normalization_requires_exact_completion(self):
        n = normalize_order_status
        self.assertEqual(n("ORDER_COMPLETED", "200"), OrderStatus.SUCCESSFUL)
        self.assertEqual(n("ORDER_COMPLETED", "500"), OrderStatus.PENDING)      # contradictory -> don't trust
        for failed in ("ORDER_CANCELLED", "ORDER_FAILED", "ORDER_ERROR"):
            self.assertEqual(n(failed), OrderStatus.FAILED, failed)
        self.assertEqual(n("ORDER_ONHOLD"), OrderStatus.PENDING)
        self.assertEqual(n("INCOMPLETE_THING"), OrderStatus.UNKNOWN)            # no substring matching on COMPLETED
        self.assertEqual(n("WHO_KNOWS"), OrderStatus.UNKNOWN)
        self.assertEqual(n(None), OrderStatus.UNKNOWN)

    @mock.patch("vtu_app.gateway.adapters.nellobytes.requests.get")
    def test_query_uses_order_id_else_request_id_and_failure_is_unknown(self, get):
        get.return_value = http_json({"orderstatus": "ORDER_COMPLETED", "statuscode": "200"})
        self.assertEqual(self.adapter().query_status("77").status, OrderStatus.SUCCESSFUL)
        self.assertEqual(get.call_args.kwargs["params"]["OrderID"], "77")
        self.adapter().query_status("", "TIC-REF")
        self.assertEqual(get.call_args.kwargs["params"]["RequestID"], "TIC-REF")
        get.side_effect = requests.ReadTimeout()
        self.assertEqual(self.adapter().query_status("77").status, OrderStatus.UNKNOWN)

    def test_capability_and_plan_ownership(self):
        a = self.adapter()
        self.assertTrue(a.supports(airtime_req("MTN")) and a.supports(airtime_req("9MOBILE")))
        self.assertFalse(a.supports(airtime_req("BOGUS")))
        ours = DataPlan.objects.create(network="01", plan_name="n", dataplan_id="1000", cost_price=1, price=1)
        theirs = DataPlan.objects.create(network="01", plan_name="g", dataplan_id="281", cost_price=1, price=1, source="gongoz")
        mk = lambda p: PurchaseRequest(service=Service.DATA, reference="", network="MTN", plan_id=p.dataplan_id, plan_pk=p.pk)  # noqa: E731
        self.assertTrue(a.supports(mk(ours)))
        self.assertFalse(a.supports(mk(theirs)))        # a Gongoz plan id must never reach Nellobytes

    @mock.patch("vtu_app.gateway.adapters.nellobytes.requests.get")
    def test_customer_lookup(self, get):
        a = self.adapter()
        get.return_value = http_json({"customer_name": "JOHN DOE"})
        self.assertEqual(a.verify_customer(Service.ELECTRICITY, "04", "1234567890", "01"), "JOHN DOE")
        self.assertTrue(get.call_args.args[0].endswith("/APIVerifyElectricityV1.asp"))
        self.assertEqual(a.verify_customer(Service.CABLE, "GOTV", "1234567890"), "JOHN DOE")
        self.assertTrue(get.call_args.args[0].endswith("/APIVerifyCableTVV1.0.asp"))     # the endpoint this app has always used
        get.return_value = http_json({"customer_name": "INVALID_METERNO"})
        with self.assertRaises(CustomerVerificationError) as cm:
            a.verify_customer(Service.ELECTRICITY, "04", "1", "01")
        self.assertTrue(cm.exception.invalid)
        get.side_effect = requests.ReadTimeout()
        with self.assertRaises(CustomerVerificationError) as cm:
            a.verify_customer(Service.ELECTRICITY, "04", "1", "01")
        self.assertFalse(cm.exception.invalid)

    @mock.patch("vtu_app.gateway.adapters.nellobytes.requests.get")
    def test_connectivity_probe_only_reads_the_balance(self, get):
        get.return_value = http_json({"balance": "1500.00"})
        r = self.adapter().check_connectivity()
        self.assertTrue(r["ok"])
        self.assertIn("APIWalletBalanceV1", get.call_args.args[0])
        get.return_value = http_json({"status": "INVALID_CREDENTIALS"})
        self.assertFalse(self.adapter().check_connectivity()["ok"])
        self.assertEqual(self.adapter().get_balance(), "Auth Error")


@override_settings(PASSWORD_HASHERS=FAST_HASH)
class ConfigTests(GatewayTestCase):
    fakes = False

    def test_new_names_win_and_secrets_are_masked(self):
        with mock.patch.dict(os.environ, {"NELLOBYTE_USER_ID": "legacy", "VTU_NELLOBYTES_USER_ID": "newuser",
                                          "VTU_NELLOBYTES_API_KEY": "newkey-VALUE"}):
            cfg = registry.get_adapter("nellobytes").config
            self.assertEqual(cfg.get("USER_ID"), "newuser")
            self.assertNotIn("newkey-VALUE", repr(cfg))
            self.assertNotIn("newuser", repr(cfg))

    def test_the_names_this_app_used_before_still_work_and_are_enabled_by_default(self):
        with mock.patch.dict(os.environ, {"VTU_NELLOBYTES_ENABLED": "", "CK_USER_ID": "ck-user", "NELLOBYTE_API_KEY": "k",
                                          "NELLOBYTE_BASE_URL": "https://example.test/"}):
            cfg = registry.get_adapter("nellobytes").config
            self.assertTrue(cfg.enabled and cfg.is_valid)
            self.assertEqual((cfg.get("USER_ID"), cfg.get("BASE_URL")), ("ck-user", "https://example.test/"))

    def test_validation_flags_enabled_but_incomplete_provider(self):
        with mock.patch.dict(os.environ, {"VTU_NELLOBYTES_ENABLED": "true", "VTU_NELLOBYTES_API_KEY": ""}):
            self.assertTrue(any("VTU_NELLOBYTES_API_KEY" in p for p in registry.validate_configuration()))
        with mock.patch.dict(os.environ, {"VTU_NELLOBYTES_ENABLED": "false"}):
            self.assertEqual(registry.validate_configuration(), [])

    def test_redact(self):
        out = redact({"APIKey": "k", "UserID": "u", "meterno": "12345678901", "n": {"password": "p", "ok": 1}})
        self.assertEqual((out["APIKey"], out["UserID"]), ("***", "***"))
        self.assertTrue(out["meterno"].endswith("8901") and out["meterno"].startswith("*"))


# ---------------------------------------------------------------------------
@override_settings(PASSWORD_HASHERS=FAST_HASH)
class RoutingTests(GatewayTestCase):
    def names(self, req=None):
        return [a.name for a in router.select(req or airtime_req()).adapters]

    def test_priority_then_overrides(self):
        self.assertEqual(self.names(), ["fakea", "fakeb"])
        with mock.patch.dict(os.environ, {"VTU_FAKEB_PRIORITY": "1"}):
            self.assertEqual(self.names(), ["fakeb", "fakea"])
            ProviderState.objects.update_or_create(name="fakea", defaults={"priority_override": 0})
            self.assertEqual(self.names(), ["fakea", "fakeb"])

    def test_disabled_misconfigured_and_suspended_providers_are_skipped(self):
        with mock.patch.dict(os.environ, {"VTU_FAKEA_ENABLED": "false"}):
            self.assertEqual(self.names(), ["fakeb"])
        ProviderState.objects.update_or_create(name="fakeb", defaults={"admin_enabled": False})
        self.assertEqual(self.names(), ["fakea"])
        with mock.patch.dict(os.environ, {"VTU_NELLOBYTES_ENABLED": "true", "VTU_NELLOBYTES_API_KEY": "", "VTU_NELLOBYTES_USER_ID": ""}):
            self.assertNotIn("nellobytes", self.names())

    def test_unsupported_vs_unavailable(self):
        FakeA.network_ok = FakeB.network_ok = False
        try:
            self.assertEqual(engine.check_available(airtime_req())[0], 400)
        finally:
            FakeA.network_ok = FakeB.network_ok = True
        for n in ("fakea", "fakeb"):
            ProviderState.objects.update_or_create(name=n, defaults={"admin_enabled": False})
        self.assertEqual(engine.check_available(airtime_req())[0], 503)

    def test_custom_policy_is_pluggable(self):
        with mock.patch.dict(os.environ, {"VTU_ROUTING_POLICY": "vtu_app.test_gateway.ReversePolicy"}):
            self.assertEqual(self.names(), ["fakeb", "fakea"])


class ReversePolicy(router.RoutingPolicy):
    def order(self, candidates, request):
        return [a for a, _ in sorted(candidates, key=lambda c: c[0].name, reverse=True)]


@override_settings(PASSWORD_HASHERS=FAST_HASH)
class CircuitBreakerTests(GatewayTestCase):
    env = {"VTU_FAILURE_THRESHOLD": "3", "VTU_SUSPEND_MINUTES": "10"}

    def test_suspends_after_threshold_and_recovers(self):
        for _ in range(2):
            health.record_failure("fakea", ErrorCategory.TIMEOUT, "t")
        self.assertFalse(health.is_suspended(health.get_state("fakea")))
        health.record_failure("fakea", ErrorCategory.TIMEOUT, "t")
        self.assertTrue(health.is_suspended(health.get_state("fakea")))
        self.assertEqual([a.name for a in router.select(airtime_req()).adapters], ["fakeb"])
        ProviderState.objects.filter(name="fakea").update(suspended_until=timezone.now() - timedelta(seconds=1))
        self.assertEqual([a.name for a in router.select(airtime_req()).adapters], ["fakea", "fakeb"])
        health.record_failure("fakea", ErrorCategory.MAINTENANCE)       # half-open failure re-suspends immediately
        self.assertTrue(health.is_suspended(health.get_state("fakea")))
        health.record_success("fakea")
        st = health.get_state("fakea")
        self.assertEqual((st.consecutive_failures, st.suspended_until), (0, None))

    def test_invalid_request_failures_do_not_count_against_health(self):
        for _ in range(5):
            health.record_failure("fakea", ErrorCategory.INVALID_REQUEST)
        self.assertEqual(health.get_state("fakea").consecutive_failures, 0)


# ---------------------------------------------------------------------------
@override_settings(PASSWORD_HASHERS=FAST_HASH)
class EngineFailoverTests(GatewayTestCase):
    def setUp(self):
        super().setUp()
        self.user = make_user()

    def test_failover_after_confirmed_provider_side_rejection(self):
        FakeA.script = [rejected(ErrorCategory.MAINTENANCE)]
        FakeB.script = [accepted("B-1")]
        tx, d = buy(self.user)
        self.assertEqual((d.kind, d.order_id), ("accepted", "B-1"))
        tx.refresh_from_db()
        self.assertEqual((tx.provider, tx.order_id, tx.status), ("fakeb", "B-1", "Pending"))
        self.assertEqual(balance(self.user), Decimal("4500.00"))            # debited exactly once
        self.assertEqual(list(tx.attempts.values_list("provider", "sequence", "outcome", "reference")),
                         [("fakea", 1, "REJECTED", tx.reference), ("fakeb", 2, "ACCEPTED", f"{tx.reference}-2")])

    def test_invalid_request_does_not_fail_over_and_refunds(self):
        FakeA.script = [rejected(ErrorCategory.INVALID_REQUEST, "INVALID_RECIPIENT")]
        tx, d = buy(self.user)
        self.assertEqual((d.kind, d.http_status, d.message), ("refunded", 400, "INVALID_RECIPIENT"))
        self.assertEqual(FakeB.calls, [])
        tx.refresh_from_db()
        self.assertEqual((tx.status, balance(self.user)), ("Refunded", Decimal("5000.00")))
        self.assertIsNotNone(tx.refunded_at)

    def test_ambiguous_timeout_never_fails_over_or_refunds(self):
        FakeA.script = [ambiguous()]
        FakeB.script = [accepted("SHOULD-NOT-HAPPEN")]
        tx, d = buy(self.user)
        self.assertEqual(d.kind, "unclear")
        self.assertEqual(FakeB.calls, [])                                   # the double-purchase guard
        tx.refresh_from_db()
        self.assertEqual((tx.status, tx.refunded_at, balance(self.user)), ("Pending", None, Decimal("4500.00")))
        self.assertEqual(tx.attempts.get().outcome, Outcome.AMBIGUOUS)

    def test_adapter_crash_is_treated_as_ambiguous(self):
        FakeA.script = []           # pop() raises inside purchase
        tx, d = buy(self.user)
        self.assertEqual(d.kind, "unclear")
        self.assertEqual(FakeB.calls, [])
        tx.refresh_from_db()
        self.assertEqual(tx.status, "Pending")

    def test_all_providers_reject_refunds_the_exact_debit_once(self):
        FakeA.script = [rejected(ErrorCategory.MAINTENANCE)]
        FakeB.script = [rejected(ErrorCategory.AUTH, "INVALID_CREDENTIALS")]
        tx, d = buy(self.user, amount="1000")
        self.assertEqual((d.kind, d.http_status), ("refunded", 502))
        self.assertNotIn("INVALID_CREDENTIALS", d.message)                  # provider detail never reaches customers
        tx.refresh_from_db()
        self.assertEqual((tx.status, balance(self.user)), ("Refunded", Decimal("5000.00")))
        self.assertEqual(tx.attempts.count(), 2)
        self.assertEqual(sum(e.wallet_delta for e in tx.events.all()), Decimal("0.00"))     # ledger nets to zero

    def test_provider_float_exhausted_shows_maintenance_not_customer_wallet(self):
        FakeA.script = [rejected(ErrorCategory.PROVIDER_BALANCE, "INSUFFICIENT_BALANCE")]
        FakeB.script = [rejected(ErrorCategory.PROVIDER_BALANCE, "INSUFFICIENT_BALANCE")]
        _, d = buy(self.user)
        self.assertEqual(d.http_status, 503)
        self.assertIn("maintenance", d.message)

    def test_refund_with_auto_refund_off_leaves_settlement_to_the_caller(self):
        FakeA.script = [rejected(ErrorCategory.INVALID_REQUEST, "INVALID_RECIPIENT")]
        tx, d = buy(self.user, auto_refund=False)
        self.assertEqual(d.kind, "rejected")
        tx.refresh_from_db()
        self.assertEqual((tx.status, balance(self.user)), ("Pending", Decimal("4500.00")))

    def test_invalid_dataplan_disables_the_plan_everywhere(self):
        plan = DataPlan.objects.create(network="01", plan_name="x", dataplan_id="1000", cost_price=1, price=100)
        FakeA.script = [rejected(ErrorCategory.UNSUPPORTED, "INVALID_DATAPLAN")]
        FakeB.script = [rejected(ErrorCategory.UNSUPPORTED, "INVALID_DATAPLAN")]
        tx = place(self.user, "100", prefix="DTM", service_type="Data Purchase (Mobile)")
        engine.dispatch(tx, flows.data_request(plan, "08030000000"))
        plan.refresh_from_db()
        self.assertFalse(plan.is_active)

    def test_nothing_eligible_means_no_debit_when_preflight_is_used(self):
        for n in ("fakea", "fakeb"):
            ProviderState.objects.update_or_create(name=n, defaults={"admin_enabled": False})
        self.assertEqual(engine.check_available(airtime_req())[0], 503)
        self.assertEqual((balance(self.user), Transaction.objects.count()), (Decimal("5000.00"), 0))

    def test_attempt_rows_store_no_secret_or_full_number(self):
        FakeA.script = [accepted("E-1")]
        buy(self.user)
        self.assertNotIn("08030000000", str(list(PurchaseAttempt.objects.values())))

    def test_outage_then_routes_around_the_suspended_provider(self):
        with mock.patch.dict(os.environ, {"VTU_FAILURE_THRESHOLD": "3"}):
            for i in range(3):
                FakeA.script.append(rejected(ErrorCategory.MAINTENANCE))
                FakeB.script.append(accepted(f"B-{i}"))
                self.assertEqual(buy(self.user)[1].kind, "accepted")
            self.assertTrue(health.is_suspended(health.get_state("fakea")))
            calls = len(FakeA.calls)
            FakeB.script.append(accepted("B-next"))
            buy(self.user)
            self.assertEqual(len(FakeA.calls), calls)


@override_settings(PASSWORD_HASHERS=FAST_HASH)
class WalletSafetyTests(GatewayTestCase):
    def setUp(self):
        super().setUp()
        self.user = make_user()

    def test_idempotency_key_charges_once(self):
        a = place(self.user, key="key-aaaaaaaa")
        ok, b = TransactionService.process_debit(
            user=self.user, amount=Decimal("500"), service_type="Airtime Purchase (Mobile)", plan_name="x", recipient="r",
            reference=flows.new_reference("ATM"), description="d", idempotency_key="key-aaaaaaaa")
        self.assertTrue(ok)
        self.assertEqual((b.pk, getattr(b, "replayed", False)), (a.pk, True))
        self.assertEqual((balance(self.user), Transaction.objects.count()), (Decimal("4500.00"), 1))

    def test_unique_key_race_rolls_back_the_losing_debit(self):
        """Two requests pass the pre-check together: the DB constraint must undo the second debit."""
        first = place(self.user, key="race-key-001")
        real = Transaction.objects.filter
        calls = {"n": 0}

        def flaky(*args, **kwargs):
            if kwargs.get("idempotency_key") == "race-key-001" and calls["n"] == 0:
                calls["n"] += 1
                return Transaction.objects.none()        # the pre-check sees nothing (the race window)
            return real(*args, **kwargs)

        with mock.patch.object(Transaction.objects, "filter", side_effect=flaky):
            ok, tx = TransactionService.process_debit(
                user=self.user, amount=Decimal("500"), service_type="Airtime Purchase (Mobile)", plan_name="x", recipient="r",
                reference=flows.new_reference("ATM"), description="d", idempotency_key="race-key-001")
        self.assertTrue(ok)
        self.assertEqual((tx.pk, balance(self.user), Transaction.objects.count()), (first.pk, Decimal("4500.00"), 1))

    def test_same_key_for_different_users_is_independent(self):
        other = make_user(name="other")
        place(self.user, key="shared-key-1")
        place(other, key="shared-key-1")
        self.assertEqual(Transaction.objects.count(), 2)

    def test_insufficient_balance_moves_nothing(self):
        ok, msg = TransactionService.process_debit(
            user=self.user, amount=Decimal("9000"), service_type="Airtime Purchase (Mobile)", plan_name="x", recipient="r",
            reference=flows.new_reference("ATM"), description="d")
        self.assertEqual((ok, msg, balance(self.user), Transaction.objects.count()), (False, "Insufficient balance.", Decimal("5000.00"), 0))

    def test_references_are_unique_even_within_the_same_second(self):
        self.assertEqual(len({flows.new_reference("ATM") for _ in range(200)}), 200)

    def test_refund_exactly_once_however_often_it_is_triggered(self):
        tx = place(self.user)
        results = [ledger.settle(tx.pk, OrderStatus.FAILED, src) for src in ("webhook", "reconcile", "status-poll")]
        self.assertEqual(results, [True, False, False])
        self.assertEqual(balance(self.user), Decimal("5000.00"))
        self.assertEqual(tx.events.filter(wallet_delta=Decimal("500.00")).count(), 1)
        self.assertEqual(WalletTransaction.objects.filter(transaction_type="CREDIT").count(), 1)

    def test_process_refund_is_exactly_once_and_idempotent(self):
        tx = place(self.user)
        self.assertEqual(TransactionService.process_refund(self.user, tx.amount_customer_paid, tx.reference, "r")[0], True)
        self.assertEqual(TransactionService.process_refund(self.user, tx.amount_customer_paid, tx.reference, "r"), (False, "Already refunded"))
        self.assertEqual(balance(self.user), Decimal("5000.00"))

    def test_success_is_terminal_and_cannot_later_refund(self):
        tx = place(self.user)
        self.assertTrue(ledger.settle(tx.pk, OrderStatus.SUCCESSFUL, "webhook"))
        self.assertFalse(ledger.settle(tx.pk, OrderStatus.FAILED, "reconcile"))
        tx.refresh_from_db()
        self.assertEqual((tx.status, balance(self.user)), ("Successful", Decimal("4500.00")))
        self.assertEqual(tx.net_profit, Decimal("15.00"))       # 500 paid - 485 cost: profit totals computed at confirmation

    def test_late_success_after_refund_is_flagged_not_applied(self):
        tx = place(self.user)
        ledger.settle(tx.pk, OrderStatus.FAILED, "reconcile")
        with self.assertLogs("vtu_app.gateway", level="CRITICAL"):
            self.assertFalse(ledger.settle(tx.pk, OrderStatus.SUCCESSFUL, "webhook"))
        self.assertTrue(tx.events.filter(detail__startswith="ANOMALY").exists())
        self.assertEqual(balance(self.user), Decimal("5000.00"))

    def test_non_final_outcomes_change_nothing(self):
        tx = place(self.user)
        for o in (OrderStatus.PENDING, OrderStatus.UNKNOWN):
            self.assertFalse(ledger.settle(tx.pk, o, "reconcile"))
        tx.refresh_from_db()
        self.assertEqual(tx.status, "Pending")

    def test_every_transition_is_audited_and_customer_notified(self):
        from vtu_app.models import Notification
        tx = place(self.user)
        ledger.settle(tx.pk, OrderStatus.SUCCESSFUL, "webhook")
        trail = [(e.from_status, e.to_status, e.source) for e in tx.events.all()]
        self.assertEqual((trail[0], trail[-1]), (("", "Pending", "purchase"), ("Pending", "Successful", "webhook")))
        self.assertTrue(Notification.objects.filter(user=self.user, title="Airtime purchase successful").exists())

    def test_refund_notifies_the_customer(self):
        from vtu_app.models import Notification
        tx = place(self.user)
        ledger.settle(tx.pk, OrderStatus.FAILED, "reconcile")
        self.assertTrue(Notification.objects.filter(user=self.user, title="Airtime purchase failed").exists())


@override_settings(PASSWORD_HASHERS=FAST_HASH)
class ReconciliationTests(GatewayTestCase):
    def setUp(self):
        super().setUp()
        self.user = make_user()

    def unclear(self):
        FakeA.script = [ambiguous()]
        return buy(self.user)[0]

    def test_unclear_order_confirmed_successful_by_reference(self):
        tx = self.unclear()
        FakeA.statuses[tx.reference] = OrderStatus.SUCCESSFUL
        self.assertEqual(engine.reconcile_transaction(tx), "Successful")
        self.assertEqual(balance(self.user), Decimal("4500.00"))

    def test_unclear_order_confirmed_failed_is_refunded_once_and_never_resubmitted(self):
        tx = self.unclear()
        FakeA.statuses[tx.reference] = OrderStatus.FAILED
        engine.reconcile_transaction(tx)
        engine.reconcile_transaction(tx)
        self.assertEqual((balance(self.user), FakeB.calls), (Decimal("5000.00"), []))

    def test_unknown_or_pending_never_refunds(self):
        tx = self.unclear()
        for answer in (OrderStatus.UNKNOWN, OrderStatus.PENDING):
            FakeA.statuses[tx.reference] = answer
            self.assertEqual(engine.reconcile_transaction(tx), "Pending")
        self.assertEqual(balance(self.user), Decimal("4500.00"))

    def test_orphan_with_no_submission_refunds_only_after_grace(self):
        tx = place(self.user)
        self.assertEqual(engine.reconcile_transaction(tx), "Pending")
        Transaction.objects.filter(pk=tx.pk).update(created_at=timezone.now() - timedelta(minutes=30))
        self.assertEqual(engine.reconcile_transaction(tx), "Refunded")
        self.assertEqual(balance(self.user), Decimal("5000.00"))

    def test_older_pending_rows_from_before_the_gateway_are_never_auto_refunded(self):
        old = Transaction.objects.create(user=self.user, service_type="Data Purchase (Mobile)", plan_name="x", recipient="r",
                                         amount_customer_paid=Decimal("400"), status="Pending", reference="DTM-LEGACY")
        Transaction.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=3))
        self.assertEqual(engine.reconcile_transaction(old), "Pending")       # real outcome unknowable: left for staff
        self.assertEqual(balance(self.user), Decimal("5000.00"))

    def test_crash_mid_call_leaves_a_submitting_attempt_that_blocks_orphan_refund(self):
        tx = place(self.user)
        PurchaseAttempt.objects.create(transaction=tx, provider="fakea", reference=tx.reference)
        Transaction.objects.filter(pk=tx.pk).update(created_at=timezone.now() - timedelta(minutes=30))
        self.assertEqual(engine.reconcile_transaction(tx), "Pending")
        FakeA.statuses[tx.reference] = OrderStatus.SUCCESSFUL
        self.assertEqual(engine.reconcile_transaction(tx), "Successful")

    def test_sweep_command_and_only_purchase_types(self):
        tx = self.unclear()
        wallet_tx = Transaction.objects.create(user=self.user, service_type="Wallet Funding", plan_name="x", recipient="Wallet",
                                               amount_customer_paid=Decimal("100"), status="Pending", reference="FUND-1")
        Transaction.objects.filter(pk__in=[tx.pk, wallet_tx.pk]).update(created_at=timezone.now() - timedelta(minutes=10))
        FakeA.statuses[tx.reference] = OrderStatus.SUCCESSFUL
        from django.core.management import call_command
        from io import StringIO
        out = StringIO()
        call_command("reconcile_vtu_orders", stdout=out)
        self.assertIn("checked=1 resolved=1", out.getvalue())
        wallet_tx.refresh_from_db()
        self.assertEqual(wallet_tx.status, "Pending")

    def test_history_screen_self_heals_the_users_pending_orders(self):
        tx = self.unclear()
        Transaction.objects.filter(pk=tx.pk).update(created_at=timezone.now() - timedelta(minutes=1))
        FakeA.statuses[tx.reference] = OrderStatus.SUCCESSFUL
        client = APIClient()
        client.force_authenticate(self.user)
        r = client.get("/api/v1/transactions/")
        self.assertEqual(r.status_code, 200)
        tx.refresh_from_db()
        self.assertEqual(tx.status, "Successful")


# ---------------------------------------------------------------------------
class SignedFake(FakeA):
    """Adapter whose webhook is cryptographically verified (shared secret in a header)."""
    name = "signedfake"
    webhook_verified = True

    def parse_webhook(self, request):
        if request.headers.get("X-Sig") != "good":
            raise WebhookRejected("bad signature")
        d = request.data
        return WebhookEvent(provider_order_id=d.get("order", ""), reference=d.get("ref", ""),
                            claimed_status=d.get("status"), verified=True)


@override_settings(PASSWORD_HASHERS=FAST_HASH)
class WebhookTests(GatewayTestCase):
    env = {"VTU_SIGNEDFAKE_ENABLED": "true"}

    def setUp(self):
        super().setUp()
        registry.register(SignedFake)
        self.addCleanup(registry.unregister, "signedfake")
        SignedFake.reset()
        for n in ("fakea", "fakeb"):
            ProviderState.objects.update_or_create(name=n, defaults={"admin_enabled": False})
        self.user = make_user()
        self.client = APIClient()

    def pending(self, cls, order="W-1"):
        ProviderState.objects.update_or_create(name=cls.name, defaults={"admin_enabled": True})
        cls.script = [accepted(order)]
        return buy(self.user)[0]

    def url(self, provider):
        return reverse("vtu_provider_webhook", kwargs={"provider": provider})

    def test_duplicate_callback_is_ignored(self):
        self.pending(FakeA)
        FakeA.statuses["W-1"] = OrderStatus.FAILED
        r1 = self.client.post(self.url("fakea"), {"orderid": "W-1"}, format="json")
        r2 = self.client.post(self.url("fakea"), {"orderid": "W-1"}, format="json")
        self.assertEqual((r1.json()["status"], r2.json()["status"]), ("acknowledged", "already_resolved"))
        self.assertEqual(balance(self.user), Decimal("5000.00"))

    def test_unknown_order_and_unknown_provider(self):
        self.assertEqual(self.client.post(self.url("fakea"), {"orderid": "nope"}, format="json").status_code, 404)
        self.assertEqual(self.client.post(self.url("doesnotexist"), {"orderid": "x"}, format="json").status_code, 404)

    def test_invalid_signature_rejected_and_changes_nothing(self):
        tx = self.pending(SignedFake, "SIG-1")
        r = self.client.post(self.url("signedfake"), {"order": "SIG-1", "status": "FAILED"}, format="json", HTTP_X_SIG="forged")
        self.assertEqual(r.status_code, 401)
        tx.refresh_from_db()
        self.assertEqual((tx.status, balance(self.user)), ("Pending", Decimal("4500.00")))

    def test_valid_signature_settles_and_duplicates_are_noops(self):
        tx = self.pending(SignedFake, "SIG-2")
        body = {"order": "SIG-2", "status": "FAILED"}
        self.assertEqual(self.client.post(self.url("signedfake"), body, format="json", HTTP_X_SIG="good").status_code, 200)
        self.client.post(self.url("signedfake"), body, format="json", HTTP_X_SIG="good")
        tx.refresh_from_db()
        self.assertEqual((tx.status, balance(self.user)), ("Refunded", Decimal("5000.00")))


@override_settings(PASSWORD_HASHERS=FAST_HASH)
class NellobytesWebhookTests(GatewayTestCase):
    nello = True
    fakes = False

    def setUp(self):
        super().setUp()
        self.user = make_user()
        self.client = APIClient()

    def place_order(self, order):
        with mock.patch("vtu_app.gateway.adapters.nellobytes.requests.get") as get:
            get.return_value = http_json({"status": "ORDER_RECEIVED", "orderid": order})
            return buy(self.user)[0]

    def hit(self, status_body, **query):
        with mock.patch("vtu_app.gateway.adapters.nellobytes.requests.get") as get:
            get.return_value = http_json(status_body)
            return self.client.get(reverse("vtu_provider_webhook", kwargs={"provider": "nellobytes"}), query)

    def test_forged_failure_callback_cannot_trigger_refund(self):
        """Unsigned callback claims CANCELLED; the supplier's own answer is COMPLETED -> no refund."""
        tx = self.place_order("N-9")
        r = self.hit({"orderstatus": "ORDER_COMPLETED", "statuscode": "200"}, orderid="N-9", requestid=tx.reference, orderstatus="ORDER_CANCELLED")
        self.assertEqual(r.status_code, 200)
        tx.refresh_from_db()
        self.assertEqual((tx.status, balance(self.user)), ("Successful", Decimal("4500.00")))

    def test_forged_success_callback_does_not_complete_a_pending_order(self):
        tx = self.place_order("N-8")
        self.hit({"orderstatus": "ORDER_ONHOLD"}, orderid="N-8", orderstatus="ORDER_COMPLETED")
        tx.refresh_from_db()
        self.assertEqual(tx.status, "Pending")

    def test_genuine_failure_callback_refunds_once_and_replays_are_ignored(self):
        self.place_order("N-7")
        r1 = self.hit({"orderstatus": "ORDER_CANCELLED"}, orderid="N-7")
        r2 = self.hit({"orderstatus": "ORDER_CANCELLED"}, orderid="N-7")
        self.assertEqual((r1.json()["status"], r2.json()["status"]), ("acknowledged", "already_resolved"))
        self.assertEqual(balance(self.user), Decimal("5000.00"))

    def test_supplier_query_failure_leaves_the_order_pending(self):
        tx = self.place_order("N-6")
        with mock.patch("vtu_app.gateway.adapters.nellobytes.requests.get", side_effect=requests.ReadTimeout()):
            r = self.client.get(reverse("vtu_provider_webhook", kwargs={"provider": "nellobytes"}), {"orderid": "N-6"})
        self.assertEqual(r.status_code, 200)
        tx.refresh_from_db()
        self.assertEqual(tx.status, "Pending")

    def test_callback_url_is_sent_to_the_supplier(self):
        with override_settings(PUBLIC_BASE_URL="https://www.example.test"), \
                mock.patch("vtu_app.gateway.adapters.nellobytes.requests.get") as get:
            get.return_value = http_json({"status": "ORDER_RECEIVED", "orderid": "N-1"})
            buy(self.user)
        self.assertEqual(get.call_args.kwargs["params"]["CallBackURL"], "https://www.example.test/api/v1/vtu/webhook/nellobytes/")
