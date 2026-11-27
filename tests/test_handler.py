from datetime import datetime
from unittest.mock import MagicMock

import pytest

import handler as h


class DataUnavailable(Exception):
    pass


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
    ce.exceptions.DataUnavailableException = DataUnavailable
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


def test_steps_back_when_newest_day_not_ready(aws, monkeypatch):
    ce, sns, _ = aws

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 17, 14, tzinfo=tz)

    monkeypatch.setattr(h, "datetime", FrozenDatetime)
    ce.get_cost_and_usage.side_effect = [DataUnavailable(), ce_response(60)]
    out = h.handler({}, None)  # yesterday = Nov 16 is not ready, so it checks Nov 15
    assert out["spike"] is True
    ends = [c.kwargs["TimePeriod"]["End"] for c in ce.get_cost_and_usage.call_args_list]
    assert ends == ["2026-11-17", "2026-11-16"]
    assert "2026-11-15" in sns.publish.call_args.kwargs["Subject"]


def test_fixed_date_does_not_step_back(aws):
    ce, _, _ = aws
    ce.get_cost_and_usage.side_effect = DataUnavailable()
    with pytest.raises(DataUnavailable):
        h.handler({"date": "2026-11-15"}, None)
