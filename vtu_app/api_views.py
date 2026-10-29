import json
import logging
import re
import time
from decimal import Decimal, InvalidOperation
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from django.contrib.auth.models import User
from django.db.models import Q
from django.shortcuts import get_object_or_404
from .models import DataPlan, Transaction, Profile, CablePlan, Notification, PaystackTransaction
from .serializers import DataPlanSerializer, TransactionSerializer, CablePlanSerializer
from .services import MonnifyService, PaystackService
from .services.transaction_service import TransactionService, resolve_paystack_user
from .notifications import notify
from .gateway import engine, flows
from .gateway.types import Service

logger = logging.getLogger(__name__)

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def api_get_notifications(request):
    """Serve combined global and personal notifications for the mobile app, newest first."""
    # Get global notifications OR notifications meant for this user
    notifications = Notification.objects.filter(
        Q(user=request.user) | Q(user__isnull=True)
    )  # default ordering (-created_at) comes from Notification.Meta

    data = [{
        "id": n.id,
        "title": n.title,
        "message": n.message,
        "created_at": n.created_at.isoformat(),
        "is_read": n.is_read,
    } for n in notifications]

    return Response(data)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def api_mark_notification_read(request, pk):
    """Mark a single notification (owned by the requesting user) as read.

    Deliberately scoped to the user's own rows rather than the global feed —
    a global (user=None) row is shared by every user, so flipping its
    is_read here would incorrectly mark it read for everyone.
    """
    notification = get_object_or_404(Notification, pk=pk, user=request.user)
    if not notification.is_read:
        notification.is_read = True
        notification.save(update_fields=['is_read'])
    return Response(status=status.HTTP_204_NO_CONTENT)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def api_mark_all_notifications_read(request):
    """Mark all of the requesting user's own notifications as read."""
    Notification.objects.filter(user=request.user, is_read=False).update(is_read=True)
    return Response(status=status.HTTP_204_NO_CONTENT)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def api_paystack_verify(request, reference):
    """
    Verify a Paystack card payment and credit the wallet on success.

    This is the "did it work?" call the app makes right after the Paystack
    charge sheet closes. The webhook below is the safety net for when this
    call never happens (app killed, network drop) — both paths share the
    same idempotent TransactionService.process_paystack_credit().
    """
    service = PaystackService()
    result = service.verify_transaction(reference)

    if not result.get('status'):
        logger.warning(f"PAYSTACK_VERIFY_FAILED: {reference} — {result.get('message')}")
        return Response({"status": "failed"})

    data = result.get('data') or {}
    paystack_status = data.get('status')

    if paystack_status != 'success':
        # "pending" covers async methods (bank transfer/USSD); anything else
        # (abandoned, failed, reversed) is a hard failure.
        return Response({"status": "pending" if paystack_status == 'pending' else "failed"})

    # Ownership check: if the payment carries an identifiable owner (via
    # metadata.user_id or the paying card's email) that isn't the caller,
    # refuse — otherwise a leaked/guessed reference could be used to credit
    # the wrong account. If it carries no identifiable owner at all, we
    # trust the authenticated caller (Token auth already proved who they are).
    paying_user = resolve_paystack_user(data)
    if paying_user is not None and paying_user.id != request.user.id:
        return Response({"message": "This transaction does not belong to your account."}, status=status.HTTP_403_FORBIDDEN)

    credited, new_balance, paystack_tx = TransactionService.process_paystack_credit(request.user, data)
    return Response({"status": "success", "new_balance": str(new_balance)})


@csrf_exempt
def api_paystack_webhook(request):
    """
    Paystack server-to-server webhook — the real safety net for card funding.

    No Token auth: Paystack calls this directly, so authenticity comes from
    verifying the `x-paystack-signature` header (HMAC-SHA512 of the raw
    body with PAYSTACK_SECRET_KEY) rather than a bearer token. Register this
    URL in the Paystack dashboard under Settings -> API Keys & Webhooks.
    """
    if request.method != 'POST':
        return HttpResponse(status=405)

    service = PaystackService()
    signature = request.headers.get('x-paystack-signature')

    if not service.verify_webhook_signature(request.body, signature):
        logger.warning(f"PAYSTACK_WEBHOOK: Signature mismatch. Remote IP: {request.META.get('REMOTE_ADDR')}")
        return HttpResponse(status=401)

    try:
        event = json.loads(request.body)
    except ValueError:
        return HttpResponse(status=400)

    if event.get('event') != 'charge.success':
        # Acknowledge anything we don't act on so Paystack doesn't retry it.
        return HttpResponse(status=200)

    data = event.get('data') or {}
    if data.get('status') != 'success':
        return HttpResponse(status=200)

    user = resolve_paystack_user(data)
    if user is None:
        logger.error(f"PAYSTACK_WEBHOOK: Could not identify a user for reference {data.get('reference')}. "
                     f"Make sure the app sends metadata.user_id (or a matching email) when initializing the charge.")
        # Acknowledge receipt (200) so Paystack stops retrying — there's
        # nothing more we can do without knowing who to credit.
        return HttpResponse(status=200)

    try:
        TransactionService.process_paystack_credit(user, data)
    except Exception as e:
        logger.error(f"PAYSTACK_WEBHOOK_ERROR: {str(e)}")
        # 500 tells Paystack to retry — appropriate here since this is
        # exactly the "make sure the wallet still gets credited" safety net.
        return HttpResponse(status=500)

    return HttpResponse(status=200)


class MobileDashboard(APIView):
    permission_classes = [IsAuthenticated] # Must have a Token

    def get(self, request):
        user = request.user
        profile = user.profile
        
        # id/email exposed for the mobile app's Paystack integration:
        # metadata.user_id (-> id) lets the payment webhook — which has no
        # auth context of its own — identify the account to credit, and
        # email lets the app open Paystack checkout with the user's real
        # address instead of a synthesized placeholder. See
        # resolve_paystack_user() in services/transaction_service.py.
        # We ensure the bank_accounts field is sent as a list
        return Response({
            "id": user.id,
            "username": user.username,
            "email": user.email,
            "profile": {
                "wallet_balance": str(profile.wallet_balance),
                "referral_code": profile.referral_code,
                "bank_accounts": profile.bank_accounts if isinstance(profile.bank_accounts, list) else []
            },
            "system_announcement": "Welcome to the new BT DataPlug Mobile App!"
        })

class DataPlanList(APIView):
    permission_classes = [] # Public access
    
    def get(self, request):
        plans = DataPlan.objects.filter(is_active=True)
        serializer = DataPlanSerializer(plans, many=True)
        return Response(serializer.data)

class CablePlanList(APIView):
    permission_classes = [] # Public access
    
    def get(self, request):
        plans = CablePlan.objects.filter(is_active=True).order_by('price')
        serializer = CablePlanSerializer(plans, many=True)
        return Response(serializer.data)

@api_view(['POST'])
@permission_classes([AllowAny])
def api_register(request):
    data = request.data
    
    # Extract data (matching your website form fields)
    username = data.get('username')
    email = data.get('email')
    password = data.get('password')
    first_name = data.get('first_name', '')
    last_name = data.get('last_name', '')

    # 1. Validation (Same as website)
    if not password or len(password) < 8:
        return Response({"message": "Password must be at least 8 characters"}, status=status.HTTP_400_BAD_REQUEST)

    if User.objects.filter(username=username).exists():
        return Response({"message": "Username already exists"}, status=status.HTTP_400_BAD_REQUEST)
    
    if User.objects.filter(email=email).exists():
        return Response({"message": "Email already registered"}, status=status.HTTP_400_BAD_REQUEST)

    try:
        # 2. Create User exactly like the web registration
        user = User.objects.create_user(
            username=username,
            email=email,
            password=password,
            first_name=first_name,
            last_name=last_name
        )

        # 3. Create Profile (Ensure it matches your website signal or manual creation)
        profile, created = Profile.objects.get_or_create(user=user)

        # 4. Handle Referral Tracking
        ref_code = data.get('referral_code')
        if ref_code:
            referrer_profile = Profile.objects.filter(referral_code=ref_code.strip().upper()).first()
            if referrer_profile:
                profile.referred_by = referrer_profile.user
                profile.save()

        # 5. Welcome notification (shows up in the app's notification history)
        notify(
            user,
            "Welcome to BT DataPlug",
            f"Hi {first_name or username}! Your account is ready — fund your wallet to start buying data, airtime, cable and electricity."
        )

        # 6. Trigger the Monnify Account Reservation (The "YUS" Branding)
        # This is the "Magic" that makes the mobile app match the site
        monnify = MonnifyService()
        response = monnify.reserve_account(user)

        if response.get('requestSuccessful'):
            accounts = response.get('responseBody', {}).get('accounts', [])
            profile.bank_accounts = accounts
            profile.save()
            return Response({
                "message": "Registration Successful",
                "accounts_generated": True
            }, status=status.HTTP_201_CREATED)
        else:
            # Even if Monnify fails, the user is created (just like your site logic)
            return Response({
                "message": "Account created, but bank numbers are pending.",
                "accounts_generated": False
            }, status=status.HTTP_201_CREATED)

    except Exception as e:
        return Response({"message": f"Server Error: {str(e)}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def api_transaction_history(request):
    # Orders the supplier has accepted stay Pending until confirmed; settle this user's few newest on read.
    engine.reconcile_user_pending(request.user)
    # Fetch last 30 transactions (Excluding BTKKM/ID 25)
    transactions = Transaction.objects.filter(user=request.user).exclude(
        id=25
    ).exclude(
        plan_name__icontains='BTKKM'
    ).exclude(
        service_type__icontains='BTKKM'
    ).order_by('-created_at')[:30]
    
    data = []
    for tx in transactions:
        data.append({
            "id": tx.id,
            "service": tx.service_type,
            "recipient": tx.recipient,
            "amount": str(tx.amount_customer_paid),
            "status": tx.status, 
            "date": tx.created_at.strftime("%d %b, %Y"),
            "time": tx.created_at.strftime("%I:%M %p"),
            "ref": tx.reference
        })
    
    return Response(data)

def _idem_key(request):
    """Optional client-supplied Idempotency-Key (header or body): the same key never charges twice."""
    key = str(request.headers.get('Idempotency-Key') or request.data.get('idempotency_key') or '').strip()
    return key if re.fullmatch(r'[A-Za-z0-9._:-]{8,100}', key) else None


def _replay(tx):
    """Answer for a repeated request whose Idempotency-Key already produced transaction ``tx``."""
    tx.refresh_from_db()
    body = {"reference": tx.reference, "transaction_id": tx.id, "order_id": tx.order_id or None, "idempotent_replay": True}
    if tx.status == "Refunded":
        return Response({**body, "message": "This purchase did not go through and was refunded."}, status=status.HTTP_409_CONFLICT)
    return Response({**body, "status": "successful" if tx.status == "Successful" else "pending",
                     "message": "Your order has been received." if tx.status == "Pending" else "Transaction Successful!"})


def _blocked(template):
    """Refuse BEFORE any money moves when no provider can serve the request."""
    problem = engine.check_available(template)
    if problem:
        return Response({"message": problem[1]}, status=problem[0])
    return None


def _complete(user, tx, template, payload, service_charge=None):
    """
    Submit the debited order and translate the outcome to the response shapes the mobile app already
    understands: 200 + receipt fields when the supplier accepted it (or the outcome is still unclear and
    will be settled by reconciliation), or an error + automatic refund when every provider rejected it.
    """
    result = engine.dispatch(tx, template)
    user.profile.refresh_from_db()
    if result.kind in ('accepted', 'unclear'):
        if service_charge is not None:
            Transaction.objects.filter(pk=tx.pk).update(bt_service_charge=service_charge)
        body = dict(payload(result))
        body.update({"reference": tx.reference, "status": "pending", "new_balance": str(user.profile.wallet_balance)})
        if result.kind == 'unclear':
            body["message"] = "Your order is being processed. You will be notified once it completes."
        return Response(body, status=status.HTTP_200_OK)
    return Response({"message": f"{result.message} Your funds have been refunded."}, status=result.http_status)


@csrf_exempt
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def api_buy_data(request):
    """
    Production-grade Mobile Data Purchase.
    Atomic debit -> provider-agnostic gateway (safe failover) -> exactly-once refund on confirmed failure.
    """
    user = request.user
    plan_id = request.data.get('plan_id')
    phone = request.data.get('phone')
    pin = request.data.get('pin')

    if not plan_id or not phone or not pin:
        return Response({"message": "Missing plan_id, phone number, or pin"}, status=status.HTTP_400_BAD_REQUEST)

    # SECURE PIN VERIFICATION
    if not user.profile.check_pin(pin):
        return Response({"message": "Invalid Transaction PIN"}, status=status.HTTP_400_BAD_REQUEST)

    try:
        plan = DataPlan.objects.get(id=plan_id)
    except (DataPlan.DoesNotExist, ValueError, TypeError):
        return Response({"message": "Invalid Plan Selected"}, status=status.HTTP_404_NOT_FOUND)
    if not plan.is_active:
        return Response({"message": "This plan is temporarily unavailable. Please choose another."}, status=status.HTTP_400_BAD_REQUEST)

    template = flows.data_request(plan, phone)
    blocked = _blocked(template)
    if blocked:
        return blocked

    # 1. ATOMIC DEBIT (a repeated Idempotency-Key returns the original order instead of charging again)
    success, result = TransactionService.process_debit(
        user=user,
        amount=plan.price,
        service_type="Data Purchase (Mobile)",
        plan_name=plan.plan_name,
        recipient=phone,
        reference=flows.new_reference("DTM"),
        description=f"Mobile purchase of {plan.plan_name} for {phone}",
        cost_price=plan.cost_price,
        idempotency_key=_idem_key(request),
    )
    if not success:
        return Response({"message": f"Transaction failed: {result}"}, status=status.HTTP_400_BAD_REQUEST)
    if getattr(result, 'replayed', False):
        return _replay(result)

    # 2. GATEWAY: route, submit, fail over only when provably safe, refund once if everything rejected
    return _complete(user, result, template, lambda r: {
        "message": "Transaction Successful!",
        "transaction_id": result.id,
        "plan": plan.plan_name,
        "phone": phone,
        "amount_paid": str(result.amount_customer_paid),
        "order_id": r.order_id or result.reference,
    }, service_charge=plan.additional_fee)

@csrf_exempt
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def api_buy_airtime(request):
    """
    Production-grade Mobile Airtime Purchase.
    Atomic debit -> provider-agnostic gateway (safe failover) -> exactly-once refund on confirmed failure.
    """
    user = request.user
    network = request.data.get('network')
    amount_str = request.data.get('amount')
    phone = request.data.get('phone')
    pin = request.data.get('pin')

    if not network or not amount_str or not phone or not pin:
        return Response({"message": "Missing network, amount, phone, or pin"}, status=status.HTTP_400_BAD_REQUEST)

    # SECURE PIN VERIFICATION
    if not user.profile.check_pin(pin):
        return Response({"message": "Invalid Transaction PIN"}, status=status.HTTP_400_BAD_REQUEST)

    network_name = str(network).upper()
    if network_name not in flows.NETWORK_CODES:
        return Response({"message": f"Invalid Network: {network}. Select MTN, GLO, AIRTEL, or 9MOBILE."}, status=status.HTTP_400_BAD_REQUEST)

    try:
        amount = float(amount_str)
        if amount <= 0 or amount != amount or amount in (float('inf'),):
            raise ValueError
    except (ValueError, TypeError):
        return Response({"message": "Invalid amount"}, status=status.HTTP_400_BAD_REQUEST)

    # CTO LOGIC: 1% Discount (User pays 99%, you pay 97%, Profit is 2%)
    selling_price = (Decimal(str(amount)) * Decimal('0.99')).quantize(Decimal('0.01'))
    cost_price = (Decimal(str(amount)) * Decimal('0.97')).quantize(Decimal('0.01'))

    template = flows.airtime_request(network_name, amount, phone)
    blocked = _blocked(template)
    if blocked:
        return blocked

    # 1. ATOMIC DEBIT
    success, tx = TransactionService.process_debit(
        user=user,
        amount=selling_price,
        service_type="Airtime Purchase (Mobile)",
        plan_name=f"{network} Airtime (₦{amount})",
        recipient=phone,
        reference=flows.new_reference("ATM"),
        description=f"Mobile Airtime purchase of {network} {amount} for {phone}",
        cost_price=cost_price,
        idempotency_key=_idem_key(request),
    )
    if not success:
        return Response({"message": f"Transaction failed: {tx}"}, status=status.HTTP_400_BAD_REQUEST)
    if getattr(tx, 'replayed', False):
        return _replay(tx)

    # 2. GATEWAY
    return _complete(user, tx, template, lambda r: {
        "message": "Airtime Sent!",
        "transaction_id": tx.id,
        "network": network,
        "amount": str(amount),
        "phone": phone,
        "amount_paid": str(tx.amount_customer_paid),
        "order_id": r.order_id or tx.reference,
    })

@api_view(['POST'])
@permission_classes([IsAuthenticated])
def api_change_pin(request):
    """Secure Mobile API endpoint to update Transaction PIN."""
    user = request.user
    old_pin = request.data.get('old_pin')
    new_pin = request.data.get('new_pin')

    if not old_pin or not new_pin:
        return Response({"message": "Current PIN and New PIN are required"}, status=status.HTTP_400_BAD_REQUEST)

    # 1. Verify Old PIN (Secure Hashing Check)
    if not user.profile.check_pin(old_pin):
        return Response({"message": "The current PIN you entered is incorrect"}, status=status.HTTP_400_BAD_REQUEST)
    
    # 2. Validation (Ensure 4 digits)
    if not str(new_pin).isdigit() or len(str(new_pin)) != 4:
        return Response({"message": "New PIN must be exactly 4 digits"}, status=status.HTTP_400_BAD_REQUEST)

    # 3. Save New PIN (Secure Hashing)
    user.profile.set_pin(new_pin)
    user.profile.save()
    
    return Response({"message": "Transaction PIN updated successfully"})

@api_view(['POST'])
@permission_classes([IsAuthenticated])
def api_change_password(request):
    """Mobile 'Update password': verifies the current password, then sets the new one."""
    user = request.user
    old_password = request.data.get('old_password') or ''
    new_password = request.data.get('new_password') or ''

    if not old_password or not new_password:
        return Response({"message": "Current password and new password are required"}, status=status.HTTP_400_BAD_REQUEST)
    if not user.check_password(old_password):
        return Response({"message": "The current password you entered is incorrect"}, status=status.HTTP_400_BAD_REQUEST)
    if len(new_password) < 6:
        return Response({"message": "New password must be at least 6 characters"}, status=status.HTTP_400_BAD_REQUEST)
    if new_password == old_password:
        return Response({"message": "New password must be different from the current one"}, status=status.HTTP_400_BAD_REQUEST)

    user.set_password(new_password)
    user.save()  # the DRF auth token stays valid, so the app session continues
    return Response({"message": "Password updated successfully"})


class PasswordResetThrottle(AnonRateThrottle):
    rate = '5/hour'


@api_view(['POST'])
@permission_classes([AllowAny])
@throttle_classes([PasswordResetThrottle])
def api_password_reset(request):
    """
    Mobile 'Forgot password': emails a reset link (the link opens the web reset page).
    Always answers the same way so it can't be used to discover which emails are registered.
    """
    from django.contrib.auth.forms import PasswordResetForm
    email = (request.data.get('email') or '').strip()
    if email:
        form = PasswordResetForm({'email': email})
        if form.is_valid():
            try:
                form.save(request=request, use_https=request.is_secure(),
                          email_template_name='registration/password_reset_email.html',
                          subject_template_name='registration/password_reset_subject.txt')
            except Exception:
                logger.exception("PASSWORD_RESET_EMAIL_FAILED")
    return Response({"message": "If an account exists for that email, a reset link has been sent."})


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def api_validate_cable(request):
    """Mobile API for real-time decoder verification (providers asked in priority order)."""
    cable_tv = request.GET.get('cable_tv') # gotv, dstv, etc
    smart_card = request.GET.get('smart_card')

    if not cable_tv or not smart_card:
        return Response({"error": "Missing cable_tv or smart_card"}, status=400)

    return Response(flows.verify_customer(Service.CABLE, str(cable_tv).upper(), smart_card))

@csrf_exempt
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def api_buy_cable(request):
    """Secure Mobile Cable TV purchase: PIN, atomic debit, gateway, exactly-once refund."""
    user = request.user
    cable_tv = request.data.get('cable_tv')
    package_id = request.data.get('package_id') # This is the plan_id
    smart_card = request.data.get('smart_card')
    phone = request.data.get('phone')
    pin = request.data.get('pin')

    if not all([cable_tv, package_id, smart_card, phone, pin]):
        return Response({"message": "Missing required fields"}, status=400)

    # 1. PIN VERIFICATION
    if not user.profile.check_pin(pin):
        return Response({"message": "Invalid Transaction PIN"}, status=400)

    # 2. GET PLAN (scoped to the decoder brand: provider plan ids are not unique across brands)
    plan = CablePlan.objects.filter(plan_id=package_id, cable_type=str(cable_tv).lower(), is_active=True).first()
    if plan is None:
        return Response({"message": "Invalid Package ID"}, status=400)

    template = flows.cable_request(plan, smart_card, phone)
    blocked = _blocked(template)
    if blocked:
        return blocked

    # 3. ATOMIC DEBIT
    success, tx = TransactionService.process_debit(
        user=user,
        amount=plan.price,
        service_type=f"Cable: {plan.name} (Mobile)",
        plan_name=f"{plan.cable_type.upper()}: {plan.name}",
        recipient=smart_card,
        reference=flows.new_reference("CB"),
        description=f"Mobile Subscription for {plan.name} on {smart_card}",
        cost_price=plan.cost_price,
        idempotency_key=_idem_key(request),
    )
    if not success:
        return Response({"message": f"Transaction failed: {tx}"}, status=400)
    if getattr(tx, 'replayed', False):
        return _replay(tx)

    # 4. GATEWAY
    return _complete(user, tx, template, lambda r: {
        "message": "Subscription Received Successfully",
        "transaction_id": tx.id,
        "order_id": r.order_id or tx.reference,
    })

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def api_validate_meter(request):
    """Mobile API for real-time meter verification (providers asked in priority order)."""
    disco = request.GET.get('disco')
    meter_no = request.GET.get('meter_no')
    meter_type = request.GET.get('meter_type')

    if not disco or not meter_no or not meter_type:
        return Response({"error": "Missing disco, meter_no, or meter_type"}, status=400)

    return Response(flows.verify_customer(Service.ELECTRICITY, disco, meter_no, meter_type))

@csrf_exempt
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def api_pay_electricity(request):
    """Secure Mobile Electricity bill payment: PIN, atomic debit, gateway, exactly-once refund."""
    user = request.user
    disco = request.data.get('disco')
    meter_no = request.data.get('meter_no')
    meter_type = request.data.get('meter_type')
    amount_str = request.data.get('amount')
    phone = request.data.get('phone')
    pin = request.data.get('pin')

    if not all([disco, meter_no, meter_type, amount_str, phone, pin]):
        return Response({"message": "Missing required fields"}, status=400)

    # 1. PIN VERIFICATION
    if not user.profile.check_pin(pin):
        return Response({"message": "Invalid Transaction PIN"}, status=400)

    try:
        amount = Decimal(str(amount_str))
        if not amount.is_finite():
            raise InvalidOperation
        if amount < 1000:
            return Response({"message": "Minimum amount for electricity is ₦1,000"}, status=400)
    except (InvalidOperation, ValueError, TypeError):
        return Response({"message": "Invalid amount"}, status=400)

    # 2. Calculation (Bill + BT Service Fee)
    service_fee = Decimal('100.00')
    total_to_pay = amount + service_fee

    template = flows.electricity_request(disco, meter_type, meter_no, amount, phone)
    blocked = _blocked(template)
    if blocked:
        return blocked

    # 3. ATOMIC DEBIT (Fintech Grade)
    success, tx = TransactionService.process_debit(
        user=user,
        amount=total_to_pay,
        service_type=f"Electricity: {disco} (Mobile)",
        plan_name=f"Electricity: {disco} ({'Prepaid' if meter_type == '01' else 'Postpaid'})",
        recipient=meter_no,
        reference=flows.new_reference("ELE"),
        description=f"Mobile Bill Payment for {meter_no}. Amount: ₦{amount}, Fee: ₦{service_fee}",
        cost_price=amount,
        idempotency_key=_idem_key(request),
    )
    if not success:
        return Response({"message": f"Transaction failed: {tx}"}, status=400)
    if getattr(tx, 'replayed', False):
        return _replay(tx)

    # 4. GATEWAY (the token is returned only if the supplier already included it; otherwise it arrives by SMS)
    def payload(r):
        body = {"message": "Payment Successful", "transaction_id": tx.id, "order_id": r.order_id or tx.reference}
        token = (r.extra or {}).get('metertoken')
        if token:
            body["token"] = token
        return body
    return _complete(user, tx, template, payload, service_charge=service_fee)
