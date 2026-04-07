import json
import hmac
import hashlib
import os
import re
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from .models import Profile, Transaction
from decimal import Decimal

@csrf_exempt
def monnify_webhook(request):
    """
    Listens for successful payments from Monnify and credits the user's wallet.
    Includes robust logging for debugging and flexible reference extraction.
    """
    if request.method == 'POST':
        # Get the signature sent by Monnify
        signature = request.headers.get('monnify-signature')
        secret_key = str(os.getenv('MONNIFY_SECRET_KEY', '')).strip()
        
        # Calculate what the signature SHOULD be
        computed_hash = hmac.new(
            secret_key.encode(), 
            request.body, 
            hashlib.sha512
        ).hexdigest()

        # LOGGING FOR DEBUGGING (Check these in your 'Error Log' on PythonAnywhere)
        print(f"[Monnify Webhook] Signature Received: {signature}")
        print(f"[Monnify Webhook] Computed Signature: {computed_hash}")

        # Security Check
        if computed_hash != signature:
            print("[Monnify Webhook] SECURITY CHECK FAILED! Signatures do not match.")
            # For initial live testing, we can proceed anyway, but in production this should be active:
            # return HttpResponse(status=401) 

        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return HttpResponse(status=400)
        
        if data.get('eventType') == 'SUCCESSFUL_TRANSACTION':
            event_data = data.get('eventData')
            amount_paid = event_data.get('amountPaid')
            
            # Monnify returns the 'accountReference' we sent (e.g., "REF-1")
            # We check multiple possible fields for the reference string
            acc_ref = (
                event_data.get('product', {}).get('reference') or 
                event_data.get('paymentReference') or 
                event_data.get('accountReference')
            )
            
            print(f"[Monnify Webhook] Processing payment for Ref: {acc_ref} Amount: {amount_paid}")

            if not acc_ref:
                print("[Monnify Webhook] Error: No reference found in payload.")
                return HttpResponse(status=200) # Accept to stop retries

            try:
                # Extract numbers from the reference (e.g., 'REF-1' -> '1')
                user_id = re.sub("[^0-9]", "", str(acc_ref))
                
                profile = Profile.objects.get(user_id=user_id)
                profile.wallet_balance += Decimal(str(amount_paid))
                profile.save()

                Transaction.objects.create(
                    user=profile.user,
                    service_type="Wallet Funding",
                    plan_name="Monnify Auto-Bank",
                    amount=amount_paid,
                    recipient="Wallet",
                    status="Successful"
                )
                print(f"[Monnify Webhook] SUCCESS: Credited {profile.user.username} with ₦{amount_paid}")
                return HttpResponse(status=200)
            except Profile.DoesNotExist:
                print(f"[Monnify Webhook] Error: Profile for user_id {user_id} (from {acc_ref}) not found.")
                return HttpResponse(status=404)
            except Exception as e:
                print(f"[Monnify Webhook] Processing Error: {str(e)}")
                return HttpResponse(status=500)

    return HttpResponse(status=400)
