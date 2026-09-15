from app.lifecycle import allowed_transitions, apply_event, status_risk


def test_transition_graph():
    assert "LAST_TIME_BUY" in allowed_transitions("EOL_ANNOUNCED")
    assert "OBSOLETE" in allowed_transitions("EOL_ANNOUNCED")


def test_apply_event_moves_forward():
    assert apply_event("ACTIVE", "EOL_ANNOUNCED") == "NRND"  # NRND is the first forward step
    assert apply_event("OBSOLETE", "LAST_SHIPMENT") == "OBSOLETE"  # terminal


def test_status_risk_magnitude():
    assert status_risk("ACTIVE") == 0.0
    assert status_risk("OBSOLETE") == 1.0
    assert status_risk("NRND") < status_risk("LAST_TIME_BUY")


def test_unknown_is_risky_not_total():
    assert status_risk("UNKNOWN") == 0.5
