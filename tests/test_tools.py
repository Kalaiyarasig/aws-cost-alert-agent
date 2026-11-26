import json
from unittest.mock import MagicMock

import pytest

import tools


@pytest.fixture
def ce(monkeypatch):
    mock = MagicMock()
    monkeypatch.setattr(tools, "ce", mock)
    return mock


def test_schemas_match_functions():
    assert {t["name"] for t in tools.TOOL_SCHEMAS} == set(tools.FUNCTIONS)
    for t in tools.TOOL_SCHEMAS:
        assert t["input_schema"]["additionalProperties"] is False


def test_service_breakdown_ranks_and_converts_end_date(ce):
    ce.get_cost_and_usage.return_value = {"ResultsByTime": [{"Groups": [
        {"Keys": ["USE1-BoxUsage:m5.large"], "Metrics": {"UnblendedCost": {"Amount": "3"}}},
        {"Keys": ["USE1-NatGateway-Bytes"], "Metrics": {"UnblendedCost": {"Amount": "30"}}},
    ]}]}
    body, is_error = tools.run_tool("get_service_breakdown", {
        "service": "EC2 - Other", "start_date": "2026-11-01", "end_date": "2026-11-15"})
    assert not is_error
    assert list(json.loads(body)["top_items_usd"]) == ["USE1-NatGateway-Bytes", "USE1-BoxUsage:m5.large"]
    kwargs = ce.get_cost_and_usage.call_args.kwargs
    assert kwargs["TimePeriod"] == {"Start": "2026-11-01", "End": "2026-11-16"}
    assert kwargs["GroupBy"] == [{"Type": "DIMENSION", "Key": "USAGE_TYPE"}]


def test_daily_trend_follows_pagination(ce):
    ce.get_cost_and_usage.side_effect = [
        {"ResultsByTime": [{"TimePeriod": {"Start": "2026-11-01"}, "Total": {"UnblendedCost": {"Amount": "1.234"}}}],
         "NextPageToken": "t"},
        {"ResultsByTime": [{"TimePeriod": {"Start": "2026-11-02"}, "Total": {"UnblendedCost": {"Amount": "9"}}}]},
    ]
    body, _ = tools.run_tool("get_daily_trend", {"start_date": "2026-11-01", "end_date": "2026-11-02"})
    assert json.loads(body) == {"service": "ALL", "daily_usd": {"2026-11-01": 1.23, "2026-11-02": 9.0}}


@pytest.mark.parametrize("args", [
    {"start_date": "2026-11-10", "end_date": "2026-11-01"},
    {"start_date": "2026-01-01", "end_date": "2026-11-01"},
    {"start_date": "yesterday", "end_date": "2026-11-01"},
    {"start_date": "2026-11-01"},
])
def test_bad_input_is_an_error_without_calling_aws(ce, args):
    _, is_error = tools.run_tool("get_cost_by_service", args)
    assert is_error
    ce.get_cost_and_usage.assert_not_called()


def test_unknown_tool_and_bad_dimension(ce):
    body, is_error = tools.run_tool("delete_everything", {})
    assert is_error and "Unknown" in body
    body, is_error = tools.run_tool("get_service_breakdown", {
        "service": "S3", "start_date": "2026-11-01", "end_date": "2026-11-02", "dimension": "TAG"})
    assert is_error and "dimension" in body


def test_aws_failure_is_reported_not_raised(ce):
    ce.get_cost_and_usage.side_effect = RuntimeError("boom")
    body, is_error = tools.run_tool("get_cost_by_service", {"start_date": "2026-11-01", "end_date": "2026-11-02"})
    assert is_error and "RuntimeError" in body
