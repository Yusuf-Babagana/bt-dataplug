from django.shortcuts import render, redirect
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth import login
from django.contrib import messages
from django.db import transaction
from .services import ClubKonnectService, MonnifyService
from .forms import DataPurchaseForm, KYCForm
from .models import DataPlan, Transaction as TxModel
import json
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
            login(request, user)
            return redirect('dashboard')
    else:
        form = UserCreationForm()
    return render(request, 'registration/register.html', {'form': form})

def dashboard(request):
    # Only logged-in users should see the dashboard
    if not request.user.is_authenticated:
        return redirect('login')
        
    form = DataPurchaseForm()
    
    # Only show the ClubKonnect balance to you (the Admin)
    ck_balance = "0.00"
    provider_balance = "0.00" # Placeholder for non-staff
    
    if request.user.is_staff:
        ck_service = ClubKonnectService()
        res = ck_service.get_balance()
        ck_balance = res.get('balance', 'Error')
        provider_balance = ck_balance # Link for context if needed
    
    context = {
        'form': form,
        'ck_balance': ck_balance,
        'provider_balance': provider_balance,
        'user_wallet': request.user.profile.wallet_balance, # This is the user's money on your site
        'site_name': 'BT DataPlug',
        'plan_data_json': json.dumps(DATA_PLANS), # For the dynamic dropdown
    }
    return render(request, 'vtu_app/dashboard.html', context)

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
        form = KYCForm(request.POST)
        if form.is_valid():
            bvn = form.cleaned_data.get('bvn')
            nin = form.cleaned_data.get('nin')
            
            service = MonnifyService()
            try:
                response = service.reserve_account(request.user, bvn=bvn, nin=nin)
                
                if response.get('requestSuccessful'):
                    profile = request.user.profile
                    profile.bank_accounts = response['responseBody']['accounts']
                    profile.bvn = bvn
                    profile.nin = nin
                    profile.kyc_verified = True
                    profile.save()
                    messages.success(request, "KYC Completed! Bank accounts generated.")
                    return redirect('dashboard')
                else:
                    messages.error(request, f"Monnify Refused: {response.get('responseMessage')}")
            except Exception as e:
                messages.error(request, f"System Error: {str(e)}")
    else:
        form = KYCForm()
    
    return render(request, 'vtu_app/kyc.html', {'form': form})


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