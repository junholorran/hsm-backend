import copy
from kairos_daily_report import notification_parts
import test_daily_report_candidate as fixtures


def sample():
    r = fixtures.ReportTests().report([fixtures.level(value=120+i) for i in range(200)])
    r.update(structure_context={'D1':'alta','H4':'baixa','H1':'baixa'},
             current_price={'value':109,'close_ts':2*86400000},
             stage={'code':'AGUARDANDO_CONFIRMACAO_M15'},
             daily_review_required=True,
             m15_observation={'kind':'BOS','side':'DOWN','level':110,'close_ts':86400000},
             zones=[dict(tf='H1',type='FVG_bullish',bottom=106,top=108,state='ATIVA',role='CONTEXTO'),
                    dict(tf='H4',type='OB_bullish',bottom=40,top=50,state='ATIVA',role='CONTEXTO')])
    return r


def test_default_is_one_short_summary_without_inventory_or_old_bos():
    r=sample(); before=copy.deepcopy(r)
    pages=notification_parts(r)
    assert len(pages)==1
    assert len(pages[0].splitlines())<=20
    assert 'VIÉS DIÁRIO' in pages[0]
    assert 'D1 ↑ | H4 ↓ | H1 ↓' in pages[0]
    assert 'aguardando confirmação M15' in pages[0]
    assert 'H1 SWING_HIGH @ 319' not in pages[0]
    assert 'BOS DOWN' not in pages[0]
    assert 'SEM TRADE' in pages[0]
    assert r==before


def test_review_names_actual_reference_and_comparison():
    r=sample()
    text=notification_parts(r)[0]
    assert '110,00' in text
    assert 'máxima anterior' in text
    assert '≤' in text
    assert 'recuperou/perdeu' not in text


def test_full_map_remains_available_explicitly_and_paginated():
    pages=notification_parts(sample(),max_chars=1000,detailed=True)
    assert len(pages)>1
    assert sum(line.startswith('H1 SWING_HIGH @') for p in pages for line in p.splitlines())==200
    assert all(len(p)<=1000 for p in pages)


def test_unknown_stage_does_not_claim_confirmation_or_authority():
    r=sample(); r['stage']={'code':'UNRECOGNIZED'}
    text=notification_parts(r)[0]
    assert 'estado operacional indisponível' in text
    assert 'SEM TRADE' in text


def test_known_reaction_and_reset_stages_are_explained():
    for code,label in [('AGUARDANDO_REACAO_CONFIRMADA','aguardando reação confirmada'),
                       ('AGUARDANDO_REINICIALIZACAO_CAUSAL','aguardando reinicialização causal')]:
        r=sample(); r['stage']={'code':code}
        text=notification_parts(r)[0]
        assert label in text
        assert 'indisponível' not in text


def test_delivery_archives_complete_evidence_while_sending_only_summary(tmp_path,monkeypatch):
    import json
    import sqlite3
    import kairos_daily_delivery as delivery
    monkeypatch.setenv('KAIROS_DAILY_BRIEF_ENABLED','true')
    r=sample(); before=copy.deepcopy(r); messages=[]
    db_path=str(tmp_path/'daily.db')
    assert delivery.deliver(r,lambda text:messages.append(text) or True,db_path)
    assert len(messages)==1
    with sqlite3.connect(db_path) as db:
        payload=db.execute('SELECT payload FROM daily_delivery_evidence').fetchone()[0]
    assert json.loads(payload)==before
    assert len(json.loads(payload)['levels'])==200
