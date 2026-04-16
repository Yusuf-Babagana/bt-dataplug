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
