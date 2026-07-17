"""Balanced return-first overlay for BTC Polymarket 15-minute signals."""
from __future__ import annotations

import argparse
import json
from collections import defaultdict, deque

import numpy as np
import pandas as pd


def fee_rate(price: float) -> float:
    x = min(price, 1.0 - price)
    return 0.0156 * (x / 0.5) ** 2


def load_events(markets: str, predictions: str) -> pd.DataFrame:
    m = pd.read_csv(markets)
    p = pd.read_csv(predictions).drop(columns=["actual"], errors="ignore")
    if "start_ts" not in p:
        p["start_ts"] = pd.to_datetime(p["timestamp"], utc=True).map(
            lambda x: int(x.timestamp())
        )
    z = m.merge(p, on="start_ts", suffixes=("_market", "_model"))
    z = z.dropna(subset=["actual", "prediction", "up_price", "down_price"])
    z = z[(z.up_votes >= 28) | (z.down_votes >= 28)].copy()
    return z.sort_values("start_ts").reset_index(drop=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--markets", required=True)
    ap.add_argument("--predictions", required=True)
    ap.add_argument("--out", default="balanced_report.json")
    ap.add_argument("--bankroll", type=float, default=1000.0)
    ap.add_argument("--base-position", type=float, default=0.01)
    ap.add_argument("--slippage", type=float, default=0.01)
    a = ap.parse_args()

    z = load_events(a.markets, a.predictions)
    bankroll = peak = a.bankroll
    loss_streak = 0
    caution_left = 0
    cooldown = 0
    day_pnl = defaultdict(float)
    side_returns = {0: deque(maxlen=20), 1: deque(maxlen=20)}
    rows = []
    skipped = defaultdict(int)

    for _, r in z.iterrows():
        direction = int(r.prediction)
        actual = int(r.actual)
        correct = direction == actual
        price = float(r.up_price if direction == 1 else r.down_price)
        paid = min(0.99, price + a.slippage)
        day = pd.to_datetime(int(r.start_ts), unit="s", utc=True).strftime("%Y-%m-%d")

        if cooldown:
            cooldown -= 1
            skipped["five_loss_cooldown"] += 1
            continue
        if day_pnl[day] <= -0.04 * max(a.bankroll, bankroll):
            skipped["daily_stop"] += 1
            continue

        # Core stake remains active. Price tiers add capital where payout is better.
        if paid <= 0.55:
            value_mult = 1.60
        elif paid <= 0.70:
            value_mult = 1.30
        elif paid <= 0.82:
            value_mult = 1.00
        else:
            value_mult = 0.55

        vote_strength = max(int(r.up_votes), int(r.down_votes))
        vote_mult = 1.10 if vote_strength >= 30 else (0.90 if vote_strength == 28 else 1.0)
        current_dd = (bankroll - peak) / peak if peak else 0.0
        drawdown_mult = 0.40 if current_dd <= -0.07 else (0.70 if current_dd <= -0.04 else 1.0)
        risk_mult = (0.60 if caution_left else 1.0) * drawdown_mult

        recent_side = list(side_returns[direction])
        side_mult = 0.65 if len(recent_side) >= 10 and np.mean(recent_side) < 0 else 1.0
        fraction = min(0.02, a.base_position * value_mult * vote_mult * risk_mult * side_mult)
        # Lock half of accumulated profit and size risk from the remaining capital.
        risk_capital = a.bankroll + max(0.0, bankroll - a.bankroll) * 0.50
        stake = risk_capital * fraction
        fee = stake * fee_rate(paid)
        pnl = stake / paid - stake - fee if correct else -stake - fee
        bankroll += pnl
        peak = max(peak, bankroll)
        day_pnl[day] += pnl
        side_returns[direction].append(pnl / stake)

        action = "core"
        if correct:
            loss_streak = 0
        else:
            loss_streak += 1
            if loss_streak >= 2:
                caution_left = 3
                action = "reduce_40pct_next_3"
            if loss_streak >= 5:
                cooldown = 4
                action = "pause_4_after_5_losses"
        if caution_left:
            caution_left -= 1

        rows.append(
            {
                "start_ts": int(r.start_ts),
                "direction": direction,
                "actual": actual,
                "correct": bool(correct),
                "paid_price": paid,
                "vote_strength": vote_strength,
                "stake": stake,
                "pnl": pnl,
                "bankroll": bankroll,
                "drawdown": bankroll - peak,
                "loss_streak_after": loss_streak,
                "risk_action": action,
            }
        )

    t = pd.DataFrame(rows)
    returns = t.pnl / t.stake if len(t) else pd.Series(dtype=float)
    report = {
        "candidate_signals": int(len(z)),
        "trades": int(len(t)),
        "win_rate": float(t.correct.mean()) if len(t) else None,
        "net_profit": float(t.pnl.sum()) if len(t) else 0.0,
        "return_on_initial_bankroll": float(t.pnl.sum() / a.bankroll) if len(t) else 0.0,
        "return_on_turnover": float(t.pnl.sum() / t.stake.sum()) if len(t) else None,
        "ending_bankroll": float(bankroll),
        "max_drawdown": float(t.drawdown.min()) if len(t) else None,
        "sharpe_per_trade": float(returns.mean() / returns.std()) if len(t) and returns.std() > 0 else None,
        "skipped": dict(skipped),
        "rules": {
            "core_position": a.base_position,
            "maximum_position": 0.02,
            "two_losses": "reduce 40% for next 3 trades",
            "five_losses": "pause only 4 signals",
            "side_drift": "reduce that side 35%, do not disable it",
            "daily_stop": "4% of bankroll",
            "drawdown_throttle": "30% reduction at -4%; 60% reduction at -7%",
            "profit_lock": "only half of accumulated profit increases position size",
            "price_tiers": "add size below 0.70; cut size above 0.82",
        },
        "research_warning": "Backtest only; parameters require longer walk-forward validation.",
    }
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    t.to_csv(a.out.replace(".json", "_trades.csv"), index=False)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
