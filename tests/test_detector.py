import pytest

import detector as det



def history(days=14, ec2=10.0, s3=2.0, jitter=0.5):
    return {f"2026-11-{d:02d}": {"EC2": ec2 + (jitter if d % 2 else -jitter), "S3": s3} for d in range(1, days + 1)}


def test_flags_real_spike_and_ranks_mover():
    daily = history()
    daily["2026-11-15"] = {"EC2": 45.0, "S3": 2.0, "SageMaker": 8.0}
    r = det.detect_spike(daily, "2026-11-15")
    assert r.is_spike
    assert r.top_movers[0].service == "EC2"
    assert {m.service for m in r.top_movers} == {"EC2", "SageMaker"}
    assert "EC2" in r.summary()


def test_small_absolute_increase_is_ignored():
    daily = {f"2026-11-{d:02d}": {"Lambda": 0.10} for d in range(1, 15)}
    daily["2026-11-15"] = {"Lambda": 1.00}  # +900% but under $5
    r = det.detect_spike(daily, "2026-11-15")
    assert not r.is_spike
    assert "abs" in r.reason


def test_normal_day_is_not_a_spike():
    daily = history()
    daily["2026-11-15"] = {"EC2": 10.4, "S3": 2.0}
    assert not det.detect_spike(daily, "2026-11-15").is_spike


def test_noisy_history_needs_larger_z():
    daily = {f"2026-11-{d:02d}": {"EC2": 10.0 if d % 2 else 40.0} for d in range(1, 15)}
    daily["2026-11-15"] = {"EC2": 40.0}  # +60% over mean but normal for this account
    r = det.detect_spike(daily, "2026-11-15")
    assert not r.is_spike
    assert "z" in r.reason


def test_not_enough_history():
    daily = {"2026-11-01": {"EC2": 1}, "2026-11-02": {"EC2": 100}}
    assert not det.detect_spike(daily, "2026-11-02").is_spike


def test_missing_date_raises():
    with pytest.raises(ValueError):
        det.detect_spike(history(), "2026-12-01")


def test_parse_cost_explorer():
    raw = [{
        "TimePeriod": {"Start": "2026-11-01", "End": "2026-11-02"},
        "Groups": [
            {"Keys": ["EC2"], "Metrics": {"UnblendedCost": {"Amount": "1.5", "Unit": "USD"}}},
            {"Keys": ["S3"], "Metrics": {"UnblendedCost": {"Amount": "0.25", "Unit": "USD"}}},
        ],
    }]
    assert det.parse_cost_explorer(raw) == {"2026-11-01": {"EC2": 1.5, "S3": 0.25}}
