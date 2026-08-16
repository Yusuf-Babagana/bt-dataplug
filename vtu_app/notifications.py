"""
Shared helper for creating persistent Notification rows.

Called from web views (views.py), API views (api_views.py), and the Monnify
webhook (webhooks.py) so every channel that triggers a wallet/purchase/account
event leaves the same trail in the /api/v1/notifications/ history feed.
"""
import logging

from .models import Notification

logger = logging.getLogger(__name__)


def notify(user, title, message):
    """
    Create a Notification row for `user`.

    Best-effort: a notification failure should never take down the
    purchase/webhook flow that triggered it, so errors are logged and
    swallowed rather than raised.
    """
    try:
        Notification.objects.create(user=user, title=title, message=message)
    except Exception as e:
        logger.error(f"NOTIFICATION_CREATE_FAILED: {str(e)}")
