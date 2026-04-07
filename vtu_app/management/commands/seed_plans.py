from django.core.management.base import BaseCommand
from vtu_app.models import DataPlan


class Command(BaseCommand):
    help = 'Seeds the database with correct ClubKonnect data plans (clears old ones first)'

    def handle(self, *args, **kwargs):
        # Clear all existing plans to avoid duplicates with wrong IDs
        deleted, _ = DataPlan.objects.all().delete()
        self.stdout.write(self.style.WARNING(f'Cleared {deleted} old plan(s).'))

        plans = [
            # ── MTN SME Plans (network: 01) ──────────────────────────────────
            {'network': '01', 'plan_name': 'MTN 500MB SME - 7 days',   'dataplan_id': '500.0',    'price': 420},
            {'network': '01', 'plan_name': 'MTN 1GB SME - 7 days',     'dataplan_id': '1000.0',   'price': 590},
            {'network': '01', 'plan_name': 'MTN 2GB SME - 7 days',     'dataplan_id': '2000.0',   'price': 1180},
            {'network': '01', 'plan_name': 'MTN 3GB SME - 7 days',     'dataplan_id': '3000.0',   'price': 1750},
            {'network': '01', 'plan_name': 'MTN 5GB SME - 7 days',     'dataplan_id': '5000.0',   'price': 2650},

            # MTN Direct Data Monthly
            {'network': '01', 'plan_name': 'MTN 1GB Weekly',           'dataplan_id': '800.01',   'price': 850},
            {'network': '01', 'plan_name': 'MTN 1.5GB Weekly',         'dataplan_id': '1000.03',  'price': 1050},
            {'network': '01', 'plan_name': 'MTN 2GB Monthly',          'dataplan_id': '1500.02',  'price': 1600},
            {'network': '01', 'plan_name': 'MTN 3.5GB Monthly',        'dataplan_id': '2000.01',  'price': 2100},
            {'network': '01', 'plan_name': 'MTN 7GB Monthly',          'dataplan_id': '3500.02',  'price': 3650},
            {'network': '01', 'plan_name': 'MTN 10GB Monthly',         'dataplan_id': '4500.01',  'price': 4700},

            # MTN Awoof / Daily
            {'network': '01', 'plan_name': 'MTN 110MB Daily',          'dataplan_id': '100.01',   'price': 110},
            {'network': '01', 'plan_name': 'MTN 500MB Daily',          'dataplan_id': '350.01',   'price': 370},
            {'network': '01', 'plan_name': 'MTN 1GB Daily',            'dataplan_id': '500.01',   'price': 530},
            {'network': '01', 'plan_name': 'MTN 2.5GB Daily',          'dataplan_id': '750.01',   'price': 790},

            # ── Glo SME Plans (network: 02) ──────────────────────────────────
            {'network': '02', 'plan_name': 'Glo 200MB SME - 14 days',  'dataplan_id': '200',      'price': 100},
            {'network': '02', 'plan_name': 'Glo 500MB SME - 7 days',   'dataplan_id': '500',      'price': 250},
            {'network': '02', 'plan_name': 'Glo 1GB SME - 30 days',    'dataplan_id': '1000',     'price': 490},
            {'network': '02', 'plan_name': 'Glo 2GB SME - 30 days',    'dataplan_id': '2000',     'price': 980},
            {'network': '02', 'plan_name': 'Glo 3GB SME - 30 days',    'dataplan_id': '3000',     'price': 1470},
            {'network': '02', 'plan_name': 'Glo 5GB SME - 30 days',    'dataplan_id': '5000',     'price': 2450},
            {'network': '02', 'plan_name': 'Glo 10GB SME - 30 days',   'dataplan_id': '10000',    'price': 4900},

            # Glo Direct / Awoof
            {'network': '02', 'plan_name': 'Glo 1.5GB - 14 days',      'dataplan_id': '500.01',   'price': 530},
            {'network': '02', 'plan_name': 'Glo 2.6GB Monthly',        'dataplan_id': '1000.01',  'price': 1050},
            {'network': '02', 'plan_name': 'Glo 5GB Monthly',          'dataplan_id': '1500.01',  'price': 1600},
            {'network': '02', 'plan_name': 'Glo 6GB Monthly',          'dataplan_id': '2000.01',  'price': 2100},
            {'network': '02', 'plan_name': 'Glo 10GB Monthly',         'dataplan_id': '3000.01',  'price': 3150},

            # ── Airtel Plans (network: 04) ────────────────────────────────────
            # Airtel Awoof / Daily
            {'network': '04', 'plan_name': 'Airtel 1GB Daily',         'dataplan_id': '499.91',   'price': 530},
            {'network': '04', 'plan_name': 'Airtel 2GB - 2 days',      'dataplan_id': '749.91',   'price': 800},
            {'network': '04', 'plan_name': 'Airtel 3GB - 2 days',      'dataplan_id': '999.91',   'price': 1060},
            {'network': '04', 'plan_name': 'Airtel 5GB - 2 days',      'dataplan_id': '1499.91',  'price': 1590},

            # Airtel Weekly Direct
            {'network': '04', 'plan_name': 'Airtel 500MB Weekly',      'dataplan_id': '499.92',   'price': 530},
            {'network': '04', 'plan_name': 'Airtel 1GB Weekly',        'dataplan_id': '799.91',   'price': 850},
            {'network': '04', 'plan_name': 'Airtel 3.5GB Weekly',      'dataplan_id': '1499.92',  'price': 1590},

            # Airtel Monthly
            {'network': '04', 'plan_name': 'Airtel 2GB Monthly',       'dataplan_id': '1499.93',  'price': 1590},
            {'network': '04', 'plan_name': 'Airtel 3GB Monthly',       'dataplan_id': '1999.91',  'price': 2100},
            {'network': '04', 'plan_name': 'Airtel 8GB Monthly',       'dataplan_id': '2999.92',  'price': 3150},
            {'network': '04', 'plan_name': 'Airtel 10GB Monthly',      'dataplan_id': '3999.91',  'price': 4200},

            # ── 9mobile Plans (network: 03) ───────────────────────────────────
            {'network': '03', 'plan_name': '9mobile 100MB SME',        'dataplan_id': '100',      'price': 50},
            {'network': '03', 'plan_name': '9mobile 300MB SME',        'dataplan_id': '300',      'price': 145},
            {'network': '03', 'plan_name': '9mobile 500MB SME',        'dataplan_id': '500',      'price': 240},
            {'network': '03', 'plan_name': '9mobile 1GB SME',          'dataplan_id': '1000',     'price': 475},
            {'network': '03', 'plan_name': '9mobile 2GB SME',          'dataplan_id': '2000',     'price': 950},
            {'network': '03', 'plan_name': '9mobile 5GB SME',          'dataplan_id': '5000',     'price': 2360},
            {'network': '03', 'plan_name': '9mobile 10GB SME',         'dataplan_id': '10000',    'price': 4720},

            # 9mobile Awoof / Direct
            {'network': '03', 'plan_name': '9mobile 100MB Daily',      'dataplan_id': '100.01',   'price': 110},
            {'network': '03', 'plan_name': '9mobile 650MB - 3 days',   'dataplan_id': '500.01',   'price': 530},
            {'network': '03', 'plan_name': '9mobile 1.1GB Monthly',    'dataplan_id': '1000.01',  'price': 1060},
            {'network': '03', 'plan_name': '9mobile 2.44GB Monthly',   'dataplan_id': '2000.01',  'price': 2100},
            {'network': '03', 'plan_name': '9mobile 6.5GB Monthly',    'dataplan_id': '5000.01',  'price': 5250},
        ]

        created = 0
        for plan_data in plans:
            DataPlan.objects.create(
                network=plan_data['network'],
                plan_name=plan_data['plan_name'],
                dataplan_id=plan_data['dataplan_id'],
                price=plan_data['price'],
            )
            created += 1

        self.stdout.write(self.style.SUCCESS(
            f'✅ Done! Created {created} data plans across MTN, Glo, Airtel and 9mobile.'
        ))
        self.stdout.write(self.style.WARNING(
            '⚠️  Prices are estimates. Update them via /admin/ to set your actual selling prices.'
        ))
