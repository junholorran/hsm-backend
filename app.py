from flask import Flask, request, jsonify, send_from_directory, Response
import anthropic
import os
import time
import requests
import sqlite3
import re
import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import base64
import io
from datetime import datetime, timezone
from PIL import Image, ImageDraw, ImageFont

import scalp_engine

app = Flask(__name__)
client = anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY'))

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')
DB_FILE = '/data/alerts.db'
app.config['DB_FILE'] = DB_FILE

PRECOS_TICKER = {}


# ── NOVO: status do modo Normal (CHoCH — reversão), religado no ciclo
# depois de descoberto que nunca era chamado no run_live_cycle. ──



# ── NOVO: status da camada de narrativa HTF (D1/H4/H1) — contexto,
# não gatilho. Calculado uma vez por ciclo em run_live_cycle. ──

CACHE_WINDOW_SECONDS = 15 * 60  # 15 minutos

RE_SCORE = re.compile(r'SCORE\s*OPERACIONAL\s*:[^\d]*(\d{1,3})\s*/\s*100', re.IGNORECASE)
RE_SL = re.compile(r'Stop\s*Loss\s*[^:]*:[^\d]*\$?\s*([\d,.]+)', re.IGNORECASE)
RE_TP = re.compile(r'Take\s*Profit\s*\d?\s*[^:]*:[^\d]*\$?\s*([\d,.]+)', re.IGNORECASE)
RE_ENTRY = re.compile(r'Entrada\s*Conservadora\s*[^:\n]{0,30}:[^\d]*\$?\s*([\d,.]+)', re.IGNORECASE)
RE_STYLE = re.compile(r'(scalp|swing|intraday)', re.IGNORECASE)
TIMEFRAMES_MAP = ["D1", "H4", "H1", "M15", "M5", "M1"]

RE_DIRECAO_FINAL = re.compile(r'DIRECAO_FINAL\s*:\s*(LONG|SHORT|NEUTRO)', re.IGNORECASE)
RE_SCORE_FINAL = re.compile(r'SCORE_FINAL\s*:\s*(\d{1,3})', re.IGNORECASE)
RE_ENTRY_FINAL = re.compile(r'ENTRY_FINAL\s*:\s*\$?\s*([\d,.]+)', re.IGNORECASE)
RE_SL_FINAL = re.compile(r'SL_FINAL\s*:\s*\$?\s*([\d,.]+)', re.IGNORECASE)
RE_TP1_FINAL = re.compile(r'TP1_FINAL\s*:\s*\$?\s*([\d,.]+)', re.IGNORECASE)
RE_TP2_FINAL = re.compile(r'TP2_FINAL\s*:\s*\$?\s*([\d,.]+)', re.IGNORECASE)
RE_TP3_FINAL = re.compile(r'TP3_FINAL\s*:\s*\$?\s*([\d,.]+)', re.IGNORECASE)

GOLDEN_RULES_BLOCK = (
    "\n\n---\n\n"
    "<b>🛡️ Regras de Ouro de Gestão de Risco</b>\n"
    "• Nunca arrisque mais de 1-2% do capital numa única operação\n"
    "• Sempre use Stop Loss — entre já sabendo exatamente quanto pode perder\n"
    "• Realize parcial no TP1 (50-70% da posição) e deixe o resto correr com trailing stop\n"
    "• Não opere contra a tendência principal a menos que haja sinais claros de reversão com alta confluência\n"
    "• Nunca persiga o preço — espere confirmação real antes de entrar"
)


def extract_trade_info(analysis, timeframes_str):
    if not analysis:
        return "LONG", 50, "", [], "", ""

    tfs = timeframes_str.upper() if timeframes_str else ""
    tf_components = []
    style_match = RE_STYLE.search(analysis)
    if style_match:
        tf_components.append(style_match.group(1).upper())
    found_tfs = [tf for tf in TIMEFRAMES_MAP if tf in tfs]
    tf_components.extend(found_tfs)
    tf_label = " ".join(tf_components)

    dm = RE_DIRECAO_FINAL.search(analysis)
    sm = RE_SCORE_FINAL.search(analysis)
    if dm and sm:
        direction = dm.group(1).upper()
        score = int(sm.group(1))
        if score > 100:
            score = 100
        entry_m = RE_ENTRY_FINAL.search(analysis)
        sl_m = RE_SL_FINAL.search(analysis)
        tp1_m = RE_TP1_FINAL.search(analysis)
        tp2_m = RE_TP2_FINAL.search(analysis)
        tp3_m = RE_TP3_FINAL.search(analysis)
        entry = entry_m.group(1).replace(',', '.') if entry_m else ""
        sl = sl_m.group(1).replace(',', '.') if sl_m else ""
        tps = []
        for m in (tp1_m, tp2_m, tp3_m):
            if m:
                tps.append(m.group(1).replace(',', '.'))
        return direction, score, sl, tps, tf_label, entry

    sm_old = RE_SCORE.search(analysis)
    score = int(sm_old.group(1)) if sm_old else 50
    if score > 100:
        score = 100

    window = analysis
    if sm_old:
        start = max(0, sm_old.start() - 200)
        window = analysis[start:]
    if "RECOMENDACAO FINAL" in analysis.upper():
        idx = analysis.upper().find("RECOMENDACAO FINAL")
        window = analysis[idx:idx + 600]

    wl = window.lower()
    sell_count = sum(wl.count(w) for w in ['short', 'bearish', 'venda'])
    buy_count = sum(wl.count(w) for w in ['long', 'bullish', 'compra'])
    direction = "SHORT" if sell_count > buy_count else "LONG"

    setup1_block = analysis
    if "SETUP #2" in analysis:
        setup1_block = analysis.split("SETUP #2")[0]
    sl_match = RE_SL.search(setup1_block)
    sl = sl_match.group(1).replace(',', '.') if sl_match else ""
    tp_matches = RE_TP.findall(setup1_block)
    tps = [tp.replace(',', '.') for tp in tp_matches[:3]]
    entry_match = RE_ENTRY.search(setup1_block)
    entry = entry_match.group(1).replace(',', '.') if entry_match else ""

    return direction, score, sl, tps, tf_label, entry


def init_db():
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS alerts (
                    id TEXT PRIMARY KEY,
                    pair TEXT,
                    target REAL,
                    analysis TEXT,
                    timeframes TEXT
                )
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS journal (
                    id TEXT PRIMARY KEY,
                    pair TEXT,
                    created_at INTEGER,
                    direction TEXT,
                    score INTEGER,
                    entry TEXT,
                    sl TEXT,
                    tp1 TEXT,
                    tp2 TEXT,
                    tp3 TEXT,
                    timeframes TEXT,
                    analysis TEXT,
                    status TEXT DEFAULT 'pending',
                    pnl REAL DEFAULT 0,
                    notes TEXT DEFAULT ''
                )
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS analysis_cache (
                    cache_key TEXT PRIMARY KEY,
                    pair TEXT,
                    created_at INTEGER,
                    raw_text TEXT,
                    display_text TEXT
                )
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS live_watch (
                    pair TEXT PRIMARY KEY,
                    interval_min INTEGER DEFAULT 10,
                    enabled INTEGER DEFAULT 1,
                    last_run INTEGER DEFAULT 0,
                    last_direction TEXT,
                    last_score INTEGER,
                    last_entry TEXT,
                    last_sl TEXT,
                    last_tp1 TEXT,
                    last_tp2 TEXT,
                    last_result TEXT,
                    last_alerted_signature TEXT,
                    updated_at INTEGER DEFAULT 0
                )
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS live_signals (
                    id TEXT PRIMARY KEY,
                    pair TEXT,
                    created_at INTEGER,
                    direction TEXT,
                    score INTEGER,
                    entry TEXT,
                    sl TEXT,
                    tp1 TEXT,
                    tp2 TEXT,
                    alerted INTEGER DEFAULT 0,
                    gate_teria_pulado INTEGER DEFAULT 0,
                    cascade_score INTEGER,
                    cascade_motivo TEXT
                )
            ''')
            conn.commit()

            for alter_sql in [
                "ALTER TABLE live_signals ADD COLUMN gate_teria_pulado INTEGER DEFAULT 0",
                "ALTER TABLE live_signals ADD COLUMN cascade_score INTEGER",
                "ALTER TABLE live_signals ADD COLUMN cascade_motivo TEXT",
            ]:
                try:
                    cursor.execute(alter_sql)
                    conn.commit()
                except Exception:
                    pass

        print("Base de dados SQLite inicializada com sucesso!")
    except Exception as e:
        print(f"Erro ao inicializar Base de Dados: {e}")


def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        response = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}, timeout=10)
        return response.status_code == 200 and response.json().get('ok') is True
    except Exception as e:
        print(f"Telegram erro: {e}")
        return False


def check_alerts_inline():
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id, pair, target, analysis, timeframes FROM alerts")
            db_alerts = cursor.fetchall()
        if not db_alerts:
            return
        for row in db_alerts:
            alert_id, pair, target, analysis, timeframes_str = row[0], row[1], row[2], row[3], row[4]
            if timeframes_str is None:
                timeframes_str = ""
            current_price = PRECOS_TICKER.get(pair)
            if current_price is None:
                continue
            distancia = abs(current_price - target)
            margem_tolerancia = target * 0.0015
            if distancia <= margem_tolerancia:
                with sqlite3.connect(DB_FILE) as conn_del:
                    cursor_del = conn_del.cursor()
                    cursor_del.execute("DELETE FROM alerts WHERE id = ?", (alert_id,))
                    linhas_afetadas = cursor_del.rowcount
                    conn_del.commit()
                if linhas_afetadas > 0:
                    direction, score, sl, tps, tf_label, entry = extract_trade_info(analysis, timeframes_str)
                    arrow = "📈" if direction == "LONG" else "📉"
                    emoji_score = "🟢" if score >= 75 else "🟡" if score >= 50 else "🔴"
                    msg = f"🎯 <b>{pair} ATINGIDO!</b>\n\n"
                    msg += f"{arrow} <b>{direction}</b> | {tf_label}\n"
                    msg += f"💰 <b>Preço Atual:</b> ${current_price:,.2f}\n"
                    msg += f"🎯 <b>Alvo atingido:</b> ${target:,.2f}\n"
                    msg += f"-------------------------------------\n"
                    if entry:
                        msg += f"📍 <b>Entrada Conservadora:</b> ${entry}\n"
                    if sl:
                        msg += f"🛑 <b>Stop Loss (SL):</b> ${sl}\n"
                    if tps:
                        for i, tp in enumerate(tps, 1):
                            msg += f"✅ <b>Take Profit {i} (TP{i}):</b> ${tp}\n"
                    else:
                        msg += f"✅ <b>Take Profit (TP):</b> N/A\n"
                    msg += f"-------------------------------------\n"
                    msg += f"{emoji_score} <b>Score Operacional:</b> {score}/100\n\n"
                    msg += f"💡 <i>Aguardar mitigação de FVG ou Order Block se aplicável.</i>"
                    send_telegram(msg)
    except Exception as e:
        print(f"Erro ao processar alertas inline: {e}")


init_db()
scalp_engine.init_paper_trading_v2_db(DB_FILE)
app.register_blueprint(scalp_engine.explicacao_bp)

# ─────────────────────────────────────────────────────────────────────────────
# AUDITORIA BTC — execução automática UMA VEZ por arranque
# Diagnóstico apenas. Não altera sinais, Entry, SL, TP, Telegram ou DB.
# Roda em thread separada para não bloquear o boot/live cycle.
# ─────────────────────────────────────────────────────────────────────────────
_kairos_btc_audit_started = False
_kairos_btc_audit_lock = threading.Lock()

def _run_btc_math_audit_once():
    global _kairos_btc_audit_started
    with _kairos_btc_audit_lock:
        if _kairos_btc_audit_started:
            return
        _kairos_btc_audit_started = True

    try:
        time.sleep(15)
        print("[BTC_AUDIT] INICIO auditoria matematica BTCUSD eventos=100", flush=True)
        r = scalp_engine.auditar_btc_liquidez_matematica(sample_limit=100)

        print(
            f"[BTC_AUDIT] RESUMO pair={r.get('pair')} "
            f"all_math_pass={r.get('all_math_pass')} "
            f"total_fail={r.get('total_fail')}",
            flush=True,
        )

        for tf, x in (r.get("resultado") or {}).items():
            piv = x.get("pivots") or {}
            pools = x.get("pools") or {}
            sweeps = x.get("sweeps") or {}
            poi = x.get("poi_overlap") or {}
            print(
                f"[BTC_AUDIT] {tf} "
                f"all_math_pass={x.get('all_math_pass')} "
                f"pivots={piv.get('pass',0)}/{piv.get('total',0)} fail={piv.get('fail',0)} "
                f"pools={pools.get('pass',0)}/{pools.get('total',0)} fail={pools.get('fail',0)} "
                f"sweeps={sweeps.get('pass',0)}/{sweeps.get('total',0)} fail={sweeps.get('fail',0)} "
                f"poi={poi.get('pass',0)}/{poi.get('total',0)} fail={poi.get('fail',0)}",
                flush=True,
            )

        if r.get("total_fail", 0):
            for tf, x in (r.get("resultado") or {}).items():
                samples = x.get("samples") or {}
                print(f"[BTC_AUDIT_FAIL] {tf} samples={samples}", flush=True)

        print("[BTC_AUDIT] FIM", flush=True)


    except Exception as e:
        import traceback
        print(f"[BTC_AUDIT] ERRO {type(e).__name__}: {e}", flush=True)
        traceback.print_exc()

threading.Thread(
    target=_run_btc_math_audit_once,
    name="btc-math-audit-once",
    daemon=True,
).start()

ICT_SYSTEM_PROMPT = (
    "Es um mentor institucional ICT (Inner Circle Trader) e SMC de elite, com "
    "os olhos e o raciocinio dos melhores traders profissionais do mundo. "
    "Zero tolerancia para analises vagas, ficticias ou com nivel de confianca "
    "inventado.\n\n"

    "COMO FALAR (TOM DE VOZ — OBRIGATORIO):\n"
    "- Fala como um trader senior explicando o grafico a um mentorado, nao "
    "como um relatorio ou formulario preenchido.\n"
    "- Cada camada de analise deve ser NARRADA em frases conectadas, "
    "explicando o raciocinio de uma para a outra — nao uma lista seca de "
    "campos.\n"
    "- Evita repetir a mesma estrutura robotica em cada linha. Varia a forma "
    "de comecar as frases.\n"
    "- Nao perdes nenhuma camada tecnica nem nenhum numero exato so para "
    "soar mais natural — o rigor tecnico e os valores exatos sao "
    "inegociaveis. Humanizar e na FORMA de contar, nao no conteudo.\n"
    "- Nunca uses frases vagas tipo 'na regiao de' ou 'por ai' — sempre "
    "preco exato visivel no grafico.\n\n"

    "PRINCIPIO FUNDAMENTAL — PROBABILIDADE, NUNCA PREVISAO:\n"
    "- Tu NUNCA prevês para onde o preco vai. Tu avalias PROBABILIDADE "
    "baseada em confluencia tecnica real, sempre com nivel de invalidacao "
    "(stop) definido.\n"
    "- Numeros escritos no grafico (RSI, MACD, preco, MAs) sao leitura de "
    "texto — reporta com certeza total, sem arredondar ou inventar.\n"
    "- Zonas estruturais (OB, FVG, suporte/resistencia, CHoCH) sao "
    "interpretacao tecnica competente, nao fato objetivo — trata como tal, "
    "mas com o mesmo rigor de um analista ICT senior.\n\n"

    "REGRA DE OURO ABSOLUTA:\n"
    "- NUNCA recomendar LONG quando o preco esta no topo do range diario ou em resistencia forte\n"
    "- NUNCA recomendar SHORT quando o preco esta no fundo do range diario ou em suporte forte\n"
    "- NUNCA inventar niveis — todos os valores devem ser visiveis nos graficos fornecidos\n"
    "- Se nao ha setup claro = dizer FORA DO MERCADO sem hesitar\n"
    "- Stop Loss SEMPRE atras de estrutura ICT real — NUNCA arbitrario\n"
    "- TP SEMPRE onde o mercado vai buscar liquidez real — BSL/SSL identificados\n\n"

    "RACIOCINIO EM CASCATA — OBRIGATORIO ANTES DAS 16 CAMADAS:\n"
    "Antes de entrar em qualquer camada isolada, narra o raciocinio "
    "descendo os timeframes disponiveis (D1 -> H4 -> H1 -> M15/M5), "
    "explicando o que cada timeframe mostra e por que isso importa para o "
    "proximo. Exemplo de espirito (nao copiar literalmente): 'No D1 vejo "
    "que o preco varreu a liquidez em X e reagiu, entao o bias vira Y. "
    "Descendo para H4, a estrutura confirma isso porque...'. So depois "
    "desta cascata entras nas 16 camadas detalhadas.\n\n"

    "ANALISE OBRIGATORIA - 16 CAMADAS ICT:\n"
    "1. HTF Narrative & Daily Bias (D1/W1 - tendencia macro)\n"
    "2. Liquidez Pendente (BSL e SSL com valores exatos)\n"
    "3. Premium vs Discount Zone (Fibonacci 50% — onde esta o preco agora)\n"
    "4. Order Blocks (OB bullish e bearish com zonas exatas por TF)\n"
    "5. Fair Value Gaps (FVG com zonas exatas e status: preenchido/aberto)\n"
    "6. CHoCH / MSS (confirmado ou potencial, com nivel exato)\n"
    "7. Liquidity Sweeps (varreduras recentes com valores exatos)\n"
    "8. Mitigation & Breaker Blocks\n"
    "9. Killzones & Session Patterns (Asia/London/NY — qual esta ativa)\n"
    "10. Midnight Open (valor exato e posicao do preco em relacao a ele — define bias intraday)\n"
    "11. OTE - Optimal Trade Entry (61.8%, 70.5%, 79% do swing criado pelo sweep)\n"
    "12. Wyckoff Phase (Acumulacao/Markup/Distribuicao/Markdown + Spring/UTAD)\n"
    "13. Power of Three - PO3/AMD (Accumulation/Manipulation/Distribution intraday)\n"
    "14. IFVG - Inversion Fair Value Gap (FVGs invertidos)\n"
    "15. Gatilhos de Continuidade/Reversao (ver secao propria abaixo)\n"
    "16. Divergencias RSI/MACD x preco (ver secao propria abaixo)\n\n"

    "ANCORAGEM OBRIGATORIA DE ZONAS ESTRUTURAIS (evitar adivinhacao):\n"
    "Toda vez que identificares um OB, FVG, Breaker Block ou nivel de "
    "suporte/resistencia, tens de citar a caracteristica exata da(s) "
    "vela(s) que forma(m) aquela zona — nao basta dar a zona numerica "
    "solta. Exemplo do nivel de detalhe exigido: 'OB Bearish em 61.915-"
    "62.490: ultima vela vermelha antes do rompimento, seguida de 3 velas "
    "verdes consecutivas de forte volume'. Se nao conseguires descrever a "
    "vela especifica que forma a zona, isso e sinal de que estas a "
    "adivinhar — nesse caso declara 'zona nao confirmada com clareza "
    "suficiente' em vez de reportar como certeza.\n\n"

    "CAMADA 15 — GATILHOS DE CONTINUIDADE/REVERSAO (regras de classificacao):\n"
    "- Order Block (OB) -> papel: CONTINUIDADE. Sempre reporta e sempre "
    "conta no score.\n"
    "- Fair Value Gap (FVG) -> papel: CONTINUIDADE. Sempre reporta e "
    "sempre conta no score.\n"
    "- Breaker Block -> papel: REVERSAO. So reporta e so conta no score se "
    "conseguires descrever a sequencia completa: (1) OB original, (2) "
    "rompimento do OB, (3) retorno do preco respeitando aquele nivel na "
    "direcao oposta. Se nao conseguires ver os 3 eventos com clareza, "
    "escreve 'Breaker Block nao confirmado com sequencia completa neste "
    "recorte' e NAO contas no score.\n"
    "- BPR (Balanced Price Range) -> exige 2 FVGs de polaridade oposta "
    "sobrepostos. So reporta se identificares com clareza os dois FVGs. "
    "Se identificares, reporta como informacao mas NAO conta no score "
    "(baixa confiabilidade de deteccao visual). Se nao identificares, "
    "escreve 'BPR nao identificavel com precisao neste recorte'.\n"
    "- IDM (Inducement) -> e interpretacao de intencao de mercado, nao "
    "geometria pura. Se identificares um candidato claro, reporta como "
    "informacao mas NAO conta no score. Se nao houver clareza, escreve "
    "'IDM nao identificavel com precisao neste recorte'.\n\n"

    "CAMADA 16 — DIVERGENCIAS (regra rigida, nao confundir com sobrecompra/sobrevenda):\n"
    "- Divergencia SO existe quando ha DOIS topos (ou dois fundos) "
    "comparaveis no PRECO, com o oscilador (RSI ou MACD) se movendo na "
    "direcao OPOSTA entre esses dois pontos.\n"
    "- RSI ou StochRSI sozinho em zona extrema (>70 ou <30) SEM um segundo "
    "topo/fundo para comparar NAO e divergencia — e apenas 'alerta de "
    "exaustao'. Reporta a diferenca explicitamente quando aplicavel: "
    "'sobrecompra extrema, mas sem segundo topo para confirmar divergencia' "
    "versus 'divergencia bearish confirmada: preco fez topo mais alto, RSI "
    "fez topo mais baixo'.\n"
    "- So conta no score como divergencia se a condicao dos dois topos/"
    "fundos comparaveis estiver satisfeita.\n\n"

    "CAMADA EXTRA — QUALIDADE DA LIQUIDEZ VARRIDA (pre-requisito do CHoCH/BOS):\n"
    "Antes de contar o peso do CHoCH/MSS no score, classifica a liquidez "
    "varrida que o antecedeu:\n"
    "- LIQUIDEZ FORTE (conta peso cheio): equal highs/lows com 2+ toques, "
    "swing high/low estrutural relevante (respeitado por varias velas), "
    "maxima/minima de sessao (killzone London/NY) — mesmo se de dias "
    "anteriores e ainda intocada, ou equivalente em Semanal/Diario.\n"
    "- LIQUIDEZ FRACA (NAO conta peso do CHoCH/MSS): pavio isolado sem "
    "multiplos toques, sem ser topo/fundo estrutural relevante — trata "
    "como possivel ruido, reduz a confianca da narrativa.\n"
    "- Se o sweep ocorreu dentro de uma killzone (London 07-10h ou NY "
    "13-16h, horario Portugal) ou coincide com fase de Acumulacao/"
    "Distribuicao Wyckoff no TF maior, menciona isso como reforco extra "
    "na narrativa (nao soma pontos separados, mas eleva a confianca do "
    "peso do CHoCH ja concedido).\n"
    "- Se a liquidez varrida coincide dentro de uma zona OB/FVG/iFVG ja "
    "identificada (confluencia), destaca isso explicitamente — e o "
    "gatilho de maior probabilidade do sistema.\n"
    "- Classifica tambem se o CHoCH/BOS foi de CONTINUACAO (a favor do "
    "bias D1/H4) ou REVERSAO (contra o bias anterior). Setups de reversao "
    "exigem confirmacao mais forte (liquidez forte + displacement maior) "
    "antes de contarem peso cheio.\n\n"

    "CALCULO DO SCORE — DETERMINISTICO, NUNCA POR SENSACAO:\n"
    "O SCORE_FINAL (0-100) e resultado de somar o peso de cada camada que "
    "vota, nao uma impressao geral. Estrutura de pesos (soma normalizada "
    "para 100):\n"
    "- Bias D1/H4 alinhado com a direcao = +15\n"
    "- CHoCH/MSS confirmado na direcao, PRECEDIDO de liquidez FORTE "
    "varrida (ver camada extra acima) = +15. Se o CHoCH nao foi precedido "
    "de liquidez forte, este peso cai para +5 e a narrativa deve deixar "
    "isso explicito como fator de cautela\n"
    "- Premium/Discount extremo (>70% ou <30% do range) a favor = +10\n"
    "- RSI/StochRSI sobrecomprado ou sobrevendido a favor = +10\n"
    "- MACD cruzamento confirmado a favor = +10\n"
    "- OB ativo na direcao = +10\n"
    "- FVG aberto na direcao = +10\n"
    "- Divergencia confirmada (regra rigida da Camada 16) a favor = +10\n"
    "- Breaker Block confirmado (sequencia completa) a favor = +5\n"
    "- Gatilho de SCALP M5 confirmado (candle real de M5 mostrando "
    "displacement/rejeicao clara na direcao, dentro da zona de entrada "
    "OB/FVG ja identificada) = +10. Sem candle de M5 fornecido nesta "
    "analise, ou sem gatilho claro nele, este peso e simplesmente omitido "
    "(nao soma, nao penaliza)\n"
    "- Volume do candle-chave (sweep, CHoCH ou entrada) visivelmente acima "
    "da media dos ultimos candles no mesmo grafico (ha um painel de volume "
    "desenhado abaixo do preco em cada grafico) = +5 (confirma forca real "
    "por tras do movimento). Se o volume nao estiver visivel ou for "
    "medio/baixo no candle-chave, este peso e simplesmente omitido (nao "
    "soma, nao penaliza)\n"
    "- ADR ja esgotado (>80% usado) contra novas entradas = -10 "
    "(penalizacao, nao soma para nenhum lado)\n"
    "- Preco AINDA fora da zona de entrada valida (OB/FVG) no momento "
    "desta analise = -15 (penalizacao explicita — o setup existe mas nao "
    "e acionavel agora). Se o preco JA esta dentro da zona de entrada, "
    "esta penalizacao nao se aplica\n\n"

    "OBRIGATORIO — MOSTRAR A SOMA POR EXTENSO ANTES DO SCORE_FINAL:\n"
    "Na secao SCORE OPERACIONAL da resposta, antes de declarar o numero "
    "final, escreve a soma completa e explicita de todos os pesos que "
    "contaram, no formato: '15 (bias) + 15 (CHoCH) + 10 (premium) + 10 "
    "(OB) + 10 (FVG) - 15 (fora da zona de entrada) = 45'. O numero que "
    "aparece depois do '=' TEM de ser o SCORE_FINAL usado no "
    "BLOCO_DADOS — nao pode haver diferenca entre a soma mostrada e o "
    "score reportado. E PROIBIDO ajustar o score pra cima ou pra baixo "
    "por uma razao que nao esteja na lista de pesos acima — se sentires "
    "que o score parece muito alto ou muito baixo pela tua leitura "
    "geral, a correcao tem de vir de adicionar ou remover uma linha de "
    "peso concreta da lista (ex: a penalizacao de 'fora da zona de "
    "entrada' acima), nunca de uma alteracao livre do numero final sem "
    "peso correspondente.\n\n"
    "Soma os pesos das camadas que se confirmaram na mesma direcao "
    "dominante. O resultado dessa soma, mostrada por extenso, e o "
    "SCORE_FINAL. Se LONG e SHORT tiverem pesos parecidos e nenhum "
    "ultrapassar folga clara, a direcao e NEUTRO.\n\n"

    "ANALISE TECNICA OBRIGATORIA POR TIMEFRAME:\n"
    "Para cada TF disponivel (D1, H4, H1, M15, M5) identificar:\n"
    "- RSI: valor exato + sobrecomprado (>70) / sobrevendido (<30) / neutro\n"
    "- MACD: DIF vs DEA — cruzamento bullish/bearish + divergencia se existir (aplicar regra da Camada 16)\n"
    "- Estocástico: valor exato + zona de reversao potencial\n"
    "- ADR (Average Daily Range): calcular range medio diario e quanto JA foi usado hoje — se >80% do ADR usado = NAO ENTRAR na direcao do movimento\n\n"

    "TIPO DE SETUP — IDENTIFICAR SEMPRE:\n"
    "- TENDENCIA: pullback para OB/FVG na direcao do HTF\n"
    "- REVERSAO: sweep de liquidez + CHoCH confirmado\n"
    "- CONTINUIDADE: BOS confirmado + pullback para breaker/FVG\n\n"

    "NARRATIVA ICT COMPLETA:\n"
    "1. SWEEP: qual liquidez foi varrida, onde e quando\n"
    "2. CHoCH: confirmacao com nivel exato\n"
    "3. OTE: fibonacci do swing criado pelo sweep\n"
    "4. ENTRADA: retrace para 61.8%, 70.5% ou 79% com trigger exato\n"
    "5. GESTAO POS-ENTRADA: o que esperar depois\n"
    "6. PROXIMOS ALVOS: onde o mercado vai buscar liquidez\n"
    "7. CENARIOS ALTERNATIVOS: re-sweep, invalidacao, continuacao\n\n"

    "REGRAS DE ENTRADA:\n"
    "- Entrada SEMPRE em OB ou FVG — NUNCA fora dessas zonas\n"
    "- Stop SEMPRE atras de estrutura real — explicar PORQUE aquele nivel\n"
    "- Se BSL/SSL proximo do stop = AVISAR e alargar ou NAO entrar\n"
    "- D1 bearish + setup long = AVISO CRITICO obrigatorio\n"
    "- Minimo 3 confluencias ICT para entrada\n"
    "- Probabilidade: 3=60%, 4=70%, 5=80%, 6+=90%\n"
    "- Trigger obrigatorio: Engolfo Bullish/Bearish, Pin Bar, Inside Bar no M15\n\n"

    "GATE DE RISCO/RETORNO — OBRIGATORIO, POR SETUP, NAO GLOBAL:\n"
    "O documento ja pede 3 setups por cenario (SCALP M5/M15, INTRADAY "
    "H1, SWING H4/D1). Cada um tem seu proprio Entry/SL/TP — e cada um "
    "TEM DE SER avaliado por RR de forma INDEPENDENTE, nunca misturados "
    "num score unico. Um setup pode ser viavel e outro invalido ao "
    "mesmo tempo.\n\n"
    "Para CADA setup (Scalp, Intraday, Swing), dentro do cenario LONG e "
    "dentro do cenario SHORT, calcula:\n"
    "RR = distancia(Entry, TP1) / distancia(Entry, SL)\n"
    "Mostra essa conta por extenso ao lado de cada setup (ex: \"RR "
    "Scalp: 136/492 = 0.28 — INVIAVEL\").\n\n"
    "RR MINIMO POR SETUP: 1.5.\n\n"
    "Se um setup especifico ficar abaixo de 1.5, antes de descarta-lo, "
    "tenta as duas correcoes (SL mais proximo real, ou TP mais "
    "distante real) so DENTRO daquele mesmo timeframe do setup — nao "
    "pega emprestado nivel de outro timeframe. Se ainda assim nao "
    "resolver, esse setup especifico fica marcado como \"INVIAVEL (RR "
    "[X])\" no corpo da resposta, mas isso NAO invalida os outros "
    "setups do mesmo par.\n\n"
    "REGRA DE OURO: cada setup que aparecer na resposta com "
    "Entry/SL/TP preenchidos TEM de ter RR >= 1.5 ao lado. Se nao "
    "tiver, o setup nao pode ser apresentado como executavel — ou "
    "corrige com nivel real, ou marca como INVIAVEL explicitamente.\n\n"
    "QUAL SETUP REPORTAR NO BLOCO_DADOS FINAL:\n"
    "O bloco de dados no fim da resposta (ENTRY_FINAL, SL_FINAL, etc.) "
    "reporta o setup de MAIOR PRIORIDADE que passou no RR minimo, "
    "nesta ordem de preferencia: Scalp > Intraday > Swing (prioriza a "
    "entrada mais proxima do preco atual, entre as que sao viaveis).\n"
    "Se NENHUM dos 3 setups (nem Scalp, nem Intraday, nem Swing) "
    "passar do RR minimo de 1.5, entao e so entao:\n"
    "- DIRECAO_FINAL = NEUTRO\n"
    "- SCORE_FINAL nao ultrapassa 40\n"
    "Se PELO MENOS UM setup passar, o SCORE_FINAL reflete a forca "
    "tecnica normal (soma de camadas), e o BLOCO_DADOS usa os niveis "
    "daquele setup especifico que passou — nunca mistura niveis de "
    "setups diferentes.\n\n"

    "AMARRACAO SCORE x SETUP REPORTADO — OBRIGATORIO:\n"
    "As camadas de score que dependem de UMA entrada especifica — 'OB "
    "ativo na direcao' (+10), 'FVG aberto na direcao' (+10), e 'Preco "
    "AINDA fora da zona de entrada valida' (-15) — usam SEMPRE a zona "
    "de entrada do setup que sera reportado no BLOCO_DADOS (o setup de "
    "maior prioridade que passou no RR minimo, seguindo Scalp > "
    "Intraday > Swing). Nunca usa zona de entrada de um timeframe "
    "diferente do que sera efetivamente reportado. As demais camadas "
    "(bias D1/H4, CHoCH, RSI, divergencia, ADR, etc.) continuam "
    "avaliadas para o par como um todo, independente de qual setup for "
    "reportado. Isso garante que o SCORE_FINAL nunca contradiga o "
    "proprio setup que aparece no BLOCO_DADOS.\n\n"

    "BLOCO_DADOS — OBRIGATORIO NO FIM DA RESPOSTA, SEM EXCECAO:\n"
    "Depois de toda a analise narrada, termina SEMPRE com este bloco "
    "exatamente neste formato, em texto plano (sem tags HTML dentro do "
    "bloco), cada campo numa linha propria, com estes nomes de campo "
    "EXATOS (isto e lido por codigo, nao pode variar):\n"
    "BLOCO_DADOS_INICIO\n"
    "DIRECAO_FINAL: [LONG ou SHORT ou NEUTRO]\n"
    "SCORE_FINAL: [numero de 0 a 100]\n"
    "ENTRY_FINAL: [preco exato da entrada conservadora/intraday principal]\n"
    "SL_FINAL: [preco exato do stop loss dessa entrada]\n"
    "TP1_FINAL: [preco exato]\n"
    "TP2_FINAL: [preco exato]\n"
    "TP3_FINAL: [preco exato ou deixa em branco se nao aplicavel]\n"
    "BLOCO_DADOS_FIM\n"
    "Este bloco tem de ser 100 porcento consistente com a direcao e os "
    "precos discutidos no resto da resposta. Nunca contradizer.\n\n"

    "FORMATACAO:\n"
    "- NUNCA uses markdown (* # [ ])\n"
    "- Usa APENAS tags HTML: <b> <i> <u> no corpo da analise (fora do BLOCO_DADOS)\n"
    "- Fecha todas as tags HTML abertas\n"
)
SPOT_SYSTEM_PROMPT = (
    "Es um analista de acumulacao Spot/DCA (Dollar Cost Averaging) de "
    "elite, especializado em identificar zonas de reforco de posicao em "
    "timeframes altos (Diario e Semanal). O teu trabalho NAO e timing de "
    "curto prazo — e ajudar a decidir ONDE e QUANDO reforcar uma posicao "
    "de longo prazo que a pessoa ja tem, com o minimo de risco possivel.\n\n"

    "DIFERENCA CRITICA EM RELACAO A ANALISE ICT DE CURTO PRAZO:\n"
    "- NAO uses CHoCH (Change of Character) — esse conceito e de timing de "
    "curto prazo e NAO se aplica aqui. Se pensares em CHoCH, para e "
    "reformula em termos de zona Semanal/Diaria.\n"
    "- O horizonte aqui e de MESES, nao de horas. Nao ha pressa. Se a "
    "confluencia nao estiver clara, a resposta correta e AGUARDAR, nunca "
    "forcar um sinal.\n"
    "- Tu recebes o PM (preco medio de compra) real e a quantidade que a "
    "pessoa ja tem naquele ativo. Usa isso para calibrar a tua resposta — "
    "nao repitas so a analise tecnica generica, conecta com a posicao real "
    "dela.\n\n"

    "COMO FALAR (TOM DE VOZ):\n"
    "- Fala como um analista senior de acumulacao explicando o raciocinio, "
    "nao como um formulario preenchido.\n"
    "- Narra em frases conectadas, explicando o porque de cada camada, nao "
    "lista seca de campos.\n"
    "- Nunca uses frases vagas tipo 'na regiao de' — sempre preco exato "
    "visivel no grafico.\n\n"

    "PRINCIPIO FUNDAMENTAL — PROBABILIDADE, NUNCA CERTEZA DE FUNDO:\n"
    "- Nunca afirmas que um nivel 'e o fundo'. Tu avalias se uma zona tem "
    "confluencia suficiente para justificar reforco parcial, sempre "
    "fatiado, nunca all-in.\n"
    "- Numeros escritos no grafico (RSI, BMSB, precos) sao leitura de "
    "texto — reporta com certeza total.\n"
    "- Zonas estruturais (FVG, Order Block, fundo duplo/triplo) sao "
    "interpretacao tecnica competente, nao fato objetivo.\n\n"

    "ESTRUTURA DE GATILHO EM 3 CAMADAS — OBRIGATORIA, NESTA ORDEM:\n\n"

    "CAMADA 1 — SEMANAL (ONDE, condicao obrigatoria):\n"
    "Identifica se o preco esta numa zona relevante no grafico Semanal: "
    "FVG Semanal nao mitigado, Order Block Semanal, Bull Market Support "
    "Band (BMSB — 20W SMA + 21W EMA), ou fundo duplo/triplo Semanal "
    "formado dentro dessa zona. SEM uma zona Semanal valida confirmada, "
    "NAO existe sinal de alta conviccao — a resposta tem de ser NEUTRO "
    "independente do resto, e DIRECAO_FINAL tem de ser NEUTRO.\n\n"

    "CAMADA 2 — DIARIO (QUANDO, dentro do contexto Semanal ja validado):\n"
    "Se a Camada 1 confirmou uma zona Semanal, verifica se ha um fundo "
    "duplo Diario mais recente formado dentro dessa mesma zona — isso da "
    "o timing mais fino de quando reforcar dentro da tese maior.\n\n"

    "CAMADA 3 — CONFIRMACAO DE FORCA (SE ainda ha forca vendedora saindo):\n"
    "Dentro da zona confirmada, avalia pelo menos estes fatores (conta "
    "quantos estao alinhados a favor do reforco):\n"
    "- RSI Diario/Semanal sobrevendido OU com divergencia de alta\n"
    "- MACD Semanal aproximando ou ja cruzando para alta\n"
    "- Volume caindo apos pico de capitulacao (exaustao vendedora)\n"
    "- % distancia do ATH em nivel historicamente extremo\n"
    "- Golden Cross recente ou Death Cross ja antigo perdendo forca\n"
    "Precisas de pelo menos 2-3 destes fatores alinhados para justificar "
    "reforco. Com 0-1 fator, a resposta e AGUARDAR.\n\n"

    "REGRA DOS TOQUES NA ZONA (fundo duplo/triplo — NAO e 'quanto mais, "
    "melhor'):\n"
    "- 2 a 3 toques na mesma zona = confluencia REFORCADA (demanda real, "
    "compradores defenderam o nivel repetidamente). Soma pontos ao score.\n"
    "- 4 ou mais toques = zona 'cansada'. Cada teste consome liquidez de "
    "ordens de compra da zona — mais toques significa MAIOR risco de "
    "rompimento, nao confirmacao extra. NAO soma pontos adicionais, trata "
    "como alerta de cautela na tua narrativa.\n\n"

    "CALCULO DO SCORE — DETERMINISTICO:\n"
    "- Camada 1 (zona Semanal valida) = obrigatoria. Sem isso, SCORE_FINAL "
    "maximo e 40 e DIRECAO_FINAL e NEUTRO.\n"
    "- Camada 1 valida + fundo duplo/triplo Semanal com 2-3 toques = +30\n"
    "- Camada 2 (fundo duplo Diario dentro da zona) confirmada = +20\n"
    "- Cada fator da Camada 3 alinhado (RSI/MACD/Volume/%ATH/Cross) = +10 "
    "cada (maximo +50 combinando todos)\n"
    "- 4+ toques na mesma zona = nao soma pontos extra, mencionar como "
    "cautela\n"
    "Soma tudo, limitado a 100. Se o total ficar abaixo de 60, "
    "DIRECAO_FINAL e NEUTRO mesmo que a Camada 1 tenha validado a zona — "
    "confluencia insuficiente para conviccao de reforco.\n\n"

    "REGRA DE ENTRADA FATIADA — SEMPRE, SEM EXCECAO:\n"
    "Nunca sugere uma unica entrada de tamanho total. Sugere sempre 3 "
    "fatias escalonadas dentro e abaixo da zona confirmada — a Fatia 1 na "
    "borda superior da zona, Fatia 2 no meio, Fatia 3 na borda inferior ou "
    "no nivel do fundo duplo/triplo mais forte. As fatias seguintes (2 e "
    "3) so fazem sentido SE a forca (Camada 3) continuar aparecendo na "
    "pratica — deixa isso explicito na tua narrativa.\n\n"

    "BUCKET CORE vs TACTICAL:\n"
    "- Se o ativo for do bucket Core (informado no contexto): a posicao so "
    "se reforca, nunca se vende por impulso. So sugere saida (Saida "
    "Tactical) em cenarios de exaustao compradora bem extrema (RSI "
    "sobrecomprado extremo + proximidade de resistencia historica forte + "
    "sinais de euforia) — e mesmo assim, deixa claro que e so um alerta, "
    "nao uma ordem de venda do Core.\n"
    "- Se nao houver bucket informado ou o cenario nao justificar saida, "
    "deixa TP3_FINAL vazio.\n\n"

    "USO DO PM REAL (preco medio de compra) FORNECIDO:\n"
    "Quando o PM e a quantidade da posicao existente forem fornecidos no "
    "contexto, menciona explicitamente na tua narrativa como a zona "
    "identificada se relaciona com o PM atual (ex: 'reforcar aqui desce o "
    "teu PM de $X para aproximadamente $Y' ou 'o preco atual ja esta X% "
    "abaixo do teu PM'). Nunca inventes o PM — usa exatamente o valor "
    "fornecido.\n\n"

    "BLOCO_DADOS — OBRIGATORIO NO FIM DA RESPOSTA, SEM EXCECAO:\n"
    "Termina SEMPRE com este bloco, texto plano, cada campo numa linha "
    "propria, nomes de campo EXATOS (lido por codigo, reaproveita os "
    "mesmos nomes do sistema ICT mas com o significado redefinido acima "
    "para o contexto Spot):\n"
    "BLOCO_DADOS_INICIO\n"
    "DIRECAO_FINAL: [LONG se ha sinal de reforco valido, ou NEUTRO se deve aguardar]\n"
    "SCORE_FINAL: [numero de 0 a 100, conforme a formula acima]\n"
    "ENTRY_FINAL: [preco exato da Fatia 1]\n"
    "SL_FINAL: [preco exato da invalidacao da tese — nivel onde a zona Semanal se rompe]\n"
    "TP1_FINAL: [preco exato da Fatia 2]\n"
    "TP2_FINAL: [preco exato da Fatia 3]\n"
    "TP3_FINAL: [preco exato da Saida Tactical, ou vazio se nao aplicavel]\n"
    "BLOCO_DADOS_FIM\n"
    "Este bloco tem de ser 100 porcento consistente com a narrativa do "
    "resto da resposta.\n\n"

    "FORMATACAO:\n"
    "- NUNCA uses markdown (* # [ ])\n"
    "- Usa APENAS tags HTML: <b> <i> <u> no corpo da analise (fora do BLOCO_DADOS)\n"
    "- Fecha todas as tags HTML abertas\n"
)


def build_dynamic_prompt(pair, valid_tfs):
    return (
        f"Analisa os graficos de {pair} nos timeframes ({', '.join(valid_tfs)}) com maxima precisao e objetividade.\n\n"
        "FORMATO OBRIGATORIO DA RESPOSTA — SEGUIR EXATAMENTE:\n\n"

        "<b>⚡ KAIROS MENTOR — " + pair + "</b>\n"
        "Data/Hora: [data e hora UTC]\n\n"
        "---\n\n"

        "<b>🧭 RACIOCINIO EM CASCATA (D1 → H4 → H1 → M15/M5)</b>\n"
        "[narra o raciocinio descendo os timeframes, conectando o que cada "
        "um mostra e por que importa para o proximo, em tom de trader "
        "explicando, nao lista de campos]\n\n"
        "---\n\n"

        "<b>🧭 SENTIDO DO MERCADO</b>\n"
        "<b>Dominante:</b> [📈 LONG / 📉 SHORT / ⏸ NEUTRO — FORA]\n"
        "<b>Tipo de Setup:</b> [TENDENCIA / REVERSAO / CONTINUIDADE]\n"
        "<b>Proximo Passo Logico:</b> [1-2 frases diretas — o que o mercado vai fazer]\n"
        "<b>Midnight Open:</b> [valor exato] — preco esta [ACIMA/ABAIXO] — bias [BULLISH/BEARISH]\n"
        "<b>ADR Hoje:</b> [range ja usado hoje] de [ADR medio] — [% usado] — [ENTRADA OK / CUIDADO — range quase esgotado]\n\n"
        "---\n\n"

        "<b>📉 CENARIO SHORT — Probabilidade: [X]%</b>\n"
        "<b>Condicao:</b> [o que tem de acontecer para este cenario]\n\n"
        "<b>⚡ SCALP (M5/M15):</b>\n"
        "Short: $[valor] | SL: $[valor] | TP: $[valor] | RR: 1:[x]\n"
        "<b>Trigger:</b> [vela de confirmacao obrigatoria]\n\n"
        "<b>🕐 INTRADAY (H1):</b>\n"
        "Short: $[valor] | SL: $[valor] | TP1: $[valor] | TP2: $[valor] | RR: 1:[x]\n"
        "<b>Trigger:</b> [vela de confirmacao obrigatoria]\n\n"
        "<b>📅 SWING (H4/D1):</b>\n"
        "Short: $[valor] | SL: $[valor] | TP1: $[valor] | TP2: $[valor] | TP3: $[valor] | RR: 1:[x]\n"
        "<b>Trigger:</b> [vela de confirmacao obrigatoria]\n\n"
        "<b>😴 PASSIVO (ordem limite short):</b>\n"
        "Limit Short: $[valor] | SL: $[valor] | TP: $[valor] | RR: 1:[x]\n\n"
        "<b>⚠️ Invalida SHORT se:</b> [nivel exato]\n\n"
        "---\n\n"

        "<b>📈 CENARIO LONG — Probabilidade: [X]%</b>\n"
        "<b>Condicao:</b> [o que tem de acontecer para este cenario]\n\n"
        "<b>⚡ SCALP (M5/M15):</b>\n"
        "Long: $[valor] | SL: $[valor] | TP: $[valor] | RR: 1:[x]\n"
        "<b>Trigger:</b> [vela de confirmacao obrigatoria]\n\n"
        "<b>🕐 INTRADAY (H1):</b>\n"
        "Long: $[valor] | SL: $[valor] | TP1: $[valor] | TP2: $[valor] | RR: 1:[x]\n"
        "<b>Trigger:</b> [vela de confirmacao obrigatoria]\n\n"
        "<b>📅 SWING (H4/D1):</b>\n"
        "Long: $[valor] | SL: $[valor] | TP1: $[valor] | TP2: $[valor] | TP3: $[valor] | RR: 1:[x]\n"
        "<b>Trigger:</b> [vela de confirmacao obrigatoria]\n\n"
        "<b>😴 PASSIVO (ordem limite long):</b>\n"
        "Limit Long: $[valor] | SL: $[valor] | TP: $[valor] | RR: 1:[x]\n\n"
        "<b>⚠️ Invalida LONG se:</b> [nivel exato]\n\n"
        "---\n\n"

        "<b>📊 ANALISE COMPLETA — 16 CAMADAS ICT</b>\n\n"

        "<b>BIAS DE MERCADO</b>\n"
        "- <b>D1:</b> [BULLISH/BEARISH] — RSI:[valor] MACD:[bull/bear] Estoc:[valor]\n"
        "- <b>H4:</b> [BULLISH/BEARISH] — RSI:[valor] MACD:[bull/bear] Estoc:[valor]\n"
        "- <b>H1:</b> [bias] — RSI:[valor] MACD:[bull/bear] Estoc:[valor]\n"
        "- <b>M15/M5:</b> [bias] — RSI:[valor] MACD:[bull/bear] Estoc:[valor]\n\n"

        "<b>DIVERGENCIAS (Camada 16)</b>\n"
        "[para cada timeframe onde houver dois topos/fundos comparaveis, "
        "reporta se ha ou nao divergencia confirmada segundo a regra rigida. "
        "Onde nao houver segundo topo/fundo, reporta como 'alerta de "
        "exaustao', nunca como divergencia]\n\n"

        "<b>LIQUIDEZ & ESTRUTURA</b>\n"
        "- <b>BSL:</b> $[valor] — [contexto]\n"
        "- <b>SSL:</b> $[valor] — [contexto]\n"
        "- <b>AVISO LIQUIDEZ:</b> [BSL/SSL que pode varrer stop]\n"
        "- <b>OB Bearish:</b> [zona exata] — papel: CONTINUIDADE\n"
        "- <b>OB Bullish:</b> [zona exata] — papel: CONTINUIDADE\n"
        "- <b>FVG Aberto:</b> [zona e status] — papel: CONTINUIDADE\n"
        "- <b>IFVG:</b> [zona e status]\n"
        "- <b>Breaker Block:</b> [zona se sequencia completa confirmada, "
        "senao 'nao confirmado neste recorte'] — papel: REVERSAO\n"
        "- <b>BPR:</b> [zona se identificado, senao 'nao identificavel com "
        "precisao neste recorte'] — informativo, nao conta no score\n"
        "- <b>IDM:</b> [nivel se identificado, senao 'nao identificavel com "
        "precisao neste recorte'] — informativo, nao conta no score\n"
        "- <b>CHoCH:</b> [status e nivel exato]\n\n"

        "<b>OTE — OPTIMAL TRADE ENTRY</b>\n"
        "- Swing: [low] para [high] — Range: [x] pontos\n"
        "- 61.8%: $[valor]\n"
        "- 70.5%: $[valor]\n"
        "- 79.0%: $[valor]\n"
        "- Zona OTE ideal: $[valor] — $[valor]\n\n"

        "<b>WYCKOFF + PO3/AMD</b>\n"
        "- Fase Wyckoff: [fase atual]\n"
        "- PO3: Accumulation [zona] / Manipulation [nivel] / Distribution [em curso/aguarda]\n\n"

        "<b>SCORE OPERACIONAL: [X]/100</b>\n"
        "- Confluencias ativas (com peso de cada uma): [lista]\n"
        "- Penalizacoes: [lista]\n"
        "- Soma por extenso: [ex: 15+15+10+10+10-15 = 45] — este numero "
        "TEM de ser identico ao [X] declarado no titulo desta secao e ao "
        "SCORE_FINAL do bloco de dados no fim da resposta\n\n"

        "<b>RECOMENDACAO FINAL</b>\n"
        "[narrativa completa mas objetiva — maximo 6 linhas, tom de trader "
        "explicando a decisao, nao relatorio]\n\n"
        "---\n\n"

        "BLOCO_DADOS_INICIO\n"
        "DIRECAO_FINAL: [LONG ou SHORT ou NEUTRO]\n"
        "SCORE_FINAL: [numero]\n"
        "ENTRY_FINAL: [preco exato]\n"
        "SL_FINAL: [preco exato]\n"
        "TP1_FINAL: [preco exato]\n"
        "TP2_FINAL: [preco exato]\n"
        "TP3_FINAL: [preco exato ou vazio]\n"
        "BLOCO_DADOS_FIM"
    )


def build_dynamic_spot_prompt(pair, valid_tfs, holding):
    holding_txt = "Sem posicao registada neste ativo — trata como analise exploratoria, sem PM para referenciar."
    if holding:
        pm = holding.get('pm')
        qty = holding.get('qty')
        bucket = holding.get('bucket') or 'fora do Core'
        holding_txt = (
            f"Posicao real existente: {qty} unidades, PM (preco medio de "
            f"compra) = ${pm}, bucket = {bucket}. Usa este PM real na tua "
            f"narrativa, nunca inventes outro valor."
        )

    return (
        f"Analisa os graficos SPOT/DCA de {pair} nos timeframes "
        f"({', '.join(valid_tfs)}) com maxima precisao e objetividade, "
        "seguindo a estrutura de 3 camadas (Semanal define ONDE, Diario "
        "define QUANDO, indicadores confirmam SE ainda ha forca).\n\n"
        f"CONTEXTO DA POSICAO ATUAL: {holding_txt}\n\n"
        "FORMATO OBRIGATORIO DA RESPOSTA — SEGUIR EXATAMENTE:\n\n"

        "<b>🟢 KAIROS MENTOR SPOT — " + pair + "</b>\n"
        "Data/Hora: [data e hora UTC]\n\n"
        "---\n\n"

        "<b>💰 SUA POSICAO</b>\n"
        "[se houver PM/quantidade no contexto, resume aqui: quanto tem, "
        "PM atual, e a que distancia percentual o preco de hoje esta desse "
        "PM. Se nao houver posicao, diz isso claramente.]\n\n"
        "---\n\n"

        "<b>🧭 CAMADA 1 — SEMANAL (ONDE)</b>\n"
        "[identifica FVG/OB/BMSB/fundo duplo-triplo Semanal, com precos "
        "exatos. Se nao houver zona valida, declara isso explicitamente e "
        "explica que sem isso a resposta e AGUARDAR]\n\n"

        "<b>📅 CAMADA 2 — DIARIO (QUANDO)</b>\n"
        "[fundo duplo Diario dentro da zona Semanal, se houver, com preco "
        "exato]\n\n"

        "<b>📊 CAMADA 3 — CONFIRMACAO DE FORCA (SE)</b>\n"
        "[lista cada fator avaliado — RSI, MACD, Volume, %ATH, Golden/Death "
        "Cross — dizendo se esta alinhado a favor do reforco ou nao, e "
        "quantos no total estao alinhados]\n\n"

        "<b>🔁 TOQUES NA ZONA</b>\n"
        "[quantos toques identificados na zona principal, e se isso reforca "
        "(2-3) ou exige cautela (4+)]\n\n"
        "---\n\n"

        "<b>🎯 SINAL: [REFORCAR / AGUARDAR]</b>\n"
        "<b>Score de Confluencia:</b> [X]/100\n\n"

        "<b>Entrada Escalonada (nunca all-in):</b>\n"
        "Fatia 1: $[valor] | Fatia 2: $[valor] | Fatia 3: $[valor]\n"
        "[explica brevemente por que cada fatia esta onde esta, e deixa "
        "claro que a Fatia 2 e 3 so entram se a forca continuar se "
        "confirmando na pratica]\n\n"

        "<b>Invalidacao da Tese:</b> $[valor] — [explica o que muda se "
        "romper esse nivel]\n\n"

        "<b>Saida Tactical (se aplicavel):</b> [preco exato ou 'nao "
        "aplicavel — bucket Core, nao se vende por impulso']\n\n"
        "---\n\n"

        "<b>RESUMO FINAL</b>\n"
        "[narrativa objetiva, maximo 5 linhas, conectando a leitura tecnica "
        "com a posicao real da pessoa]\n\n"
        "---\n\n"

        "BLOCO_DADOS_INICIO\n"
        "DIRECAO_FINAL: [LONG ou NEUTRO]\n"
        "SCORE_FINAL: [numero]\n"
        "ENTRY_FINAL: [preco exato — Fatia 1]\n"
        "SL_FINAL: [preco exato — Invalidacao]\n"
        "TP1_FINAL: [preco exato — Fatia 2]\n"
        "TP2_FINAL: [preco exato — Fatia 3]\n"
        "TP3_FINAL: [preco exato — Saida Tactical, ou vazio]\n"
        "BLOCO_DADOS_FIM"
    )


def compute_cache_key(pair, images_by_tf):
    hasher = hashlib.sha256()
    hasher.update(pair.encode('utf-8'))
    for tf in sorted(images_by_tf.keys()):
        img = images_by_tf[tf]
        if img and isinstance(img, dict) and img.get('base64'):
            hasher.update(tf.encode('utf-8'))
            hasher.update(img['base64'].encode('utf-8'))
    return hasher.hexdigest()


def get_cached_analysis(cache_key):
    try:
        cutoff = int(time.time()) - CACHE_WINDOW_SECONDS
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute(
                'SELECT raw_text, display_text FROM analysis_cache WHERE cache_key = ? AND created_at >= ?',
                (cache_key, cutoff)
            )
            row = cursor.fetchone()
        if row:
            return row[0], row[1]
    except Exception as e:
        print(f"Erro ao ler cache: {e}")
    return None


def save_cache(cache_key, pair, raw_text, display_text):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute(
                'INSERT OR REPLACE INTO analysis_cache (cache_key, pair, created_at, raw_text, display_text) VALUES (?, ?, ?, ?, ?)',
                (cache_key, pair, int(time.time()), raw_text, display_text)
            )
            cutoff = int(time.time()) - CACHE_WINDOW_SECONDS
            cursor.execute('DELETE FROM analysis_cache WHERE created_at < ?', (cutoff,))
            conn.commit()
    except Exception as e:
        print(f"Erro ao salvar cache: {e}")


def analyze_single_pair(pair, images_by_tf, category='ict', holding=None):
    valid_tfs = [tf for tf, img in images_by_tf.items() if img and isinstance(img, dict) and img.get('base64')]
    if len(valid_tfs) < 2 and category != 'spot':
        return None, None, f"Par {pair} precisa de pelo menos 2 graficos"
    if len(valid_tfs) < 1:
        return None, None, f"Par {pair} precisa de pelo menos 1 grafico"

    cache_key = compute_cache_key(pair + '_' + category, images_by_tf)
    cached = get_cached_analysis(cache_key)
    if cached:
        raw_text, display_text = cached
        return raw_text, display_text, None

    if category == 'spot':
        dynamic_prompt = build_dynamic_spot_prompt(pair, valid_tfs, holding)
        system_prompt = SPOT_SYSTEM_PROMPT
    else:
        dynamic_prompt = build_dynamic_prompt(pair, valid_tfs)
        system_prompt = ICT_SYSTEM_PROMPT

    content = []
    for tf in valid_tfs:
        img = images_by_tf[tf]
        b64_data = img['base64']
        if "," in b64_data:
            b64_data = b64_data.split(",")[-1]
        b64_data = b64_data.strip().replace("\n", "").replace("\r", "")
        mime = img.get('mimeType', 'image/jpeg') or 'image/jpeg'
        content.append({"type": "text", "text": f"Grafico {tf}:"})
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": mime, "data": b64_data}
        })
    content.append({"type": "text", "text": dynamic_prompt})

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=6000,
        temperature=0,
        system=[{
            "type": "text",
            "text": system_prompt,
            "cache_control": {"type": "ephemeral", "ttl": "1h"}
        }],
        messages=[{"role": "user", "content": content}]
    )

    raw_text = response.content[0].text

    display_text = raw_text
    if "BLOCO_DADOS_INICIO" in raw_text:
        display_text = raw_text.split("BLOCO_DADOS_INICIO")[0].rstrip()
        display_text = display_text.rstrip("-").rstrip()

    display_text = display_text + build_news_block(limit=4)

    display_text = display_text + GOLDEN_RULES_BLOCK

    save_cache(cache_key, pair, raw_text, display_text)

    return raw_text, display_text, None


LIVE_SYMBOL_MAP = {
    'BTCUSD': 'BTCUSDT', 'ETHUSD': 'ETHUSDT', 'SOLUSD': 'SOLUSDT', 'XRPUSD': 'XRPUSDT',
    'LINKUSD': 'LINKUSDT', 'ADAUSD': 'ADAUSDT', 'AVAXUSD': 'AVAXUSDT', 'BNBUSD': 'BNBUSDT',
    'AAVEUSD': 'AAVEUSDT', 'ONDOUSD': 'ONDOUSDT', 'INJUSD': 'INJUSDT', 'NEARUSD': 'NEARUSDT',
    'PENDLEUSD': 'PENDLEUSDT', 'SUIUSD': 'SUIUSDT', 'JTOUSD': 'JTOUSDT', 'ETHFIUSD': 'ETHFIUSDT',
    'JUPUSD': 'JUPUSDT', 'ENAUSD': 'ENAUSDT',
    'OPUSD': 'OPUSDT', 'RENDERUSD': 'RENDERUSDT', 'RUNEUSD': 'RUNEUSDT',
    'TAOUSD': 'TAOUSDT', 'TIAUSD': 'TIAUSDT', 'VIRTUALUSD': 'VIRTUALUSDT',
    'FILUSD': 'FILUSDT', 'HBARUSD': 'HBARUSDT', 'ICPUSD': 'ICPUSDT',
    'LTCUSD': 'LTCUSDT', 'ATOMUSD': 'ATOMUSDT', 'ENSUSD': 'ENSUSDT', 'FETUSD': 'FETUSDT',
}
def fetch_bybit_klines(symbol, interval, limit=200):
    url = 'https://api.bybit.com/v5/market/kline'
    params = {'category': 'linear', 'symbol': symbol, 'interval': interval, 'limit': limit}
    headers = {'User-Agent': 'Mozilla/5.0 (compatible; KairosMentor/1.0)'}
    r = requests.get(url, params=params, headers=headers, timeout=15)
    try:
        data = r.json()
    except Exception:
        print(f"[bybit-diag] status={r.status_code} body_start={r.text[:300]!r}")
        raise Exception(f"resposta não-JSON da Bybit (status {r.status_code}) — provável bloqueio de IP server-side")
    lst = (data.get('result') or {}).get('list') or []
    if len(lst) < 5:
        raise Exception(f'sem candles suficientes para {symbol} — resposta: {str(data)[:200]}')
    candles = [{
        't': int(k[0]), 'o': float(k[1]), 'h': float(k[2]), 'l': float(k[3]), 'c': float(k[4]),
        'v': float(k[5]) if len(k) > 5 else 0.0
    } for k in lst]
    candles.reverse()
    return candles


def compute_sma(values, period):
    out = [None] * len(values)
    if len(values) < period:
        return out
    s = sum(values[:period])
    out[period - 1] = s / period
    for i in range(period, len(values)):
        s += values[i] - values[i - period]
        out[i] = s / period
    return out


def render_live_chart_png_base64(candles, pair_label, tf_label, scalp_result=None):
    W, H = 900, 570
    padL, padR, padT, padB = 60, 20, 40, 30
    priceH = 400
    volH = 90
    volGap = 20
    volTop = padT + priceH + volGap
    img = Image.new('RGB', (W, H), (10, 10, 15))
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.load_default()
    except Exception:
        font = None

    closes = [c['c'] for c in candles]
    highs = [c['h'] for c in candles]
    lows = [c['l'] for c in candles]
    volumes = [c.get('v', 0) for c in candles]
    max_p, min_p = max(highs), min(lows)
    rng = (max_p - min_p) or 1
    plot_w = W - padL - padR
    cw = plot_w / len(candles)

    def x_for(i):
        return padL + i * cw + cw / 2

    def y_for(p):
        return padT + priceH - ((p - min_p) / rng) * priceH

    for i in range(5):
        yy = padT + (priceH / 4) * i
        draw.line([(padL, yy), (W - padR, yy)], fill=(42, 42, 58), width=1)
        price_at_y = max_p - (rng / 4) * i
        draw.text((4, yy - 5), f"{price_at_y:.2f}", fill=(110, 118, 129), font=font)

    if scalp_result:
        preco_atual_fundo = closes[-1]
        y_preco = y_for(preco_atual_fundo)
        fundo_overlay = Image.new('RGBA', (W, H), (0, 0, 0, 0))
        fundo_draw = ImageDraw.Draw(fundo_overlay)
        fundo_draw.rectangle([padL, y_preco, W - padR, padT + priceH], fill=(63, 185, 80, 22))
        fundo_draw.rectangle([padL, padT, W - padR, y_preco], fill=(248, 81, 73, 22))
        fundo_draw.line([(padL, y_preco), (W - padR, y_preco)], fill=(240, 192, 64, 160), width=1)
        img.paste(fundo_overlay, (0, 0), fundo_overlay)

    for i, c in enumerate(candles):
        x = x_for(i)
        up = c['c'] >= c['o']
        color = (63, 185, 80) if up else (248, 81, 73)
        draw.line([(x, y_for(c['h'])), (x, y_for(c['l']))], fill=color, width=1)
        body_top = y_for(max(c['o'], c['c']))
        body_bot = y_for(min(c['o'], c['c']))
        half = max(1, cw * 0.35)
        draw.rectangle([x - half, body_top, x + half, max(body_bot, body_top + 1)], fill=color)

    ma_specs = [(25, (95, 217, 104)), (50, (227, 179, 65)), (100, (255, 152, 0)), (200, (188, 140, 255))]
    for period, color in ma_specs:
        ma = compute_sma(closes, period)
        pts = [(x_for(i), y_for(v)) for i, v in enumerate(ma) if v is not None]
        if len(pts) >= 2:
            draw.line(pts, fill=color, width=2)

    if scalp_result:
        _draw_scalp_overlays(img, draw, scalp_result, y_for, W, H, padR, font, preco_atual=closes[-1])

    max_vol = max(volumes) if volumes and max(volumes) > 0 else 1
    avg_vol = (sum(volumes) / len(volumes)) if volumes else 0
    draw.line([(padL, volTop), (W - padR, volTop)], fill=(42, 42, 58), width=1)
    draw.line([(padL, volTop + volH), (W - padR, volTop + volH)], fill=(42, 42, 58), width=1)
    for i, c in enumerate(candles):
        x = x_for(i)
        vol = c.get('v', 0)
        bar_h = (vol / max_vol) * volH if max_vol > 0 else 0
        up = c['c'] >= c['o']
        is_above_avg = vol > avg_vol * 1.3
        if up:
            color = (63, 185, 80) if is_above_avg else (45, 110, 58)
        else:
            color = (248, 81, 73) if is_above_avg else (140, 60, 58)
        half = max(1, cw * 0.35)
        draw.rectangle([x - half, volTop + volH - bar_h, x + half, volTop + volH], fill=color)
    draw.text((padL, volTop - 14), "VOLUME (barras vivas = acima da média)", fill=(150, 150, 160), font=font)

    last_close = candles[-1]['c']
    draw.text((padL, 10), f"{pair_label} · {tf_label} · ${last_close:,.2f}", fill=(240, 192, 64), font=font)
    stamp = 'GERADO EM: ' + datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M') + ' UTC'
    draw.text((W - padR - 220, 10), stamp, fill=(255, 229, 138), font=font)

    buf = io.BytesIO()
    img.save(buf, format='PNG')
    return base64.b64encode(buf.getvalue()).decode('utf-8')


def _draw_scalp_overlays(img, draw, resultado, y_for, W, H, padR, font, preco_atual=None):
    x_end = W - padR

    if resultado.get('zona_top') is not None and resultado.get('zona_bottom') is not None:
        zona_top = resultado['zona_top']
        zona_bottom = resultado['zona_bottom']
        y_top = y_for(zona_top)
        y_bottom = y_for(zona_bottom)

        if resultado.get('zona_ativa') or preco_atual is None:
            cor_rgb = (227, 179, 65)
            label_papel = "ZONA D1 (ativa)"
        elif preco_atual > zona_top:
            cor_rgb = (63, 185, 80)
            label_papel = "ZONA D1 (suporte)"
        elif preco_atual < zona_bottom:
            cor_rgb = (248, 81, 73)
            label_papel = "ZONA D1 (resistência)"
        else:
            cor_rgb = (150, 150, 160)
            label_papel = "ZONA D1"

        overlay = Image.new('RGBA', (W, H), (0, 0, 0, 0))
        overlay_draw = ImageDraw.Draw(overlay)
        overlay_draw.rectangle(
            [0, min(y_top, y_bottom), x_end, max(y_top, y_bottom)],
            fill=(*cor_rgb, 45),
            outline=(*cor_rgb, 255),
            width=1,
        )
        img.paste(overlay, (0, 0), overlay)
        draw.text((6, min(y_top, y_bottom) - 12), label_papel, fill=cor_rgb, font=font)

    if resultado.get('sweep_nivel') is not None:
        y_sweep = y_for(resultado['sweep_nivel'])
        cor = (248, 81, 73) if resultado.get('sweep_lado') == 'alta' else (63, 185, 80)
        for x in range(0, x_end, 8):
            draw.line([(x, y_sweep), (x + 4, y_sweep)], fill=cor, width=1)
        label = f"Sweep {resultado.get('sweep_lado', '')} {resultado['sweep_nivel']:.2f}"
        draw.rectangle([x_end - 160, y_sweep - 10, x_end, y_sweep + 10], fill=cor)
        draw.text((x_end - 156, y_sweep - 6), label[:24], fill=(10, 10, 15), font=font)

    if resultado.get('choch_nivel') is not None:
        y_choch = y_for(resultado['choch_nivel'])
        cor = (63, 185, 80) if resultado.get('choch_direcao') == 'alta' else (248, 81, 73)
        draw.line([(0, y_choch), (x_end, y_choch)], fill=cor, width=2)
        label = f"CHoCH {resultado.get('choch_direcao', '')}"
        draw.rectangle([x_end - 150, y_choch - 22, x_end, y_choch - 2], fill=cor)
        draw.text((x_end - 146, y_choch - 18), label, fill=(10, 10, 15), font=font)

    if resultado.get('entry_zone_top') is not None and resultado.get('entry_zone_bottom') is not None:
        y_top = y_for(resultado['entry_zone_top'])
        y_bottom = y_for(resultado['entry_zone_bottom'])
        cor = (240, 192, 64)
        draw.rectangle([0, min(y_top, y_bottom), x_end, max(y_top, y_bottom)], outline=cor, width=2)
        tipo = resultado.get('entry_zone_tipo', 'Entrada')
        draw.text((6, min(y_top, y_bottom) - 12), f"Entrada ({tipo})", fill=cor, font=font)


def save_scalp_signal_to_journal(pair, modo_label, exec_tf, direcao, score, entry, sl, tp, motivo):
    if entry is None or sl is None or tp is None or not direcao:
        return
    try:
        journal_id = f"scalp_{modo_label}_{pair}_{int(time.time()*1000)}"
        direction_label = 'LONG' if direcao == 'alta' else 'SHORT'
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO journal (id, pair, created_at, direction, score, entry, sl, tp1, tp2, tp3, timeframes, analysis, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                journal_id, pair, int(time.time()),
                direction_label, int(score or 0),
                str(entry), str(sl), str(tp), '', '',
                exec_tf or '', f"[Scalp — {modo_label}] {motivo or ''}", 'pending'
            ))
            conn.commit()
    except Exception as e:
        print(f"[journal] erro ao salvar sinal de scalp ({modo_label}, {pair}): {e}")


def resolve_pending_journal_trades(pair, candles):
    if not candles:
        return
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, direction, entry, sl, tp1, created_at FROM journal WHERE pair=? AND status='pending'",
                (pair,)
            )
            pendentes = cursor.fetchall()
        if not pendentes:
            return

        for trade_id, direction, entry_s, sl_s, tp1_s, created_at in pendentes:
            try:
                entry = float(str(entry_s).replace(',', '.'))
                sl = float(str(sl_s).replace(',', '.'))
                tp1 = float(str(tp1_s).replace(',', '.'))
            except (TypeError, ValueError):
                continue
            if not entry or not sl or not tp1:
                continue

            created_at_ms = (created_at or 0) * 1000
            candles_apos = [c for c in candles if c['t'] >= created_at_ms]
            if not candles_apos:
                continue

            resultado = None
            is_long = (direction or '').upper() == 'LONG'
            for c in candles_apos:
                if is_long:
                    if c['l'] <= sl:
                        resultado = 'loss'
                        break
                    if c['h'] >= tp1:
                        resultado = 'win'
                        break
                else:
                    if c['h'] >= sl:
                        resultado = 'loss'
                        break
                    if c['l'] <= tp1:
                        resultado = 'win'
                        break

            if resultado:
                risco_pct = abs(entry - sl) / entry * 100 if entry else 0
                retorno_pct = abs(entry - tp1) / entry * 100 if entry else 0
                pnl_pct = retorno_pct if resultado == 'win' else -risco_pct
                try:
                    with sqlite3.connect(DB_FILE) as conn2:
                        conn2.execute(
                            'UPDATE journal SET status=?, pnl=? WHERE id=?',
                            (resultado, round(pnl_pct, 2), trade_id)
                        )
                        conn2.commit()
                except Exception as e:
                    print(f"[journal] erro ao resolver trade {trade_id}: {e}")
    except Exception as e:
        print(f"[journal] erro ao checar pendentes de {pair}: {e}")
# ═══════════════════════════════════════════════════════════════════════
# PAPER TRADING V2 — SCHEDULER AUTOMÁTICO (aditivo, sem tocar em nada
# do que já existia acima). Reaproveita exatamente o mesmo padrão do
# live_scheduler_loop: thread daemon própria, loop com sleep(30), só
# que com um controle de intervalo interno pra rodar o tick a cada
# PAPER_TICK_INTERVAL_SECONDS em vez de todo ciclo de 30s. Chama
# scalp_engine.paper_trading_v2_tick_todos_pares() diretamente (função
# já existente, sem alteração nenhuma) — não passa pela rota HTTP
# protegida por secret, é chamada interna do próprio processo.
# ═══════════════════════════════════════════════════════════════════════

_ultimo_paper_tick_ts = 0
PAPER_TICK_INTERVAL_SECONDS = 5 * 60  # a cada 5 minutos


def paper_tick_scheduler_loop():
    global _ultimo_paper_tick_ts
    while True:
        try:
            now = int(time.time())
            if (now - _ultimo_paper_tick_ts) >= PAPER_TICK_INTERVAL_SECONDS:
                resultado = scalp_engine.paper_trading_v2_tick_todos_pares(DB_FILE)
                _ultimo_paper_tick_ts = now
                print(f"[paper_trading_v2] tick automático concluído: {resultado}")
        except Exception as e:
            print(f"[paper_trading_v2] erro no tick automático: {e}")
        time.sleep(30)


@app.route('/')
def index():
    return send_from_directory('.', 'index.html')


@app.route('/update_prices', methods=['POST'])
def update_prices():
    try:
        dados = request.json or {}
        for pair, price in dados.items():
            if price is not None:
                PRECOS_TICKER[pair] = float(price)
        check_alerts_inline()
        return jsonify({'ok': True, 'prices_stored': len(PRECOS_TICKER)})
    except Exception as e:
        return jsonify({'error': str(e)}), 400


@app.route('/analyze', methods=['POST'])
def analyze():
    try:
        data = request.json or {}
        pair = data.get('pair', 'BTCUSD')
        images = data.get('images', {})
        valid_tfs = [tf for tf, img in images.items() if img and isinstance(img, dict) and img.get('base64')]
        if len(valid_tfs) < 2:
            return jsonify({'error': 'Carrega pelo menos 2 graficos validos!'}), 400

        raw_text, display_text, error = analyze_single_pair(pair, images)
        if error:
            return jsonify({'error': error}), 400

        direction, score, sl, tps, tf_label, entry = extract_trade_info(raw_text, ','.join(valid_tfs))
        journal_id = f"{pair}_{int(time.time() * 1000)}"
        try:
            with sqlite3.connect(DB_FILE) as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT INTO journal (id, pair, created_at, direction, score, entry, sl, tp1, tp2, tp3, timeframes, analysis, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    journal_id, pair, int(time.time()),
                    direction, score, entry, sl,
                    tps[0] if len(tps) > 0 else '',
                    tps[1] if len(tps) > 1 else '',
                    tps[2] if len(tps) > 2 else '',
                    ','.join(valid_tfs), raw_text, 'pending'
                ))
                conn.commit()
        except Exception as e:
            print(f"Erro ao guardar no journal: {e}")

        return jsonify({
            'result': display_text,
            'timeframes': ','.join(valid_tfs),
            'journal_id': journal_id,
            'score': score,
            'direction': direction,
            'entry': entry,
            'sl': sl,
            'tp1': tps[0] if len(tps) > 0 else '',
            'tp2': tps[1] if len(tps) > 1 else '',
            'tp3': tps[2] if len(tps) > 2 else '',
        })
    except Exception as e:
        return jsonify({'error': f"Erro na API Anthropic: {str(e)}"}), 500


@app.route('/analyze_multi', methods=['POST'])
def analyze_multi():
    try:
        data = request.json or {}
        pairs_data = data.get('pairs', {})
        category = data.get('category', 'ict')
        holding = data.get('holding')

        if not pairs_data:
            return jsonify({'error': 'Nenhum par recebido'}), 400

        results = []
        errors = []

        for pair, images_by_tf in pairs_data.items():
            try:
                raw_text, display_text, error = analyze_single_pair(pair, images_by_tf, category=category, holding=holding)
                if error:
                    errors.append({'pair': pair, 'error': error})
                    continue

                valid_tfs = [tf for tf, img in images_by_tf.items() if img and isinstance(img, dict) and img.get('base64')]
                direction, score, sl, tps, tf_label, entry = extract_trade_info(raw_text, ','.join(valid_tfs))
                journal_id = f"{pair}_{int(time.time() * 1000)}"

                try:
                    with sqlite3.connect(DB_FILE) as conn:
                        cursor = conn.cursor()
                        cursor.execute('''
                            INSERT INTO journal (id, pair, created_at, direction, score, entry, sl, tp1, tp2, tp3, timeframes, analysis, status)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ''', (
                            journal_id, pair, int(time.time()),
                            direction, score, entry, sl,
                            tps[0] if len(tps) > 0 else '',
                            tps[1] if len(tps) > 1 else '',
                            tps[2] if len(tps) > 2 else '',
                            ','.join(valid_tfs), raw_text, 'pending'
                        ))
                        conn.commit()
                except Exception as e:
                    print(f"Erro ao guardar journal {pair}: {e}")

                results.append({
                    'pair': pair,
                    'result': display_text,
                    'timeframes': ','.join(valid_tfs),
                    'journal_id': journal_id,
                    'score': score,
                    'direction': direction,
                    'entry': entry,
                    'sl': sl,
                    'tp1': tps[0] if len(tps) > 0 else '',
                    'tp2': tps[1] if len(tps) > 1 else '',
                    'tp3': tps[2] if len(tps) > 2 else '',
                })

            except Exception as e:
                errors.append({'pair': pair, 'error': str(e)})

        return jsonify({'results': results, 'errors': errors})

    except Exception as e:
        return jsonify({'error': f"Erro geral: {str(e)}"}), 500


@app.route('/set_alert', methods=['POST'])
def set_alert():
    try:
        data = request.json or {}
        pair = data.get('pair', 'BTCUSD')
        target = float(data.get('target'))
        analysis = data.get('analysis', '')
        timeframes = data.get('timeframes', '')
        current_price = PRECOS_TICKER.get(pair)
        if not current_price:
            current_price = float(data.get('current_price', 0))
        alert_unique_id = f"{pair}_{target}_{int(time.time() * 1000)}"
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO alerts (id, pair, target, analysis, timeframes) VALUES (?, ?, ?, ?, ?)",
                (alert_unique_id, pair, target, analysis, timeframes)
            )
            conn.commit()
        send_telegram(f"<b>Alerta Gravado para {pair}</b>\nAlvo: ${target:,.2f}\nPreco atual: ${current_price:,.2f}")
        return jsonify({'ok': True, 'current_price': current_price})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/journal', methods=['GET'])
def get_journal():
    try:
        pair_filter = request.args.get('pair', '')
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            if pair_filter:
                cursor.execute('SELECT id, pair, created_at, direction, score, entry, sl, tp1, tp2, tp3, timeframes, status, pnl FROM journal WHERE pair=? ORDER BY created_at DESC LIMIT 100', (pair_filter,))
            else:
                cursor.execute('SELECT id, pair, created_at, direction, score, entry, sl, tp1, tp2, tp3, timeframes, status, pnl FROM journal ORDER BY created_at DESC LIMIT 100')
            rows = cursor.fetchall()
        trades = []
        for r in rows:
            trades.append({
                'id': r[0], 'pair': r[1], 'created_at': r[2],
                'direction': r[3], 'score': r[4], 'entry': r[5],
                'sl': r[6], 'tp1': r[7], 'tp2': r[8], 'tp3': r[9],
                'timeframes': r[10], 'status': r[11], 'pnl': r[12]
            })
        return jsonify({'trades': trades})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/journal/update', methods=['POST'])
def update_journal():
    try:
        data = request.json or {}
        trade_id = data.get('id')
        status = data.get('status')
        pnl = float(data.get('pnl', 0))
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute('UPDATE journal SET status=?, pnl=? WHERE id=?', (status, pnl, trade_id))
            conn.commit()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/journal/stats', methods=['GET'])
def journal_stats():
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT status, pnl, score, pair FROM journal WHERE status != "pending" AND status != "cancelled"')
            rows = cursor.fetchall()
        total_trades = len(rows)
        wins = sum(1 for r in rows if r[0] == 'win')
        losses = sum(1 for r in rows if r[0] == 'loss')
        total_pnl = sum(r[1] for r in rows)
        win_rate = round((wins / total_trades * 100), 1) if total_trades > 0 else 0
        score_brackets = {'75-100': {'w': 0, 'l': 0}, '60-74': {'w': 0, 'l': 0}, '50-59': {'w': 0, 'l': 0}}
        for r in rows:
            s = r[2]
            if s >= 75:
                bracket = '75-100'
            elif s >= 60:
                bracket = '60-74'
            else:
                bracket = '50-59'
            if r[0] == 'win':
                score_brackets[bracket]['w'] += 1
            else:
                score_brackets[bracket]['l'] += 1
        pair_stats = {}
        for r in rows:
            p = r[3]
            if p not in pair_stats:
                pair_stats[p] = {'w': 0, 'l': 0, 'pnl': 0}
            pair_stats[p]['pnl'] += r[1]
            if r[0] == 'win':
                pair_stats[p]['w'] += 1
            else:
                pair_stats[p]['l'] += 1
        return jsonify({
            'total_trades': total_trades, 'wins': wins, 'losses': losses,
            'total_pnl': round(total_pnl, 2), 'win_rate': win_rate,
            'score_brackets': score_brackets, 'pair_stats': pair_stats
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/live/watch', methods=['POST'])
def live_watch_start():
    try:
        data = request.json or {}
        pair = data.get('pair')
        interval_min = int(data.get('interval_min', 10))
        if not pair:
            return jsonify({'error': 'pair obrigatório'}), 400
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT pair FROM live_watch WHERE pair=?', (pair,))
            exists = cursor.fetchone()
            if exists:
                cursor.execute(
                    'UPDATE live_watch SET interval_min=?, enabled=1 WHERE pair=?',
                    (interval_min, pair)
                )
            else:
                cursor.execute(
                    'INSERT INTO live_watch (pair, interval_min, enabled, last_run) VALUES (?, ?, 1, 0)',
                    (pair, interval_min)
                )
            conn.commit()
        return jsonify({'ok': True, 'pair': pair, 'interval_min': interval_min})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/live/unwatch', methods=['POST'])
def live_watch_stop():
    try:
        data = request.json or {}
        pair = data.get('pair')
        if not pair:
            return jsonify({'error': 'pair obrigatório'}), 400
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute('UPDATE live_watch SET enabled=0 WHERE pair=?', (pair,))
            conn.commit()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


NEWS_RSS_FEEDS = [
    'https://pt.investing.com/rss/news_301.rss',
    'https://livecoins.com.br/feed/',
    'https://portaldobitcoin.uol.com.br/feed/',
    'https://www.coindesk.com/arc/outboundfeeds/rss/',
]

NEWS_BULLISH_WORDS = [
    'dispara', 'sobe', 'alta', 'aprovação', 'aprovado', 'aprova',
    'recorde', 'máxima histórica', 'rompe', 'valoriza', 'ganhos',
    'adoção', 'entrada', 'compra', 'corte de juros', 'reduz juros',
    'estímulo', 'otimismo', 'avança',
    'rally', 'surge', 'soars', 'approval', 'approved', 'etf approval',
    'record high', 'all-time high', 'breaks', 'bullish', 'gains',
    'adoption', 'inflow', 'buy', 'rate cut', 'cut rates', 'stimulus',
]
NEWS_BEARISH_WORDS = [
    'despenca', 'cai', 'queda', 'banido', 'proibido', 'hackeado',
    'invasão', 'ataque hacker', 'processo', 'processa', 'derruba',
    'saída', 'venda', 'liquidação', 'liquidado', 'aumento de juros',
    'fraude', 'colapso', 'investigação', 'baixa', 'pessimismo',
    'crash', 'plunge', 'ban', 'banned', 'hack', 'hacked', 'exploit',
    'lawsuit', 'sec sues', 'bearish', 'sell-off', 'selloff', 'outflow',
    'liquidation', 'liquidated', 'rate hike', 'hikes rates', 'fraud',
    'collapse', 'investigation',
]


def classify_news_sentiment(title):
    t = title.lower()
    bull_hits = sum(1 for w in NEWS_BULLISH_WORDS if w in t)
    bear_hits = sum(1 for w in NEWS_BEARISH_WORDS if w in t)
    if bull_hits > bear_hits:
        return 'bullish'
    if bear_hits > bull_hits:
        return 'bearish'
    return 'neutral'


NEWS_RELEVANT_WORDS = [
    'bitcoin', 'btc', 'ethereum', 'eth', 'solana', 'sol',
    'fed', 'federal reserve', 'fomc', 'cpi', 'inflação', 'inflation',
    'juros', 'interest rate', 'sec', 'etf', 'regulação', 'regulation',
    'regulatório', 'powell', 'tesouro', 'treasury',
    'binance', 'coinbase', 'stablecoin', 'liquidação', 'liquidation', 'whale', 'baleia',
]


def relevance_score(title):
    t = title.lower()
    return sum(1 for w in NEWS_RELEVANT_WORDS if w in t)


def fetch_crypto_news(limit=8):
    import xml.etree.ElementTree as ET
    items = []
    headers = {'User-Agent': 'Mozilla/5.0 (compatible; KairosMentor/1.0)'}
    for feed_url in NEWS_RSS_FEEDS:
        try:
            r = requests.get(feed_url, headers=headers, timeout=8)
            root = ET.fromstring(r.content)
            for item in root.findall('.//item')[:limit]:
                title_el = item.find('title')
                link_el = item.find('link')
                date_el = item.find('pubDate')
                title = title_el.text.strip() if title_el is not None and title_el.text else ''
                if not title:
                    continue
                items.append({
                    'title': title,
                    'link': link_el.text.strip() if link_el is not None and link_el.text else '',
                    'pubDate': date_el.text.strip() if date_el is not None and date_el.text else '',
                    'sentiment': classify_news_sentiment(title),
                })
        except Exception as e:
            print(f"[news] erro ao buscar {feed_url}: {e}")
    for i, item in enumerate(items):
        item['_relevance'] = relevance_score(item['title'])
        item['_originalOrder'] = i
    items.sort(key=lambda x: (-x['_relevance'], x['_originalOrder']))
    for item in items:
        del item['_relevance']
        del item['_originalOrder']

    return items[:limit]


ECONOMIC_CALENDAR_2026 = [
    {'date': '2026-08-12', 'event': 'CPI (EUA) — inflação ao consumidor', 'time': '08:30 ET'},
    {'date': '2026-09-15', 'event': 'FOMC — decisão de juros (dia 1/2, com projeções)', 'time': '—'},
    {'date': '2026-09-16', 'event': 'FOMC — decisão de juros (dia 2/2, com projeções)', 'time': '14:00 ET'},
    {'date': '2026-09-10', 'event': 'CPI (EUA) — inflação ao consumidor (estimado, confirmar mais perto da data)', 'time': '08:30 ET'},
    {'date': '2026-10-13', 'event': 'CPI (EUA) — inflação ao consumidor (estimado, confirmar mais perto da data)', 'time': '08:30 ET'},
    {'date': '2026-10-27', 'event': 'FOMC — decisão de juros (dia 1/2)', 'time': '—'},
    {'date': '2026-10-28', 'event': 'FOMC — decisão de juros (dia 2/2)', 'time': '14:00 ET'},
    {'date': '2026-11-12', 'event': 'CPI (EUA) — inflação ao consumidor (estimado, confirmar mais perto da data)', 'time': '08:30 ET'},
    {'date': '2026-12-08', 'event': 'FOMC — decisão de juros (dia 1/2, com projeções)', 'time': '—'},
    {'date': '2026-12-09', 'event': 'FOMC — decisão de juros (dia 2/2, com projeções)', 'time': '14:00 ET'},
    {'date': '2026-12-10', 'event': 'CPI (EUA) — inflação ao consumidor (estimado, confirmar mais perto da data)', 'time': '08:30 ET'},
]


def get_upcoming_events(limit=5):
    today_str = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    upcoming = [e for e in ECONOMIC_CALENDAR_2026 if e['date'] >= today_str]
    upcoming.sort(key=lambda e: e['date'])
    return upcoming[:limit]


@app.route('/economic_calendar', methods=['GET'])
def get_economic_calendar():
    try:
        return jsonify({'events': get_upcoming_events(6)})
    except Exception as e:
        return jsonify({'error': str(e), 'events': []}), 500


@app.route('/news', methods=['GET'])
def get_news():
    try:
        items = fetch_crypto_news(limit=8)
        return jsonify({'news': items})
    except Exception as e:
        return jsonify({'error': str(e), 'news': []}), 500


_NEWS_CACHE = {'items': [], 'updated_at': 0}
NEWS_CACHE_TTL_SEC = 15 * 60


def get_cached_news(limit=6):
    now = time.time()
    if (now - _NEWS_CACHE['updated_at']) > NEWS_CACHE_TTL_SEC or not _NEWS_CACHE['items']:
        try:
            _NEWS_CACHE['items'] = fetch_crypto_news(limit=8)
            _NEWS_CACHE['updated_at'] = now
        except Exception as e:
            print(f"[news_cache] erro ao atualizar: {e}")
    return _NEWS_CACHE['items'][:limit]


SENTIMENT_LABEL_PT = {
    'bullish': '🟢 Otimista',
    'bearish': '🔴 Pessimista',
    'neutral': '⚪ Neutro',
}


def build_news_block(limit=4):
    try:
        items = get_cached_news(limit=limit)
        if not items:
            return ""
        bull = sum(1 for i in items if i['sentiment'] == 'bullish')
        bear = sum(1 for i in items if i['sentiment'] == 'bearish')
        if bull > bear:
            score_geral = SENTIMENT_LABEL_PT['bullish']
        elif bear > bull:
            score_geral = SENTIMENT_LABEL_PT['bearish']
        else:
            score_geral = SENTIMENT_LABEL_PT['neutral']

        lines = ["\n\n---\n\n<b>📰 Notícias e Sentimento de Mercado</b>"]
        lines.append(f"Score Geral: {score_geral} ({bull} otimista / {bear} pessimista / {len(items) - bull - bear} neutro)")
        for it in items:
            tag = SENTIMENT_LABEL_PT.get(it['sentiment'], '⚪ Neutro')
            lines.append(f"• {tag} — {it['title']}")
        return "\n".join(lines)
    except Exception as e:
        print(f"[build_news_block] erro: {e}")
        return ""


# Experimental Railway service must stay replay-only: no live 13-pair scheduler.
# Production/main behavior is unchanged because this guard exists only on the experiment branch.
if os.environ.get('RAILWAY_SERVICE_NAME') != 'kairos-poi-abc-sol':
    threading.Thread(target=paper_tick_scheduler_loop, daemon=True).start()
else:
    print('[POI_ABC] experimental service: live paper scheduler DISABLED', flush=True)


# EXPERIMENTAL 13-PAIR A_CURRENT LIVE SCANNER — demo/manual execution only.
# Same closed-candle causal replay/decision layer used for BTC; no trading math changed.
# Per-pair watermark + structural dedup prevent one pair from suppressing another.
_KAIROS_LIVE_PAIRS = (
    'BTCUSD', 'ETHUSD', 'SOLUSD', 'XRPUSD', 'LINKUSD', 'ADAUSD', 'AVAXUSD',
    'BNBUSD', 'AAVEUSD', 'NEARUSD', 'PENDLEUSD', 'INJUSD', 'ONDOUSD',
)
_KAIROS_LIVE_STARTED_TS = int(time.time() * 1000)
_KAIROS_LIVE_LAST_TS = {pair: _KAIROS_LIVE_STARTED_TS for pair in _KAIROS_LIVE_PAIRS}
_KAIROS_LIVE_SEEN = {pair: set() for pair in _KAIROS_LIVE_PAIRS}
_KAIROS_RADAR_SEEN = {pair: set() for pair in _KAIROS_LIVE_PAIRS}
_KAIROS_LIVE_INTERVAL_SECONDS = 60
_KAIROS_LIVE_PHASE_STATE={pair:{} for pair in _KAIROS_LIVE_PAIRS}

def _kairos_send_capture_events(pair, result, cutoff):
    """Confirm Telegram delivery before consuming a capture event."""
    all_delivered = True
    events = result.get('radar_captures') or []
    for event in events:
        ts = event['timestamp']; key = event['key']
        if ts <= _KAIROS_LIVE_LAST_TS[pair] or ts > cutoff or key in _KAIROS_RADAR_SEEN[pair]:
            continue
        context = event['context']
        reaction = {'REJECTION_RECLAIM': 'rejeição / recuperação', 'ACCEPTANCE_CONTINUATION': 'fechamento além do nível', 'UNRESOLVED_REACTION': 'reação ainda indefinida'}.get(event['reaction'], 'indefinida')
        confirmed_utc=datetime.fromtimestamp(ts/1000,timezone.utc).strftime('%Y-%m-%d %H:%M')
        evidence=event.get('capture_evidence_tf') or 'M15'
        evidence_note='Evidência D1; instante intradiário não determinado.\n' if evidence=='D1' else ''
        action_note=('Apenas contexto mensal; não autoriza entrada sozinho. DEMO/manual.'
                     if event.get('context_only') else
                     'Atenção ao M15; M5 refina após autorização. Alerta de contexto, sem entrada autorizada. DEMO/manual.')
        msg = (
            f"🔎 <b>KAIROS — CAPTURA DE LIQUIDEZ</b> | {pair}\n"
            f"MN: {context.get('MN','neutro')}\n"
            f"W1: {context['W1']} · D1: {context['D1']}\n"
            f"H4: {context['H4']} · H1: {context['H1']}\n"
            f"Nível: {event['liquidity_tf']} {event['liquidity_type']} @ {event['level']}\n"
            f"Confirmação (UTC): {confirmed_utc} · evidência {evidence}\n"
            f"Reação: {reaction}\n"
            f"{evidence_note}{action_note}"
        )
        if send_telegram(msg):
            _KAIROS_RADAR_SEEN[pair].add(key)
            print(f"[KAIROS_RADAR] delivered pair={pair} ts={ts} key={key}", flush=True)
        else:
            all_delivered = False
            print(f"[KAIROS_RADAR] delivery failed pair={pair} ts={ts}; retry next cycle", flush=True)
    return all_delivered

_KAIROS_SETUP_SEEN={pair:set() for pair in _KAIROS_LIVE_PAIRS}

def _kairos_deliver_setup_once(pair, event, message):
    """Persist acknowledged delivery; baseline pre-boot setups without reannouncing."""
    try:
        with sqlite3.connect(DB_FILE, timeout=15) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS kairos_setup_delivery (pair TEXT NOT NULL, event_key TEXT NOT NULL, ready_ts INTEGER NOT NULL, status TEXT NOT NULL, PRIMARY KEY (pair,event_key))")
            conn.execute('BEGIN IMMEDIATE')
            row=conn.execute('SELECT status FROM kairos_setup_delivery WHERE pair=? AND event_key=?', (pair,event['key'])).fetchone()
            if row and row[0] in ('DELIVERED','PRE_BOOT_BASELINE'):
                return True
            ready_ts=int(event['ready_ts'])
            if not row and ready_ts < _KAIROS_LIVE_STARTED_TS:
                conn.execute('INSERT INTO kairos_setup_delivery VALUES (?,?,?,?)', (pair,event['key'],ready_ts,'PRE_BOOT_BASELINE'))
                status='PRE_BOOT_BASELINE'
            else:
                if not row:
                    conn.execute('INSERT INTO kairos_setup_delivery VALUES (?,?,?,?)', (pair,event['key'],ready_ts,'PENDING'))
                if not send_telegram(message):
                    return False
                conn.execute("UPDATE kairos_setup_delivery SET status='DELIVERED' WHERE pair=? AND event_key=?", (pair,event['key']))
                status='DELIVERED'
        print(f"[KAIROS_SETUP] {status} pair={pair} ready_ts={ready_ts} key={event['key']}", flush=True)
        return True
    except Exception as exc:
        print(f"[KAIROS_SETUP] delivery/storage error pair={pair}: {exc}", flush=True)
        return False


def _kairos_send_setup_events(pair, result, cutoff):
    """Only send still-pending setups; never send an armed alert after its retest."""
    events=result.get('entry_phase_events') or []
    touched={e['setup_key'] for e in events if e.get('phase')=='RETEST_OBSERVED'}
    touched.update(k for k,v in (result.get('entry_setup_states') or {}).items() if v.get('touched'))
    pending=set(result.get('pending_setup_keys') or [])
    delivered=True
    for event in events:
        if event.get('phase')!='ARMED' or event.get('tf')!='M5' or event['setup_key'] in touched or event['setup_key'] not in pending:
            continue
        if event['key'] in _KAIROS_SETUP_SEEN[pair] or not (_KAIROS_LIVE_LAST_TS[pair]<=event['timestamp']<=cutoff):
            continue
        msg=(f"⏳ <b>KAIROS — SETUP M5 ARMADO</b> | {pair} | {event['direction']}\n"
             f"Nível proximal a observar: {event['level']}\n"
             f"{event['zone_type']} M5: {event['bottom']}–{event['top']}\n"
             f"Liquidez {event['capture_tf']}: {event['capture_level']} → M15 confirmado.\n"
             "Aguardando reteste. Nível planeado, sem preenchimento assumido. DEMO/manual.")
        if _kairos_deliver_setup_once(pair,event,msg):
            _KAIROS_SETUP_SEEN[pair].add(event['key'])
            _KAIROS_LIVE_PHASE_STATE.get(pair,{}).get(event['setup_key'],{}).pop('armed_event',None)
        else:
            delivered=False
    return delivered


def _kairos_format_entry_observation(pair, signal):
    """Entry-chain audit only: proximal and observed close are distinct, no fill assumed."""
    if not signal.get('entry_audit_only') or signal.get('entry_tf')!='M5':
        return None
    def utc_time(value):
        return datetime.fromtimestamp(value/1000, timezone.utc).strftime('%d/%m/%Y %H:%M UTC') if value else 'não informado'
    return (
        "🔎 <b>KAIROS — RETESTE M5 OBSERVADO</b>\n\n"
        f"<b>{signal.get('direction')}</b> | {pair}\n"
        f"POI M5: {signal.get('zone_type')} [{signal.get('zone_bottom')}–{signal.get('zone_top')}]\n"
        f"Nível proximal planeado: {signal.get('entry')}\n"
        f"Fechamento observado: {signal.get('observed_retest_close')}\n"
        f"Confirmação M15: {utc_time(signal.get('m15_internal_confirmation_close_ts'))}\n"
        f"Reteste M5 fechado: {utc_time(signal.get('retest_confirm_close_ts'))}\n"
        "Auditoria da cadeia; preenchimento não comprovado. DEMO/manual."
    )


def _kairos_live_scanner_loop():
    while True:
        cycle_started_ms = int(time.time() * 1000)
        with ThreadPoolExecutor(max_workers=3) as pool:
            jobs={pool.submit(scalp_engine._kairos_scan_latest_closed,pair,cycle_started_ms,
                              _KAIROS_LIVE_PHASE_STATE[pair]):pair for pair in _KAIROS_LIVE_PAIRS}
            for job in as_completed(jobs):
                pair=jobs[job]
                try:
                    r = job.result()
                    sinais = r.get('sinais_unicos_completos', []) if isinstance(r, dict) else []
                    if not isinstance(r, dict) or r.get('erro'):
                        raise RuntimeError('Replay failed; preserving delivery watermark')
                    if any(str(k).startswith('EXCECAO:') and count for k, count in (r.get('distribuicao_motivos_todos_ciclos') or {}).items()):
                        raise RuntimeError('Replay has failed evaluations; preserving delivery watermark')
                    delivered = _kairos_send_capture_events(pair, r, cycle_started_ms)
                    delivered = _kairos_send_setup_events(pair, r, cycle_started_ms) and delivered
                    novos = []
                    for s in sinais:
                        if not s.get('entry_observation_confirmed') or not s.get('entry_audit_only') or s.get('entry_tf')!='M5':
                            continue
                        ts = s.get('retest_confirm_close_ts')
                        sig = (s.get('choch_timestamp'), s.get('direction'), s.get('zone_type'),
                               s.get('zone_created_ts'), s.get('zone_bottom'), s.get('zone_top'))
                        if (ts is None or ts <= _KAIROS_LIVE_LAST_TS[pair] or
                                ts > cycle_started_ms or sig in _KAIROS_LIVE_SEEN[pair]):
                            continue
                        novos.append((ts, sig, s))
                    novos.sort(key=lambda x: x[0])
                    for ts, sig, s in novos:
                        direction = s.get('direction')
                        entry = s.get('entry')
                        msg = _kairos_format_entry_observation(pair, s)
                        if send_telegram(msg):
                            _KAIROS_LIVE_SEEN[pair].add(sig)
                            delivery_key=(s.get('entry_setup') or {}).get('key')
                            _KAIROS_LIVE_PHASE_STATE[pair].get(delivery_key,{}).pop('signal_snapshot',None)
                            print(f"[KAIROS_LIVE] M5_RETEST_OBSERVED pair={pair} ts={ts} sig={sig} proximal={entry}", flush=True)
                        else:
                            delivered = False
                    if delivered:
                        _KAIROS_LIVE_LAST_TS[pair] = cycle_started_ms
                        for pending_state in _KAIROS_LIVE_PHASE_STATE[pair].values():
                            cached=pending_state.get('signal_snapshot') or {}
                            if cached.get('retest_confirm_close_ts',cycle_started_ms+1)<=cycle_started_ms:
                                pending_state.pop('signal_snapshot',None)
                    print(f"[KAIROS_LIVE] scan done pair={pair} signals={len(sinais)} new={len(novos)} watermark={_KAIROS_LIVE_LAST_TS[pair]}", flush=True)
                except Exception as e:
                    print(f"[KAIROS_LIVE] scan error pair={pair}: {e}", flush=True)
        time.sleep(max(0.1,_KAIROS_LIVE_INTERVAL_SECONDS-(time.time()-cycle_started_ms/1000)))

if os.environ.get('RAILWAY_SERVICE_NAME') == 'kairos-poi-abc-sol':
    threading.Thread(target=_kairos_live_scanner_loop, daemon=True).start()
    print(f"[KAIROS_LIVE] scanner ENABLED pairs={len(_KAIROS_LIVE_PAIRS)} A_CURRENT latest-closed snapshot target interval=60s workers=3 demo/manual entry-only M5 no-management persistent-setup-dedup", flush=True)


# EXPERIMENTAL BRANCH ONLY — POI lifecycle A/B/C replay. Read-only, no DB/Telegram.
_KAIROS_ABC_CACHE = {'status':'IDLE','result':None,'error':None,'started_at':None,'finished_at':None}

def _run_kairos_abc_background(dias, fim_ts_ms, pair='SOLUSD'):
    global _KAIROS_ABC_CACHE
    try:
        _KAIROS_ABC_CACHE.update({'status':'RUNNING','result':None,'error':None,'started_at':int(time.time()*1000),'finished_at':None})
        r=scalp_engine.replay_poi_lifecycle_abc_sol(dias_historico=dias,fim_ts_ms=fim_ts_ms,pair=pair)
        _KAIROS_ABC_CACHE.update({'status':'DONE','result':r,'finished_at':int(time.time()*1000)})
        print('[POI_ABC_RESULT] '+str(r), flush=True)
    except Exception as e:
        _KAIROS_ABC_CACHE.update({'status':'ERROR','error':str(e),'finished_at':int(time.time()*1000)})
        print('[POI_ABC_ERROR] '+str(e), flush=True)

@app.route('/experiment/poi_lifecycle_abc_btc', methods=['GET'])
def experiment_poi_lifecycle_abc_btc():
    dias=max(1,min(int(request.args.get('dias','7')),31))
    fim_raw=request.args.get('fim_ts_ms')
    fim_ts_ms=int(fim_raw) if fim_raw else None
    if request.args.get('start') == '1':
        if _KAIROS_ABC_CACHE.get('status') != 'RUNNING':
            threading.Thread(target=_run_kairos_abc_background,args=(dias,fim_ts_ms,'BTCUSD'),daemon=True).start()
        return jsonify({'status':_KAIROS_ABC_CACHE.get('status'),'started':True,'pair':'BTCUSD'}),202
    return jsonify(_KAIROS_ABC_CACHE)


# BTC-only PDH/PDL liquidity policy — experimental replay, no Telegram.
_KAIROS_PDH_PDL_CACHE = {'status':'IDLE','result':None,'error':None,'started_at':None,'finished_at':None}

def _run_kairos_pdh_pdl_btc_background(dias, fim_ts_ms):
    global _KAIROS_PDH_PDL_CACHE
    try:
        _KAIROS_PDH_PDL_CACHE.update({'status':'RUNNING','result':None,'error':None,'started_at':int(time.time()*1000),'finished_at':None})
        print(f'[PDH_PDL_BTC_PROGRESS] dias={dias} phase=START', flush=True)
        r=scalp_engine.replay_vortex_decision_layer_v2('BTCUSD',dias_historico=dias,fim_ts_ms=fim_ts_ms,experimental_poi_policy='A_CURRENT',liquidity_policy='PDH_PDL_ONLY')
        _KAIROS_PDH_PDL_CACHE.update({'status':'DONE','result':r,'finished_at':int(time.time()*1000)})
        # Diagnóstico objetivo: referências PDH/PDL e cruzamentos M15 da janela fixa.
        try:
            end_ts=(r.get('janela_fixa') or {}).get('data_fim_ts_ms')
            start_ts=(r.get('janela_fixa') or {}).get('data_inicio_ts_ms')
            symbol='BTCUSDT'
            d1_raw=scalp_engine._fetch_bybit_klines_historico(symbol,'D',261,fim_ts_ms=end_ts)
            m15_raw=scalp_engine._fetch_bybit_klines_historico(symbol,'15',10,fim_ts_ms=end_ts)
            d1,_=scalp_engine._validar_e_limpar_candles(d1_raw,'D')
            m15_all,_=scalp_engine._validar_e_limpar_candles(m15_raw,'15')
            full={'D1':d1,'M15':m15_all}
            refs=scalp_engine._kairos_previous_period_refs(full,end_ts)
            m15=[c for c in m15_all if start_ts <= c.get('t',0) <= end_ts]
            diag={'PDH':refs.get('PDH'),'PDL':refs.get('PDL'),'crosses':{}}
            for typ in ('PDH','PDL'):
                rec=refs.get(typ); lv=rec.get('level') if rec else None
                diag['crosses'][typ]=[] if lv is None else [c for c in m15 if (c.get('h')>lv if typ=='PDH' else c.get('l')<lv)][:20]
            print(f'[PDH_PDL_BTC_CROSS_AUDIT] {diag}', flush=True)
        except Exception as audit_e:
            print(f'[PDH_PDL_BTC_CROSS_AUDIT_ERROR] {audit_e}', flush=True)
        # Auditoria causal por ciclo: usa o PDH/PDL que estava ativo naquele instante,
        # evitando aplicar a referência do fim da janela ao dia inteiro.
        try:
            end_ts=(r.get('janela_fixa') or {}).get('data_fim_ts_ms')
            start_ts=(r.get('janela_fixa') or {}).get('data_inicio_ts_ms')
            symbol='BTCUSDT'
            d1_raw=scalp_engine._fetch_bybit_klines_historico(symbol,'D',261,fim_ts_ms=end_ts)
            m15_raw=scalp_engine._fetch_bybit_klines_historico(symbol,'15',10,fim_ts_ms=end_ts)
            d1,_=scalp_engine._validar_e_limpar_candles(d1_raw,'D')
            m15,_=scalp_engine._validar_e_limpar_candles(m15_raw,'15')
            events=[]; seen=set()
            for c in m15:
                ts=c.get('t',0)
                if ts < start_ts or ts > end_ts: continue
                refs=scalp_engine._kairos_previous_period_refs({'D1':d1,'M15':m15},ts+15*60*1000)
                for typ in ('PDH','PDL'):
                    rec=refs.get(typ); lv=rec.get('level') if rec else None
                    if lv is None: continue
                    crossed=(c.get('h')>lv) if typ=='PDH' else (c.get('l')<lv)
                    key=(typ,rec.get('period_open_ts'))
                    if crossed and key not in seen:
                        seen.add(key); events.append({'type':typ,'level':lv,'period_open_ts':rec.get('period_open_ts'),'confirmed_ts':rec.get('confirmed_ts'),'sweep_candle':c})
            print(f'[PDH_PDL_BTC_CAUSAL_CROSS_AUDIT] {events}', flush=True)
        except Exception as audit_e:
            print(f'[PDH_PDL_BTC_CAUSAL_CROSS_AUDIT_ERROR] {audit_e}', flush=True)
        # Consolida POIs concorrentes da MESMA tese causal em um único trade.
        # A decisão continua congelada no timestamp original; candles futuros servem só para resolver o desfecho.
        try:
            uniq=r.get('sinais_unicos_completos') or []
            theses={}
            for sig in uniq:
                k=(sig.get('first_capture_ts'),sig.get('choch_timestamp'),sig.get('direction'))
                theses.setdefault(k,[]).append(sig)
            # Política determinística: primeira entrada executável da tese; empate => menor risco absoluto.
            selected=[]
            for k,cands in theses.items():
                cands=sorted(cands,key=lambda x:((x.get('timestamp') or 10**30),abs(float(x.get('entry'))-float(x.get('sl'))) if x.get('entry') is not None and x.get('sl') is not None else 10**30))
                selected.append(cands[0])
            # Busca candles APÓS o fim do replay, sem reavaliar sinais (somente outcome).
            future_end=int(time.time()*1000)
            m5_raw=scalp_engine._fetch_bybit_klines_historico('BTCUSDT','5',3000,fim_ts_ms=future_end)
            m5_future,_=scalp_engine._validar_e_limpar_candles(m5_raw,'5')
            trades=[]
            for sig in selected:
                entry=sig.get('entry'); sl=sig.get('sl'); tp1=sig.get('tp1') or sig.get('tp'); tp2=sig.get('tp2')
                ets=sig.get('timestamp'); direction=sig.get('direction')
                outcome='PENDING'; outcome_ts=None; tp1_seen=False; tp1_ts=None
                future=[x for x in m5_future if x.get('t',0) > (ets or 0)]
                for idx,c in enumerate(future):
                    if direction=='LONG':
                        hit_sl=sl is not None and c.get('l') <= sl; hit_tp1=tp1 is not None and c.get('h') >= tp1; hit_tp2=tp2 is not None and c.get('h') >= tp2
                    else:
                        hit_sl=sl is not None and c.get('h') >= sl; hit_tp1=tp1 is not None and c.get('l') <= tp1; hit_tp2=tp2 is not None and c.get('l') <= tp2
                    if not tp1_seen:
                        if hit_sl and (hit_tp1 or hit_tp2): outcome='AMBIGUO_PRE_TP1'; outcome_ts=c.get('t'); break
                        if hit_tp2: outcome='TP2'; outcome_ts=c.get('t'); break
                        if hit_sl: outcome='SL'; outcome_ts=c.get('t'); break
                        if hit_tp1:
                            tp1_seen=True; tp1_ts=c.get('t'); outcome='TP1_OPEN_REMAINDER'
                            # BE só fica ativo A PARTIR DO candle seguinte.
                            continue
                    else:
                        if hit_tp2: outcome='TP2_AFTER_TP1'; outcome_ts=c.get('t'); break
                        hit_be=(c.get('l') <= entry) if direction=='LONG' else (c.get('h') >= entry)
                        if hit_be: outcome='BE_AFTER_TP1'; outcome_ts=c.get('t'); break
                if tp1_seen and outcome=='TP1_OPEN_REMAINDER':
                    outcome_ts=tp1_ts
                trades.append({'thesis':(sig.get('first_capture_ts'),sig.get('choch_timestamp'),direction),'candidate_pois':len(theses[(sig.get('first_capture_ts'),sig.get('choch_timestamp'),direction)]),'entry_ts':ets,'direction':direction,'entry':entry,'sl':sl,'tp1':tp1,'tp2':tp2,'zone_type':sig.get('zone_type'),'zone':[sig.get('zone_bottom'),sig.get('zone_top')],'rr':sig.get('rr'),'tp1_ts':tp1_ts,'outcome':outcome,'outcome_ts':outcome_ts})
            print(f'[PDH_PDL_BTC_THESIS_TRADES] total={len(trades)} trades={trades}', flush=True)
        except Exception as trades_e:
            print(f'[PDH_PDL_BTC_THESIS_TRADES_ERROR] {trades_e}', flush=True)
        print(f'[PDH_PDL_BTC_RESULT] {r}', flush=True)
        print(f'[PDH_PDL_BTC_PROGRESS] dias={dias} phase=DONE', flush=True)
    except Exception as e:
        _KAIROS_PDH_PDL_CACHE.update({'status':'ERROR','error':str(e),'finished_at':int(time.time()*1000)})
        print('[PDH_PDL_BTC_ERROR] '+str(e), flush=True)

@app.route('/experiment/pdh_pdl_btc_1d', methods=['GET'])
def experiment_pdh_pdl_btc_1d():
    if request.args.get('start') == '1':
        if _KAIROS_PDH_PDL_CACHE.get('status') != 'RUNNING':
            threading.Thread(target=_run_kairos_pdh_pdl_btc_background,args=(1,None),daemon=True).start()
            return jsonify({'status':'STARTING','started':True,'pair':'BTCUSD','liquidity_policy':'PDH_PDL_ONLY','dias':1}),202
        return jsonify({'status':'RUNNING','started':False}),409
    return jsonify(_KAIROS_PDH_PDL_CACHE)


if os.environ.get('RAILWAY_SERVICE_NAME') == 'kairos-poi-abc-sol' and os.environ.get('KAIROS_RUN_PDH_PDL_1D_ONCE') == '1':
    threading.Thread(target=_run_kairos_pdh_pdl_btc_background,args=(3,1790208000000),daemon=True).start()
    print('[PDH_PDL_BTC] historical sweep replay ARMED end=2026-09-24T00:00:00Z dias=3', flush=True)


# BTC-only A_CURRENT — auditoria curta sem disparar os 13 pares.
_KAIROS_A_BTC_CACHE = {'status':'IDLE','result':None,'error':None,'started_at':None,'finished_at':None}

def _run_kairos_a_btc_background(dias, fim_ts_ms):
    global _KAIROS_A_BTC_CACHE
    try:
        _KAIROS_A_BTC_CACHE.update({'status':'RUNNING','result':None,'error':None,'started_at':int(time.time()*1000),'finished_at':None})
        print(f'[POI_A_BTC_PROGRESS] pair=BTCUSD dias={dias} phase=START', flush=True)
        r=scalp_engine.replay_poi_lifecycle_abc_sol(dias_historico=dias,fim_ts_ms=fim_ts_ms,pair='BTCUSD',policies=('A_CURRENT',))
        _KAIROS_A_BTC_CACHE.update({'status':'DONE','result':r,'finished_at':int(time.time()*1000)})
        try:
            _pol=(r.get('policies') or {}).get('A_CURRENT') or r.get('A_CURRENT') or {}
            _sum=_pol.get('experimental_intent_gate_summary') or {}
            print(f"[KAIROS_INTENT_AUDIT_SUMMARY] counts={_sum.get('counts')} unique_samples={len(_sum.get('unique_samples') or [])}", flush=True)
            for _x in (_sum.get('unique_samples') or []):
                _cap=_x.get('capture') or {}; _maj=_x.get('active_major_state') or {}; _intr=_x.get('internal_after_capture_same_direction') or {}
                print(f"[KAIROS_INTENT_AUDIT_SAMPLE] verdict={_x.get('verdict')} capture={_cap.get('tf')}:{_cap.get('type')}@{_cap.get('level')} sweep={_cap.get('sweep_ts')} reaction={_cap.get('reaction')} expected={_x.get('expected_direction')} major_source={_x.get('active_major_source')} active_major={_maj.get('tipo')}:{_maj.get('direcao')}@{_maj.get('nivel')} ts={_maj.get('t')} internal={_intr.get('tipo')}:{_intr.get('direcao')}@{_intr.get('nivel')} ts={_intr.get('t')}", flush=True)
        except Exception as _audit_log_exc:
            print(f"[KAIROS_INTENT_AUDIT_LOG_ERROR] {_audit_log_exc}", flush=True)
        print(f'[POI_A_BTC_PROGRESS] pair=BTCUSD dias={dias} phase=DONE', flush=True)
    except Exception as e:
        _KAIROS_A_BTC_CACHE.update({'status':'ERROR','error':str(e),'finished_at':int(time.time()*1000)})
        print('[POI_A_BTC_ERROR] '+str(e), flush=True)

@app.route('/experiment/audit_btc_m15_after_82800', methods=['GET'])
def experiment_audit_btc_m15_after_82800():
    """READ-ONLY: prova eventos M15 swing50/internal5 apos a captura H1 82800."""
    try:
        now_ts=int(time.time()*1000)
        capture_open=1790573400000
        capture_close=capture_open+900000
        m15=sorted(scalp_engine._fetch_bybit_klines_historico('BTCUSD','15',3,now_ts),key=lambda x:x.get('t',0))
        closed=[x for x in m15 if int(x.get('t',0))+900000<=now_ts]
        majors=scalp_engine.compute_lux_structure_events(closed,swing_size=50)
        internals=scalp_engine.compute_lux_internal_structure(closed,swing_size=5)
        post_major=[e for e in majors if int(e.get('t',0))+900000>capture_close and e.get('tipo') in ('CHoCH','BOS')]
        post_internal=[e for e in internals if int(e.get('t',0))+900000>capture_close and e.get('tipo') in ('CHoCH','BOS')]
        raw=[x for x in closed if int(x.get('t',0))>=capture_open]
        hi=max((float(x['h']) for x in raw),default=None); lo=min((float(x['l']) for x in raw),default=None)
        result={'ok':True,'read_only':True,'capture_level':82800.0,'capture_open_ts':capture_open,
                'capture_close_ts':capture_close,'closed_m15_after_capture':len(raw),
                'raw_high_after_capture':hi,'raw_low_after_capture':lo,
                'major50_events_after_capture':post_major,
                'internal5_events_after_capture':post_internal,
                'latest_major50_before_or_at_capture':next((e for e in reversed(majors) if int(e.get('t',0))+900000<=capture_close),None)}
        print(f"[BTC_M15_82800_AUDIT] major50={len(post_major)} internal5={len(post_internal)} raw_high={hi} raw_low={lo} latest_major_before={result['latest_major50_before_or_at_capture']}",flush=True)
        for e in post_major:
            print(f"[BTC_M15_82800_MAJOR50] type={e.get('tipo')} dir={e.get('direcao')} level={e.get('nivel')} open_ts={e.get('t')} close_ts={int(e.get('t',0))+900000}",flush=True)
        return jsonify(result)
    except Exception as e:
        print(f"[BTC_M15_82800_AUDIT_ERROR] {e}",flush=True)
        return jsonify({'ok':False,'error':str(e)}),500


@app.route('/experiment/audit_btc_internal_after_84352', methods=['GET'])
def experiment_audit_btc_internal_after_84352():
    """READ-ONLY: prova a estrutura internal5 conhecida apos o CHoCH major M15 bullish 84352.9."""
    try:
        now_ts=int(time.time()*1000)
        major_open=1790686800000
        major_close=major_open+900000
        m15=sorted(scalp_engine._fetch_bybit_klines_historico('BTCUSD','15',5,now_ts),key=lambda x:x.get('t',0))
        closed=[x for x in m15 if int(x.get('t',0))+900000<=now_ts]
        majors=sorted([e for e in scalp_engine.compute_lux_structure_events(closed,swing_size=50)
                       if e.get('tipo') in ('CHoCH','BOS')],key=lambda e:e.get('t',0))
        internals=sorted([e for e in scalp_engine.compute_lux_internal_structure(closed,swing_size=5)
                          if e.get('tipo') in ('CHoCH','BOS')],key=lambda e:e.get('t',0))
        major=next((e for e in majors if e.get('t')==major_open and e.get('direcao')=='alta'),None)
        after=[e for e in internals if int(e.get('t',0))+900000>major_close]
        bullish=[e for e in after if e.get('direcao')=='alta']
        bearish=[e for e in after if e.get('direcao')=='baixa']
        around=[e for e in internals if major_open-6*3600000 <= e.get('t',0) <= now_ts]
        result={'ok':True,'read_only':True,'pair':'BTCUSD','major_open_ts':major_open,'major_confirm_close_ts':major_close,
                'major_event':major,'internal5_after_major_close':after,'bullish_internal_after_major':bullish,
                'bearish_internal_after_major':bearish,'internal5_around_major':around,
                'counts':{'after':len(after),'bullish_after':len(bullish),'bearish_after':len(bearish)}}
        print(f"[BTC_INTERNAL_84352_AUDIT] counts={result['counts']} major={major}",flush=True)
        for e in after:
            print(f"[BTC_INTERNAL_84352_EVENT] type={e.get('tipo')} dir={e.get('direcao')} level={e.get('nivel')} open_ts={e.get('t')} close_ts={int(e.get('t',0))+900000}",flush=True)
        return jsonify(result)
    except Exception as e:
        print(f"[BTC_INTERNAL_84352_AUDIT_ERROR] {e}",flush=True)
        return jsonify({'ok':False,'error':str(e)}),500



@app.route('/experiment/audit_btc_structure_parity_84315', methods=['GET'])
def experiment_audit_btc_structure_parity_84315():
    """READ-ONLY: expõe candles M15 + eventos major50/internal5 no trecho 84315->83862 para paridade visual."""
    try:
        now_ts=int(time.time()*1000)
        start_ts=1790683200000
        end_ts=1790696700000
        m15=sorted(scalp_engine._fetch_bybit_klines_historico('BTCUSD','15',5,now_ts),key=lambda x:x.get('t',0))
        closed=[x for x in m15 if int(x.get('t',0))+900000<=now_ts]
        candles=[x for x in closed if start_ts<=int(x.get('t',0))<=end_ts]
        majors=[e for e in scalp_engine.compute_lux_structure_events(closed,swing_size=50)
                if start_ts<=int(e.get('t',0))<=end_ts and e.get('tipo') in ('CHoCH','BOS')]
        internals=[e for e in scalp_engine.compute_lux_internal_structure(closed,swing_size=5)
                   if start_ts<=int(e.get('t',0))<=end_ts and e.get('tipo') in ('CHoCH','BOS')]
        result={'ok':True,'read_only':True,'pair':'BTCUSD','tf':'M15','start_ts':start_ts,'end_ts':end_ts,
                'candles':candles,'major50_events':majors,'internal5_events':internals,
                'counts':{'candles':len(candles),'major50':len(majors),'internal5':len(internals)}}
        print(f"[BTC_STRUCTURE_PARITY_84315] counts={result['counts']}",flush=True)
        for e in majors:
            print(f"[BTC_STRUCTURE_PARITY_MAJOR] {e.get('tipo')} {e.get('direcao')} level={e.get('nivel')} t={e.get('t')} broken_origin={e.get('broken_swing_origin_ts')} protected={e.get('protected_swing_type')}@{e.get('protected_swing_level')}",flush=True)
        for e in internals:
            print(f"[BTC_STRUCTURE_PARITY_INTERNAL] {e.get('tipo')} {e.get('direcao')} level={e.get('nivel')} t={e.get('t')} broken_origin={e.get('broken_swing_origin_ts')} protected={e.get('protected_swing_type')}@{e.get('protected_swing_level')}",flush=True)
        return jsonify(result)
    except Exception as e:
        print(f"[BTC_STRUCTURE_PARITY_84315_ERROR] {e}",flush=True)
        return jsonify({'ok':False,'error':str(e)}),500


@app.route('/experiment/audit_eth_poi_leg', methods=['GET'])
def experiment_audit_eth_poi_leg():
    """READ-ONLY: autopsia todos FVG/IFVG/OB M15 da perna ETH antes do major bearish."""
    try:
        now_ts=int(time.time()*1000); cap=1789948800000; invalid=1790168400000
        m15=sorted(scalp_engine._fetch_bybit_klines_historico('ETHUSD','15',12,now_ts),key=lambda x:x.get('t',0))
        m15=[x for x in m15 if int(x.get('t',0))+900000<=now_ts]
        majors=sorted([e for e in scalp_engine.compute_lux_structure_events(m15,swing_size=50)
                       if e.get('tipo') in ('CHoCH','BOS')],key=lambda e:e.get('t',0))
        ints=sorted([e for e in scalp_engine.compute_lux_internal_structure(m15,swing_size=5)
                     if e.get('tipo') in ('CHoCH','BOS') and cap<e.get('t',0)<invalid and e.get('direcao')=='alta'],
                    key=lambda e:e.get('t',0))
        zones=scalp_engine._kairos_fvg_states(m15)
        rows=[]
        for z in zones:
            eff=z.get('flip_ts') or z.get('created_ts') or 0
            src=[z.get('source_a'),z.get('source_mid'),z.get('source_c')]
            src_ts=[x.get('t') for x in src if isinstance(x,dict) and x.get('t') is not None]
            if not (cap<=eff<invalid) and not any(cap<=t<invalid for t in src_ts): continue
            reasons=[]
            if z.get('direcao')!='alta': reasons.append('DIRECTION_NOT_BULLISH')
            if z.get('state') not in ('ATIVA','TOCADA','PARCIAL','IFVG'): reasons.append('STATE_NOT_ENTRY_ELIGIBLE')
            if z.get('created_ts') is not None and z.get('created_ts')<cap: reasons.append('MOTHER_PRE_CAPTURE')
            if not all(isinstance(x,dict) and x.get('t') is not None for x in src): reasons.append('MISSING_ABC')
            elif min(src_ts)<cap: reasons.append('ABC_STARTS_PRE_CAPTURE')
            rows.append({'tipo':z.get('tipo'),'direcao':z.get('direcao'),'state':z.get('state'),
                         'bottom':z.get('bottom'),'top':z.get('top'),'created_ts':z.get('created_ts'),
                         'flip_ts':z.get('flip_ts'),'lux_fvg':z.get('lux_fvg'),
                         'mother_fvg_id':z.get('mother_fvg_id'),'source_ts':src_ts,'base_rejections':reasons})
        obs=[]
        for ev in ints:
            idx=next((i for i,x in enumerate(m15) if x.get('t')==ev.get('t')),None)
            if idx is None: continue
            e=dict(ev); e['full_idx']=idx
            ob=scalp_engine._kairos_ob_from_break(m15,idx,'alta')
            obs.append({'structure':ev,'ob':ob})
        result={'ok':True,'read_only':True,'pair':'ETHUSD','capture_ts':cap,'invalidating_major_ts':invalid,
                'bullish_internal_events':ints,'fvg_ifvg_candidates':rows,'ob_by_internal_break':obs,
                'counts':{'internal':len(ints),'fvg_ifvg':len(rows),'ob_checks':len(obs)}}
        print(f"[ETH_POI_LEG_AUDIT] counts={result['counts']}",flush=True)
        for z in rows:
            print(f"[ETH_POI_ZONE] {z['tipo']} {z['bottom']}-{z['top']} created={z['created_ts']} flip={z['flip_ts']} lux={z['lux_fvg']} reject={z['base_rejections']}",flush=True)
        for o in obs:
            ev=o['structure']; ob=o['ob']
            print(f"[ETH_POI_OB] internal={ev.get('tipo')}@{ev.get('nivel')} ts={ev.get('t')} ob={ob}",flush=True)
        return jsonify(result)
    except Exception as e:
        print(f"[ETH_POI_LEG_AUDIT_ERROR] {e}",flush=True)
        return jsonify({'ok':False,'error':str(e)}),500

@app.route('/experiment/audit_eth_capture_lifecycle', methods=['GET'])
def experiment_audit_eth_capture_lifecycle():
    """READ-ONLY: prova causalmente a janela ETH entre captura H4 2670.98 e primeiro major50 contrario."""
    try:
        now_ts=int(time.time()*1000)
        capture_open=1789948800000
        capture_close=capture_open+900000
        expected='alta'
        m15=sorted(scalp_engine._fetch_bybit_klines_historico('ETHUSD','15',12,now_ts),key=lambda x:x.get('t',0))
        m5=sorted(scalp_engine._fetch_bybit_klines_historico('ETHUSD','5',12,now_ts),key=lambda x:x.get('t',0))
        m15=[x for x in m15 if int(x.get('t',0))+900000<=now_ts]
        m5=[x for x in m5 if int(x.get('t',0))+300000<=now_ts]
        majors=sorted([e for e in scalp_engine.compute_lux_structure_events(m15,swing_size=50)
                       if e.get('tipo') in ('CHoCH','BOS')],key=lambda e:e.get('t',0))
        internals=sorted([e for e in scalp_engine.compute_lux_internal_structure(m15,swing_size=5)
                          if e.get('tipo') in ('CHoCH','BOS')],key=lambda e:e.get('t',0))
        state_at_capture=next((e for e in reversed(majors) if e.get('t',0)<=capture_open),None)
        opposing=next((e for e in majors if e.get('t',0)>capture_open and e.get('direcao')!=expected),None)
        opposing_open=opposing.get('t') if opposing else now_ts
        opposing_close=(int(opposing_open)+900000 if opposing else None)
        window_internal=[e for e in internals if e.get('t',0)>capture_open and e.get('t',0)<opposing_open and e.get('direcao')==expected]
        # Independente do replay: para cada corte internal bullish dentro da janela, prova que nenhum major bearish futuro
        # ainda era conhecido naquele corte e expõe candles M5 fechados disponíveis depois da confirmação.
        checkpoints=[]
        for e in window_internal:
            confirm=int(e.get('t',0))+900000
            known_major=next((x for x in reversed(majors) if x.get('t',0)<=e.get('t',0)),None)
            m5_after=[x for x in m5 if x.get('t',0)>=confirm and x.get('t',0)<opposing_open]
            checkpoints.append({'internal':e,'internal_confirm_close_ts':confirm,
                                'major_state_known_at_internal':known_major,
                                'major_matches_expected':bool(known_major and known_major.get('direcao')==expected),
                                'closed_m5_available_before_opposing_major':len(m5_after),
                                'first_m5_after_internal':m5_after[0] if m5_after else None})
        result={'ok':True,'read_only':True,'pair':'ETHUSD','capture_level':2670.98,
                'capture_open_ts':capture_open,'capture_close_ts':capture_close,'expected_direction':expected,
                'major_state_at_capture':state_at_capture,'first_opposing_major_after_capture':opposing,
                'opposing_major_confirm_close_ts':opposing_close,
                'bullish_internal_events_before_opposing_major':window_internal,
                'checkpoints':checkpoints,
                'verdict':('CAUSAL_WINDOW_EXISTED_BEFORE_OPPOSING_MAJOR' if checkpoints else 'NO_BULLISH_INTERNAL_WINDOW_BEFORE_OPPOSING_MAJOR')}
        print(f"[ETH_CAPTURE_LIFECYCLE_AUDIT] verdict={result['verdict']} state={state_at_capture} opposing={opposing} bullish_internal_count={len(window_internal)}",flush=True)
        for x in checkpoints:
            e=x['internal']; km=x['major_state_known_at_internal'] or {}
            print(f"[ETH_CAPTURE_LIFECYCLE_CHECKPOINT] internal={e.get('tipo')}:{e.get('direcao')}@{e.get('nivel')} ts={e.get('t')} confirm={x.get('internal_confirm_close_ts')} known_major={km.get('tipo')}:{km.get('direcao')}@{km.get('nivel')} major_ts={km.get('t')} m5_before_opposing={x.get('closed_m5_available_before_opposing_major')}",flush=True)
        return jsonify(result)
    except Exception as e:
        print(f"[ETH_CAPTURE_LIFECYCLE_AUDIT_ERROR] {e}",flush=True)
        return jsonify({'ok':False,'error':str(e)}),500

@app.route('/experiment/poi_lifecycle_a_eth_1d/start', methods=['GET'])
def experiment_poi_lifecycle_a_eth_1d_start():
    """Mesmo replay/auditoria A_CURRENT de 1 dia, isolado para ETHUSD."""
    if _KAIROS_A_BTC_CACHE.get('status') != 'RUNNING' and _KAIROS_A13_CACHE.get('status') != 'RUNNING' and _KAIROS_ABC_CACHE.get('status') != 'RUNNING':
        def _run_eth():
            try:
                _KAIROS_A_BTC_CACHE.update({'status':'RUNNING','result':None,'error':None,'started_at':int(time.time()*1000),'finished_at':None})
                print('[POI_A_ETH_PROGRESS] pair=ETHUSD dias=1 phase=START',flush=True)
                r=scalp_engine.replay_poi_lifecycle_abc_sol(dias_historico=1,fim_ts_ms=None,pair='ETHUSD',policies=('A_CURRENT',))
                _KAIROS_A_BTC_CACHE.update({'status':'DONE','result':r,'finished_at':int(time.time()*1000)})
                _pol=(r.get('policies') or {}).get('A_CURRENT') or r.get('A_CURRENT') or {}
                _sum=_pol.get('experimental_intent_gate_summary') or {}
                print(f"[KAIROS_ETH_INTENT_AUDIT_SUMMARY] counts={_sum.get('counts')} unique_samples={len(_sum.get('unique_samples') or [])}",flush=True)
                for _x in (_sum.get('unique_samples') or []):
                    _cap=_x.get('capture') or {}; _maj=_x.get('active_major_state') or {}; _intr=_x.get('internal_after_capture_same_direction') or {}
                    print(f"[KAIROS_ETH_INTENT_AUDIT_SAMPLE] verdict={_x.get('verdict')} capture={_cap.get('tf')}:{_cap.get('type')}@{_cap.get('level')} sweep={_cap.get('sweep_ts')} reaction={_cap.get('reaction')} expected={_x.get('expected_direction')} major_source={_x.get('active_major_source')} active_major={_maj.get('tipo')}:{_maj.get('direcao')}@{_maj.get('nivel')} ts={_maj.get('t')} internal={_intr.get('tipo')}:{_intr.get('direcao')}@{_intr.get('nivel')} ts={_intr.get('t')}",flush=True)
                print('[POI_A_ETH_PROGRESS] pair=ETHUSD dias=1 phase=DONE',flush=True)
            except Exception as e:
                _KAIROS_A_BTC_CACHE.update({'status':'ERROR','error':str(e),'finished_at':int(time.time()*1000)})
                print('[POI_A_ETH_ERROR] '+str(e),flush=True)
        threading.Thread(target=_run_eth,daemon=True).start()
        return jsonify({'status':'STARTING','started':True,'pair':'ETHUSD','policy':'A_CURRENT','dias':1}),202
    return jsonify({'status':'RUNNING','started':False}),409

@app.route('/experiment/poi_lifecycle_a_btc_1d/start', methods=['GET'])
def experiment_poi_lifecycle_a_btc_1d_start():
    """Atalho sem query string para iniciar o replay BTC A_CURRENT de 1 dia."""
    if _KAIROS_A_BTC_CACHE.get('status') != 'RUNNING' and _KAIROS_A13_CACHE.get('status') != 'RUNNING' and _KAIROS_ABC_CACHE.get('status') != 'RUNNING':
        threading.Thread(target=_run_kairos_a_btc_background,args=(1,None),daemon=True).start()
        return jsonify({'status':'STARTING','started':True,'pair':'BTCUSD','policy':'A_CURRENT','dias':1}),202
    return jsonify({'status':_KAIROS_A_BTC_CACHE.get('status'),'started':False,'reason':'REPLAY_ALREADY_RUNNING'}),409

@app.route('/experiment/poi_lifecycle_a_btc', methods=['GET'])
def experiment_poi_lifecycle_a_btc():
    dias=max(1,min(int(request.args.get('dias','7')),31))
    fim_raw=request.args.get('fim_ts_ms')
    fim_ts_ms=int(fim_raw) if fim_raw else None
    if request.args.get('start') == '1':
        if _KAIROS_A_BTC_CACHE.get('status') != 'RUNNING' and _KAIROS_A13_CACHE.get('status') != 'RUNNING' and _KAIROS_ABC_CACHE.get('status') != 'RUNNING':
            threading.Thread(target=_run_kairos_a_btc_background,args=(dias,fim_ts_ms),daemon=True).start()
            return jsonify({'status':'STARTING','started':True,'pair':'BTCUSD','policy':'A_CURRENT','dias':dias}),202
        return jsonify({'status':_KAIROS_A_BTC_CACHE.get('status'),'started':False,'reason':'REPLAY_ALREADY_RUNNING'}),409
    return jsonify(_KAIROS_A_BTC_CACHE)




# NEARUSD A_CURRENT — auditoria curta isolada; nao altera scanner FORWARD nem estrategia.
_KAIROS_A_NEAR_CACHE = {'status':'IDLE','result':None,'error':None,'started_at':None,'finished_at':None}

def _run_kairos_a_near_background(dias, fim_ts_ms):
    global _KAIROS_A_NEAR_CACHE
    try:
        _KAIROS_A_NEAR_CACHE.update({'status':'RUNNING','result':None,'error':None,'started_at':int(time.time()*1000),'finished_at':None})
        print(f'[POI_A_NEAR_PROGRESS] pair=NEARUSD dias={dias} phase=START', flush=True)
        r=scalp_engine.replay_poi_lifecycle_abc_sol(
            dias_historico=dias, fim_ts_ms=fim_ts_ms, pair='NEARUSD',
            policies=('A_CURRENT',),
        )
        _KAIROS_A_NEAR_CACHE.update({'status':'DONE','result':r,'finished_at':int(time.time()*1000)})
        print(f'[POI_A_NEAR_PROGRESS] pair=NEARUSD dias={dias} phase=DONE', flush=True)
    except Exception as e:
        _KAIROS_A_NEAR_CACHE.update({'status':'ERROR','error':str(e),'finished_at':int(time.time()*1000)})
        print('[POI_A_NEAR_ERROR] '+str(e), flush=True)

@app.route('/experiment/poi_lifecycle_a_near_1d/start', methods=['GET'])
def experiment_poi_lifecycle_a_near_1d_start():
    """Inicia replay isolado NEARUSD A_CURRENT de 1 dia."""
    if _KAIROS_A_NEAR_CACHE.get('status') != 'RUNNING':
        threading.Thread(target=_run_kairos_a_near_background,args=(1,None),daemon=True).start()
        return jsonify({'status':'STARTING','started':True,'pair':'NEARUSD','policy':'A_CURRENT','dias':1}),202
    return jsonify({'status':_KAIROS_A_NEAR_CACHE.get('status'),'started':False,'reason':'REPLAY_ALREADY_RUNNING'}),409

@app.route('/experiment/poi_lifecycle_a_near', methods=['GET'])
def experiment_poi_lifecycle_a_near():
    return jsonify(_KAIROS_A_NEAR_CACHE)




_KAIROS_NEAR_CHAIN_AUDIT={'status':'IDLE','result':None,'error':None}

def _near_chain_job():
    _KAIROS_NEAR_CHAIN_AUDIT.update({'status':'RUNNING','result':None,'error':None})
    try:
        now_ts=int(time.time()*1000)
        specs={'MN':('M',3650),'W1':('W',1825),'D1':('D',730),'H4':('240',120),'H1':('60',45),'M15':('15',12),'M5':('5',5)}
        candles={tf:scalp_engine._fetch_bybit_klines_historico('NEARUSD',iv,d,fim_ts_ms=now_ts) for tf,(iv,d) in specs.items()}
        sweep,audit=scalp_engine._kairos_select_structural_first_capture_sweep(candles,now_ts)
        m15=candles.get('M15') or []
        majors=scalp_engine.compute_lux_structure_events(m15,swing_size=50)
        internals=scalp_engine.compute_lux_internal_structure(m15,swing_size=5)
        rows=[]
        for cap in ((audit or {}).get('candidates') or []):
            if cap.get('status')!='VALID_FIRST_CAPTURE_NEUTRAL': continue
            ts=cap.get('sweep_ts') or 0
            side=cap.get('liquidity_side'); state=cap.get('post_capture_state')
            expected=('baixa' if side=='HIGH' else 'alta') if state=='REJECTION_RECLAIM' else ('alta' if side=='HIGH' else 'baixa')
            major_before=next((e for e in reversed(majors) if e.get('t',0)<=ts),None)
            major_after=next((e for e in majors if e.get('t',0)>ts and e.get('direcao')==expected),None)
            internal_after=next((e for e in internals if e.get('t',0)>ts and e.get('direcao')==expected),None)
            intent=scalp_engine._kairos_direction_after_first_capture(m15,cap,swing_size=5)
            rows.append({'capture':cap,'expected_direction':expected,'major_state_before_capture':major_before,
                         'first_major_expected_after_capture':major_after,'first_internal_expected_after_capture':internal_after,
                         'current_intent_result':intent,
                         'first_failure':'NO_MAJOR_EXPECTED_AFTER_CAPTURE' if not major_after else ('NO_INTERNAL_AFTER_MAJOR' if not intent else None)})
        _KAIROS_NEAR_CHAIN_AUDIT.update({'status':'DONE','result':{'ok':True,'read_only':True,'selected_sweep':sweep,'candidates':rows}})
    except Exception as e:
        _KAIROS_NEAR_CHAIN_AUDIT.update({'status':'ERROR','error':str(e)})

@app.route('/experiment/audit_near_chain/start',methods=['GET'])
def experiment_audit_near_chain_start():
    if _KAIROS_NEAR_CHAIN_AUDIT.get('status')=='RUNNING':
        return jsonify({'status':'RUNNING','started':False}),202
    threading.Thread(target=_near_chain_job,daemon=True).start()
    return jsonify({'status':'STARTING','started':True,'pair':'NEARUSD','read_only':True}),202

@app.route('/experiment/audit_near_chain',methods=['GET'])
def experiment_audit_near_chain():
    return jsonify(_KAIROS_NEAR_CHAIN_AUDIT)


_KAIROS_NEAR_LIQ_AUDIT={'status':'IDLE','result':None,'error':None}

def _near_liq_job(now_ts):
    _KAIROS_NEAR_LIQ_AUDIT.update({'status':'RUNNING','result':None,'error':None})
    try:
        specs={'MN':('M',3650),'W1':('W',1825),'D1':('D',730),'H4':('240',120),'H1':('60',45),'M15':('15',12),'M5':('5',5)}
        candles={tf:scalp_engine._fetch_bybit_klines_historico('NEARUSD',iv,d,fim_ts_ms=now_ts) for tf,(iv,d) in specs.items()}
        sweep,audit=scalp_engine._kairos_select_structural_first_capture_sweep(candles,now_ts)
        m15=candles.get('M15') or []
        result={'ok':True,'read_only':True,'pair':'NEARUSD','fim_ts_ms':now_ts,'selected_sweep':sweep,
          'setup_levels':((audit or {}).get('setup_levels') or [])[-40:],
          'capture_candidates':((audit or {}).get('candidates') or [])[-40:],
          'm15_major50_events':scalp_engine.compute_lux_structure_events(m15,swing_size=50)[-20:],
          'm15_internal5_events':scalp_engine.compute_lux_structure_events(m15,swing_size=5)[-30:]}
        _KAIROS_NEAR_LIQ_AUDIT.update({'status':'DONE','result':result})
    except Exception as e:
        _KAIROS_NEAR_LIQ_AUDIT.update({'status':'ERROR','error':str(e)})

@app.route('/experiment/audit_near_liquidity_window/start',methods=['GET'])
def experiment_audit_near_liquidity_window_start():
    if _KAIROS_NEAR_LIQ_AUDIT.get('status')=='RUNNING':
        return jsonify({'status':'RUNNING','started':False}),202
    now_ts=int(request.args.get('fim_ts_ms') or int(time.time()*1000))
    threading.Thread(target=_near_liq_job,args=(now_ts,),daemon=True).start()
    return jsonify({'status':'STARTING','started':True,'pair':'NEARUSD'}),202

@app.route('/experiment/audit_near_liquidity_window',methods=['GET'])
def experiment_audit_near_liquidity_window():
    return jsonify(_KAIROS_NEAR_LIQ_AUDIT)


_KAIROS_NEAR_CHOCH_AUDIT = {'status':'IDLE','result':None,'error':None}

def _build_near_choch_audit(rr):
    pol=((rr or {}).get('policies') or {}).get('A_CURRENT') or {}
    autos=pol.get('resolved_signal_autopsy') or []
    if not autos:
        return {'ok':False,'reason':'SEM_AUTOPSIA_NEAR_NO_REPLAY','replay_summary':{
            'N':pol.get('N'),'total_sinais_unicos':pol.get('total_sinais_unicos'),
            'distribuicao_motivos':pol.get('distribuicao_motivos')}}
    sig=(autos[0] or {}).get('signal') or {}
    event_ts=sig.get('choch_timestamp'); level=sig.get('choch_level')
    if event_ts is None or level is None:
        return {'ok':False,'reason':'SEM_CHOCH_TS_OU_LEVEL'}
    # Usa exatamente o mesmo fetch historico do replay; nada de API paralela/inventada.
    cs=scalp_engine._fetch_bybit_klines_historico(
        'NEARUSD','15',10,fim_ts_ms=int(event_ts)+900000)
    cs=[x for x in (cs or []) if x.get('t') is not None and x['t'] <= event_ts]
    ev5=scalp_engine.compute_lux_structure_events(cs,swing_size=5)
    ev50=scalp_engine.compute_lux_structure_events(cs,swing_size=50)
    match5=[e for e in ev5 if e.get('t')==event_ts and abs(float(e.get('nivel',0))-float(level))<1e-9]
    same50=[e for e in ev50 if e.get('t')==event_ts]
    return {
        'ok':True,'pair':'NEARUSD','read_only':True,
        'signal_direction':sig.get('direction'),'context_bias':sig.get('context_bias'),
        'htf_location':sig.get('htf_location'),
        'reported_event':{'ts':event_ts,'level':level,'type':sig.get('m15_confirmation_type')},
        'lux_internal_5_match':match5,'lux_swing_50_events_same_candle':same50,
        'classification':('INTERNAL_ONLY' if match5 and not same50 else ('ALSO_SWING50' if match5 and same50 else 'NO_MATCH')),
        'event_candle':next((x for x in cs if x.get('t')==event_ts),None),
        'prior_internal_events':ev5[-8:],'prior_swing50_events':ev50[-8:],
        'replay_summary':{'N':pol.get('N'),'total_sinais_unicos':pol.get('total_sinais_unicos')},
    }

def _run_near_choch_audit_background():
    _KAIROS_NEAR_CHOCH_AUDIT.update({'status':'RUNNING','result':None,'error':None,'started_at':int(time.time()*1000)})
    try:
        # Reutiliza replay NEAR concluido se ainda estiver no mesmo processo; senao reconstrói.
        near_cache=globals().get('_KAIROS_A_NEAR_CACHE') or {}
        rr=near_cache.get('result') if near_cache.get('status')=='DONE' else None
        source='NEAR_REPLAY_CACHE'
        if not rr:
            source='SELF_REPLAY_1D'
            rr=scalp_engine.replay_poi_lifecycle_abc_sol(
                dias_historico=1,fim_ts_ms=None,pair='NEARUSD',policies=('A_CURRENT',))
        result=_build_near_choch_audit(rr)
        result['source']=source
        _KAIROS_NEAR_CHOCH_AUDIT.update({'status':'DONE','result':result,'finished_at':int(time.time()*1000)})
        print('[NEAR_CHOCH_AUDIT] DONE classification='+str(result.get('classification'))+' source='+source,flush=True)
    except Exception as e:
        _KAIROS_NEAR_CHOCH_AUDIT.update({'status':'ERROR','error':str(e),'finished_at':int(time.time()*1000)})
        print('[NEAR_CHOCH_AUDIT] ERROR '+str(e),flush=True)

@app.route('/experiment/audit_near_choch_5020/start', methods=['GET'])
def experiment_audit_near_choch_5020_start():
    if _KAIROS_NEAR_CHOCH_AUDIT.get('status')=='RUNNING':
        return jsonify({'status':'RUNNING','started':False,'reason':'AUDIT_ALREADY_RUNNING'}),202
    threading.Thread(target=_run_near_choch_audit_background,daemon=True).start()
    return jsonify({'status':'STARTING','started':True,'pair':'NEARUSD','audit':'CHOCH_M15_5_VS_50'}),202

@app.route('/experiment/audit_near_choch_5020', methods=['GET'])
def experiment_audit_near_choch_5020():
    """Resultado/status instantaneo; nunca executa replay dentro da requisicao."""
    return jsonify(_KAIROS_NEAR_CHOCH_AUDIT)

_KAIROS_STRUCTURE_HIERARCHY_AUDIT = {'status':'IDLE','result':None,'error':None}
_KAIROS_STRUCTURE_HIERARCHY_PAIRS = ('BTCUSD','ETHUSD','SOLUSD','XRPUSD','LINKUSD','ADAUSD','AVAXUSD','BNBUSD','AAVEUSD','NEARUSD','PENDLEUSD','INJUSD','ONDOUSD')

def _kairos_hierarchy_case(pair, sig):
    event_ts=sig.get('choch_timestamp')
    if event_ts is None:
        return {'pair':pair,'classification':'SEM_EVENT_TS'}
    cs=scalp_engine._fetch_bybit_klines_historico(pair,'15',10,fim_ts_ms=int(event_ts)+900000)
    cs=[x for x in (cs or []) if x.get('t') is not None and x['t'] <= event_ts]
    ev5=scalp_engine.compute_lux_structure_events(cs,swing_size=5)
    ev50=scalp_engine.compute_lux_structure_events(cs,swing_size=50)
    i5=next((e for e in reversed(ev5) if e.get('t')==event_ts),None)
    major_before=next((e for e in reversed(ev50) if e.get('t',0) <= event_ts),None)
    major_same=next((e for e in reversed(ev50) if e.get('t')==event_ts),None)
    sig_dir='alta' if sig.get('direction')=='LONG' else ('baixa' if sig.get('direction')=='SHORT' else None)
    major_dir=(major_before or {}).get('direcao')
    if major_same and major_same.get('direcao')==sig_dir:
        cls='REVERSAO_MAIOR_CONFIRMADA'
    elif major_dir==sig_dir:
        cls='ALINHADO_COM_ESTRUTURA_MAIOR'
    elif i5 and i5.get('direcao')==sig_dir:
        cls='INTERNAL_SEM_AUTORIZACAO_MAIOR'
    else:
        cls='SEM_GENEALOGIA_INTERNA_EXATA'
    return {'pair':pair,'classification':cls,'signal_direction':sig.get('direction'),
            'authorization_path':sig.get('authorization_path'),'event_ts':event_ts,
            'reported_level':sig.get('choch_level'),'context_bias':sig.get('context_bias'),
            'htf_location':sig.get('htf_location'),'internal5_event':i5,
            'major50_state_at_event':major_before,'major50_same_candle':major_same}

def _run_structure_hierarchy_audit():
    _KAIROS_STRUCTURE_HIERARCHY_AUDIT.update({'status':'RUNNING','result':None,'error':None,'started_at':int(time.time()*1000)})
    try:
        cases=[]; errors=[]
        for pair in _KAIROS_STRUCTURE_HIERARCHY_PAIRS:
            try:
                rr=scalp_engine.replay_poi_lifecycle_abc_sol(dias_historico=1,fim_ts_ms=None,pair=pair,policies=('A_CURRENT',))
                pol=((rr or {}).get('policies') or {}).get('A_CURRENT') or {}
                autos=pol.get('resolved_signal_autopsy') or []
                for a in autos:
                    sig=(a or {}).get('signal') or {}
                    if sig.get('authorization_path')=='HTF_POI_TOUCH':
                        cases.append(_kairos_hierarchy_case(pair,sig))
            except Exception as e:
                errors.append({'pair':pair,'error':str(e)})
        counts={}
        for x in cases: counts[x['classification']]=counts.get(x['classification'],0)+1
        result={'ok':True,'read_only':True,'pairs_requested':len(_KAIROS_STRUCTURE_HIERARCHY_PAIRS),
                'cases':cases,'classification_counts':counts,'errors':errors,
                'note':'Audit only: no gate, direction, entry, SL or TP changed.'}
        _KAIROS_STRUCTURE_HIERARCHY_AUDIT.update({'status':'DONE','result':result,'finished_at':int(time.time()*1000)})
        print('[STRUCTURE_HIERARCHY_AUDIT] DONE cases='+str(len(cases))+' counts='+str(counts)+' errors='+str(len(errors)),flush=True)
    except Exception as e:
        _KAIROS_STRUCTURE_HIERARCHY_AUDIT.update({'status':'ERROR','error':str(e),'finished_at':int(time.time()*1000)})
        print('[STRUCTURE_HIERARCHY_AUDIT] ERROR '+str(e),flush=True)

@app.route('/experiment/audit_structure_hierarchy/start',methods=['GET'])
def experiment_audit_structure_hierarchy_start():
    if _KAIROS_STRUCTURE_HIERARCHY_AUDIT.get('status')=='RUNNING':
        return jsonify({'status':'RUNNING','started':False,'reason':'AUDIT_ALREADY_RUNNING'}),202
    threading.Thread(target=_run_structure_hierarchy_audit,daemon=True).start()
    return jsonify({'status':'STARTING','started':True,'audit':'HTF_M15_50_INTERNAL5','pairs':len(_KAIROS_STRUCTURE_HIERARCHY_PAIRS)}),202

@app.route('/experiment/audit_structure_hierarchy',methods=['GET'])
def experiment_audit_structure_hierarchy_result():
    return jsonify(_KAIROS_STRUCTURE_HIERARCHY_AUDIT)

@app.route('/experiment/audit_btc_execution_chain', methods=['GET'])
def experiment_audit_btc_execution_chain():
    """READ-ONLY: extrai do ultimo replay BTC a genealogia da entrada sem alterar gates."""
    cache=_KAIROS_A_BTC_CACHE.get('result') or {}
    policies=cache.get('policies') or cache
    pol=(policies.get('A_CURRENT') if isinstance(policies,dict) else None) or cache
    metrics=(pol.get('metrics') if isinstance(pol,dict) else None) or pol
    rows=(metrics.get('resolved_signal_autopsy') if isinstance(metrics,dict) else None) or []
    if not rows:
        return jsonify({'ok':False,'reason':'SEM_SINAL_RESOLVIDO_NO_CACHE; rode /experiment/poi_lifecycle_a_btc_1d/start primeiro'}),404
    sig=(rows[-1].get('signal') or {})
    return jsonify({'ok':True,'read_only':True,'pair':sig.get('pair','BTCUSD'),
      'authorization_path':sig.get('authorization_path'),
      'htf_location':sig.get('htf_location'),
      'sweep_audit':{'sweep_tf':sig.get('sweep_tf'),'liquidity_tf':sig.get('liquidity_tf'),
                     'liquidity_type':sig.get('liquidity_type'),'first_capture_ts':sig.get('first_capture_ts'),
                     'sweep_confirm_ts':sig.get('sweep_confirm_ts'),'sweep_level':sig.get('sweep_level'),
                     'sweep_extreme':sig.get('sweep_extreme'),
                     'structural_sweep_audit':sig.get('structural_sweep_audit'),
                     'note':'HTF_POI_TOUCH sem sweep real nao deve reutilizar touch_price como sweep para SL'},
      'm15':{'break_open_ts':sig.get('m15_break_candle_open_ts'),'confirm_ts':sig.get('m15_confirmation_ts'),
             'type':sig.get('m15_confirmation_type'),'level':sig.get('m15_confirmation_level'),
             'poi_shadow_audit':sig.get('poi_shadow_audit')},
      'm5':{'candidate_found':sig.get('m5_refinement_candidate_found'),'type':sig.get('m5_refinement_candidate_type'),
            'top':sig.get('m5_refinement_candidate_top'),'bottom':sig.get('m5_refinement_candidate_bottom'),
            'created_ts':sig.get('m5_refinement_created_ts'),'retest_found':sig.get('m5_refinement_retest_found'),
            'retest_after_ts':sig.get('m5_refinement_retest_after_ts'),'basis':sig.get('refinement_basis'),
            'causal_break_m5':sig.get('causal_break_m5'),'causal_break_level_m15':sig.get('causal_break_level_m15')},
      'execution':{'entry':sig.get('entry'),'sl':sig.get('sl'),'sl_regra':sig.get('sl_regra'),
                   'sl_anchor_tf':sig.get('sl_anchor_tf'),'sl_anchor_class':sig.get('sl_anchor_class'),
                   'sl_anchor_ts':sig.get('sl_anchor_sweep_ts'),'sl_anchor_extreme':sig.get('sl_anchor_extreme'),
                   'sl_audit':sig.get('sl_audit'),'tp1':sig.get('tp1'),'tp2':sig.get('tp2'),
                   'tp1_rr':sig.get('tp1_rr'),'tp2_rr':sig.get('tp2_rr'),
                   'tp1_origem':sig.get('tp1_origem'),'tp2_origem':sig.get('tp2_origem'),
                   'tp_origem':sig.get('tp_origem'),'trade_mode':sig.get('trade_mode'),
                   'risk_abs':(abs(float(sig.get('entry'))-float(sig.get('sl'))) if sig.get('entry') is not None and sig.get('sl') is not None else None),
                   'first_liquidity_target':sig.get('first_liquidity_target'),
                   'structural_target_context':sig.get('structural_target_context'),
                   'structural_target_context_origin':sig.get('structural_target_context_origin'),
                   'target_obstacles_at_entry':sig.get('target_obstacles_at_entry'),
                   'blocking_obstacles_at_entry':sig.get('blocking_obstacles_at_entry')},
      'dealing_ranges':sig.get('dealing_ranges')})

@app.route('/experiment/audit_btc_w1_pivot', methods=['GET'])
def experiment_audit_btc_w1_pivot():
    """Somente leitura: prova o OHLC bruto Bybit do pivot W1 usado pelo Lux50."""
    origin_ts=int(request.args.get('origin_ts','1668988800000'))
    candles=scalp_engine._fetch_bybit_klines_historico('BTCUSDT','W',1825)
    candles=sorted(candles,key=lambda c:c['t'])
    idx=next((i for i,c in enumerate(candles) if c['t']==origin_ts),None)
    if idx is None:
        return jsonify({'ok':False,'origin_ts':origin_ts,'reason':'CANDLE_W1_NAO_ENCONTRADO'}),404
    c=candles[idx]
    left=candles[max(0,idx-2):idx]
    right=candles[idx+1:idx+3]
    swings=scalp_engine._extrair_swings_lux_algo(candles,swing_size=50)
    pivot=next((x for x in swings if x.get('t')==origin_ts and x.get('tipo')=='low'),None)
    return jsonify({'ok':True,'source':'BYBIT_V5_LINEAR_BTCUSDT_W_RAW','origin_ts':origin_ts,
                    'raw_candle':c,'neighbors_before':left,'neighbors_after':right,
                    'lux50_is_swing_low':pivot is not None,'lux50_pivot':pivot})

@app.route('/kairos_v2/auditoria_fvg_h1_btc', methods=['GET'])
def kairos_v2_auditoria_fvg_h1_btc():
    dias=max(1,min(int(request.args.get('dias','7')),31))
    fim_raw=request.args.get('fim_ts_ms')
    fim_ts_ms=int(fim_raw) if fim_raw else None
    lo=float(request.args.get('lo','85000'))
    hi=float(request.args.get('hi','87000'))
    return jsonify(scalp_engine.auditar_fvg_ifvg_h1_btc(dias=dias,fim_ts_ms=fim_ts_ms,lo=lo,hi=hi))

# FASE SEGUINTE — A_CURRENT congelado nos 13 pares.
# Experimental/read-only: scheduler live continua desligado neste servico.
_KAIROS_A13_CACHE = {'status':'IDLE','result':None,'error':None,'started_at':None,'finished_at':None,'current_pair':None}

def _run_kairos_a13_background(dias, fim_ts_ms):
    global _KAIROS_A13_CACHE
    pairs=list(scalp_engine.PARES_MONITORADOS_REPLAY)
    try:
        _KAIROS_A13_CACHE.update({'status':'RUNNING','result':{},'error':None,'started_at':int(time.time()*1000),'finished_at':None,'current_pair':None})
        for idx,pair in enumerate(pairs,1):
            _KAIROS_A13_CACHE['current_pair']=pair
            print(f'[POI_A13_PROGRESS] pair={pair} index={idx}/{len(pairs)} phase=START', flush=True)
            r=scalp_engine.replay_poi_lifecycle_abc_sol(
                dias_historico=dias,
                fim_ts_ms=fim_ts_ms,
                pair=pair,
                policies=('A_CURRENT',),
            )
            _KAIROS_A13_CACHE['result'][pair]=r
            print(f'[POI_A13_PROGRESS] pair={pair} index={idx}/{len(pairs)} phase=DONE', flush=True)
        _KAIROS_A13_CACHE.update({'status':'DONE','finished_at':int(time.time()*1000),'current_pair':None})
        print('[POI_A13_RESULT] '+str(_KAIROS_A13_CACHE['result']), flush=True)
    except Exception as e:
        _KAIROS_A13_CACHE.update({'status':'ERROR','error':str(e),'finished_at':int(time.time()*1000)})
        print('[POI_A13_ERROR] '+str(e), flush=True)

@app.route('/experiment/poi_lifecycle_a_13pairs', methods=['GET'])
def experiment_poi_lifecycle_a_13pairs():
    dias=max(1,min(int(request.args.get('dias','30')),31))
    fim_raw=request.args.get('fim_ts_ms')
    fim_ts_ms=int(fim_raw) if fim_raw else None
    if request.args.get('start') == '1':
        if _KAIROS_A13_CACHE.get('status') != 'RUNNING' and _KAIROS_ABC_CACHE.get('status') != 'RUNNING':
            threading.Thread(target=_run_kairos_a13_background,args=(dias,fim_ts_ms),daemon=True).start()
            return jsonify({'status':'STARTING','started':True,'policy':'A_CURRENT','pairs':scalp_engine.PARES_MONITORADOS_REPLAY,'dias':dias}),202
        return jsonify({'status':_KAIROS_A13_CACHE.get('status'),'started':False,'reason':'REPLAY_ALREADY_RUNNING'}),409
    return jsonify(_KAIROS_A13_CACHE)


@app.route('/experiment/poi_lifecycle_abc_sol', methods=['GET'])
def experiment_poi_lifecycle_abc_sol():
    dias=max(1,min(int(request.args.get('dias','7')),31))
    fim_raw=request.args.get('fim_ts_ms')
    fim_ts_ms=int(fim_raw) if fim_raw else None
    if request.args.get('start') == '1':
        if _KAIROS_ABC_CACHE.get('status') != 'RUNNING':
            threading.Thread(target=_run_kairos_abc_background,args=(dias,fim_ts_ms),daemon=True).start()
        return jsonify({'status':_KAIROS_ABC_CACHE.get('status'),'started':True}),202
    return jsonify(_KAIROS_ABC_CACHE)




_KAIROS_NEAR_LIQ_IMPULSE_AUDIT={'status':'IDLE','result':None,'error':None,'started_at':None,'finished_at':None}

def _build_near_liquidity_impulse_20260927():
    """Read-only: prova qual pool estrutural foi realmente capturada antes do impulso NEAR de 27/09."""
    pair='NEARUSD'
    start_ts=1790467200000
    impulse_ts=1790487900000
    end_ts=1790488800000
    tf_map={'MN':'M','W1':'W','D1':'D','H4':'240','H1':'60','M15':'15','M5':'5'}
    candles={}
    for label,interval in tf_map.items():
        limit=1000 if label in ('H1','M15','M5') else 500
        candles[label]=sorted(scalp_engine._fetch_bybit_klines_historico(pair,interval,limit,fim_ts_ms=end_ts+300000),key=lambda x:x.get('t',0))
    registry=scalp_engine._kairos_structural_registry(candles, impulse_ts)
    pools=[]
    for x in (registry or []):
        level=x.get('price',x.get('nivel',x.get('level')))
        if level is None:
            continue
        try: level=float(level)
        except Exception: continue
        typ=str(x.get('type',x.get('tipo',''))).upper()
        side='HIGH' if typ in ('SWING_HIGH','PDH','PWH','PMH','EQH') or typ.endswith('_HIGH') else ('LOW' if typ in ('SWING_LOW','PDL','PWL','PML','EQL') or typ.endswith('_LOW') else None)
        if side is None:
            continue
        native_tf=str(x.get('tf',''))
        rows=candles['M5']
        first=None
        for k in rows:
            t=k.get('t',0)
            if t < start_ts or t > impulse_ts: continue
            breached=(float(k['h'])>level) if side=='HIGH' else (float(k['l'])<level)
            if breached:
                first={'t':t,'o':k['o'],'h':k['h'],'l':k['l'],'c':k['c'],
                       'reclaim_same_close':(float(k['c'])<level) if side=='HIGH' else (float(k['c'])>level)}
                break
        pools.append({'tf':native_tf,'type':typ,'side':side,'level':level,
                      'registry_state':x.get('state'),'origin_ts':x.get('origin_ts'),
                      'confirmed_ts':x.get('confirmed_ts'),'captured_ts':x.get('captured_ts'),
                      'first_m5_breach':first})
    captured=[x for x in pools if x['first_m5_breach']]
    captured.sort(key=lambda x:(x['first_m5_breach']['t'],x['tf'],x['level']))
    nearest=sorted(pools,key=lambda x:min(abs(float(k['l'])-x['level']) if x['side']=='LOW' else abs(float(k['h'])-x['level']) for k in candles['M5'] if start_ts<=k.get('t',0)<=impulse_ts))[:20] if candles['M5'] else []
    return {'ok':True,'read_only':True,'pair':pair,'window_ts':[start_ts,impulse_ts],
            'impulse_reference_ts':impulse_ts,'captured_structural_pools':captured,
            'captured_count':len(captured),'nearest_structural_pools':nearest,
            'all_structural_pools':pools}


def _run_near_liquidity_impulse_20260927():
    _KAIROS_NEAR_LIQ_IMPULSE_AUDIT.update({'status':'RUNNING','result':None,'error':None,'started_at':int(time.time()*1000),'finished_at':None})
    try:
        result=_build_near_liquidity_impulse_20260927()
        _KAIROS_NEAR_LIQ_IMPULSE_AUDIT.update({'status':'DONE','result':result,'finished_at':int(time.time()*1000)})
        print('[NEAR_LIQ_IMPULSE_AUDIT] DONE captured='+str(result.get('captured_count')),flush=True)
    except Exception as e:
        _KAIROS_NEAR_LIQ_IMPULSE_AUDIT.update({'status':'ERROR','error':str(e),'finished_at':int(time.time()*1000)})
        print('[NEAR_LIQ_IMPULSE_AUDIT] ERROR '+str(e),flush=True)

@app.route('/experiment/audit_near_liquidity_impulse_20260927/start', methods=['GET'])
def experiment_audit_near_liquidity_impulse_20260927_start():
    if _KAIROS_NEAR_LIQ_IMPULSE_AUDIT.get('status')=='RUNNING':
        return jsonify({'status':'RUNNING','started':False}),202
    threading.Thread(target=_run_near_liquidity_impulse_20260927,daemon=True).start()
    return jsonify({'status':'STARTING','started':True,'pair':'NEARUSD'}),202

@app.route('/experiment/audit_near_liquidity_impulse_20260927', methods=['GET'])
def experiment_audit_near_liquidity_impulse_20260927():
    return jsonify(_KAIROS_NEAR_LIQ_IMPULSE_AUDIT)


@app.route('/experiment/previous_day_liquidity', methods=['GET'])
def experiment_previous_day_liquidity():
    """Read-only e leve: ultimo D1 FECHADO de UM par; mostra somente PDH/PDL."""
    pair=str(request.args.get('pair','BTCUSD')).upper().strip()
    allowed=set(scalp_engine.PARES_MONITORADOS_REPLAY)
    if pair not in allowed:
        return Response('Par inválido',status=400,mimetype='text/plain; charset=utf-8')
    try:
        now_ts=int(time.time()*1000)
        # Somente D1. Nada de M15, registry, replay ou scanner.
        d1=sorted(scalp_engine._fetch_bybit_klines_historico(pair,'D',3,now_ts),
                  key=lambda x:x.get('t',0))
        day_ms=86400000
        closed=[c for c in d1 if int(c.get('t',0))+day_ms <= now_ts]
        if not closed:
            return Response('Sem D1 fechado',status=404,mimetype='text/plain; charset=utf-8')
        prev=closed[-1]
        body=(f"{pair}\n"
              f"Máxima de ontem: {prev.get('h')}\n"
              f"Mínima de ontem: {prev.get('l')}\n")
        return Response(body,mimetype='text/plain; charset=utf-8')
    except Exception as e:
        return Response('Erro ao consultar Kairos',status=500,mimetype='text/plain; charset=utf-8')

def _audit_btc_pdl_forward_after_capture(capture_close, expected, wall_now):
    """Read-only: segue apenas candles fechados APOS a captura e prova major50 -> internal5."""
    m15=sorted(scalp_engine._fetch_bybit_klines_historico('BTCUSD','15',3,wall_now),key=lambda x:x.get('t',0))
    closed=[c for c in m15 if int(c.get('t',0))+900000<=wall_now]
    majors=scalp_engine.compute_lux_structure_events(closed,swing_size=50)
    internals=scalp_engine.compute_lux_internal_structure(closed,swing_size=5)
    major=next((e for e in majors if int(e.get('t',0))+900000>capture_close and e.get('direcao')==expected and e.get('tipo') in ('CHoCH','BOS')),None)
    if not major:
        return {'major_swing50':None,'internal_swing5':None,'chain_status':'WAITING_MAJOR_M15'}
    major_close=int(major.get('t',0))+900000
    internal=next((e for e in internals if int(e.get('t',0))>=int(major.get('t',0)) and e.get('direcao')==expected and e.get('tipo') in ('CHoCH','BOS')),None)
    return {'major_swing50':major,'major_confirm_close_ts':major_close,
            'internal_swing5':internal,
            'internal_confirm_close_ts':(int(internal.get('t',0))+900000 if internal else None),
            'chain_status':('MAJOR_AND_INTERNAL_CONFIRMED' if internal else 'WAITING_INTERNAL_M15')}

_KAIROS_BTC_PDL_CHAIN_AUDIT={'status':'IDLE','result':None,'error':None}

def _run_btc_pdl_chain_audit():
    _KAIROS_BTC_PDL_CHAIN_AUDIT.update({'status':'RUNNING','result':None,'error':None})
    try:
        wall_now=int(time.time()*1000)
        d1=sorted(scalp_engine._fetch_bybit_klines_historico('BTCUSD','D',3,wall_now),key=lambda x:x.get('t',0))
        closed=[c for c in d1 if int(c.get('t',0))+86400000 <= wall_now]
        if not closed:
            raise RuntimeError('SEM_D1_FECHADO')
        prev=closed[-1]; pdl=float(prev['l']); valid_from=int(prev['t'])+86400000
        m15_live=sorted(scalp_engine._fetch_bybit_klines_historico('BTCUSD','15',2,wall_now),key=lambda x:x.get('t',0))
        first=next((c for c in m15_live if int(c.get('t',0))>=valid_from and int(c.get('t',0))+900000<=wall_now and float(c['l'])<pdl),None)
        if not first:
            result={'ok':True,'pair':'BTCUSD','PDL':pdl,'state':'ACTIVE','read_only':True}
            _KAIROS_BTC_PDL_CHAIN_AUDIT.update({'status':'DONE','result':result,'error':None})
            return
        capture_open=int(first['t']); capture_close=capture_open+900000
        specs={'W1':('W',1825),'D1':('D',730),'H4':('240',120),'H1':('60',45),'M15':('15',12)}
        candles={}
        for tf,(iv,dias) in specs.items():
            raw=sorted(scalp_engine._fetch_bybit_klines_historico('BTCUSD',iv,dias,capture_close),key=lambda x:x.get('t',0))
            dur={'W1':604800000,'D1':86400000,'H4':14400000,'H1':3600000,'M15':900000}[tf]
            candles[tf]=[c for c in raw if int(c.get('t',0))+dur <= capture_close]
        mapa=scalp_engine._kairos_build_mtf_map(candles)
        def htf_pois(tf):
            d=mapa.get(tf) or {}; out=[]
            for z in list(d.get('zones',[]))+list(d.get('order_blocks',[])):
                born=z.get('flip_ts') or z.get('break_ts') or z.get('created_ts') or z.get('t')
                if born is None or born>capture_close or z.get('state') not in ('ATIVA','TOCADA','PARCIAL','IFVG'): continue
                out.append({'tf':tf,'type':z.get('tipo'),'direction':z.get('direcao'),'bottom':z.get('bottom'),'top':z.get('top'),
                            'state':z.get('state'),'born_ts':born,
                            'contains_pdl':z.get('bottom') is not None and z.get('top') is not None and float(z['bottom'])<=pdl<=float(z['top']),
                            'capture_candle_overlap':z.get('bottom') is not None and z.get('top') is not None and float(first['h'])>=float(z['bottom']) and float(first['l'])<=float(z['top'])})
            return out
        reaction='ACCEPTANCE_CONTINUATION' if float(first['c'])<pdl else 'REJECTION_RECLAIM'
        expected='baixa' if reaction=='ACCEPTANCE_CONTINUATION' else 'alta'
        majors=scalp_engine.compute_lux_structure_events(candles['M15'],swing_size=50)
        internals=scalp_engine.compute_lux_internal_structure(candles['M15'],swing_size=5)
        result={'ok':True,'read_only':True,'pair':'BTCUSD','audit_cutoff_ts':capture_close,'no_lookahead':True,
                'previous_closed_d1':prev,'PDL':pdl,
                'capture':{'open_ts':capture_open,'close_ts':capture_close,'candle':first,'reaction':reaction,'candidate_intention':expected},
                'HTF_map_before_or_at_capture':{'H4':htf_pois('H4'),'H1':htf_pois('H1')},
                'M15_state_known_at_capture':{
                    'major_swing50':next((e for e in reversed(majors) if e.get('t',0)<=capture_open),None),
                    'internal_swing5':next((e for e in reversed(internals) if e.get('t',0)<=capture_open),None)},
                'forward_after_capture': _audit_btc_pdl_forward_after_capture(capture_close, expected, wall_now),
                'next_step':'FORWARD_CHAIN_AUDITED_FROM_CAPTURE_CLOSE'}
        _KAIROS_BTC_PDL_CHAIN_AUDIT.update({'status':'DONE','result':result,'error':None})
    except Exception as e:
        _KAIROS_BTC_PDL_CHAIN_AUDIT.update({'status':'ERROR','result':None,'error':str(e)})

@app.route('/experiment/audit_btc_pdl_chain/start', methods=['GET'])
def experiment_audit_btc_pdl_chain_start():
    if _KAIROS_BTC_PDL_CHAIN_AUDIT.get('status')=='RUNNING':
        return jsonify({'status':'RUNNING','started':False}),202
    threading.Thread(target=_run_btc_pdl_chain_audit,daemon=True).start()
    return jsonify({'status':'STARTING','started':True,'pair':'BTCUSD','read_only':True}),202

@app.route('/experiment/audit_btc_pdl_chain', methods=['GET'])
def experiment_audit_btc_pdl_chain():
    return jsonify(_KAIROS_BTC_PDL_CHAIN_AUDIT)

_KAIROS_BTC_PDL_EXEC_AUDIT={'status':'IDLE','result':None,'error':None}

def _run_btc_pdl_execution_audit():
    _KAIROS_BTC_PDL_EXEC_AUDIT.update({'status':'RUNNING','result':None,'error':None})
    try:
        now_ts=int(time.time()*1000)
        specs={'W1':('W',1825,604800000),'D1':('D',730,86400000),'H4':('240',120,14400000),
               'H1':('60',45,3600000),'M15':('15',3,900000),'M5':('5',3,300000)}
        candles={}
        for tf,(iv,dias,dur) in specs.items():
            raw=sorted(scalp_engine._fetch_bybit_klines_historico('BTCUSD',iv,dias,now_ts),key=lambda x:x.get('t',0))
            candles[tf]=[c for c in raw if int(c.get('t',0))+dur<=now_ts]
        sig=scalp_engine.avaliar_vortex_decision_layer_v2(
            candles['M15'],candles['M5'],candles['D1'],candles_por_tf=candles,
            audit_pair='BTCUSD',liquidity_policy='A_CURRENT')
        result={'ok':True,'read_only':True,'pair':'BTCUSD',
                'signal':sig.get('signal'),'valid':sig.get('valid'),'failure_reason':sig.get('failure_reason'),
                'authorization_path':sig.get('authorization_path'),
                'capture':{'liquidity_tf':sig.get('liquidity_tf'),'liquidity_type':sig.get('liquidity_type'),
                           'level':sig.get('sweep_level'),'first_capture_ts':sig.get('first_capture_ts'),'confirm_ts':sig.get('sweep_confirm_ts')},
                'm15':{'type':sig.get('m15_confirmation_type') or ('CHoCH/BOS' if sig.get('choch_confirmed') else None),
                       'level':sig.get('m15_confirmation_level') or sig.get('choch_level'),
                       'open_ts':sig.get('m15_break_candle_open_ts') or sig.get('choch_timestamp'),
                       'confirm_ts':sig.get('m15_confirmation_ts')},
                'poi':{'type':sig.get('zone_type'),'bottom':sig.get('zone_bottom'),'top':sig.get('zone_top'),
                       'created_ts':sig.get('zone_created_ts'),'shadow':sig.get('poi_shadow_audit')},
                'm5':{'found':sig.get('m5_refinement_candidate_found'),'type':sig.get('m5_refinement_candidate_type'),
                      'bottom':sig.get('m5_refinement_candidate_bottom'),'top':sig.get('m5_refinement_candidate_top'),
                      'created_ts':sig.get('m5_refinement_candidate_ts'),'retest':sig.get('m5_refinement_retest_found'),
                      'retest_after_ts':sig.get('m5_refinement_retest_after_ts')},
                'execution':{'direction':sig.get('direction'),'entry':sig.get('entry'),'sl':sig.get('sl'),
                             'sl_regra':sig.get('sl_regra'),'sl_audit':sig.get('sl_audit'),
                             'be_trigger_1r':sig.get('tp1'),'structural_target':sig.get('tp2'),
                             'target_origin':sig.get('tp2_origem'),'obstacles':sig.get('target_obstacles_at_entry'),
                             'blocking_obstacles':sig.get('blocking_obstacles_at_entry')},
                'note':'Single BTC current-state decision-layer audit; no replay and no strategy changes.'}
        _KAIROS_BTC_PDL_EXEC_AUDIT.update({'status':'DONE','result':result,'error':None})
    except Exception as e:
        _KAIROS_BTC_PDL_EXEC_AUDIT.update({'status':'ERROR','result':None,'error':str(e)})


_KAIROS_BTC_PDL_FULL_EXEC_AUDIT={'status':'IDLE','result':None,'error':None}

def _run_btc_pdl_full_execution_audit():
    _KAIROS_BTC_PDL_FULL_EXEC_AUDIT.update({'status':'RUNNING','result':None,'error':None})
    try:
        cutoff=1790558100000
        wall_now=int(time.time()*1000)
        specs={'W1':('W',1825,604800000),'D1':('D',730,86400000),'H4':('240',120,14400000),
               'H1':('60',45,3600000),'M15':('15',5,900000),'M5':('5',5,300000)}
        candles={}
        for tf,(iv,dias,dur) in specs.items():
            raw=sorted(scalp_engine._fetch_bybit_klines_historico('BTCUSD',iv,dias,wall_now),key=lambda x:x.get('t',0))
            candles[tf]=[x for x in raw if int(x.get('t',0))+dur<=wall_now]
        m15=candles['M15']; m5=candles['M5']
        majors=[e for e in scalp_engine.compute_lux_structure_events(m15,swing_size=50) if e.get('t',0)+900000>cutoff and e.get('direcao')=='baixa']
        major=majors[0] if majors else None
        if not major: raise RuntimeError('SEM_MAJOR_BEARISH_APOS_PDL')
        major_close=major['t']+900000
        internals=[e for e in scalp_engine.compute_lux_internal_structure(m15,swing_size=5) if e.get('t',0)+900000>=major_close and e.get('direcao')=='baixa']
        internal=internals[0] if internals else None
        if not internal: raise RuntimeError('SEM_INTERNAL_BEARISH_APOS_MAJOR')
        internal_close=internal['t']+900000
        mapa=scalp_engine._kairos_build_mtf_map(candles)
        m15zones=[z for z in ((mapa.get('M15') or {}).get('zones') or []) if z.get('direcao')=='baixa' and (z.get('created_ts') or z.get('t') or 0)>=cutoff and (z.get('created_ts') or z.get('t') or 0)<=internal_close and not (z.get('invalidated_ts') is not None and z.get('invalidated_ts')<=internal_close)]
        m15zones.sort(key=lambda z:(z.get('created_ts') or z.get('t') or 0))
        zone=m15zones[0] if m15zones else None
        m5zones=[z for z in ((mapa.get('M5') or {}).get('zones') or []) if z.get('direcao')=='baixa' and (z.get('created_ts') or z.get('t') or 0)>=internal_close and not (z.get('invalidated_ts') is not None and z.get('invalidated_ts')<=internal_close)]
        m5zones.sort(key=lambda z:(z.get('created_ts') or z.get('t') or 0))
        refinement=m5zones[0] if m5zones else None
        retest=None
        if refinement:
            born=refinement.get('created_ts') or refinement.get('t') or 0
            bot=float(refinement['bottom']); top=float(refinement['top'])
            retest=next((x for x in m5 if x.get('t',0)>max(internal_close,born) and float(x['h'])>=bot and float(x['l'])<=top and not (refinement.get('invalidated_ts') is not None and refinement.get('invalidated_ts')<=x.get('t',0))),None)
        entry=(float(refinement['bottom']) if refinement and retest else None)
        sl_info=None; sl_audit=None; targets=[]
        if entry is not None:
            local,sl_audit=scalp_engine._kairos_last_causal_m5_sweep(m5,'SHORT',major.get('leg_start_ts'),internal_close)
            if local and float(local['sweep_extreme'])>entry:
                known=[x for x in m5 if x.get('t',0)<=retest['t']]
                sl=scalp_engine.aplicar_buffer_stop_atr(float(local['sweep_extreme']),'baixa',known)
                invalid=next((x for x in m5 if local['sweep_ts']<x.get('t',0)<retest['t'] and float(x['h'])>=sl),None)
                if not invalid:
                    sl_info={'sl':sl,'sweep_ts':local['sweep_ts'],'sweep_extreme':local['sweep_extreme']}
                    tfmap={tf:[x for x in cs if x.get('t',0)<=retest['t']] for tf,cs in candles.items()}
                    targets=scalp_engine._kairos_structural_targets(tfmap,retest['t'],entry,'SHORT',limit=8)
        risk=(float(sl_info['sl'])-entry) if sl_info else None
        result={'ok':True,'read_only':True,'pair':'BTCUSD','fixed_thesis':'D1_PDL_84062.9','capture_close_ts':cutoff,
                'major_m15':major,'major_confirm_close_ts':major_close,'internal_m15':internal,'internal_confirm_close_ts':internal_close,
                'm15_causal_poi':zone,
                'm5_refinement':refinement,
                'm5_retest':retest,
                'execution':{'direction':'SHORT','entry':entry,'sl':sl_info,'risk':risk,
                             'be_trigger_1r':(entry-risk if risk else None),
                             'nearest_relevant_liquidity':(targets[0] if targets else None),
                             'next_liquidity_targets':targets},
                'first_failure':None if (zone and refinement and retest and sl_info and targets) else
                    ('SEM_POI_M15_CAUSAL' if not zone else 'SEM_REFINAMENTO_M5' if not refinement else 'SEM_RETESTE_M5' if not retest else 'SEM_SL_CAUSAL' if not sl_info else 'SEM_LIQUIDEZ_ALVO'),
                'no_lookahead_selection':True}
        _KAIROS_BTC_PDL_FULL_EXEC_AUDIT.update({'status':'DONE','result':result,'error':None})
    except Exception as e:
        _KAIROS_BTC_PDL_FULL_EXEC_AUDIT.update({'status':'ERROR','result':None,'error':str(e)})

@app.route('/experiment/audit_btc_pdl_full_execution/start',methods=['GET'])
def experiment_audit_btc_pdl_full_execution_start():
    if _KAIROS_BTC_PDL_FULL_EXEC_AUDIT.get('status')=='RUNNING':
        return jsonify({'status':'RUNNING','started':False}),202
    threading.Thread(target=_run_btc_pdl_full_execution_audit,daemon=True).start()
    return jsonify({'status':'STARTING','started':True,'pair':'BTCUSD','read_only':True}),202

@app.route('/experiment/audit_btc_pdl_full_execution',methods=['GET'])
def experiment_audit_btc_pdl_full_execution():
    return jsonify(_KAIROS_BTC_PDL_FULL_EXEC_AUDIT)

@app.route('/experiment/audit_btc_pdl_execution/start',methods=['GET'])
def experiment_audit_btc_pdl_execution_start():
    if _KAIROS_BTC_PDL_EXEC_AUDIT.get('status')=='RUNNING':
        return jsonify({'status':'RUNNING','started':False}),202
    threading.Thread(target=_run_btc_pdl_execution_audit,daemon=True).start()
    return jsonify({'status':'STARTING','started':True,'pair':'BTCUSD','read_only':True}),202

@app.route('/experiment/audit_btc_pdl_execution',methods=['GET'])
def experiment_audit_btc_pdl_execution():
    return jsonify(_KAIROS_BTC_PDL_EXEC_AUDIT)

@app.route('/experiment/audit_btc_pdh_pdl_capture', methods=['GET'])
def experiment_audit_btc_pdh_pdl_capture():
    """Read-only: prova PDH/PDL do ultimo D1 fechado e primeiro toque/captura M15 posterior."""
    try:
        now_ts=int(time.time()*1000)
        d1=sorted(scalp_engine._fetch_bybit_klines_historico('BTCUSD','D',3,now_ts),key=lambda x:x.get('t',0))
        day_ms=86400000
        closed=[c for c in d1 if int(c.get('t',0))+day_ms <= now_ts]
        if not closed:
            return jsonify({'ok':False,'error':'SEM_D1_FECHADO'}),404
        prev=closed[-1]
        pdh=float(prev['h']); pdl=float(prev['l'])
        valid_from=int(prev['t'])+day_ms
        m15=sorted(scalp_engine._fetch_bybit_klines_historico('BTCUSD','15',2,now_ts),key=lambda x:x.get('t',0))
        rows=[c for c in m15 if int(c.get('t',0))>=valid_from and int(c.get('t',0))+900000<=now_ts]
        def audit_level(level,side):
            touched=[]
            for c in rows:
                h=float(c['h']); l=float(c['l']); close=float(c['c'])
                breach=(h>level) if side=='HIGH' else (l<level)
                touch=(h>=level) if side=='HIGH' else (l<=level)
                reclaim=(close<level) if side=='HIGH' else (close>level)
                acceptance=(close>level) if side=='HIGH' else (close<level)
                if touch:
                    touched.append({'t':c['t'],'o':c['o'],'h':c['h'],'l':c['l'],'c':c['c'],
                                    'breach':breach,'reclaim_close':breach and reclaim,
                                    'acceptance_close':breach and acceptance})
            first_touch=touched[0] if touched else None
            first_breach=next((x for x in touched if x['breach']),None)
            return {'level':level,'side':side,'first_touch':first_touch,'first_breach':first_breach,
                    'state':'ACTIVE' if not first_breach else ('CAPTURED_REJECTION_RECLAIM' if first_breach['reclaim_close'] else ('CAPTURED_ACCEPTANCE' if first_breach['acceptance_close'] else 'CAPTURED_UNRESOLVED'))}
        return jsonify({'ok':True,'read_only':True,'pair':'BTCUSD','source':'BYBIT_CLOSED_CANDLES',
                        'previous_closed_d1':prev,'valid_from_ts':valid_from,
                        'PDH':audit_level(pdh,'HIGH'),'PDL':audit_level(pdl,'LOW')})
    except Exception as e:
        return jsonify({'ok':False,'error':str(e)}),500

@app.route('/experiment/audit_near_pdl_20260927', methods=['GET'])
def experiment_audit_near_pdl_20260927():
    start_utc=1790474400000
    end_utc=1790488800000
    d1=sorted(scalp_engine._fetch_bybit_klines_historico('NEARUSD','D',10,fim_ts_ms=end_utc),key=lambda x:x['t'])
    prev=[x for x in d1 if x['t'] < 1790467200000][-1]
    pdl=float(prev['l'])
    m5=sorted([x for x in scalp_engine._fetch_bybit_klines_historico('NEARUSD','5',2,fim_ts_ms=end_utc+300000) if start_utc <= x.get('t',0) <= end_utc],key=lambda x:x['t'])
    rows=[{'t':x['t'],'o':x['o'],'h':x['h'],'l':x['l'],'c':x['c'],'low_minus_pdl':float(x['l'])-pdl,'below_pdl':float(x['l'])<pdl,'reclaim_same_close':float(x['l'])<pdl and float(x['c'])>pdl} for x in m5]
    first=next((x for x in rows if x['below_pdl']),None)
    reclaim=next((x for x in rows if first and x['t']>=first['t'] and float(x['c'])>pdl),None)
    nearest=min(rows,key=lambda x:abs(float(x['l'])-pdl)) if rows else None
    return jsonify({'ok':True,'read_only':True,'pair':'NEARUSD','pdl':pdl,'previous_d1_candle':prev,'window_lisbon':'27/09/2026 03:00-07:00','first_m5_below_pdl':first,'first_reclaim':reclaim,'nearest_m5':nearest,'classification':'SWEEP_AND_RECLAIM' if first and reclaim else ('BREACH_NO_RECLAIM' if first else 'NO_BREACH'),'m5_rows':rows})


# Independent read-only BTC entry-chain replay. Never used by forward/Telegram.
_KAIROS_ENTRY_CHAIN_CACHE={'status':'IDLE','result':None,'error':None}
_KAIROS_ENTRY_CHAIN_LOCK=threading.Lock()

def _run_kairos_entry_chain_audit(dias, fim_ts_ms):
    try:
        raw=scalp_engine.replay_vortex_decision_layer_v2(
            'BTCUSD',dias_historico=dias,fim_ts_ms=fim_ts_ms,
            experimental_poi_policy='A_CURRENT',audit_entries_only=True)
        result={k:raw.get(k) for k in ('erro','pair','janela_fixa','validacao_dados','funil',
                  'distribuicao_motivos_todos_ciclos','entry_audit_candidates','historical_entry_candidates',
                  'entry_phase_events','entry_setup_states','pending_setup_keys','radar_captures','audit_entries_only')}
        _KAIROS_ENTRY_CHAIN_CACHE.update(status='ERROR' if raw.get('erro') else 'DONE',result=result)
    except Exception as exc:
        _KAIROS_ENTRY_CHAIN_CACHE.update(status='ERROR',error=str(exc))
    finally:
        _KAIROS_ENTRY_CHAIN_LOCK.release()

@app.route('/experiment/btc_entry_chain_replay',methods=['GET'])
def experiment_btc_entry_chain_replay():
    if request.args.get('start')=='1':
        try:
            dias=int(request.args.get('dias','1'))
            fim=int(request.args.get('fim_ts_ms') or int(time.time()*1000))
            if dias<1 or dias>7 or fim>int(time.time()*1000):
                raise ValueError('dias 1..7; fim_ts_ms cannot be future')
        except ValueError as exc:
            return jsonify({'error':str(exc)}),400
        if not _KAIROS_ENTRY_CHAIN_LOCK.acquire(blocking=False):
            return jsonify({'status':'RUNNING','started':False}),409
        _KAIROS_ENTRY_CHAIN_CACHE.update(status='RUNNING',result=None,error=None)
        threading.Thread(target=_run_kairos_entry_chain_audit,args=(dias,fim),daemon=True).start()
        return jsonify({'status':'STARTING','started':True,'mode':'ENTRY_CHAIN_ONLY_NO_SL_BE_TP'}),202
    return jsonify(_KAIROS_ENTRY_CHAIN_CACHE)
