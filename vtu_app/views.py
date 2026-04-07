from django.shortcuts import render, redirect
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth import login
from django.contrib import messages
from .services import ClubKonnectService
from .forms import DataPurchaseForm
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

def generate_my_accounts(request):
    from .services import MonnifyService
    
    if request.method != 'POST':
        messages.error(request, "Invalid request method.")
        return redirect('dashboard')
        
    bvn = request.POST.get('bvn')
    if not bvn or len(bvn.strip()) < 11:
        messages.error(request, "A valid 11-digit BVN or NIN is required.")
        return redirect('dashboard')
        
    bvn = bvn.strip()
    service = MonnifyService()
    try:
        # Save BVN to profile for future reference
        profile = request.user.profile
        profile.national_id = bvn
        profile.save()
        
        response = service.reserve_account(request.user, bvn)
        
        # This will print the error in your PythonAnywhere Error Log
        print(f"Monnify Response: {response}") 

        if response.get('requestSuccessful') is True:
            profile.bank_accounts = response['responseBody']['accounts']
            profile.save()
            messages.success(request, "Success! Your bank accounts are ready.")
        else:
            # This will show you exactly what Monnify says is wrong
            error_msg = response.get('responseMessage', 'Unknown Error')
            messages.error(request, f"Monnify Refused: {error_msg}")
            
    except Exception as e:
        messages.error(request, f"System Error: {str(e)}")
        
    return redirect('dashboard')
