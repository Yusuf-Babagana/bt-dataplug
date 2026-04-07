from django.shortcuts import render, redirect
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth import login
from django.contrib import messages
from .services import ClubKonnectService, MonnifyService
from .forms import DataPurchaseForm, KYCForm
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
    service = ClubKonnectService()
    provider_balance = service.get_balance()
    
    context = {
        'form': form,
        'provider_balance': provider_balance,
        'user_wallet': request.user.profile.wallet_balance, # This is the user's money on your site
        'site_name': 'BT DataPlug',
        'plan_data_json': json.dumps(DATA_PLANS), # For the dynamic dropdown
    }
    return render(request, 'vtu_app/dashboard.html', context)

def buy_data(request):
    if request.method == 'POST':
        # For now, we just simulate success until the API whitelist is ready
        messages.success(request, "Order received! Processing your data...")
        return redirect('dashboard')
    return redirect('dashboard')

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
