"""Read-only Cost Explorer tools that Claude calls while it investigates a spike.

Each Cost Explorer request costs $0.01, so date ranges are capped and results trimmed.
"""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta

import boto3

log = logging.getLogger(__name__)

ce = boto3.client("ce", region_name="us-east-1")

MAX_RANGE_DAYS = 90
TOP_N = 10
ALLOWED_DIMENSIONS = {"USAGE_TYPE", "REGION", "OPERATION", "LINKED_ACCOUNT", "INSTANCE_TYPE", "PURCHASE_TYPE"}
EXCLUDE_CREDITS = {"Not": {"Dimensions": {"Key": "RECORD_TYPE", "Values": ["Credit", "Refund"]}}}


class ToolError(ValueError):
    pass


def _dates(start: str, end: str) -> tuple[str, str]:
    """Inclusive [start, end] -> Cost Explorer's [start, end+1)."""
    try:
        s, e = date.fromisoformat(start), date.fromisoformat(end)
    except (TypeError, ValueError) as exc:
        raise ToolError("Dates must be YYYY-MM-DD") from exc
    if e < s:
        raise ToolError("end_date is before start_date")
    if (e - s).days + 1 > MAX_RANGE_DAYS:
        raise ToolError(f"Range too long; max {MAX_RANGE_DAYS} days")
    return s.isoformat(), (e + timedelta(days=1)).isoformat()


def _service_filter(service: str | None) -> dict:
    if not service:
        return EXCLUDE_CREDITS
    return {"And": [EXCLUDE_CREDITS, {"Dimensions": {"Key": "SERVICE", "Values": [service]}}]}


def _grouped_totals(start: str, end: str, key: str, filt: dict) -> list[tuple[str, float]]:
    totals: dict[str, float] = {}
    token = None
    while True:
        kwargs = dict(
            TimePeriod={"Start": start, "End": end},
            Granularity="MONTHLY",
            Metrics=["UnblendedCost"],
            GroupBy=[{"Type": "DIMENSION", "Key": key}],
            Filter=filt,
        )
        if token:
            kwargs["NextPageToken"] = token
        resp = ce.get_cost_and_usage(**kwargs)
        for period in resp["ResultsByTime"]:
            for g in period.get("Groups", []):
                k = g["Keys"][0]
                totals[k] = totals.get(k, 0.0) + float(g["Metrics"]["UnblendedCost"]["Amount"])
        token = resp.get("NextPageToken")
        if not token:
            break
    ranked = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)
    return [(k, round(v, 2)) for k, v in ranked if abs(v) >= 0.005][:TOP_N]


def get_cost_by_service(start_date: str, end_date: str) -> dict:
    s, e = _dates(start_date, end_date)
    rows = _grouped_totals(s, e, "SERVICE", EXCLUDE_CREDITS)
    return {"start_date": start_date, "end_date": end_date, "top_services_usd": dict(rows)}


def get_service_breakdown(service: str, start_date: str, end_date: str, dimension: str = "USAGE_TYPE") -> dict:
    dimension = (dimension or "USAGE_TYPE").upper()
    if dimension not in ALLOWED_DIMENSIONS:
        raise ToolError(f"dimension must be one of {sorted(ALLOWED_DIMENSIONS)}")
    if not service:
        raise ToolError("service is required, for example 'Amazon Elastic Compute Cloud - Compute'")
    s, e = _dates(start_date, end_date)
    rows = _grouped_totals(s, e, dimension, _service_filter(service))
    return {"service": service, "dimension": dimension, "start_date": start_date,
            "end_date": end_date, "top_items_usd": dict(rows)}


def get_daily_trend(start_date: str, end_date: str, service: str | None = None) -> dict:
    s, e = _dates(start_date, end_date)
    series: dict[str, float] = {}
    token = None
    while True:
        kwargs = dict(
            TimePeriod={"Start": s, "End": e},
            Granularity="DAILY",
            Metrics=["UnblendedCost"],
            Filter=_service_filter(service),
        )
        if token:
            kwargs["NextPageToken"] = token
        resp = ce.get_cost_and_usage(**kwargs)
        for period in resp["ResultsByTime"]:
            series[period["TimePeriod"]["Start"]] = round(float(period["Total"]["UnblendedCost"]["Amount"]), 2)
        token = resp.get("NextPageToken")
        if not token:
            break
    return {"service": service or "ALL", "daily_usd": series}


FUNCTIONS = {
    "get_cost_by_service": get_cost_by_service,
    "get_service_breakdown": get_service_breakdown,
    "get_daily_trend": get_daily_trend,
}

_DATE = {"type": "string", "description": "Date as YYYY-MM-DD"}
_SERVICE_DESC = ("Exact Cost Explorer service name, as returned by get_cost_by_service, "
                 "e.g. 'Amazon Elastic Compute Cloud - Compute' or 'EC2 - Other'")

# Tool definitions sent to Claude (Messages API format).
TOOL_SCHEMAS = [
    {
        "name": "get_cost_by_service",
        "description": "Top AWS services by unblended cost in USD between two dates (both inclusive). "
                       "Use it to see which services account for the spend in a period.",
        "input_schema": {
            "type": "object",
            "properties": {"start_date": _DATE, "end_date": _DATE},
            "required": ["start_date", "end_date"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_service_breakdown",
        "description": "Break one service's cost down by a dimension to find what drove a change. "
                       "Start with USAGE_TYPE; use REGION, OPERATION, INSTANCE_TYPE, PURCHASE_TYPE or "
                       "LINKED_ACCOUNT when usage type alone does not explain it.",
        "input_schema": {
            "type": "object",
            "properties": {
                "service": {"type": "string", "description": _SERVICE_DESC},
                "start_date": _DATE,
                "end_date": _DATE,
                "dimension": {"type": "string", "enum": sorted(ALLOWED_DIMENSIONS),
                              "description": "Dimension to group by; defaults to USAGE_TYPE"},
            },
            "required": ["service", "start_date", "end_date"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_daily_trend",
        "description": "Daily cost series in USD for the whole account, or for one service. "
                       "Use it to tell a one-day blip from a new steady level.",
        "input_schema": {
            "type": "object",
            "properties": {
                "start_date": _DATE,
                "end_date": _DATE,
                "service": {"type": "string", "description": "Optional. " + _SERVICE_DESC},
            },
            "required": ["start_date", "end_date"],
            "additionalProperties": False,
        },
    },
]


def run_tool(name: str, args: dict) -> tuple[str, bool]:
    """Run one tool call. Returns (JSON text for the tool_result, is_error)."""
    log.info(json.dumps({"tool": name, "input": args}))
    fn = FUNCTIONS.get(name)
    if fn is None:
        return json.dumps({"error": f"Unknown tool {name}"}), True
    try:
        return json.dumps(fn(**(args or {}))), False
    except ToolError as exc:
        return json.dumps({"error": str(exc)}), True
    except TypeError as exc:
        return json.dumps({"error": f"Bad parameters: {exc}"}), True
    except Exception as exc:  # noqa: BLE001 - report AWS failures to the model instead of crashing
        log.exception("Tool %s failed", name)
        return json.dumps({"error": f"Cost Explorer call failed: {exc.__class__.__name__}"}), True
