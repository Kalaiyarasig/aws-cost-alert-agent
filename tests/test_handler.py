from unittest.mock import MagicMock

import pytest

import handler as h


def ce_response(spike_day_cost):
    days = []
    for d in range(1, 16):
        cost = spike_day_cost if d == 15 else (10.5 if d % 2 else 9.5)
        days.append({"TimePeriod": {"Start": f"2026-11-{d:02d}"},
                     "Groups": [{"Keys": ["EC2"], "Metrics": {"UnblendedCost": {"Amount": str(cost)}}}]})
    return {"ResultsByTime": days}


@pytest.fixture
def aws(monkeypatch):
    ce, sns, explain = MagicMock(), MagicMock(), MagicMock(return_value="EC2 NAT gateway traffic jumped.")
    monkeypatch.setattr(h, "ce", ce)
    monkeypatch.setattr(h, "sns", sns)
    monkeypatch.setattr(h, "explain_spike", explain)
    return ce, sns, explain


def test_spike_is_explained_and_emailed(aws):
    ce, sns, explain = aws
    ce.get_cost_and_usage.return_value = ce_response(60)
    out = h.handler({"date": "2026-11-15"}, None)
    assert out["spike"] is True
    assert "2026-11-15" in explain.call_args.args[0]
    msg = sns.publish.call_args.kwargs
    assert msg["Message"].startswith("EC2 NAT gateway traffic jumped.")
    assert "Raw numbers" in msg["Message"]
    assert len(msg["Subject"]) <= 100


def test_quiet_day_sends_nothing(aws):
    ce, sns, explain = aws
    ce.get_cost_and_usage.return_value = ce_response(10)
    assert h.handler({"date": "2026-11-15"}, None)["spike"] is False
    explain.assert_not_called()
    sns.publish.assert_not_called()


def test_ai_failure_still_alerts(aws):
    ce, sns, explain = aws
    ce.get_cost_and_usage.return_value = ce_response(60)
    explain.side_effect = RuntimeError("throttled")
    h.handler({"date": "2026-11-15"}, None)
    assert "unavailable" in sns.publish.call_args.kwargs["Message"]


def test_force_explains_even_without_spike(aws):
    ce, sns, _ = aws
    ce.get_cost_and_usage.return_value = ce_response(10)
    assert h.handler({"date": "2026-11-15", "force": True}, None)["forced"] is True
    sns.publish.assert_called_once()
