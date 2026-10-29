"""
Which provider can sell a given customer-facing plan, under which code, at what cost.

A plan is always sold by the provider that owns it (``plan.source``) at ``plan.cost_price``. Other providers can sell
the SAME product only through a VERIFIED, active ``PlanOffer``. Nothing else is ever routed, so a plan id can never
reach a provider that gives that id a different meaning.
"""
from dataclasses import dataclass
from decimal import Decimal

from vtu_app.models import CablePlan, DataPlan, PlanOffer, ProviderRate

from .types import Service


@dataclass(frozen=True)
class Offer:
    code: str                   # the provider's own plan code
    cost: Decimal | None        # what the provider charges us (None = unknown)


def _plan(request):
    if not request.plan_pk:
        return None
    model = DataPlan if request.service == Service.DATA else CablePlan
    return model.objects.filter(pk=request.plan_pk).first()


def offer(provider, request) -> Offer | None:
    """The provider's way of selling this request's plan, or None if it may not sell it."""
    if request.service not in (Service.DATA, Service.CABLE):
        return None
    plan = _plan(request)
    if plan is None:
        return None
    if plan.source == provider:
        return Offer(plan.dataplan_id if request.service == Service.DATA else plan.plan_id, plan.cost_price)
    link = {"data_plan": plan} if request.service == Service.DATA else {"cable_plan": plan}
    po = PlanOffer.objects.filter(provider=provider, verified=True, is_active=True, **link).first()
    return Offer(po.provider_code, po.cost_price) if po else None


def airtime_percent(provider, network):
    """Percent of face value the provider charges us for this network's airtime, or None if unknown."""
    row = ProviderRate.objects.filter(provider=provider, key=f"airtime:{str(network).upper()}").first()
    return row.value if row else None


def quote(provider, request) -> Decimal | None:
    """Our estimated cost of fulfilling ``request`` through ``provider`` (None = unknown)."""
    if request.service in (Service.DATA, Service.CABLE):
        o = offer(provider, request)
        return o.cost if o else None
    if request.service == Service.AIRTIME and request.amount:
        pct = airtime_percent(provider, request.network)
        return (Decimal(str(request.amount)) * pct / Decimal("100")).quantize(Decimal("0.01")) if pct else None
    return None
