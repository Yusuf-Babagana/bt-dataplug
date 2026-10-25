from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from developer_api.models import ApiRequestLog


class Command(BaseCommand):
    help = 'Delete API request logs older than --days (default 30) to keep the database small.'

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=30)

    def handle(self, *args, **options):
        n, _ = ApiRequestLog.objects.filter(created_at__lt=timezone.now() - timedelta(days=options['days'])).delete()
        self.stdout.write(f'Deleted {n} old log rows.')
