# Kairos Radar V2.1 — core causal multi-timeframe
# Clean audit build: no legacy V1 evaluator paths, no duplicate function definitions.
# Execution only on M15/M5/M1. Context/liquidity map: W1..M1.

KAIROS_ICT_DISPLACEMENT_ATR_MIN = 1.05
MIN_FVG_GAP_PCT = 0.0005
STOP_BUFFER_PCT = 0.001
SWING_LOOKBACK = 5
ATR_BUFFER_MULT = 0.25
MSS_CORPO_MIN_PCT = 0.5

KAIROS_EXEC_TFS = ('M15', 'M5', 'M1')
KAIROS_CONTEXT_TFS = ('W1', 'D1', 'H4', 'H1', 'M30', 'M15', 'M5', 'M1')
KAIROS_MIN_RR_OPERACIONAL = 2.0
KAIROS_SWEEP_MAX_AGE_BARS = {'M15': 24, 'M5': 48, 'M1': 90}
KAIROS_POI_MAX_AGE_BARS = {'M15': 16, 'M5': 30, 'M1': 60}


def detect_exec_swings(exec_candles, lookback=SWING_LOOKBACK):
    swings = []
    for i in range(lookback, len(exec_candles) - lookback):
        c = exec_candles[i]
        window = exec_candles[i - lookback:i + lookback + 1]
        if all(c['h'] >= o['h'] for o in window if o is not c):
            swings.append({'index': i, 'tipo': 'high', 'valor': c['h'], 't': c['t']})
        if all(c['l'] <= o['l'] for o in window if o is not c):
            swings.append({'index': i, 'tipo': 'low', 'valor': c['l'], 't': c['t']})
    return swings

def compute_atr(candles, period=14):
    n = len(candles)
    if n < period + 1:
        return [None] * n
    tr = [None] * n
    for i in range(1, n):
        h, l, prev_c = candles[i]['h'], candles[i]['l'], candles[i - 1]['c']
        tr[i] = max(h - l, abs(h - prev_c), abs(l - prev_c))

    atr = [None] * n
    primeiros_tr = [tr[i] for i in range(1, period + 1)]
    atr[period] = sum(primeiros_tr) / period
    for i in range(period + 1, n):
        atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period
    return atr

def compute_bias_from_swings(candles, lookback=SWING_LOOKBACK):
    if not candles or len(candles) < (lookback * 2 + 5):
        return 'neutro'
    swings = detect_exec_swings(candles, lookback=lookback)
    highs = [s for s in swings if s['tipo'] == 'high'][-2:]
    lows = [s for s in swings if s['tipo'] == 'low'][-2:]
    if len(highs) == 2 and len(lows) == 2:
        topos_sobem = highs[1]['valor'] > highs[0]['valor']
        fundos_sobem = lows[1]['valor'] > lows[0]['valor']
        topos_descem = highs[1]['valor'] < highs[0]['valor']
        fundos_descem = lows[1]['valor'] < lows[0]['valor']
        if topos_sobem and fundos_sobem:
            return 'alta'
        if topos_descem and fundos_descem:
            return 'baixa'
    return 'neutro'

def detect_exec_swings_causal(candles, lookback=3):
    """Pivot + momento em que ele se tornou conhecível (sem lookahead)."""
    out=[]
    for i in range(lookback, len(candles)-lookback):
        c=candles[i]
        left=candles[i-lookback:i]; right=candles[i+1:i+lookback+1]
        confirmed_at=right[-1]['t']
        if all(c['h'] > x['h'] for x in left+right):
            out.append({'tipo':'high','valor':c['h'],'index':i,'pivot_ts':c['t'],'confirmed_at':confirmed_at})
        if all(c['l'] < x['l'] for x in left+right):
            out.append({'tipo':'low','valor':c['l'],'index':i,'pivot_ts':c['t'],'confirmed_at':confirmed_at})
    return out

def find_equal_highs_lows_causal(candles, length=3, atr_mult=0.10):
    """EQ só existe depois do segundo pivot confirmado."""
    atr=compute_atr(candles,14)
    atr_now=next((x for x in reversed(atr) if x),None)
    if not atr_now: return []
    swings=detect_exec_swings_causal(candles,length)
    groups=[]
    for s in swings:
        matched=None
        for g in groups:
            if g['pivot_tipo']==s['tipo'] and abs(s['valor']-g['nivel']) <= atr_mult*atr_now:
                matched=g; break
        if matched:
            matched['pontos'].append(s)
            matched['nivel']=sum(x['valor'] for x in matched['pontos'])/len(matched['pontos'])
            matched['confirmed_at']=max(x['confirmed_at'] for x in matched['pontos'])
        else:
            groups.append({'pivot_tipo':s['tipo'],'nivel':s['valor'],'pontos':[s], 'confirmed_at':s['confirmed_at']})
    return [{'tipo':'EQH' if g['pivot_tipo']=='high' else 'EQL','nivel':g['nivel'],
             'toques':len(g['pontos']),'confirmed_at':g['confirmed_at'],
             'pivot_ts':g['pontos'][-1]['pivot_ts']}
            for g in groups if len(g['pontos'])>=2]

def detect_fvgs_causal(candles, min_gap_pct=MIN_FVG_GAP_PCT):
    out=[]
    for i in range(2,len(candles)):
        a,b,c=candles[i-2],candles[i-1],candles[i]
        if c['l'] > a['h']:
            gap=(c['l']-a['h'])/a['h'] if a['h'] else 0
            if gap>=min_gap_pct:
                out.append({'tipo':'FVG','direcao':'alta','bottom':a['h'],'top':c['l'],
                            'origin_ts':b['t'],'confirmed_at':c['t'],'index':i})
        if c['h'] < a['l']:
            gap=(a['l']-c['h'])/a['l'] if a['l'] else 0
            if gap>=min_gap_pct:
                out.append({'tipo':'FVG','direcao':'baixa','bottom':c['h'],'top':a['l'],
                            'origin_ts':b['t'],'confirmed_at':c['t'],'index':i})
    return out

def detect_ifvgs_causal(candles):
    """FVG invertido somente após close atravessar a borda oposta."""
    out=[]
    for f in detect_fvgs_causal(candles):
        for j in range(f['index']+1,len(candles)):
            c=candles[j]
            if f['direcao']=='alta' and c['c'] < f['bottom']:
                out.append({**f,'tipo':'IFVG','direcao':'baixa','parent_fvg_confirmed_at':f['confirmed_at'],
                            'confirmed_at':c['t'],'index':j}); break
            if f['direcao']=='baixa' and c['c'] > f['top']:
                out.append({**f,'tipo':'IFVG','direcao':'alta','parent_fvg_confirmed_at':f['confirmed_at'],
                            'confirmed_at':c['t'],'index':j}); break
    return out

def _zone_state(zone,candles):
    post=[c for c in candles if c['t']>zone['confirmed_at']]
    if not post: return 'OPEN'
    if zone['direcao']=='alta' and any(c['c'] < zone['bottom'] for c in post): return 'INVALIDATED'
    if zone['direcao']=='baixa' and any(c['c'] > zone['top'] for c in post): return 'INVALIDATED'
    if any(c['h']>=zone['bottom'] and c['l']<=zone['top'] for c in post): return 'MITIGATED'
    return 'OPEN'

def detect_order_blocks_causal(candles):
    out=[]
    if len(candles)<3: return out
    bodies=[abs(c['c']-c['o']) for c in candles]
    avg=sum(bodies)/len(bodies) if bodies else 0
    for i in range(len(candles)-1):
        c,n=candles[i],candles[i+1]
        if avg<=0 or abs(n['c']-n['o']) < avg*1.5: continue
        if c['c']<c['o'] and n['c']>n['o']:
            out.append({'tipo':'OB','direcao':'alta','bottom':min(c['o'],c['c']),'top':max(c['o'],c['c']),
                        'origin_ts':c['t'],'confirmed_at':n['t'],'index':i})
        elif c['c']>c['o'] and n['c']<n['o']:
            out.append({'tipo':'OB','direcao':'baixa','bottom':min(c['o'],c['c']),'top':max(c['o'],c['c']),
                        'origin_ts':c['t'],'confirmed_at':n['t'],'index':i})
    return out

def detect_breakers_causal(candles):
    out=[]
    for ob in detect_order_blocks_causal(candles):
        for j,c in enumerate(candles):
            if c['t']<=ob['confirmed_at']: continue
            # Falha do OB transforma a mesma faixa em breaker do lado oposto.
            if ob['direcao']=='alta' and c['c']<ob['bottom']:
                out.append({**ob,'tipo':'BREAKER','direcao':'baixa','parent_ob_confirmed_at':ob['confirmed_at'],
                            'confirmed_at':c['t'],'index':j}); break
            if ob['direcao']=='baixa' and c['c']>ob['top']:
                out.append({**ob,'tipo':'BREAKER','direcao':'alta','parent_ob_confirmed_at':ob['confirmed_at'],
                            'confirmed_at':c['t'],'index':j}); break
    return out


def _liquidity_capture_state(candles, side, level, confirmed_at):
    """Returns AVAILABLE/CAPTURED using only candles strictly after confirmed_at."""
    for c in candles:
        if c['t'] <= confirmed_at:
            continue
        crossed = (c['h'] > level) if side == 'BSL' else (c['l'] < level)
        if crossed:
            return {'state': 'CAPTURED', 'captured_at': c['t']}
    return {'state': 'AVAILABLE', 'captured_at': None}


def map_market_objects(candles_por_tf):
    """Causal map of liquidity + PD arrays. Liquidity carries availability state."""
    market={'liquidity':[],'zones':[]}
    for tf in KAIROS_CONTEXT_TFS:
        cs=candles_por_tf.get(tf) or []
        if len(cs)<9:
            continue
        lb=2 if tf in ('M1','M5') else 3
        for s in detect_exec_swings_causal(cs,lb):
            side='BSL' if s['tipo']=='high' else 'SSL'
            st=_liquidity_capture_state(cs,side,float(s['valor']),s['confirmed_at'])
            market['liquidity'].append({
                'tf':tf,
                'kind':'SWING_HIGH' if side=='BSL' else 'SWING_LOW',
                'side':side,
                'level':float(s['valor']),
                'pivot_ts':s['pivot_ts'],
                'confirmed_at':s['confirmed_at'],
                **st,
            })
        for e in find_equal_highs_lows_causal(cs,lb):
            side='BSL' if e['tipo']=='EQH' else 'SSL'
            st=_liquidity_capture_state(cs,side,float(e['nivel']),e['confirmed_at'])
            market['liquidity'].append({
                'tf':tf,
                'kind':e['tipo'],
                'side':side,
                'level':float(e['nivel']),
                'pivot_ts':e['pivot_ts'],
                'confirmed_at':e['confirmed_at'],
                **st,
            })
        zones=(detect_fvgs_causal(cs)+detect_ifvgs_causal(cs)+
               detect_order_blocks_causal(cs)+detect_breakers_causal(cs))
        for z in zones:
            market['zones'].append({**z,'tf':tf,'state':_zone_state(z,cs),'role':'CONTEXT'})
    return market


def _classify_context(candles_por_tf, direction):
    """Contexto informa; não bloqueia countertrend/pullback."""
    votes=[]
    for tf in ('W1','D1','H4','H1'):
        cs=candles_por_tf.get(tf) or []
        if len(cs)>=15:
            b=compute_bias_from_swings(cs,lookback=min(3,max(2,(len(cs)-1)//3)))
            if b!='neutro': votes.append((tf,b))
    wanted='alta' if direction=='LONG' else 'baixa'
    aligned=sum(1 for _,b in votes if b==wanted); opposed=sum(1 for _,b in votes if b!=wanted)
    if aligned>opposed: cls='CONTINUATION'
    elif opposed>aligned: cls='PULLBACK_COUNTERTREND'
    else: cls='NEUTRAL_TRANSITION'
    return {'class':cls,'votes':votes}


def _find_recent_sweep(exec_candles, liq, direction, max_age_bars):
    """First valid capture after liquidity confirmation; strict confirmed_at < sweep_ts."""
    if not exec_candles:
        return None
    start_ts=liq['confirmed_at']
    for idx,c in enumerate(exec_candles):
        if c['t'] <= start_ts:
            continue
        if direction=='LONG':
            if c['c'] < liq['level']:
                return None
            if c['l'] < liq['level'] and c['c'] > liq['level']:
                bars_old=len(exec_candles)-1-idx
                return ({'t':c['t'],'extreme':c['l'],'level':liq['level'],'index_global':idx}
                        if bars_old<=max_age_bars else None)
        else:
            if c['c'] > liq['level']:
                return None
            if c['h'] > liq['level'] and c['c'] < liq['level']:
                bars_old=len(exec_candles)-1-idx
                return ({'t':c['t'],'extreme':c['h'],'level':liq['level'],'index_global':idx}
                        if bars_old<=max_age_bars else None)
    return None


def _find_structure_break_after_sweep(exec_candles,sweep,direction):
    post_start=sweep['index_global']+1
    post=exec_candles[post_start:]
    if len(post)<7:
        return None
    swings=detect_exec_swings_causal(post,2)
    wanted='high' if direction=='LONG' else 'low'
    refs=[s for s in swings if s['tipo']==wanted]
    for ref in refs:
        for local_idx,c in enumerate(post):
            if c['t']<=ref['confirmed_at']:
                continue
            rng=c['h']-c['l']; body=abs(c['c']-c['o'])
            if rng<=0 or body/rng<MSS_CORPO_MIN_PCT:
                continue
            broke=(c['c']>ref['valor']) if direction=='LONG' else (c['c']<ref['valor'])
            if not broke:
                continue
            global_idx=post_start+local_idx
            upto=exec_candles[:global_idx+1]
            atr=next((x for x in reversed(compute_atr(upto,14)) if x),None)
            disp=(rng/atr) if atr else 0
            if disp<KAIROS_ICT_DISPLACEMENT_ATR_MIN:
                continue
            return {
                't':c['t'],
                'level':ref['valor'],
                'confirmed_ref_at':ref['confirmed_at'],
                'displacement_atr':disp,
                'index_global':global_idx,
                'kind':'MSS_CHOCH_BOS',
            }
    return None


def _find_execution_poi(exec_candles,sweep,brk,direction):
    wanted='alta' if direction=='LONG' else 'baixa'
    max_i=brk['index_global']+6
    pools=[]
    for detector in (detect_fvgs_causal,detect_ifvgs_causal,detect_breakers_causal):
        for z in detector(exec_candles):
            if z['direcao']!=wanted or z['confirmed_at']<brk['t'] or z['index']>max_i: continue
            if z.get('origin_ts',z['confirmed_at'])<=sweep['t']: continue
            if _zone_state(z,exec_candles)=='INVALIDATED': continue
            pools.append(z)
    if pools:
        rank={'IFVG':0,'BREAKER':1,'FVG':2}
        pools.sort(key=lambda z:(rank.get(z['tipo'],9),z['confirmed_at']))
        return pools[0]
    # OB de execução: origin candle entre sweep e break, confirmado pelo próprio break.
    lo=sweep['index_global']+1; hi=brk['index_global']
    for i in range(hi-1,lo-1,-1):
        c=exec_candles[i]; up=c['c']>=c['o']
        if (direction=='LONG' and not up) or (direction=='SHORT' and up):
            return {'tipo':'OB','direcao':wanted,'top':max(c['o'],c['c']),'bottom':min(c['o'],c['c']),
                    'origin_ts':c['t'],'confirmed_at':brk['t'],'index':i,'state':'OPEN'}
    return None


def _structural_targets(market, direction, entry, risk):
    """Only uncaptured opposite-side liquidity can be a structural target."""
    side='BSL' if direction=='LONG' else 'SSL'
    out=[]
    for x in market['liquidity']:
        if x['side']!=side:
            continue
        if x.get('state')!='AVAILABLE':
            continue
        level=float(x['level'])
        ok=level>entry if direction=='LONG' else level<entry
        if not ok:
            continue
        rr=abs(level-entry)/risk if risk>0 else 0
        out.append({**x,'rr':rr})
    out.sort(key=lambda x:abs(x['level']-entry))
    return out


def _entry_from_poi(poi):
    # ponto médio determinístico; depois o replay pode A/B testar proximal/50%/CE.
    return (float(poi['top'])+float(poi['bottom']))/2.0


def avaliar_kairos_radar_v2(candles_por_tf,pair=None,min_rr=KAIROS_MIN_RR_OPERACIONAL):
    """Generate LIMIT-ready setups only after a complete causal chain."""
    market=map_market_objects(candles_por_tf)
    coverage={}
    for tf in KAIROS_CONTEXT_TFS:
        cs=candles_por_tf.get(tf) or []
        coverage[tf]={'bars':len(cs),'sufficient':len(cs)>=9}
    result={
        'pair':pair,
        'status':'NO_SETUP',
        'signal':False,
        'setups':[],
        'market_counts':{'liquidity':len(market['liquidity']),'zones':len(market['zones'])},
        'data_coverage':coverage,
        'missing_or_insufficient_tfs':[tf for tf,x in coverage.items() if not x['sufficient']],
    }
    if not market['liquidity']:
        return result

    seen=set()
    for exec_tf in KAIROS_EXEC_TFS:
        cs=candles_por_tf.get(exec_tf) or []
        if len(cs)<30:
            continue
        now=cs[-1]['t']; px=cs[-1]['c']
        for direction,needed_side in (('LONG','SSL'),('SHORT','BSL')):
            liqs=[x for x in market['liquidity']
                  if x['side']==needed_side and x['confirmed_at']<now]
            liqs.sort(key=lambda x:abs(float(x['level'])-px))
            for liq in liqs[:30]:
                sweep=_find_recent_sweep(cs,liq,direction,KAIROS_SWEEP_MAX_AGE_BARS[exec_tf])
                if not sweep:
                    continue
                brk=_find_structure_break_after_sweep(cs,sweep,direction)
                if not brk or brk['t']<=sweep['t']:
                    continue
                poi=_find_execution_poi(cs,sweep,brk,direction)
                if not poi:
                    continue
                if poi['confirmed_at']<brk['t'] or poi['confirmed_at']<=sweep['t']:
                    continue

                post=[c for c in cs if c['t']>poi['confirmed_at']]
                if any(c['h']>=poi['bottom'] and c['l']<=poi['top'] for c in post):
                    continue

                poi_bars_old=sum(1 for c in cs if c['t']>poi['confirmed_at'])
                if poi_bars_old>KAIROS_POI_MAX_AGE_BARS[exec_tf]:
                    continue

                entry=_entry_from_poi(poi)
                atr=next((x for x in reversed(compute_atr(cs[:brk['index_global']+1],14)) if x),None)
                buffer=(atr*ATR_BUFFER_MULT) if atr else abs(sweep['extreme'])*STOP_BUFFER_PCT
                sl=sweep['extreme']-buffer if direction=='LONG' else sweep['extreme']+buffer

                if direction=='LONG' and not (sl < sweep['extreme'] < entry):
                    continue
                if direction=='SHORT' and not (sl > sweep['extreme'] > entry):
                    continue

                risk=abs(entry-sl)
                if risk<=0:
                    continue

                targets=_structural_targets(market,direction,entry,risk)
                valid_targets=[x for x in targets if x['rr']>=min_rr]
                if not valid_targets:
                    continue
                tp=valid_targets[0]

                context=_classify_context(candles_por_tf,direction)
                one_r=entry+risk if direction=='LONG' else entry-risk
                two_r=entry+2*risk if direction=='LONG' else entry-2*risk
                three_r=entry+3*risk if direction=='LONG' else entry-3*risk

                event_id=(f"{pair or 'PAIR'}:{liq['tf']}:{liq['kind']}:{liq['confirmed_at']}:"
                          f"{sweep['t']}:{exec_tf}:{brk['t']}:{poi['tipo']}:{poi['confirmed_at']}")
                if event_id in seen:
                    continue
                seen.add(event_id)

                setup={
                    'event_id':event_id,
                    'direction':direction,
                    'setup_class':context['class'],
                    'context_votes':context['votes'],
                    'liquidity_tf':liq['tf'],
                    'liquidity_kind':liq['kind'],
                    'liquidity_side':liq['side'],
                    'liquidity_level':round(liq['level'],8),
                    'liquidity_confirmed_at':liq['confirmed_at'],
                    'liquidity_captured_at':liq.get('captured_at'),
                    'sweep_ts':sweep['t'],
                    'sweep_extreme':round(sweep['extreme'],8),
                    'confirmation_tf':exec_tf,
                    'structure_break_ts':brk['t'],
                    'structure_break_level':round(brk['level'],8),
                    'displacement_atr':round(brk['displacement_atr'],2),
                    'poi_type':poi['tipo'],
                    'poi_tf':exec_tf,
                    'poi_bottom':round(poi['bottom'],8),
                    'poi_top':round(poi['top'],8),
                    'poi_origin_ts':poi.get('origin_ts'),
                    'poi_confirmed_at':poi['confirmed_at'],
                    'entry_limit':round(entry,8),
                    'sl':round(sl,8),
                    'invalidation_level':round(sl,8),
                    'risk_points':round(risk,8),
                    'one_r':round(one_r,8),
                    'two_r':round(two_r,8),
                    'three_r':round(three_r,8),
                    'tp':round(tp['level'],8),
                    'tp_kind':tp['kind'],
                    'tp_tf':tp['tf'],
                    'tp_confirmed_at':tp['confirmed_at'],
                    'tp_state':tp.get('state'),
                    'rr':round(tp['rr'],2),
                    'be_alert_at':round(one_r,8),
                    'status':'AWAITING_LIMIT_RETEST',
                }
                result['setups'].append(setup)
                break

    result['setups']=sorted(result['setups'],key=lambda s:s['rr'],reverse=True)
    if result['setups']:
        result['status']='SETUP_LIMIT'
        result['signal']=True
    return result


def format_telegram_setup(setup,pair):
    side='🟢 LONG' if setup['direction']=='LONG' else '🔴 SHORT'
    return (f"KAIROS — {side} — {pair}\n"
            f"Contexto: {setup['setup_class']}\n"
            f"Liquidez: {setup['liquidity_tf']} {setup['liquidity_kind']} @ {setup['liquidity_level']}\n"
            f"Sweep: {setup['sweep_extreme']}\n"
            f"Confirmação: {setup['confirmation_tf']} MSS/CHoCH/BOS + displacement {setup['displacement_atr']} ATR\n"
            f"POI: {setup['poi_type']} {setup['poi_tf']} [{setup['poi_bottom']}–{setup['poi_top']}]\n"
            f"LIMIT: {setup['entry_limit']}\nSL: {setup['sl']}\n"
            f"1R/BE: {setup['one_r']} | 2R: {setup['two_r']} | 3R: {setup['three_r']}\n"
            f"TP: {setup['tp']} — {setup['tp_tf']} {setup['tp_kind']} | RR {setup['rr']}R\n"
            f"Estado: AGUARDANDO RETESTE DA ORDEM")
