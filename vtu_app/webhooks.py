import json
import hmac
import hashlib
import os
import logging
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from .models import Profile, Transaction
from django.contrib.auth.models import User
from decimal import Decimal

logger = logging.getLogger(__name__)

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
            logger.warning(f"BT DataPlug: Webhook Signature Mismatch! Remote IP: {request.META.get('REMOTE_ADDR')}")
            # In production, we strictly return 401 on mismatch
            return HttpResponse(status=401)

        try:
            data = json.loads(request.body)
            if data.get('eventType') == 'SUCCESSFUL_TRANSACTION':
                event_data = data.get('eventData')
                
                # Monnify sends amount as a float/string, we must convert carefully
                amount_paid = Decimal(str(event_data.get('amountPaid', 0)))
                
                # --- DYNAMIC MONNIFY FEE LOGIC ---
                # 1% capped at 50
                monnify_fee = amount_paid * Decimal('0.01')
                if monnify_fee > Decimal('50.00'):
                    monnify_fee = Decimal('50.00')
                
                credit_amount = amount_paid - monnify_fee
                # -----------------------

                # Get the Reference (e.g., "REF-1")
                product_data = event_data.get('product', {})
                raw_ref = product_data.get('reference') or event_data.get('paymentReference')

                if raw_ref:
                    # Extract the ID from the REF-{user_id}-{timestamp} format
                    parts = str(raw_ref).split('-')
                    if len(parts) >= 2 and parts[1].isdigit():
                        user_id = parts[1]
                    else:
                        # Fallback for unexpected formats
                        user_id = "".join(filter(str.isdigit, str(raw_ref)))
                    
                    # 2. Update Wallet
                    try:
                        profile = Profile.objects.get(user_id=user_id)
                        profile.wallet_balance += credit_amount
                        profile.save()

                        # 3. Create History with Deep Audit Fields
                        tx = Transaction.objects.create(
                            user=profile.user,
                            service_type="Wallet Funding",
                            plan_name="Monnify Transfer",
                            amount_customer_paid=amount_paid,
                            cost_from_klubconnect=Decimal('0.00'),
                            monnify_fee_on_this_tx=monnify_fee,
                            bt_service_charge=Decimal('0.00'),
                            recipient="Wallet",
                            status="Successful",
                            reference=raw_ref
                        )
                        tx.calculate_totals()
                        
                        logger.info(f"PAYMENT_RECEIVED: User {profile.user.username} credited with ₦{credit_amount} (Fee: ₦{monnify_fee})")
                        return HttpResponse(status=200)
                    except Profile.DoesNotExist:
                        logger.error(f"PAYMENT_ERROR: Profile for User ID {user_id} not found.")
                        return HttpResponse(status=404)

        except Exception as e:
            logger.error(f"BT DataPlug Webhook Error: {str(e)}")
            # Returning 500 triggers a 'Failure' retry from Monnify's perspective
            return HttpResponse(status=500)

    if request.method == 'GET':
        return HttpResponse("BT DataPlug Webhook Listener is Active! (Waiting for Monnify POST notifications)")

    return HttpResponse("Invalid request method. Expected POST.", status=405)
