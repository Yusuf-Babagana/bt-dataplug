import json
import logging
import time
from decimal import Decimal, InvalidOperation
from django.utils import timezone
from django.shortcuts import render, redirect
from django.contrib.auth.models import User
from django.contrib import messages
from django.contrib.auth import login, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Sum
from django.http import JsonResponse
from .models import Profile, DataPlan, Transaction as TxModel, CablePlan, WalletTransaction
from .services import MonnifyService, ClubKonnectService
from .services.transaction_service import TransactionService
from .plan_data import DATA_PLANS
from django.contrib.admin.views.decorators import staff_member_required
from datetime import timedelta

logger = logging.getLogger(__name__)

# Create your views here.

def test_connection(request):
    service = ClubKonnectService()
    balance = service.get_balance()
    return render(request, 'test.html', {'balance': balance})


def smart_redirect(user):
    """Sends staff/admin to Manager Dashboard, regular users to their dashboard."""
    if user.is_staff:
        return redirect('manager_dashboard')
    if not user.profile.is_pin_set:
        return redirect('set_pin')
    return redirect('dashboard')


def user_login(request):
    """Custom login view that redirects staff to the manager dashboard."""
    if request.user.is_authenticated:
        return smart_redirect(request.user)

    if request.method == 'POST':
        from django.contrib.auth import authenticate
        username = request.POST.get('username')
        password = request.POST.get('password')
        user = authenticate(request, username=username, password=password)
        if user is not None:
            login(request, user)
            logger.info(f"LOGIN: User {user.id} logged in (staff={user.is_staff})")
            return smart_redirect(user)
        else:
            messages.error(request, "Invalid username or password.")

    return render(request, 'registration/login.html')


def register(request):
    if request.method == 'POST':
        # Get data from the professional form we built
        username = request.POST.get('username')
        email = request.POST.get('email')
        first_name = request.POST.get('first_name')
        last_name = request.POST.get('last_name')
        password = request.POST.get('password')
        confirm_password = request.POST.get('confirm_password')

        # Basic validation
        if password != confirm_password:
            messages.error(request, "Passwords do not match.")
            return render(request, 'registration/register.html')

        if User.objects.filter(username=username).exists():
            messages.error(request, "Username already taken.")
            return render(request, 'registration/register.html')

        # 1. Create and Save the User
        user = User.objects.create_user(
            username=username, 
            email=email, 
            password=password,
            first_name=first_name,
            last_name=last_name
        )
        
        # 2. Call Monnify
        try:
            # Handle Referrals
            ref_code = request.POST.get('referral_code')
            if ref_code:
                referrer_profile = Profile.objects.filter(referral_code=ref_code.strip().upper()).first()
                if referrer_profile:
                    user.profile.referred_by = referrer_profile.user
                    user.profile.save()

            service = MonnifyService()
            response = service.reserve_account(user)
            
            if response.get('requestSuccessful'):
                accounts = response.get('responseBody', {}).get('accounts', [])
                if accounts:
                    profile = user.profile
                    profile.bank_accounts = accounts
                    profile.save()
                    # ONLY show success if accounts were actually saved
                    messages.success(request, f"Welcome {username}! Your funding accounts are ready.")
                else:
                    # If the API was successful but the bank hasn't finished generating the number
                    messages.info(request, "Account created! Your bank numbers will appear in a few seconds.")
            else:
                logger.error(f"MONNIFY_ERROR: {response.get('responseMessage')}")
        except Exception as e:
            logger.critical(f"ACCOUNT_GEN_CRASH: {str(e)}")
            messages.warning(request, "Welcome! We're setting up your bank accounts shortly.")

        # 3. Log the user in and redirect smartly
        login(request, user)
        if user.is_staff:
            return smart_redirect(user)
        messages.info(request, "Welcome! Please set a 4-digit Transaction PIN to secure your account.")
        return redirect('set_pin')

    return render(request, 'registration/register.html')

def ajax_get_balance(request):
    """Fetch the latest wallet balance for the live dashboard refresh."""
    if not request.user.is_authenticated:
        return JsonResponse({'error': 'Unauthorized'}, status=401)
    
    balance = request.user.profile.wallet_balance
    return JsonResponse({'balance': f"{balance:,.2f}"})

def dashboard(request):
    # Only logged-in users should see the dashboard
    if not request.user.is_authenticated:
        return redirect('login')
        
    user_profile = request.user.profile
    
    # MANDATORY PIN CHECK
    if not user_profile.is_pin_set:
        return redirect('set_pin')
    
    # Calculate Real Stats for this user
    total_spent = TxModel.objects.filter(
        user=request.user, 
        status="Successful"
    ).aggregate(Sum('amount_customer_paid'))['amount_customer_paid__sum'] or 0
    
    # Recent Transactions for the dashboard table
    recent_transactions = TxModel.objects.filter(user=request.user).order_by('-created_at')[:5]

    # Only show the ClubKonnect balance to you (the Admin)
    ck_balance = "0.00"
    if request.user.is_staff:
        ck_service = ClubKonnectService()
        ck_balance = ck_service.get_balance()
    
    # Generate the full referral link
    ref_link = request.build_absolute_uri(f"/register/?ref={user_profile.referral_code}")
    
    context = {
        'ck_balance': ck_balance,
        'user_wallet': user_profile.wallet_balance,
        'total_spent': total_spent,
        'recent_transactions': recent_transactions,
        'plan_data_json': json.dumps(DATA_PLANS),
        'site_name': 'BT DataPlug',
        'pin_not_set': not user_profile.is_pin_set,
        'referral_code': user_profile.referral_code,
        'ref_link': ref_link,
    }
    return render(request, 'vtu_app/dashboard.html', context)

def transaction_history(request):
    if not request.user.is_authenticated:
        return redirect('login')
        
    # MANDATORY PIN CHECK
    if not request.user.profile.is_pin_set:
        return redirect('set_pin')
        
    transactions = TxModel.objects.filter(user=request.user).order_by('-created_at')
    return render(request, 'vtu_app/transactions.html', {'transactions': transactions})

def buy_data(request):
    if not request.user.is_authenticated:
        return redirect('login')

    plans = DataPlan.objects.all().order_by('network', 'price')

    if request.method == 'POST':
        plan_id = request.POST.get('plan')
        phone = request.POST.get('phone')

        # SECURE PIN VERIFICATION (Hashed)
        input_pin = request.POST.get('pin')
        user_profile = request.user.profile
        if not user_profile.check_pin(input_pin):
            messages.error(request, "Invalid Transaction PIN!")
            return redirect('buy_data')

        try:
            plan = DataPlan.objects.get(id=plan_id)
        except DataPlan.DoesNotExist:
            messages.error(request, "Invalid plan selected.")
            return redirect('buy_data')

        user_profile = request.user.profile

        # ACQUISITION OF LOCK & ATOMIC DEBIT
        success, result = TransactionService.process_debit(
            user=request.user,
            amount=plan.price,
            service_type="Data Purchase",
            plan_name=plan.plan_name,
            recipient=phone,
            reference=f"DT-{int(time.time())}",
            description=f"Purchase of {plan.plan_name} for {phone}",
            cost_price=plan.cost_price
        )

        if not success:
            messages.error(request, f"Transaction failed: {result}")
            return redirect('buy_data')

        # CALL PROVIDER API
        try:
            ck = ClubKonnectService()
            response, req_id = ck.buy_data(plan.network, plan.dataplan_id, phone)

            if response.get('status') == 'ORDER_RECEIVED':
                TxModel.objects.filter(reference=result.reference).update(
                    status="Successful",
                    bt_service_charge=plan.additional_fee
                )
                tx = TxModel.objects.get(reference=result.reference)
                tx.calculate_totals()
                return redirect('receipt', tx_id=tx.id)
            
            else:
                # REFUND ON API FAILURE
                TransactionService.process_refund(request.user, plan.price, result.reference, "API Failure")
                messages.error(request, "Service provider is currently busy. Your funds have been refunded.")
                logger.warning(f"DATA_API_FAILED: {response.get('status')} - User {request.user.id}")

        except Exception as e:
            TransactionService.process_refund(request.user, plan.price, result.reference, "System Crash")
            logger.critical(f"DATA_CRASH: {str(e)}")
            messages.error(request, "System error occurred. Funds refunded.")

        return redirect('dashboard')

        return redirect('dashboard')

    return render(request, 'vtu_app/buy_data.html', {'plans': plans})

def complete_kyc(request):
    if not request.user.is_authenticated:
        return redirect('login')
        
    if request.method == 'POST':
        service = MonnifyService()
        try:
            response = service.reserve_account(request.user)
            
            if response.get('requestSuccessful'):
                profile = request.user.profile
                profile.bank_accounts = response['responseBody']['accounts']
                profile.kyc_verified = True # Mark as basic verified
                profile.save()
                messages.success(request, "Bank accounts generated successfully!")
                return redirect('dashboard')
            else:
                messages.error(request, f"Monnify error: {response.get('responseMessage')}")
        except Exception as e:
            messages.error(request, f"System error: {str(e)}")
    
    return render(request, 'vtu_app/kyc.html')


def receipt(request, tx_id):
    """Display a professional receipt for a completed data purchase."""
    if not request.user.is_authenticated:
        return redirect('login')
    try:
        # Ensure users can only see their own receipts
        tx = TxModel.objects.get(id=tx_id, user=request.user)
    except TxModel.DoesNotExist:
        messages.error(request, "Receipt not found.")
        return redirect('dashboard')
    return render(request, 'vtu_app/receipt.html', {'tx': tx})


def buy_airtime(request):
    if not request.user.is_authenticated:
        return redirect('login')

    if request.method == 'POST':
        network = request.POST.get('network')
        amount = request.POST.get('amount')
        phone = request.POST.get('phone')

        # SECURE PIN VERIFICATION
        input_pin = request.POST.get('pin')
        user_profile = request.user.profile
        if not user_profile.check_pin(input_pin):
            messages.error(request, "Invalid Transaction PIN!")
            return redirect('buy_airtime')

        try:
            amount = Decimal(amount)
        except (ValueError, TypeError, InvalidOperation):
            messages.error(request, "Invalid amount entered.")
            return redirect('buy_airtime')

        if amount < 50:
            messages.error(request, "Minimum airtime purchase is ₦50.")
            return redirect('buy_airtime')

        # ACQUISITION OF LOCK & ATOMIC DEBIT
        # Airtime profit is typically a percentage. Hardcoded to 2% profit for now.
        airtime_cost = amount * Decimal('0.98') 

        success, result = TransactionService.process_debit(
            user=request.user,
            amount=amount,
            service_type="Airtime Purchase",
            plan_name=f"{network} Airtime",
            recipient=phone,
            reference=f"AT-{int(time.time())}",
            description=f"Purchase of ₦{amount} {network} Airtime for {phone}",
            cost_price=airtime_cost
        )

        if not success:
            messages.error(request, f"Transaction failed: {result}")
            return redirect('buy_airtime')

        # CALL PROVIDER API
        try:
            ck = ClubKonnectService()
            response, req_id = ck.buy_airtime(network, amount, phone)

            if response.get('status') == 'ORDER_RECEIVED':
                TxModel.objects.filter(reference=result.reference).update(status="Successful")
                return redirect('receipt', tx_id=TxModel.objects.get(reference=result.reference).id)
            
            else:
                # REFUND ON API FAILURE
                TransactionService.process_refund(request.user, amount, result.reference, "API Failure")
                messages.error(request, "Service provider is currently busy. Your funds have been refunded.")
                logger.warning(f"AIRTIME_API_FAILED: {response.get('status')} - User {request.user.id}")

        except Exception as e:
            TransactionService.process_refund(request.user, amount, result.reference, "System Crash")
            logger.critical(f"AIRTIME_CRASH: {str(e)}")
            messages.error(request, "System error occurred. Funds refunded.")

        return redirect('dashboard')

    return render(request, 'vtu_app/buy_airtime.html')
def buy_cable(request):
    """Professional Cable TV subscription view."""
    if not request.user.is_authenticated:
        return redirect('login')
        
    plans = CablePlan.objects.all().order_by('name')
    
    if request.method == 'POST':
        cable_tv = request.POST.get('cable_tv') # gotv, dstv
        package = request.POST.get('package')   # gotv-jolli
        smart_card = request.POST.get('smart_card')
        phone = request.POST.get('phone')
        input_pin = request.POST.get('pin')

        user_profile = request.user.profile
        
        # 1. PIN Verification
        if not user_profile.check_pin(input_pin):
            messages.error(request, "Invalid Transaction PIN!")
            return redirect('buy_cable')

        # 2. Get Plan from our DB to check price
        try:
            plan = CablePlan.objects.get(plan_id=package)
        except CablePlan.DoesNotExist:
            messages.error(request, "Invalid plan selected.")
            return redirect('buy_cable')

        # 3. ACQUISITION OF LOCK & ATOMIC DEBIT (Fintech Grade)
        success, result = TransactionService.process_debit(
            user=request.user,
            amount=plan.price,
            service_type=f"Cable: {plan.name}",
            plan_name=f"{plan.cable_type.upper()}: {plan.name}",
            recipient=smart_card,
            reference=f"CB-{int(time.time())}",
            description=f"Subscription for {plan.name} on {smart_card}",
            cost_price=plan.cost_price
        )

        if not success:
            messages.error(request, f"Transaction failed: {result}")
            return redirect('buy_cable')

        # 4. CALL PROVIDER API
        try:
            ck = ClubKonnectService()
            response, req_id = ck.buy_cable(cable_tv, package, smart_card, phone)

            if response.get('status') == 'ORDER_RECEIVED':
                # Mark Successful
                TxModel.objects.filter(reference=result.reference).update(status="Successful")
                messages.success(request, f"Subscription for {smart_card} was successful!")
                return redirect('receipt', tx_id=TxModel.objects.get(reference=result.reference).id)
            
            else:
                # REFUND ON API FAILURE
                TransactionService.process_refund(request.user, plan.price, result.reference, "API Failure")
                messages.error(request, "Service provider is currently busy. Your funds have been refunded.")
                logger.warning(f"CABLE_API_FAILED: {response.get('status')} - User {request.user.id}")

        except Exception as e:
            TransactionService.process_refund(request.user, plan.price, result.reference, "System Crash")
            logger.critical(f"CABLE_CRASH: {str(e)}")
            messages.error(request, "System error occurred. Funds refunded.")

        return redirect('dashboard')

    return render(request, 'vtu_app/buy_cable.html', {'plans': plans})


def validate_cable(request):
    if not request.user.is_authenticated:
        return JsonResponse({'error': 'Unauthorized'}, status=401)
        
    cable_tv = request.GET.get('cable_tv')
    smartcard = request.GET.get('smartcard')
    
    if not cable_tv or not smartcard:
        return JsonResponse({'error': 'Missing parameters'}, status=400)
        
    ck = ClubKonnectService()
    # Using the new alias from the service
    result = ck.validate_decoder(cable_tv, smartcard)
    return JsonResponse(result)

def set_transaction_pin(request):
    """View to set or update the 4-digit transaction PIN."""
    if not request.user.is_authenticated:
        return redirect('login')
        
    if request.method == 'POST':
        pin = request.POST.get('pin')
        confirm_pin = request.POST.get('confirm_pin')
        
        if not pin or len(pin) != 4 or not pin.isdigit():
            messages.error(request, "PIN must be exactly 4 digits.")
            return render(request, 'vtu_app/set_pin.html')
            
        if pin != confirm_pin:
            messages.error(request, "PINs do not match.")
            return render(request, 'vtu_app/set_pin.html')
            
        profile = request.user.profile
        profile.set_pin(pin)
        
        logger.info(f"PIN_SETUP: User {request.user.id} configured their Transaction PIN.")
        messages.success(request, "Transaction PIN set successfully!")
        return redirect('dashboard')
        
    return render(request, 'vtu_app/set_pin.html')


def manager_dashboard(request):
    """CTO/Owner-only Business Intelligence Dashboard."""
    if not request.user.is_authenticated or not request.user.is_staff:
        return redirect('dashboard')

    today = timezone.now().date()

    # 1. Total Wallet Liability (money owed to users)
    total_wallet_balances = Profile.objects.aggregate(Sum('wallet_balance'))['wallet_balance__sum'] or 0

    # 2. Today's Performance
    todays_tx = TxModel.objects.filter(created_at__date=today, status="Successful")
    daily_revenue = todays_tx.aggregate(Sum('amount_customer_paid'))['amount_customer_paid__sum'] or 0
    daily_profit = todays_tx.aggregate(Sum('net_profit'))['net_profit__sum'] or 0
    daily_count = todays_tx.count()

    # 3. All-Time Stats
    all_success = TxModel.objects.filter(status="Successful")
    total_revenue = all_success.aggregate(Sum('amount_customer_paid'))['amount_customer_paid__sum'] or 0
    total_profit = all_success.aggregate(Sum('net_profit'))['net_profit__sum'] or 0
    total_count = all_success.count()

    # 4. Breakdown by service type (today)
    service_breakdown = (
        todays_tx.values('service_type')
        .annotate(revenue=Sum('amount_customer_paid'), profit=Sum('net_profit'))
        .order_by('-revenue')
    )

    service = ClubKonnectService()
    api_balance = service.get_balance()
    print(f"DEBUG: CK Balance Response is: {api_balance}") # Check your server logs for this

    context = {
        'api_balance': api_balance,
        'total_wallet_balances': total_wallet_balances,
        'daily_revenue': daily_revenue,
        'daily_profit': daily_profit,
        'daily_count': daily_count,
        'total_revenue': total_revenue,
        'total_profit': total_profit,
        'total_count': total_count,
        'recent_sales': todays_tx.order_by('-created_at')[:20],
        'service_breakdown': service_breakdown,
        'today': today,
    }
    return render(request, 'vtu_app/manager_dashboard.html', context)

@staff_member_required
def staff_dashboard(request):
    today = timezone.now().date()
    yesterday = today - timedelta(days=1)
    
    # Aggregates
    stats = {
        'total_revenue': TxModel.objects.filter(status='Successful').aggregate(Sum('amount_customer_paid'))['amount_customer_paid__sum'] or 0,
        'total_profit': TxModel.objects.filter(status='Successful').aggregate(Sum('net_profit'))['net_profit__sum'] or 0,
        'today_sales': TxModel.objects.filter(status='Successful', created_at__date=today).count(),
        'today_profit': TxModel.objects.filter(status='Successful', created_at__date=today).aggregate(Sum('net_profit'))['net_profit__sum'] or 0,
    }
    
    # Recent Transactions for the table
    recent_tx = TxModel.objects.all().order_by('-created_at')[:10]
    
    return render(request, 'staff/dashboard.html', {'stats': stats, 'recent_tx': recent_tx})

@login_required
def profile_settings(request):
    if request.method == 'POST':
        # Logic for password change
        if 'change_password' in request.POST:
            old_pass = request.POST.get('old_password')
            new_pass = request.POST.get('new_password')
            if request.user.check_password(old_pass):
                request.user.set_password(new_pass)
                request.user.save()
                update_session_auth_hash(request, request.user) # Keeps user logged in
                messages.success(request, "Password updated successfully!")
            else:
                messages.error(request, "Incorrect old password.")
        return redirect('profile_settings')
    
    # Generate the full referral link
    # In production, use your actual domain
    ref_link = f"https://www.btdataplug.com/register/?ref={request.user.profile.referral_code}"
    
    return render(request, 'vtu_app/profile.html', {'ref_link': ref_link})

@login_required
def change_pin_view(request):
    if request.method == 'POST':
        old_pin = request.POST.get('old_pin')
        new_pin = request.POST.get('new_pin')
        confirm_pin = request.POST.get('confirm_pin')

        profile = request.user.profile

        # 1. Verify Old PIN (Secure Check)
        if not profile.check_pin(old_pin):
            messages.error(request, "The current PIN you entered is incorrect.")
        
        # 2. Match New PINs
        elif new_pin != confirm_pin:
            messages.error(request, "New PIN and Confirmation do not match.")
        
        # 3. Validation (Ensure 4 digits)
        elif not new_pin.isdigit() or len(new_pin) != 4:
            messages.error(request, "PIN must be exactly 4 digits.")
            
        else:
            # 4. Save New PIN (Secure Hashing)
            profile.set_pin(new_pin)
            messages.success(request, "Transaction PIN updated successfully! You can now use it for purchases.")
            return redirect('profile_settings')

    return render(request, 'dashboard/change_pin.html')

def referral_redirect(request, ref_id):
    """
    Handles clean referral links like bt-dataplug.com/ref/3 or bt-dataplug.com/ref/BT-123456
    Redirects to the registration page with the ref parameter.
    """
    from .models import Profile
    
    # 1. Try to find by Referral Code (Modern way)
    profile = Profile.objects.filter(referral_code=ref_id.strip().upper()).first()
    
    # 2. If not found and ref_id is numeric, try to find by User ID (Legacy compatibility)
    if not profile and str(ref_id).isdigit():
        profile = Profile.objects.filter(user__id=ref_id).first()
        
    ref_param = profile.referral_code if profile else ref_id
    return redirect(f"/register/?ref={ref_param}")


@login_required
def electricity_view(request):
    """View to handle Electricity Bill Payments (Realigned)."""
    if request.method == 'POST':
        disco_code = request.POST.get('disco')
        meter_no = request.POST.get('meter_no')
        meter_type = request.POST.get('meter_type')
        amount_str = request.POST.get('amount')
        input_pin = request.POST.get('pin')

        user_profile = request.user.profile

        # 1. PIN Verification (Required Security)
        if not user_profile.check_pin(input_pin):
            messages.error(request, "Invalid Transaction PIN!")
            return redirect('electricity_view')

        try:
            amount = Decimal(amount_str)
            if amount < 1000:
                messages.error(request, "Minimum amount for electricity is ₦1,000.")
                return redirect('electricity_view')
        except (InvalidOperation, ValueError, TypeError):
            messages.error(request, "Invalid amount entered.")
            return redirect('electricity_view')

        # 2. Calculation (Bill + ₦100 Convenience Fee)
        service_fee = Decimal('100.00')
        total_to_pay = amount + service_fee

        # 3. Secure Atomic Debit
        success, result = TransactionService.process_debit(
            user=request.user,
            amount=total_to_pay,
            service_type=f"Electricity ({disco_code})",
            plan_name=f"Electricity: {disco_code} ({'Prepaid' if meter_type == '01' else 'Postpaid'})",
            recipient=meter_no,
            reference=f"ELE-{int(time.time())}",
            description=f"Bill Payment for {meter_no}. Amnt: ₦{amount}, Fee: ₦{service_fee}",
            cost_price=amount
        )

        if not success:
            messages.error(request, f"Transaction failed: {result}")
            return redirect('electricity_view')

        # 4. API Call
        try:
            from .utils import ClubKonnectService as UtilityCK
            ck = UtilityCK()
            response, req_id = ck.buy_electricity(disco_code, meter_no, meter_type, amount, request.user.profile.phone_number or '08069278540')

            if response.get('status') == 'ORDER_RECEIVED':
                # Register Token and finalize
                TxModel.objects.filter(reference=result.reference).update(
                    status="Successful",
                    bt_service_charge=service_fee
                )
                final_tx = TxModel.objects.get(reference=result.reference)
                final_tx.calculate_totals()
                
                messages.success(request, f"Payment Successful! Token: {response.get('metertoken', 'Processing...')}")
                return redirect('receipt', tx_id=final_tx.id)
            
            else:
                # Refund logic
                TransactionService.process_refund(request.user, total_to_pay, result.reference, "Provider API Error")
                messages.error(request, f"Service provider error: {response.get('remark', 'Failed')}. Balance refunded.")

        except Exception as e:
            TransactionService.process_refund(request.user, total_to_pay, result.reference, "System Error")
            messages.error(request, "A system error occurred. Your balance has been fully refunded.")

        return redirect('dashboard')

    return render(request, 'dashboard/buy_electricity.html')


def validate_meter(request):
    """AJAX view for real-time meter verification."""
    if not request.user.is_authenticated:
        return JsonResponse({'error': 'Unauthorized'}, status=401)
        
    disco = request.GET.get('disco')
    meter = request.GET.get('meter')
    mtype = request.GET.get('type')
    
    if not all([disco, meter, mtype]):
        return JsonResponse({'error': 'Missing parameters'}, status=400)
        
    ck = ClubKonnectService()
    result = ck.validate_meter(disco, meter, mtype)
    return JsonResponse(result)