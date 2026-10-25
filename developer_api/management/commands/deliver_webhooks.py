from django.core.management.base import BaseCommand

from developer_api import webhooks


class Command(BaseCommand):
    help = 'Retry webhook deliveries that are due. Schedule every few minutes (PythonAnywhere Tasks).'

    def handle(self, *args, **options):
        n = 0
        for delivery in webhooks.due_deliveries().select_related('application')[:200]:
            webhooks.deliver(delivery)
            n += 1
        self.stdout.write(f'Attempted {n} webhook deliveries.')
