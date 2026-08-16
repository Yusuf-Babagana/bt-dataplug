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
