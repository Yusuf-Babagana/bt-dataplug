"""Cheapest-provider routing: offers, rates, the matcher, the cost refresher, and the safety of unverified links."""
import os
from decimal import Decimal
from io import StringIO
from unittest import mock

import requests
from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import override_settings

from vtu_app.gateway import engine, flows, offers, router
from vtu_app.gateway.catalog import CatalogPlan, match_cable, match_data, parse_days, parse_kind, parse_size_mb
from vtu_app.gateway.types import ErrorCategory, PurchaseRequest, Service
from vtu_app.models import CablePlan, DataPlan, PlanOffer, Profile, ProviderRate, Transaction
from vtu_app.services.transaction_service import TransactionService
from vtu_app.testing import FakeA, FakeB, GatewayTestCase, SupplierStub, accepted, http_json, rejected

FAST_HASH = ['django.contrib.auth.hashers.MD5PasswordHasher']
GZ = "vtu_app.gateway.adapters.gongoz.requests.request"
NB = "vtu_app.gateway.adapters.nellobytes.requests.get"
KEY = "GONGOZ-SECRET-TOKEN-XYZ"


def make_user(balance="20000", name="p"):
    user = User.objects.create_user(name, f"{name}@example.com", "pw12345")
    profile, _ = Profile.objects.get_or_create(user=user)
    profile.wallet_balance = Decimal(balance)
    profile.save()
    return User.objects.get(pk=user.pk)


def wallet(user):
    return Profile.objects.get(user=user).wallet_balance


def cp(kind, network, code, cost, text, name=None):
    return CatalogPlan(kind, network, code, Decimal(str(cost)), name or text, text)


class ParsingTests(GatewayTestCase):
    def test_size(self):
        self.assertEqual(parse_size_mb("MTN 1GB SME - 7 days"), 1000)
        self.assertEqual(parse_size_mb("1.0 GB"), 1000)
        self.assertEqual(parse_size_mb("1000.0 MB"), 1000)
        self.assertEqual(parse_size_mb("1.5 TB"), 1_500_000)
        self.assertIsNone(parse_size_mb("500.0 WEEKLY PLAN"))        # no unit: never guessed
        self.assertIsNone(parse_size_mb(None))

    def test_validity(self):
        cases = {"30 days": 30, "30DAYS": 30, "7Day (Binge)": 7, "2DAY PLAN": 2, "2 WEEKS [GIFTING]": 14, "WEEKLY D *323*4#": 7,
                 "CORPORATE MONTHLY": 30, "DAILY": 1, "3 MONTH PLAN": 90, "YEARLY": 365, "no idea": None}
        for text, days in cases.items():
            self.assertEqual(parse_days(text), days, text)

    def test_family(self):
        self.assertEqual(parse_kind("CORPORATE GIFTING"), "CORPORATE")
        self.assertEqual(parse_kind("MTN 1GB SME"), "SME")
        self.assertEqual(parse_kind("Gifting"), "GIFTING")
        self.assertEqual(parse_kind("Awoof Data"), "COUPON")
        self.assertIsNone(parse_kind("MTN 1GB"))


class MatcherTests(GatewayTestCase):
    cands = [
        cp("data", "01", "281", 900, "1.0 GB CORPORATE MONTHLY CORPORATE GIFTING"),
        cp("data", "01", "290", 774, "1000.0 MB WEEKLY DIAL *323*4# FOR BALAN SME"),
        cp("data", "01", "453", 772, "1.0 GB 7 DAYS PLAN DATA COUPON"),
        cp("data", "04", "313", 950, "1.0 GB CG MONTHLY CORPORATE GIFTING"),
    ]

    def test_exact_size_validity_and_network_match(self):
        m = match_data("MTN 1GB SME - 7 days", "01", self.cands)
        self.assertEqual([c.code for c in m], ["290"])                  # CORPORATE monthly / COUPON excluded (family or validity)

    def test_family_mismatch_and_other_network_are_excluded(self):
        self.assertEqual(match_data("MTN 1GB Corporate Gifting - 30 days", "01", self.cands)[0].code, "281")
        self.assertEqual(match_data("MTN 1GB SME - 30 days", "01", self.cands), [])
        self.assertEqual(match_data("1GB SME - 7 days", "04", self.cands), [])      # the 1GB/7-day SME candidate is on MTN, not Airtel

    def test_unparseable_plans_never_match(self):
        self.assertEqual(match_data("Mystery bundle", "01", self.cands), [])
        self.assertEqual(match_data("500.0 WEEKLY PLAN", "01", self.cands), [])

    def test_several_candidates_are_returned_cheapest_first_for_a_human_to_choose(self):
        extra = self.cands + [cp("data", "01", "999", 700, "1.0 GB 7 DAYS PLAN")]
        self.assertEqual([c.code for c in match_data("MTN 1GB - 7 days", "01", extra)], ["999", "453", "290"])

    def test_cable_by_name_with_a_cost_guard(self):
        cc = [cp("cable", "gotv", "93", 3900, "GOtv Jinja", "GOtv Jinja"), cp("cable", "gotv", "94", 9000, "GOtv Jinja", "GOtv Jinja")]
        got = match_cable("GOtv Jinja", "gotv", cc, cost_hint=Decimal("3850"))
        self.assertEqual([c.code for c in got], ["93"])
        self.assertEqual(match_cable("GOtv Jinja", "dstv", cc), [])


@override_settings(PASSWORD_HASHERS=FAST_HASH)
class OfferTests(GatewayTestCase):
    def setUp(self):
        super().setUp()
        self.plan = DataPlan.objects.create(network="01", plan_name="MTN 1GB SME - 7 days", dataplan_id="1000",
                                            cost_price=Decimal("563"), price=Decimal("650"))
        self.req = flows.data_request(self.plan, "08030000000")

    def test_owner_sells_at_its_own_cost_and_code(self):
        self.assertEqual(offers.offer("nellobytes", self.req), offers.Offer("1000", Decimal("563.00")))

    def test_other_providers_need_a_verified_active_offer(self):
        po = PlanOffer.objects.create(data_plan=self.plan, provider="gongoz", provider_code="290", cost_price=Decimal("500"))
        self.assertIsNone(offers.offer("gongoz", self.req))              # unverified: never routed
        po.verified = True
        po.save()
        self.assertEqual(offers.offer("gongoz", self.req), offers.Offer("290", Decimal("500.00")))
        po.is_active = False
        po.save()
        self.assertIsNone(offers.offer("gongoz", self.req))              # provider stopped listing it

    def test_airtime_quote_uses_the_published_rate(self):
        req = flows.airtime_request("MTN", 1000, "0803")
        self.assertIsNone(offers.quote("gongoz", req))                   # unknown rate = unknown cost
        ProviderRate.objects.create(provider="gongoz", key="airtime:MTN", value=Decimal("97"))
        self.assertEqual(offers.quote("gongoz", req), Decimal("970.00"))

    def test_electricity_has_no_quote(self):
        self.assertIsNone(offers.quote("gongoz", PurchaseRequest(service=Service.ELECTRICITY, reference="", network="04", amount=2000)))


@override_settings(PASSWORD_HASHERS=FAST_HASH)
class LowestCostRoutingTests(GatewayTestCase):
    def setUp(self):
        super().setUp()
        self.user = make_user()

    def rate(self, provider, pct, net="MTN"):
        ProviderRate.objects.update_or_create(provider=provider, key=f"airtime:{net}", defaults={"value": Decimal(pct)})

    def names(self, req=None):
        return [a.name for a in router.select(req or flows.airtime_request("MTN", 1000, "0803")).adapters]

    def test_cheaper_provider_is_tried_first_even_with_a_worse_priority(self):
        self.rate("fakea", "97")
        self.rate("fakeb", "95")
        self.assertEqual(self.names(), ["fakeb", "fakea"])            # fakea has the better priority, fakeb the better price

    def test_unknown_cost_sorts_after_known_and_no_data_means_priority_order(self):
        self.assertEqual(self.names(), ["fakea", "fakeb"])
        self.rate("fakeb", "97")
        self.assertEqual(self.names(), ["fakeb", "fakea"])            # a known cost beats an unknown one

    def test_ties_fall_back_to_priority(self):
        self.rate("fakea", "97")
        self.rate("fakeb", "97")
        self.assertEqual(self.names(), ["fakea", "fakeb"])

    def test_rates_are_per_network(self):
        self.rate("fakea", "97", "MTN")
        self.rate("fakeb", "95", "MTN")
        self.rate("fakea", "96", "GLO")
        self.rate("fakeb", "98", "GLO")
        self.assertEqual(self.names(flows.airtime_request("GLO", 1000, "0803")), ["fakea", "fakeb"])

    def test_priority_policy_can_still_be_selected(self):
        self.rate("fakeb", "90")
        with mock.patch.dict(os.environ, {"VTU_ROUTING_POLICY": "vtu_app.gateway.router.PriorityPolicy"}):
            self.assertEqual(self.names(), ["fakea", "fakeb"])

    def test_a_crashing_quote_never_breaks_routing(self):
        with mock.patch.object(FakeA, "quote", side_effect=RuntimeError("boom")):
            self.assertEqual(self.names(), ["fakea", "fakeb"])

    def test_failover_goes_to_the_next_cheapest_and_records_the_real_cost(self):
        self.rate("fakea", "97")
        self.rate("fakeb", "95")
        FakeB.script = [rejected(ErrorCategory.MAINTENANCE)]          # the cheapest is down...
        FakeA.script = [accepted("A-1")]                              # ...so the next cheapest takes it
        ok, tx = TransactionService.process_debit(
            user=self.user, amount=Decimal("990"), service_type="Airtime Purchase (Mobile)", plan_name="x", recipient="r",
            reference=flows.new_reference("ATM"), description="d", cost_price=Decimal("970"))
        d = engine.dispatch(tx, flows.airtime_request("MTN", 1000, "0803"))
        self.assertEqual(d.kind, "accepted")
        tx.refresh_from_db()
        self.assertEqual((tx.provider, tx.cost_from_klubconnect), ("fakea", Decimal("970.00")))
        self.assertEqual([c.reference for c in FakeB.calls], [tx.reference])

    def test_cost_of_the_provider_that_took_the_order_drives_the_profit(self):
        self.rate("fakeb", "95")
        FakeB.script = [accepted("B-1")]
        ok, tx = TransactionService.process_debit(
            user=self.user, amount=Decimal("990"), service_type="Airtime Purchase (Mobile)", plan_name="x", recipient="r",
            reference=flows.new_reference("ATM"), description="d", cost_price=Decimal("970"))
        engine.dispatch(tx, flows.airtime_request("MTN", 1000, "0803"))
        from vtu_app.gateway import ledger
        from vtu_app.gateway.types import OrderStatus
        ledger.settle(tx.pk, OrderStatus.SUCCESSFUL, "webhook")
        tx.refresh_from_db()
        self.assertEqual((tx.cost_from_klubconnect, tx.net_profit), (Decimal("950.00"), Decimal("40.00")))    # not the 20 a 97% supplier would give


@override_settings(PASSWORD_HASHERS=FAST_HASH)
class DataPlanRoutingTests(GatewayTestCase):
    """A plan owned by Nellobytes, with a Gongoz alternative that is cheaper once a human has verified it."""
    nello = True
    fakes = False
    env = {"VTU_GONGOZ_ENABLED": "true", "VTU_GONGOZ_API_KEY": KEY}

    def setUp(self):
        super().setUp()
        self.user = make_user()
        self.plan = DataPlan.objects.create(network="01", plan_name="MTN 1GB SME - 7 days", dataplan_id="1000",
                                            cost_price=Decimal("563"), price=Decimal("650"))
        self.offer = PlanOffer.objects.create(data_plan=self.plan, provider="gongoz", provider_code="290", cost_price=Decimal("500"))

    def buy(self):
        ok, tx = TransactionService.process_debit(
            user=self.user, amount=self.plan.price, service_type="Data Purchase (Mobile)", plan_name=self.plan.plan_name,
            recipient="08030000000", reference=flows.new_reference("DTM"), description="d", cost_price=self.plan.cost_price)
        return tx, engine.dispatch(tx, flows.data_request(self.plan, "08030000000"))

    @mock.patch(GZ)
    @mock.patch(NB)
    def test_unverified_offer_is_ignored_entirely(self, nb, gz):
        nb.return_value = http_json({"status": "ORDER_RECEIVED", "orderid": "N-1"})
        tx, d = self.buy()
        self.assertEqual(d.kind, "accepted")
        gz.assert_not_called()
        self.assertEqual(Transaction.objects.get(pk=tx.pk).provider, "nellobytes")

    @mock.patch(GZ)
    @mock.patch(NB)
    def test_verified_cheaper_offer_wins_and_is_sent_under_its_own_code(self, nb, gz):
        PlanOffer.objects.update(verified=True)
        gz.return_value = http_json({"id": 77}, 201)
        tx, d = self.buy()
        self.assertEqual((d.kind, d.order_id), ("accepted", "data:77"))
        self.assertEqual(gz.call_args.kwargs["json"]["plan"], 290)       # Gongoz's code, not Nellobytes' 1000
        nb.assert_not_called()
        tx.refresh_from_db()
        self.assertEqual((tx.provider, tx.cost_from_klubconnect), ("gongoz", Decimal("500.00")))

    @mock.patch(GZ)
    @mock.patch(NB)
    def test_if_the_cheaper_provider_rejects_the_owner_takes_it_with_its_own_code(self, nb, gz):
        PlanOffer.objects.update(verified=True)
        gz.return_value = http_json({"detail": "x"}, 401)                # confirmed rejection, failover-safe
        nb.return_value = http_json({"status": "ORDER_RECEIVED", "orderid": "N-2"})
        tx, d = self.buy()
        self.assertEqual(d.kind, "accepted")
        self.assertEqual(nb.call_args.kwargs["params"]["DataPlan"], "1000")
        tx.refresh_from_db()
        self.assertEqual((tx.provider, tx.cost_from_klubconnect), ("nellobytes", Decimal("563.00")))
        self.assertEqual(wallet(self.user), Decimal("20000") - self.plan.price)

    @mock.patch(GZ)
    @mock.patch(NB)
    def test_a_timeout_at_the_cheaper_provider_never_double_buys_from_the_owner(self, nb, gz):
        PlanOffer.objects.update(verified=True)
        gz.side_effect = requests.ReadTimeout()
        tx, d = self.buy()
        self.assertEqual(d.kind, "unclear")
        nb.assert_not_called()                                           # the double-purchase guard still holds

    @mock.patch(GZ)
    @mock.patch(NB)
    def test_inactive_offer_stops_routing(self, nb, gz):
        PlanOffer.objects.update(verified=True, is_active=False)
        nb.return_value = http_json({"status": "ORDER_RECEIVED", "orderid": "N-3"})
        self.buy()
        gz.assert_not_called()


CATALOG = {
    "Dataplans": {"MTN_PLAN": {"ALL": [
        {"dataplan_id": "290", "plan_type": "SME", "plan": "1000.0 MB", "month_validate": "WEEKLY", "plan_amount": "480"},
        {"dataplan_id": "281", "plan_type": "CORPORATE GIFTING", "plan": "1.0 GB", "month_validate": "CORPORATE MONTHLY", "plan_amount": "905"},
    ]}},
    "Cableplan": {"GOTVPLAN": [{"cableplan_id": "93", "cable": "GOTV", "package": "GOtv Jinja", "plan_amount": "3850"}]},
    "topuppercentage": {"MTN": {"VTU": 97, "Share and Sell": 97}, "AIRTEL": {"VTU": 98}},
}
NELLO_CATALOG = {"MOBILE_NETWORK": {"MTN": [{"ID": "01", "PRODUCT": [
    {"PRODUCT_CODE": "1000", "PRODUCT_NAME": "MTN 1GB SME - 7 days", "PRODUCT_AMOUNT": "540"}]}]}}


@override_settings(PASSWORD_HASHERS=FAST_HASH)
class RefreshAndSuggestTests(GatewayTestCase):
    nello = True
    fakes = False
    env = {"VTU_GONGOZ_ENABLED": "true", "VTU_GONGOZ_API_KEY": KEY}

    def setUp(self):
        super().setUp()
        self.plan = DataPlan.objects.create(network="01", plan_name="MTN 1GB SME - 7 days", dataplan_id="1000",
                                            cost_price=Decimal("563"), price=Decimal("650"))
        self.cable = CablePlan.objects.create(cable_type="gotv", name="GOtv Jinja", plan_id="gotv-jinja",
                                              cost_price=Decimal("3900"), price=Decimal("4000"))

    def run_cmd(self, name, *args, gz=CATALOG, nb=NELLO_CATALOG):
        out = StringIO()
        with mock.patch(GZ, return_value=http_json(gz)), mock.patch(NB, return_value=http_json(nb)):
            call_command(name, *args, stdout=out)
        return out.getvalue()

    def test_suggest_saves_only_unambiguous_matches_as_unverified(self):
        out = self.run_cmd("suggest_plan_offers", "--save")
        self.assertIn("CHEAPER by N83", out)                           # 563 vs Gongoz 480
        po = PlanOffer.objects.get(data_plan=self.plan)
        self.assertEqual((po.provider, po.provider_code, po.cost_price, po.verified), ("gongoz", "290", Decimal("480"), False))
        cable = PlanOffer.objects.get(cable_plan=self.cable)
        self.assertEqual((cable.provider_code, cable.cost_price, cable.verified), ("93", Decimal("3850"), False))
        self.run_cmd("suggest_plan_offers", "--save")                  # idempotent
        self.assertEqual(PlanOffer.objects.count(), 2)

    def test_suggest_is_a_preview_without_save(self):
        out = self.run_cmd("suggest_plan_offers")
        self.assertIn("preview", out)
        self.assertEqual(PlanOffer.objects.count(), 0)

    def test_ambiguous_matches_are_listed_not_saved(self):
        dup = {"Dataplans": {"MTN_PLAN": {"ALL": [
            {"dataplan_id": "290", "plan_type": "SME", "plan": "1000.0 MB", "month_validate": "WEEKLY", "plan_amount": "480"},
            {"dataplan_id": "291", "plan_type": "SME", "plan": "1.0 GB", "month_validate": "7 DAYS", "plan_amount": "470"}]}}}
        out = self.run_cmd("suggest_plan_offers", "--save", gz=dup)
        self.assertIn("ambiguous", out)
        self.assertFalse(PlanOffer.objects.filter(data_plan=self.plan).exists())

    def test_refresh_updates_costs_only_never_prices(self):
        PlanOffer.objects.create(data_plan=self.plan, provider="gongoz", provider_code="290", cost_price=Decimal("999"), verified=True)
        out = self.run_cmd("refresh_provider_costs")
        self.plan.refresh_from_db()
        self.assertEqual((self.plan.cost_price, self.plan.price), (Decimal("540.00"), Decimal("650.00")))   # Nellobytes' new cost; price untouched
        self.assertEqual(PlanOffer.objects.get().cost_price, Decimal("480.00"))
        self.assertEqual(ProviderRate.objects.get(provider="gongoz", key="airtime:MTN").value, Decimal("97"))
        self.assertEqual(ProviderRate.objects.get(provider="gongoz", key="airtime:AIRTEL").value, Decimal("98"))
        self.assertIn("plan costs updated=1", out)

    def test_refresh_deactivates_dropped_offers_and_reactivates_returning_ones(self):
        po = PlanOffer.objects.create(data_plan=self.plan, provider="gongoz", provider_code="GONE", cost_price=Decimal("1"), verified=True)
        self.run_cmd("refresh_provider_costs")
        po.refresh_from_db()
        self.assertFalse(po.is_active)
        po.provider_code = "290"
        po.save()
        self.run_cmd("refresh_provider_costs")
        po.refresh_from_db()
        self.assertEqual((po.is_active, po.cost_price), (True, Decimal("480.00")))

    def test_a_manual_rate_is_never_overwritten(self):
        ProviderRate.objects.create(provider="gongoz", key="airtime:MTN", value=Decimal("90"), source="manual")
        self.run_cmd("refresh_provider_costs")
        self.assertEqual(ProviderRate.objects.get(provider="gongoz", key="airtime:MTN").value, Decimal("90"))

    def test_unreachable_provider_changes_nothing(self):
        out = StringIO()
        PlanOffer.objects.create(data_plan=self.plan, provider="gongoz", provider_code="290", cost_price=Decimal("999"), verified=True)
        with mock.patch(GZ, side_effect=requests.ConnectionError()), mock.patch(NB, side_effect=requests.ConnectionError()):
            call_command("refresh_provider_costs", stdout=out)
        self.plan.refresh_from_db()
        self.assertEqual((self.plan.cost_price, PlanOffer.objects.get().cost_price, PlanOffer.objects.get().is_active),
                         (Decimal("563.00"), Decimal("999.00"), True))
        self.assertIn("unreachable", out.getvalue())

    def test_dry_run_saves_nothing_and_loss_makers_are_flagged(self):
        DataPlan.objects.filter(pk=self.plan.pk).update(price=Decimal("500"))
        out = self.run_cmd("refresh_provider_costs", "--dry-run")
        self.plan.refresh_from_db()
        self.assertEqual(self.plan.cost_price, Decimal("563.00"))
        self.assertIn("sell at or below cost", out)
        self.assertEqual(ProviderRate.objects.count(), 0)


@override_settings(ALLOWED_HOSTS=['testserver'], PASSWORD_HASHERS=FAST_HASH)
class PricingAdminTests(GatewayTestCase):
    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_superuser('root', 'r@example.com', 'pw12345')
        self.client.login(username='root', password='pw12345')
        self.plan = DataPlan.objects.create(network="01", plan_name="p", dataplan_id="1", cost_price=Decimal("500"), price=Decimal("600"))
        self.offer = PlanOffer.objects.create(data_plan=self.plan, provider="gongoz", provider_code="2", cost_price=Decimal("450"))

    def test_pages_render(self):
        for path in ("planoffer", "providerrate", "dataplan"):
            self.assertEqual(self.client.get(f"/admin/vtu_app/{path}/").status_code, 200, path)
        self.assertEqual(self.client.get(f"/admin/vtu_app/dataplan/{self.plan.pk}/change/").status_code, 200)

    def test_verify_action_turns_the_offer_on(self):
        self.client.post("/admin/vtu_app/planoffer/", {"action": "mark_verified", "_selected_action": [self.offer.pk]})
        self.offer.refresh_from_db()
        self.assertTrue(self.offer.verified)
        self.assertEqual(offers.offer("gongoz", flows.data_request(self.plan, "0803")).code, "2")

    def test_a_rate_saved_in_the_admin_is_marked_manual(self):
        rate = ProviderRate.objects.create(provider="nellobytes", key="airtime:MTN", value=Decimal("96"), source="auto")
        self.client.post(f"/admin/vtu_app/providerrate/{rate.pk}/change/", {"provider": "nellobytes", "key": "airtime:MTN", "value": "95", "source": "auto"})
        rate.refresh_from_db()
        self.assertEqual((rate.value, rate.source), (Decimal("95"), "manual"))
