from django.core.management.base import BaseCommand

from vtu_app.gateway import engine


class Command(BaseCommand):
    help = (
        "Settle Pending VTU purchases (mobile app, website, developer API): asks the provider holding each "
        "order for its real status and finalises it exactly as a provider callback would. Only a confirmed "
        "provider answer changes anything - unknown/pending answers and query errors leave the order Pending "
        "(never auto-refunded). Schedule it every few minutes."
    )

    def add_arguments(self, parser):
        parser.add_argument("--older-than-minutes", type=int, default=3,
                            help="Only reconcile Pending orders older than this many minutes (default: 3).")

    def handle(self, *args, **options):
        checked, resolved = engine.reconcile_pending(options["older_than_minutes"])
        self.stdout.write(f"OK checked={checked} resolved={resolved}")
