import time
from decimal import Decimal
from django.views.decorators.csrf import csrf_exempt
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from django.contrib.auth.models import User
from .models import DataPlan, Transaction, Profile, CablePlan, Notification
from .serializers import DataPlanSerializer, TransactionSerializer, CablePlanSerializer
from .services import MonnifyService, ClubKonnectService
from .services.transaction_service import TransactionService

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def api_get_notifications(request):
    """Serve combined global and personal notifications for the mobile app."""
    from django.db.models import Q
    # Get global notifications OR notifications meant for this user
    notifications = Notification.objects.filter(
        Q(user=request.user) | Q(user__isnull=True)
    ).order_by('-created_at')
    
    data = [{
        "id": n.id,
        "title": n.title,
        "message": n.message,
        "date": n.created_at.strftime("%d %b, %H:%M"),
        "is_global": n.user is None,
        "is_read": n.is_read
    } for n in notifications]
    
    return Response(data)

class MobileDashboard(APIView):
    permission_classes = [IsAuthenticated] # Must have a Token

    def get(self, request):
        user = request.user
        profile = user.profile
        
        # We ensure the bank_accounts field is sent as a list
        return Response({
            "username": user.username,
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
        # Removed .filter(is_active=True) because the field doesn't exist yet
        plans = DataPlan.objects.all() 
        serializer = DataPlanSerializer(plans, many=True)
        return Response(serializer.data)

class CablePlanList(APIView):
    permission_classes = [] # Public access
    
    def get(self, request):
        plans = CablePlan.objects.all().order_by('price')
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

        # 5. Trigger the Monnify Account Reservation (The "YUS" Branding)
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
    # Fetch last 30 transactions for the user
    transactions = Transaction.objects.filter(user=request.user).order_by('-created_at')[:30]
    
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

@csrf_exempt
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def api_buy_data(request):
    """
    Production-grade Mobile Data Purchase.
    Uses TransactionService for atomic balance deductions and profit tracking.
    """
    user = request.user
    plan_id = request.data.get('plan_id')
    phone = request.data.get('phone')
    pin = request.data.get('pin')

    if not plan_id or not phone or not pin:
        return Response({"message": "Missing plan_id, phone number, or pin"}, status=status.HTTP_400_BAD_REQUEST)

    # SECURE PIN VERIFICATION
    if not user.profile.check_pin(pin):
        return Response({"message": "Invalid Transaction PIN"}, status=status.HTTP_401_UNAUTHORIZED)

    try:
        plan = DataPlan.objects.get(id=plan_id)
    except DataPlan.DoesNotExist:
        return Response({"message": "Invalid Plan Selected"}, status=status.HTTP_404_NOT_FOUND)

    # 1. ACQUISITION OF LOCK & ATOMIC DEBIT
    success, result = TransactionService.process_debit(
        user=user,
        amount=plan.price,
        service_type="Data Purchase (Mobile)",
        plan_name=plan.plan_name,
        recipient=phone,
        reference=f"DTM-{int(time.time())}",
        description=f"Mobile purchase of {plan.plan_name} for {phone}",
        cost_price=plan.cost_price
    )

    if not success:
        return Response({"message": f"Transaction failed: {result}"}, status=status.HTTP_400_BAD_REQUEST)

    # 2. CALL PROVIDER API
    try:
        ck = ClubKonnectService()
        # Corrected argument order: network, plan_id, phone
        response, req_id = ck.buy_data(plan.network, plan.dataplan_id, phone)

        if response.get('status') in ['ORDER_RECEIVED', 'SUCCESSFUL']:
            # 3. SUCCESS - Finalize record
            TxModel.objects.filter(reference=result.reference).update(
                status="Successful",
                bt_service_charge=plan.additional_fee
            )
            tx = TxModel.objects.get(reference=result.reference)
            tx.calculate_totals() # Recalculate with potential service charge
            
            return Response({
                "message": "Transaction Successful!",
                "new_balance": str(user.profile.wallet_balance),
                "transaction_id": tx.id,
                "plan": plan.plan_name,
                "phone": phone,
                "amount_paid": str(tx.amount_customer_paid),
                "order_id": response.get('order_id', req_id)
            }, status=status.HTTP_200_OK)
        
        else:
            # 4. REFUND ON API FAILURE
            TransactionService.process_refund(user, plan.price, result.reference, "API Failure (Mobile)")
            return Response({
                "message": f"Provider Error: {response.get('remarks', 'Try again later')}. Funds refunded."
            }, status=status.HTTP_400_BAD_REQUEST)

    except Exception as e:
        TransactionService.process_refund(user, plan.price, result.reference, "System Crash (Mobile)")
        return Response({"message": f"System error occurred. Funds refunded. Detail: {str(e)}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

@csrf_exempt
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def api_buy_airtime(request):
    """
    Production-grade Mobile Airtime Purchase.
    Uses TransactionService for atomic balance deductions and audit tracking.
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
        return Response({"message": "Invalid Transaction PIN"}, status=status.HTTP_401_UNAUTHORIZED)

    # 0. NETWORK MAPPING (String to ClubKonnect ID)
    network_map = {
        'MTN': '01',
        'GLO': '02',
        'AIRTEL': '03',
        '9MOBILE': '04'
    }
    
    # Handle matching regardless of case
    network_id = network_map.get(str(network).upper())

    if not network_id:
        return Response({"message": f"Invalid Network: {network}. Select MTN, GLO, AIRTEL, or 9MOBILE."}, status=status.HTTP_400_BAD_REQUEST)

    try:
        amount = float(amount_str)
        if amount <= 0:
            raise ValueError
    except ValueError:
        return Response({"message": "Invalid amount"}, status=status.HTTP_400_BAD_REQUEST)

    # CTO LOGIC: 1:1 Charge (No discount)
    selling_price = Decimal(str(amount)).quantize(Decimal('0.01'))
    cost_price = Decimal(str(amount * 0.97)).quantize(Decimal('0.01')) # Assuming 3% is what they charge YOU
    
    # 1. ACQUISITION OF LOCK & ATOMIC DEBIT
    success, tx = TransactionService.process_debit(
        user=user,
        amount=selling_price,
        service_type=f"Airtime Purchase (Mobile)",
        plan_name=f"{network} {amount}",
        recipient=phone,
        reference=f"ATM-{int(time.time())}",
        description=f"Mobile Airtime purchase of {network} {amount} for {phone}",
        cost_price=cost_price
    )

    if not success:
        return Response({"message": f"Transaction failed: {tx}"}, status=status.HTTP_400_BAD_REQUEST)

    # 2. CALL PROVIDER API
    try:
        ck = ClubKonnectService()
        response, req_id = ck.buy_airtime(network_id, amount, phone)

        if response.get('status') in ['ORDER_RECEIVED', 'SUCCESSFUL']:
            # 3. SUCCESS - Finalize record
            Transaction.objects.filter(reference=tx.reference).update(status="Successful")
            final_tx = Transaction.objects.get(reference=tx.reference)
            final_tx.calculate_totals()
            
            return Response({
                "message": "Airtime Sent!",
                "new_balance": str(user.profile.wallet_balance),
                "transaction_id": final_tx.id,
                "network": network,
                "amount": str(amount),
                "phone": phone,
                "amount_paid": str(final_tx.amount_customer_paid),
                "order_id": response.get('order_id', req_id)
            }, status=status.HTTP_200_OK)
        
        else:
            # 4. REFUND ON API FAILURE
            TransactionService.process_refund(user, selling_price, tx.reference, "API Failure (Mobile)")
            return Response({
                "message": f"Provider Error: {response.get('remark', 'Try again later')}. Funds refunded."
            }, status=status.HTTP_400_BAD_REQUEST)

    except Exception as e:
        TransactionService.process_refund(user, selling_price, tx.reference, "System Crash (Mobile)")
        return Response({"message": f"System error occurred. Funds refunded. Detail: {str(e)}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

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

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def api_validate_cable(request):
    """Mobile API for real-time decoder verification."""
    cable_tv = request.GET.get('cable_tv') # gotv, dstv, etc
    smart_card = request.GET.get('smart_card')
    
    if not cable_tv or not smart_card:
        return Response({"error": "Missing cable_tv or smart_card"}, status=400)
    
    service = ClubKonnectService()
    result = service.validate_decoder(cable_tv, smart_card)
    return Response(result)

@csrf_exempt
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def api_buy_cable(request):
    """
    Secure Mobile Cable TV purchase.
    Uses TransactionService for atomic deductions and PIN verification.
    """
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
        return Response({"message": "Invalid Transaction PIN"}, status=401)

    # 2. GET PLAN
    try:
        plan = CablePlan.objects.get(plan_id=package_id)
    except CablePlan.DoesNotExist:
        return Response({"message": "Invalid Package ID"}, status=400)

    # 3. ATOMIC DEBIT
    success, tx = TransactionService.process_debit(
        user=user,
        amount=plan.price,
        service_type=f"Cable: {plan.name} (Mobile)",
        plan_name=f"{plan.cable_type.upper()}: {plan.name}",
        recipient=smart_card,
        reference=f"CB-{int(time.time())}",
        description=f"Mobile Subscription for {plan.name} on {smart_card}",
        cost_price=plan.cost_price
    )

    if not success:
        return Response({"message": f"Transaction failed: {tx}"}, status=400)

    # 4. CALL PROVIDER API
    try:
        ck = ClubKonnectService()
        response, req_id = ck.buy_cable(cable_tv, package_id, smart_card, phone)

        if response.get('status') == 'ORDER_RECEIVED':
            # SUCCESS
            Transaction.objects.filter(reference=tx.reference).update(status="Successful")
            final_tx = Transaction.objects.get(reference=tx.reference)
            final_tx.calculate_totals()
            
            return Response({
                "message": "Subscription Received Successfully",
                "new_balance": str(user.profile.wallet_balance),
                "transaction_id": final_tx.id,
                "order_id": response.get('orderid', req_id)
            })
        
        else:
            # FAILURE - REFUND
            TransactionService.process_refund(user, plan.price, tx.reference, "API Failure (Mobile)")
            return Response({
                "message": f"Provider Error: {response.get('remark', 'Try again later')}",
                "status": response.get('status')
            }, status=400)

    except Exception as e:
        TransactionService.process_refund(user, plan.price, tx.reference, "System Crash (Mobile)")
        return Response({"message": f"System error: {str(e)}"}, status=500)

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def api_validate_meter(request):
    """Mobile API for real-time meter verification."""
    disco = request.GET.get('disco')
    meter_no = request.GET.get('meter_no')
    meter_type = request.GET.get('meter_type')
    
    if not disco or not meter_no or not meter_type:
        return Response({"error": "Missing disco, meter_no, or meter_type"}, status=400)
    
    service = ClubKonnectService()
    result = service.validate_meter(disco, meter_no, meter_type)
    return Response(result)

@csrf_exempt
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def api_pay_electricity(request):
    """
    Secure Mobile Electricity bill payment.
    Uses TransactionService for atomic deductions and PIN verification.
    """
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
        return Response({"message": "Invalid Transaction PIN"}, status=401)

    try:
        amount = Decimal(str(amount_str))
        if amount < 1000:
            return Response({"message": "Minimum amount for electricity is ₦1,000"}, status=400)
    except (InvalidOperation, ValueError, TypeError):
        return Response({"message": "Invalid amount"}, status=400)

    # 2. Calculation (Bill + BT Service Fee)
    service_fee = Decimal('100.00')
    total_to_pay = amount + service_fee

    # 3. ATOMIC DEBIT (Fintech Grade)
    success, tx = TransactionService.process_debit(
        user=user,
        amount=total_to_pay,
        service_type=f"Electricity: {disco} (Mobile)",
        plan_name=f"Electricity: {disco} ({'Prepaid' if meter_type == '01' else 'Postpaid'})",
        recipient=meter_no,
        reference=f"ELE-{int(time.time())}",
        description=f"Mobile Bill Payment for {meter_no}. Amount: ₦{amount}, Fee: ₦{service_fee}",
        cost_price=amount
    )

    if not success:
        return Response({"message": f"Transaction failed: {tx}"}, status=400)

    # 4. CALL PROVIDER API
    try:
        ck = ClubKonnectService()
        response, req_id = ck.pay_electricity(disco, meter_no, meter_type, amount, phone)

        if response.get('status') == 'ORDER_RECEIVED':
            # Mark Successful
            TxModel.objects.filter(reference=tx.reference).update(
                status="Successful",
                bt_service_charge=service_fee
            )
            final_tx = TxModel.objects.get(reference=tx.reference)
            final_tx.calculate_totals()
            
            return Response({
                "message": "Payment Successful",
                "token": response.get('metertoken', 'Processing...'),
                "new_balance": str(user.profile.wallet_balance),
                "transaction_id": final_tx.id,
                "order_id": response.get('orderid', req_id)
            })
        
        else:
            # FAILURE - REFUND
            TransactionService.process_refund(user, total_to_pay, tx.reference, "API Failure (Mobile)")
            return Response({
                "message": f"Provider Error: {response.get('remark', 'Try again later')}",
                "status": response.get('status')
            }, status=400)

    except Exception as e:
        TransactionService.process_refund(user, total_to_pay, tx.reference, "System Crash (Mobile)")
        return Response({"message": f"System error: {str(e)}"}, status=500)
