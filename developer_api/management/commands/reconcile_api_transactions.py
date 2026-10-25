from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from developer_api import services
from developer_api.models import ApiTransaction


class Command(BaseCommand):
    help = ('Settle pending developer-API orders. Live orders are looked up at the provider; orders stuck in "processing" '
            '(server died mid-request) are moved to pending for review; sandbox pending orders resolve to success.')

    def handle(self, *args, **options):
        now = timezone.now()
        # 1. Requests that crashed after accepting the order: never auto-refund, make them visible and reconcilable.
        stuck = ApiTransaction.objects.filter(status='processing', updated_at__lt=now - timedelta(minutes=5))
        stuck_n = 0
        for tx in stuck:
            if tx.is_test or tx.core_reference:
                tx.status = 'pending'
                tx.save(update_fields=['status', 'updated_at'])
                stuck_n += 1
            else:
                tx.delete()  # never debited: nothing to settle
        # 2. Pending live orders: ask the provider (skip the very freshest, the original request may still be running).
        checked = 0
        for tx in ApiTransaction.objects.filter(status='pending', is_test=False, updated_at__lt=now - timedelta(minutes=2),
                                                reconcile_attempts__lt=50):
            services.reconcile(tx)
            checked += 1
        # 3. Failed orders whose refund did not complete: retry the refund.
        refunds = 0
        for tx in ApiTransaction.objects.filter(status='failed', refunded=False, is_test=False).exclude(core_reference=''):
            services.settle(tx.pk, 'failed')
            refunds += 1
        # 4. Sandbox: orders left pending resolve to success after a minute so webhook flows can be tested.
        for tx in ApiTransaction.objects.filter(status='pending', is_test=True, updated_at__lt=now - timedelta(minutes=1)):
            services.settle(tx.pk, 'successful', {'status': 'SIMULATED_SUCCESS'})
        self.stdout.write(f'stuck->pending: {stuck_n}, provider checks: {checked}, refund retries: {refunds}')
