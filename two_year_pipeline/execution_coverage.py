"""Audit real historical BUY-ask availability; never fabricate a price.
This is a quote-coverage gate, NOT a profitable 24M backtest."""
import csv,json,urllib.request,urllib.parse,time,collections
from datetime import datetime,timezone
from pathlib import Path
out=Path("two_year_execution");out.mkdir(exist_ok=True)
START=int(datetime(2024,10,1,tzinfo=timezone.utc).timestamp())
END=int(datetime(2026,10,1,tzinfo=timezone.utc).timestamp())
def get(url,params):
    full=url+"?"+urllib.parse.urlencode(params)
    last=None
    for attempt in range(3):
        try:
            req=urllib.request.Request(full,headers={"User-Agent":"btc15m-research-audit/1.0"})
            with urllib.request.urlopen(req,timeout=25) as r:return json.load(r)
        except Exception as e:last=e;time.sleep(min(5,1+attempt*2))
    raise RuntimeError(str(last))
rows=[];errors=[]
# Representative historical markets; use full 24M timeline for coverage assessment.
# Do not mistake price history for executable asks.
for month in range(24):
    y=2024+(9+month)//12;m=(9+month)%12+1
    dt=datetime(y,m,15,12,0,tzinfo=timezone.utc)
    ts=int(dt.timestamp())//900*900
    slug=f"btc-updown-15m-{ts}"
    record={"month":f"{y}-{m:02d}","start_ts":ts,"slug":slug,
            "up_buy_ask":"UNAVAILABLE","down_buy_ask":"UNAVAILABLE",
            "orderbook_at_decision":"UNAVAILABLE","fee_at_decision":"UNAVAILABLE"}
    try:
        events=get("https://gamma-api.polymarket.com/events",{"slug":slug})
        record["market_found"]=bool(events)
        if events and events[0].get("markets"):
            market=events[0]["markets"][0]
            record["condition_id"]=market.get("conditionId")
            record["closed"]=market.get("closed")
            record["token_ids"]=market.get("clobTokenIds")
            record["historical_trades_not_quote"]=True
    except Exception as e:
        record["error"]=str(e);errors.append({"slug":slug,"error":str(e)})
    rows.append(record)
    (out/"checkpoint.json").write_text(json.dumps({"checked":len(rows),"total_months":24,"rows":rows},indent=2))
    print(slug,"found",record.get("market_found"),"error",record.get("error",""),flush=True)
with (out/"monthly_quote_coverage.csv").open("w",newline="") as f:
    fieldnames=["month","start_ts","slug","market_found","condition_id","closed","up_buy_ask","down_buy_ask","orderbook_at_decision","fee_at_decision","error"]
    w=csv.DictWriter(f,fieldnames=fieldnames,extrasaction="ignore");w.writeheader();w.writerows(rows)
summary={"months_checked":len(rows),"market_slugs_found":sum(bool(r.get("market_found")) for r in rows),
"months_with_verified_historical_two_sided_buy_asks":0,
"executable_24m_pnl":"NOT_CALCULATED",
"reason":"Public historical trade/price-history APIs do not establish point-in-time executable two-sided asks and depth; no archived quote snapshots supplied.",
"legacy_original_31node_direction":"ALREADY_REPLAYED_SEPARATELY",
"strategy_stable2517":"REQUIRES_HISTORICAL_BUY_QUOTES_FOR_EXECUTABLE_BACKTEST",
"notional_fixed_stake":"10U","compound_comparison":"NOT_CALCULATED_WITHOUT_QUOTES",
"network_errors":len(errors)}
(out/"coverage_summary.json").write_text(json.dumps(summary,indent=2))
print(json.dumps(summary,indent=2))
