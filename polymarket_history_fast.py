"""Download recurring Polymarket BTC 15m market outcomes and 1-minute price history.

The slug timestamp is the UTC window start. Gamma supplies exact resolution and
token ids; CLOB prices-history supplies the historical outcome price series.
"""
from __future__ import annotations
import argparse, json, time, threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import pandas as pd

GAMMA='https://gamma-api.polymarket.com/events'
HISTORY='https://clob.polymarket.com/prices-history'
TRADES='https://data-api.polymarket.com/trades'

def decode(x):
    if isinstance(x,str):
        try:return json.loads(x)
        except json.JSONDecodeError:return x
    return x

def market_for(start_ts, session):
    slug=f'btc-updown-15m-{int(start_ts)}'
    r=session.get(GAMMA,params={'slug':slug},timeout=20); r.raise_for_status(); events=r.json()
    if not events:return None
    e=events[0]; markets=e.get('markets') or []
    if not markets:return None
    m=markets[0]; outcomes=decode(m.get('outcomes',[])); tokens=decode(m.get('clobTokenIds',[])); final=decode(m.get('outcomePrices',[]))
    if len(outcomes)!=2 or len(tokens)!=2:return None
    by={str(o).lower():str(t) for o,t in zip(outcomes,tokens)}
    if 'up' not in by or 'down' not in by:return None
    winner=None
    if m.get('closed') and len(final)==2:
        fp=[float(x) for x in final]; winner=str(outcomes[int(np_argmax(fp))]).lower()
    return {'slug':slug,'start_ts':int(start_ts),'end_ts':int(start_ts)+900,'up_token':by['up'],'down_token':by['down'],
      'winner':winner,'closed':bool(m.get('closed')),'condition_id':m.get('conditionId'),'volume':float(m.get('volumeNum') or m.get('volume') or 0)}

def np_argmax(x): return max(range(len(x)),key=x.__getitem__)

def price_at(token,start,end,decision,session):
    r=session.get(HISTORY,params={'market':token,'startTs':start,'endTs':end,'fidelity':1},timeout=20); r.raise_for_status()
    hist=r.json().get('history',[]); pts=[(int(z['t']),float(z['p'])) for z in hist if int(z['t'])<=decision]
    return max(pts,key=lambda z:z[0])[1] if pts else None

def trade_prices_at(condition_id,up_token,down_token,start,end,decision,session):
    """Use public executed trades; returns last trade and local VWAP at decision."""
    rows=[]; offset=0
    while offset<=10000:
        r=session.get(TRADES,params={'market':condition_id,'limit':10000,'offset':offset},timeout=20); r.raise_for_status(); page=r.json()
        if not page:break
        rows.extend(page); offset+=len(page)
        if len(page)<10000:break
    def side(token):
        x=[]
        for z in rows:
            if str(z.get('asset'))!=str(token):continue
            ts=int(z['timestamp']);
            if start<=ts<=decision:x.append((ts,float(z['price']),float(z.get('size') or 0)))
        if not x:return (None,None)
        x.sort(); recent=[q for q in x if q[0]>=decision-60] or x[-20:]
        den=sum(q[2] for q in recent); vwap=sum(q[1]*q[2] for q in recent)/den if den else recent[-1][1]
        return x[-1][1],vwap
    ul,uv=side(up_token); dl,dv=side(down_token); return ul,dl,uv,dv

_local=threading.local()

def worker_session():
    if not hasattr(_local,'session'):
        s=requests.Session(); s.headers['User-Agent']='btc-consensus-research/3.2'
        retry=Retry(total=5,connect=5,read=5,backoff_factor=.35,status_forcelist=(429,500,502,503,504),allowed_methods=('GET',))
        s.mount('https://',HTTPAdapter(max_retries=retry,pool_connections=4,pool_maxsize=4))
        _local.session=s
    return _local.session

def collect_one(ts,decision_minute):
    s=worker_session()
    try:
        m=market_for(ts,s)
        if not m or not m['closed'] or not m['winner']:return None
        decision=ts+decision_minute*60
        ul,dl,uv,dv=trade_prices_at(m['condition_id'],m['up_token'],m['down_token'],ts,m['end_ts'],decision,s)
        up=ul if ul is not None else price_at(m['up_token'],ts,m['end_ts'],decision,s)
        down=dl if dl is not None else price_at(m['down_token'],ts,m['end_ts'],decision,s)
        return {**m,'decision_ts':decision,'up_price':up,'down_price':down,'up_vwap_60s':uv,'down_vwap_60s':dv,'price_source':'executed_trade' if ul is not None and dl is not None else 'minute_history_fallback','actual':1 if m['winner']=='up' else 0}
    except requests.RequestException as e:
        return {'slug':f'btc-updown-15m-{ts}','start_ts':ts,'error':str(e)}

def collect(anchor_ts,windows,decision_minute=5,workers=10):
    rows=[]
    first=anchor_ts-(windows-1)*900
    timestamps=list(range(first,anchor_ts+1,900))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures={pool.submit(collect_one,ts,decision_minute):ts for ts in timestamps}
        for k,f in enumerate(as_completed(futures),1):
            row=f.result()
            if row is not None:rows.append(row)
            if k%100==0:print(f'{k}/{windows} windows',flush=True)
    return pd.DataFrame(rows).sort_values('start_ts').reset_index(drop=True)

def main():
    p=argparse.ArgumentParser(); p.add_argument('--anchor-slug',default='btc-updown-15m-1784117700'); p.add_argument('--windows',type=int,default=2688,help='28 days at 96 windows/day'); p.add_argument('--decision-minute',type=int,default=5); p.add_argument('--workers',type=int,default=10); p.add_argument('--out',default='polymarket_btc15_history.csv'); a=p.parse_args()
    anchor=int(a.anchor_slug.rsplit('-',1)[1]); d=collect(anchor,a.windows,a.decision_minute,a.workers); d.to_csv(a.out,index=False)
    ok=d.dropna(subset=['actual','up_price','down_price']); print(f'wrote {len(d)} rows; {len(ok)} complete rows to {a.out}')
if __name__=='__main__': main()
