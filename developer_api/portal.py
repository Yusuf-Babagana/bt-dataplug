import logging
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import ApiApplication, ApiRequestLog, ApiTransaction, DeveloperAccount, SERVICES, WebhookDelivery
from .openapi import build_spec
from .security import validate_ip_whitelist, validate_webhook_url

logger = logging.getLogger('vtu_app')
MAX_APPS = 10


def _base_url(request):
    return f'{request.scheme}://{request.get_host()}/api/v1/dev'


# ---------------------------------------------------------------------------------------------
# Public documentation
# ---------------------------------------------------------------------------------------------
def docs(request):
    return render(request, 'developer/docs.html', {'base_url': _base_url(request)})


def openapi_json(request):
    return JsonResponse(build_spec(_base_url(request)))


def swagger(request):
    return render(request, 'developer/swagger.html')


# ---------------------------------------------------------------------------------------------
# Console (logged-in users)
# ---------------------------------------------------------------------------------------------
def _developer(request):
    return getattr(request.user, 'developer_account', None)


@login_required
def console(request):
    dev = _developer(request)
    apps = []
    if dev:
        since = timezone.now() - timedelta(hours=24)
        for app in dev.applications.all():
            agg = app.request_logs.filter(created_at__gte=since).aggregate(n=Count('id'), errors=Count('id', filter=Q(status_code__gte=400)))
            app.requests_24h, app.errors_24h = agg['n'], agg['errors']
            apps.append(app)
    return render(request, 'developer/console.html', {
        'dev': dev, 'apps': apps, 'balance': request.user.profile.wallet_balance, 'base_url': _base_url(request)})


@login_required
@require_POST
def enable(request):
    if _developer(request):
        return redirect('developer_console')
    company = (request.POST.get('company_name') or '').strip()[:120]
    if not company or not request.POST.get('accept_terms'):
        messages.error(request, 'Enter your business name and accept the developer terms.')
        return redirect('developer_console')
    website = (request.POST.get('website') or '').strip()
    DeveloperAccount.objects.create(user=request.user, company_name=company, website=website if website.startswith('http') else '')
    messages.success(request, 'Developer access enabled. Create a test application to get started.')
    return redirect('developer_console')


@login_required
@require_POST
def app_create(request):
    dev = _developer(request)
    if not dev or dev.status != 'active':
        return redirect('developer_console')
    name = (request.POST.get('name') or '').strip()[:80]
    env = request.POST.get('environment')
    if not name or env not in ('test', 'live'):
        messages.error(request, 'Give the application a name and choose test or live.')
        return redirect('developer_console')
    if dev.applications.exclude(status='revoked').count() >= MAX_APPS:
        messages.error(request, f'You can have at most {MAX_APPS} applications.')
        return redirect('developer_console')
    app = ApiApplication(developer=dev, name=name, environment=env, status='active' if env == 'test' else 'pending')
    secret = app.issue_credentials(new_public_key=True)
    app.save()
    request.session['new_secret'] = {'app': app.pk, 'secret': secret}
    if env == 'live':
        messages.info(request, 'Live applications must be approved by BT DataPlug before they can be used. Your test keys work immediately.')
    return redirect('developer_app', pk=app.pk)


@login_required
def app_detail(request, pk):
    dev = _developer(request)
    if not dev:
        return redirect('developer_console')
    app = get_object_or_404(ApiApplication, pk=pk, developer=dev)
    now = timezone.now()
    day, week = now - timedelta(hours=24), now - timedelta(days=7)
    logs = app.request_logs
    usage = {
        'req_24h': logs.filter(created_at__gte=day).count(),
        'err_24h': logs.filter(created_at__gte=day, status_code__gte=400).count(),
        'req_7d': logs.filter(created_at__gte=week).count(),
    }
    txs = app.transactions
    usage['tx_ok'] = txs.filter(status='successful').count()
    usage['tx_pending'] = txs.filter(status__in=['pending', 'processing']).count()
    usage['tx_failed'] = txs.filter(status__in=['failed', 'reversed']).count()
    secret_info = request.session.pop('new_secret', None)
    new_secret = secret_info['secret'] if secret_info and secret_info.get('app') == app.pk else None
    return render(request, 'developer/app_detail.html', {
        'dev': dev, 'app': app, 'usage': usage, 'new_secret': new_secret, 'all_services': SERVICES,
        'transactions': txs.all()[:15], 'request_logs': logs.all()[:15], 'deliveries': app.webhook_deliveries.all()[:15],
        'base_url': _base_url(request), 'balance': request.user.profile.wallet_balance})


@login_required
@require_POST
def app_action(request, pk):
    dev = _developer(request)
    app = get_object_or_404(ApiApplication, pk=pk, developer=dev) if dev else None
    if not app:
        return redirect('developer_console')
    if dev.status != 'active':
        messages.error(request, 'Your developer account is suspended.')
        return redirect('developer_app', pk=pk)
    action = request.POST.get('action')

    if action == 'rotate_secret':
        if app.status == 'revoked':
            messages.error(request, 'This application was revoked. Create a new one.')
        else:
            secret = app.issue_credentials()
            app.save()
            request.session['new_secret'] = {'app': app.pk, 'secret': secret}
            logger.warning('API_SECRET_ROTATED app=%s user=%s', app.pk, request.user.pk)
            messages.success(request, 'New secret generated. The old secret no longer works.')
    elif action == 'revoke':
        app.revoke()
        app.save()
        logger.warning('API_APP_REVOKED app=%s user=%s', app.pk, request.user.pk)
        messages.success(request, 'Application revoked. Its keys no longer work.')
    elif action == 'rotate_webhook_secret':
        app.webhook_secret_version += 1
        app.save(update_fields=['webhook_secret_version'])
        messages.success(request, 'Webhook signing secret rotated. Update your server with the new value.')
    elif action == 'settings':
        if app.status == 'revoked':
            messages.error(request, 'This application was revoked.')
            return redirect('developer_app', pk=pk)
        url = (request.POST.get('webhook_url') or '').strip()
        whitelist = (request.POST.get('ip_whitelist') or '').strip()
        try:
            if url:
                validate_webhook_url(url)
            validate_ip_whitelist(whitelist)
        except ValueError as exc:
            messages.error(request, f'Not saved: {exc}')
            return redirect('developer_app', pk=pk)
        chosen = [s for s in SERVICES if request.POST.get(f'svc_{s}')]
        app.webhook_url, app.ip_whitelist, app.allowed_services = url, whitelist, chosen
        app.save(update_fields=['webhook_url', 'ip_whitelist', 'allowed_services'])
        messages.success(request, 'Settings saved.')
    return redirect('developer_app', pk=pk)
