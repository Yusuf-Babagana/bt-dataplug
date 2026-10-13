from django.contrib import admin
from .models import Profile, Transaction, DataPlan

@admin.register(DataPlan)
class DataPlanAdmin(admin.ModelAdmin):
    list_display = ('plan_name', 'get_network_display', 'dataplan_id', 'price', 'additional_fee')
    list_filter = ('network',)
    search_fields = ('plan_name', 'dataplan_id')
    ordering = ('network', 'price')

from django.db.models import Sum
from .models import Profile, Transaction, DataPlan, CablePlan, WalletTransaction

@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = ('user', 'service_type', 'plan_name', 'amount_customer_paid', 'cost_from_klubconnect', 'net_profit', 'status', 'created_at')
    list_filter = ('service_type', 'status', 'created_at')
    search_fields = ('user__username', 'recipient', 'reference')
    ordering = ('-created_at',)
    
    def changelist_view(self, request, extra_context=None):
        # Calculate totals for successful transactions
        success_tx = Transaction.objects.filter(status="Successful")
        
        extra_context = extra_context or {}
        extra_context['total_revenue'] = success_tx.aggregate(Sum('amount_customer_paid'))['amount_customer_paid__sum'] or 0
        extra_context['total_profit'] = success_tx.aggregate(Sum('net_profit'))['net_profit__sum'] or 0
        
        return super().changelist_view(request, extra_context=extra_context)

@admin.register(Profile)
class ProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'wallet_balance', 'referral_code', 'referred_by', 'kyc_verified')
    search_fields = ('user__username', 'referral_code')
    list_filter = ('kyc_verified',)

from .models import Notification

@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ('title', 'user', 'is_read', 'created_at')
    list_filter = ('is_read', 'created_at')
    search_fields = ('title', 'message', 'user__username')
    ordering = ('-created_at',)

from .models import PaystackTransaction

@admin.register(PaystackTransaction)
class PaystackTransactionAdmin(admin.ModelAdmin):
    list_display = ('reference', 'user', 'amount', 'status', 'created_at')
    list_filter = ('status', 'created_at')
    search_fields = ('reference', 'user__username')
    ordering = ('-created_at',)

@admin.register(WalletTransaction)
class WalletTransactionAdmin(admin.ModelAdmin):
    list_display = ('user', 'transaction_type', 'amount', 'previous_balance', 'new_balance', 'reference', 'created_at')
    list_filter = ('transaction_type', 'created_at')
    search_fields = ('user__username', 'reference', 'description')
    ordering = ('-created_at',)

@admin.register(CablePlan)
class CablePlanAdmin(admin.ModelAdmin):
    list_display = ('name', 'get_cable_type_display', 'plan_id', 'price', 'cost_price', 'additional_fee')
    list_filter = ('cable_type',)
    search_fields = ('name', 'plan_id')
    ordering = ('cable_type', 'price')

from .models import ServiceSwitch

@admin.register(ServiceSwitch)
class ServiceSwitchAdmin(admin.ModelAdmin):
    list_display = ('network', 'is_active', 'updated_at')
    list_filter = ('is_active',)
    ordering = ('network',)


from django.utils import timezone
from django.contrib.auth.models import User
from rest_framework.authtoken.models import Token
from .models import AccountDeletionRequest, Notification as _Notification


def anonymize_user(user):
    """
    Closes an account: removes personal data and login access but keeps the
    financial Transaction / WalletTransaction / PaystackTransaction rows
    (needed for accounting and regulatory record-keeping), now unlinked from
    any identifying details.
    """
    Token.objects.filter(user=user).delete()
    _Notification.objects.filter(user=user).delete()
    profile = getattr(user, 'profile', None)
    if profile:
        profile.phone_number = None
        profile.bvn = None
        profile.nin = None
        profile.bank_accounts = None
        profile.referred_by = None
        profile.save()
    user.username = f"deleted_{user.id}"
    user.email = ''
    user.first_name = ''
    user.last_name = ''
    user.is_active = False
    user.is_staff = False
    user.is_superuser = False
    user.set_unusable_password()
    user.save()


@admin.register(AccountDeletionRequest)
class AccountDeletionRequestAdmin(admin.ModelAdmin):
    list_display = ('username', 'email', 'phone_number', 'status', 'created_at', 'processed_at')
    list_filter = ('status',)
    search_fields = ('username', 'email', 'phone_number')
    actions = ['delete_accounts', 'reject_requests']

    @admin.action(description="Delete account & personal data for selected requests")
    def delete_accounts(self, request, queryset):
        done = 0
        for req in queryset.filter(status='pending'):
            user = User.objects.filter(username__iexact=req.username).first()
            if user is None or user.is_superuser:
                self.message_user(request, f"No deletable account found for '{req.username}'.", level='warning')
                continue
            anonymize_user(user)
            req.status = 'completed'
            req.processed_at = timezone.now()
            req.save()
            done += 1
        self.message_user(request, f"{done} account(s) deleted.")

    @admin.action(description="Mark selected requests as rejected")
    def reject_requests(self, request, queryset):
        queryset.filter(status='pending').update(status='rejected', processed_at=timezone.now())
