import logging
from decimal import Decimal
from django.db import transaction
from django.db.models import F
from ..models import Profile, WalletTransaction, Transaction as TxModel

logger = logging.getLogger('vtu_app')

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

                # 3. Create Service Log (Transaction) with Profit Tracking
                profit = Decimal(amount) - Decimal(cost_price)
                
                TxModel.objects.create(
                    user=user,
                    service_type=service_type,
                    plan_name=plan_name,
                    amount=amount,
                    selling_price=amount,
                    cost_price=cost_price,
                    profit=profit,
                    recipient=recipient,
                    status="Pending",
                    reference=reference
                )
                
                return True, profile
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
