"""Two-year 1m Binance spot data assembler. STRICT DATA ONLY."""
import csv, io, json, hashlib, urllib.request, zipfile
from pathlib import Path
from datetime import datetime,timezone
out=Path("two_year_model_data");out.mkdir(exist_ok=True)
months=[f"{y}-{m:02d}" for y in range(2024,2027) for m in range(1,13) if "2024-10"<=f"{y}-{m:02d}"<="2026-09"]
dest=out/"BTCUSDT_1m.csv"; manifest=[]; previous=None;total=0; missing=0; disorder=0
with dest.open("w",newline="") as f:
    w=csv.writer(f);w.writerow(["timestamp","open","high","low","close","volume"])
    for month in months:
        target=out/f"BTCUSDT-1m-{month}.zip"
        url=f"https://data.binance.vision/data/spot/monthly/klines/BTCUSDT/1m/BTCUSDT-1m-{month}.zip"
        entry={"month":month,"source":url}
        try:
            if not target.exists():
                with urllib.request.urlopen(urllib.request.Request(url,headers={"User-Agent":"Mozilla/5.0"}),timeout=180) as r, target.open("wb") as z:
                    while (chunk:=r.read(1<<20)):z.write(chunk)
            entry["sha256"]=hashlib.file_digest(target.open("rb"),"sha256").hexdigest()
            count=0
            with zipfile.ZipFile(target) as z:
                with z.open(z.namelist()[0]) as g:
                    for row in csv.reader(io.TextIOWrapper(g,encoding="utf8")):
                        if len(row)<6:continue
                        try:
                            ts=int(row[0])
                            if ts>10**14:ts//=1000
                            stamp=(ts//60000)*60000
                            o,h,l,c,v=map(float,row[1:6])
                            assert min(o,h,l,c)>0 and l<=min(o,h,c)<=max(o,h,c)<=h and v>=0
                            if previous is not None:
                                if stamp<=previous:disorder+=1;continue
                                if stamp>previous+60000:missing+=(stamp-previous)//60000-1
                            previous=stamp
                            w.writerow([stamp,o,h,l,c,v]);count+=1;total+=1
                        except Exception as exc:
                            entry["parse_errors"]=entry.get("parse_errors",0)+1
            entry.update(status="ok",rows=count)
        except Exception as exc:
            entry.update(status="failed",error=str(exc))
        manifest.append(entry)
        (out/"manifest.json").write_text(json.dumps(manifest,indent=2))
        print(month,entry.get("rows"),entry.get("status"),flush=True)
summary={"expected_rows":730*1440,"downloaded_rows":total,"missing_minutes":missing,"out_of_order_or_duplicates":disorder,"months_ok":sum(x["status"]=="ok" for x in manifest),"quality_pass":total==730*1440 and missing==0 and disorder==0}
(out/"quality.json").write_text(json.dumps(summary,indent=2))
print(summary,flush=True)
if not summary["quality_pass"]:raise SystemExit(2)
