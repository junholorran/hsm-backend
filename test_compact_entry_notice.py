import copy,html
from kairos_notice_context import format_pair_update
from test_organized_alerts import proof as base_proof

def proof(*a,**kw):
    e=base_proof(*a,**kw);e["primary_policy"]="existing";return e


def render(events,context=None):
    groups=[dict(seqs=[i+1],events=[e]) for i,e in enumerate(events)]
    return [html.unescape(p) for p in format_pair_update('BNBUSD',groups,context or {},now_ts=events[-1]['observed_ts'])]


def test_long_short_are_explicit_and_never_claim_execution():
    for side,word in [('LONG','🟢 COMPRA'),('SHORT','🔴 VENDA')]:
        e=proof(stage='M15_ARMADO',direction=side);e['primary_policy']='existing'
        original=copy.deepcopy(e);pages=render([e]);text='\n'.join(pages)
        assert word in text and '🎯 KAIROS | PREPARAR ORDEM LIMITE' in text
        assert 'CHoCH LUDO 🔥' in text and 'Captura: H4 EQL @ 724,32' in text
        assert 'Zona de entrada M15: 721,50–723,04' in text
        assert 'stop exato não calculado' in text
        assert 'não coloca ordens nem confirma execução' in text and e==original


def test_same_plan_m15_and_optional_m5_are_one_message_without_context_history():
    a=proof(stage='M15_ARMADO');b=proof();a['primary_policy']=b['primary_policy']='existing'
    pages=render([a,b],{'liquidity':{'D1':dict(timestamp=1,level=999)}})
    assert len(pages)==1
    assert pages[0].count('PREPARAR ORDEM LIMITE')==1 and 'M5 opcional: 721,50–722,92' in pages[0]
    assert 'CONTEXTO DE LIQUIDEZ' not in pages[0] and 'SEQUÊNCIA INDEPENDENTE' not in pages[0]


def test_different_directions_or_parent_zones_remain_separate():
    a=proof(stage='M15_ARMADO');b=proof(stage='M15_ARMADO',direction='SHORT')
    assert len(render([a,b]))==2
    b=copy.deepcopy(a);b['parent_zone']['bottom']=720
    assert len(render([a,b]))==2


def test_no_ready_event_keeps_old_notice_and_does_not_invent_force():
    e=proof(stage='M15_QUEBRA')
    assert 'PREPARAR ORDEM LIMITE' not in '\n'.join(render([e]))
    e=proof(stage='M15_ARMADO');e.pop('primary_policy')
    assert '🔥' not in '\n'.join(render([e]))


def test_touch_origin_is_not_called_liquidity_capture():
    e=proof('h1',stage='M15_ARMADO')
    text='\n'.join(render([e]))
    assert 'Origem: toque FVG H1' in text and '💧 Captura:' not in text


def test_delayed_notice_warns_and_retest_is_never_ready():
    e=proof(stage='M15_ARMADO')
    groups=[dict(seqs=[1],events=[e])]
    text='\n'.join(format_pair_update('BNBUSD',groups,{},now_ts=e['observed_ts']+1800000))
    assert 'Aviso atrasado' in text
    assert 'PREPARAR ORDEM LIMITE' not in '\n'.join(render([proof(stage='RETESTE_OBSERVADO')]))


def test_same_cycle_invalidation_or_retest_prevents_stale_prepare_heading():
    for stage in ('PLANO_INVALIDADO','TOQUE_E_INVALIDACAO','RETESTE_OBSERVADO'):
        a=proof(stage='M15_ARMADO');b=proof(stage=stage)
        text='\n'.join(render([a,b]))
        assert 'PREPARAR ORDEM LIMITE' not in text


def test_default_clock_keeps_stale_warning(monkeypatch):
    import kairos_notice_context as n
    e=proof(stage='M15_ARMADO')
    monkeypatch.setattr(n.time,'time',lambda:(e['observed_ts']+1800000)/1000)
    assert 'Aviso atrasado' in '\n'.join(n.format_entry_notice([e]))
