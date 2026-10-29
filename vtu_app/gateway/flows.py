"""Small helpers shared by the mobile API, the website and the developer API."""
import logging
import time
import uuid

from . import router
from .base import CustomerVerificationError
from .types import PurchaseRequest, Service

logger = logging.getLogger("vtu_app.gateway")

NETWORK_NAMES = {"01": "MTN", "02": "GLO", "03": "9MOBILE", "04": "AIRTEL"}
NETWORK_CODES = {v: k for k, v in NETWORK_NAMES.items()}


def new_reference(prefix):
    """Unique per call (the old time-only reference collided for two buyers in the same second)."""
    return f"{prefix}-{int(time.time())}{uuid.uuid4().hex[:6].upper()}"


def network_name(code_or_name):
    key = str(code_or_name or "").strip()
    return NETWORK_NAMES.get(key, key.upper())


def data_request(plan, phone):
    return PurchaseRequest(service=Service.DATA, reference="", network=network_name(plan.network),
                           plan_id=str(plan.dataplan_id), plan_pk=plan.pk, target=phone)


def airtime_request(network, amount, phone):
    return PurchaseRequest(service=Service.AIRTIME, reference="", network=network_name(network), amount=amount, target=phone)


def cable_request(plan, smartcard, phone):
    return PurchaseRequest(service=Service.CABLE, reference="", network=str(plan.cable_type).upper(),
                           plan_id=str(plan.plan_id), plan_pk=plan.pk, target=smartcard, phone=phone)


def electricity_request(disco, meter_type, meter_no, amount, phone):
    return PurchaseRequest(service=Service.ELECTRICITY, reference="", network=str(disco), amount=amount,
                           target=meter_no, phone=phone, meter_type=str(meter_type))


def verify_customer(service, network, target, meter_type="01"):
    """
    Smartcard / meter lookup through the providers in priority order. Returns the legacy dict
    ``{"customer_name": ...}``: the real name, ``INVALID_SMARTCARDNO`` / ``INVALID_METERNO`` when a provider
    definitively said the number does not exist, or an error string when nobody could confirm.
    """
    probe = PurchaseRequest(service=service, reference="", network=str(network or ""))
    token = "INVALID_SMARTCARDNO" if service == Service.CABLE else "INVALID_METERNO"
    invalid = False
    for adapter in router.select(probe, ignore_support=True).adapters:
        try:
            return {"customer_name": adapter.verify_customer(service, probe.network, target, meter_type)}
        except NotImplementedError:
            continue
        except CustomerVerificationError as e:      # this provider couldn't confirm: ask the next one
            invalid = invalid or e.invalid
        except Exception:
            logger.exception("verify_customer crashed on %s", adapter.name)
    if invalid:
        return {"customer_name": token}
    return {"customer_name": "Error validating number" if service == Service.CABLE else "Error validating meter"}
