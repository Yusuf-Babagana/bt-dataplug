from django.db.models.signals import post_save
from django.dispatch import receiver
from django.contrib.auth.models import User
from .models import Profile
from .services import MonnifyService

@receiver(post_save, sender=User)
def create_monnify_account(sender, instance, created, **kwargs):
    if created:
        # 1. Create the local profile (if not already handled)
        profile, _ = Profile.objects.get_or_create(user=instance)
        
        # 2. Call Monnify to reserve accounts
        monnify = MonnifyService()
        try:
            response = monnify.reserve_account(instance)
            if response.get('requestSuccessful'):
                # Extract accounts (Wema, Moniepoint, etc.)
                accounts = response['responseBody']['accounts']
                profile.bank_accounts = accounts
                profile.save()
        except Exception as e:
            print(f"Monnify Account Reservation Failed: {e}")
