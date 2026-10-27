"""Read-only archived native candles -> FVG strength observations.

This is a structural/FVG comparison, NOT a liquidity-qualified entry replay.
No database, Telegram, provider scan or exchange calls. All cutoffs are closed.
"""
import argparse
import copy
import hashlib
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from kairos_fvg_force_shadow import assess_fvg
from kairos_ludo_structure import structure_snapshot
import scalp_engine as engine


def replay(source,start,end,timeframes=('M5','M15','M30')):
    raw=source['candles']
    digest=hashlib.sha256(json.dumps(raw,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    if digest!=source['sha256']:
        raise ValueError('Native archive hash mismatch')
    observations=[];coverage={}
    for tf in timeframes:
        d={'M5':300000,'M15':900000,'M30':1800000}[tf]
        rows=[r for r in raw.get(tf,[]) if r['t']+d<=end]
        gaps=sum(b['t']-a['t']!=d for a,b in zip(rows,rows[1:]))
        coverage[tf]=dict(count=len(rows),gaps=gaps,first_open=rows[0]['t'] if rows else None,
                          last_close=rows[-1]['t']+d if rows else None)
        if gaps or not rows:continue
        events=structure_snapshot(rows,d,end)['events']
        for e in events:
            if not start<=e['close_ts']<=end:continue
            ready=min(e['close_ts']+d,end)
            prefix=[r for r in rows if r['t']+d<=ready]
            zones=engine._kairos_fvg_states(prefix,lookback=len(prefix))
            results=[]
            for zone in zones:
                a=zone.get('source_a') or {};c=zone.get('source_c') or {}
                if (zone.get('lux_fvg') is not True or not zone.get('tipo','').startswith('FVG_')
                    or a.get('t',-1)<e['opposite_origin_ts'] or c.get('t',ready)+d>ready):continue
                r=assess_fvg(prefix,zone,e,d,ready)
                if r.get('geometry_valid') and (r.get('direct_break') or r.get('inside_causal_leg')):
                    results.append(dict(r,zone_id=zone.get('id'),bottom=zone['bottom'],top=zone['top']))
            observations.append(dict(tf=tf,event=copy.deepcopy(e),observed_cutoff=ready,
                                     fvgs=results,liquidity_qualification='NOT_EVALUATED',
                                     entry_decision_changed=False))
    return dict(mode='OFFLINE_OBSERVATION_ONLY',source_sha256=digest,start_ts=start,end_ts=end,
                coverage=coverage,observations=observations,
                method='Native Ludo sequential closed bars; native Lux FVG detection on closed prefix through next candle. Experimental direct-middle-candle criteria only; no liquidity qualification, entry filtering, fills or profitability claim.')

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('source');p.add_argument('output')
    p.add_argument('--start',type=int,required=True);p.add_argument('--end',type=int,required=True)
    args=p.parse_args()
    result=replay(json.loads(Path(args.source).read_text()),args.start,args.end)
    Path(args.output).write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print(json.dumps(dict(coverage=result['coverage'],events=len(result['observations']),
        direct_fvgs=sum(r['direct_break'] for o in result['observations'] for r in o['fvgs']),
        experimental_candidates=sum(r['experimental_candidate'] is True for o in result['observations'] for r in o['fvgs']))))
