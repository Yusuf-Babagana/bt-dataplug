from django.db import migrations
from django.db.models import F


def mark_legacy_refunds(apps, schema_editor):
    """
    Before refunded_at existed, a refund set the transaction status to "Refunded". Mark those rows so the
    exactly-once refund guard (and any admin refund action) treats them as already refunded.
    """
    Transaction = apps.get_model("vtu_app", "Transaction")
    Transaction.objects.filter(status="Refunded", refunded_at__isnull=True).update(refunded_at=F("created_at"))


class Migration(migrations.Migration):

    dependencies = [
        ("vtu_app", "0024_vtu_gateway"),
    ]

    operations = [
        migrations.RunPython(mark_legacy_refunds, migrations.RunPython.noop),
    ]
