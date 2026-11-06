from decimal import ROUND_HALF_UP, Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from vtu_app.models import DataPlan

CENT = Decimal("0.01")

# Hand-picked, customer-attractive Gongoz plans: (network code, Gongoz dataplan_id, label, expected Gongoz cost).
# Network codes: 01 MTN, 02 Glo, 03 9mobile, 04 Airtel. (Smile is not a DataPlan network yet.)
CURATED = [
    # MTN
    ("01", "544", "500MB Weekly", "250"), ("01", "554", "1GB 7 days", "370"),
    ("01", "527", "2GB 30 days", "850"), ("01", "528", "3GB 30 days", "1100"),
    ("01", "529", "5GB Monthly", "1700"), ("01", "434", "10GB Monthly", "4343"),
    ("01", "461", "12.5GB Monthly", "5308"), ("01", "494", "20GB Monthly", "7238"),
    ("01", "580", "34GB 30 days", "9650"), ("01", "436", "75GB 30 days", "17370"),
    # Airtel
    ("04", "477", "1GB 7 days", "800"), ("04", "467", "2GB 30 days", "1500"),
    ("04", "468", "3GB 30 days", "2000"), ("04", "478", "4GB 30 days", "2500"),
    ("04", "470", "8GB 30 days", "3000"), ("04", "471", "10GB 30 days", "4000"),
    ("04", "472", "13GB 30 days", "5000"), ("04", "473", "18GB 30 days", "6000"),
    ("04", "474", "25GB 30 days", "8000"), ("04", "475", "35GB 30 days", "10000"),
    # Glo
    ("02", "321", "1GB Monthly", "395"), ("02", "322", "2GB Monthly", "790"),
    ("02", "323", "3GB Monthly", "1185"), ("02", "324", "5GB Monthly", "1975"),
    ("02", "325", "10GB Monthly", "3950"), ("02", "482", "3.5GB 30 days", "960"),
    ("02", "612", "5.2GB 30 days", "1500"), ("02", "613", "6.25GB 30 days", "2000"),
    ("02", "615", "10.5GB 30 days", "3000"), ("02", "618", "16.5GB 30 days", "5000"),
    # 9mobile
    ("03", "320", "1.1GB Monthly", "400"), ("03", "91", "1.6GB Monthly", "500"),
    ("03", "92", "2.3GB Monthly", "700"), ("03", "301", "4.5GB Monthly", "1600"),
    ("03", "302", "5GB Monthly", "1800"), ("03", "303", "10GB Monthly", "3000"),
    ("03", "328", "1GB Monthly", "425"), ("03", "330", "1.5GB Monthly", "633"),
    ("03", "331", "2GB Monthly", "844"), ("03", "334", "4GB Monthly", "1688"),
]


class Command(BaseCommand):
    help = (
        "Show customers only the curated, attractive Gongoz data plans: activates the plans in CURATED and "
        "deactivates every other active Gongoz data plan. Dry run unless --apply. Run gongoz_import_plans --apply "
        "first so the curated plans exist. Use --margin to (re)price curated plans at Gongoz cost + N%."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Write changes (default: preview only)")
        parser.add_argument("--margin", type=float, default=None,
                            help="Reset curated plans' selling price to cost + this percent (default: keep current price)")

    def handle(self, *args, **opts):
        apply, margin = opts["apply"], opts["margin"]
        factor = Decimal(str(1 + margin / 100)) if margin is not None else None
        keep, missing, cost_changed = set(), [], []

        with transaction.atomic():
            for net, pid, label, expected in CURATED:
                plan = DataPlan.objects.filter(network=net, dataplan_id=pid, source="gongoz").first()
                tag = f"{dict(DataPlan.NETWORK_CHOICES)[net]} #{pid} {label}"
                if not plan:
                    missing.append(tag)
                    continue
                keep.add(plan.pk)
                if plan.cost_price != Decimal(expected):
                    cost_changed.append(f"{tag}: expected cost {expected}, Gongoz now {plan.cost_price}")
                if apply:
                    plan.is_active = True
                    fields = ["is_active"]
                    if factor is not None:
                        plan.price = (plan.cost_price * factor).quantize(CENT, rounding=ROUND_HALF_UP)
                        fields.append("price")
                    plan.save(update_fields=fields)

            hide = DataPlan.objects.filter(source="gongoz", is_active=True).exclude(pk__in=keep)
            hidden = hide.count()
            if apply:
                hide.update(is_active=False)

        for line in cost_changed:
            self.stdout.write(self.style.WARNING("COST DIFFERS  " + line))
        for tag in missing:
            self.stdout.write(self.style.ERROR("NOT IMPORTED  " + tag))
        verb = "APPLIED" if apply else "PREVIEW (nothing written; add --apply)"
        self.stdout.write(f"{verb}: curated active={len(keep)}/{len(CURATED)}, other Gongoz plans hidden={hidden}, "
                          f"missing={len(missing)}, cost differs={len(cost_changed)}")
        if missing:
            self.stdout.write("Run `python manage.py gongoz_import_plans --apply` first, then re-run this command.")
