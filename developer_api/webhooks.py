"""Signed webhook delivery with retries. Delivery never blocks or affects the purchase flow."""
import hashlib
import hmac
import json
import logging
import threading
import time
import uuid
from datetime import timedelta

import requests
from django.conf import settings
from django.db import close_old_connections, transaction
from django.utils import timezone

from .models import WebhookDelivery
from .security import validate_webhook_url

logger = logging.getLogger('vtu_app')

EVENT_TYPES = {
    'successful': 'transaction.successful',
    'failed': 'transaction.failed',
    'pending': 'transaction.pending',
    'reversed': 'transaction.reversed',
}
RETRY_DELAYS = [60, 300, 1800, 7200, 21600, 86400]  # seconds; 7 attempts in total
SIGNATURE_HEADER = 'X-BTDataPlug-Signature'


def sign(secret, timestamp, body_bytes):
    msg = f'{timestamp}.'.encode() + body_bytes
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


def queue_event(api_tx):
    """Record an event for the transaction's current status and try to deliver it in the background."""
    app = api_tx.application
    event_type = EVENT_TYPES.get(api_tx.status)
    if not event_type or not app.webhook_url:
        return None
    from .services import serialize_transaction
    event_id = f'evt_{uuid.uuid4().hex}'
    delivery = WebhookDelivery.objects.create(
        application=app, transaction=api_tx, event_id=event_id, event_type=event_type,
        payload={
            'id': event_id,
            'type': event_type,
            'created': int(time.time()),
            'environment': app.environment,
            'data': serialize_transaction(api_tx),
        },
        next_attempt_at=timezone.now(),
    )
    if getattr(settings, 'DEVELOPER_API_ASYNC_WEBHOOKS', True):
        transaction.on_commit(lambda: threading.Thread(target=deliver_by_id, args=(delivery.pk,), daemon=True).start())
    return delivery


def deliver_by_id(delivery_id):
    close_old_connections()
    try:
        delivery = WebhookDelivery.objects.select_related('application').get(pk=delivery_id)
        deliver(delivery)
    except Exception:
        logger.exception('WEBHOOK_THREAD_ERROR delivery=%s', delivery_id)
    finally:
        close_old_connections()


def deliver(delivery):
    """One delivery attempt. Updates status/backoff. Safe to call repeatedly; delivered events are never resent."""
    if delivery.status in ('delivered', 'exhausted'):
        return delivery
    app = delivery.application
    body = json.dumps(delivery.payload, separators=(',', ':'), sort_keys=True).encode()
    ts = int(time.time())
    headers = {
        'Content-Type': 'application/json',
        'User-Agent': 'BTDataPlug-Webhooks/1.0',
        'X-BTDataPlug-Event-Id': delivery.event_id,
        'X-BTDataPlug-Event-Type': delivery.event_type,
        SIGNATURE_HEADER: f't={ts},v1={sign(app.webhook_signing_secret, ts, body)}',
    }
    delivery.attempts += 1
    code, error = None, ''
    try:
        validate_webhook_url(app.webhook_url)
        resp = requests.post(app.webhook_url, data=body, headers=headers, timeout=8, allow_redirects=False)
        code = resp.status_code
        if not 200 <= code < 300:
            error = f'HTTP {code}'
    except Exception as exc:
        error = type(exc).__name__ if not isinstance(exc, ValueError) else str(exc)[:150]

    delivery.last_status_code = code
    delivery.last_error = error[:200]
    if not error:
        delivery.status = 'delivered'
        delivery.delivered_at = timezone.now()
        delivery.next_attempt_at = None
    elif delivery.attempts > len(RETRY_DELAYS):
        delivery.status = 'exhausted'
        delivery.next_attempt_at = None
    else:
        delivery.status = 'failed'
        delivery.next_attempt_at = timezone.now() + timedelta(seconds=RETRY_DELAYS[delivery.attempts - 1])
    delivery.save()
    return delivery


def due_deliveries():
    return WebhookDelivery.objects.filter(status__in=['pending', 'failed'], next_attempt_at__lte=timezone.now())
