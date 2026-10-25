import csv
import uuid
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.contrib import admin, messages
from django.contrib.auth.models import User
from django.db import transaction as db_transaction
from django.db.models import Sum, F
from django.http import HttpResponse
from django.template.response import TemplateResponse
from django.utils import timezone
from django.utils.html import format_html

from .models import (
    Profile, Transaction, DataPlan, CablePlan, WalletTransaction, Notification,
    PaystackTransaction, ServiceSwitch,
)
from .notifications import notify
from .services.transaction_service import TransactionService
from . import admin_dashboard

admin.site.site_header = "BT DataPlug"
admin.site.site_title = "BT DataPlug Admin"
admin.site.index_title = "Business control centre"
admin_dashboard.install()


def _badge(text, tone):
    return format_html('<span class="bt-badge bt-{}">{}</span>', tone, text)


def _is_owner(request):
    return request.user.is_superuser


# ---------------------------------------------------------------- Pricing
class MarginFilter(admin.SimpleListFilter):
    title = 'margin'
    parameter_name = 'margin'

    def lookups(self, request, model_admin):
        return [('negative', 'At or below cost'), ('positive', 'Profitable')]

    def queryset(self, request, queryset):
        if self.value() == 'negative':
            return queryset.filter(price__lte=F('cost_price'))
        if self.value() == 'positive':
            return queryset.filter(price__gt=F('cost_price'))
        return queryset


class PlanAdminBase(admin.ModelAdmin):
    list_editable = ('price', 'cost_price', 'additional_fee')
    list_per_page = 50
    actions = ['raise_5', 'raise_10', 'lower_5']

    @admin.display(description='Margin')
    def margin(self, obj):
        m = obj.price - obj.cost_price
        return _badge(f'₦{m:,.2f}', 'ok' if m > 0 else 'bad')

    def _bump(self, request, queryset, pct):
        factor = Decimal(100 + pct) / Decimal(100)
        count = 0
        for plan in queryset:
            plan.price = (plan.price * factor).quantize(Decimal('0.01'))
            plan.save(update_fields=['price'])
            count += 1
        self.message_user(request, f"Updated {count} plan price(s) by {pct:+d}%.")

    @admin.action(description="Raise selling price by 5%%", permissions=['owner'])
    def raise_5(self, request, queryset):
        self._bump(request, queryset, 5)

    @admin.action(description="Raise selling price by 10%%", permissions=['owner'])
    def raise_10(self, request, queryset):
        self._bump(request, queryset, 10)

    @admin.action(description="Lower selling price by 5%%", permissions=['owner'])
    def lower_5(self, request, queryset):
        self._bump(request, queryset, -5)

    def has_owner_permission(self, request):
        return _is_owner(request)


@admin.register(DataPlan)
class DataPlanAdmin(PlanAdminBase):
    list_display = ('plan_name', 'network', 'dataplan_id', 'cost_price', 'price', 'additional_fee', 'margin')
    list_filter = ('network', MarginFilter)
    search_fields = ('plan_name', 'dataplan_id')
    ordering = ('network', 'price')


@admin.register(CablePlan)
class CablePlanAdmin(PlanAdminBase):
    list_display = ('name', 'cable_type', 'plan_id', 'cost_price', 'price', 'additional_fee', 'margin')
    list_filter = ('cable_type', MarginFilter)
    search_fields = ('name', 'plan_id')
    ordering = ('cable_type', 'price')


@admin.register(ServiceSwitch)
class ServiceSwitchAdmin(admin.ModelAdmin):
    list_display = ('network', 'state', 'is_active', 'updated_at')
    list_display_links = ('network',)
    list_editable = ('is_active',)
    ordering = ('network',)

    @admin.display(description='State')
    def state(self, obj):
        return _badge('LIVE', 'ok') if obj.is_active else _badge('OFF', 'bad')


# ----------------------------------------------------------- Transactions
class HealthFilter(admin.SimpleListFilter):
    title = 'order health'
    parameter_name = 'health'

    def lookups(self, request, model_admin):
        return [('failed', 'Failed (last 2 days)'), ('stuck', 'Pending over 10 min'), ('today', 'Today only')]

    def queryset(self, request, queryset):
        now = timezone.now()
        if self.value() == 'failed':
            return queryset.exclude(status__in=['Successful', 'Pending', 'Refunded']).filter(created_at__gte=now - timedelta(days=2))
        if self.value() == 'stuck':
            return queryset.filter(status='Pending', created_at__lte=now - timedelta(minutes=10))
        if self.value() == 'today':
            return queryset.filter(created_at__date=timezone.localdate())
        return queryset


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'user', 'service_type', 'plan_name', 'recipient', 'amount_customer_paid', 'cost_from_klubconnect', 'net_profit', 'state')
    list_filter = (HealthFilter, 'service_type', 'status', 'created_at')
    search_fields = ('user__username', 'recipient', 'reference', 'plan_name')
    ordering = ('-created_at',)
    date_hierarchy = 'created_at'
    list_per_page = 40
    list_select_related = ('user',)
    readonly_fields = ('user', 'service_type', 'plan_name', 'amount_customer_paid', 'cost_from_klubconnect',
                       'monnify_fee_on_this_tx', 'bt_service_charge', 'net_profit', 'recipient', 'reference', 'created_at')
    actions = ['refund_to_wallet', 'export_csv']

    @admin.display(description='Status', ordering='status')
    def state(self, obj):
        tone = {'Successful': 'ok', 'Pending': 'warn', 'Refunded': 'info'}.get(obj.status, 'bad')
        return _badge(obj.status, tone)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        ok = Transaction.objects.filter(status="Successful").exclude(service_type='Wallet Funding')
        extra_context = extra_context or {}
        extra_context['total_revenue'] = ok.aggregate(t=Sum('amount_customer_paid'))['t'] or 0
        extra_context['total_profit'] = ok.aggregate(t=Sum('net_profit'))['t'] or 0
        return super().changelist_view(request, extra_context=extra_context)

    @admin.action(description="Refund selected failed/pending orders to wallet", permissions=['owner'])
    def refund_to_wallet(self, request, queryset):
        done = skipped = 0
        for tx in queryset:
            if tx.status in ('Successful', 'Refunded') or tx.service_type == 'Wallet Funding' or not tx.reference:
                skipped += 1
                continue
            ok, _ = TransactionService.process_refund(
                tx.user, tx.amount_customer_paid, tx.reference, f"Admin refund ({request.user.username})")
            if ok:
                notify(tx.user, "Order refunded", f"₦{tx.amount_customer_paid:,.2f} for {tx.plan_name} was returned to your wallet.")
                done += 1
            else:
                skipped += 1
        if done:
            self.message_user(request, f"{done} order(s) refunded to the customers' wallets.", messages.SUCCESS)
        if skipped:
            self.message_user(request, f"{skipped} skipped (already successful or refunded, wallet funding, no reference, or refund failed).", messages.WARNING)

    @admin.action(description="Export selected to CSV")
    def export_csv(self, request, queryset):
        resp = HttpResponse(content_type='text/csv')
        resp['Content-Disposition'] = f'attachment; filename="transactions-{timezone.localdate()}.csv"'
        w = csv.writer(resp)
        w.writerow(['Date', 'User', 'Service', 'Plan', 'Recipient', 'Paid', 'Cost', 'Profit', 'Status', 'Reference'])
        for t in queryset.select_related('user'):
            w.writerow([t.created_at.strftime('%Y-%m-%d %H:%M'), t.user.username, t.service_type, t.plan_name,
                        t.recipient, t.amount_customer_paid, t.cost_from_klubconnect, t.net_profit, t.status, t.reference])
        return resp

    def has_owner_permission(self, request):
        return _is_owner(request)


@admin.register(PaystackTransaction)
class PaystackTransactionAdmin(admin.ModelAdmin):
    list_display = ('reference', 'user', 'amount', 'status', 'created_at')
    list_filter = ('status', 'created_at')
    search_fields = ('reference', 'user__username')
    ordering = ('-created_at',)
    date_hierarchy = 'created_at'


@admin.register(WalletTransaction)
class WalletTransactionAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'user', 'kind', 'amount', 'previous_balance', 'new_balance', 'reference')
    list_filter = ('transaction_type', 'created_at')
    search_fields = ('user__username', 'reference', 'description')
    ordering = ('-created_at',)
    date_hierarchy = 'created_at'
    list_select_related = ('user',)

    @admin.display(description='Type', ordering='transaction_type')
    def kind(self, obj):
        return _badge('CREDIT', 'ok') if obj.transaction_type == 'CREDIT' else _badge('DEBIT', 'warn')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


# ------------------------------------------------------- Users & wallets
class BalanceFilter(admin.SimpleListFilter):
    title = 'wallet'
    parameter_name = 'wallet'

    def lookups(self, request, model_admin):
        return [('funded', 'Has balance'), ('empty', 'Empty'), ('big', 'Over ₦50,000')]

    def queryset(self, request, queryset):
        if self.value() == 'funded':
            return queryset.filter(wallet_balance__gt=0)
        if self.value() == 'empty':
            return queryset.filter(wallet_balance__lte=0)
        if self.value() == 'big':
            return queryset.filter(wallet_balance__gt=50000)
        return queryset


@admin.register(Profile)
class ProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'email', 'phone_number', 'wallet', 'kyc_verified', 'account', 'referred_by', 'joined')
    search_fields = ('user__username', 'user__email', 'phone_number', 'referral_code')
    list_filter = (BalanceFilter, 'kyc_verified', 'user__is_active')
    list_select_related = ('user', 'referred_by')
    ordering = ('-user__date_joined',)
    list_per_page = 40
    exclude = ('transaction_pin',)
    readonly_fields = ('wallet_balance', 'referral_code', 'bank_accounts')
    actions = ['adjust_wallet', 'suspend_users', 'activate_users']

    @admin.display(description='Email', ordering='user__email')
    def email(self, obj):
        return obj.user.email

    @admin.display(description='Wallet', ordering='wallet_balance')
    def wallet(self, obj):
        return f"₦{obj.wallet_balance:,.2f}"

    @admin.display(description='Account', ordering='user__is_active')
    def account(self, obj):
        return _badge('ACTIVE', 'ok') if obj.user.is_active else _badge('SUSPENDED', 'bad')

    @admin.display(description='Joined', ordering='user__date_joined')
    def joined(self, obj):
        return obj.user.date_joined.strftime('%d %b %Y')

    def has_owner_permission(self, request):
        return _is_owner(request)

    @admin.action(description="Suspend selected users", permissions=['owner'])
    def suspend_users(self, request, queryset):
        n = User.objects.filter(profile__in=queryset, is_superuser=False).update(is_active=False)
        self.message_user(request, f"{n} user(s) suspended.", messages.WARNING)

    @admin.action(description="Reactivate selected users", permissions=['owner'])
    def activate_users(self, request, queryset):
        n = User.objects.filter(profile__in=queryset).update(is_active=True)
        self.message_user(request, f"{n} user(s) reactivated.", messages.SUCCESS)

    @admin.action(description="Credit / debit wallet (with audit trail)", permissions=['owner'])
    def adjust_wallet(self, request, queryset):
        profiles = list(queryset.select_related('user'))
        ctx = dict(self.admin_site.each_context(request), title="Adjust wallet", profiles=profiles,
                   opts=self.model._meta, action_checkbox_name=admin.helpers.ACTION_CHECKBOX_NAME)
        template = 'admin/vtu_app/wallet_adjust.html'
        if 'apply' not in request.POST:
            return TemplateResponse(request, template, ctx)
        try:
            amount = Decimal(request.POST.get('amount', '0')).quantize(Decimal('0.01'))
            if amount <= 0:
                raise InvalidOperation
        except InvalidOperation:
            self.message_user(request, "Enter an amount greater than zero.", messages.ERROR)
            return TemplateResponse(request, template, ctx)
        kind = request.POST.get('kind')
        reason = (request.POST.get('reason') or '').strip()
        if kind not in ('CREDIT', 'DEBIT') or not reason:
            self.message_user(request, "Choose credit or debit and give a reason.", messages.ERROR)
            return TemplateResponse(request, template, ctx)
        done = 0
        for p in profiles:
            with db_transaction.atomic():
                prof = Profile.objects.select_for_update().get(pk=p.pk)
                old = prof.wallet_balance
                new = old + amount if kind == 'CREDIT' else old - amount
                if new < 0:
                    self.message_user(request, f"{p.user.username}: balance ₦{old:,.2f} is too low to debit ₦{amount:,.2f}.", messages.ERROR)
                    continue
                prof.wallet_balance = new
                prof.save()
                WalletTransaction.objects.create(
                    user=prof.user, amount=amount, previous_balance=old, new_balance=new,
                    transaction_type=kind, reference=f"ADM-{uuid.uuid4().hex[:12].upper()}",
                    description=f"Admin {kind.lower()} by {request.user.username}: {reason}")
            notify(p.user, "Wallet updated", f"Your wallet was {'credited' if kind == 'CREDIT' else 'debited'} with ₦{amount:,.2f}. {reason}")
            done += 1
        if done:
            self.message_user(request, f"Wallet {kind.lower()} of ₦{amount:,.2f} applied to {done} user(s).", messages.SUCCESS)
        return None


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ('title', 'user', 'is_read', 'created_at')
    list_filter = ('is_read', 'created_at')
    search_fields = ('title', 'message', 'user__username')
    ordering = ('-created_at',)


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
