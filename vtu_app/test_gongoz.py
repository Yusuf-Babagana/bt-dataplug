"""Gongoz adapter + importer tests - HTTP is mocked; response shapes are ASSUMED (the vendor docs show none)."""
import os
from decimal import Decimal
from io import StringIO
from unittest import mock

import requests
from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import override_settings

from vtu_app.gateway import engine, flows, registry, router
from vtu_app.gateway.base import CustomerVerificationError, WebhookRejected
from vtu_app.gateway.types import ErrorCategory, OrderStatus, Outcome, PurchaseRequest, Service
from vtu_app.models import CablePlan, DataPlan, Profile, Transaction
from vtu_app.services.transaction_service import TransactionService
from vtu_app.testing import GatewayTestCase, http_json

KEY = "GONGOZ-SECRET-TOKEN-XYZ"
HTTP = "vtu_app.gateway.adapters.gongoz.requests.request"
FAST_HASH = ['django.contrib.auth.hashers.MD5PasswordHasher']


def make_user(balance="20000", name="g"):
    user = User.objects.create_user(name, f"{name}@example.com", "pw12345")
    profile, _ = Profile.objects.get_or_create(user=user)
    profile.wallet_balance = Decimal(balance)
    profile.save()
    return user


def wallet(user):
    return Profile.objects.get(user=user).wallet_balance


def gdata(**kw):
    defaults = dict(network="01", plan_name="1GB", dataplan_id="281", cost_price=Decimal("900"), price=Decimal("990"), source="gongoz")
    defaults.update(kw)
    return DataPlan.objects.create(**defaults)


@override_settings(PASSWORD_HASHERS=FAST_HASH)
class GongozAdapterTests(GatewayTestCase):
    fakes = False
    env = {"VTU_GONGOZ_ENABLED": "true", "VTU_GONGOZ_API_KEY": KEY}

    def adapter(self):
        return registry.get_adapter("gongoz")

    def req(self, service=Service.AIRTIME, network="MTN", **kw):
        return PurchaseRequest(service=service, reference="R1", network=network, amount=kw.pop("amount", 500),
                               target="08030000000", **kw)

    def buy(self, **kw):
        return self.adapter().purchase(self.req(**kw))

    @mock.patch(HTTP)
    def test_airtime_request_matches_documented_body_and_auth(self, http):
        http.return_value = http_json({"id": 58, "Status": "pending"}, 201)
        self.buy()
        args, kw = http.call_args
        self.assertEqual(args, ("POST", "https://www.gongozconcept.com/api/topup/"))
        self.assertEqual(kw["headers"]["Authorization"], f"Token {KEY}")
        self.assertEqual(kw["json"], {"network": 1, "amount": 500, "mobile_number": "08030000000", "Ported_number": True, "airtime_type": "VTU"})

    @mock.patch(HTTP)
    def test_electricity_maps_this_apps_disco_and_meter_codes(self, http):
        http.return_value = http_json({"id": 9}, 201)
        r = self.buy(service=Service.ELECTRICITY, network="01", meter_type="02")     # 01 = Eko here, 02 = postpaid
        self.assertEqual(http.call_args.kwargs["json"], {"disco_name": 2, "amount": 500, "meter_number": "08030000000", "MeterType": 2})
        self.assertEqual(r.provider_order_id, "billpayment:9")
        self.buy(service=Service.ELECTRICITY, network="08", meter_type="01")          # 08 = Enugu in the app labels -> Gongoz 5
        self.assertEqual(http.call_args.kwargs["json"]["disco_name"], 5)
        self.buy(service=Service.ELECTRICITY, network="09", meter_type="01")          # 09 = Kaduna -> Gongoz 8
        self.assertEqual(http.call_args.kwargs["json"]["disco_name"], 8)

    @mock.patch(HTTP)
    def test_data_and_cable_only_for_plans_imported_from_gongoz(self, http):
        http.return_value = http_json({"id": 3}, 201)
        a = self.adapter()
        ours = DataPlan.objects.create(network="01", plan_name="n", dataplan_id="1000", cost_price=1, price=1)
        theirs = gdata()
        cable = CablePlan.objects.create(cable_type="dstv", name="Padi", plan_id="74", cost_price=1, price=1, source="gongoz")
        legacy_cable = CablePlan.objects.create(cable_type="dstv", name="Padi", plan_id="dstv-padi", cost_price=1, price=1)
        self.assertFalse(a.supports(flows.data_request(ours, "0803")))
        self.assertTrue(a.supports(flows.data_request(theirs, "0803")))
        self.assertTrue(a.supports(flows.cable_request(cable, "123", "0803")))
        self.assertFalse(a.supports(flows.cable_request(legacy_cable, "123", "0803")))
        a.purchase(flows.data_request(theirs, "08030000000"))
        self.assertEqual(http.call_args.kwargs["json"], {"network": 1, "mobile_number": "08030000000", "plan": 281, "Ported_number": True})
        a.purchase(flows.cable_request(cable, "1234567890", "08030000000"))
        self.assertEqual(http.call_args.kwargs["json"], {"cablename": 2, "cableplan": 74, "smart_card_number": "1234567890"})

    def test_fractional_airtime_is_refused_not_truncated(self):
        r = self.buy(amount=Decimal("99.5"))
        self.assertEqual((r.outcome, r.category), (Outcome.REJECTED, ErrorCategory.UNSUPPORTED))

    @mock.patch(HTTP)
    def test_outcome_classification(self, http):
        http.return_value = http_json({"id": 58}, 201)
        r = self.buy()
        self.assertEqual((r.outcome, r.provider_order_id), (Outcome.ACCEPTED, "topup:58"))
        for code, cat in ((401, ErrorCategory.AUTH), (403, ErrorCategory.AUTH), (429, ErrorCategory.RATE_LIMIT)):
            http.return_value = http_json({"detail": "nope"}, code)
            r = self.buy()
            self.assertEqual((r.outcome, r.category, r.failover_safe), (Outcome.REJECTED, cat, True))
        http.return_value = http_json({"mobile_number": ["Enter a valid number"]}, 400)
        r = self.buy()
        self.assertEqual((r.category, r.message, r.failover_safe), (ErrorCategory.INVALID_REQUEST, "Enter a valid number", False))
        http.return_value = http_json({"error": "Insufficient balance. Your balance is 12.00"}, 400)
        r = self.buy()
        self.assertEqual(r.category, ErrorCategory.PROVIDER_BALANCE)
        self.assertNotIn("12.00", r.message)

    @mock.patch(HTTP)
    def test_ambiguous_cases_never_count_as_rejections(self, http):
        for side in (requests.ReadTimeout(), requests.ConnectionError("reset")):
            http.side_effect = side
            self.assertEqual(self.buy().outcome, Outcome.AMBIGUOUS)
        http.side_effect = None
        for resp in (http_json({"id": 1}, 500), http_json({"Status": "ok"}, 200), http_json({"id": ""}, 201)):
            http.return_value = resp
            self.assertEqual(self.buy().outcome, Outcome.AMBIGUOUS)
        http.side_effect = requests.ConnectTimeout()
        self.assertEqual(self.buy().outcome, Outcome.REJECTED)

    @mock.patch(HTTP)
    def test_token_never_appears_in_results(self, http):
        http.return_value = http_json({"id": 1, "Authorization": f"Token {KEY}", "api_key": KEY}, 201)
        self.assertNotIn(KEY, repr(self.buy()))
        http.side_effect = requests.ConnectionError(f"https://x?token={KEY}")
        self.assertNotIn(KEY, repr(self.buy()))

    @mock.patch(HTTP)
    def test_status_is_unresolved_until_the_vocabulary_is_configured(self, http):
        http.return_value = http_json({"id": 58, "Status": "successful"})
        self.assertEqual(self.adapter().query_status("topup:58").status, OrderStatus.UNKNOWN)
        self.assertEqual(http.call_args.args, ("GET", "https://www.gongozconcept.com/api/topup/58"))
        with mock.patch.dict(os.environ, {"VTU_GONGOZ_SUCCESS_STATUSES": "successful, delivered", "VTU_GONGOZ_FAILED_STATUSES": "Failed"}):
            for body, want in (({"Status": "Successful"}, OrderStatus.SUCCESSFUL), ({"status": "FAILED"}, OrderStatus.FAILED),
                               ({"Status": "processing"}, OrderStatus.UNKNOWN), ({}, OrderStatus.UNKNOWN)):
                http.return_value = http_json(body)
                self.assertEqual(self.adapter().query_status("data:1").status, want, body)

    @mock.patch(HTTP)
    def test_cannot_query_without_provider_id_or_on_errors(self, http):
        a = self.adapter()
        self.assertEqual(a.query_status("", "ATM-REF").status, OrderStatus.UNKNOWN)
        self.assertEqual(a.query_status("weird:../x").status, OrderStatus.UNKNOWN)
        http.assert_not_called()
        http.return_value = http_json({"detail": "not found"}, 404)
        self.assertEqual(a.query_status("topup:1").status, OrderStatus.UNKNOWN)
        http.side_effect = requests.ReadTimeout()
        self.assertEqual(a.query_status("topup:1").status, OrderStatus.UNKNOWN)

    @mock.patch(HTTP)
    def test_connectivity_probe_reads_user_endpoint_and_hides_the_body(self, http):
        http.return_value = http_json({"user": {"username": "someone", "pin": "1234"}})
        r = self.adapter().check_connectivity()
        self.assertEqual((r["ok"], r["detail"]), (True, "authenticated"))
        self.assertEqual(http.call_args.args, ("GET", "https://www.gongozconcept.com/api/user/"))
        http.return_value = http_json({}, 401)
        self.assertFalse(self.adapter().check_connectivity()["ok"])

    @mock.patch(HTTP)
    def test_verify_customer_uses_trailing_slash_urls_and_refuses_unknown_shapes(self, http):
        a = self.adapter()
        http.return_value = http_json({"name": "JANE DOE"})
        self.assertEqual(a.verify_customer(Service.CABLE, "GOTV", "123"), "JANE DOE")
        self.assertEqual(http.call_args.args[1], "https://www.gongozconcept.com/api/validateiuc/")
        self.assertEqual(http.call_args.kwargs["params"], {"smart_card_number": "123", "cablename": 1})
        self.assertEqual(a.verify_customer(Service.ELECTRICITY, "04", "999", "01"), "JANE DOE")
        self.assertEqual(http.call_args.kwargs["params"], {"meternumber": "999", "disconame": 4, "mtype": 1})
        http.return_value = http_json({"unexpected": "shape"})
        with self.assertRaises(CustomerVerificationError) as cm:
            a.verify_customer(Service.CABLE, "GOTV", "123")
        self.assertFalse(cm.exception.invalid)      # "couldn't confirm" - never reported as a definitive invalid number

    def test_webhook_refused_not_guessed(self):
        with self.assertRaises(WebhookRejected):
            self.adapter().parse_webhook(mock.Mock())

    def test_off_by_default_and_not_routed_without_a_key(self):
        with mock.patch.dict(os.environ, {"VTU_GONGOZ_API_KEY": ""}):
            self.assertTrue(any("VTU_GONGOZ_API_KEY" in p for p in registry.validate_configuration()))
            self.assertNotIn("gongoz", [a.name for a in router.select(self.req()).adapters])
        with mock.patch.dict(os.environ, {"VTU_GONGOZ_ENABLED": ""}):
            self.assertFalse(registry.get_adapter("gongoz").config.enabled)


CATALOG = {
    "Dataplans": {
        "MTN_PLAN": {"ALL": [
            {"dataplan_id": "281", "plan_type": "CORPORATE GIFTING", "plan": "1.0 GB", "month_validate": "CORPORATE MONTHLY", "plan_amount": "900"},
            {"dataplan_id": "636", "plan_type": "TALKMORE", "plan": "400.0", "month_validate": "7 DAYS", "plan_amount": "100"},
        ]},
        "AIRTEL_PLAN": {"ALL": [
            {"dataplan_id": "313", "plan_type": "CORPORATE GIFTING", "plan": "1.0 GB", "month_validate": "CG MONTHLY", "plan_amount": "950"}]},
    },
    "Cableplan": {
        "DSTVPLAN": [{"cableplan_id": "74", "cable": "DSTV", "package": "DStv Padi", "plan_amount": "4400"}],
        "STARTIMEPLAN": [{"cableplan_id": "103", "cable": "STARTIME", "package": "Nova - 600 Naira - 1 Week", "plan_amount": "600"}],
    },
}


@override_settings(PASSWORD_HASHERS=FAST_HASH)
class GongozPrimaryTests(GatewayTestCase):
    """Gongoz as the primary provider: plans come from Gongoz, Nellobytes never sees Gongoz plan ids."""
    nello = True
    fakes = False
    env = {"VTU_GONGOZ_ENABLED": "true", "VTU_GONGOZ_API_KEY": KEY, "VTU_GONGOZ_PRIORITY": "1", "VTU_NELLOBYTES_PRIORITY": "50"}

    def setUp(self):
        super().setUp()
        self.user = make_user()

    def run_import(self, *args):
        out = StringIO()
        with mock.patch(HTTP, return_value=http_json(CATALOG)):
            call_command("gongoz_import_plans", *args, stdout=out)
        return out.getvalue()

    def test_preview_writes_nothing_then_apply_imports(self):
        self.assertIn("PREVIEW", self.run_import("--margin", "10"))
        self.assertEqual(DataPlan.objects.filter(source="gongoz").count(), 0)
        self.run_import("--apply", "--margin", "10")
        g = DataPlan.objects.get(network="01", dataplan_id="281")
        self.assertEqual((g.source, g.cost_price, g.price, g.is_active), ("gongoz", Decimal("900"), Decimal("990.00"), True))
        self.assertFalse(DataPlan.objects.filter(dataplan_id="636").exists())             # TALKMORE skipped
        self.assertEqual(CablePlan.objects.get(plan_id="103").cable_type, "startimes")
        self.assertEqual(DataPlan.objects.get(network="04", dataplan_id="313").source, "gongoz")

    def test_reimport_keeps_admin_price_and_retires_dropped_plans(self):
        self.run_import("--apply")
        DataPlan.objects.filter(dataplan_id="281").update(price=Decimal("1234"))
        gdata(network="02", dataplan_id="999", plan_name="gone")
        self.run_import("--apply", "--margin", "50")
        self.assertEqual(DataPlan.objects.get(dataplan_id="281").price, Decimal("1234.00"))
        self.assertFalse(DataPlan.objects.get(dataplan_id="999").is_active)

    def test_same_provider_id_under_both_sources_stays_separate(self):
        ours = DataPlan.objects.create(network="01", plan_name="n", dataplan_id="281", cost_price=1, price=1)
        self.run_import("--apply")
        self.assertEqual(DataPlan.objects.filter(network="01", dataplan_id="281").count(), 2)
        ours.refresh_from_db()
        self.assertEqual(ours.source, "nellobytes")

    def test_deactivate_nellobytes_only_when_asked(self):
        old = DataPlan.objects.create(network="01", plan_name="n", dataplan_id="500", cost_price=1, price=1)
        self.run_import("--apply")
        old.refresh_from_db()
        self.assertTrue(old.is_active)
        self.run_import("--apply", "--deactivate-nellobytes")
        old.refresh_from_db()
        self.assertFalse(old.is_active)
        self.assertTrue(DataPlan.objects.get(network="01", dataplan_id="281").is_active)

    def test_seed_and_sync_commands_leave_imported_plans_alone(self):
        self.run_import("--apply")
        call_command("seed_plans", stdout=StringIO())
        self.assertTrue(DataPlan.objects.get(network="01", dataplan_id="281", source="gongoz").is_active)
        self.assertTrue(DataPlan.objects.filter(source="nellobytes").exists())          # and it did reseed its own

    @mock.patch("vtu_app.gateway.adapters.nellobytes.requests.get")
    @mock.patch(HTTP)
    def test_data_purchase_goes_to_gongoz_and_never_to_nellobytes(self, gz, nb):
        self.run_import("--apply")
        plan = DataPlan.objects.get(network="01", dataplan_id="281")
        gz.return_value = http_json({"id": 4242}, 201)
        ok, tx = TransactionService.process_debit(
            user=self.user, amount=plan.price, service_type="Data Purchase (Mobile)", plan_name=plan.plan_name, recipient="08030000000",
            reference=flows.new_reference("DTM"), description="d", cost_price=plan.cost_price)
        d = engine.dispatch(tx, flows.data_request(plan, "08030000000"))
        self.assertEqual((d.kind, d.order_id), ("accepted", "data:4242"))
        self.assertEqual(gz.call_args.kwargs["json"]["plan"], 281)
        nb.assert_not_called()
        tx.refresh_from_db()
        self.assertEqual(tx.provider, "gongoz")

    @mock.patch("vtu_app.gateway.adapters.nellobytes.requests.get")
    @mock.patch(HTTP)
    def test_a_rejected_gongoz_plan_is_refunded_never_failed_over_to_nellobytes(self, gz, nb):
        self.run_import("--apply")
        plan = DataPlan.objects.get(network="01", dataplan_id="281")
        gz.return_value = http_json({"detail": "x"}, 401)         # confirmed, failover-safe rejection
        ok, tx = TransactionService.process_debit(
            user=self.user, amount=plan.price, service_type="Data Purchase (Mobile)", plan_name="p", recipient="r",
            reference=flows.new_reference("DTM"), description="d")
        d = engine.dispatch(tx, flows.data_request(plan, "08030000000"))
        self.assertEqual(d.kind, "refunded")
        nb.assert_not_called()                                    # Nellobytes cannot serve a Gongoz plan id
        self.assertEqual(wallet(self.user), Decimal("20000.00"))

    def test_legacy_plans_stay_nellobytes_only_and_airtime_prefers_gongoz(self):
        legacy = DataPlan.objects.create(network="01", plan_name="n", dataplan_id="1000", cost_price=1, price=1)
        self.assertEqual([a.name for a in router.select(flows.data_request(legacy, "0803")).adapters], ["nellobytes"])
        req = flows.airtime_request("MTN", 500, "0803")
        self.assertEqual([a.name for a in router.select(req).adapters], ["gongoz", "nellobytes"])

    @mock.patch("vtu_app.gateway.adapters.nellobytes.requests.get")
    @mock.patch(HTTP)
    def test_customer_lookup_falls_through_to_the_next_provider(self, gz, nb):
        gz.return_value = http_json({"unexpected": "shape"})
        nb.return_value = http_json({"customer_name": "JOHN DOE"})
        self.assertEqual(flows.verify_customer(Service.ELECTRICITY, "04", "1234567890", "01"), {"customer_name": "JOHN DOE"})
        nb.return_value = http_json({"customer_name": "INVALID_METERNO"})
        self.assertEqual(flows.verify_customer(Service.ELECTRICITY, "04", "1", "01"), {"customer_name": "INVALID_METERNO"})
        nb.side_effect = requests.ReadTimeout()
        self.assertEqual(flows.verify_customer(Service.ELECTRICITY, "04", "1", "01"), {"customer_name": "Error validating meter"})
