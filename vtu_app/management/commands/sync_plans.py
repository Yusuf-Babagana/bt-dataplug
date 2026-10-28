import json
import os
from decimal import Decimal, ROUND_CEILING

import requests
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from vtu_app.models import DataPlan

NETWORK_KEYS = {'MTN': '01', 'Glo': '02', 'm_9mobile': '03', 'Airtel': '04'}

# Nellobyte reports these MTN product codes as duplicated; skip them until fixed.
SKIP_CODES = {('01', c) for c in ('38', '39', '40', '41')}


class Command(BaseCommand):
    help = (
        "Sync data plans from Nellobyte's plan list (APIDatabundlePlansV2.asp), "
        "storing the PRODUCT_CODE in dataplan_id and switching off plans that are no longer listed."
    )

    def add_arguments(self, parser):
        parser.add_argument('--file', help='Path to the saved JSON from APIDatabundlePlansV2.asp (otherwise fetched live).')
        parser.add_argument('--markup', default='1.07', help='Selling price = supplier price x markup for NEW plans (default 1.07).')
        parser.add_argument('--dry-run', action='store_true', help='Show what would change without saving.')

    def load(self, path):
        if path:
            with open(path, encoding='utf-8-sig') as f:
                return json.load(f)
        user_id = os.getenv('NELLOBYTE_USER_ID', os.getenv('CK_USER_ID', '')).strip()
        base = os.getenv('NELLOBYTE_BASE_URL', 'https://www.nellobytesystems.com').strip().rstrip('/')
        r = requests.get(f'{base}/APIDatabundlePlansV2.asp?UserID={user_id}', timeout=30,
                         headers={'User-Agent': 'Mozilla/5.0 BT-DataPlug/1.0'})
        try:
            return r.json()
        except ValueError:
            raise CommandError('Plan list could not be fetched live; save it from the browser and pass --file.')

    def handle(self, *args, **opts):
        markup = Decimal(opts['markup'])
        data = self.load(opts['file']).get('MOBILE_NETWORK', {})

        seen = set()
        created = updated = 0
        with transaction.atomic():
            for key, net in NETWORK_KEYS.items():
                for block in data.get(key, []):
                    for p in block.get('PRODUCT', []):
                        code = str(p['PRODUCT_CODE']).strip()
                        if (net, code) in SKIP_CODES:
                            continue
                        cost = Decimal(str(p['PRODUCT_AMOUNT'])).quantize(Decimal('0.01'))
                        seen.add((net, code))
                        plan = DataPlan.objects.filter(network=net, dataplan_id=code).first()
                        if plan:
                            plan.plan_name = p['PRODUCT_NAME'].strip()[:100]
                            plan.cost_price = cost
                            plan.is_active = True
                            if plan.price <= cost:
                                plan.price = (cost * markup).to_integral_value(ROUND_CEILING)
                            if not opts['dry_run']:
                                plan.save()
                            updated += 1
                        else:
                            if not opts['dry_run']:
                                DataPlan.objects.create(
                                    network=net, dataplan_id=code,
                                    plan_name=p['PRODUCT_NAME'].strip()[:100],
                                    cost_price=cost,
                                    price=(cost * markup).to_integral_value(ROUND_CEILING),
                                )
                            created += 1

            # Anything still using an old-style plan ID (or unlisted) can no longer be bought.
            stale = [pl for pl in DataPlan.objects.filter(is_active=True) if (pl.network, pl.dataplan_id) not in seen]
            if not opts['dry_run']:
                DataPlan.objects.filter(pk__in=[pl.pk for pl in stale]).update(is_active=False)

            if opts['dry_run']:
                transaction.set_rollback(True)

        self.stdout.write(self.style.SUCCESS(
            f"{'[dry run] ' if opts['dry_run'] else ''}created {created}, updated {updated}, switched off {len(stale)} old plan(s)."
        ))
