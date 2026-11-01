"""Short event and scheduled context presentation. No trading authority."""
import copy
import hashlib
import html
import json
import time

from kairos_alert_presentation import price,stamp,_pages,_facts,_reason_lines,_zone

H4=14400000


def plan_ref(e):
    parent,zone,extreme=_facts(e)
    key=[e.get('pair'),e.get('direction'),e.get('break_ts'),_zone(parent or zone),extreme]
    return str(e.get('pair','')).replace('USD','')+'-'+hashlib.sha256(json.dumps(key).encode()).hexdigest()[:6]


def _origins(events):
    lines=[]
    for e in events:
        c=e.get('capture') or {}
        if not c:continue
        loc=c.get('htf_location') or {}
        if c.get('origin_kind')=='HTF_FVG_TOUCH':
            line=f"📦 FVG {loc.get('tf')}: {price(loc.get('bottom'))}–{price(loc.get('top'))} · sem captura de liquidez presumida"
        else:
            kind=str(c.get('liquidity_type','liquidez')).replace('LUDO_SWING_LOW','fundo Ludo').replace('LUDO_SWING_HIGH','topo Ludo')
            line=f"💧 {c.get('liquidity_tf')} {kind} @ {price(c.get('nivel'))}"
        line+=f" · {stamp(c.get('native_capture_confirm_ts'))}"
        if line not in lines:lines.append(line)
    return lines


def format_event_group(events,now_ts=None):
    e=next((x for x in events if x['stage']=='CAPTURA'),events[-1]);stage=e['stage'];pair=e['pair'];side=e.get('direction')
    trade={'LONG':'🟢 COMPRA','SHORT':'🔴 VENDA'}.get(side,'')
    titles={'CAPTURA':'💧 KAIROS | LIQUIDEZ CAPTURADA','RADAR_CONTEXT':'👀 KAIROS | REAÇÃO ATUALIZADA',
            'POI_HTF_TOCADO':'📦 KAIROS | FVG TOCADO','M15_QUEBRA':'👀 KAIROS | QUEBRA M15 CONFIRMADA',
            'M5_ANTECIPADO':'👀 KAIROS | QUEBRA M5 ANTECIPADA',
            'M5_ANTECIPADO_INVALIDADO':'❌ KAIROS | HIPÓTESE M5 ENCERRADA',
            'RETESTE_OBSERVADO':'👀 KAIROS | RETESTE OBSERVADO',
            'PLANO_INVALIDADO':'❌ KAIROS | PLANO INVALIDADO',
            'TOQUE_E_INVALIDACAO':'❌ KAIROS | TOQUE E INVALIDAÇÃO',
            'ORIGEM_INVALIDADA':'❌ KAIROS | ORIGEM INVALIDADA',
            'HISTORICO_INSUFICIENTE':'⚠️ KAIROS | HISTÓRICO INSUFICIENTE'}
    if stage not in titles:
        from kairos_alert_presentation import format_chain_events
        return format_chain_events(events,now_ts)
    lines=[f'{pair}'+(' · '+trade if trade else ''),f"🕐 {stamp(e['observed_ts'])} Portugal"]
    if e.get('break_ts'):lines.append('🔖 Plano: '+plan_ref(e))
    if stage=='RADAR_CONTEXT':
        r=e.get('radar_event') or {}
        lines.append(f"💧 {r.get('liquidity_tf')} {r.get('liquidity_type')} @ {price(r.get('level'))}")
        labels={'REJECTION_RECLAIM':'rejeição / recuperação','ACCEPTANCE_CONTINUATION':'aceitação / continuação','UNRESOLVED_REACTION':'indefinida'}
        lines.append('Reação: '+labels.get(r.get('reaction'),'indefinida'))
        if r.get('capture_confirm_ts'):lines.append('Captura original: '+stamp(r['capture_confirm_ts'])+' Portugal')
        lines.append('⏳ Aguardar plano M15; reação isolada não autoriza entrada.')
    elif stage in ('CAPTURA','POI_HTF_TOCADO'):
        lines+=_origins(events)
        rx=next((x['radar_event'] for x in reversed(events) if x.get('radar_event')),None)
        if rx:
            labels={'REJECTION_RECLAIM':'rejeição / recuperação','ACCEPTANCE_CONTINUATION':'aceitação / continuação','UNRESOLVED_REACTION':'ainda indefinida'}
            lines.append('Reação: '+labels.get(rx.get('reaction'),'a confirmar'))
        if stage=='POI_HTF_TOCADO':
            from kairos_htf_fvg_alerts import confluence_line
            confluence=confluence_line(e.get('liquidity_inside'))
            if confluence:lines.append(confluence)
            lines.append('M5 apenas refina depois do plano M15.')
        lines.append('⏳ Aguardar reação e quebra M15; ainda sem plano de entrada. SEM TRADE.')
    elif stage in ('M15_QUEBRA','M5_ANTECIPADO'):
        lines+=_origins(events)
        tf='M15' if stage=='M15_QUEBRA' else 'M5'
        from kairos_m15_primary import notice_is_strong
        fire=' 🔥' if notice_is_strong(e) and stage=='M15_QUEBRA' else ''
        lines.append(f"✅ {e.get('kind','Quebra')} {tf} · LUDO{fire} além de {price(e.get('level'))}")
        lines.append(f"Fechamento: {stamp(e.get('break_ts'))} Portugal")
        lines.append('⏳ Aguardar terceira vela e FVG confirmado; ordem ainda não preparada.' if tf=='M15' else '⏳ Antecipação M5; M15 e plano ainda por confirmar.')
    elif stage=='RETESTE_OBSERVADO':
        parent,zone,_=_facts(e);z=parent or zone
        if z:lines.append(f"📦 Zona M15: {price(z['bottom'])}–{price(z['top'])}" if parent else f"📦 Zona M5: {price(z['bottom'])}–{price(z['top'])}")
        if e.get('candle_ts') is not None:lines.append('Retorno confirmado: '+stamp(e['candle_ts']+300000)+' Portugal')
        lines.append('✅ Preço regressou à zona do plano.')
        lines.append('Execução na Bybit não confirmada; não é uma nova entrada.')
    else:
        lines+=_reason_lines(e)
        lines.append('Este aviso não cancela ordens na Bybit; verificar ordens e posições manualmente.')
        if stage=='TOQUE_E_INVALIDACAO':lines.append('Ordem dos movimentos dentro da vela desconhecida; execução não presumida.')
    now_ts=int(time.time()*1000) if now_ts is None else now_ts
    if now_ts-e['observed_ts']>900000:lines.append('⚠️ Aviso histórico / entrega atrasada; confirmar estado atual.')
    return _pages(lines,titles[stage])


def state_summary(state,events,cutoff):
    """Read existing state; never reset, select or authorize a plan."""
    from kairos_capture_chains import state_view
    plans=[];seen=set()
    for cid,rec in state.get('captures',{}).items():
        p=rec.get('plan') or {}
        if not p or p.get('invalidated') or rec.get('origin_invalidated') or rec.get('history_lost'):continue
        v=state_view(dict(state,captures={cid:rec}));z=p.get('m15_zone') or p.get('m5_zone')
        if not z or v.get('code')=='SETUP_ANTERIOR_INVALIDADO':continue
        side='LONG' if p['break']['side']=='UP' else 'SHORT'
        e=dict(pair=state.get('pair',''),direction=side,break_ts=p['break']['close_ts'],
               parent_zone=p.get('parent_zone'),zone=z,invalidation=p.get('invalidation'))
        identity=(side,p['break']['close_ts'],z.get('id'),z['bottom'],z['top'])
        if identity in seen:continue
        seen.add(identity)
        plans.append(dict(ref=plan_ref(e),direction=side,code=v['code'],bottom=z['bottom'],top=z['top'],
                          confirmed_ts=p['break']['close_ts'],touched=bool(p.get('touched'))))
    return dict(asof_ts=cutoff,plans=plans,changes=copy.deepcopy(events))


def scheduled_parts(report,max_chars=3500):
    from kairos_daily_report import REASONS
    d=report['daily'];cut=report['asof_ts'];bucket=cut//H4*H4
    title='🧭 KAIROS | MAPA DO DIA' if bucket==d['available_ts'] else '🔄 KAIROS | PONTO DE SITUAÇÃO'
    bias={'BULLISH':'ALTA','BEARISH':'BAIXA','NEUTRO':'INDEFINIDO'}.get(d.get('bias'),'INDEFINIDO')
    lines=[title,f"{report['pair']} · {stamp(cut)} Portugal",'Cenário diário: '+bias+(' · EM REAVALIAÇÃO' if report.get('daily_review_required') else ''),REASONS.get(d.get('reason'),'Leitura diária indisponível.')]
    ctx=report.get('structure_context') or {};arrows={'alta':'↑','baixa':'↓','neutro':'↔'}
    if ctx:lines.append('Estrutura: '+' | '.join(f'{tf} {arrows.get(ctx[tf],"?")}' for tf in ('MN','W1','D1','H4','H1') if tf in ctx))
    if report.get('daily_review_required'):
        rule={'CLOSE_ABOVE_PREVIOUS_HIGH':('≤','máxima anterior','h'),
              'CLOSE_BELOW_PREVIOUS_LOW':('≥','mínima anterior','l'),
              'HIGH_CAPTURE_AND_CLOSE_INSIDE':('≥','máxima anterior','h'),
              'LOW_CAPTURE_AND_CLOSE_INSIDE':('≤','mínima anterior','l')}.get(d.get('reason'))
        if rule:
            op,label,key=rule
            lines.append(f"Reavaliar: M5 fechou {op} {label} ({price(d['previous_day'][key])}).")
    px=(report.get('current_price') or {}).get('value')
    if px is not None:lines.append(f'Preço de referência: {price(px)} · último M5 fechado')
    else:lines.append('Preço de referência atual: indisponível neste mapa.')
    summary=report.get('notice_summary') or {};changes=summary.get('changes') or []
    recent={}
    for e in changes:
        c=e.get('capture') or {};r=e.get('radar_event') or {};loc=c.get('htf_location') or {}
        key=(e.get('stage'),e.get('direction'),e.get('break_ts'),c.get('liquidity_tf') or r.get('liquidity_tf'),c.get('liquidity_type') or r.get('liquidity_type'),c.get('nivel') or r.get('level'),loc.get('zone_id'))
        recent[key]=e
    lines+=['','Aconteceu · últimas 4 horas:']
    for z in report.get('notification_fvg_inventory') or []:
        touched=z.get('last_touch_close_ts')
        if touched is not None and cut-H4<touched<=cut:
            lines.append(f"• {stamp(touched)}: toque observado FVG {z['tf']} {price(z['bottom'])}–{price(z['top'])}")
    labels={'CAPTURA':'captura','RADAR_CONTEXT':'reação atualizada','M15_QUEBRA':'quebra M15','M15_ARMADO':'plano M15 pronto','M5_ARMADO':'refinamento M5','RETESTE_OBSERVADO':'reteste observado','PLANO_INVALIDADO':'plano invalidado','TOQUE_E_INVALIDACAO':'toque e invalidação','M5_ANTECIPADO':'antecipação M5'}
    selected=sorted(recent.values(),key=lambda e:e.get('observed_ts',0),reverse=True)
    for e in selected[:5]:
        c=e.get('capture') or {};r=e.get('radar_event') or {}
        extra=(f" · {c.get('liquidity_tf')} {c.get('liquidity_type')} {price(c.get('nivel'))}" if e['stage']=='CAPTURA' else '')
        lines.append(f"• {stamp(e.get('observed_ts') or e.get('known_ts'))}: {labels.get(e['stage'],e['stage'])}"+extra)
    if len(selected)>5:lines.append(f'• Mais {len(selected)-5} atualizações já notificadas; histórico preservado.')
    if not selected and not any(z.get('last_touch_close_ts') and cut-H4<z['last_touch_close_ts']<=cut for z in report.get('notification_fvg_inventory') or []):lines.append('Sem novo evento registado neste intervalo.')
    stage_labels={'AGUARDANDO_CONFIRMACAO_M15':'aguardando confirmação M15 Ludo',
      'AGUARDANDO_REACAO_CONFIRMADA':'aguardando reação confirmada após captura',
      'AGUARDANDO_REINICIALIZACAO_CAUSAL':'aguardando reinicialização causal',
      'AGUARDANDO_ZONA_OU_CAPTURA':'aguardando zona relevante ou captura',
      'AGUARDANDO_FVG_M15_CONFIRMADO':'quebra M15 confirmada; aguardando FVG',
      'AGUARDANDO_RETESTE_M15':'plano pronto; aguardando retorno M15',
      'RETESTE_M15_OBSERVADO':'reteste M15 observado',
      'RETESTE_HISTORICO_SEM_NOVA_ENTRADA':'reteste anterior; não é nova entrada',
      'SETUP_ANTERIOR_INVALIDADO':'plano anterior invalidado',
      'AGUARDANDO_POI_CAUSAL_OU_REFINAMENTO_M5':'aguardando POI causal / refinamento M5',
      'AGUARDANDO_RETESTE_M5':'aguardando reteste M5','RETESTE_M5_OBSERVADO':'reteste M5 observado'}
    lines.append('Estado: '+stage_labels.get((report.get('stage') or {}).get('code'),'estado operacional indisponível'))
    lines+=['','Agora · planos em acompanhamento:']
    plans=summary.get('plans') or []
    for p in plans:
        label='🟢 COMPRA' if p['direction']=='LONG' else '🔴 VENDA'
        status='reteste já observado; não é nova entrada' if p.get('touched') or 'RETESTE' in p.get('code','') and not p.get('code','').startswith('AGUARDANDO') else 'aguardando retorno'
        lines.append(f"• {p['ref']} · {label} · {price(p['bottom'])}–{price(p['top'])} · {status}")
    if not plans:lines.append('Sem plano de entrada pronto em acompanhamento.')
    zones=report.get('notification_fvg_inventory') or report.get('zones') or []
    lines+=['','FVGs próximos · mapa atual:']
    chosen=[]
    for tf in ('MN','W1','D1','H4','H1'):
        zs=[z for z in zones if z.get('tf')==tf and str(z.get('type','')).startswith('FVG_')]
        for direction in ('alta','baixa'):
            candidates=[z for z in zs if z.get('direction',{'FVG_bullish':'alta','FVG_bearish':'baixa'}.get(z.get('type')))==direction]
            if not candidates:continue
            z=min(candidates,key=lambda z:min(abs(z['bottom']-(px or z['bottom'])),abs(z['top']-(px or z['top']))))
            chosen.append(z)
            lines.append(f"• {tf} {'comprador' if direction=='alta' else 'vendedor'} {price(z['bottom'])}–{price(z['top'])}"+(' · preço na zona' if z.get('inside') else ''))
            from kairos_htf_fvg_alerts import confluence_line
            confluence=confluence_line(z.get('liquidity_inside'))
            if confluence:lines.append('  '+confluence)
    if not chosen:lines.append('Nenhum FVG disponível neste mapa.')
    for side,label in (('above','Acima'),('below','Abaixo')):
        lev=(report.get('nearest_available') or {}).get(side)
        if lev:lines.append(f"{label}: {price(lev['level'])} · {lev['tf']} {lev['type']}")
    lines+=['','Esperar: sinal 🎯 para um novo plano; toque/captura isolado não autoriza entrada.',
            'Capturas, toques em FVG e planos continuam com alertas imediatos.',
            'CONTEXTO · SEM TRADE · DEMO / MANUAL']
    pages=[];page=[]
    for line in lines:
        # Telegram limit is UTF-16 after HTML entities are decoded.
        if len(('\n'.join(page+[line])).encode('utf-16-le'))//2>max_chars and page:
            pages.append('\n'.join(page));page=[title+' · continuação']
        page.append(line)
    if page:pages.append('\n'.join(page))
    return pages


def fvg_inventory(maps,levels,m15,m5,cutoff):
    """Describe native zones and available confluence, never create a POI."""
    from kairos_htf_fvg_alerts import HTF_FVG_TFS,formation_close,liquidity_inside_zone
    out=[];closed=[c for c in m5 if c['t']+300000<=cutoff]
    last=closed[-1] if closed else None
    for tf in HTF_FVG_TFS:
        for z in (maps.get(tf) or {}).get('zones',[]):
            if z.get('ludo_fvg') is not True or z.get('state') not in ('ATIVA','TOCADA','PARCIAL'):continue
            if z.get('tipo') not in ('FVG_bullish','FVG_bearish'):continue
            ready=formation_close(z,tf)
            if ready is None or ready>cutoff:continue
            bull=z['tipo']=='FVG_bullish';bottom=z.get('bottom');top=z.get('top')
            if bottom is None or top is None or bottom>=top:continue
            after=[c for c in closed if c['t']>=ready]
            if any(c['l']<=bottom if bull else c['h']>=top for c in after):continue
            inside=lambda c:c['h']>=bottom and c['l']<=top
            touches=[c['t']+300000 for prev,c in zip(closed,closed[1:]) if c['t']>=ready and inside(c) and (prev['t']<ready or not inside(prev))]
            out.append(dict(last_touch_close_ts=max(touches) if touches else None,tf=tf,type=z['tipo'],id=z.get('id'),bottom=bottom,top=top,
                direction='alta' if bull else 'baixa',state=z['state'],
                inside=bool(last and bottom<=last['c']<=top),
                liquidity_inside=liquidity_inside_zone(z,levels,last['t'] if last else cutoff,m15_candles=m15,asof=cutoff)))
    return out


def brief_groups(groups):
    """Coalesce capture+reaction of the exact same origin/cycle in presentation."""
    out=[];slots={}
    for group in groups:
        for e in group['events']:
            if e['stage'] not in ('CAPTURA','RADAR_CONTEXT'):
                # Preserve existing grouping of operational proofs and origins.
                if group['events'] not in out:out.append(group['events'])
                break
            c=e.get('capture') or {};r=e.get('radar_event') or {}
            key=(e['pair'],e['observed_ts']//300000,c.get('liquidity_tf',r.get('liquidity_tf')),
                 c.get('liquidity_type',r.get('liquidity_type')),c.get('nivel',r.get('level')),
                 c.get('native_capture_confirm_ts',r.get('capture_confirm_ts')))
            if key not in slots:slots[key]=[];out.append(slots[key])
            slots[key].append(e)
    return out
