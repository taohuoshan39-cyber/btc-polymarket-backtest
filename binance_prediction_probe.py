"""Read-only Binance Prediction Trading market discovery probe.

This program cannot place orders.  It signs Binance Prediction REST requests,
finds live BTC 15-minute Up/Down topics, and writes an auditable JSON snapshot.
Secrets are read only from the process environment.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo


BASE_URL = os.getenv("BINANCE_BASE_URL", "https://api.binance.com").rstrip("/")
API_KEY = os.getenv("BINANCE_API_KEY", "")
API_SECRET = os.getenv("BINANCE_API_SECRET", "")


def signed_get(path: str, base_url: str = BASE_URL, **params: Any) -> Any:
    if not API_KEY or not API_SECRET:
        raise RuntimeError("missing BINANCE_API_KEY or BINANCE_API_SECRET")
    params["timestamp"] = int(time.time() * 1000)
    params.setdefault("recvWindow", 5000)
    query = urllib.parse.urlencode(params)
    signature = hmac.new(API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
    request = urllib.request.Request(
        f"{base_url}{path}?{query}&signature={signature}",
        headers={"X-MBX-APIKEY": API_KEY, "User-Agent": "btc-15m-shadow-probe/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        # The body is safe to retain: Binance errors do not echo the secret or
        # signature. Never retain the full request URL/query string.
        raise RuntimeError(f"HTTP {exc.code}: {body[:300]}") from exc


def objects(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from objects(child)


def text_of(item: dict[str, Any]) -> str:
    keys = ("title", "name", "question", "marketName", "topicName", "symbol", "description")
    return " ".join(str(item.get(k, "")) for k in keys).lower()


def is_btc_15m(item: dict[str, Any]) -> bool:
    text = text_of(item).replace("分钟", " minute ")
    btc = "btc" in text or "bitcoin" in text or "比特币" in text
    fifteen = any(x in text for x in ("15m", "15 min", "15-minute", "15 minute"))
    updown = any(x in text for x in ("up", "down", "涨", "跌"))
    return btc and fifteen and updown


def main() -> None:
    # Official docs publish several equivalent API hosts.  Probe the configured
    # host first and fall back only for endpoint/rollout diagnostics.
    hosts = list(dict.fromkeys([BASE_URL, "https://api.binance.com", "https://api1.binance.com", "https://api-gcp.binance.com"]))
    calls = [
        ("category_list", "/sapi/v1/w3w/wallet/prediction/category/list", {}),
        ("market_list", "/sapi/v1/w3w/wallet/prediction/market/list", {
            "l1Category": "crypto", "l2Category": "up-down", "sortBy": "END_DATE",
            "orderBy": "ASC", "offset": 0, "limit": 100,
        }),
        ("market_search", "/sapi/v1/w3w/wallet/prediction/market/search", {
            "query": "BTC 15 minute up down", "topK": 50,
        }),
    ]
    diagnostics: list[dict[str, Any]] = []
    responses: list[tuple[str, str, Any]] = []
    for host in hosts:
        for name, path, params in calls:
            try:
                data = signed_get(path, base_url=host, **params)
                diagnostics.append({"host": host, "call": name, "ok": True})
                responses.append((host, name, data))
            except Exception as exc:  # retain only the sanitized status/body
                diagnostics.append({"host": host, "call": name, "ok": False, "error": str(exc)})
        if any(h == host for h, _, _ in responses):
            break

    raw = responses[-1][2] if responses else None
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for _, _, response in responses:
      for item in objects(response):
        if not is_btc_15m(item):
            continue
        identity = str(item.get("marketTopicId") or item.get("marketId") or item.get("id") or item)
        if identity in seen:
            continue
        seen.add(identity)
        candidates.append(item)

    snapshot = {
        "mode": "SHADOW_READ_ONLY",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "generated_at_beijing": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "endpoint": BASE_URL,
        "diagnostics": diagnostics,
        "candidate_count": len(candidates),
        "candidates": candidates,
        "raw_response": raw if responses and not candidates else None,
        "orders_enabled": False,
    }
    with open("binance_prediction_snapshot.json", "w", encoding="utf-8") as fh:
        json.dump(snapshot, fh, ensure_ascii=False, indent=2)
    print(json.dumps(snapshot, ensure_ascii=False, indent=2))
    if not responses:
        print("Prediction SAPI unavailable. Inspect diagnostics for 404/-2015/permission/region status.", file=sys.stderr)
        raise SystemExit(3)
    if not candidates:
        print("Prediction SAPI works, but no BTC 15m candidate was returned.", file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
