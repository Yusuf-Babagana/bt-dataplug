import logging
from decimal import Decimal
from django.db import transaction
from django.db.models import F
from ..models import Profile, WalletTransaction, Transaction as TxModel, PaystackTransaction
from ..notifications import notify

logger = logging.getLogger('vtu_app')


def resolve_paystack_user(data):
    """
    Figure out which BT DataPlug user a Paystack transaction belongs to.

    Preferred: the app sends `metadata: {"user_id": ...}` when it initializes
    the charge, which Paystack echoes back on verify/webhook. Falls back to
    matching the paying card's email against a user's account email.
    """
    from django.contrib.auth.models import User

    metadata = data.get('metadata') or {}
    user_id = metadata.get('user_id') if isinstance(metadata, dict) else None
    if user_id:
        try:
            return User.objects.get(id=user_id)
        except (User.DoesNotExist, ValueError, TypeError):
            pass

    email = (data.get('customer') or {}).get('email')
    if email:
        return User.objects.filter(email__iexact=email).first()

    return None


class TransactionService:
    @staticmethod
    def process_debit(user, amount, service_type, plan_name, recipient, reference, description, cost_price=Decimal('0.00')):
        """
        Fintech-grade balance deduction with race-condition protection.
        Now includes cost_price and profit tracking.
        """
        try:
            with transaction.atomic():
                # Lock the profile record for wait
                profile = Profile.objects.select_for_update().get(user=user)
                
                if profile.wallet_balance < amount:
                    return False, "Insufficient balance."

                old_balance = profile.wallet_balance
                new_balance = old_balance - amount
                
                # 1. Update Profile Balance
                profile.wallet_balance = new_balance
                profile.save()

                # 2. Create Audit Log (WalletTransaction)
                WalletTransaction.objects.create(
                    user=user,
                    amount=amount,
                    previous_balance=old_balance,
                    new_balance=new_balance,
                    transaction_type='DEBIT',
                    reference=f"WAL-{reference}",
                    description=description
                )

                # 3. Create Service Log (Transaction) with Deep Audit Fields
                tx = TxModel.objects.create(
                    user=user,
                    service_type=service_type,
                    plan_name=plan_name,
                    amount_customer_paid=amount,
                    cost_from_klubconnect=cost_price,
                    monnify_fee_on_this_tx=Decimal('0.00'), # Usually 0 for debits
                    bt_service_charge=Decimal('0.00'),      # Will be set by caller if needed
                    recipient=recipient,
                    status="Pending",
                    reference=reference
                )
                tx.calculate_totals()
                
                return True, tx
        except Exception as e:
            logger.error(f"DEBIT_ERROR: {str(e)}")
            return False, str(e)

    @staticmethod
    def process_refund(user, amount, reference, reason):
        """Standardized refund logic with audit log."""
        try:
            with transaction.atomic():
                profile = Profile.objects.select_for_update().get(user=user)
                old_balance = profile.wallet_balance
                new_balance = old_balance + amount
                
                profile.wallet_balance = new_balance
                profile.save()

                WalletTransaction.objects.create(
                    user=user,
                    amount=amount,
                    previous_balance=old_balance,
                    new_balance=new_balance,
                    transaction_type='CREDIT',
                    reference=f"REF-{reference}",
                    description=f"Refund: {reason}"
                )
                
                # Update original transaction status if it exists
                TxModel.objects.filter(reference=reference).update(status="Refunded")
                
                return True, new_balance
        except Exception as e:
            logger.error(f"REFUND_ERROR: {str(e)}")
            return False, str(e)

    @staticmethod
    def process_paystack_credit(user, paystack_data):
        """
        Idempotently credit `user`'s wallet for one successful Paystack
        transaction. Safe to call twice (or more) for the same reference —
        called from both the /verify/ endpoint and the webhook, and only the
        first call to actually see a given reference does any crediting.

        `paystack_data` is the `data` object from Paystack's verify response
        or webhook payload (must already be confirmed status == "success").

        Returns (credited, new_balance, paystack_tx):
          - credited=True the first time a reference is processed.
          - credited=False on any later call for the same reference — the
            current wallet balance is still returned, nothing is re-credited.
        """
        reference = paystack_data.get('reference')
        amount = (Decimal(str(paystack_data.get('amount', 0))) / Decimal('100')).quantize(Decimal('0.01'))

        with transaction.atomic():
            paystack_tx, created = PaystackTransaction.objects.select_for_update().get_or_create(
                reference=reference,
                defaults={
                    'user': user,
                    'amount': amount,
                    'status': 'successful',
                    'raw_response': paystack_data,
                }
            )

            if not created:
                logger.info(f"PAYSTACK_DUPLICATE: Reference {reference} already processed — skipping re-credit.")
                return False, Profile.objects.get(user=paystack_tx.user).wallet_balance, paystack_tx

            profile = Profile.objects.select_for_update().get(user=user)
            old_balance = profile.wallet_balance
            new_balance = old_balance + amount
            profile.wallet_balance = new_balance
            profile.save()

            tx = TxModel.objects.create(
                user=user,
                service_type="Wallet Funding (Card)",
                plan_name="Paystack Card Funding",
                amount_customer_paid=amount,
                cost_from_klubconnect=Decimal('0.00'),
                monnify_fee_on_this_tx=Decimal('0.00'),
                bt_service_charge=Decimal('0.00'),
                recipient="Wallet",
                status="Successful",
                reference=reference,
            )
            tx.calculate_totals()

            WalletTransaction.objects.create(
                user=user,
                amount=amount,
                previous_balance=old_balance,
                new_balance=new_balance,
                transaction_type='CREDIT',
                reference=f"PSK-{reference}",
                description=f"Paystack card funding — ref {reference}"
            )

        notify(user, "Wallet funded", f"₦{amount} was credited to your wallet.")
        logger.info(f"PAYSTACK_CREDIT: User {user.username} credited ₦{amount} (ref {reference})")

        return True, new_balance, paystack_tx
