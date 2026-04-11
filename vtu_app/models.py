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
    ]
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    service_type = models.CharField(max_length=50, choices=SERVICE_CHOICES)
    plan_name = models.CharField(max_length=100)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
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

    def __str__(self):
        return f"{self.user.username} - {self.service_type} - {self.amount}"


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
    price = models.DecimalField(max_digits=10, decimal_places=2)  # What you charge

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
    price = models.DecimalField(max_digits=10, decimal_places=2)

    def __str__(self):
        return f"{self.get_cable_type_display()} - {self.name} (₦{self.price})"
