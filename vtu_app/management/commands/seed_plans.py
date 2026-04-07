from django.core.management.base import BaseCommand
from vtu_app.models import DataPlan


class Command(BaseCommand):
    help = 'Seeds the database with initial data plans from ClubKonnect'

    def handle(self, *args, **kwargs):
        plans = [
            # MTN Plans (network code: 01)
            {'network': '01', 'plan_name': 'MTN 500MB SME',    'dataplan_id': '58',  'price': 147},
            {'network': '01', 'plan_name': 'MTN 1GB SME',      'dataplan_id': '59',  'price': 290},
            {'network': '01', 'plan_name': 'MTN 2GB SME',      'dataplan_id': '60',  'price': 575},
            {'network': '01', 'plan_name': 'MTN 3GB SME',      'dataplan_id': '61',  'price': 862},
            {'network': '01', 'plan_name': 'MTN 5GB SME',      'dataplan_id': '62',  'price': 1435},
            {'network': '01', 'plan_name': 'MTN 10GB SME',     'dataplan_id': '63',  'price': 2870},

            # Airtel Plans (network code: 04)
            {'network': '04', 'plan_name': 'Airtel 500MB',     'dataplan_id': '7',   'price': 155},
            {'network': '04', 'plan_name': 'Airtel 1GB',       'dataplan_id': '8',   'price': 305},
            {'network': '04', 'plan_name': 'Airtel 2GB',       'dataplan_id': '9',   'price': 605},
            {'network': '04', 'plan_name': 'Airtel 5GB',       'dataplan_id': '11',  'price': 1505},
            {'network': '04', 'plan_name': 'Airtel 10GB',      'dataplan_id': '13',  'price': 3005},

            # Glo Plans (network code: 02)
            {'network': '02', 'plan_name': 'Glo 500MB',        'dataplan_id': '19',  'price': 125},
            {'network': '02', 'plan_name': 'Glo 1GB',          'dataplan_id': '20',  'price': 245},
            {'network': '02', 'plan_name': 'Glo 2GB',          'dataplan_id': '21',  'price': 490},
            {'network': '02', 'plan_name': 'Glo 5GB',          'dataplan_id': '23',  'price': 1225},
            {'network': '02', 'plan_name': 'Glo 10GB',         'dataplan_id': '25',  'price': 2450},

            # 9mobile Plans (network code: 03)
            {'network': '03', 'plan_name': '9mobile 500MB',    'dataplan_id': '31',  'price': 130},
            {'network': '03', 'plan_name': '9mobile 1GB',      'dataplan_id': '32',  'price': 255},
            {'network': '03', 'plan_name': '9mobile 2GB',      'dataplan_id': '33',  'price': 505},
            {'network': '03', 'plan_name': '9mobile 5GB',      'dataplan_id': '35',  'price': 1255},
        ]

        created = 0
        skipped = 0
        for plan_data in plans:
            obj, was_created = DataPlan.objects.get_or_create(
                network=plan_data['network'],
                dataplan_id=plan_data['dataplan_id'],
                defaults={
                    'plan_name': plan_data['plan_name'],
                    'price': plan_data['price'],
                }
            )
            if was_created:
                created += 1
            else:
                skipped += 1

        self.stdout.write(self.style.SUCCESS(
            f'Done! Created {created} plans, skipped {skipped} existing plans.'
        ))
        self.stdout.write(self.style.WARNING(
            'IMPORTANT: Verify DataPlan IDs match your ClubKonnect account before going live!'
        ))
