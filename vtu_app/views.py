import json
import logging
from decimal import Decimal, InvalidOperation
from django.shortcuts import render, redirect
from django.contrib.auth.models import User
from django.contrib.auth import login
from django.contrib import messages
from django.db import transaction
from django.db.models import Sum
from django.http import JsonResponse
from .models import Profile, DataPlan, Transaction as TxModel, CablePlan
from .services import MonnifyService, ClubKonnectService
from .plan_data import DATA_PLANS

logger = logging.getLogger(__name__)

# Create your views here.

def test_connection(request):
    service = ClubKonnectService()
    balance = service.get_balance()
    return render(request, 'test.html', {'balance': balance})

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
                print(f"Monnify Error: {response.get('responseMessage')}")
                # We remove the warning here so the user isn't alarmed unnecessarily
        except Exception as e:
            print(f"Critical Account Generation Error: {e}")
            messages.warning(request, "Welcome! We're setting up your bank accounts shortly.")

        # 3. Log the user in and redirect to dashboard
        login(request, user)
        return redirect('dashboard')

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
    
    # Calculate Real Stats for this user
    total_spent = TxModel.objects.filter(
        user=request.user, 
        status="Successful"
    ).aggregate(Sum('amount'))['amount__sum'] or 0
    
    # Recent Transactions for the dashboard table
    recent_transactions = TxModel.objects.filter(user=request.user).order_by('-created_at')[:5]

    # Only show the ClubKonnect balance to you (the Admin)
    ck_balance = "0.00"
    if request.user.is_staff:
        ck_service = ClubKonnectService()
        res = ck_service.get_balance()
        ck_balance = res.get('balance', 'Error')
    
    context = {
        'ck_balance': ck_balance,
        'user_wallet': user_profile.wallet_balance,
        'total_spent': total_spent,
        'recent_transactions': recent_transactions,
        'plan_data_json': json.dumps(DATA_PLANS),
        'site_name': 'BT DataPlug',
        'pin_not_set': not user_profile.is_pin_set,
    }
    return render(request, 'vtu_app/dashboard.html', context)

def transaction_history(request):
    if not request.user.is_authenticated:
        return redirect('login')
        
    transactions = TxModel.objects.filter(user=request.user).order_by('-created_at')
    return render(request, 'vtu_app/transactions.html', {'transactions': transactions})

def buy_data(request):
    if not request.user.is_authenticated:
        return redirect('login')

    plans = DataPlan.objects.all().order_by('network', 'price')

    if request.method == 'POST':
        plan_id = request.POST.get('plan')
        phone = request.POST.get('phone')

        # Check Transaction PIN
        input_pin = request.POST.get('pin')
        user_profile = request.user.profile
        if input_pin != user_profile.transaction_pin:
            messages.error(request, "Invalid Transaction PIN!")
            return redirect('buy_data')

        try:
            plan = DataPlan.objects.get(id=plan_id)
        except DataPlan.DoesNotExist:
            messages.error(request, "Invalid plan selected.")
            return redirect('buy_data')

        user_profile = request.user.profile

        # 1. Check Balance
        if user_profile.wallet_balance < plan.price:
            messages.error(request, f"Insufficient balance! You need ₦{plan.price} but have ₦{user_profile.wallet_balance}.")
            return redirect('buy_data')

        # 2. Atomic Transaction: deduct first, refund if API fails
        try:
            with transaction.atomic():
                user_profile.wallet_balance -= plan.price
                user_profile.save()

                ck = ClubKonnectService()
                response, req_id = ck.buy_data(plan.network, plan.dataplan_id, phone)

                if response.get('status') == 'ORDER_RECEIVED':
                    tx = TxModel.objects.create(
                        user=request.user,
                        service_type="Data Purchase",
                        plan_name=plan.plan_name,
                        amount=plan.price,
                        recipient=phone,
                        status="Successful",
                        reference=req_id
                    )
                    logger.info(f"DATA_PURCHASE_SUCCESS: User {request.user.id} bought {plan.plan_name} for {phone}")
                    return redirect('receipt', tx_id=tx.id)
                
                # --- START OF ERROR MASKING UPDATES ---
                elif response.get('status') == 'INSUFFICIENT_BALANCE':
                    # Refund on company wallet failure
                    user_profile.wallet_balance += plan.price
                    user_profile.save()
                    
                    # Mask the error for the client
                    messages.error(request, "This service is currently undergoing a brief technical update. Please try again in 10 minutes or choose another network.")
                    logger.error(f"ADMIN_ALERT: ClubKonnect balance empty for User {request.user.id}")
                
                else:
                    # Refund on all other general failures
                    user_profile.wallet_balance += plan.price
                    user_profile.save()
                    
                    # Extract error message for logging
                    error_msg = response.get('remark') or response.get('message') or str(response)
                    logger.warning(f"DATA_PURCHASE_FAILED: User {request.user.id} - Error: {error_msg}")
                    messages.error(request, "The network provider is currently busy. Please try again later.")
                # --- END OF ERROR MASKING UPDATES ---
        except Exception as e:
            # System crash or unexpected error: Safety Refund
            user_profile.wallet_balance = Profile.objects.get(user=request.user).wallet_balance + plan.price
            user_profile.save()
            logger.critical(f"DATA_PURCHASE_CRASH: User {request.user.id} - Error: {str(e)}")
            messages.error(request, "Connection timeout. Money refunded.")

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

        # Check Transaction PIN
        input_pin = request.POST.get('pin')
        user_profile = request.user.profile
        if input_pin != user_profile.transaction_pin:
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

        user_profile = request.user.profile

        if user_profile.wallet_balance < amount:
            messages.error(request, "Insufficient balance to perform this transaction.")
            return redirect('buy_airtime')

        try:
            with transaction.atomic():
                user_profile.wallet_balance -= amount
                user_profile.save()

                ck = ClubKonnectService()
                response, req_id = ck.buy_airtime(network, amount, phone)

                if response.get('status') == 'ORDER_RECEIVED':
                    tx = TxModel.objects.create(
                        user=request.user,
                        service_type="Airtime Top-up",
                        plan_name=f"{network} Airtime",
                        amount=amount,
                        recipient=phone,
                        status="Successful",
                        reference=req_id
                    )
                    logger.info(f"AIRTIME_PURCHASE_SUCCESS: User {request.user.id} - ₦{amount} for {phone}")
                    return redirect('receipt', tx_id=tx.id)
                
                elif response.get('status') == 'INSUFFICIENT_BALANCE':
                    user_profile.wallet_balance += amount
                    user_profile.save()
                    messages.error(request, "Service temporarily unavailable. Please try again later.")
                    logger.error(f"ADMIN_ALERT: ClubKonnect balance empty for Airtime - User {request.user.id}")
                else:
                    user_profile.wallet_balance += amount
                    user_profile.save()
                    messages.error(request, "Network provider is currently busy. Try again shortly.")
                    logger.warning(f"AIRTIME_PURCHASE_FAILED: {response.get('status')}")
        except Exception as e:
            user_profile.wallet_balance = Profile.objects.get(user=request.user).wallet_balance + amount
            user_profile.save()
            logger.critical(f"AIRTIME_PURCHASE_CRASH: Error: {str(e)}")
            messages.error(request, "Connection error. Your money has been refunded.")

        return redirect('dashboard')

    return render(request, 'vtu_app/buy_airtime.html')


def buy_cable(request):
    if not request.user.is_authenticated:
        return redirect('login')
        
    plans = CablePlan.objects.all().order_by('cable_type', 'price')
    
    if request.method == 'POST':
        plan_id = request.POST.get('plan')
        smartcard = request.POST.get('smartcard')
        phone = request.POST.get('phone')
        
        # Check Transaction PIN
        input_pin = request.POST.get('pin')
        user_profile = request.user.profile
        if input_pin != user_profile.transaction_pin:
            messages.error(request, "Invalid Transaction PIN!")
            return redirect('buy_cable')

        try:
            plan = CablePlan.objects.get(id=plan_id)
        except CablePlan.DoesNotExist:
            messages.error(request, "Invalid plan selected.")
            return redirect('buy_cable')

        user_profile = request.user.profile

        if user_profile.wallet_balance < plan.price:
            messages.error(request, "Insufficient balance.")
            return redirect('buy_cable')

        try:
            with transaction.atomic():
                user_profile.wallet_balance -= plan.price
                user_profile.save()

                ck = ClubKonnectService()
                response, req_id = ck.buy_cable(plan.cable_type, plan.package_code, smartcard, phone)

                if response.get('status') == 'ORDER_RECEIVED':
                    tx = TxModel.objects.create(
                        user=request.user,
                        service_type="Cable TV",
                        plan_name=f"{plan.cable_type.upper()}: {plan.name}",
                        amount=plan.price,
                        recipient=smartcard,
                        status="Successful",
                        reference=req_id
                    )
                    logger.info(f"CABLE_PURCHASE_SUCCESS: User {request.user.id} - {plan.name}")
                    return redirect('receipt', tx_id=tx.id)
                else:
                    user_profile.wallet_balance += plan.price
                    user_profile.save()
                    messages.error(request, f"Failed: {response.get('status', 'Unknown Error')}")
                    logger.warning(f"CABLE_PURCHASE_FAILED: {response.get('status')}")
        except Exception as e:
            user_profile.wallet_balance = Profile.objects.get(user=request.user).wallet_balance + plan.price
            user_profile.save()
            logger.critical(f"CABLE_PURCHASE_CRASH: Error: {str(e)}")
            messages.error(request, "An unexpected error occurred. Money refunded.")

    return render(request, 'vtu_app/buy_cable.html', {'plans': plans})


def validate_cable(request):
    if not request.user.is_authenticated:
        return JsonResponse({'error': 'Unauthorized'}, status=401)
        
    cable_tv = request.GET.get('cable_tv')
    smartcard = request.GET.get('smartcard')
    
    if not cable_tv or not smartcard:
        return JsonResponse({'error': 'Missing parameters'}, status=400)
        
    ck = ClubKonnectService()
    res = ck.verify_cable(cable_tv, smartcard)
    return JsonResponse(res)

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
        profile.transaction_pin = pin
        profile.is_pin_set = True
        profile.save()
        
        messages.success(request, "Transaction PIN set successfully!")
        return redirect('dashboard')
        
    return render(request, 'vtu_app/set_pin.html')