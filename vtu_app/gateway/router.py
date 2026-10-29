"""
Provider selection.

``select`` filters providers to those that are eligible right now, then asks the
active ``RoutingPolicy`` to order them. Swap the policy with
``VTU_ROUTING_POLICY=dotted.path.ToYourPolicy`` - e.g. lowest cost or highest
success rate - without touching the engine.
"""
from dataclasses import dataclass, field

from django.utils.module_loading import import_string

from . import config, health, registry
from .types import PurchaseRequest


class RoutingPolicy:
    def order(self, candidates, request: PurchaseRequest):
        """candidates: list of (adapter, ProviderState). Return adapters, best first."""
        raise NotImplementedError


def effective_priority(adapter, state):
    """Admin override > VTU_<NAME>_PRIORITY in .env > adapter default. Lower wins."""
    if state.priority_override is not None:
        return state.priority_override
    if adapter.config.priority is not None:
        return adapter.config.priority
    return adapter.default_priority


class PriorityPolicy(RoutingPolicy):
    def order(self, candidates, request):
        return [a for a, s in sorted(candidates, key=lambda c: (effective_priority(*c), c[0].name))]


@dataclass
class Selection:
    adapters: list = field(default_factory=list)    # eligible, in attempt order
    supporting: int = 0                             # providers that could serve the request at all


def get_policy() -> RoutingPolicy:
    return import_string(config.routing_policy_path())()


def select(request: PurchaseRequest, ignore_support=False) -> Selection:
    """Eligible providers in attempt order. ``ignore_support`` skips the per-request capability check
    (used by customer lookups, which have no plan to match)."""
    candidates, supporting = [], 0
    for adapter in registry.all_adapters():
        if not adapter.config.enabled or not adapter.config.is_valid:
            continue
        if not ignore_support and not adapter.supports(request):
            continue
        supporting += 1
        state = health.get_state(adapter.name)
        if not state.admin_enabled or health.is_suspended(state):
            continue
        candidates.append((adapter, state))
    return Selection(get_policy().order(candidates, request), supporting)
