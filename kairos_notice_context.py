"""Unified notification context; no trading decisions or signal suppression."""
import copy
import hashlib
import html
import json
import math
import time

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


def _format_context_update(pair,groups,context,now_ts=None):
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


_READY_STAGES = {'M15_ARMADO', 'M5_ARMADO'}


def _entry_key(e):
    p=e.get('plan_details') or {}
    parent=e.get('parent_zone') or p.get('parent_zone') or {}
    # Presentation grouping only: never join different structural plans.
    return json.dumps([e.get('pair'),e.get('direction'),e.get('break_ts'),e.get('observed_ts'),
        parent, e.get('invalidation',p.get('invalidation'))],sort_keys=True)



def format_entry_notice(events,now_ts=None):
    """Render existing ready events; never authorize or execute an order."""
    e=next((x for x in events if x['stage']=='M15_ARMADO'),events[-1])
    now_ts=int(time.time()*1000) if now_ts is None else now_ts
    side=e.get('direction')
    if side not in ('LONG','SHORT'):
        return format_chain_events(events,now_ts=now_ts)
    p=e.get('plan_details') or {};br=p.get('m15_break') or {}
    parent=e.get('parent_zone') or p.get('parent_zone')
    zone=parent or e.get('zone') or p.get('m5_zone')
    if not zone or zone.get('bottom') is None or zone.get('top') is None:
        return format_chain_events(events,now_ts=now_ts)
    trade='🟢 COMPRA' if side=='LONG' else '🔴 VENDA'
    # Only the existing strict primary policy proves the force badge.
    fire=' 🔥' if e.get('primary_policy') else ''
    kind=br.get('kind') or e.get('kind') or 'Quebra'
    lines=[f"{trade} · {e['pair']} · M15",f"{kind} LUDO{fire} · confirmado por fechamento",
           f"🕐 Quebra: {stamp(e.get('break_ts') or br.get('close_ts'))} Portugal"]
    origins=set()
    for source in events:
        c=source.get('capture') or {};loc=c.get('htf_location') or {}
        if c.get('origin_kind')=='HTF_FVG_TOUCH':
            text=f"📦 Origem: toque FVG {loc.get('tf')} {price(loc.get('bottom'))}–{price(loc.get('top'))}"
        else:
            typ=str(c.get('liquidity_type') or 'liquidez').replace('LUDO_SWING_LOW','fundo Ludo').replace('LUDO_SWING_HIGH','topo Ludo')
            text=f"💧 Captura: {c.get('liquidity_tf')} {typ} @ {price(c.get('nivel'))}"
        text+=f" · {stamp(c.get('native_capture_confirm_ts'))}"
        if text not in origins:lines.append(text);origins.add(text)
    tf='M15' if parent else e.get('zone_tf','M5')
    lines.append(f"📍 Zona de entrada {tf}: {price(zone['bottom'])}–{price(zone['top'])}")
    refinements=set()
    for source in events:
        z=source.get('zone')
        if source['stage']=='M5_ARMADO' and source.get('zone_tf','M5')=='M5' and z:
            refinements.add(f"🔍 M5 opcional: {price(z['bottom'])}–{price(z['top'])}")
    lines.extend(sorted(refinements))
    extreme=e.get('invalidation',p.get('invalidation'))
    if extreme is not None:
        label='fundo' if side=='LONG' else 'topo'
        lines.append(f"🛑 Referência de proteção ({label}): {price(extreme)} · stop exato não calculado")
    else:lines.append('🛑 Proteção: não informada · stop não calculado')
    lines += [f"🕐 Plano disponível: {stamp(max(x['observed_ts'] for x in events))} Portugal",
              '⏳ Aguardar retorno à zona para a ordem limite manual.',
              'O Kairos não coloca ordens nem confirma execução na Bybit.']
    if now_ts is not None and now_ts-max(x['observed_ts'] for x in events)>900000:
        lines.append('⚠️ Aviso atrasado: confirmar validade atual antes de agir.')
    return _pages(lines,'🎯 KAIROS | PREPARAR ORDEM LIMITE')


def format_pair_update(pair,groups,context,now_ts=None):
    # Ready plans get their own short message, outside the context envelope.
    entries={};others=[]
    terminal={_entry_key(e)
              for g in groups for e in g['events']
              if e['stage'] in ('PLANO_INVALIDADO','TOQUE_E_INVALIDACAO','RETESTE_OBSERVADO')}
    for group in groups:
        ready=[e for e in group['events'] if e['stage'] in _READY_STAGES and e.get('primary_policy')]
        rest=[e for e in group['events'] if e not in ready
              and not (e['stage']=='M15_QUEBRA' and any(
                  e.get('break_ts')==r.get('break_ts') and e.get('direction')==r.get('direction')
                  for r in ready))]
        for e in ready:
            if _entry_key(e) not in terminal:
                entries.setdefault(_entry_key(e),[]).append(e)
        if rest:others.append(dict(group,events=rest))
    if not entries:return _format_context_update(pair,others or groups,context,now_ts)
    pages=[]
    for events in entries.values():pages.extend(format_entry_notice(events,now_ts))
    if others:pages.extend(_format_context_update(pair,others,context,now_ts))
    return pages
