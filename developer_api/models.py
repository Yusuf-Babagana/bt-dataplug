import hashlib
import hmac
import secrets
import uuid

from django.conf import settings
from django.contrib.auth.models import User
from django.db import models

SERVICES = ['airtime', 'data', 'electricity', 'cable']


def default_services():
    return list(SERVICES)


class DeveloperAccount(models.Model):
    """A BT DataPlug user who has opted in to the developer API. Their wallet funds live purchases."""
    STATUS = [('active', 'Active'), ('suspended', 'Suspended')]
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='developer_account')
    company_name = models.CharField(max_length=120)
    website = models.URLField(blank=True)
    status = models.CharField(max_length=10, choices=STATUS, default='active')
    notes = models.TextField(blank=True, help_text='Internal notes (not shown to the developer).')
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'{self.company_name} ({self.user.username})'


class ApiApplication(models.Model):
    """One integration. Holds a public key, a hashed secret, permissions and security settings."""
    ENVIRONMENTS = [('test', 'Test (sandbox, no real money)'), ('live', 'Live (real money)')]
    STATUS = [
        ('pending', 'Pending approval'),
        ('active', 'Active'),
        ('suspended', 'Suspended'),
        ('revoked', 'Revoked'),
    ]

    developer = models.ForeignKey(DeveloperAccount, on_delete=models.CASCADE, related_name='applications')
    name = models.CharField(max_length=80)
    environment = models.CharField(max_length=4, choices=ENVIRONMENTS, default='test')
    status = models.CharField(max_length=10, choices=STATUS, default='pending')

    public_key = models.CharField(max_length=64, unique=True, editable=False)
    secret_hash = models.CharField(max_length=64, blank=True, editable=False)  # SHA-256 of the secret key
    secret_hint = models.CharField(max_length=8, blank=True, editable=False)    # last 4 chars, for display
    secret_rotated_at = models.DateTimeField(null=True, blank=True, editable=False)

    allowed_services = models.JSONField(default=default_services, blank=True)
    ip_whitelist = models.TextField(blank=True, help_text='Optional. One IP or CIDR range per line. Empty = any IP.')
    webhook_url = models.URLField(blank=True)
    webhook_secret_version = models.PositiveIntegerField(default=1)

    rate_limit_per_minute = models.PositiveIntegerField(default=60)
    daily_request_limit = models.PositiveIntegerField(default=10000, help_text='0 = unlimited')

    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.name} [{self.environment}]'

    @property
    def is_live(self):
        return self.environment == 'live'

    # --- credentials -------------------------------------------------------------------------
    @staticmethod
    def hash_secret(secret):
        return hashlib.sha256(secret.encode('utf-8')).hexdigest()

    def issue_credentials(self, *, new_public_key=False):
        """Generate a new secret (and a public key when creating). Returns the plaintext secret ONCE; only its hash is stored."""
        from django.utils import timezone
        secret = f'sk_{self.environment}_{secrets.token_urlsafe(32)}'
        if new_public_key or not self.public_key:
            self.public_key = f'pk_{self.environment}_{secrets.token_hex(12)}'
        self.secret_hash = self.hash_secret(secret)
        self.secret_hint = secret[-4:]
        self.secret_rotated_at = timezone.now()
        return secret

    def check_secret(self, secret):
        if not self.secret_hash or not secret:
            return False
        return hmac.compare_digest(self.secret_hash, self.hash_secret(secret))

    def revoke(self):
        self.secret_hash = ''
        self.secret_hint = ''
        self.status = 'revoked'

    # --- webhooks ----------------------------------------------------------------------------
    @property
    def webhook_signing_secret(self):
        """Derived from the server SECRET_KEY, so it is never stored. Bumping webhook_secret_version rotates it."""
        msg = f'whsec:{self.pk}:{self.webhook_secret_version}'.encode()
        return 'whsec_' + hmac.new(settings.SECRET_KEY.encode(), msg, hashlib.sha256).hexdigest()

    def allows(self, service):
        return service in (self.allowed_services or [])


class ApiTransaction(models.Model):
    """A purchase made through the developer API. Links to the core Transaction (live) for money tracking."""
    STATUS = [
        ('processing', 'Processing'),   # internal: request accepted, debit/provider call in flight
        ('pending', 'Pending'),         # sent to provider, outcome not confirmed yet
        ('successful', 'Successful'),
        ('failed', 'Failed'),           # provider rejected; wallet refunded
        ('reversed', 'Reversed'),       # was successful, later reversed by staff; wallet refunded
    ]

    application = models.ForeignKey(ApiApplication, on_delete=models.PROTECT, related_name='transactions')
    reference = models.CharField(max_length=40, unique=True)        # our reference, e.g. BTD-xxxx
    client_reference = models.CharField(max_length=64, blank=True)  # developer's own order id (optional)
    idempotency_key = models.CharField(max_length=64)
    request_fingerprint = models.CharField(max_length=64)           # hash of the request body, to detect key reuse

    service = models.CharField(max_length=12)
    description = models.CharField(max_length=160)
    recipient = models.CharField(max_length=40)
    amount = models.DecimalField(max_digits=12, decimal_places=2)   # what the developer's wallet is charged
    status = models.CharField(max_length=12, choices=STATUS, default='processing')
    is_test = models.BooleanField(default=False)
    failure_reason = models.CharField(max_length=160, blank=True)
    result = models.JSONField(default=dict, blank=True)            # safe extras shown to the developer (token, order id)

    provider_request_id = models.CharField(max_length=40, blank=True)
    provider_status = models.CharField(max_length=60, blank=True)   # last raw provider status (internal only)
    core_reference = models.CharField(max_length=50, blank=True)    # vtu_app.Transaction.reference
    refunded = models.BooleanField(default=False)
    reconcile_attempts = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(fields=['application', 'idempotency_key'], name='uniq_app_idempotency_key'),
        ]
        indexes = [models.Index(fields=['status', 'updated_at'])]

    def __str__(self):
        return f'{self.reference} {self.service} {self.status}'

    @staticmethod
    def new_reference():
        return f'BTD-{uuid.uuid4().hex[:20].upper()}'


class ApiRequestLog(models.Model):
    """Every authenticated or rejected API call: powers rate limits, usage stats and abuse monitoring."""
    application = models.ForeignKey(ApiApplication, null=True, blank=True, on_delete=models.CASCADE, related_name='request_logs')
    public_key_tried = models.CharField(max_length=64, blank=True)
    method = models.CharField(max_length=8)
    path = models.CharField(max_length=200)
    status_code = models.PositiveSmallIntegerField()
    error_code = models.CharField(max_length=40, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    duration_ms = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['application', 'created_at'])]


class WebhookDelivery(models.Model):
    STATUS = [('pending', 'Pending'), ('delivered', 'Delivered'), ('failed', 'Failed, will retry'), ('exhausted', 'Gave up')]

    application = models.ForeignKey(ApiApplication, on_delete=models.CASCADE, related_name='webhook_deliveries')
    transaction = models.ForeignKey(ApiTransaction, on_delete=models.CASCADE, related_name='webhook_deliveries')
    event_id = models.CharField(max_length=40, unique=True)
    event_type = models.CharField(max_length=40)
    payload = models.JSONField()
    status = models.CharField(max_length=10, choices=STATUS, default='pending')
    attempts = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_status_code = models.PositiveSmallIntegerField(null=True, blank=True)
    last_error = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    delivered_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
