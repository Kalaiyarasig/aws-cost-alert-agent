"""Pure spike-detection logic. No AWS calls here, so it is easy to unit test."""

from __future__ import annotations

from dataclasses import dataclass, field
from statistics import mean, pstdev


@dataclass
class ServiceDelta:
    service: str
    today: float
    baseline: float

    @property
    def delta(self) -> float:
        return self.today - self.baseline


@dataclass
class SpikeResult:
    date: str
    total: float
    baseline: float
    stdev: float
    is_spike: bool
    reason: str
    top_movers: list[ServiceDelta] = field(default_factory=list)

    @property
    def delta(self) -> float:
        return self.total - self.baseline

    @property
    def pct_change(self) -> float:
        return (self.delta / self.baseline * 100) if self.baseline else float("inf")

    def summary(self) -> str:
        lines = [
            f"Date: {self.date}",
            f"Spend: ${self.total:,.2f} vs baseline ${self.baseline:,.2f} "
            f"({self.delta:+,.2f} USD, {self.pct_change:+.0f}%)",
            "Top movers vs baseline:",
        ]
        for m in self.top_movers:
            lines.append(f"  - {m.service}: ${m.today:,.2f} (baseline ${m.baseline:,.2f}, {m.delta:+,.2f})")
        return "\n".join(lines)


def parse_cost_explorer(results_by_time: list[dict]) -> dict[str, dict[str, float]]:
    """Turn GetCostAndUsage (DAILY, GROUP BY SERVICE) output into {date: {service: cost}}."""
    out: dict[str, dict[str, float]] = {}
    for day in results_by_time:
        date = day["TimePeriod"]["Start"]
        services: dict[str, float] = {}
        for g in day.get("Groups", []):
            amount = float(g["Metrics"]["UnblendedCost"]["Amount"])
            services[g["Keys"][0]] = services.get(g["Keys"][0], 0.0) + amount
        out[date] = services
    return out


def detect_spike(
    daily: dict[str, dict[str, float]],
    target_date: str,
    pct_threshold: float = 30.0,
    min_abs_increase: float = 5.0,
    z_threshold: float = 2.0,
    top_n: int = 5,
) -> SpikeResult:
    """Compare target_date's spend to the mean of the days before it.

    A spike needs ALL of:
      * spend above baseline by more than pct_threshold percent,
      * an absolute increase of at least min_abs_increase USD (ignores noise on tiny bills),
      * a z-score of at least z_threshold when there is enough history to compute one.
    """
    if target_date not in daily:
        raise ValueError(f"No cost data for {target_date}")

    history_dates = sorted(d for d in daily if d < target_date)
    if len(history_dates) < 3:
        return SpikeResult(target_date, sum(daily[target_date].values()), 0.0, 0.0, False,
                           "Not enough history (need at least 3 prior days)")

    totals = [sum(daily[d].values()) for d in history_dates]
    baseline = mean(totals)
    stdev = pstdev(totals)
    today_services = daily[target_date]
    total = sum(today_services.values())

    # Per-service baseline, used to rank which services moved most.
    all_services = set(today_services)
    for d in history_dates:
        all_services.update(daily[d])
    movers = [
        ServiceDelta(s, today_services.get(s, 0.0), mean(daily[d].get(s, 0.0) for d in history_dates))
        for s in all_services
    ]
    movers.sort(key=lambda m: m.delta, reverse=True)
    top = [m for m in movers if m.delta > 0][:top_n]

    increase = total - baseline
    pct = (increase / baseline * 100) if baseline > 0 else (float("inf") if increase > 0 else 0.0)
    z = (increase / stdev) if stdev > 0 else (float("inf") if increase > 0 else 0.0)

    checks = {
        "pct": pct >= pct_threshold,
        "abs": increase >= min_abs_increase,
        "z": z >= z_threshold,
    }
    is_spike = all(checks.values())
    if is_spike:
        reason = f"Spend up {pct:.0f}% (${increase:,.2f}), z-score {z:.1f}"
    else:
        failed = ", ".join(k for k, ok in checks.items() if not ok)
        reason = f"No spike (below threshold on: {failed})"

    return SpikeResult(target_date, total, baseline, stdev, is_spike, reason, top)
