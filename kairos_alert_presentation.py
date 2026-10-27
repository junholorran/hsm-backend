"""Presentation and same-plan grouping only; never evaluates a trade."""
import hashlib
import html
import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo

M5=300000
M15=900000


def price(value):
    if value is None:return 'não informado'
    text=f'{float(value):.8f}'.rstrip('0').rstrip('.')
    integer,_,fraction=text.partition('.')
    return integer+','+fraction.ljust(2,'0')


def stamp(value):
    if value is None:return 'não informado'
    return datetime.fromtimestamp(value/1000,ZoneInfo('Europe/Lisbon')).strftime('%d/%m/%Y %H:%M')


def _zone(z):
    return tuple((z or {}).get(k) for k in ('id','tipo','bottom','top','created_ts','flip_ts','t','break_ts'))


def _facts(e):
    p=e.get('plan_details') or {}
    return (e.get('parent_zone') or p.get('parent_zone'),e.get('zone') or p.get('m5_zone'),
            e.get('invalidation',p.get('invalidation')))


def bundle_events(rows):
    """Merge only same-cycle equivalent plans, preserving every source seq.

    M15 proof and M5 readiness for the same origin/confirmation may share a
    notice. Invalidation, retest and contrary directions never disappear into
    that merge. Distinct zones, reasons or observation cycles remain separate.
    """
    units=[];by_origin={}
    for seq,e in rows:
        key=(e['pair'],e['capture_id'],e.get('break_ts'),e['observed_ts'],e.get('direction'))
        if e['stage'] in ('M15_QUEBRA','M5_ARMADO') and key in by_origin:
            old=by_origin[key]
            if ({old['events'][-1]['stage'],e['stage']}=={'M15_QUEBRA','M5_ARMADO'}
                    and _zone(_facts(old['events'][-1])[0])==_zone(_facts(e)[0])):
                old['seqs'].append(seq);old['events'].append(e)
                continue
        unit=dict(seqs=[seq],events=[e]);units.append(unit)
        if e['stage'] in ('M15_QUEBRA','M5_ARMADO'):by_origin[key]=unit
    groups=[];index={}
    for u in units:
        chosen=next((e for e in u['events'] if e['stage']=='M5_ARMADO'),u['events'][-1])
        parent,zone,extreme=_facts(chosen)
        # Without a structural confirmation, independent origins stay separate.
        origin=None if chosen.get('break_ts') else chosen['capture_id']
        signature=[chosen['pair'],chosen['observed_ts'],chosen['stage'],chosen.get('direction'),
                   chosen.get('break_ts'),chosen.get('candle_ts'),origin,_zone(parent),_zone(zone),extreme,
                   chosen.get('reason'),chosen.get('invalidation_reasons'),
                   (chosen.get('plan_details') or {}).get('had_retest')]
        key=json.dumps(signature,sort_keys=True,separators=(',',':'))
        if key not in index:
            index[key]=dict(seqs=[],events=[]);groups.append(index[key])
        index[key]['seqs'].extend(u['seqs']);index[key]['events'].extend(u['events'])
    for g in groups:g['seqs'].sort()
    return groups


def _reason_lines(e):
    reasons=e.get('invalidation_reasons') or [dict(code=e.get('reason'))]
    lines=[]
    for r in reasons:
        code=r.get('code')
        if code=='OPPOSITE_M15_BREAK':
            text=f"{r.get('kind') or 'Quebra'} contrário M15 além de {price(r.get('level'))}; fechamento {price(r.get('close'))}"
        else:
            text={'CONFIRMED_REACTION_CHANGED':('A reação confirmada da origem mudou'+
                    (f" em torno de {price(r['level'])}" if r.get('level') is not None else '')),
                  'STRUCTURAL_EXTREME_BREACHED':f"Extremo estrutural ultrapassado: {price(r.get('level'))}",
                  'PARENT_POI_INVALIDATED':'POI causal M15 invalidado pelo detector',
                  'M5_POI_INVALIDATED':'POI M5 invalidado pelo detector',
                  'ENTRY_POLICY_CHANGED':'Modelo corrigido: plano fora da regra de sweep HTF relevante e FVG causal',
                  'HTF_ORIGIN_INVALIDATED':'FVG HTF de origem invalidado',
                  'OPPOSITE_M5_BREAK':'Quebra contrária M5 confirmada por fechamento',
                  'NO_DIRECT_M15_FVG':'A terceira vela não confirmou um FVG M15 válido ligado à quebra',
                  'HISTORY_LOST':'Histórico insuficiente para continuar a sequência'}.get(code,
                  'Motivo específico não disponível no evento original')
        if r.get('confirmed_ts') is not None:text+=f" | {stamp(r['confirmed_ts'])} Portugal"
        lines.append('• '+text)
    return lines


def _pages(lines,header,max_units=3400):
    """Split before HTML escaping, never inside an entity or surrogate pair."""
    def units(s):return len(s.encode('utf-16-le'))//2
    top='<b>'+html.escape(header)+'</b>\n'
    footer='\nDEMO / MANUAL · execução não comprovada.'
    pages=[];page=top
    for line in lines:
        chunks=[line[i:i+400] for i in range(0,len(line),400)] or ['']
        for chunk in chunks:
            escaped=html.escape(chunk)+'\n'
            if units(page+escaped+footer)>max_units:
                pages.append(page.rstrip()+footer);page=top
            page+=escaped
    pages.append(page.rstrip()+footer)
    if len(pages)>1:
        pages=[p+f'\nParte {i+1}/{len(pages)}' for i,p in enumerate(pages)]
    return pages


def format_chain_events(events,now_ts=None):
    e=next((x for x in events if x['stage']=='M5_ARMADO'),events[-1])
    stage=e['stage'];side=e.get('direction');trade='COMPRA' if side=='LONG' else 'VENDA' if side=='SHORT' else 'PLANO'
    titles={'CAPTURA':'💧 LIQUIDEZ CAPTURADA','POI_HTF_TOCADO':'📦 TOQUE EM POI HTF',
        'M5_ANTECIPADO':f'🔔 {trade} | QUEBRA M5 ANTECIPADA',
        'M5_ANTECIPADO_INVALIDADO':'🚫 HIPÓTESE M5 ANTECIPADA ENCERRADA',
        'M15_ARMADO':f'🟡 PLANO M15 DE {trade}',
        'M15_QUEBRA':f'🔎 {trade} | M15 CONFIRMADO','M5_ARMADO':f'🟡 PLANO DE {trade}',
        'RETESTE_OBSERVADO':f'👀 {trade} | RETESTE OBSERVADO',
        'PLANO_INVALIDADO':f'🚫 PLANO DE {trade} INVALIDADO',
        'TOQUE_E_INVALIDACAO':'⚠️ TOQUE E INVALIDAÇÃO NO MESMO CANDLE',
        'ORIGEM_INVALIDADA':'🚫 SEQUÊNCIA DESTA ORIGEM ENCERRADA',
        'HISTORICO_INSUFICIENTE':'⚠️ HISTÓRICO INSUFICIENTE'}
    parent,zone,extreme=_facts(e)
    ref=[e['pair'],side,e.get('break_ts'),_zone(parent)] if e.get('break_ts') else e['capture_id']
    ident=hashlib.sha256(json.dumps(ref,separators=(',',':')).encode()).hexdigest()[:8]
    header=f"KAIROS | {e['pair']} | {titles.get(stage,stage)}"
    if stage=='POI_HTF_TOCADO' and e.get('htf_tf'):
        header=f"KAIROS | {e['pair']} | 📦 TOQUE EM FVG {e['htf_tf']}"
    lines=[f"Sequência {ident} · atualização {stamp(e['observed_ts'])} Portugal"]
    now_ts=int(time.time()*1000) if now_ts is None else now_ts
    if now_ts-e['observed_ts']>M15:lines.append('⚠️ Aviso histórico / entrega atrasada: confirme o estado atual antes de agir.')
    context=e.get('context') or {}
    if context:
        arrow={'alta':'↑','baixa':'↓','neutro':'→'}
        lines.append('Contexto estrutural: '+' | '.join(f"{tf} {arrow.get(context[tf],'→')}" for tf in ('MN','W1','D1','H4','H1') if tf in context))
    lines+=['','ORIGENS IDENTIFICADAS']
    origins={}
    for source in events:origins.setdefault(source['capture_id'],source)
    for cid,source in origins.items():
        c=source['capture'];loc=c.get('htf_location') or {}
        if c.get('origin_kind')=='HTF_FVG_TOUCH':
            lines.append(f"📦 POI {loc.get('tf')} FVG: {price(loc.get('bottom'))}–{price(loc.get('top'))} | origem {cid[:8]}")
            lines.append(f"Toque HTF confirmado: {stamp(c.get('native_capture_confirm_ts'))} Portugal; sem captura de liquidez presumida.")
        else:
            typ=str(c.get('liquidity_type','LIQUIDEZ')).replace('LUDO_SWING_HIGH','topo Ludo').replace('LUDO_SWING_LOW','fundo Ludo')
            lines.append(f"💧 Liquidez {c.get('liquidity_tf')} {typ}: {price(c.get('nivel'))} | origem {cid[:8]}")
            lines.append(f"Captura confirmada: {stamp(c.get('native_capture_confirm_ts'))} Portugal.")
        rx=source.get('reaction')
        if rx and rx.get('state')!='HTF_FVG_TOUCH':
            label={'REJECTION_RECLAIM':'rejeição / recuperação','ACCEPTANCE_CONTINUATION':'aceitação / continuação'}.get(rx['state'],'a confirmar')
            lines.append(f"Reação confirmada: {label} | {stamp(rx.get('confirmed_ts'))} Portugal | {rx.get('direction','a confirmar')}.")
    br=(e.get('plan_details') or {}).get('m15_break') or {}
    original=next((x for x in events if x['stage']=='M15_QUEBRA'
                   and x.get('break_ts')==e.get('break_ts') and x.get('direction')==side),{})
    kind=e.get('kind') or br.get('kind') or original.get('kind')
    level=e.get('level',br.get('level',original.get('level')))
    if kind:
        heading='CONFIRMAÇÃO ORIGINAL DO PLANO' if stage in ('PLANO_INVALIDADO','TOQUE_E_INVALIDACAO') else 'CONFIRMAÇÃO ESTRUTURAL'
        confirmation_tf='M5' if stage in ('M5_ANTECIPADO','M5_ANTECIPADO_INVALIDADO') else 'M15'
        lines+=['',heading,f"{kind} {confirmation_tf} por fechamento além de {price(level)}.",
                f"Confirmado em {stamp(e.get('break_ts',br.get('close_ts')))} Portugal | estrutura Ludo."]
    if parent or zone:
        lines+=['','ZONAS DO PLANO']
        names={'FVG_bullish':'FVG comprador','FVG_bearish':'FVG vendedor',
               'IFVG_bullish':'IFVG comprador','IFVG_bearish':'IFVG vendedor',
               'OB_bullish':'OB comprador','OB_bearish':'OB vendedor'}
        if parent:lines.append(f"M15 {names.get(parent.get('tipo'),'POI')}: {price(parent['bottom'])}–{price(parent['top'])}")
        if zone:
            zone_tf=e.get('zone_tf','M5')
            if zone_tf!='M15' or not parent:
                lines.append(f"{zone_tf} {names.get(zone.get('tipo'),'POI')}: {price(zone['bottom'])}–{price(zone['top'])}")
            proximal=zone['top'] if side=='LONG' else zone['bottom'] if side=='SHORT' else None
            if proximal is not None:lines.append(f'Proximal planeada: {price(proximal)}; não confirma execução.')
    if extreme is not None:
        label='Fundo estrutural abaixo da entrada' if side=='LONG' else 'Topo estrutural acima da entrada' if side=='SHORT' else 'Extremo estrutural'
        lines += [f'{label}: {price(extreme)}.','Referência estrutural para proteção. Stop exato não calculado; margem não definida.']
    lines+=['','ESTADO / PRÓXIMO PASSO']
    if stage=='M15_ARMADO':
        lines.append('PLANO M15 PRONTO · aguardar retorno ao FVG M15. M5 opcional para refinamento; não exige nova quebra.')
        lines.append('Quebra com força pelo critério experimental flexível; ordem limite e stop são decisões manuais.')
    elif stage=='M5_ARMADO':
        lines.append('REFINAMENTO M5 OPCIONAL · plano principal M15 independente.' if e.get('primary_policy') else 'AGUARDAR RETESTE M5 · entrada ainda não confirmada.')
    elif stage=='M5_ANTECIPADO':
        lines.append('M15 por confirmar · POI causal e reteste ainda por verificar. SEM TRADE AUTORIZADO POR ESTE AVISO.')
        if e['observed_ts']-e['break_ts']>=M5:
            lines.append('Quebra M5 histórica / aviso atrasado; não constitui uma nova entrada agora.')
    elif stage=='M5_ANTECIPADO_INVALIDADO':
        lines+=_reason_lines(e)
        lines.append('Hipótese antecipada M5 encerrada; não cancela automaticamente um plano M15 independente nem ordens na exchange.')
    elif stage=='M15_QUEBRA':lines.append('QUEBRA M15 COM FORÇA · aguardar fechamento da terceira vela e confirmação do FVG M15; ainda sem plano pronto.' if e.get('primary_policy') else 'AGUARDAR POI CAUSAL / REFINAMENTO M5 · ainda sem entrada confirmada.')
    elif stage in ('CAPTURA','POI_HTF_TOCADO'):
        from kairos_m15_primary import enabled as m15_primary_enabled
        lines.append('AGUARDAR REAÇÃO / M5 ANTECIPADO; plano principal: quebra forte M15 → FVG M15 confirmado → aguardar retorno. M5 opcional. SEM TRADE por captura isolada.' if m15_primary_enabled() else 'AGUARDAR REAÇÃO / QUEBRA M5 ANTECIPADA; confirmação M15 Ludo → POI causal → refinamento M5 → reteste. SEM TRADE. Captura ou toque isolado não autoriza entrada.')
    elif stage=='RETESTE_OBSERVADO':
        lines.append(f"Candle M5 confirmado: {stamp(e.get('candle_ts',0)+M5)} Portugal.")
        if e.get('zone_tf')=='M15':lines.append('Retorno ao FVG M15 observado em candle M5 fechado.')
        lines.append('Reteste observado; execução não comprovada. Confirme o estado atual antes de decidir.')
        if e['observed_ts']-e.get('candle_ts',0)-M5>=M5:lines.append('Reteste histórico; nenhuma nova entrada presumida agora.')
    elif stage in ('PLANO_INVALIDADO','TOQUE_E_INVALIDACAO'):
        lines+=_reason_lines(e)
        new=e.get('new_reaction')
        if new:
            label={'REJECTION_RECLAIM':'rejeição / recuperação','ACCEPTANCE_CONTINUATION':'aceitação / continuação'}.get(new['state'],'a confirmar')
            lines.append(f"Nova reação: {label} | hipótese {new.get('direction','a confirmar')}; não é entrada automática no sentido oposto.")
        had=(e.get('plan_details') or {}).get('had_retest')
        if had is False:lines.append('Plano encerrado sem reteste observado.')
        elif had:lines.append('Plano encerrado após reteste observado; posição real desconhecida.')
        lines.append('Este aviso não cancela ordens na exchange.')
        if stage=='TOQUE_E_INVALIDACAO':lines.append('Sequência intrabar desconhecida; não presumir entrada válida.')
    elif stage=='ORIGEM_INVALIDADA':
        lines.append('FVG HTF desta origem invalidado; não significa que todos os outros planos foram cancelados.')
        if e.get('candle_ts') is not None:lines.append(f"Invalidação confirmada: {stamp(e['candle_ts'])} Portugal.")
    elif stage=='HISTORICO_INSUFICIENTE':lines.append('Acompanhamento encerrado por falta de histórico; aguardar nova sequência verificável.')
    return _pages(lines,header)


def format_radar_capture(pair,event):
    observed=event.get('timestamp')
    if observed is None:observed=int(time.time()*1000)
    key=event.get('key') or json.dumps([pair,event.get('liquidity_tf'),event.get('level'),observed])
    side='HIGH' if 'HIGH' in str(event.get('liquidity_type')) or event.get('liquidity_type') in ('PDH','PWH','PMH','EQH') else 'LOW'
    rx=event.get('reaction') or 'UNRESOLVED_REACTION'
    direction=('SHORT' if side=='HIGH' else 'LONG') if rx=='REJECTION_RECLAIM' else ('LONG' if side=='HIGH' else 'SHORT')
    proof=dict(pair=pair,capture_id=hashlib.sha256(str(key).encode()).hexdigest()[:20],
        observed_ts=observed,stage='CAPTURA',context=event.get('context'),
        capture=dict(liquidity_tf=event.get('liquidity_tf'),liquidity_type=event.get('liquidity_type'),
            nivel=event.get('level'),native_capture_confirm_ts=event.get('capture_confirm_ts',event.get('timestamp'))))
    if rx!='UNRESOLVED_REACTION':
        proof['reaction']=dict(state=rx,direction=direction,confirmed_ts=observed)
    return '\n\n'.join(format_chain_events([proof]))


def format_htf_touch(pair,event):
    observed=event.get('touch_close_ts')
    if observed is None:observed=int(time.time()*1000)
    proof=dict(pair=pair,capture_id=hashlib.sha256(str(event.get('key') or event).encode()).hexdigest()[:20],
        observed_ts=observed,stage='POI_HTF_TOCADO',htf_tf=event.get('tf'),
        capture=dict(origin_kind='HTF_FVG_TOUCH',native_capture_confirm_ts=observed,
            htf_location=dict(tf=event.get('tf'),bottom=event.get('bottom'),top=event.get('top'))))
    return '\n\n'.join(format_chain_events([proof]))
