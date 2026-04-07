from django.db import models
from django.contrib.auth.models import User
import json

class Profile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    phone_number = models.CharField(max_length=15, unique=True, null=True, blank=True)
    wallet_balance = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    bvn = models.CharField(max_length=11, null=True, blank=True)
    nin = models.CharField(max_length=11, null=True, blank=True)
    kyc_verified = models.BooleanField(default=False)
    
    # Store Monnify account details as JSON (Bank Name, Account Number, etc.)
    bank_accounts = models.JSONField(null=True, blank=True) 
    
    def __str__(self):
        return f"{self.user.username}'s Profile"

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
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user.username} - {self.service_type} - {self.amount}"
