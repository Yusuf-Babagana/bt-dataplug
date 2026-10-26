import logging
import uuid
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.core.cache import cache
from django.db import transaction
from django.shortcuts import render, redirect

from .models import DataPlan, Profile, Transaction as TxModel, WalletTransaction
from .notifications import notify

logger = logging.getLogger(__name__)

MIN_TRANSFER = Decimal('50')
PIN_MAX_FAILS = 5
PIN_LOCK_SECONDS = 15 * 60

# Features shown on the dashboard that need a supplier integration before they can go live.
COMING_SOON = {
    'airtime-to-cash': {
        'title': 'Airtime 2 Cash',
        'icon': 'fa-right-left',
        'text': 'Convert your extra airtime into wallet cash.',
    },
    'bulk-sms': {
        'title': 'Bulk SMS',
        'icon': 'fa-comments',
        'text': 'Send one message to hundreds of contacts at once.',
    },
    'result-checker': {
        'title': 'Result Checker',
        'icon': 'fa-graduation-cap',
        'text': 'Buy WAEC, NECO and NABTEB result-checking PINs.',
    },
    'data-card': {
        'title': 'Data Card Printing',
        'icon': 'fa-print',
        'text': 'Print data vouchers to sell to your own customers.',
    },
    'recharge-card': {
        'title': 'Recharge Card Printing',
        'icon': 'fa-receipt',
        'text': 'Print airtime recharge cards to sell to your own customers.',
    },
}


@login_required
def calculator(request):
    """Price calculator: data plans (from the live price list) and airtime (1% discount)."""
    plans = [
        {
            'network': p.get_network_display(),
            'name': p.plan_name,
            'price': float(p.price),
        }
        for p in DataPlan.objects.filter(is_active=True).order_by('network', 'price')
    ]
    return render(request, 'vtu_app/calculator.html', {'plans': plans})


@login_required
def coming_soon(request, slug):
    feature = COMING_SOON.get(slug)
    if feature is None:
        return redirect('dashboard')
    return render(request, 'vtu_app/coming_soon.html', {'feature': feature})


def _pin_key(user):
    return f"transfer_pin_fails_{user.id}"


@login_required
def wallet_transfer(request):
    """Send money from your wallet to another BT DataPlug user (by username)."""
    profile = request.user.profile

    if request.method == 'POST':
        recipient_name = (request.POST.get('recipient') or '').strip()
        pin = request.POST.get('pin') or ''
        try:
            amount = Decimal(request.POST.get('amount') or '0').quantize(Decimal('0.01'))
        except InvalidOperation:
            amount = Decimal('0')

        if cache.get(_pin_key(request.user), 0) >= PIN_MAX_FAILS:
            messages.error(request, "Too many wrong PIN attempts. Please try again in 15 minutes.")
            return redirect('wallet_transfer')

        recipient = User.objects.filter(username__iexact=recipient_name, is_active=True).first()

        if not profile.check_pin(pin):
            cache.set(_pin_key(request.user), cache.get(_pin_key(request.user), 0) + 1, PIN_LOCK_SECONDS)
            messages.error(request, "Incorrect transaction PIN.")
        elif recipient is None:
            messages.error(request, "We could not find a BT DataPlug user with that username.")
        elif recipient.id == request.user.id:
            messages.error(request, "You cannot send money to yourself.")
        elif amount < MIN_TRANSFER:
            messages.error(request, f"Minimum transfer is ₦{MIN_TRANSFER}.")
        else:
            cache.delete(_pin_key(request.user))
            ref = uuid.uuid4().hex[:12].upper()
            try:
                with transaction.atomic():
                    # Lock both wallets in a fixed order so two opposite transfers cannot deadlock.
                    first, second = sorted([request.user.id, recipient.id])
                    locked = {p.user_id: p for p in Profile.objects.select_for_update().filter(user_id__in=[first, second])}
                    sender_p, recv_p = locked[request.user.id], locked[recipient.id]

                    if sender_p.wallet_balance < amount:
                        raise ValueError("Insufficient balance.")

                    s_old, r_old = sender_p.wallet_balance, recv_p.wallet_balance
                    sender_p.wallet_balance = s_old - amount
                    recv_p.wallet_balance = r_old + amount
                    sender_p.save()
                    recv_p.save()

                    WalletTransaction.objects.create(
                        user=request.user, amount=amount, previous_balance=s_old,
                        new_balance=sender_p.wallet_balance, transaction_type='DEBIT',
                        reference=f"TRF-OUT-{ref}", description=f"Transfer to {recipient.username}")
                    WalletTransaction.objects.create(
                        user=recipient, amount=amount, previous_balance=r_old,
                        new_balance=recv_p.wallet_balance, transaction_type='CREDIT',
                        reference=f"TRF-IN-{ref}", description=f"Transfer from {request.user.username}")
                    tx = TxModel.objects.create(
                        user=request.user, service_type="Wallet Transfer",
                        plan_name=f"Transfer to {recipient.username}", amount_customer_paid=amount,
                        cost_from_klubconnect=Decimal('0.00'), monnify_fee_on_this_tx=Decimal('0.00'),
                        bt_service_charge=Decimal('0.00'), recipient=recipient.username, status="Successful", reference=ref)
                    tx.calculate_totals()
            except ValueError as e:
                messages.error(request, str(e))
            except Exception:
                logger.exception("TRANSFER_ERROR")
                messages.error(request, "Transfer failed. No money was moved. Please try again.")
            else:
                notify(request.user, "Transfer sent", f"₦{amount} was sent to {recipient.username}.")
                notify(recipient, "Money received", f"{request.user.username} sent you ₦{amount}.")
                logger.info(f"TRANSFER: {request.user.id} -> {recipient.id} NGN {amount} ref {ref}")
                messages.success(request, f"₦{amount} sent to {recipient.username}.")
        return redirect('wallet_transfer')

    return render(request, 'vtu_app/transfer.html', {'min_transfer': MIN_TRANSFER})
