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


def load_events(markets_path: str, predictions_path: str, btc_path: str | None = None) -> pd.DataFrame:
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
    z = z.sort_values("start_ts").reset_index(drop=True)
    z["decision_ts"] = z["start_ts"] + 300
    if not btc_path:
        z["vol_ratio"] = z["range_ratio"] = 1.0
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
    b["vol_ratio"] = vol30 / vol_base
    b["range_ratio"] = range15 / range_base
    b["shock_z"] = b["close"].pct_change(5).abs() / shock_scale
    cols = ["btc_ts", "vol_ratio", "range_ratio", "shock_z"]
    return pd.merge_asof(
        z.sort_values("decision_ts"),
        b[cols].replace([np.inf, -np.inf], np.nan).sort_values("btc_ts"),
        left_on="decision_ts",
        right_on="btc_ts",
        direction="backward",
    ).fillna({"vol_ratio": 1.0, "range_ratio": 1.0, "shock_z": 0.0})


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--markets", required=True)
    ap.add_argument("--predictions", required=True)
    ap.add_argument("--btc")
    ap.add_argument("--out", default="risk_report.json")
    ap.add_argument("--bankroll", type=float, default=1000.0)
    ap.add_argument("--edge", type=float, default=0.06)
    ap.add_argument("--slippage", type=float, default=0.01)
    ap.add_argument("--kelly-fraction", type=float, default=0.25)
    ap.add_argument("--max-position", type=float, default=0.02)
    ap.add_argument("--warmup", type=int, default=40)
    ap.add_argument("--min-votes", type=int, default=28)
    ap.add_argument("--min-paid", type=float, default=0.0)
    ap.add_argument("--profit-reinvest", type=float, default=1.0)
    ap.add_argument("--risk-capital-cap", type=float, default=0.0)
    ap.add_argument("--fixed-stake", type=float, default=0.0)
    ap.add_argument("--daily-stop", type=float, default=0.03)
    ap.add_argument("--dd-soft", type=float, default=0.04)
    ap.add_argument("--dd-hard", type=float, default=0.07)
    ap.add_argument("--dd-soft-mult", type=float, default=0.70)
    ap.add_argument("--dd-hard-mult", type=float, default=0.40)
    ap.add_argument("--pretrade-regime-filter", action="store_true")
    ap.add_argument("--max-vol-ratio", type=float, default=1.8)
    ap.add_argument("--max-range-ratio", type=float, default=2.2)
    ap.add_argument("--max-shock-z", type=float, default=3.0)
    ap.add_argument("--require-executed-trades", action="store_true")
    ap.add_argument("--max-vwap-deviation", type=float, default=0.0)
    ap.add_argument("--max-complement-gap", type=float, default=0.0)
    a = ap.parse_args()

    z = load_events(a.markets, a.predictions, a.btc)
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

        vote_strength = max(int(r["up_votes"]), int(r["down_votes"]))
        if vote_strength < a.min_votes:
            skipped["weak_consensus"] += 1
            outcomes.append(int(correct))
            side_outcomes[direction].append(int(correct))
            continue
        if paid < a.min_paid:
            skipped["price_below_winrate_floor"] += 1
            outcomes.append(int(correct))
            side_outcomes[direction].append(int(correct))
            continue

        # Hard pre-trade gates: never wait for losses before reacting to a shock.
        if a.pretrade_regime_filter and (
            float(r["vol_ratio"]) > a.max_vol_ratio
            or float(r["range_ratio"]) > a.max_range_ratio
            or float(r["shock_z"]) > a.max_shock_z
        ):
            skipped["extreme_regime"] += 1
            outcomes.append(int(correct))
            side_outcomes[direction].append(int(correct))
            continue

        if a.require_executed_trades and r.get("price_source") != "executed_trade":
            skipped["no_executed_trade"] += 1
            outcomes.append(int(correct))
            side_outcomes[direction].append(int(correct))
            continue
        selected_vwap = r.get("up_vwap_60s") if direction == 1 else r.get("down_vwap_60s")
        if a.max_vwap_deviation > 0 and (
            pd.isna(selected_vwap) or abs(market_price - float(selected_vwap)) > a.max_vwap_deviation
        ):
            skipped["unstable_trade_price"] += 1
            outcomes.append(int(correct))
            side_outcomes[direction].append(int(correct))
            continue
        complement_gap = abs(float(r.get("up_price", 0.5)) + float(r.get("down_price", 0.5)) - 1.0)
        if a.max_complement_gap > 0 and complement_gap > a.max_complement_gap:
            skipped["incoherent_complement_prices"] += 1
            outcomes.append(int(correct))
            side_outcomes[direction].append(int(correct))
            continue

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
        if a.daily_stop > 0 and day_pnl[day] <= -a.daily_stop * max(a.bankroll, bankroll):
            skipped["daily_stop"] += 1
            outcomes.append(int(correct))
            side_outcomes[direction].append(int(correct))
            continue

        full_kelly = max(0.0, edge / max(1e-9, 1.0 - paid))
        current_dd = (bankroll - peak) / peak if peak else 0.0
        dd_mult = (
            a.dd_hard_mult
            if current_dd <= -a.dd_hard
            else (a.dd_soft_mult if current_dd <= -a.dd_soft else 1.0)
        )
        fraction = min(a.max_position, a.kelly_fraction * full_kelly) * dd_mult
        if loss_streak >= 2 or recovery_wins < 2:
            fraction *= 0.5
        risk_capital = a.bankroll + max(0.0, bankroll - a.bankroll) * a.profit_reinvest
        if a.risk_capital_cap > 0:
            risk_capital = min(risk_capital, a.risk_capital_cap)
        stake = a.fixed_stake if a.fixed_stake > 0 else risk_capital * fraction
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
                "vol_ratio": float(r["vol_ratio"]),
                "range_ratio": float(r["range_ratio"]),
                "shock_z": float(r["shock_z"]),
                "vwap_deviation": None if pd.isna(selected_vwap) else abs(market_price - float(selected_vwap)),
                "complement_gap": complement_gap,
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
        "max_drawdown_pct": float(((trades.bankroll - trades.bankroll.cummax()) / trades.bankroll.cummax()).min()) if len(trades) else None,
        "sharpe_per_trade": sharpe,
        "skipped": dict(skipped),
        "rules": {
            "minimum_calibrated_edge": a.edge,
            "minimum_votes": a.min_votes,
            "minimum_paid_price": a.min_paid,
            "profit_reinvest_fraction": a.profit_reinvest,
            "risk_capital_cap": a.risk_capital_cap,
            "fixed_stake": a.fixed_stake,
            "daily_stop_fraction": a.daily_stop,
            "pretrade_regime_filter": a.pretrade_regime_filter,
            "maximum_volatility_ratio": a.max_vol_ratio,
            "maximum_range_ratio": a.max_range_ratio,
            "maximum_shock_z": a.max_shock_z,
            "require_executed_trades": a.require_executed_trades,
            "maximum_vwap_deviation": a.max_vwap_deviation,
            "maximum_complement_gap": a.max_complement_gap,
            "drawdown_throttle": {
                "soft_drawdown": a.dd_soft,
                "soft_multiplier": a.dd_soft_mult,
                "hard_drawdown": a.dd_hard,
                "hard_multiplier": a.dd_hard_mult,
            },
            "quarter_kelly_cap": a.max_position,
            "two_losses": "halve position",
            "three_losses": "cool down 4 eligible signals",
            "five_losses": "pause 16 eligible signals",
            "side_drift": "pause one direction for 12 signals",
            "daily_stop": "disabled" if a.daily_stop <= 0 else f"{a.daily_stop:.1%} of bankroll",
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
