from django.db import models
from django.contrib.auth.models import User
import json

class Profile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    phone_number = models.CharField(max_length=15, unique=True, null=True, blank=True)
    wallet_balance = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    bvn = models.CharField(max_length=11, null=True, blank=True)
    nin = models.CharField(max_length=11, null=True, blank=True)
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
    package_code = models.CharField(max_length=50) # e.g., gotv-jolli
    cost_price = models.DecimalField(max_digits=10, decimal_places=2, default=0.00) # What you pay
    price = models.DecimalField(max_digits=10, decimal_places=2) # What you charge
    additional_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0.00) # Internal charge

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
