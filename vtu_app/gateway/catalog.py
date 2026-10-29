"""
Provider catalogues and a deliberately CONSERVATIVE matcher that suggests which plans of two providers are the same
product. It only ever *suggests*: suggestions are saved unverified and a human confirms them in the admin.
"""
import re
from dataclasses import dataclass
from decimal import Decimal

_UNITS = {"TB": Decimal(1_000_000), "GB": Decimal(1000), "MB": Decimal(1)}


@dataclass
class CatalogPlan:
    kind: str           # "data" | "cable"
    network: str        # data: "01".."04"   cable: "dstv" | "gotv" | "startimes"
    code: str           # the provider's own plan code
    cost: Decimal       # what the provider charges us
    name: str           # display text
    text: str = ""      # everything descriptive (name + validity + type), used for matching


def parse_size_mb(text):
    """'1.5GB' / '1000.0 MB' -> megabytes; None when no explicit unit (e.g. '500.0 WEEKLY PLAN') - never guessed."""
    m = re.search(r"(\d+(?:\.\d+)?)\s*(TB|GB|MB)\b", str(text or ""), re.I)
    return (Decimal(m.group(1)) * _UNITS[m.group(2).upper()]).normalize() if m else None


def parse_days(text):
    """Validity in days from '30 days', '7Day', '2 WEEKS', 'WEEKLY', 'MONTHLY' ...; None when unclear."""
    t = str(text or "").upper()
    m = re.search(r"(\d+)\s*[- ]?\s*DAYS?\b", t)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d+)\s*[- ]?\s*WEEKS?\b", t)
    if m:
        return int(m.group(1)) * 7
    m = re.search(r"(\d+)\s*[- ]?\s*MONTHS?\b", t)
    if m:
        return int(m.group(1)) * 30
    for word, days in (("DAILY", 1), ("WEEKLY", 7), ("MONTHLY", 30), ("YEARLY", 365)):
        if word in t:
            return days
    return None


def parse_kind(text):
    """Coarse product family, so SME is never matched to GIFTING; None when the text does not say."""
    t = str(text or "").upper()
    if "CORPORATE" in t or re.search(r"\bCG\b", t):
        return "CORPORATE"
    if "SME" in t:
        return "SME"
    if "GIFTING" in t:
        return "GIFTING"
    if "COUPON" in t or "AWOOF" in t:
        return "COUPON"
    return None


def _norm_name(text):
    return re.sub(r"[^a-z0-9+]", "", str(text or "").lower())


def match_data(plan_text, network, candidates):
    """
    Candidates (``CatalogPlan``) that look like the same product as a plan described by ``plan_text``: same network, same
    size in MB, same validity in days, and a compatible family when both sides state one. Unparseable = no match.
    """
    size, days, kind = parse_size_mb(plan_text), parse_days(plan_text), parse_kind(plan_text)
    if size is None or days is None:
        return []
    out = []
    for c in candidates:
        if c.kind != "data" or c.network != network:
            continue
        if parse_size_mb(c.text) != size or parse_days(c.text) != days:
            continue
        ckind = parse_kind(c.text)
        if kind and ckind and kind != ckind:
            continue
        out.append(c)
    return sorted(out, key=lambda c: c.cost)


def match_cable(plan_name, cable_type, candidates, cost_hint=None):
    """
    Same decoder brand and same package name (ignoring punctuation/case). Providers' costs for one package differ by
    a little (that is the point), so cost is only a plausibility guard: within 25% of ``cost_hint`` when given.
    """
    want = _norm_name(plan_name)
    out = []
    for c in candidates:
        if c.kind != "cable" or c.network != cable_type or _norm_name(c.name) != want:
            continue
        if cost_hint and not (Decimal("0.75") * cost_hint <= c.cost <= Decimal("1.25") * cost_hint):
            continue
        out.append(c)
    return sorted(out, key=lambda c: c.cost)
