"""Fail-closed risk gate for BTC Polymarket 15-minute live/paper execution.

This module never places an order.  It validates a point-in-time snapshot and
returns an auditable ALLOW/SKIP decision for an execution adapter.
"""
from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict, dataclass, field


@dataclass
class LiveSnapshot:
    timestamp: float
    market_data_timestamp: float
    btc_data_timestamp: float
    votes: int
    calibrated_probability: float
    ask_price: float
    best_bid: float
    opposite_ask: float
    vwap_60s: float
    top5_ask_notional: float
    vol_ratio: float
    range_ratio: float
    shock_z: float
    bankroll: float
    day_pnl: float = 0.0
    loss_streak: int = 0
    api_healthy: bool = True


@dataclass
class GuardConfig:
    minimum_votes: int = 30
    minimum_edge: float = 0.06
    minimum_price: float = 0.45
    maximum_price: float = 0.90
    maximum_spread: float = 0.03
    maximum_complement_gap: float = 0.10
    maximum_vwap_deviation: float = 0.12
    maximum_vol_ratio: float = 1.8
    maximum_range_ratio: float = 2.2
    maximum_shock_z: float = 3.0
    maximum_data_age_seconds: float = 2.0
    maximum_depth_fraction: float = 0.10
    maximum_stake: float = 10.0
    maximum_bankroll_fraction: float = 0.01
    daily_loss_fraction: float = 0.03
    loss_streak_pause: int = 3


@dataclass
class GuardDecision:
    allowed: bool
    stake: float
    reasons: list[str] = field(default_factory=list)
    observed_edge: float = 0.0


def evaluate(snapshot: LiveSnapshot, config: GuardConfig | None = None) -> GuardDecision:
    c = config or GuardConfig()
    reasons: list[str] = []
    now = snapshot.timestamp or time.time()
    edge = snapshot.calibrated_probability - snapshot.ask_price

    if not snapshot.api_healthy:
        reasons.append("api_unhealthy")
    if now - snapshot.market_data_timestamp > c.maximum_data_age_seconds:
        reasons.append("stale_market_data")
    if now - snapshot.btc_data_timestamp > c.maximum_data_age_seconds:
        reasons.append("stale_btc_data")
    if snapshot.votes < c.minimum_votes:
        reasons.append("weak_consensus")
    if edge < c.minimum_edge:
        reasons.append("insufficient_edge_after_price")
    if not c.minimum_price <= snapshot.ask_price <= c.maximum_price:
        reasons.append("price_outside_live_band")
    if snapshot.ask_price - snapshot.best_bid > c.maximum_spread:
        reasons.append("spread_too_wide")
    if abs(snapshot.ask_price + snapshot.opposite_ask - 1.0) > c.maximum_complement_gap:
        reasons.append("incoherent_complement_prices")
    if abs(snapshot.ask_price - snapshot.vwap_60s) > c.maximum_vwap_deviation:
        reasons.append("unstable_trade_price")
    if snapshot.vol_ratio > c.maximum_vol_ratio:
        reasons.append("extreme_volatility")
    if snapshot.range_ratio > c.maximum_range_ratio:
        reasons.append("extreme_range")
    if snapshot.shock_z > c.maximum_shock_z:
        reasons.append("price_shock")
    if snapshot.day_pnl <= -c.daily_loss_fraction * snapshot.bankroll:
        reasons.append("daily_loss_stop")
    if snapshot.loss_streak >= c.loss_streak_pause:
        reasons.append("loss_streak_pause")

    stake = min(c.maximum_stake, c.maximum_bankroll_fraction * snapshot.bankroll)
    if stake > c.maximum_depth_fraction * snapshot.top5_ask_notional:
        reasons.append("insufficient_top5_depth")
    if stake < 1.0:
        reasons.append("stake_below_minimum")

    return GuardDecision(not reasons, stake if not reasons else 0.0, reasons, edge)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("snapshot", help="Point-in-time JSON snapshot")
    a = p.parse_args()
    with open(a.snapshot, encoding="utf-8") as fh:
        snapshot = LiveSnapshot(**json.load(fh))
    print(json.dumps(asdict(evaluate(snapshot)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
