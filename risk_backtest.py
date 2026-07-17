"""Walk-forward value and risk engine for BTC Polymarket 15-minute markets.

All adaptive decisions use only information available before each market.
This module is a research backtester, not an order-execution client.
"""
from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict, deque

import numpy as np
import pandas as pd


def fee_rate(price: float) -> float:
    x = min(price, 1.0 - price)
    return 0.0156 * (x / 0.5) ** 2


def wilson_lower(wins: int, total: int, z: float = 1.0) -> float:
    if total <= 0:
        return 0.0
    p = wins / total
    d = 1.0 + z * z / total
    c = p + z * z / (2.0 * total)
    r = z * math.sqrt((p * (1.0 - p) + z * z / (4.0 * total)) / total)
    return (c - r) / d


def load_events(markets_path: str, predictions_path: str) -> pd.DataFrame:
    m = pd.read_csv(markets_path)
    p = pd.read_csv(predictions_path)
    if "start_ts" not in p:
        p["start_ts"] = pd.to_datetime(p["timestamp"], utc=True).map(
            lambda x: int(x.timestamp())
        )
    p = p.drop(columns=["actual"], errors="ignore")
    z = m.merge(p, on="start_ts", suffixes=("_market", "_model"))
    z = z.dropna(subset=["actual", "prediction", "up_price", "down_price"])
    z = z[(z["up_votes"] >= 28) | (z["down_votes"] >= 28)].copy()
    return z.sort_values("start_ts").reset_index(drop=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--markets", required=True)
    ap.add_argument("--predictions", required=True)
    ap.add_argument("--out", default="risk_report.json")
    ap.add_argument("--bankroll", type=float, default=1000.0)
    ap.add_argument("--edge", type=float, default=0.06)
    ap.add_argument("--slippage", type=float, default=0.01)
    ap.add_argument("--kelly-fraction", type=float, default=0.25)
    ap.add_argument("--max-position", type=float, default=0.02)
    ap.add_argument("--warmup", type=int, default=40)
    a = ap.parse_args()

    z = load_events(a.markets, a.predictions)
    bankroll = a.bankroll
    peak = bankroll
    loss_streak = 0
    recovery_wins = 0
    cooldown = 0
    hard_pause = 0
    side_pause = {0: 0, 1: 0}
    outcomes: deque[int] = deque(maxlen=240)
    side_outcomes = {0: deque(maxlen=80), 1: deque(maxlen=80)}
    side_pnls = {0: deque(maxlen=20), 1: deque(maxlen=20)}
    day_pnl: dict[str, float] = defaultdict(float)
    rows = []
    skipped = defaultdict(int)

    for _, r in z.iterrows():
        direction = int(r["prediction"])
        actual = int(r["actual"])
        market_price = float(r["up_price"] if direction == 1 else r["down_price"])
        paid = min(0.99, market_price + a.slippage)
        correct = direction == actual
        day = pd.to_datetime(int(r["start_ts"]), unit="s", utc=True).strftime("%Y-%m-%d")

        # Decrement pauses on eligible market signals, not wall-clock time.
        if cooldown > 0:
            cooldown -= 1
            skipped["cooldown"] += 1
            outcomes.append(int(correct))
            side_outcomes[direction].append(int(correct))
            continue
        if hard_pause > 0:
            hard_pause -= 1
            skipped["hard_pause"] += 1
            outcomes.append(int(correct))
            side_outcomes[direction].append(int(correct))
            continue
        for side in (0, 1):
            side_pause[side] = max(0, side_pause[side] - 1)
        if side_pause[direction] > 0:
            skipped["side_pause"] += 1
            outcomes.append(int(correct))
            side_outcomes[direction].append(int(correct))
            continue

        # Bayesian-smoothed, walk-forward calibration. No current outcome is used.
        hist = list(outcomes)
        shist = list(side_outcomes[direction])
        global_p = (sum(hist) + 13.0) / (len(hist) + 20.0)  # prior mean 65%
        side_p = (sum(shist) + 6.5) / (len(shist) + 10.0)
        calibrated_p = 0.55 * side_p + 0.45 * global_p
        confidence_floor = min(0.95, max(r["up_votes"], r["down_votes"]) / 31.0)
        calibrated_p = min(calibrated_p, confidence_floor)
        edge = calibrated_p - paid

        if len(hist) < a.warmup:
            skipped["calibration_warmup"] += 1
            outcomes.append(int(correct))
            side_outcomes[direction].append(int(correct))
            continue
        if edge < a.edge:
            skipped["no_value"] += 1
            outcomes.append(int(correct))
            side_outcomes[direction].append(int(correct))
            continue
        if day_pnl[day] <= -0.03 * max(a.bankroll, bankroll):
            skipped["daily_stop"] += 1
            outcomes.append(int(correct))
            side_outcomes[direction].append(int(correct))
            continue

        full_kelly = max(0.0, edge / max(1e-9, 1.0 - paid))
        fraction = min(a.max_position, a.kelly_fraction * full_kelly)
        if loss_streak >= 2 or recovery_wins < 2:
            fraction *= 0.5
        stake = bankroll * fraction
        if stake < 1.0:
            skipped["stake_too_small"] += 1
            outcomes.append(int(correct))
            side_outcomes[direction].append(int(correct))
            continue

        fee = stake * fee_rate(paid)
        pnl = stake / paid - stake - fee if correct else -stake - fee
        bankroll += pnl
        peak = max(peak, bankroll)
        day_pnl[day] += pnl
        side_pnls[direction].append(pnl / stake)

        if correct:
            loss_streak = 0
            recovery_wins += 1
        else:
            loss_streak += 1
            recovery_wins = 0

        action = "normal"
        if loss_streak == 2:
            action = "reduce_half"
        elif loss_streak == 3:
            cooldown = 4
            action = "cooldown_4"
        elif loss_streak >= 5:
            hard_pause = 16
            action = "hard_pause_16"

        recent_side = list(side_pnls[direction])
        if len(recent_side) >= 10 and np.mean(recent_side) < -0.03:
            side_pause[direction] = 12
            action = f"pause_side_{direction}_12"

        rows.append(
            {
                "start_ts": int(r["start_ts"]),
                "direction": direction,
                "actual": actual,
                "correct": bool(correct),
                "market_price": market_price,
                "paid_price": paid,
                "calibrated_probability": calibrated_p,
                "edge": edge,
                "stake": stake,
                "pnl": pnl,
                "bankroll": bankroll,
                "drawdown": bankroll - peak,
                "loss_streak_after": loss_streak,
                "risk_action": action,
            }
        )
        outcomes.append(int(correct))
        side_outcomes[direction].append(int(correct))

    trades = pd.DataFrame(rows)
    if len(trades):
        returns = trades["pnl"] / trades["stake"]
        sharpe = float(returns.mean() / returns.std()) if returns.std() > 0 else None
        max_dd = float(trades["drawdown"].min())
        wr = float(trades["correct"].mean())
        profit = float(trades["pnl"].sum())
        turnover = float(trades["stake"].sum())
    else:
        sharpe = max_dd = wr = None
        profit = turnover = 0.0

    report = {
        "candidate_signals": int(len(z)),
        "trades": int(len(trades)),
        "coverage_of_signals": float(len(trades) / len(z)) if len(z) else 0.0,
        "win_rate": wr,
        "net_profit": profit,
        "return_on_initial_bankroll": float(profit / a.bankroll),
        "return_on_turnover": float(profit / turnover) if turnover else None,
        "ending_bankroll": float(bankroll),
        "max_drawdown": max_dd,
        "sharpe_per_trade": sharpe,
        "skipped": dict(skipped),
        "rules": {
            "minimum_calibrated_edge": a.edge,
            "quarter_kelly_cap": a.max_position,
            "two_losses": "halve position",
            "three_losses": "cool down 4 eligible signals",
            "five_losses": "pause 16 eligible signals",
            "side_drift": "pause one direction for 12 signals",
            "daily_stop": "3% of bankroll",
            "recovery": "half size until two wins",
        },
        "research_warning": "Backtest only. Validate on longer walk-forward and live paper data.",
    }
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, ensure_ascii=False)
    trades.to_csv(a.out.replace(".json", "_trades.csv"), index=False)
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
