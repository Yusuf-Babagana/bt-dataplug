import logging
from datetime import timedelta

from django.contrib import admin, messages
from django.db.models import Count, Q
from django.utils import timezone

from . import services, webhooks
from .models import ApiApplication, ApiRequestLog, ApiTransaction, DeveloperAccount, WebhookDelivery

logger = logging.getLogger('vtu_app')


@admin.register(DeveloperAccount)
class DeveloperAccountAdmin(admin.ModelAdmin):
    list_display = ('company_name', 'user', 'status', 'app_count', 'created_at')
    list_filter = ('status',)
    search_fields = ('company_name', 'user__username', 'user__email')
    actions = ['suspend', 'reactivate']

    @admin.display(description='Applications')
    def app_count(self, obj):
        return obj.applications.count()

    @admin.action(description='Suspend selected developers (blocks all their API keys)')
    def suspend(self, request, queryset):
        n = queryset.update(status='suspended')
        logger.warning('API_DEVELOPER_SUSPENDED by=%s count=%s', request.user.pk, n)
        self.message_user(request, f'{n} developer(s) suspended.')

    @admin.action(description='Reactivate selected developers')
    def reactivate(self, request, queryset):
        self.message_user(request, f'{queryset.update(status="active")} developer(s) reactivated.')


@admin.register(ApiApplication)
class ApiApplicationAdmin(admin.ModelAdmin):
    list_display = ('name', 'developer', 'environment', 'status', 'rate_limit_per_minute', 'requests_24h', 'last_used_at')
    list_filter = ('status', 'environment')
    list_editable = ('rate_limit_per_minute',)
    search_fields = ('name', 'public_key', 'developer__company_name', 'developer__user__username')
    readonly_fields = ('public_key', 'secret_hint', 'secret_rotated_at', 'created_at', 'last_used_at')
    exclude = ('secret_hash',)
    actions = ['approve', 'suspend', 'revoke']

    def get_queryset(self, request):
        since = timezone.now() - timedelta(hours=24)
        return super().get_queryset(request).annotate(_req24=Count('request_logs', filter=Q(request_logs__created_at__gte=since)))

    @admin.display(description='Requests 24h', ordering='_req24')
    def requests_24h(self, obj):
        return obj._req24

    @admin.action(description='Approve / activate (live applications need this before use)')
    def approve(self, request, queryset):
        n = queryset.exclude(status='revoked').update(status='active')
        logger.warning('API_APP_APPROVED by=%s count=%s', request.user.pk, n)
        self.message_user(request, f'{n} application(s) active.')

    @admin.action(description='Suspend (keys stop working, can be re-approved)')
    def suspend(self, request, queryset):
        n = queryset.exclude(status='revoked').update(status='suspended')
        logger.warning('API_APP_SUSPENDED by=%s count=%s', request.user.pk, n)
        self.message_user(request, f'{n} application(s) suspended.')

    @admin.action(description='Revoke credentials permanently')
    def revoke(self, request, queryset):
        for app in queryset:
            app.revoke()
            app.save()
        logger.warning('API_APP_REVOKED_BY_STAFF by=%s count=%s', request.user.pk, queryset.count())
        self.message_user(request, f'{queryset.count()} application(s) revoked.')


@admin.register(ApiTransaction)
class ApiTransactionAdmin(admin.ModelAdmin):
    list_display = ('reference', 'application', 'service', 'recipient', 'amount', 'status', 'is_test', 'refunded', 'created_at')
    list_filter = ('status', 'service', 'is_test', 'refunded')
    search_fields = ('reference', 'client_reference', 'recipient', 'provider_request_id', 'application__name')
    readonly_fields = [f.name for f in ApiTransaction._meta.fields]
    actions = ['reconcile_now', 'mark_successful', 'mark_failed_and_refund']
    date_hierarchy = 'created_at'

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.action(description='Ask the provider about selected pending live orders')
    def reconcile_now(self, request, queryset):
        n = 0
        for tx in queryset.filter(status='pending', is_test=False):
            services.reconcile(tx)
            n += 1
        self.message_user(request, f'Checked {n} order(s).')

    @admin.action(description='Resolve pending: mark SUCCESSFUL (provider confirmed delivery)')
    def mark_successful(self, request, queryset):
        if not request.user.is_superuser:
            return self.message_user(request, 'Superusers only.', messages.ERROR)
        for tx in queryset.filter(status='pending'):
            services.settle(tx.pk, 'successful', {'status': 'MANUAL'})
            logger.warning('API_MANUAL_SUCCESS ref=%s by=%s', tx.reference, request.user.pk)
        self.message_user(request, 'Done.')

    @admin.action(description='Resolve pending: mark FAILED and refund the developer wallet (provider confirmed no delivery)')
    def mark_failed_and_refund(self, request, queryset):
        if not request.user.is_superuser:
            return self.message_user(request, 'Superusers only.', messages.ERROR)
        for tx in queryset.filter(status='pending'):
            services.settle(tx.pk, 'failed', {'status': 'MANUAL'}, 'Cancelled after review by BT DataPlug.')
            logger.warning('API_MANUAL_REFUND ref=%s by=%s', tx.reference, request.user.pk)
        self.message_user(request, 'Done.')


@admin.register(ApiRequestLog)
class ApiRequestLogAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'application', 'method', 'path', 'status_code', 'error_code', 'ip_address', 'duration_ms')
    list_filter = ('status_code', 'error_code', 'application')
    search_fields = ('ip_address', 'public_key_tried', 'path')
    date_hierarchy = 'created_at'
    readonly_fields = [f.name for f in ApiRequestLog._meta.fields]

    def has_add_permission(self, request):
        return False


@admin.register(WebhookDelivery)
class WebhookDeliveryAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'application', 'event_type', 'status', 'attempts', 'last_status_code', 'last_error')
    list_filter = ('status', 'event_type')
    search_fields = ('event_id', 'transaction__reference')
    readonly_fields = [f.name for f in WebhookDelivery._meta.fields]
    actions = ['retry']

    def has_add_permission(self, request):
        return False

    @admin.action(description='Retry delivery now')
    def retry(self, request, queryset):
        for d in queryset.exclude(status='delivered'):
            d.status = 'failed'
            webhooks.deliver(d)
        self.message_user(request, 'Retried.')
