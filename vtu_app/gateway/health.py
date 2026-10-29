"""
Provider health + circuit breaker, persisted in ``ProviderState`` so it is
shared across workers.

* N consecutive health-affecting failures (``VTU_FAILURE_THRESHOLD``) suspend a
  provider for ``VTU_SUSPEND_MINUTES``.
* When the suspension lapses the provider is tried again (half-open). One more
  failure re-suspends it immediately; one success clears the counter.
"""
import logging
from datetime import timedelta

from django.db.models import F
from django.utils import timezone

from vtu_app.models import ProviderState

from . import config
from .types import ErrorCategory

logger = logging.getLogger("vtu_app.gateway")


def get_state(name) -> ProviderState:
    return ProviderState.objects.get_or_create(name=name)[0]


def is_suspended(state: ProviderState) -> bool:
    return bool(state.suspended_until and state.suspended_until > timezone.now())


def record_success(name):
    get_state(name)
    ProviderState.objects.filter(name=name).update(
        consecutive_failures=0, suspended_until=None, last_success_at=timezone.now(),
    )


def record_failure(name, category, message=""):
    """Count a failure toward the circuit breaker if it says something about provider health."""
    if category not in ErrorCategory.HEALTH_AFFECTING:
        return
    get_state(name)
    now = timezone.now()
    ProviderState.objects.filter(name=name).update(
        consecutive_failures=F("consecutive_failures") + 1,
        last_failure_at=now, last_error=f"{category}: {message}"[:255],
    )
    state = ProviderState.objects.get(name=name)
    if state.consecutive_failures >= config.failure_threshold() and not is_suspended(state):
        until = now + timedelta(minutes=config.suspend_minutes())
        ProviderState.objects.filter(name=name).update(suspended_until=until)
        logger.warning("VTU provider %s suspended until %s after %s consecutive failures",
                       name, until.isoformat(), state.consecutive_failures)


def reset(name):
    ProviderState.objects.filter(name=name).update(consecutive_failures=0, suspended_until=None)
