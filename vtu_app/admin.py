from django.contrib import admin
from .models import Profile, Transaction, DataPlan

@admin.register(DataPlan)
class DataPlanAdmin(admin.ModelAdmin):
    list_display = ('plan_name', 'get_network_display', 'dataplan_id', 'price')
    list_filter = ('network',)
    search_fields = ('plan_name', 'dataplan_id')
    ordering = ('network', 'price')

@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = ('user', 'service_type', 'plan_name', 'amount', 'recipient', 'status', 'reference', 'created_at')
    list_filter = ('service_type', 'status')
    search_fields = ('user__username', 'recipient', 'reference')
    ordering = ('-created_at',)

@admin.register(Profile)
class ProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'wallet_balance', 'kyc_verified', 'phone_number')
    search_fields = ('user__username',)
