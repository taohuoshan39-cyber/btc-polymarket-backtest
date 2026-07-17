"""Create a secret-free Beijing-time summary from a shadow/live trade ledger."""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd


def pick(frame: pd.DataFrame, *names: str):
    for name in names:
        if name in frame:
            return frame[name]
    return pd.Series(index=frame.index, dtype=float)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--trades", default="shadow_trades.csv")
    p.add_argument("--out", default="daily_summary.json")
    a = p.parse_args()
    path = Path(a.trades)
    frame = pd.read_csv(path) if path.exists() and path.stat().st_size else pd.DataFrame()
    correct = pick(frame, "correct", "won", "is_correct")
    pnl = pd.to_numeric(pick(frame, "pnl", "profit", "net_pnl"), errors="coerce")
    stake = pd.to_numeric(pick(frame, "stake", "stake_usdt", "notional"), errors="coerce")
    mode = pick(frame, "mode", "trading_mode")
    summary = {
        "generated_at_beijing": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "source": a.trades,
        "signals": int(len(frame)),
        "settled": int(correct.notna().sum()),
        "wins": int(correct.fillna(False).astype(bool).sum()) if len(correct) else 0,
        "win_rate": float(correct.dropna().astype(bool).mean()) if correct.notna().any() else None,
        "net_pnl_usdt": float(pnl.fillna(0).sum()) if len(pnl) else 0.0,
        "turnover_usdt": float(stake.fillna(0).sum()) if len(stake) else 0.0,
        "shadow_signals": int(mode.astype(str).str.upper().str.contains("SHADOW").sum()) if len(mode) else int(len(frame)),
    }
    Path(a.out).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
