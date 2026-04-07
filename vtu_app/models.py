from django.db import models
from django.contrib.auth.models import User
import json

class Profile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    phone_number = models.CharField(max_length=15, unique=True, null=True, blank=True)
    national_id = models.CharField(max_length=11, null=True, blank=True) # BVN or NIN
    wallet_balance = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    
    # Store Monnify account details as JSON (Bank Name, Account Number, etc.)
    bank_accounts = models.JSONField(null=True, blank=True) 
    
    def __str__(self):
        return f"{self.user.username}'s Profile"
