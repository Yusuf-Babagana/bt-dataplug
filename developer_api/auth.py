import logging
from datetime import timedelta

from django.utils import timezone
from rest_framework.authentication import BaseAuthentication

from .errors import ApiError
from .models import ApiApplication, ApiRequestLog
from .security import client_ip, ip_allowed

logger = logging.getLogger('vtu_app')

FAILED_AUTH_LIMIT = 20          # bad-credential attempts per IP...
FAILED_AUTH_WINDOW = timedelta(minutes=15)  # ...within this window


def _bearer(request):
    header = request.headers.get('Authorization', '')
    if header.lower().startswith('bearer '):
        return header[7:].strip()
    return ''


class ApiKeyAuthentication(BaseAuthentication):
    """
    Headers:  X-API-Key: pk_...   Authorization: Bearer sk_...
    Secrets are stored hashed. Also enforces application status, IP whitelist and rate limits.
    """

    def authenticate(self, request):
        django_request = request._request
        ip = client_ip(django_request)
        django_request.api_client_ip = ip
        public_key = request.headers.get('X-API-Key', '').strip()
        secret = _bearer(request)
        django_request.api_public_key = public_key[:64]

        if not public_key or not secret:
            raise ApiError(401, 'missing_credentials', 'Send your public key in X-API-Key and your secret key as "Authorization: Bearer <secret>".')

        since = timezone.now() - FAILED_AUTH_WINDOW
        if ip and ApiRequestLog.objects.filter(ip_address=ip, error_code='invalid_credentials', created_at__gte=since).count() >= FAILED_AUTH_LIMIT:
            logger.warning('API_AUTH_LOCKOUT ip=%s', ip)
            raise ApiError(429, 'too_many_failed_attempts', 'Too many failed authentication attempts. Try again later.', headers={'Retry-After': '900'})

        app = ApiApplication.objects.select_related('developer__user').filter(public_key=public_key).first()
        if not app or not app.check_secret(secret):
            logger.warning('API_AUTH_FAILED ip=%s key=%s', ip, public_key[:20])
            raise ApiError(401, 'invalid_credentials', 'The API key or secret is not valid.')
        django_request.api_app = app

        if app.status == 'revoked':
            raise ApiError(401, 'invalid_credentials', 'The API key or secret is not valid.')
        if app.status == 'pending':
            raise ApiError(403, 'application_pending', 'This application is awaiting approval.')
        if app.status != 'active' or app.developer.status != 'active':
            raise ApiError(403, 'application_suspended', 'This application or developer account is suspended.')
        if not ip_allowed(ip, app.ip_whitelist):
            logger.warning('API_IP_BLOCKED app=%s ip=%s', app.pk, ip)
            raise ApiError(403, 'ip_not_allowed', 'This IP address is not on the application whitelist.')

        now = timezone.now()
        recent = ApiRequestLog.objects.filter(application=app, created_at__gte=now - timedelta(seconds=60)).count()
        if app.rate_limit_per_minute and recent >= app.rate_limit_per_minute:
            raise ApiError(429, 'rate_limited', 'Rate limit exceeded. Slow down and retry.', headers={'Retry-After': '30'})
        if app.daily_request_limit:
            day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            if ApiRequestLog.objects.filter(application=app, created_at__gte=day_start).count() >= app.daily_request_limit:
                raise ApiError(429, 'daily_limit_reached', 'Daily request limit reached.', headers={'Retry-After': '3600'})

        ApiApplication.objects.filter(pk=app.pk).update(last_used_at=now)
        return (app.developer.user, app)
