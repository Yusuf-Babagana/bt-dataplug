from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from .models import Profile
import json
from decimal import Decimal

@csrf_exempt
def monnify_webhook(request):
    if request.method == 'POST':
        data = json.loads(request.body)
        
        # Verify transaction status
        if data['eventType'] == 'SUCCESSFUL_TRANSACTION':
            payment_ref = data['eventData']['customer']['customerReference']
            user_id = payment_ref.replace('REF-', '')
            amount_paid = data['eventData']['amountPaid']
            
            # Logic to credit user wallet
            profile = Profile.objects.get(user_id=user_id)
            profile.wallet_balance += Decimal(str(amount_paid))
            profile.save()
            
            return HttpResponse(status=200)
    return HttpResponse(status=400)
