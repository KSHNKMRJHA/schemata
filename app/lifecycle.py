"""Lifecycle state machine + status risk weights."""

from __future__ import annotations

from app.models import LifecycleStatus

_TRANSITIONS: dict[LifecycleStatus, tuple[LifecycleStatus, ...]] = {
    LifecycleStatus.ACTIVE: (LifecycleStatus.NRND, LifecycleStatus.EOL_ANNOUNCED, LifecycleStatus.UNKNOWN),
    LifecycleStatus.NRND: (LifecycleStatus.EOL_ANNOUNCED, LifecycleStatus.ACTIVE, LifecycleStatus.UNKNOWN),
    LifecycleStatus.EOL_ANNOUNCED: (
        LifecycleStatus.LAST_TIME_BUY,
        LifecycleStatus.OBSOLETE,
        LifecycleStatus.NRND,
        LifecycleStatus.ACTIVE,
        LifecycleStatus.UNKNOWN,
    ),
    LifecycleStatus.LAST_TIME_BUY: (LifecycleStatus.OBSOLETE, LifecycleStatus.EOL_ANNOUNCED, LifecycleStatus.UNKNOWN),
    LifecycleStatus.OBSOLETE: (LifecycleStatus.ACTIVE, LifecycleStatus.UNKNOWN),
    LifecycleStatus.UNKNOWN: tuple(LifecycleStatus),
}

_STATUS_RISK: dict[LifecycleStatus, float] = {
    LifecycleStatus.ACTIVE: 0.0,
    LifecycleStatus.NRND: 0.30,
    LifecycleStatus.EOL_ANNOUNCED: 0.60,
    LifecycleStatus.LAST_TIME_BUY: 0.80,
    LifecycleStatus.OBSOLETE: 1.0,
    LifecycleStatus.UNKNOWN: 0.50,
}

_COLORS = {
    LifecycleStatus.ACTIVE.value: "green",
    LifecycleStatus.NRND.value: "yellow",
    LifecycleStatus.EOL_ANNOUNCED.value: "yellow",
    LifecycleStatus.LAST_TIME_BUY.value: "red",
    LifecycleStatus.OBSOLETE.value: "red",
    LifecycleStatus.UNKNOWN.value: "blue",
}

_DESC = {
    LifecycleStatus.ACTIVE.value: "In production and supported. Normal availability expected.",
    LifecycleStatus.NRND.value: "Not Recommended for New Design. Existing designs should plan a replacement.",
    LifecycleStatus.EOL_ANNOUNCED.value: "End-of-life announced. Final order windows may be open.",
    LifecycleStatus.LAST_TIME_BUY.value: "Last time buy window in progress. Stock will not be replenished.",
    LifecycleStatus.OBSOLETE.value: "Discontinued. Sourcing only via brokers/excess stock.",
    LifecycleStatus.UNKNOWN.value: "No reliable public source found. Treat as unverified.",
}


def allowed_transitions(status: str) -> list[str]:
    try:
        st = LifecycleStatus(status)
    except ValueError:
        st = LifecycleStatus.UNKNOWN
    return [t.value for t in _TRANSITIONS[st]]


def status_color(status: str) -> str:
    try:
        return _COLORS[LifecycleStatus(status)]
    except ValueError:
        return "blue"


def status_description(status: str) -> str:
    try:
        return _DESC[LifecycleStatus(status)]
    except ValueError:
        return _DESC[LifecycleStatus.UNKNOWN.value]


def status_risk(status: str) -> float:
    """0 = no lifecycle risk, 1 = maximum lifecycle risk."""
    try:
        return _STATUS_RISK[LifecycleStatus(status)]
    except ValueError:
        return _STATUS_RISK[LifecycleStatus.UNKNOWN]


def status_is_active(status: str) -> bool:
    return status == LifecycleStatus.ACTIVE.value


def apply_event(current: str, event_type: str) -> str:
    """Derive a state after an event without a full transition table lookup."""
    mapping = {
        "PCN": None,  # PCN/PDN are notices, do not necessarily change status
        "PDN": None,
    }
    if event_type in mapping:
        return current
    forward = {
        LifecycleStatus.ACTIVE.value: LifecycleStatus.NRND.value,
        LifecycleStatus.NRND.value: LifecycleStatus.EOL_ANNOUNCED.value,
        LifecycleStatus.EOL_ANNOUNCED.value: LifecycleStatus.LAST_TIME_BUY.value,
        LifecycleStatus.LAST_TIME_BUY.value: LifecycleStatus.OBSOLETE.value,
    }
    return forward.get(current, current)
