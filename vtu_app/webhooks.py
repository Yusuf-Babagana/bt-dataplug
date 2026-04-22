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
                
                # YOUR PROFIT RULE: Hard-coded 50 Naira deduction
                BT_FIXED_FEE = Decimal('50.00')
                
                # Calculate what the customer actually gets
                if amount_paid > BT_FIXED_FEE:
                    credit_amount = amount_paid - BT_FIXED_FEE
                else:
                    # If they send 50 or less, they get 0 (protects from losing money)
                    credit_amount = Decimal('0.00')
                
                monnify_fee = BT_FIXED_FEE # Record the deduction as the fee
                # -----------------------

                # Get the Reference (e.g., "REF-1")
                product_data = event_data.get('product', {})
                raw_ref = product_data.get('reference') or event_data.get('paymentReference')
                if raw_ref:
                    # 2. Duplicate Protection: Check if this reference was already processed
                    if Transaction.objects.filter(reference=raw_ref, service_type="Wallet Funding").exists():
                        logger.info(f"MONNIFY_WEBHOOK: Duplicate notification for Ref {raw_ref} ignored.")
                        return HttpResponse(status=200)

                    # Extract the ID from the REF-{user_id}-{timestamp} format
                    parts = str(raw_ref).split('-')
                    if len(parts) >= 2 and parts[1].isdigit():
                        user_id = parts[1]
                    else:
                        # Fallback for unexpected formats
                        user_id = "".join(filter(str.isdigit, str(raw_ref)))
                    
                    # 3. Update Wallet
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
