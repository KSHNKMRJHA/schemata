"""Alternative / replacement engine: drop-in vs functional vs parametric.

Candidates come from the local database (previously seen parts) plus the
built-in demo catalog. Scoring uses electrical, mechanical (package),
functional (category/family) and supply attributes. Similarity does NOT
mean drop-in — the kind classification gates that claim.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.lifecycle import status_risk
from app.models import LifecycleStatus

NUM_RE = re.compile(r"[-+]?\d*\.?\d+")
RANGE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:-|to|…)\s*(\d+(?:\.\d+)?)")


def _parse_number(text: str) -> float | None:
    if not text:
        return None
    m = NUM_RE.search(text)
    return float(m.group(0)) if m else None


def _parse_limits(text: str) -> tuple[float | None, float | None]:
    if not text:
        return None, None
    m = RANGE_RE.search(text)
    if m:
        return float(m.group(1)), float(m.group(2))
    value = _parse_number(text)
    return (value, value) if value is not None else (None, None)


def _compatible(target: str, candidate: str) -> bool:
    """True if candidate window covers target, or windows overlap."""
    if not target or not candidate:
        return True
    tlo, thi = _parse_limits(target)
    clo, chi = _parse_limits(candidate)
    if tlo is None:
        return bool(clo is None)  # no numeric info on either side -> neutral
    if clo is None:
        return True
    cand_lo = clo if thi is None else min(clo, chi)
    cand_hi = chi if tlo is None else max(clo, chi)
    return tlo >= cand_lo - 1e-9 and (thi is None or thi <= cand_hi + 1e-9)


def _category_group(category: str) -> str:
    cat = (category or "").lower()
    groups = {
        "regulator": "power",
        "switching": "power",
        "transistor": "discrete",
        "mosfet": "discrete",
        "diode": "discrete",
        "microcontroller": "mcu",
        "microprocessor": "mcu",
        "op amp": "analog",
        "adc": "analog",
        "dac": "analog",
        "switch": "analog",
        "transceiver": "comms",
        "bridge": "comms",
        "interface": "comms",
        "logic": "logic",
        "buffer": "logic",
        "register": "logic",
    }
    for key, group in groups.items():
        if key in cat:
            return group
    return (category or "unknown").split()[0] if category else "unknown"


def score_candidate(target: dict, cand: dict) -> AlternativeScore:
    """Score one candidate against a target part. `target` and `cand` are
    flat dicts with the same keys (see AlternativeIn schema)."""
    reasons: list[str] = []

    package_match = _normalize_pkg(target.get("package", "")) == _normalize_pkg(cand.get("package", ""))
    category_match = (target.get("category") or "").lower() == (cand.get("category") or "").lower()
    group_match = _category_group(target.get("category", "")) == _category_group(cand.get("category", ""))
    if cand.get("mpn", "").upper() == target.get("mpn_normalized", "").upper():
        return AlternativeScore(
            mpn=cand["mpn"],
            manufacturer=cand.get("manufacturer", ""),
            category=cand.get("category", ""),
            package=cand.get("package", ""),
            lifecycle_status=cand.get("lifecycle_status", LifecycleStatus.UNKNOWN.value),
            kind="PARAMETRIC",
            score=0.0,
            reasons=["Same part (identity) — excluded."],
        )

    vol = _compatible(target.get("operating_voltage", ""), cand.get("operating_voltage", ""))
    cur = _compatible(target.get("current", ""), cand.get("current", ""))
    frq = _compatible(target.get("frequency", ""), cand.get("frequency", ""))
    tmp = _compatible(target.get("temperature", ""), cand.get("temperature", ""))

    source_score = 0.0
    if group_match:
        source_score += 0.35
    if category_match:
        source_score += 0.15
    if package_match:
        source_score += 0.25
    else:
        source_score += 0.0
    e_score = 0.0
    for ok, weight in ((vol, 0.10), (cur, 0.10), (frq, 0.05), (tmp, 0.05)):
        e_score += weight * (1.0 if ok else 0.35)

    supply = 1.0 - status_risk(cand.get("lifecycle_status", LifecycleStatus.UNKNOWN.value))
    avail = 1.0 if cand.get("available") else 0.6
    supply_score = 0.05 * supply + 0.05 * avail

    total = round(min(1.0, source_score + e_score + supply_score), 3)

    vol_ok = bool(vol) and not (target.get("operating_voltage") and not cand.get("operating_voltage"))
    if package_match and category_match and vol_ok:
        kind = "DROP_IN"
        reasons.append("Pin/package compatible")
        reasons.append("Same functional category")
        if cand.get("operating_voltage"):
            reasons.append("Electrical limits compatible")
        else:
            reasons.append("Candidate electrical data unknown — verify before use")
            total = round(total * 0.8, 3)
    elif category_match or group_match:
        kind = "FUNCTIONAL"
        reasons.append("Same function" if category_match else "Related function group")
        reasons.append("Review PCB / firmware / supply changes")
    else:
        kind = "PARAMETRIC"
        reasons.append("Parametric candidate — engineer must validate all constraints")

    lifecycle = cand.get("lifecycle_status", LifecycleStatus.UNKNOWN.value)
    if lifecycle != LifecycleStatus.ACTIVE.value:
        reasons.append(f"Lifecycle: {lifecycle}")
        total = round(total * (1.0 - 0.15 * status_risk(lifecycle)), 3)

    return AlternativeScore(
        mpn=cand.get("mpn", ""),
        manufacturer=cand.get("manufacturer", ""),
        category=cand.get("category", "") or "",
        package=cand.get("package", "") or "",
        lifecycle_status=lifecycle,
        kind=kind,
        score=total,
        reasons=reasons,
    )


def _normalize_pkg(pkg: str) -> str:
    return re.sub(r"[- ]", "", (pkg or "").upper())


def rank_alternatives(target: dict, candidates: list[dict], limit: int = 12) -> list[AlternativeScore]:
    scored = [score_candidate(target, c) for c in candidates]
    scored = [s for s in scored if s.score > 0]
    scored.sort(key=lambda s: (s.kind == "DROP_IN", s.score), reverse=True)
    return scored[:limit]


@dataclass
class AlternativeScore:
    mpn: str
    manufacturer: str
    category: str
    package: str
    lifecycle_status: str
    kind: str
    score: float
    reasons: list[str]
