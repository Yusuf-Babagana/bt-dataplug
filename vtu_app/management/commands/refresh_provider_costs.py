from django.core.management.base import BaseCommand
from django.db import transaction

from vtu_app.gateway import registry
from vtu_app.models import CablePlan, DataPlan, PlanOffer, ProviderRate


class Command(BaseCommand):
    help = (
        "Keep provider costs current so the router can pick the cheapest one. For every enabled provider it reads the "
        "provider's own catalogue and updates ONLY costs: plan cost_price (plans that provider owns), verified-offer costs, "
        "and published airtime rates. It never creates plans, never changes selling prices and never deletes anything. "
        "An unreachable provider is skipped (nothing is touched). Schedule it every 10-30 minutes."
    )

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Show what would change without saving")

    def handle(self, *args, **opts):
        dry = opts["dry_run"]
        for adapter in registry.all_adapters():
            if not adapter.config.enabled or not adapter.config.is_valid:
                continue
            plans = adapter.catalog_plans()
            rates = adapter.fetch_airtime_rates() or {}
            if plans is None and not rates:
                self.stdout.write(self.style.WARNING(f"{adapter.name}: catalogue unreachable or unsupported - left unchanged"))
                continue
            with transaction.atomic():
                stats = self.refresh(adapter.name, plans or [], rates, dry)
                if dry:
                    transaction.set_rollback(True)
            self.stdout.write(f"{adapter.name}: {stats}")

    def refresh(self, provider, plans, rates, dry):
        index = {(p.kind, p.network, p.code): p for p in plans}
        cost_changes = offer_changes = rate_changes = unlisted = 0
        loss = []

        for model, kind, net_field, code_field in ((DataPlan, "data", "network", "dataplan_id"),
                                                    (CablePlan, "cable", "cable_type", "plan_id")):
            if not plans:
                break
            for plan in model.objects.filter(source=provider):
                hit = index.get((kind, getattr(plan, net_field), getattr(plan, code_field)))
                if hit is None:
                    unlisted += 1
                    continue
                if plan.cost_price != hit.cost:
                    plan.cost_price = hit.cost
                    plan.save(update_fields=["cost_price"])
                    cost_changes += 1
                if plan.is_active and plan.price <= plan.cost_price:
                    loss.append(f"{plan} (price {plan.price} <= cost {plan.cost_price})")

        if plans:
            for offer in PlanOffer.objects.filter(provider=provider).select_related("data_plan", "cable_plan"):
                plan = offer.data_plan or offer.cable_plan
                if offer.data_plan_id:
                    key = ("data", plan.network, offer.provider_code)
                else:
                    key = ("cable", plan.cable_type, offer.provider_code)
                hit = index.get(key)
                if hit is None:
                    if offer.is_active:
                        offer.is_active = False         # provider stopped listing it: stop routing to it
                        offer.save(update_fields=["is_active"])
                        offer_changes += 1
                    continue
                if offer.cost_price != hit.cost or not offer.is_active:
                    offer.cost_price, offer.is_active = hit.cost, True
                    offer.save(update_fields=["cost_price", "is_active"])
                    offer_changes += 1

        for network, pct in rates.items():
            row = ProviderRate.objects.filter(provider=provider, key=f"airtime:{network}").first()
            if row is None:
                ProviderRate.objects.create(provider=provider, key=f"airtime:{network}", value=pct, source=ProviderRate.AUTO)
                rate_changes += 1
            elif row.source == ProviderRate.AUTO and row.value != pct:
                row.value = pct
                row.save(update_fields=["value"])
                rate_changes += 1

        if loss:
            self.stdout.write(self.style.WARNING(f"{provider}: {len(loss)} active plan(s) now sell at or below cost:"))
            for line in loss[:15]:
                self.stdout.write(f"   {line}")
        return (f"{'[dry run] ' if dry else ''}plan costs updated={cost_changes}, offer updates={offer_changes}, "
                f"airtime rates updated={rate_changes}, own plans no longer listed={unlisted}")
