import json
import hmac
import hashlib
import os
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from .models import Profile, Transaction
from django.contrib.auth.models import User
from decimal import Decimal

@csrf_exempt
def monnify_webhook(request):
    """
    Bulletproof Monnify Webhook Listener for Production.
    Handles signature verification, amount conversion, and user identification.
    """
    if request.method == 'POST':
        # 1. Verification
        signature = request.headers.get('monnify-signature')
        secret_key = str(os.getenv('MONNIFY_SECRET_KEY', '')).strip()
        computed_hash = hmac.new(secret_key.encode(), request.body, hashlib.sha512).hexdigest()

        if computed_hash != signature:
            print("BT DataPlug: Webhook Signature Mismatch!")
            # In production, we strictly return 401 on mismatch
            return HttpResponse(status=401)

        try:
            data = json.loads(request.body)
            if data.get('eventType') == 'SUCCESSFUL_TRANSACTION':
                event_data = data.get('eventData')
                
                # Monnify sends amount as a float/string, we must convert carefully
                amount_paid = Decimal(str(event_data.get('amountPaid', 0)))
                
                # Get the Reference (e.g., "REF-1")
                # In some Monnify versions, it's under 'paymentReference' or 'product' -> 'reference'
                product_data = event_data.get('product', {})
                raw_ref = product_data.get('reference') or event_data.get('paymentReference')

                if raw_ref:
                    # Extract only digits to get the User ID (REF-1 becomes 1)
                    user_id = "".join(filter(str.isdigit, str(raw_ref)))
                    
                    # 2. Update Wallet
                    try:
                        profile = Profile.objects.get(user_id=user_id)
                        profile.wallet_balance += amount_paid
                        profile.save()

                        # 3. Create History
                        Transaction.objects.create(
                            user=profile.user,
                            service_type="Wallet Funding",
                            plan_name="Monnify Transfer",
                            amount=amount_paid,
                            recipient="Wallet",
                            status="Successful"
                        )
                        print(f"BT DataPlug: Success! Credited {profile.user.username} with {amount_paid}")
                        return HttpResponse(status=200)
                    except Profile.DoesNotExist:
                        print(f"BT DataPlug: Error - Profile for User ID {user_id} not found.")
                        return HttpResponse(status=404)

        except Exception as e:
            print(f"BT DataPlug Webhook Error: {str(e)}")
            # Returning 500 triggers a 'Failure' retry from Monnify's perspective
            return HttpResponse(status=500)

    if request.method == 'GET':
        return HttpResponse("BT DataPlug Webhook Listener is Active! (Waiting for Monnify POST notifications)")

    return HttpResponse("Invalid request method. Expected POST.", status=405)
