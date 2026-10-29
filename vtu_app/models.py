from django.db import models
from django.contrib.auth.models import User
import json
import uuid

class Profile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    phone_number = models.CharField(max_length=15, unique=True, null=True, blank=True)
    wallet_balance = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    bvn = models.CharField(max_length=11, null=True, blank=True)
    nin = models.CharField(max_length=11, null=True, blank=True)
    
    # Referral System
    referral_code = models.CharField(max_length=12, unique=True, blank=True)
    referred_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='referrals')

    # KYC & PIN Security
    kyc_verified = models.BooleanField(default=False)
    transaction_pin = models.CharField(max_length=128, default="") # Hashed
    is_pin_set = models.BooleanField(default=False)
    
    # Store Monnify account details as JSON (Bank Name, Account Number, etc.)
    bank_accounts = models.JSONField(null=True, blank=True) 
    
    def set_pin(self, raw_pin):
        from django.contrib.auth.hashers import make_password
        self.transaction_pin = make_password(raw_pin)
        self.is_pin_set = True
        self.save()

    def check_pin(self, raw_pin):
        from django.contrib.auth.hashers import check_password
        return check_password(raw_pin, self.transaction_pin)

    def save(self, *args, **kwargs):
        from django.contrib.auth.hashers import make_password
        
        # 1. Initialize Default PIN (0000) if not set
        if not self.is_pin_set or not self.transaction_pin:
            self.transaction_pin = make_password("0000")
            self.is_pin_set = True

        # 2. Generate Referral Code
        if not self.referral_code:
            # Generate a clean code like BT-XXXXXX
            self.referral_code = f"BT-{uuid.uuid4().hex[:6].upper()}"
            
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.user.username}'s Profile"

class WalletTransaction(models.Model):
    """Deep audit trail for every single balance change."""
    TRANSACTION_TYPES = [
        ('CREDIT', 'Credit'),
        ('DEBIT', 'Debit'),
    ]
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    previous_balance = models.DecimalField(max_digits=12, decimal_places=2)
    new_balance = models.DecimalField(max_digits=12, decimal_places=2)
    transaction_type = models.CharField(max_length=10, choices=TRANSACTION_TYPES)
    reference = models.CharField(max_length=100, unique=True)
    description = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user.username} - {self.transaction_type} - {self.amount}"

class Transaction(models.Model):
    SERVICE_CHOICES = [
        ('Wallet Funding', 'Wallet Funding'),
        ('Data Purchase', 'Data Purchase'),
        ('Airtime Purchase', 'Airtime Purchase'),
        ('Cable TV', 'Cable TV'),
        ('Electricity Bill', 'Electricity Bill'),
    ]
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    service_type = models.CharField(max_length=50, choices=SERVICE_CHOICES)
    plan_name = models.CharField(max_length=100)
    
    # THE MONEY MAP (Deep Audit)
    amount_customer_paid = models.DecimalField(max_digits=12, decimal_places=2, default=0.00) # Gross Selling Price
    cost_from_klubconnect = models.DecimalField(max_digits=12, decimal_places=2, default=0.00) # Provider Cost
    monnify_fee_on_this_tx = models.DecimalField(max_digits=10, decimal_places=2, default=0.00) # Monnify Charge
    bt_service_charge = models.DecimalField(max_digits=10, decimal_places=2, default=0.00) # Internal Charge
    net_profit = models.DecimalField(max_digits=12, decimal_places=2, default=0.00) # Actual Margin
    
    recipient = models.CharField(max_length=20) # Phone number or "Wallet"
    status = models.CharField(max_length=20, default="Successful")
    reference = models.CharField(max_length=50, blank=True, null=True)  # ClubKonnect RequestID
    created_at = models.DateTimeField(auto_now_add=True)

    # VTU gateway bookkeeping (all optional so existing rows stay valid).
    provider = models.CharField(max_length=30, blank=True, default="")     # provider that holds the live order
    order_id = models.CharField(max_length=100, blank=True, default="", db_index=True)
    idempotency_key = models.CharField(max_length=100, null=True, blank=True)
    # Set exactly once when the wallet debit for this row is returned. Guards every refund path
    # (automatic, reconciliation, admin action, developer API) against crediting twice.
    refunded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "idempotency_key"],
                condition=models.Q(idempotency_key__isnull=False),
                name="vtu_txn_user_idem_uniq",
            ),
        ]

    @property
    def clean_plan_name(self):
        """Removes 'Daily', 'Weekly', 'Monthly' etc from plan name for cleaner receipts."""
        import re
        if not self.plan_name:
            return ""
        # Remove common duration keywords and parentheses
        cleaned = re.sub(r'\(?(?:daily|weekly|monthly|yearly)\)?', '', self.plan_name, flags=re.IGNORECASE)
        return cleaned.strip()

    def calculate_totals(self):
        # Profit = (Selling Price - Cost Price) - (Processing Fees)
        # Note: bt_service_charge is usually already inside amount_customer_paid
        self.net_profit = (self.amount_customer_paid - self.cost_from_klubconnect) - self.monnify_fee_on_this_tx
        self.save()

    def __str__(self):
        return f"{self.user.username} - {self.service_type} - {self.amount_customer_paid}"


class DataPlan(models.Model):
    NETWORK_CHOICES = [
        ('01', 'MTN'),
        ('02', 'Glo'),
        ('03', '9mobile'),
        ('04', 'Airtel'),
    ]
    network = models.CharField(max_length=2, choices=NETWORK_CHOICES)
    plan_name = models.CharField(max_length=100)  # e.g., MTN 1GB SME
    dataplan_id = models.CharField(max_length=10)  # e.g., 1000
    cost_price = models.DecimalField(max_digits=10, decimal_places=2, default=0.00) # What you pay
    price = models.DecimalField(max_digits=10, decimal_places=2)  # What you charge
    additional_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0.00) # Internal charge
    # Switched off automatically when the supplier answers INVALID_DATAPLAN; re-enable in the admin.
    # Whose plan-id scheme this plan uses. Imported Gongoz plans carry Gongoz's ids, which must never be
    # sent to Nellobytes (a different provider's code could buy the wrong bundle), and vice versa.
    source = models.CharField(max_length=20, default="nellobytes")
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.get_network_display()} - {self.plan_name} (₦{self.price})"


class CablePlan(models.Model):
    CABLE_TYPES = [
        ('dstv', 'DStv'),
        ('gotv', 'GOtv'),
        ('startimes', 'StarTimes'),
    ]
    cable_type = models.CharField(max_length=20, choices=CABLE_TYPES)
    name = models.CharField(max_length=100) # e.g., GOtv Jolli
    plan_id = models.CharField(max_length=50) # e.g., gotv-jolli
    cost_price = models.DecimalField(max_digits=10, decimal_places=2, default=0.00) # What you pay
    price = models.DecimalField(max_digits=10, decimal_places=2) # What you charge
    additional_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0.00) # Internal charge
    # Whose plan-id scheme this plan uses. Imported Gongoz plans carry Gongoz's ids, which must never be
    # sent to Nellobytes (a different provider's code could buy the wrong bundle), and vice versa.
    source = models.CharField(max_length=20, default="nellobytes")
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.get_cable_type_display()} - {self.name} (₦{self.price})"


class ServiceSwitch(models.Model):
    """Admin kill-switch to enable/disable networks from user purchase pages."""
    NETWORK_CHOICES = [
        ('MTN', 'MTN'),
        ('Glo', 'Glo'),
        ('9mobile', '9mobile'),
        ('Airtel', 'Airtel'),
    ]
    network = models.CharField(max_length=20, choices=NETWORK_CHOICES, unique=True)
    is_active = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        status = "ACTIVE" if self.is_active else "DISABLED"
        return f"{self.network}: {status}"


class Notification(models.Model):
    """Model for global and personal notification messages."""
    user = models.ForeignKey(User, on_delete=models.CASCADE, null=True, blank=True) # Null means "Global Message"
    title = models.CharField(max_length=200)
    message = models.TextField()
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.title} ({self.created_at.strftime('%Y-%m-%d')})"


class PaystackTransaction(models.Model):
    """
    Idempotency ledger for Paystack card-funding: one row per processed
    Paystack `reference`. Both the /verify/ endpoint and the webhook check
    this before crediting a wallet, so a reference can only ever be
    credited once no matter how many times either path fires for it.
    """
    reference = models.CharField(max_length=100, unique=True)
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(max_length=20, default="successful")
    raw_response = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user.username} - {self.reference} - ₦{self.amount}"


class AccountDeletionRequest(models.Model):
    """
    Public "delete my account" requests (Google Play requires a web URL where
    users can ask for this without opening the app). Staff verify the requester
    and then run the "Delete account & personal data" admin action.
    """
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('completed', 'Completed'),
        ('rejected', 'Rejected'),
    ]
    username = models.CharField(max_length=150)
    email = models.EmailField(blank=True)
    phone_number = models.CharField(max_length=20, blank=True)
    reason = models.TextField(blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='pending')
    created_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.username} - {self.status} ({self.created_at:%Y-%m-%d})"


class TransactionEvent(models.Model):
    """Append-only audit trail: every status change and wallet movement of a purchase Transaction."""
    transaction = models.ForeignKey(Transaction, on_delete=models.CASCADE, related_name="events")
    from_status = models.CharField(max_length=20, blank=True, default="")
    to_status = models.CharField(max_length=20)
    wallet_delta = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    balance_after = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    source = models.CharField(max_length=30, blank=True, default="")   # purchase / webhook / reconcile / status-poll / admin / api
    detail = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self):
        return f"{self.transaction_id}: {self.from_status or '-'} -> {self.to_status}"


class ProviderState(models.Model):
    """
    Runtime (non-secret) management data for one registered VTU provider: admin on/off switch,
    priority override and circuit-breaker health. Credentials live only in the environment.
    """
    name = models.CharField(max_length=30, unique=True)
    admin_enabled = models.BooleanField(default=True, help_text="Runtime switch; the provider must also be enabled in .env")
    priority_override = models.IntegerField(null=True, blank=True, help_text="Lower is tried first. Blank = use the .env / default priority")
    consecutive_failures = models.PositiveIntegerField(default=0)
    suspended_until = models.DateTimeField(null=True, blank=True)
    last_success_at = models.DateTimeField(null=True, blank=True)
    last_failure_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class PurchaseAttempt(models.Model):
    """One submission of a Transaction to one provider (failover creates several)."""
    SUBMITTING = "SUBMITTING"   # recorded BEFORE the call, so a crash mid-call still leaves a trace

    transaction = models.ForeignKey(Transaction, on_delete=models.CASCADE, related_name="attempts")
    provider = models.CharField(max_length=30)
    reference = models.CharField(max_length=100, unique=True, help_text="Request id sent to the provider")
    sequence = models.PositiveSmallIntegerField(default=1)
    outcome = models.CharField(max_length=12, default=SUBMITTING)   # SUBMITTING/ACCEPTED/REJECTED/AMBIGUOUS
    error_category = models.CharField(max_length=20, blank=True, default="")
    code = models.CharField(max_length=100, blank=True, default="")
    message = models.CharField(max_length=255, blank=True, default="")
    provider_order_id = models.CharField(max_length=100, blank=True, default="", db_index=True)
    response = models.JSONField(default=dict, blank=True)   # redacted
    elapsed_ms = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["transaction_id", "sequence"]

    def __str__(self):
        return f"{self.provider}#{self.sequence} {self.outcome} ({self.reference})"
