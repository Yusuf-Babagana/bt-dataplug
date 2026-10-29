"""
Order engine: route -> submit -> (failover only when provably safe) -> settle/refund.
Provider specifics never appear here.

Callers (mobile API, website, developer API) first debit the wallet with ``TransactionService.process_debit``
and then call ``dispatch``. Failover rule (the money-safety core): the next provider is tried ONLY after the
previous one returned REJECTED for a provider-side reason (``ProviderResult.failover_safe``). ACCEPTED stops
the loop; AMBIGUOUS (timeout, dropped connection, odd reply) also stops it and leaves the transaction Pending
for reconciliation, because the order may exist. Funds are refunded only after every attempt is a confirmed
REJECTED, or a provider confirms the order failed.
"""
import logging
from dataclasses import dataclass, field
from datetime import timedelta

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from vtu_app.models import CablePlan, DataPlan, PurchaseAttempt, Transaction
from vtu_app.services.transaction_service import TransactionService

from . import health, ledger, offers, registry, router
from .types import ErrorCategory, OrderStatus, Outcome, ProviderResult, PurchaseRequest, Service

logger = logging.getLogger("vtu_app.gateway")

MAINTENANCE_MESSAGE = "This service is temporarily under maintenance. Please try again shortly."
BUSY_MESSAGE = "Service temporarily unavailable. Please try again."
# A Pending transaction with no live attempt is only refunded after this long, so an order still
# mid-failover in another worker is never mistaken for an orphan.
ORPHAN_GRACE = timedelta(minutes=10)
PURCHASE_PREFIXES = ("Data", "Airtime", "Cable", "Electricity")


@dataclass
class Dispatch:
    kind: str                       # accepted | unclear | refunded | rejected
    order_id: str = ""
    extra: dict = field(default_factory=dict)
    message: str = ""               # customer-safe text for failures
    http_status: int = 200
    category: str = ""
    code: str = ""


def check_available(template: PurchaseRequest):
    """Run BEFORE debiting: returns None when some provider can serve this, else (http_status, message)."""
    selection = router.select(template)
    if not selection.supporting:
        return 400, f"This service is not available for {template.network or 'this selection'} right now."
    if not selection.adapters:
        return 503, MAINTENANCE_MESSAGE
    return None


def dispatch(tx: Transaction, template: PurchaseRequest, auto_refund=True) -> Dispatch:
    """
    Submit the (already debited) transaction. With ``auto_refund`` a total rejection refunds the wallet here;
    callers that own their own settlement (developer API) pass False and settle from the returned ``rejected``.
    """
    selection = router.select(template)
    base_ref = tx.reference
    last = None
    for seq, adapter in enumerate(selection.adapters, start=1):
        result = _attempt(tx, adapter, template, base_ref, seq)
        last = result

        if result.outcome == Outcome.ACCEPTED:
            health.record_success(adapter.name)
            _attach(tx, adapter.name, result.provider_order_id, _cost(adapter, template))
            TransactionService.record_event(tx, "Pending", "Pending", "purchase", f"accepted by {adapter.name}")
            return Dispatch("accepted", order_id=result.provider_order_id, extra=result.extra, code=result.code)

        if result.outcome == Outcome.AMBIGUOUS:
            # May already be processed - never fail over, never refund. Reconciliation decides.
            health.record_failure(adapter.name, result.category, result.message)
            _attach(tx, adapter.name, result.provider_order_id, _cost(adapter, template))
            TransactionService.record_event(tx, "Pending", "Pending", "purchase",
                                            f"{adapter.name} outcome unclear ({result.category}); awaiting reconciliation")
            logger.warning("vtu ambiguous: ref=%s provider=%s category=%s", base_ref, adapter.name, result.category)
            return Dispatch("unclear", order_id=result.provider_order_id, category=result.category, code=result.code)

        # REJECTED: the provider confirmed nothing was processed.
        _on_rejection(template, adapter, result)
        if not result.failover_safe:
            break
        logger.warning("vtu failover: ref=%s provider=%s rejected (%s); trying next", base_ref, adapter.name, result.category)

    if last is None:        # nothing eligible (everything went down between preflight and now)
        last = ProviderResult(Outcome.REJECTED, ErrorCategory.MAINTENANCE, "NO_PROVIDER", MAINTENANCE_MESSAGE)

    message, http_status = _customer_message(last)
    detail = f"all providers rejected; last={last.code or last.category}"
    if auto_refund:
        ledger.settle(tx.pk, OrderStatus.FAILED, "purchase", detail)
        return Dispatch("refunded", message=message, http_status=http_status, category=last.category, code=last.code)
    return Dispatch("rejected", message=message, http_status=http_status, category=last.category, code=last.code)


def _on_rejection(template, adapter, result):
    if result.category == ErrorCategory.INVALID_REQUEST:
        health.record_success(adapter.name)     # it answered correctly; the request is the problem
    else:
        health.record_failure(adapter.name, result.category, result.message)
    if template.service == Service.DATA and template.plan_pk and (result.code or "").upper() == "INVALID_DATAPLAN":
        # The supplier refuses this plan code: stop selling it until an admin re-enables it.
        DataPlan.objects.filter(pk=template.plan_pk).update(is_active=False)
        logger.error("DATA_PLAN_DISABLED plan=%s provider=%s", template.plan_pk, adapter.name)


def _customer_message(last: ProviderResult):
    """Only an invalid-request reason is shown to the customer; provider-side detail stays in the logs."""
    if last.category == ErrorCategory.INVALID_REQUEST:
        return (last.message or "Invalid request")[:160], 400
    if last.category == ErrorCategory.PROVIDER_BALANCE:
        return MAINTENANCE_MESSAGE, 503     # our float at the provider ran dry: not the customer's concern
    return BUSY_MESSAGE, 502


def _cost(adapter, template):
    """What the provider that took the order charges us for it (None = unknown)."""
    try:
        return adapter.quote(template)
    except Exception:
        return None


def _attach(tx, provider, order_id="", cost=None):
    fields = {"provider": provider}
    if order_id:
        fields["order_id"] = order_id
    if cost is not None:
        fields["cost_from_klubconnect"] = cost      # so profit reports use the provider that actually fulfilled it
    Transaction.objects.filter(pk=tx.pk).update(**fields)
    tx.provider = provider
    if order_id:
        tx.order_id = order_id
    if cost is not None:
        tx.cost_from_klubconnect = cost


def _attempt(tx, adapter, template, base_ref, seq) -> ProviderResult:
    reference = base_ref if seq == 1 else f"{base_ref}-{seq}"
    request = PurchaseRequest(**{**template.__dict__, "reference": reference})
    request.callback_url = _callback_url(adapter)
    chosen = offers.offer(adapter.name, template)       # each provider sells the plan under ITS OWN code
    if chosen is not None:
        request.plan_id = chosen.code

    # Recorded BEFORE the network call: a crash mid-call still leaves a SUBMITTING row, which
    # reconciliation treats as a possibly-live order.
    attempt = PurchaseAttempt.objects.create(transaction=tx, provider=adapter.name, reference=reference, sequence=seq)
    try:
        result = adapter.purchase(request)
    except Exception:
        logger.exception("vtu adapter %s crashed submitting ref=%s", adapter.name, reference)
        result = ProviderResult(Outcome.AMBIGUOUS, ErrorCategory.UNKNOWN, "ADAPTER_ERROR", "Adapter error")

    PurchaseAttempt.objects.filter(pk=attempt.pk).update(
        outcome=result.outcome, error_category=result.category, code=result.code[:100],
        message=result.message[:255], provider_order_id=result.provider_order_id[:100],
        response=result.raw, elapsed_ms=result.elapsed_ms,
    )
    logger.info("vtu attempt: ref=%s provider=%s outcome=%s category=%s ms=%s",
                reference, adapter.name, result.outcome, result.category, result.elapsed_ms)
    return result


def _callback_url(adapter):
    base = getattr(settings, "PUBLIC_BASE_URL", "").rstrip("/")
    return f"{base}{adapter.callback_url_path()}" if base else ""


# ---------------------------------------------------------------------------
# Resolution: webhook / polling / sweep all converge on ledger.settle()
# ---------------------------------------------------------------------------

def _live_attempts(tx):
    return [a for a in tx.attempts.all()
            if a.outcome in (Outcome.ACCEPTED, Outcome.AMBIGUOUS, PurchaseAttempt.SUBMITTING)]


def _is_managed(tx):
    """Created by the gateway-era debit (has the 'purchase' audit event). Older rows are left to staff."""
    return tx.events.filter(source="purchase").exists()


def reconcile_transaction(tx, source="reconcile"):
    """
    Ask the provider(s) holding a possibly-live order what happened, and settle only on a definite answer.
    Never refunds on UNKNOWN/PENDING/query errors. Returns the transaction's resulting status.
    """
    tx.refresh_from_db()
    if tx.status != "Pending":
        return tx.status

    live = _live_attempts(tx)
    if not live and tx.order_id:
        live = [PurchaseAttempt(provider=tx.provider or "nellobytes", reference=tx.reference or "", provider_order_id=tx.order_id)]

    if not live:
        # No submission ever reached a provider (or every one was a confirmed rejection) and a worker died
        # before refunding. Safe to refund once it is clearly not mid-flight - but only for transactions this
        # gateway created, never for older Pending rows whose real outcome we cannot know.
        if _is_managed(tx) and timezone.now() - tx.created_at > ORPHAN_GRACE:
            ledger.settle(tx.pk, OrderStatus.FAILED, source, "no provider submission recorded; refunded")
        tx.refresh_from_db()
        return tx.status

    statuses = []
    for attempt in live:
        adapter = registry.get_adapter(attempt.provider)
        if adapter is None:
            statuses.append(OrderStatus.UNKNOWN)
            continue
        try:
            res = adapter.query_status(attempt.provider_order_id, attempt.reference)
        except Exception:
            logger.exception("vtu query_status crashed: provider=%s ref=%s", attempt.provider, attempt.reference)
            statuses.append(OrderStatus.UNKNOWN)
            continue
        statuses.append(res.status)

    if OrderStatus.SUCCESSFUL in statuses:
        ledger.settle(tx.pk, OrderStatus.SUCCESSFUL, source)
    elif statuses and all(s == OrderStatus.FAILED for s in statuses):
        ledger.settle(tx.pk, OrderStatus.FAILED, source)
    tx.refresh_from_db()
    return tx.status


def purchase_queryset():
    q = Q()
    for prefix in PURCHASE_PREFIXES:
        q |= Q(service_type__startswith=prefix)
    return Transaction.objects.filter(q, status="Pending")


def reconcile_pending(older_than_minutes=3):
    """Sweep Pending purchases. Returns (checked, resolved)."""
    cutoff = timezone.now() - timedelta(minutes=older_than_minutes)
    checked = resolved = 0
    for tx in purchase_queryset().filter(created_at__lt=cutoff):
        checked += 1
        if reconcile_transaction(tx, "reconcile") != "Pending":
            resolved += 1
    return checked, resolved


def reconcile_user_pending(user, limit=3, min_age_seconds=10):
    """Self-healing on read: settle this user's few most recent Pending purchases (history screens call this)."""
    cutoff = timezone.now() - timedelta(seconds=min_age_seconds)
    for tx in list(purchase_queryset().filter(user=user, created_at__lt=cutoff).order_by("-created_at")[:limit]):
        try:
            reconcile_transaction(tx, "status-poll")
        except Exception:
            logger.exception("lazy reconcile failed for txn %s", tx.pk)


def handle_webhook(adapter, request):
    """
    Returns ``(http_status, body, log_outcome, detail)``. A callback is a *hint*: unless the adapter
    cryptographically verified it, the real status is fetched from the provider.
    """
    from .base import WebhookRejected

    try:
        event = adapter.parse_webhook(request)
    except WebhookRejected as e:
        return 401, {"status": "rejected"}, "signature_rejected", str(e)[:200]

    ref = f"order_id={event.provider_order_id} request_id={event.reference}"
    attempt = None
    if event.reference:
        attempt = PurchaseAttempt.objects.filter(provider=adapter.name, reference=event.reference).select_related("transaction").first()
    if attempt is None and event.provider_order_id:
        attempt = PurchaseAttempt.objects.filter(provider=adapter.name, provider_order_id=event.provider_order_id).select_related("transaction").first()
    tx = attempt.transaction if attempt else None
    if tx is None:
        if event.provider_order_id:
            tx = Transaction.objects.filter(order_id=event.provider_order_id).first()
        if tx is None and event.reference:
            tx = Transaction.objects.filter(reference=event.reference).first()
    if tx is None:
        return 404, {"status": "not_found"}, "error", f"No matching transaction - {ref}"

    if tx.status != "Pending":
        return 200, {"status": "already_resolved"}, "duplicate_ignored", f"status={tx.status} - {ref}"

    if event.verified and event.claimed_status in (OrderStatus.SUCCESSFUL, OrderStatus.FAILED):
        ledger.settle(tx.pk, event.claimed_status, "webhook")
        return 200, {"status": "acknowledged"}, "resolved", f"outcome={event.claimed_status} (verified) - {ref}"

    status = reconcile_transaction(tx, "webhook")
    if status == "Pending":
        return 200, {"status": "acknowledged"}, "resolved", f"still pending after provider query - {ref}"
    return 200, {"status": "acknowledged"}, "resolved", f"outcome={status} - {ref}"
