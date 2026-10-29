from decimal import ROUND_HALF_UP, Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from vtu_app.gateway import registry
from vtu_app.models import CablePlan, DataPlan

DATA_NETWORKS = {"MTN_PLAN": "01", "GLO_PLAN": "02", "9MOBILE_PLAN": "03", "AIRTEL_PLAN": "04"}
CABLE_TYPES = {"DSTV": "dstv", "GOTV": "gotv", "STARTIME": "startimes", "STARTIMES": "startimes"}
CENT = Decimal("0.01")


class Command(BaseCommand):
    help = (
        "Make the app serve Gongoz data and cable plans: imports them from the Gongoz catalogue (GET /api/user/) "
        "as plans whose provider id IS the Gongoz id (source='gongoz'). Dry run unless --apply. New plans are priced "
        "at Gongoz cost + --margin %; existing imported plans keep their admin-set price (only name/cost refresh). "
        "Plans Gongoz dropped are deactivated. --deactivate-nellobytes hides the old Nellobytes plans so the app "
        "only lists Gongoz plans."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Write changes (default: preview only)")
        parser.add_argument("--margin", type=float, default=0.0,
                            help="Percent added to Gongoz cost for NEW plans' price (default 0 = sell at cost)")
        parser.add_argument("--exclude-types", default="TALKMORE", help="Gongoz plan types to skip (default: TALKMORE)")
        parser.add_argument("--types", default="", help="Only import these Gongoz plan types (comma-separated)")
        parser.add_argument("--deactivate-nellobytes", action="store_true",
                            help="Also set the existing Nellobytes data/cable plans inactive")

    def handle(self, *args, **opts):
        adapter = registry.get_adapter("gongoz")
        if not adapter.config.enabled or not adapter.config.is_valid:
            raise CommandError("Enable and configure Gongoz first (VTU_GONGOZ_ENABLED / VTU_GONGOZ_API_KEY).")
        catalog = adapter.fetch_catalog()
        if not catalog:
            raise CommandError("Could not read the Gongoz catalogue (check the token / connectivity).")

        exclude = {t.strip().upper() for t in opts["exclude_types"].split(",") if t.strip()}
        only = {t.strip().upper() for t in opts["types"].split(",") if t.strip()}
        factor = Decimal(str(1 + opts["margin"] / 100))
        apply = opts["apply"]

        data_rows = self.collect_data(catalog.get("Dataplans") or {}, exclude, only)
        cable_rows = self.collect_cable(catalog.get("Cableplan") or {})
        if not data_rows and not cable_rows:
            raise CommandError("Catalogue contained no plans; nothing changed.")

        with transaction.atomic():
            d = self.sync(DataPlan, data_rows, factor, apply, "network", "dataplan_id", "plan_name")
            c = self.sync(CablePlan, cable_rows, factor, apply, "cable_type", "plan_id", "name")
            hidden = 0
            if opts["deactivate_nellobytes"] and apply:
                hidden = (DataPlan.objects.filter(source="nellobytes", is_active=True).update(is_active=False)
                          + CablePlan.objects.filter(source="nellobytes", is_active=True).update(is_active=False))

        verb = "APPLIED" if apply else "PREVIEW (nothing written; add --apply)"
        self.stdout.write(f"{verb}: data new={d[0]} updated={d[1]} deactivated={d[2]} | "
                          f"cable new={c[0]} updated={c[1]} deactivated={c[2]} | nellobytes plans hidden={hidden}")
        if apply and factor == 1:
            self.stdout.write("NOTE: new plans are priced AT COST (margin 0). Edit prices in the admin or re-run with --margin.")

    def collect_data(self, data, exclude, only):
        rows = {}
        for key, code in DATA_NETWORKS.items():
            for p in (data.get(key) or {}).get("ALL", []):
                ptype = str(p.get("plan_type", "")).upper()
                if ptype in exclude or (only and ptype not in only):
                    continue
                try:
                    cost, pid = Decimal(str(p["plan_amount"])), str(p["dataplan_id"])
                except (KeyError, ArithmeticError):
                    continue
                if cost > 0:
                    name = f"{str(p.get('plan', '')).strip()} - {str(p.get('month_validate', '')).strip()} ({ptype.title()})"
                    rows[(code, pid)] = (name[:100], cost)
        return rows

    def collect_cable(self, cable):
        rows = {}
        for key, plans in cable.items():
            if not isinstance(plans, list):
                continue
            for p in plans:
                if "cableplan_id" not in p:
                    continue
                kind = CABLE_TYPES.get(str(p.get("cable", "")).upper())
                try:
                    cost = Decimal(str(p["plan_amount"]))
                except ArithmeticError:
                    continue
                if kind and cost > 0:
                    rows[(kind, str(p["cableplan_id"]))] = (str(p.get("package", "")).strip().strip('"').strip()[:100], cost)
        return rows

    def sync(self, model, rows, factor, apply, net_field, id_field, name_field):
        new = upd = 0
        seen = set()
        for (net, pid), (name, cost) in sorted(rows.items()):
            seen.add((net, pid))
            existing = model.objects.filter(**{net_field: net, id_field: pid, "source": "gongoz"}).first()
            if existing:
                upd += 1
                if apply:
                    setattr(existing, name_field, name)
                    existing.cost_price, existing.is_active = cost, True
                    existing.save(update_fields=[name_field, "cost_price", "is_active"])
            else:
                new += 1
                if apply:
                    model.objects.create(**{net_field: net, id_field: pid, name_field: name, "cost_price": cost,
                                            "price": (cost * factor).quantize(CENT, rounding=ROUND_HALF_UP),
                                            "source": "gongoz", "is_active": True})
        gone = 0
        for plan in model.objects.filter(source="gongoz", is_active=True):
            if (getattr(plan, net_field), getattr(plan, id_field)) not in seen:
                gone += 1
                if apply:
                    plan.is_active = False
                    plan.save(update_fields=["is_active"])
        return new, upd, gone
