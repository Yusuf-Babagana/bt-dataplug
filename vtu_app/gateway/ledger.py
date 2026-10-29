"""
Final-state changes for VTU purchase transactions.

The debit itself is ``TransactionService.process_debit`` (atomic, idempotent, audited). This module
applies a *confirmed* provider outcome to a PENDING transaction:

* PENDING -> Successful   (profit totals calculated, customer notified)
* PENDING -> Refunded     (wallet credited exactly once via ``TransactionService.process_refund``)

``settle`` is idempotent and race-safe: the Transaction row is locked, and anything that is not PENDING is
left alone. A provider reporting success AFTER we refunded is never applied automatically - it is logged
CRITICAL and recorded as an ANOMALY event for a human.
"""
import logging

from django.db import transaction as db

from vtu_app.models import Transaction
from vtu_app.notifications import notify
from vtu_app.services.transaction_service import TransactionService

from .types import OrderStatus

logger = logging.getLogger("vtu_app.gateway")

_listeners = []     # callables fn(tx, outcome) run after a transaction reaches a final state


def add_listener(fn):
    if fn not in _listeners:
        _listeners.append(fn)


def _kind(tx):
    st = (tx.service_type or "").lower()
    for key, label in (("data", "Data purchase"), ("airtime", "Airtime purchase"),
                       ("cable", "Cable purchase"), ("electricity", "Electricity purchase")):
        if st.startswith(key):
            return label
    return "Purchase"


def settle(transaction_id, outcome, source, detail=""):
    """Apply SUCCESSFUL / FAILED to a PENDING transaction. True only for the call that changed it."""
    with db.atomic():
        tx = Transaction.objects.select_for_update().select_related("user").filter(pk=transaction_id).first()
        if tx is None:
            logger.error("settle: transaction %s not found", transaction_id)
            return False

        if tx.status != "Pending":
            if outcome == OrderStatus.SUCCESSFUL and tx.status == "Refunded":
                # Provider says delivered after we refunded: money-losing, needs a human.
                logger.critical("VTU ANOMALY: txn %s is Refunded but provider reports SUCCESS", tx.reference)
                TransactionService.record_event(tx, tx.status, tx.status, source, "ANOMALY: provider reports success after refund")
            else:
                logger.info("settle: txn %s already %s - ignoring %s", tx.reference, tx.status, outcome)
            return False

        if outcome == OrderStatus.SUCCESSFUL:
            tx.status = "Successful"
            tx.save(update_fields=["status"])
            tx.calculate_totals()
            TransactionService.record_event(tx, "Pending", "Successful", source, detail or "provider confirmed")
        elif outcome == OrderStatus.FAILED:
            ok, res = TransactionService.process_refund(
                tx.user, tx.amount_customer_paid, tx.reference, detail or "provider confirmed failure", source=source)
            if not ok:
                logger.critical("VTU REFUND FAILED txn=%s reason=%s", tx.reference, res)
                db.set_rollback(True)       # leave it Pending so a later pass retries; never half-settle
                return False
            tx.refresh_from_db()
        else:
            logger.warning("settle: txn %s outcome %s is not final - left Pending", tx.reference, outcome)
            return False

    _after_final(tx, outcome)
    return True


def _after_final(tx, outcome):
    """Customer notification + listeners. Must never undo or block the status change."""
    try:
        kind = _kind(tx)
        if outcome == OrderStatus.SUCCESSFUL:
            notify(tx.user, f"{kind} successful", f"{tx.plan_name} for {tx.recipient} — ₦{tx.amount_customer_paid}.")
        else:
            notify(tx.user, f"{kind} failed",
                   f"{tx.plan_name} for {tx.recipient} could not be completed. ₦{tx.amount_customer_paid} was refunded to your wallet.")
    except Exception:
        logger.exception("post-settle notification failed for txn %s", tx.reference)
    for fn in list(_listeners):
        try:
            fn(tx, outcome)
        except Exception:
            logger.exception("settle listener %s failed for txn %s", getattr(fn, "__name__", fn), tx.reference)
