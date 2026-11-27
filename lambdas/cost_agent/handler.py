"""Scheduled Lambda: pull daily cost from Cost Explorer, detect a spike, have Claude
explain it, and email the explanation through SNS.

Manual test event (forces an explanation even without a spike):
    {"force": true, "date": "2026-11-20"}
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timedelta, timezone

import boto3

from detector import SpikeResult, detect_spike, parse_cost_explorer
from explainer import explain_spike

logging.getLogger().setLevel(logging.INFO)
log = logging.getLogger(__name__)

# Cost Explorer is a global service served from us-east-1.
ce = boto3.client("ce", region_name="us-east-1")
sns = boto3.client("sns")

LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "14"))
PCT_THRESHOLD = float(os.environ.get("PCT_THRESHOLD", "30"))
MIN_ABS_INCREASE = float(os.environ.get("MIN_ABS_INCREASE", "5"))
Z_THRESHOLD = float(os.environ.get("Z_THRESHOLD", "2"))


def fetch_daily_by_service(start: date, end_exclusive: date) -> dict[str, dict[str, float]]:
    results, token = [], None
    while True:
        kwargs = dict(
            TimePeriod={"Start": start.isoformat(), "End": end_exclusive.isoformat()},
            Granularity="DAILY",
            Metrics=["UnblendedCost"],
            GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
            # Credits and refunds would hide real usage spikes.
            Filter={"Not": {"Dimensions": {"Key": "RECORD_TYPE", "Values": ["Credit", "Refund"]}}},
        )
        if token:
            kwargs["NextPageToken"] = token
        resp = ce.get_cost_and_usage(**kwargs)
        results.extend(resp["ResultsByTime"])
        token = resp.get("NextPageToken")
        if not token:
            break
    return parse_cost_explorer(results)


def publish(result: SpikeResult, explanation: str) -> None:
    subject = f"AWS cost spike {result.date}: ${result.total:,.2f} ({result.pct_change:+.0f}%)"
    body = (
        f"{explanation}\n\n"
        "---- Raw numbers ----\n"
        f"{result.summary()}\n"
        f"Detector: {result.reason}\n"
    )
    sns.publish(TopicArn=os.environ["TOPIC_ARN"], Subject=subject[:100], Message=body)


# How many extra days to step back when the newest Cost Explorer data isn't ready yet.
MAX_DATA_LAG_DAYS = 3


def fetch_latest(target: date, fixed: bool) -> tuple[date, dict[str, dict[str, float]]]:
    """Fetch the lookback window ending on target. Cost Explorer can lag a day or two (more on new
    accounts) and then raises DataUnavailableException; unless the caller fixed the date, step back."""
    for lag in range(MAX_DATA_LAG_DAYS + 1):
        day = target - timedelta(days=lag)
        try:
            return day, fetch_daily_by_service(day - timedelta(days=LOOKBACK_DAYS), day + timedelta(days=1))
        except ce.exceptions.DataUnavailableException:
            if fixed or lag == MAX_DATA_LAG_DAYS:
                raise
            log.warning("Cost Explorer data for %s not ready yet; trying the day before", day)
    raise AssertionError("unreachable")


def handler(event, _context):
    event = event or {}
    today = datetime.now(timezone.utc).date()
    fixed = bool(event.get("date"))
    target = date.fromisoformat(event["date"]) if fixed else today - timedelta(days=1)
    target, daily = fetch_latest(target, fixed)

    result = detect_spike(
        daily, target.isoformat(),
        pct_threshold=PCT_THRESHOLD, min_abs_increase=MIN_ABS_INCREASE, z_threshold=Z_THRESHOLD,
    )
    log.info(json.dumps({"date": result.date, "total": round(result.total, 2),
                         "baseline": round(result.baseline, 2), "is_spike": result.is_spike,
                         "reason": result.reason}))

    if not (result.is_spike or event.get("force")):
        return {"spike": False, "reason": result.reason}

    try:
        explanation = explain_spike(f"{result.summary()}\nDetector verdict: {result.reason}", today=today)
    except Exception:  # noqa: BLE001 - never lose the alert because the AI step failed
        log.exception("AI explanation failed; sending numbers only")
        explanation = "(The AI explanation was unavailable for this alert. Raw numbers below.)"

    publish(result, explanation)
    return {"spike": result.is_spike, "forced": bool(event.get("force")), "reason": result.reason}
