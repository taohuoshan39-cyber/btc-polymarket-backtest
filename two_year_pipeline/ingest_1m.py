"""24-month point-in-time BTC 1-minute public data ingestion.
DATA_PIPELINE only. No ML or trade/PNL claims."""
import csv, io, json, hashlib, os, sys, zipfile, urllib.request
from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict
ROOT=Path("two_year_data"); ROOT.mkdir(exist_ok=True)
START="2024-10"; END="2026-09"
months=[f"{y:04d}-{m:02d}" for y in range(2024,2027) for m in range(1,13) if START<=f"{y:04d}-{m:02d}"<=END]
manifest=[]
all_15=[]
for month in months:
    url=f"https://data.binance.vision/data/spot/monthly/klines/BTCUSDT/1m/BTCUSDT-1m-{month}.zip"
    target=ROOT/f"BTCUSDT-1m-{month}.zip"
    entry={"month":month,"url":url,"source":"binance_public_spot_1m"}
    try:
        if not target.exists():
            req=urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=180) as response, open(target,"wb") as out:
                while True:
                    chunk=response.read(1024*1024)
                    if not chunk: break
                    out.write(chunk)
        entry["bytes"]=target.stat().st_size
        entry["sha256"]=hashlib.sha256(target.read_bytes()).hexdigest()
        with zipfile.ZipFile(target) as z:
            with z.open(z.namelist()[0]) as src:
                reader=csv.reader(io.TextIOWrapper(src,encoding="utf8"))
                raw=0; duplicate=0; bad=0; minute_gaps=0; previous=None; grouped={}
                for rec in reader:
                    if len(rec)<6: continue
                    try:
                        stamp=int(rec[0]); stamp=stamp//1000 if stamp>10**14 else stamp
                        op,hi,lo,cl,vol=map(float,rec[1:6])
                        if min(op,hi,lo,cl)<=0 or hi<max(op,cl,lo) or lo>min(op,cl,hi) or vol<0:
                            bad+=1;continue
                    except (ValueError,IndexError): bad+=1;continue
                    if previous is not None:
                        if stamp<=previous: duplicate+=1
                        if stamp>previous+60000: minute_gaps+=(stamp-previous)//60000-1
                    previous=stamp;raw+=1
                    window=(stamp//900000)*900000
                    if window not in grouped: grouped[window]=[op,hi,lo,cl,vol,1]
                    else:
                        q=grouped[window];q[1]=max(q[1],hi);q[2]=min(q[2],lo);q[3]=cl;q[4]+=vol;q[5]+=1
                for ts,values in sorted(grouped.items()):
                    if values[5]==15: all_15.append([ts]+values)
                entry.update(status="ok",rows=raw,invalid=bad,duplicates=duplicate,missing_minutes=minute_gaps,complete_15m=sum(v[5]==15 for v in grouped.values()),partial_15m=sum(v[5]!=15 for v in grouped.values()))
        print(f"{month}: rows={raw} gaps={minute_gaps} 15m={entry['complete_15m']}",flush=True)
    except Exception as e:
        entry.update(status="failed",error=str(e));print(f"{month} FAILED: {e}",flush=True)
    manifest.append(entry)
    (ROOT/"data_manifest.json").write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
    # Persist a usable partial checkpoint after every month
    with open(ROOT/"btc_15m_complete.csv","w",newline="") as f:
        writer=csv.writer(f);writer.writerow(["start_ts_ms","open","high","low","close","volume","minute_count"]);writer.writerows(all_15)
expected_15m=730*96;actual=len(all_15);good=all(z.get("status")=="ok" and z.get("missing_minutes")==0 and z.get("invalid")==0 for z in manifest)
summary={"period_utc":"2024-10-01 to 2026-10-01 exclusive","expected_1m":730*1440,"expected_15m":expected_15m,"complete_15m":actual,"coverage_15m":actual/expected_15m,"months_ok":sum(x.get("status")=="ok" for x in manifest),"months_total":len(manifest),"data_quality_pass":bool(good and actual==expected_15m),"model_validation":"NOT_RUN_MISSING_TWO_ORIGINAL_MODEL_SOURCE","economic_backtest":"NOT_RUN_MISSING_POINT_IN_TIME_QUOTES"}
(ROOT/"summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2))
print(json.dumps(summary,ensure_ascii=False,indent=2))
