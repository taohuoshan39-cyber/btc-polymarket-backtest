"""Replay an existing strategy ledger with an adaptive pause/recovery controller.

Only information available at each decision and previously settled outcomes is
used.  Paused signals are retained as paper predictions for recovery checks.
"""
from __future__ import annotations

import argparse
import json
from collections import deque

import numpy as np
import pandas as pd


def add_environment(t: pd.DataFrame, markets: str, btc: str) -> pd.DataFrame:
    m = pd.read_csv(markets)[
        ["start_ts", "price_source", "up_price", "down_price", "up_vwap_60s", "down_vwap_60s"]
    ]
    # Recompute every environment field from the raw inputs.  Some source
    # ledgers already contain older regime columns; retaining them would make
    # pandas suffix the new point-in-time fields with ``_x``/``_y``.
    derived = ["price_source", "up_price", "down_price", "up_vwap_60s", "down_vwap_60s",
               "btc_ts", "vol_ratio", "range_ratio", "shock_z", "decision_ts"]
    t = t.drop(columns=[c for c in derived if c in t], errors="ignore").merge(m, on="start_ts")
    t["decision_ts"] = t["start_ts"] + 300

    b = pd.read_csv(btc).sort_values("timestamp")
    b["btc_ts"] = (b["timestamp"] // 1000).astype("int64")
    ret1 = b["close"].pct_change()
    vol30 = ret1.rolling(30, min_periods=20).std()
    vol_base = vol30.rolling(360, min_periods=120).median()
    range15 = (b["high"].rolling(15, min_periods=10).max() - b["low"].rolling(15, min_periods=10).min()) / b["close"]
    range_base = range15.rolling(360, min_periods=120).median()
    shock_scale = ret1.rolling(360, min_periods=120).std() * np.sqrt(5.0)
    b["vol_ratio"] = vol30 / vol_base
    b["range_ratio"] = range15 / range_base
    b["shock_z"] = b["close"].pct_change(5).abs() / shock_scale
    return pd.merge_asof(
        t.sort_values("decision_ts"),
        b[["btc_ts", "vol_ratio", "range_ratio", "shock_z"]].replace([np.inf, -np.inf], np.nan).sort_values("btc_ts"),
        left_on="decision_ts", right_on="btc_ts", direction="backward",
    ).fillna({"vol_ratio": 1.0, "range_ratio": 1.0, "shock_z": 0.0})


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--trades", required=True)
    p.add_argument("--markets", required=True)
    p.add_argument("--btc", required=True)
    p.add_argument("--out", default="adaptive_report.json")
    p.add_argument("--bankroll", type=float, default=1000.0)
    p.add_argument("--rolling-floor", type=float, required=True)
    p.add_argument("--rolling-window", type=int, default=20)
    p.add_argument("--loss-trigger", type=int, default=2)
    p.add_argument("--normal-required", type=int, default=3)
    p.add_argument("--paper-window", type=int, default=3)
    p.add_argument("--paper-wins", type=int, default=2)
    a = p.parse_args()

    t = add_environment(pd.read_csv(a.trades), a.markets, a.btc)
    bankroll = peak = a.bankroll
    mode = "ACTIVE"
    loss_streak = normal_streak = 0
    recent: deque[int] = deque(maxlen=a.rolling_window)
    paper: deque[int] = deque(maxlen=a.paper_window)
    rows, events = [], []

    for _, r in t.iterrows():
        correct = bool(r.correct)
        direction = int(r.direction)
        vwap = r.up_vwap_60s if direction == 1 else r.down_vwap_60s
        price = r.up_price if direction == 1 else r.down_price
        suitable = (
            r.price_source == "executed_trade"
            and not pd.isna(vwap)
            and abs(float(price) - float(vwap)) <= 0.12
            and abs(float(r.up_price) + float(r.down_price) - 1.0) <= 0.10
            and float(r.vol_ratio) <= 1.8
            and float(r.range_ratio) <= 2.2
            and float(r.shock_z) <= 3.0
        )
        # Ordinary regime deterioration is evaluated after a loss cluster.
        # Only genuinely extreme conditions can interrupt an otherwise healthy
        # strategy immediately; this avoids turning the controller into a
        # permanently restrictive static entry filter.
        extreme = (
            r.price_source != "executed_trade"
            or pd.isna(vwap)
            or abs(float(r.up_price) + float(r.down_price) - 1.0) > 0.18
            or float(r.vol_ratio) > 2.8
            or float(r.range_ratio) > 3.2
            or float(r.shock_z) > 4.5
        )
        rolling_ok = len(recent) < a.rolling_window or np.mean(recent) >= a.rolling_floor

        if mode == "ACTIVE" and (extreme or not rolling_ok):
            mode = "PAUSED"
            normal_streak = 0
            paper.clear()
            events.append({"start_ts": int(r.start_ts), "event": "pause_environment"})

        if mode != "ACTIVE":
            recent.append(int(correct))
            normal_streak = normal_streak + 1 if suitable else 0
            if suitable:
                paper.append(int(correct))
            else:
                paper.clear()
            rolling_ok = len(recent) < a.rolling_window or np.mean(recent) >= a.rolling_floor
            if normal_streak >= a.normal_required and len(paper) == a.paper_window and sum(paper) >= a.paper_wins and rolling_ok:
                mode = "ACTIVE"
                loss_streak = 0
                events.append({"start_ts": int(r.start_ts), "event": "resume_full_size"})
            continue

        original_pre = max(1e-9, float(r.bankroll) - float(r.pnl))
        fraction = float(r.stake) / original_pre
        stake = bankroll * fraction
        pnl = stake * (float(r.pnl) / float(r.stake))
        bankroll += pnl
        peak = max(peak, bankroll)
        loss_streak = 0 if correct else loss_streak + 1
        recent.append(int(correct))
        rows.append({"start_ts": int(r.start_ts), "correct": correct, "stake": stake, "pnl": pnl, "bankroll": bankroll, "drawdown": bankroll - peak})

        if loss_streak >= a.loss_trigger:
            events.append({"start_ts": int(r.start_ts), "event": "review_after_losses"})
            # A loss cluster requests an assessment; it is not itself evidence
            # that the regime is broken. Pause only when the point-in-time
            # environment or the settled rolling hit-rate also fails.
            rolling_ok = len(recent) < a.rolling_window or np.mean(recent) >= a.rolling_floor
            if not suitable or not rolling_ok:
                mode = "REVIEW"
                normal_streak = 0
                paper.clear()

    out = pd.DataFrame(rows)
    profit = float(out.pnl.sum()) if len(out) else 0.0
    report = {
        "source_signals": int(len(t)), "trades": int(len(out)),
        "win_rate": float(out.correct.mean()) if len(out) else None,
        "net_profit": profit, "ending_bankroll": float(bankroll),
        "return_on_turnover": float(profit / out.stake.sum()) if len(out) else None,
        "max_drawdown_pct": float(((out.bankroll - out.bankroll.cummax()) / out.bankroll.cummax()).min()) if len(out) else None,
        "reviews_after_losses": sum(x["event"] == "review_after_losses" for x in events),
        "environment_pauses": sum(x["event"] == "pause_environment" for x in events),
        "full_size_resumes": sum(x["event"] == "resume_full_size" for x in events),
        "ending_mode": mode,
        "rules": {"loss_trigger": a.loss_trigger, "rolling_accuracy_floor": a.rolling_floor, "recovery": "3 suitable paper signals and 2/3 correct; resume full size"},
    }
    with open(a.out, "w", encoding="utf-8") as fh: json.dump(report, fh, ensure_ascii=False, indent=2)
    out.to_csv(a.out.replace(".json", "_trades.csv"), index=False)
    pd.DataFrame(events).to_csv(a.out.replace(".json", "_events.csv"), index=False)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__": main()
