from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from .models import Profile, Transaction
from decimal import Decimal
import json
import hmac
import hashlib
import os

@csrf_exempt
def monnify_webhook(request):
    """
    Listens for successful payments from Monnify and credits the user's wallet.
    """
    if request.method == 'POST':
        # 1. Security Check: Verify the signature from Monnify
        signature = request.headers.get('monnify-signature')
        secret_key = str(os.getenv('MONNIFY_SECRET_KEY', '')).strip()
        
        if not secret_key:
            print("[Monnify Webhook] ERROR: MONNIFY_SECRET_KEY is not set in environment.")
            return HttpResponse(status=500)

        # Validate that the request actually came from Monnify
        computed_hash = hmac.new(
            secret_key.encode(), 
            request.body, 
            hashlib.sha512
        ).hexdigest()

        if computed_hash != signature:
            print(f"[Monnify Webhook] Invalid Signature! Got {signature}, expected {computed_hash}")
            return HttpResponse(status=401) # Unauthorized

        # 2. Parse the payment data
        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return HttpResponse(status=400)

        if data.get('eventType') == 'SUCCESSFUL_TRANSACTION':
            event_data = data.get('eventData')
            amount_paid = event_data.get('amountPaid')
            
            # Identify the user using the reference we set (e.g., REF-1)
            # Monnify sends accountReference for reserved accounts
            product_ref = event_data.get('accountReference')
            
            # Fallback checks just in case
            if not product_ref:
                product_ref = event_data.get('product', {}).get('reference')
            if not product_ref:
                product_ref = event_data.get('customer', {}).get('customerReference')

            if not product_ref or 'REF-' not in product_ref:
                print(f"[Monnify Webhook] Could not find 'REF-' reference in payload: {data}")
                return HttpResponse(status=200) # Accept so they stop retrying

            user_id = str(product_ref).replace('REF-', '')

            try:
                # 3. Credit the User's Wallet
                profile = Profile.objects.get(user_id=user_id)
                profile.wallet_balance += Decimal(str(amount_paid))
                profile.save()

                # 4. Record the Transaction for history
                Transaction.objects.create(
                    user=profile.user,
                    service_type="Wallet Funding",
                    plan_name="Bank Transfer",
                    amount=amount_paid,
                    recipient="Wallet",
                    status="Successful"
                )
                print(f"[Monnify Webhook] Success: Credited {profile.user.username} with ₦{amount_paid}")
                return HttpResponse(status=200)
                
            except Profile.DoesNotExist:
                print(f"[Monnify Webhook] ERROR: Profile for user_id {user_id} not found.")
                return HttpResponse(status=404)
            except Exception as e:
                print(f"[Monnify Webhook] SYSTEM ERROR: {str(e)}")
                return HttpResponse(status=500)

    return HttpResponse(status=400)
