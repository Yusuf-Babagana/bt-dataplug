from django.core.management.base import BaseCommand

from vtu_app.gateway import registry
from vtu_app.gateway.catalog import match_cable, match_data
from vtu_app.models import CablePlan, DataPlan, PlanOffer


class Command(BaseCommand):
    help = (
        "Suggest which of the OTHER providers' plans are the same product as each of your plans, so the router can use "
        "the cheapest (or fall back to a second provider). Matching is conservative: same network, same size, same "
        "validity and a compatible family; cable by package name. Only an unambiguous single match is saved with --save, "
        "and saved offers are UNVERIFIED and unused until you tick 'verified' in the admin (Plan offers) after checking them."
    )

    def add_arguments(self, parser):
        parser.add_argument("--save", action="store_true", help="Save unambiguous suggestions as unverified offers")
        parser.add_argument("--provider", help="Only suggest offers from this provider")
        parser.add_argument("--network", help="Only data plans of this network code (01 MTN, 02 Glo, 03 9mobile, 04 Airtel)")

    def handle(self, *args, **opts):
        catalogs = {}
        for adapter in registry.all_adapters():
            if not adapter.config.enabled or not adapter.config.is_valid:
                continue
            if opts["provider"] and adapter.name != opts["provider"]:
                continue
            plans = adapter.catalog_plans()
            if plans is None:
                self.stdout.write(self.style.WARNING(f"{adapter.name}: catalogue unreachable or unsupported - skipped"))
                continue
            catalogs[adapter.name] = plans
        if not catalogs:
            self.stdout.write("No provider catalogue available.")
            return

        saved = suggested = ambiguous = 0
        data_qs = DataPlan.objects.filter(is_active=True)
        if opts["network"]:
            data_qs = data_qs.filter(network=opts["network"])
        jobs = [(p, "data_plan", p.plan_name, lambda c, p=p: match_data(p.plan_name, p.network, c)) for p in data_qs]
        if not opts["network"]:
            jobs += [(p, "cable_plan", p.name, lambda c, p=p: match_cable(p.name, p.cable_type, c, p.cost_price))
                     for p in CablePlan.objects.filter(is_active=True)]

        for plan, field, label, matcher in jobs:
            for provider, catalog in catalogs.items():
                if provider == plan.source:
                    continue
                if PlanOffer.objects.filter(provider=provider, **{field: plan}).exists():
                    continue
                matches = matcher(catalog)
                if not matches:
                    continue
                if len(matches) > 1:
                    ambiguous += 1
                    self.stdout.write(f"? {label} [{provider}] ambiguous: " + "; ".join(f"{m.code}={m.name} N{m.cost}" for m in matches[:4]))
                    continue
                m = matches[0]
                suggested += 1
                delta = plan.cost_price - m.cost
                tag = f"CHEAPER by N{delta}" if delta > 0 else ("same cost" if delta == 0 else f"dearer by N{-delta} (backup only)")
                self.stdout.write(f"+ {label} (cost N{plan.cost_price}) ~ [{provider}] {m.code}: {m.name} N{m.cost} - {tag}")
                if opts["save"]:
                    PlanOffer.objects.create(**{field: plan}, provider=provider, provider_code=m.code, cost_price=m.cost,
                                             verified=False, note=f"matched '{m.name}'"[:200])
                    saved += 1
        self.stdout.write(f"suggested={suggested} saved={saved} ambiguous={ambiguous}"
                          + ("" if opts["save"] else "  (preview; add --save, then verify in the admin: Plan offers)"))
