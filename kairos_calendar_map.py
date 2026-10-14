"""Closed calendar-year OHLCV built from complete native UTC month candles."""
from datetime import datetime,timezone
import math

def closed_calendar_years(monthly, cutoff_ts):
    cutoff=datetime.fromtimestamp(cutoff_ts/1000,timezone.utc)
    grouped={}
    for c in monthly:
        d=datetime.fromtimestamp(c['t']/1000,timezone.utc)
        grouped.setdefault(d.year,[]).append((d,c))
    years=[]
    for year,rows in sorted(grouped.items()):
        end=int(datetime(year+1,1,1,tzinfo=timezone.utc).timestamp()*1000)
        if end>cutoff_ts or len(rows)!=12:
            continue
        rows.sort(key=lambda r:r[0])
        if [d.month for d,c in rows]!=list(range(1,13)):
            continue
        if any(d.day!=1 or d.hour or d.minute or d.second or d.microsecond for d,c in rows):
            continue
        cs=[c for d,c in rows]
        if any(not all(math.isfinite(float(c[k])) for k in ('o','h','l','c')) or c['h']<max(c['o'],c['c'],c['l']) or c['l']>min(c['o'],c['c'],c['h']) for c in cs):
            continue
        years.append(dict(t=cs[0]['t'],o=cs[0]['o'],h=max(c['h'] for c in cs),
            l=min(c['l'] for c in cs),c=cs[-1]['c'],v=sum(c.get('v',0) or 0 for c in cs),
            close_ts=end,source_months=[c['t'] for c in cs]))
    # A gap cannot be hidden by concatenating nonadjacent years into a FVG.
    if not years or years[-1]['close_ts']!=int(datetime(cutoff.year,1,1,tzinfo=timezone.utc).timestamp()*1000):
        return []
    start=len(years)-1
    while start>0 and years[start-1]['close_ts']==years[start]['t']:
        start-=1
    return years[start:]
