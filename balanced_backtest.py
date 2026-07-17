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


def load_events(markets: str, predictions: str, btc_path: str | None = None) -> pd.DataFrame:
    m = pd.read_csv(markets)
    p = pd.read_csv(predictions).drop(columns=["actual"], errors="ignore")
    if "start_ts" not in p:
        p["start_ts"] = pd.to_datetime(p["timestamp"], utc=True).map(
            lambda x: int(x.timestamp())
        )
    z = m.merge(p, on="start_ts", suffixes=("_market", "_model"))
    z = z.dropna(subset=["actual", "prediction", "up_price", "down_price"])
    z = z[(z.up_votes >= 28) | (z.down_votes >= 28)].copy()
    z = z.sort_values("start_ts").reset_index(drop=True)
    z["decision_ts"] = z["start_ts"] + 300
    if not btc_path:
        z["abnormal_regime"] = False
        z["vol_ratio"] = 1.0
        z["range_ratio"] = 1.0
        z["shock_z"] = 0.0
        return z

    b = pd.read_csv(btc_path).sort_values("timestamp")
    b["btc_ts"] = (b["timestamp"] // 1000).astype("int64")
    ret1 = b["close"].pct_change()
    vol30 = ret1.rolling(30, min_periods=20).std()
    vol_base = vol30.rolling(360, min_periods=120).median()
    range15 = (
        b["high"].rolling(15, min_periods=10).max()
        - b["low"].rolling(15, min_periods=10).min()
    ) / b["close"]
    range_base = range15.rolling(360, min_periods=120).median()
    shock_scale = ret1.rolling(360, min_periods=120).std() * np.sqrt(5.0)
    b["vol_ratio"] = (vol30 / vol_base).replace([np.inf, -np.inf], np.nan)
    b["range_ratio"] = (range15 / range_base).replace([np.inf, -np.inf], np.nan)
    b["shock_z"] = (b["close"].pct_change(5).abs() / shock_scale).replace(
        [np.inf, -np.inf], np.nan
    )
    b["abnormal_regime"] = (
        (b["vol_ratio"] > 1.8)
        | (b["range_ratio"] > 2.2)
        | (b["shock_z"] > 3.0)
    )
    cols = ["btc_ts", "abnormal_regime", "vol_ratio", "range_ratio", "shock_z"]
    return pd.merge_asof(
        z.sort_values("decision_ts"),
        b[cols].dropna(subset=["btc_ts"]).sort_values("btc_ts"),
        left_on="decision_ts",
        right_on="btc_ts",
        direction="backward",
    ).fillna({"abnormal_regime": False, "vol_ratio": 1.0, "range_ratio": 1.0, "shock_z": 0.0})


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--markets", required=True)
    ap.add_argument("--predictions", required=True)
    ap.add_argument("--btc")
    ap.add_argument("--out", default="balanced_report.json")
    ap.add_argument("--bankroll", type=float, default=1000.0)
    ap.add_argument("--base-position", type=float, default=0.01)
    ap.add_argument("--slippage", type=float, default=0.01)
    ap.add_argument("--profit-reinvest", type=float, default=0.0)
    ap.add_argument("--fixed-stake", type=float, default=0.0)
    ap.add_argument("--daily-stop", type=float, default=0.04)
    a = ap.parse_args()

    z = load_events(a.markets, a.predictions, a.btc)
    bankroll = peak = a.bankroll
    loss_streak = 0
    caution_left = 0
    regime_paused = False
    normal_regime_streak = 0
    paper_results: deque[int] = deque(maxlen=3)
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

        abnormal = bool(r.abnormal_regime)
        if regime_paused:
            normal_regime_streak = normal_regime_streak + 1 if not abnormal else 0
            paper_results.append(int(correct))
            skipped["regime_monitor"] += 1
            # Re-enable only after 3 normal observations and 2/3 paper wins.
            if normal_regime_streak >= 3 and len(paper_results) == 3 and sum(paper_results) >= 2:
                regime_paused = False
                loss_streak = 0
                skipped["regime_recovered"] += 1
            continue
        if loss_streak >= 2 and abnormal:
            regime_paused = True
            normal_regime_streak = 0
            paper_results.clear()
            paper_results.append(int(correct))
            skipped["regime_trigger"] += 1
            continue
        if a.daily_stop > 0 and day_pnl[day] <= -a.daily_stop * max(a.bankroll, bankroll):
            skipped["daily_stop"] += 1
            continue

        # Core stake remains active. Price tiers add capital where payout is better.
        if paid <= 0.55:
            value_mult = 1.40
        elif paid <= 0.70:
            value_mult = 1.20
        elif paid <= 0.82:
            value_mult = 0.95
        else:
            value_mult = 0.50

        vote_strength = max(int(r.up_votes), int(r.down_votes))
        vote_mult = 1.10 if vote_strength >= 30 else (0.90 if vote_strength == 28 else 1.0)
        current_dd = (bankroll - peak) / peak if peak else 0.0
        drawdown_mult = 0.40 if current_dd <= -0.07 else (0.70 if current_dd <= -0.04 else 1.0)
        loss_mult = 0.50 if loss_streak >= 2 else 1.0
        risk_mult = (0.60 if caution_left else 1.0) * drawdown_mult * loss_mult

        recent_side = list(side_returns[direction])
        side_mult = 0.65 if len(recent_side) >= 10 and np.mean(recent_side) < 0 else 1.0
        fraction = min(0.02, a.base_position * value_mult * vote_mult * risk_mult * side_mult)
        # Money management is selectable without changing the prediction model.
        risk_capital = a.bankroll + max(0.0, bankroll - a.bankroll) * a.profit_reinvest
        stake = a.fixed_stake if a.fixed_stake > 0 else risk_capital * fraction
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
                regime_paused = True
                normal_regime_streak = 0
                paper_results.clear()
                action = "regime_review_after_5_losses"
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
                "abnormal_regime": abnormal,
                "vol_ratio": float(r.vol_ratio),
                "range_ratio": float(r.range_ratio),
                "shock_z": float(r.shock_z),
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
        "max_drawdown_pct": float(((t.bankroll - t.bankroll.cummax()) / t.bankroll.cummax()).min()) if len(t) else None,
        "sharpe_per_trade": float(returns.mean() / returns.std()) if len(t) and returns.std() > 0 else None,
        "skipped": dict(skipped),
        "rules": {
            "core_position": a.base_position,
            "fixed_stake": a.fixed_stake,
            "profit_reinvest_fraction": a.profit_reinvest,
            "maximum_position": 0.02,
            "two_losses": "reduce 40% for next 3 trades",
            "two_losses": "re-evaluate regime; halve size while still normal",
            "five_losses": "force regime review",
            "regime_pause": "monitor without orders until 3 normal checks and 2/3 paper wins",
            "recovery": "resume normal sizing after regime and paper checks pass",
            "side_drift": "reduce that side 35%, do not disable it",
            "daily_stop": "disabled" if a.daily_stop <= 0 else f"{a.daily_stop:.1%} of bankroll",
            "drawdown_throttle": "30% reduction at -4%; 60% reduction at -7%",
            "profit_lock": "enabled" if a.profit_reinvest <= 0 else "disabled/full reinvest",
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
