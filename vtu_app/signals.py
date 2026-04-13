from django.db.models.signals import post_save
from django.dispatch import receiver
from django.contrib.auth.models import User
from .models import Profile
from .services import MonnifyService

@receiver(post_save, sender=User)
def create_user_profile(sender, instance, created, **kwargs):
    if created:
        # 1. Create the local profile ONLY to avoid duplicate API calls
        # The reservation is handled in the register/kyc views
        profile, _ = Profile.objects.get_or_create(user=instance)
