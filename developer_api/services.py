"""
Purchase engine for the developer API.

Reuses the existing wallet engine (TransactionService) and the provider-agnostic VTU gateway
(vtu_app.gateway: routing, safe failover, reconciliation).
Money rules:
  * prices are always computed on the server from DataPlan/CablePlan rows or fixed rules, never from the request;
  * the wallet is debited atomically BEFORE the provider is called;
  * a refund happens only when every provider has clearly rejected the order (never on a timeout or unknown answer);
  * a provider that merely ACCEPTED the order leaves it `pending`: it becomes `successful` only when the provider
    confirms completion (webhook, status poll, or `reconcile_api_transactions` / `reconcile_vtu_orders`);
  * unknown/ambiguous outcomes stay `pending` and are settled later by reconciliation or by staff.
"""
import hashlib
import json
import logging
import re
import uuid
from decimal import Decimal, InvalidOperation

from django.db import IntegrityError, transaction

from vtu_app.models import CablePlan, DataPlan, ServiceSwitch, Transaction as CoreTx, WalletTransaction, Profile
from vtu_app.gateway import engine, flows, ledger, registry
from vtu_app.gateway.types import OrderStatus
from vtu_app.services.transaction_service import TransactionService

from .errors import ApiError, validation_error
from .models import ApiTransaction
from . import webhooks

logger = logging.getLogger('vtu_app')

NETWORKS = {'01': 'MTN', '02': 'Glo', '03': '9mobile', '04': 'Airtel'}
NETWORK_ALIASES = {'MTN': '01', 'GLO': '02', '9MOBILE': '03', 'AIRTEL': '04'}
DISCOS = {
    '01': 'EKEDC (Eko)', '02': 'IKEDC (Ikeja)', '03': 'AEDC (Abuja)', '04': 'KEDCO (Kano)',
    '05': 'PHEDC (Port Harcourt)', '06': 'JEDC (Jos)', '07': 'IBEDC (Ibadan)', '08': 'EEDC (Enugu)',
    '09': 'KAEDCO (Kaduna)', '10': 'BEDC (Benin)', '11': 'YEDC (Yola)',
}
CABLE_TYPES = ['dstv', 'gotv', 'startimes']
PHONE_RE = re.compile(r'^0[789][01]\d{8}$')

# Same commercial rules as the mobile app (api_views.py): airtime customer pays 99%, we pay 97%; electricity fee N100.
AIRTIME_SELL, AIRTIME_COST = Decimal('0.99'), Decimal('0.97')
ELECTRICITY_FEE = Decimal('100.00')
SERVICE_LABEL = {'airtime': 'Airtime Purchase', 'data': 'Data Purchase', 'electricity': 'Electricity Bill', 'cable': 'Cable TV'}

# ---------------------------------------------------------------------------------------------------
# Serialising
# ---------------------------------------------------------------------------------------------------
def serialize_transaction(tx):
    return {
        'reference': tx.reference,
        'client_reference': tx.client_reference or None,
        'service': tx.service,
        'description': tx.description,
        'recipient': tx.recipient,
        'amount': str(tx.amount),
        'status': 'pending' if tx.status == 'processing' else tx.status,
        'failure_reason': tx.failure_reason or None,
        'environment': 'test' if tx.is_test else 'live',
        'result': {k: v for k, v in (tx.result or {}).items() if not k.startswith('_')},
        'created_at': tx.created_at.isoformat(),
        'updated_at': tx.updated_at.isoformat(),
    }


# ---------------------------------------------------------------------------------------------------
# Validation -> Order
# ---------------------------------------------------------------------------------------------------
class Order:
    def __init__(self, service, description, recipient, charge, cost, template, fingerprint, extra_fee=Decimal('0.00'), network_name=None):
        self.service, self.description, self.recipient = service, description, recipient
        self.charge, self.cost, self.template, self.extra_fee = charge, cost, template, extra_fee
        self.fingerprint = fingerprint
        self.network_name = network_name
        self.plan = None  # the DataPlan row, for data orders


def _text(data, name, required=True, maxlen=64):
    value = data.get(name)
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise validation_error(**{name: 'This field is required.'})
        return ''
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise validation_error(**{name: 'Must be a string.'})
    value = str(value).strip()
    if len(value) > maxlen:
        raise validation_error(**{name: f'Must be at most {maxlen} characters.'})
    return value


def _phone(data, name='phone'):
    phone = _text(data, name)
    if not PHONE_RE.match(phone):
        raise validation_error(**{name: 'Must be an 11-digit Nigerian number, e.g. 08012345678.'})
    return phone


def _amount(data, minimum, maximum):
    raw = data.get('amount')
    if raw is None or isinstance(raw, bool):
        raise validation_error(amount='This field is required.')
    try:
        amount = Decimal(str(raw))
    except InvalidOperation:
        raise validation_error(amount='Must be a number.')
    if not amount.is_finite() or amount != amount.quantize(Decimal('0.01')):
        raise validation_error(amount='Use at most 2 decimal places.')
    if amount < minimum or amount > maximum:
        raise validation_error(amount=f'Must be between {minimum} and {maximum}.')
    return amount


def _gather(**checks):
    """Run every field check and report all invalid fields together (not just the first)."""
    values, errors = {}, {}
    for name, check in checks.items():
        try:
            values[name] = check()
        except ApiError as exc:
            if exc.code != 'validation_error':
                raise
            errors.update(exc.details or {})
    if errors:
        raise ApiError(422, 'validation_error', 'One or more fields are invalid.', details=errors)
    return values


def _fingerprint(service, fields):
    blob = json.dumps({'service': service, **fields}, sort_keys=True, separators=(',', ':'), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def _check_network_enabled(name):
    switch = ServiceSwitch.objects.filter(network=name).first()
    if switch and not switch.is_active:
        raise ApiError(503, 'service_unavailable', f'{name} purchases are temporarily unavailable.')


def build_order(service, data):
    if service == 'airtime':
        net = _text(data, 'network').upper()
        code = NETWORK_ALIASES.get(net) or (net if net in NETWORKS else None)
        if not code:
            raise validation_error(network='Use MTN, GLO, 9MOBILE or AIRTEL.')
        v = _gather(phone=lambda: _phone(data), amount=lambda: _amount(data, Decimal('50'), Decimal('50000')))
        phone, amount = v['phone'], v['amount']
        _check_network_enabled(NETWORKS[code])
        charge = (amount * AIRTIME_SELL).quantize(Decimal('0.01'))
        cost = (amount * AIRTIME_COST).quantize(Decimal('0.01'))
        return Order('airtime', f'{NETWORKS[code]} airtime N{amount}', phone, charge, cost,
                     flows.airtime_request(code, amount, phone),
                     _fingerprint(service, {'network': code, 'phone': phone, 'amount': str(amount)}))

    if service == 'data':
        plan_raw = _text(data, 'plan_id')
        phone = _phone(data)
        plan = DataPlan.objects.filter(pk=plan_raw).first() if plan_raw.isdigit() else None
        if not plan:
            raise validation_error(plan_id='Unknown plan. Fetch valid ids from GET /data-plans/.')
        _check_network_enabled(NETWORKS.get(plan.network, plan.network))
        order = Order('data', plan.plan_name, phone, plan.price, plan.cost_price,
                      flows.data_request(plan, phone),
                      _fingerprint(service, {'plan': plan.pk, 'phone': phone}), extra_fee=plan.additional_fee)
        order.plan = plan
        return order

    if service == 'electricity':
        disco = _text(data, 'disco')
        if disco not in DISCOS:
            raise validation_error(disco='Unknown disco code. See GET /electricity/discos/.')
        meter_no = _text(data, 'meter_no', maxlen=20)
        if not meter_no.isdigit() or len(meter_no) < 6:
            raise validation_error(meter_no='Must be a numeric meter number.')
        meter_type = _text(data, 'meter_type').lower()
        meter_type = {'prepaid': '01', 'postpaid': '02'}.get(meter_type, meter_type)
        if meter_type not in ('01', '02'):
            raise validation_error(meter_type='Use "prepaid" or "postpaid".')
        v = _gather(phone=lambda: _phone(data), amount=lambda: _amount(data, Decimal('1000'), Decimal('500000')))
        phone, amount = v['phone'], v['amount']
        return Order('electricity', f'{DISCOS[disco]} {"prepaid" if meter_type == "01" else "postpaid"} N{amount}', meter_no,
                     amount + ELECTRICITY_FEE, amount,
                     flows.electricity_request(disco, meter_type, meter_no, amount, phone),
                     _fingerprint(service, {'disco': disco, 'meter': meter_no, 'type': meter_type, 'phone': phone, 'amount': str(amount)}),
                     extra_fee=ELECTRICITY_FEE)

    if service == 'cable':
        cable_tv = _text(data, 'cable_tv').lower()
        if cable_tv not in CABLE_TYPES:
            raise validation_error(cable_tv='Use dstv, gotv or startimes.')
        package_id = _text(data, 'package_id')
        card = _text(data, 'smartcard', maxlen=20)
        if not card.isdigit():
            raise validation_error(smartcard='Must be numeric.')
        phone = _phone(data)
        plan = CablePlan.objects.filter(cable_type=cable_tv, plan_id=package_id, is_active=True).first()
        if not plan:
            raise validation_error(package_id='Unknown package. Fetch valid ids from GET /cable-plans/.')
        return Order('cable', f'{cable_tv.upper()} {plan.name}', card, plan.price, plan.cost_price,
                     flows.cable_request(plan, card, phone),
                     _fingerprint(service, {'cable': cable_tv, 'package': plan.plan_id, 'card': card, 'phone': phone}),
                     extra_fee=plan.additional_fee)

    raise ApiError(404, 'unknown_service', 'Unknown service.')


# ---------------------------------------------------------------------------------------------------
# Settling (single place where a transaction changes final state)
# ---------------------------------------------------------------------------------------------------
def _refund_once(api_tx):
    """Refund the debit exactly once. True when the wallet has been refunded (now or before)."""
    if api_tx.refunded:
        return True
    ref = api_tx.core_reference
    ok, _ = TransactionService.process_refund(api_tx.application.developer.user, api_tx.amount, ref, f'API order {api_tx.reference} not completed')
    if not ok and WalletTransaction.objects.filter(reference=f'REF-{ref}').exists():
        ok = True  # refund already existed
    if ok:
        api_tx.refunded = True
    else:
        logger.critical('API_REFUND_FAILED ref=%s amount=%s', api_tx.reference, api_tx.amount)
    return ok


def settle(api_tx_id, outcome, provider_response=None, reason=''):
    """Move a pending order to a final state. Idempotent: a settled order is never changed by a late/duplicate call.
    outcome: 'successful' | 'failed' | 'pending'. Returns the refreshed ApiTransaction."""
    provider_response = provider_response or {}
    with transaction.atomic():
        tx = ApiTransaction.objects.select_for_update().select_related('application__developer__user').get(pk=api_tx_id)
        if tx.status in ('successful', 'failed', 'reversed'):
            if tx.status == 'failed' and not tx.refunded and tx.core_reference and not tx.is_test:
                _refund_once(tx)  # a failed order whose refund did not complete: retry the refund only
                tx.save()
            return tx
        previous = tx.status
        tx.provider_status = str(provider_response.get('status') or '')[:60]

        if outcome == 'successful':
            tx.status = 'successful'
            token = provider_response.get('metertoken') or provider_response.get('token')
            if token:
                tx.result = {**(tx.result or {}), 'token': str(token)}
            if tx.core_reference:
                CoreTx.objects.filter(reference=tx.core_reference).update(status='Successful')
        elif outcome == 'failed':
            tx.status = 'failed'
            tx.failure_reason = reason or 'The provider could not complete this order.'
            if not tx.is_test:
                _refund_once(tx)
            else:
                tx.refunded = True
        else:
            tx.status = 'pending'
        tx.save()
        changed = tx.status != previous or (outcome == 'pending' and not tx.webhook_deliveries.exists())

    if tx.status == 'successful' and tx.core_reference:
        _finalise_core(tx)
    if changed:
        webhooks.queue_event(tx)
    return tx


def _finalise_core(api_tx):
    core = CoreTx.objects.filter(reference=api_tx.core_reference).first()
    if core:
        fee = api_tx.result.get('_fee') if isinstance(api_tx.result, dict) else None
        if fee:
            core.bt_service_charge = Decimal(str(fee))
        core.calculate_totals()


# ---------------------------------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------------------------------
def _replay(existing, fingerprint):
    if existing.request_fingerprint != fingerprint:
        raise ApiError(422, 'idempotency_key_reused', 'This Idempotency-Key was already used with different request data.')
    if existing.status == 'processing':
        raise ApiError(409, 'request_in_progress', 'A request with this Idempotency-Key is still being processed. Retry shortly.',
                       headers={'Retry-After': '3'})
    return existing, True


def execute_purchase(app, service, data, idempotency_key, client_reference=''):
    """Run one purchase end to end. Returns (ApiTransaction, replayed: bool)."""
    order = build_order(service, data)
    existing = ApiTransaction.objects.filter(application=app, idempotency_key=idempotency_key).first()
    if existing:
        return _replay(existing, order.fingerprint)

    if order.plan and not order.plan.is_active:  # after the replay check, so a retried failure still replays
        raise validation_error(plan_id='This plan is temporarily unavailable. Choose another from GET /data-plans/.')

    if app.is_live:
        blocked = engine.check_available(order.template)   # before anything is created or charged
        if blocked:
            raise ApiError(503, 'service_unavailable', 'This service is temporarily unavailable. Please try again shortly.')

    user = app.developer.user
    try:
        tx = ApiTransaction.objects.create(
            application=app, reference=ApiTransaction.new_reference(), client_reference=client_reference,
            idempotency_key=idempotency_key, request_fingerprint=order.fingerprint, service=service,
            description=order.description[:160], recipient=order.recipient, amount=order.charge,
            status='processing', is_test=not app.is_live,
        )
    except IntegrityError:  # a concurrent identical request won the race
        existing = ApiTransaction.objects.get(application=app, idempotency_key=idempotency_key)
        return _replay(existing, order.fingerprint)

    if not app.is_live:
        return _simulate(tx, order), False

    # --- live: debit first ------------------------------------------------------------------
    ok, res = TransactionService.process_debit(
        user=user, amount=order.charge, service_type=f'{SERVICE_LABEL[service]} (API)', plan_name=order.description[:100],
        recipient=order.recipient, reference=tx.reference,
        description=f'API {service}: {order.description} -> {order.recipient} [{app.name}]', cost_price=order.cost)
    if not ok:
        tx.delete()  # nothing moved; let the developer retry with the same key after funding
        if 'insufficient' in str(res).lower():
            raise ApiError(402, 'insufficient_funds', 'Wallet balance is too low for this purchase.')
        logger.error('API_DEBIT_ERROR app=%s detail=%s', app.pk, res)
        raise ApiError(500, 'internal_error', 'Could not process the request. Nothing was charged.')

    tx.core_reference = res.reference
    tx.provider_request_id = res.reference[:40]     # the id sent to the provider as RequestID for the first attempt
    tx.status = 'pending'
    if order.extra_fee:
        tx.result = {'_fee': str(order.extra_fee)}
    tx.save()

    try:
        outcome = engine.dispatch(res, order.template, auto_refund=False)
    except Exception:
        logger.exception('API_PROVIDER_EXCEPTION ref=%s', tx.reference)
        outcome = None
    logger.info('API_PURCHASE ref=%s app=%s outcome=%s', tx.reference, app.pk, getattr(outcome, 'kind', 'error'))

    if outcome is not None and outcome.kind == 'rejected':
        # Every eligible provider confirmed it did NOT take the order: the only case that refunds here.
        logger.warning('API_PURCHASE_REJECTED ref=%s code=%s category=%s', tx.reference, outcome.code, outcome.category)
        reason = 'The order was rejected by the network provider.'  # supplier detail stays in the logs, not the API reply
        return settle(tx.pk, 'failed', {'status': outcome.code}, reason), False

    if outcome is not None and outcome.kind == 'accepted':
        token = (outcome.extra or {}).get('metertoken') or (outcome.extra or {}).get('token')
        if token:
            ApiTransaction.objects.filter(pk=tx.pk).update(result={**(tx.result or {}), 'token': str(token)})
        return settle(tx.pk, 'pending', {'status': outcome.code or 'ORDER_RECEIVED'}), False

    # Unclear (timeout / dropped connection / unknown reply) or an unexpected crash: the order may exist, so it
    # stays pending - never refunded - until the provider confirms an outcome.
    return settle(tx.pk, 'pending', {'status': 'UNCLEAR'}), False


def _simulate(tx, order):
    """Sandbox: no wallet, no provider. Recipient ending 0000 => failed, 9999 => stays pending, otherwise success."""
    ending = tx.recipient[-4:]
    if ending == '0000':
        return settle(tx.pk, 'failed', {'status': 'SIMULATED_FAILURE'}, 'Simulated failure (recipient ends in 0000).')
    if ending == '9999':
        return settle(tx.pk, 'pending', {'status': 'SIMULATED_PENDING'})
    resp = {'status': 'SIMULATED_SUCCESS'}
    if tx.service == 'electricity':
        resp['metertoken'] = '1234-5678-9012-3456-7890'
    return settle(tx.pk, 'successful', resp)


# ---------------------------------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------------------------------
def reconcile(api_tx):
    """
    Ask the provider holding a pending live order what happened and settle it only on a definite answer.
    The core transaction is resolved through the gateway; the ``on_core_settled`` listener then moves this
    ApiTransaction (refund + webhook included), so there is a single place where money moves.
    """
    api_tx.reconcile_attempts += 1
    api_tx.save(update_fields=['reconcile_attempts', 'updated_at'])
    core = CoreTx.objects.filter(reference=api_tx.core_reference).first() if api_tx.core_reference else None
    if core is None:
        return api_tx
    if core.attempts.exists() or core.order_id:
        engine.reconcile_transaction(core, 'api-reconcile')
    elif api_tx.provider_request_id:
        # Order placed before the gateway existed: only the supplier's RequestID is known.
        adapter = registry.get_adapter('nellobytes')
        res = adapter.query_status('', api_tx.provider_request_id) if adapter else None
        if res is not None and res.status in (OrderStatus.SUCCESSFUL, OrderStatus.FAILED):
            ledger.settle(core.pk, res.status, 'api-reconcile')
    api_tx.refresh_from_db()
    return api_tx


def on_core_settled(core_tx, outcome):
    """Gateway listener: a core transaction reached a final state, so settle the developer order that owns it."""
    api_tx = ApiTransaction.objects.filter(core_reference=core_tx.reference).first()
    if api_tx is None or api_tx.status not in ('processing', 'pending'):
        return
    if outcome == OrderStatus.SUCCESSFUL:
        settle(api_tx.pk, 'successful', {'status': 'ORDER_COMPLETED'})
    else:
        settle(api_tx.pk, 'failed', {'status': 'ORDER_CANCELLED'}, 'The order was cancelled by the network provider.')
