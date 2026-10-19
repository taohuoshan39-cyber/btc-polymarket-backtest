"""Audit 24-month original 31-node predictions by historical market regime.
Strict out-of-sample status remains UNVERIFIED unless training code is audited.
No execution PNL or synthetic Polymarket odds.
"""
import csv, json, math
from pathlib import Path
from collections import defaultdict,Counter
from datetime import datetime,timezone
root=Path("two_year_regime");root.mkdir(exist_ok=True)
preds=list(csv.DictReader((root/"legacy_direction_predictions.csv").open()))
bars=list(csv.DictReader((root/"btc_15m_complete.csv").open()))
def tsparse(x):
    try:
        n=float(x);return int(n//1000 if n>1e12 else n)
    except (ValueError,TypeError):
        try:return int(datetime.fromisoformat(str(x).replace("Z","+00:00")).timestamp())
        except:return None
series={}
ordered=[]
for b in bars:
    t=tsparse(b["start_ts_ms"])
    if t is None:continue
    ordered.append((t,float(b["close"])))
ordered.sort()
closes={t:v for t,v in ordered}
times=[x[0] for x in ordered]
# Regime from strictly earlier closes (96 bars = 1 day; 672 bars = 7 days)
regimes={}
for i,(t,c) in enumerate(ordered):
    if i<672:continue
    ret7=c/ordered[i-672][1]-1
    recent=[math.log(ordered[k][1]/ordered[k-1][1]) for k in range(i-95,i+1)]
    vol=(sum(x*x for x in recent)/len(recent))**.5
    regimes[t]=(ret7,vol)
vals=sorted(x[1] for x in regimes.values())
threshold=vals[int(0.75*(len(vals)-1))] if vals else 0
def classify(ret,vol):
    direction="UPTREND" if ret>0.03 else ("DOWNTREND" if ret< -0.03 else "SIDEWAYS")
    return direction,("HIGH_VOL" if vol>threshold else "NORMAL_VOL")
agg=defaultdict(lambda:[0,0]); seen=0; bad=Counter(); byvote=defaultdict(lambda:[0,0])
for r in preds:
    t=tsparse(r.get("start_ts") or r.get("timestamp"))
    # Predictions are expected to refer to start of each 15m window.
    if t is None:bad["timestamp"]+=1;continue
    if t not in regimes:bad["missing_regime"]+=1;continue
    try:
        truth=int(float(r["actual"]));pick=int(float(r["prediction"]))
        votes=max(int(float(r["up_votes"])),int(float(r["down_votes"])))
        if truth not in (0,1) or pick not in (0,1):raise ValueError()
    except (ValueError,KeyError,TypeError):bad["labels_or_votes"]+=1;continue
    ret,vol=regimes[t]; trend,vtype=classify(ret,vol)
    for key in (("all","ALL"),("trend",trend),("volatility",vtype),("month",datetime.fromtimestamp(t,timezone.utc).strftime("%Y-%m")),("vote",str(votes)),("trend_vol",trend+"_"+vtype)):
        a=agg[key];a[0]+=int(pick==truth);a[1]+=1
    if votes>=28:
        a=byvote[(trend,vtype)];a[0]+=int(pick==truth);a[1]+=1
    seen+=1
def report(a):
    wins,n=a
    p=wins/n if n else None
    wilson=None
    if n:
        z=1.96;center=(p+z*z/(2*n))/(1+z*z/n)
        margin=z*((p*(1-p)/n+z*z/(4*n*n))**0.5)/(1+z*z/n)
        wilson=[round(center-margin,6),round(center+margin,6)]
    return {"wins":wins,"n":n,"accuracy":round(p,6) if p is not None else None,"wilson95":wilson}
out={"title":"BTC 24M original 31-node direction regime audit","regime_features":"7-day lookback return and 1-day realized volatility, trailing including latest historical close; 75th percentile volatility threshold estimated from full 24M distribution for descriptive analysis only","accepted_prediction_rows":seen,"unmatched":dict(bad),"threshold_28_regime":{str(k):report(v) for k,v in byvote.items()},"by_group":{f"{k[0]}:{k[1]}":report(v) for k,v in sorted(agg.items())},"validation_status":"NOT_PROVEN_OUT_OF_SAMPLE_MODEL_TRAINING_MUST_BE_AUDITED","pnl_status":"NOT_AVAILABLE_NO_HISTORICAL_EXECUTABLE_QUOTES","champion_10829_7301":"NOT_REPRODUCED_MISSING_ORIGINAL_SOURCE"}
(root/"REGIME_AUDIT.json").write_text(json.dumps(out,indent=2))
with (root/"regime_metrics.csv").open("w",newline="") as f:
    w=csv.writer(f);w.writerow(["type","bucket","wins","signals","accuracy","wilson_low","wilson_high"])
    for (typ,bucket),val in sorted(agg.items()):
        p=report(val);w.writerow([typ,bucket,p["wins"],p["n"],p["accuracy"],*(p["wilson95"] or [None,None])])
print(json.dumps({"accepted_predictions":seen,"counts":dict(bad),"all":out["by_group"].get("all:ALL"),"threshold_28_regime":out["threshold_28_regime"],"validation":out["validation_status"]},indent=2))
