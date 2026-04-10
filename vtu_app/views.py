from django.shortcuts import render, redirect
from django.http import JsonResponse
from django.db.models import Sum

from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth import login
from django.contrib import messages
from django.db import transaction
from .services import ClubKonnectService, MonnifyService
from .forms import DataPurchaseForm
from .models import DataPlan, Transaction as TxModel, CablePlan
import json
from decimal import Decimal, InvalidOperation
from .plan_data import DATA_PLANS

# Create your views here.

def test_connection(request):
    service = ClubKonnectService()
    balance = service.get_balance()
    return render(request, 'test.html', {'balance': balance})

def register(request):
    if request.method == 'POST':
        form = UserCreationForm(request.POST)
        if form.is_valid():
            user = form.save()
            
            # AUTOMATIC ACCOUNT GENERATION
            try:
                service = MonnifyService()
                response = service.reserve_account(user)
                if response.get('requestSuccessful'):
                    profile = user.profile
                    profile.bank_accounts = response['responseBody']['accounts']
                    profile.save()
            except Exception as e:
                print(f"[Auto-Account] Failed for {user.username}: {str(e)}")

            login(request, user)
            return redirect('dashboard')
    else:
        form = UserCreationForm()
    return render(request, 'registration/register.html', {'form': form})

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
                # Redirect to the receipt page with the transaction ID
                return redirect('receipt', tx_id=tx.id)
            
            # --- START OF ERROR MASKING UPDATES ---
            elif response.get('status') == 'INSUFFICIENT_BALANCE':
                # Refund on company wallet failure
                user_profile.wallet_balance += plan.price
                user_profile.save()
                
                # Mask the error for the client
                messages.error(request, "This service is currently undergoing a brief technical update. Please try again in 10 minutes or choose another network.")
                
                # Alert the Admin in the server logs
                print(f"!!! CRITICAL ADMIN ALERT: ClubKonnect balance is EMPTY. Recharge now to resume service. !!!")
            
            else:
                # Refund on all other general failures
                user_profile.wallet_balance += plan.price
                user_profile.save()
                
                # Extract error message for logging
                error_msg = response.get('remark') or response.get('message') or str(response)
                print(f"[BT DataPlug] Full API response: {response}")
                
                # Show a polite general error
                messages.error(request, "The network provider is currently busy. Please try again later.")
            # --- END OF ERROR MASKING UPDATES ---

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
                return redirect('receipt', tx_id=tx.id)
            
            elif response.get('status') == 'INSUFFICIENT_BALANCE':
                user_profile.wallet_balance += amount
                user_profile.save()
                messages.error(request, "Service temporarily unavailable. Please try again later.")
                print("!!! ADMIN ALERT: ClubKonnect Wallet Empty for Airtime !!!")
            else:
                user_profile.wallet_balance += amount
                user_profile.save()
                messages.error(request, "Network provider is currently busy. Try again shortly.")

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
        
        try:
            plan = CablePlan.objects.get(id=plan_id)
        except CablePlan.DoesNotExist:
            messages.error(request, "Invalid plan selected.")
            return redirect('buy_cable')

        user_profile = request.user.profile

        if user_profile.wallet_balance < plan.price:
            messages.error(request, "Insufficient balance.")
            return redirect('buy_cable')

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
                return redirect('receipt', tx_id=tx.id)
            else:
                user_profile.wallet_balance += plan.price
                user_profile.save()
                messages.error(request, f"Failed: {response.get('status', 'Unknown Error')}")

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