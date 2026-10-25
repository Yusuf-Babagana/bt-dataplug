"""Business dashboard shown on the admin home page (/admin/).

Everything here is read-only reporting. `install()` wraps admin.site.index so the
numbers reach the `admin/index.html` template without changing any URLs.
"""
import logging
from datetime import timedelta
from decimal import Decimal

from django.contrib import admin
from django.contrib.auth.models import User
from django.core.cache import cache
from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone

from .models import (
    AccountDeletionRequest, CablePlan, DataPlan, Profile, ServiceSwitch,
    Transaction as TxModel,
)

logger = logging.getLogger('vtu_app')

ZERO = Decimal('0.00')
BALANCE_CACHE_KEY = 'admin_supplier_balance'


def _total(qs, field):
    return qs.aggregate(t=Sum(field))['t'] or ZERO


def _period(sales, since=None):
    qs = sales.filter(created_at__gte=since) if since else sales
    return {
        'revenue': _total(qs, 'amount_customer_paid'),
        'profit': _total(qs, 'net_profit'),
        'count': qs.count(),
    }


def _supplier_balance():
    """ClubKonnect/Nellobyte wallet balance, cached so the admin home stays fast."""
    cached = cache.get(BALANCE_CACHE_KEY)
    if cached is not None:
        return cached
    try:
        from .services import ClubKonnectService
        value = ClubKonnectService().get_balance()
    except Exception:
        logger.exception("ADMIN_SUPPLIER_BALANCE_FAILED")
        value = 'Unavailable'
    try:
        float(str(value).replace(',', ''))
        ttl = 120
    except (TypeError, ValueError):
        ttl = 30   # errors are retried sooner
    cache.set(BALANCE_CACHE_KEY, value, ttl)
    return value


def _chart(sales, days=14):
    today = timezone.localdate()
    start = today - timedelta(days=days - 1)
    rows = (
        sales.filter(created_at__date__gte=start)
        .annotate(day=TruncDate('created_at'))
        .values('day')
        .annotate(revenue=Sum('amount_customer_paid'), profit=Sum('net_profit'), n=Count('id'))
    )
    by_day = {r['day']: r for r in rows}
    points = []
    for i in range(days):
        d = start + timedelta(days=i)
        r = by_day.get(d)
        points.append({
            'label': d.strftime('%d %b'),
            'short': d.strftime('%a')[0],
            'revenue': r['revenue'] if r else ZERO,
            'profit': r['profit'] if r else ZERO,
            'count': r['n'] if r else 0,
        })
    peak = max((p['revenue'] for p in points), default=ZERO) or Decimal('1')
    for p in points:
        p['height'] = int((p['revenue'] / peak) * 100)
        p['profit_height'] = int((p['profit'] / peak) * 100) if p['profit'] > 0 else 0
    return points


def dashboard_context():
    now = timezone.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    sales = TxModel.objects.filter(status='Successful').exclude(service_type='Wallet Funding')

    month_sales = sales.filter(created_at__gte=now - timedelta(days=30))
    breakdown = list(
        month_sales.values('service_type')
        .annotate(revenue=Sum('amount_customer_paid'), profit=Sum('net_profit'), n=Count('id'))
        .order_by('-revenue')
    )
    top_rev = max((b['revenue'] for b in breakdown), default=ZERO) or Decimal('1')
    for b in breakdown:
        b['width'] = int((b['revenue'] / top_rev) * 100)

    top_users = list(
        month_sales.values('user__username')
        .annotate(spent=Sum('amount_customer_paid'), n=Count('id'))
        .order_by('-spent')[:5]
    )

    funding_today = TxModel.objects.filter(
        service_type='Wallet Funding', status='Successful', created_at__gte=today_start
    )
    problem = TxModel.objects.exclude(status__in=['Successful', 'Pending', 'Refunded'])
    pending_old = TxModel.objects.filter(status='Pending', created_at__lte=now - timedelta(minutes=10))

    bad_margin = (
        DataPlan.objects.filter(price__lte=models_cost('cost_price')).count()
        + CablePlan.objects.filter(price__lte=models_cost('cost_price')).count()
    )

    attention = []
    pending_deletions = AccountDeletionRequest.objects.filter(status='pending').count()
    if pending_deletions:
        attention.append({'tone': 'warn', 'icon': 'fa-user-xmark', 'text': f'{pending_deletions} account deletion request(s) waiting',
                          'url': '/admin/vtu_app/accountdeletionrequest/?status__exact=pending'})
    n = problem.filter(created_at__gte=now - timedelta(days=2)).count()
    if n:
        attention.append({'tone': 'bad', 'icon': 'fa-triangle-exclamation', 'text': f'{n} failed order(s) in the last 2 days',
                          'url': '/admin/vtu_app/transaction/?health=failed'})
    n = pending_old.count()
    if n:
        attention.append({'tone': 'warn', 'icon': 'fa-hourglass-half', 'text': f'{n} order(s) stuck pending for over 10 minutes',
                          'url': '/admin/vtu_app/transaction/?health=stuck'})
    if bad_margin:
        attention.append({'tone': 'bad', 'icon': 'fa-tags', 'text': f'{bad_margin} plan(s) priced at or below cost',
                          'url': '/admin/vtu_app/dataplan/?margin=negative'})
    off = [s.network for s in ServiceSwitch.objects.filter(is_active=False)]
    if off:
        attention.append({'tone': 'warn', 'icon': 'fa-power-off', 'text': 'Switched off: ' + ', '.join(off),
                          'url': '/admin/vtu_app/serviceswitch/'})

    return {
        'bi_today': _period(sales, today_start),
        'bi_week': _period(sales, now - timedelta(days=7)),
        'bi_month': _period(sales, now - timedelta(days=30)),
        'bi_all': _period(sales),
        'bi_liability': _total(Profile.objects.all(), 'wallet_balance'),
        'bi_supplier': _supplier_balance(),
        'bi_funding_today': _total(funding_today, 'amount_customer_paid'),
        'bi_users': User.objects.filter(is_active=True, is_staff=False).count(),
        'bi_new_today': User.objects.filter(date_joined__gte=today_start).count(),
        'bi_new_week': User.objects.filter(date_joined__gte=now - timedelta(days=7)).count(),
        'bi_kyc': Profile.objects.filter(kyc_verified=True).count(),
        'bi_chart': _chart(sales),
        'bi_breakdown': breakdown,
        'bi_top_users': top_users,
        'bi_recent': TxModel.objects.select_related('user').order_by('-created_at')[:8],
        'bi_attention': attention,
        'bi_switches': ServiceSwitch.objects.order_by('network'),
    }


def models_cost(field):
    from django.db.models import F
    return F(field)


def install():
    """Make admin.site.index pass the dashboard numbers to admin/index.html."""
    original = admin.site.index

    def index(request, extra_context=None):
        ctx = {}
        try:
            ctx = dashboard_context()
        except Exception:
            logger.exception("ADMIN_DASHBOARD_FAILED")
        ctx.update(extra_context or {})
        return original(request, ctx)

    admin.site.index = index
