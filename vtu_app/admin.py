from django.contrib import admin
from .models import Profile, Transaction, DataPlan

@admin.register(DataPlan)
class DataPlanAdmin(admin.ModelAdmin):
    list_display = ('plan_name', 'get_network_display', 'dataplan_id', 'price')
    list_filter = ('network',)
    search_fields = ('plan_name', 'dataplan_id')
    ordering = ('network', 'price')

from django.db.models import Sum
from .models import Profile, Transaction, DataPlan, CablePlan, WalletTransaction

@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = ('user', 'service_type', 'plan_name', 'selling_price', 'cost_price', 'profit', 'status', 'created_at')
    list_filter = ('service_type', 'status', 'created_at')
    search_fields = ('user__username', 'recipient', 'reference')
    ordering = ('-created_at',)
    
    def changelist_view(self, request, extra_context=None):
        # Calculate totals for successful transactions
        success_tx = Transaction.objects.filter(status="Successful")
        
        extra_context = extra_context or {}
        extra_context['total_revenue'] = success_tx.aggregate(Sum('selling_price'))['selling_price__sum'] or 0
        extra_context['total_profit'] = success_tx.aggregate(Sum('profit'))['profit__sum'] or 0
        
        return super().changelist_view(request, extra_context=extra_context)

@admin.register(Profile)
class ProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'wallet_balance', 'kyc_verified', 'phone_number')
    search_fields = ('user__username',)
