"""Discover the current Polymarket BTC 15m market and public CLOB books.

No wallet, API key, signature, or order capability is used.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests


def get_json(url: str, **params):
    response = requests.get(url, params=params, timeout=15, headers={"User-Agent": "btc15-shadow/1.0"})
    response.raise_for_status()
    return response.json()


def main() -> None:
    now = int(time.time())
    starts = [(now // 900 + offset) * 900 for offset in (-1, 0, 1)]
    found = None
    for start in starts:
        slug = f"btc-updown-15m-{start}"
        markets = get_json("https://gamma-api.polymarket.com/markets", slug=slug)
        if markets:
            found = markets[0]
            if start <= now < start + 900:
                break
    if not found:
        raise SystemExit("No current BTC 15m Polymarket slug found")

    raw_tokens = found.get("clobTokenIds", [])
    if isinstance(raw_tokens, str):
        raw_tokens = json.loads(raw_tokens)
    raw_outcomes = found.get("outcomes", [])
    if isinstance(raw_outcomes, str):
        raw_outcomes = json.loads(raw_outcomes)
    books = {}
    for outcome, token in zip(raw_outcomes, raw_tokens):
        books[str(outcome)] = get_json("https://clob.polymarket.com/book", token_id=token)
    result = {
        "mode": "PUBLIC_READ_ONLY",
        "slug": found.get("slug"),
        "condition_id": found.get("conditionId"),
        "end_date": found.get("endDate"),
        "outcomes": raw_outcomes,
        "token_ids": raw_tokens,
        "books": books,
        "orders_enabled": False,
    }
    Path("polymarket_public_snapshot.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
