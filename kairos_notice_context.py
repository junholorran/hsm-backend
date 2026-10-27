"""Unified notification context; no trading decisions or signal suppression."""
import copy
import hashlib
import html
import json
import math

from kairos_alert_presentation import price,stamp,_pages,format_chain_events

REACTIONS={'REJECTION_RECLAIM':'rejeição / recuperação',
           'ACCEPTANCE_CONTINUATION':'aceitação / continuação',
           'UNRESOLVED_REACTION':'ainda indefinida'}


def _valid_radar(r,cutoff):
    ts=r.get('timestamp')
    return (isinstance(ts,(int,float)) and math.isfinite(ts) and ts<=cutoff
            and isinstance(r.get('level'),(int,float)) and math.isfinite(r['level']))


def context_snapshot(previous,state,radars,cutoff):
    if cutoff<(previous or {}).get('asof_ts',0):return copy.deepcopy(previous)
    result=copy.deepcopy(previous or {})
    result['asof_ts']=cutoff
    liquidity=result.setdefault('liquidity',{})
    for tf in ('M15','M5'):liquidity.pop(tf,None)
    candidates=[copy.deepcopy(r) for r in radars if _valid_radar(r,cutoff)]
    for rec in state.get('captures',{}).values():
        c=rec['capture'];rx=rec.get('reaction') or {}
        if c.get('origin_kind')=='HTF_FVG_TOUCH' or c.get('liquidity_tf') in ('M15','M5') or rec.get('history_lost'):continue
        candidates.append(dict(timestamp=rx.get('confirmed_ts') or c.get('native_capture_confirm_ts'),
            capture_confirm_ts=c.get('native_capture_confirm_ts'),liquidity_tf=c.get('liquidity_tf'),
            liquidity_type=c.get('liquidity_type'),level=c.get('nivel'),
            reaction=rx.get('state','UNRESOLVED_REACTION')))
    for r in candidates:
        if not _valid_radar(r,cutoff):continue
        tf=r.get('liquidity_tf') or 'HTF'
        if r['timestamp']>liquidity.get(tf,{}).get('timestamp',-1):liquidity[tf]=r
    if 'captures' in state:
        pois={}
        for cid,rec in state['captures'].items():
            c=rec['capture']
            if (c.get('origin_kind')!='HTF_FVG_TOUCH' or rec.get('origin_invalidated')
                    or rec.get('history_lost')):continue
            loc=c.get('htf_location') or {};known=c.get('native_capture_confirm_ts')
            if known is None or known>cutoff:continue
            key=(loc.get('tf'),loc.get('zone_id') or (loc.get('bottom'),loc.get('top')))
            if key not in pois or known>pois[key]['touch_confirm_ts']:
                pois[key]=dict(tf=loc.get('tf'),bottom=loc.get('bottom'),top=loc.get('top'),
                    zone_id=loc.get('zone_id'),touch_confirm_ts=known,capture_id=cid)
        result['pois']=list(pois.values())
    result.setdefault('pois',[])
    return result


def radar_notice(pair,r,cutoff,policy):
    key='UNIFIED_RADAR:'+pair+':'+str(r['key'])
    return dict(key=key,pair=pair,stage='RADAR_CONTEXT',observed_ts=cutoff,
        causal_policy_version=policy,capture_id=hashlib.sha256(key.encode()).hexdigest()[:20],
        radar_event=copy.deepcopy(r),fill_assumed=False,entry_authorized=False)


def update_groups(groups):
    """One envelope per pair/M5 update, with independent plan sections inside."""
    batches=[];index={}
    for group in groups:
        e=group['events'][0]
        key=(e['pair'],e['observed_ts']//300000)
        if key not in index:
            index[key]=dict(seqs=[],groups=[]);batches.append(index[key])
        index[key]['seqs'].extend(group['seqs']);index[key]['groups'].append(group)
    for batch in batches:batch['seqs'].sort()
    return batches


def _liquidity_lines(r):
    lines=[f"💧 {r.get('liquidity_tf')} {r.get('liquidity_type')}: {price(r.get('level'))}",
        f"Reação: {REACTIONS.get(r.get('reaction'),'a confirmar')} | confirmação {stamp(r.get('timestamp'))} Portugal.",
        f"Captura confirmada: {stamp(r.get('capture_confirm_ts'))} Portugal."]
    typ=str(r.get('liquidity_type') or '')
    high='HIGH' in typ or typ in ('PDH','PWH','PMH','EQH')
    low='LOW' in typ or typ in ('PDL','PWL','PML','EQL')
    if (high or low) and r.get('reaction') in ('REJECTION_RECLAIM','ACCEPTANCE_CONTINUATION'):
        up=low if r['reaction']=='REJECTION_RECLAIM' else high
        if r['reaction']=='ACCEPTANCE_CONTINUATION':
            lines.append('Contexto de continuação: '+('LONG' if up else 'SHORT')+'; não autoriza plano de entrada por esta captura.')
        else:
            lines.append('Hipótese da reação: '+('LONG' if up else 'SHORT')+'; aguardar autorização estrutural.')
    context=r.get('context') or {};arrow={'alta':'↑','baixa':'↓','neutro':'→'}
    if context:lines.append('Contexto na confirmação: '+' | '.join(f"{tf} {arrow.get(context[tf],'→')}" for tf in ('MN','W1','D1','H4','H1') if tf in context))
    if r.get('capture_evidence_tf')=='D1':lines.append('Evidência diária; instante intradiário da captura não determinado.')
    if r.get('context_only'):lines.append('Referência de contexto HTF; não autoriza entrada sozinha.')
    return lines


def format_pair_update(pair,groups,context,now_ts=None):
    events=[e for g in groups for e in g['events']]
    observed=max(e['observed_ts'] for e in events)
    lines=[f"Atualização registada: {stamp(observed)} Portugal.",
           'Referências e planos abaixo não constituem automaticamente uma única cadeia.']
    if context.get('liquidity'):
        lines+=['','CONTEXTO DE LIQUIDEZ — última confirmação por timeframe']
        for tf,r in sorted(context['liquidity'].items(),key=lambda p:(-p[1]['timestamp'],p[0])):
            lines+=_liquidity_lines(r)
    if context.get('pois'):
        lines+=['','POIs HTF EM ACOMPANHAMENTO — origens distintas da liquidez']
        for p in sorted(context['pois'],key=lambda p:(-p['touch_confirm_ts'],str(p['tf']))):
            lines.append(f"📦 FVG {p['tf']}: {price(p['bottom'])}–{price(p['top'])} | toque {stamp(p['touch_confirm_ts'])} Portugal; sem captura de liquidez presumida.")
    lines+=['','ATUALIZAÇÕES DESTE CICLO']
    operational=False
    for group in groups:
        contextual=[e for e in group['events'] if e['stage'] in ('RADAR_CONTEXT','CAPTURA','POI_HTF_TOCADO')]
        if contextual:
            for e in contextual:
                if e['stage']=='RADAR_CONTEXT':
                    r=e['radar_event'];lines+=_liquidity_lines(r)
                else:
                    c=e['capture'];loc=c.get('htf_location') or {}
                    if e['stage']=='POI_HTF_TOCADO':
                        lines.append(f"📦 Novo toque FVG {loc.get('tf')}: {price(loc.get('bottom'))}–{price(loc.get('top'))} | {stamp(c.get('native_capture_confirm_ts'))} Portugal | origem {e['capture_id'][:8]}.")
                    else:
                        lines.append(f"💧 Captura {c.get('liquidity_tf')} {c.get('liquidity_type')} @ {price(c.get('nivel'))} | {stamp(c.get('native_capture_confirm_ts'))} Portugal | origem {e['capture_id'][:8]}.")
        else:
            operational=True
            lines+=['','SEQUÊNCIA INDEPENDENTE']
            for page in format_chain_events(group['events'],now_ts=now_ts):
                plain=html.unescape(page.replace('<b>','').replace('</b>',''))
                lines += [line for line in plain.splitlines() if line!='DEMO / MANUAL · execução não comprovada.']
    if not operational:
        lines+=['','AGORA: verificar reação e primeira quebra M5 válida; a confirmação M15 e o plano causal continuam a ser acompanhados separadamente.',
                'SEM TRADE por captura ou toque isolado.']
    if context.get('asof_ts') is not None:
        lines.append(f"Contexto guardado até {stamp(context['asof_ts'])} Portugal; cada referência conserva a sua hora.")
    return _pages(lines,f'KAIROS | {pair} | ATUALIZAÇÃO')
