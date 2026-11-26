from datetime import date
from types import SimpleNamespace as NS
from unittest.mock import MagicMock

import pytest

import explainer


def text(t):
    return NS(type="text", text=t)


def tool_use(i, name="get_daily_trend", **inp):
    return NS(type="tool_use", id=f"tu_{i}", name=name,
              input=inp or {"start_date": "2026-11-01", "end_date": "2026-11-15"})


def reply(stop, *content):
    return NS(stop_reason=stop, content=list(content), usage={})


@pytest.fixture
def llm(monkeypatch):
    fake = MagicMock()
    monkeypatch.setattr(explainer, "_client", fake)
    return fake.messages.create


@pytest.fixture
def tool_calls(monkeypatch):
    calls = []

    def fake_run(name, args):
        calls.append((name, args))
        return '{"daily_usd": {}}', False

    monkeypatch.setattr(explainer, "run_tool", fake_run)
    return calls


def test_runs_tools_then_returns_text(llm, tool_calls):
    llm.side_effect = [
        reply("tool_use", text("Checking."), tool_use(1), tool_use(2, "get_cost_by_service")),
        reply("end_turn", text("What happened: EC2 up $40.")),
    ]
    out = explainer.explain_spike("summary", today=date(2026, 11, 16))
    assert out == "What happened: EC2 up $40."
    assert [c[0] for c in tool_calls] == ["get_daily_trend", "get_cost_by_service"]

    second = llm.call_args_list[1].kwargs
    results = second["messages"][-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["tu_1", "tu_2"]  # one user message, all results
    assert second["messages"][1]["role"] == "assistant"
    assert "2026-11-16" in second["messages"][0]["content"]
    assert second["tools"] == explainer.TOOL_SCHEMAS


def test_tool_budget_is_enforced(llm, tool_calls, monkeypatch):
    monkeypatch.setattr(explainer, "MAX_TOOL_CALLS", 2)
    llm.side_effect = [
        reply("tool_use", tool_use(1), tool_use(2), tool_use(3)),
        reply("end_turn", text("Done.")),
    ]
    assert explainer.explain_spike("s") == "Done."
    assert len(tool_calls) == 2
    last = llm.call_args_list[1].kwargs["messages"][-1]["content"][-1]
    assert last["is_error"] and "budget" in last["content"]


def test_refusal_raises(llm, tool_calls):
    llm.return_value = reply("refusal")
    with pytest.raises(RuntimeError):
        explainer.explain_spike("s")


def test_turn_limit(llm, tool_calls):
    llm.return_value = reply("tool_use", tool_use(1))
    with pytest.raises(RuntimeError, match="turn limit"):
        explainer.explain_spike("s")
