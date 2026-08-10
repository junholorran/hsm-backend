from flask import Flask, request, jsonify, send_from_directory
import anthropic
import os
import time
import requests
import sqlite3
import re
import hashlib
import threading
import base64
import io
from datetime import datetime, timezone
from PIL import Image, ImageDraw, ImageFont

import cascade_engine
import scalp_engine

app = Flask(__name__)
client = anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY'))

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')
DB_FILE = '/data/alerts.db'
app.config['DB_FILE'] = DB_FILE

PRECOS_TICKER = {}

CASCADE_STATUS = {}  # pair -> {'result': {...}, 'updated_at': int}
SCALP_STATUS = {}  # pair -> {'result': {...}, 'updated_at': int}
SCALP_ANTECIPADO_STATUS = {}  # pair -> {'result': {...}, 'updated_at': int}
SCALP_INDICADORES_STATUS = {}  # pair -> {'result': {...}, 'updated_at': int}
SCALP_CONTINUACAO_STATUS = {}  # pair -> {'result': {...}, 'updated_at': int}

# ── NOVO: status do modo Normal (CHoCH — reversão), religado no ciclo
# depois de descoberto que nunca era chamado no run_live_cycle. ──
SCALP_NORMAL_STATUS = {}  # pair -> {'result': {...}, 'updated_at': int}

SCALP_RAPIDO_STATUS = {}  # pair -> {'result': {...}, 'updated_at': int}

SCALP_CASCATA_STATUS = {}  # pair -> {'result': {...}, 'updated_at': int}

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
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}, timeout=10)
    except Exception as e:
        print(f"Telegram erro: {e}")


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
cascade_engine.init_cascade_db(DB_FILE)
cascade_engine.init_cascade_signal_db(DB_FILE)
cascade_engine.init_cascade_multi_tf_db(DB_FILE)
scalp_engine.init_scalp_db(DB_FILE)
scalp_engine.init_explicacao_db(DB_FILE)
app.register_blueprint(scalp_engine.explicacao_bp)


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
LIVE_TF_INTERVALS = {'W': 'W', 'D1': 'D', 'H4': '240', 'H1': '60', 'M15': '15', 'M5': '5'}
AUTO_ALERT_SCORE_THRESHOLD = 65

LIVE_TF_CANDLE_LIMIT = {
    'D1': 300,
}
DEFAULT_CANDLE_LIMIT = 200


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
def run_live_cycle(pair, interval_min):
    symbol = LIVE_SYMBOL_MAP.get(pair, pair.replace('USD', 'USDT'))

    candles_por_tf_cache = {}
    for tf_label, interval in LIVE_TF_INTERVALS.items():
        limit = LIVE_TF_CANDLE_LIMIT.get(tf_label, DEFAULT_CANDLE_LIMIT)
        candles = fetch_bybit_klines(symbol, interval, limit)
        candles_por_tf_cache[tf_label] = candles

    cascade_result = None
    if 'D1' in candles_por_tf_cache and 'M15' in candles_por_tf_cache:
        try:
            cascade_result = cascade_engine.process_pair_full(
                DB_FILE, pair,
                candles_por_tf_cache['D1'],
                candles_por_tf_cache['M15'],
                send_telegram,
            )
            CASCADE_STATUS[pair] = {'result': cascade_result, 'updated_at': int(time.time())}
        except Exception as e:
            print(f"[cascade_engine] erro no ciclo de {pair}: {e}")

    if 'D1' in candles_por_tf_cache and 'H4' in candles_por_tf_cache and 'M15' in candles_por_tf_cache:
        try:
            cascade_engine.process_pair_full_multi_tf(
                DB_FILE, pair,
                candles_por_tf_cache['D1'],
                candles_por_tf_cache['H4'],
                candles_por_tf_cache['M15'],
                send_telegram,
            )
        except Exception as e:
            print(f"[cascade_engine] erro no ciclo multi-tf de {pair}: {e}")

    scalp_result = None
    exec_tf = None
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT exec_tf FROM scalp_watch WHERE pair=? AND enabled=1', (pair,))
            row = cursor.fetchone()
        if row and 'D1' in candles_por_tf_cache:
            exec_tf = row[0] or 'M5'
            if exec_tf in candles_por_tf_cache:
                exec_candles = candles_por_tf_cache[exec_tf]
            elif exec_tf == 'M1':
                exec_candles = fetch_bybit_klines(symbol, '1', DEFAULT_CANDLE_LIMIT)
            else:
                exec_candles = None
            if exec_candles:
                try:
                    resolve_pending_journal_trades(pair, exec_candles)
                except Exception as e:
                    print(f"[journal] erro ao resolver pendentes de {pair}: {e}")

                try:
                    for tabela_gestao in (
                        'scalp_signal_state', 'scalp_signal_state_continuacao',
                        'scalp_rapido_signal_state', 'scalp_cascata_signal_state',
                        'scalp_antecipado_signal_state', 'scalp_indicadores_signal_state',
                    ):
                        scalp_engine.gerenciar_trades_abertos(
                            DB_FILE, pair, exec_candles, tabela_gestao, send_telegram,
                        )
                except Exception as e:
                    print(f"[scalp_engine be_parcial] erro ao gerenciar trades abertos de {pair}: {e}")

                try:
                    cascata_result = scalp_engine.process_pair_cascata_smc_com_explicacao(
                        DB_FILE, pair,
                        candles_por_tf_cache.get('W'),
                        candles_por_tf_cache['D1'],
                        candles_por_tf_cache.get('H4'),
                        candles_por_tf_cache.get('H1'),
                        exec_candles,
                        exec_tf,
                        send_telegram,
                    )
                    SCALP_CASCATA_STATUS[pair] = {'result': cascata_result, 'updated_at': int(time.time())}
                    SCALP_STATUS[pair] = {'result': cascata_result, 'updated_at': int(time.time())}
                    scalp_result = cascata_result

                    if cascata_result.get('sinal') and not cascata_result.get('em_cooldown'):
                        save_scalp_signal_to_journal(
                            pair, 'Cascata', exec_tf,
                            cascata_result.get('direcao'),
                            cascata_result.get('score', 100),
                            cascata_result.get('entry'), cascata_result.get('sl'), cascata_result.get('tp'),
                            cascata_result.get('motivo'),
                        )
                except Exception as e:
                    print(f"[scalp_engine cascata] erro no ciclo de {pair}: {e}")

                try:
                    normal_result = scalp_engine.process_pair_scalp_com_explicacao(
                        DB_FILE, pair,
                        candles_por_tf_cache['D1'],
                        exec_candles,
                        exec_tf,
                        send_telegram,
                        h4_candles=candles_por_tf_cache.get('H4'),
                    )
                    SCALP_NORMAL_STATUS[pair] = {'result': normal_result, 'updated_at': int(time.time())}

                    if normal_result.get('motivo') == 'entrada' and not normal_result.get('em_cooldown'):
                        save_scalp_signal_to_journal(
                            pair, 'Normal CHoCH', exec_tf,
                            normal_result.get('direcao'),
                            normal_result.get('score', 0),
                            normal_result.get('entry'), normal_result.get('sl'), normal_result.get('tp'),
                            normal_result.get('motivo'),
                        )
                except Exception as e:
                    print(f"[scalp_engine normal] erro no ciclo de {pair}: {e}")

                try:
                    continuacao_result = scalp_engine.process_pair_scalp_continuacao_com_explicacao(
                        DB_FILE, pair,
                        candles_por_tf_cache['D1'],
                        exec_candles,
                        exec_tf,
                        send_telegram,
                        h4_candles=candles_por_tf_cache.get('H4'),
                    )
                    SCALP_CONTINUACAO_STATUS[pair] = {'result': continuacao_result, 'updated_at': int(time.time())}

                    if continuacao_result.get('motivo') == 'entrada' and not continuacao_result.get('em_cooldown'):
                        save_scalp_signal_to_journal(
                            pair, 'Continuacao BOS', exec_tf,
                            continuacao_result.get('direcao'),
                            continuacao_result.get('score', 0),
                            continuacao_result.get('entry'), continuacao_result.get('sl'), continuacao_result.get('tp'),
                            continuacao_result.get('motivo'),
                        )
                except Exception as e:
                    print(f"[scalp_engine continuacao] erro no ciclo de {pair}: {e}")

                # ── PATCH 09/08: modo Antecipado v2 desativado (fora do
                # padrão Sweep->CHoCH/BOS->FVG->Entrada). Checa
                # scalp_engine.MODOS_ATIVOS antes de rodar — reativa lá
                # se quiser ligar de novo, sem precisar mexer aqui.
                if scalp_engine.MODOS_ATIVOS.get('antecipado_v2', True):
                    try:
                        antecipado_result = scalp_engine.process_pair_scalp_antecipado_v2_com_explicacao(
                            DB_FILE, pair,
                            candles_por_tf_cache['D1'],
                            exec_candles,
                            exec_tf,
                            send_telegram,
                            h4_candles=candles_por_tf_cache.get('H4'),
                        )
                        SCALP_ANTECIPADO_STATUS[pair] = {'result': antecipado_result, 'updated_at': int(time.time())}

                        if antecipado_result.get('sinal') and not antecipado_result.get('em_cooldown'):
                            save_scalp_signal_to_journal(
                                pair, 'Antecipado v2', exec_tf,
                                antecipado_result.get('direcao'),
                                100,
                                antecipado_result.get('entry'), antecipado_result.get('sl'), antecipado_result.get('tp'),
                                antecipado_result.get('motivo'),
                            )
                    except Exception as e:
                        print(f"[scalp_engine antecipado] erro no ciclo de {pair}: {e}")

                # ── PATCH 09/08: modo Confluência de Indicadores
                # desativado (votação sem gate estrutural — 16.3% WR).
                # Checa scalp_engine.MODOS_ATIVOS antes de rodar.
                if scalp_engine.MODOS_ATIVOS.get('confluencia_indicadores', True):
                    try:
                        indicadores_result = scalp_engine.process_pair_scalp_indicadores_com_explicacao(
                            DB_FILE, pair,
                            exec_candles,
                            exec_tf,
                            send_telegram,
                            d1_candles=candles_por_tf_cache.get('D1'),
                        )
                        SCALP_INDICADORES_STATUS[pair] = {'result': indicadores_result, 'updated_at': int(time.time())}

                        if indicadores_result.get('sinal') and not indicadores_result.get('em_cooldown'):
                            save_scalp_signal_to_journal(
                                pair, 'Confluência Indicadores', exec_tf,
                                indicadores_result.get('direcao'),
                                indicadores_result.get('score', 0),
                                indicadores_result.get('entry'), indicadores_result.get('sl'), indicadores_result.get('tp'),
                                indicadores_result.get('motivo'),
                            )
                    except Exception as e:
                        print(f"[scalp_engine indicadores] erro no ciclo de {pair}: {e}")

                # ── PATCH 09/08: modo Scalp Rápido desativado (sem
                # CHoCH, entra direto no sweep+RSI). Checa
                # scalp_engine.MODOS_ATIVOS antes de rodar.
                if scalp_engine.MODOS_ATIVOS.get('scalp_rapido', True):
                    try:
                        rapido_result = scalp_engine.process_pair_scalp_rapido_com_explicacao(
                            DB_FILE, pair,
                            candles_por_tf_cache['D1'],
                            exec_candles,
                            exec_tf,
                            send_telegram,
                        )
                        SCALP_RAPIDO_STATUS[pair] = {'result': rapido_result, 'updated_at': int(time.time())}

                        if rapido_result.get('sinal') and not rapido_result.get('em_cooldown'):
                            save_scalp_signal_to_journal(
                                pair, 'Scalp Rápido', exec_tf,
                                rapido_result.get('direcao'),
                                100,
                                rapido_result.get('entry'), rapido_result.get('sl'), rapido_result.get('tp'),
                                rapido_result.get('motivo'),
                            )
                    except Exception as e:
                        print(f"[scalp_engine rapido] erro no ciclo de {pair}: {e}")

                try:
                    scalp_engine.process_pair_scalp_filtros_shadow(
                        DB_FILE, pair,
                        candles_por_tf_cache['D1'],
                        exec_candles,
                        exec_tf,
                        h4_candles=candles_por_tf_cache.get('H4'),
                    )
                except Exception as e:
                    print(f"[scalp_engine filtros_shadow] erro no ciclo de {pair}: {e}")
    except Exception as e:
        print(f"[scalp_engine] erro no ciclo de {pair}: {e}")

    now = int(time.time())
    resumo_motivo = None
    resumo_score = 0
    if cascade_result:
        resumo_motivo = cascade_result.get('motivo')
        resumo_score = cascade_result.get('score') or 0
    if scalp_result and scalp_result.get('motivo'):
        resumo_motivo = scalp_result['motivo']
        resumo_score = max(resumo_score, scalp_result.get('score') or 0)

    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE live_watch SET last_run=?, last_score=?, last_result=?, updated_at=? WHERE pair=?
        ''', (now, resumo_score, resumo_motivo, now, pair))
        conn.commit()

    return {'pair': pair, 'cascade': cascade_result, 'scalp': scalp_result}


def live_scheduler_loop():
    while True:
        try:
            with sqlite3.connect(DB_FILE) as conn:
                cursor = conn.cursor()
                cursor.execute('SELECT pair, interval_min, last_run FROM live_watch WHERE enabled=1')
                watches = cursor.fetchall()
            now = int(time.time())
            for pair, interval_min, last_run in watches:
                due = (now - (last_run or 0)) >= (interval_min * 60)
                if due:
                    try:
                        run_live_cycle(pair, interval_min)
                        print(f"[live] ciclo concluído: {pair}")
                    except Exception as e:
                        print(f"[live] erro no ciclo de {pair}: {e}")
                        with sqlite3.connect(DB_FILE) as conn2:
                            conn2.execute('UPDATE live_watch SET last_run=? WHERE pair=?', (now, pair))
                            conn2.commit()
        except Exception as e:
            print(f"[live] erro no scheduler: {e}")
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


@app.route('/live/status', methods=['GET'])
def live_watch_status():
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT pair, interval_min, enabled, last_run, last_direction, last_score,
                       last_entry, last_sl, last_tp1, last_tp2, last_result, updated_at
                FROM live_watch
            ''')
            rows = cursor.fetchall()
        watches = []
        for r in rows:
            watches.append({
                'pair': r[0], 'interval_min': r[1], 'enabled': bool(r[2]), 'last_run': r[3],
                'direction': r[4], 'score': r[5], 'entry': r[6], 'sl': r[7],
                'tp1': r[8], 'tp2': r[9], 'result': r[10], 'updated_at': r[11]
            })
        return jsonify({'watches': watches})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/live/history', methods=['GET'])
def live_signals_history():
    try:
        pair_filter = request.args.get('pair', '')
        limit = int(request.args.get('limit', 30))
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            if pair_filter:
                cursor.execute(
                    'SELECT id, pair, created_at, direction, score, entry, sl, tp1, tp2, alerted FROM live_signals WHERE pair=? ORDER BY created_at DESC LIMIT ?',
                    (pair_filter, limit)
                )
            else:
                cursor.execute(
                    'SELECT id, pair, created_at, direction, score, entry, sl, tp1, tp2, alerted FROM live_signals ORDER BY created_at DESC LIMIT ?',
                    (limit,)
                )
            rows = cursor.fetchall()
        signals = []
        for r in rows:
            signals.append({
                'id': r[0], 'pair': r[1], 'created_at': r[2],
                'direction': r[3], 'score': r[4], 'entry': r[5],
                'sl': r[6], 'tp1': r[7], 'tp2': r[8], 'alerted': bool(r[9])
            })
        return jsonify({'signals': signals})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/cascade/status', methods=['GET'])
def cascade_status():
    try:
        pair = request.args.get('pair')
        if pair:
            return jsonify(CASCADE_STATUS.get(pair, {}))
        return jsonify(CASCADE_STATUS)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/gate_shadow_report', methods=['GET'])
def gate_shadow_report():
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT pair, created_at, direction, score, cascade_score, cascade_motivo
                FROM live_signals
                WHERE gate_teria_pulado = 1 AND score >= 60
                ORDER BY created_at DESC
            ''')
            casos_suspeitos = cursor.fetchall()

            cursor.execute('SELECT COUNT(*) FROM live_signals')
            total = cursor.fetchone()[0]

            cursor.execute('SELECT COUNT(*) FROM live_signals WHERE gate_teria_pulado = 1')
            teria_pulado = cursor.fetchone()[0]

        return jsonify({
            'total_ciclos': total,
            'gate_teria_pulado': teria_pulado,
            'pct_economia_estimada': round((teria_pulado / total * 100), 1) if total else 0,
            'casos_suspeitos_score_alto_apesar_do_gate': [
                {'pair': c[0], 'created_at': c[1], 'direction': c[2], 'score_claude': c[3],
                 'cascade_score': c[4], 'cascade_motivo': c[5]}
                for c in casos_suspeitos
            ]
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/scalp/watch', methods=['POST'])
def scalp_watch_start():
    try:
        data = request.json or {}
        pair = data.get('pair')
        exec_tf = data.get('exec_tf', 'M5')
        interval_min = int(data.get('interval_min', 5))
        if not pair:
            return jsonify({'error': 'pair obrigatório'}), 400
        if exec_tf not in ('M1', 'M5', 'M15'):
            return jsonify({'error': 'exec_tf deve ser M1, M5 ou M15'}), 400
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT pair FROM scalp_watch WHERE pair=?', (pair,))
            exists = cursor.fetchone()
            if exists:
                cursor.execute('UPDATE scalp_watch SET exec_tf=?, enabled=1 WHERE pair=?', (exec_tf, pair))
            else:
                cursor.execute(
                    'INSERT INTO scalp_watch (pair, exec_tf, enabled, created_at) VALUES (?, ?, 1, ?)',
                    (pair, exec_tf, int(time.time()))
                )
            cursor.execute('SELECT pair, enabled FROM live_watch WHERE pair=?', (pair,))
            live_row = cursor.fetchone()
            if not live_row:
                cursor.execute(
                    'INSERT INTO live_watch (pair, interval_min, enabled, last_run) VALUES (?, ?, 1, 0)',
                    (pair, interval_min)
                )
            elif live_row[1] == 0:
                cursor.execute('UPDATE live_watch SET enabled=1 WHERE pair=?', (pair,))
            conn.commit()
        return jsonify({'ok': True, 'pair': pair, 'exec_tf': exec_tf, 'interval_min': interval_min})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/scalp/unwatch', methods=['POST'])
def scalp_watch_stop():
    try:
        data = request.json or {}
        pair = data.get('pair')
        if not pair:
            return jsonify({'error': 'pair obrigatório'}), 400
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute('UPDATE scalp_watch SET enabled=0 WHERE pair=?', (pair,))
            conn.commit()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/scalp/status', methods=['GET'])
def scalp_status():
    try:
        pair = request.args.get('pair')
        if pair:
            return jsonify(SCALP_STATUS.get(pair, {}))
        return jsonify(SCALP_STATUS)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/scalp/history', methods=['GET'])
def scalp_history_route():
    try:
        pair = request.args.get('pair')
        limit = int(request.args.get('limit', 30))
        modo = request.args.get('modo', 'normal')
        table = 'scalp_signal_state_continuacao' if modo == 'continuacao' else 'scalp_signal_state'
        return jsonify(scalp_engine.scalp_signal_history(DB_FILE, pair=pair, limit=limit, table=table))
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/scalp_normal/status', methods=['GET'])
def scalp_normal_status():
    try:
        pair = request.args.get('pair')
        if pair:
            return jsonify(SCALP_NORMAL_STATUS.get(pair, {}))
        return jsonify(SCALP_NORMAL_STATUS)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/scalp_antecipado/status', methods=['GET'])
def scalp_antecipado_status():
    try:
        pair = request.args.get('pair')
        if pair:
            return jsonify(SCALP_ANTECIPADO_STATUS.get(pair, {}))
        return jsonify(SCALP_ANTECIPADO_STATUS)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/scalp_indicadores/status', methods=['GET'])
def scalp_indicadores_status():
    try:
        pair = request.args.get('pair')
        if pair:
            return jsonify(SCALP_INDICADORES_STATUS.get(pair, {}))
        return jsonify(SCALP_INDICADORES_STATUS)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/scalp_continuacao/status', methods=['GET'])
def scalp_continuacao_status():
    try:
        pair = request.args.get('pair')
        if pair:
            return jsonify(SCALP_CONTINUACAO_STATUS.get(pair, {}))
        return jsonify(SCALP_CONTINUACAO_STATUS)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/scalp_rapido/status', methods=['GET'])
def scalp_rapido_status():
    try:
        pair = request.args.get('pair')
        if pair:
            return jsonify(SCALP_RAPIDO_STATUS.get(pair, {}))
        return jsonify(SCALP_RAPIDO_STATUS)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/scalp_cascata/status', methods=['GET'])
def scalp_cascata_status():
    try:
        pair = request.args.get('pair')
        if pair:
            return jsonify(SCALP_CASCATA_STATUS.get(pair, {}))
        return jsonify(SCALP_CASCATA_STATUS)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/scalp_cascata/debug_zonas', methods=['GET'])
def scalp_cascata_debug_zonas():
    try:
        pair = request.args.get('pair')
        if not pair:
            return jsonify({'error': 'pair obrigatório'}), 400
        exec_tf = request.args.get('exec_tf', 'M15')
        symbol = LIVE_SYMBOL_MAP.get(pair, pair.replace('USD', 'USDT'))
        interval = LIVE_TF_INTERVALS.get(exec_tf, '15')
        d1_candles = fetch_bybit_klines(symbol, 'D', LIVE_TF_CANDLE_LIMIT.get('D1', DEFAULT_CANDLE_LIMIT))
        exec_candles = fetch_bybit_klines(symbol, interval, DEFAULT_CANDLE_LIMIT)
        debug = scalp_engine.debug_zonas_completo(d1_candles, exec_candles)
        return jsonify(debug)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/scalp_filtros_shadow/report', methods=['GET'])
def scalp_filtros_shadow_report_route():
    try:
        pair = request.args.get('pair')
        limit = int(request.args.get('limit', 50))
        return jsonify(scalp_engine.filtros_shadow_report(DB_FILE, pair=pair, limit=limit))
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/scalp/stats_por_modo', methods=['GET'])
def scalp_stats_por_modo():
    try:
        return jsonify(scalp_engine.gerar_stats_por_modo(DB_FILE))
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/scalp/trades_detalhado/<modo>', methods=['GET'])
def scalp_trades_detalhado_route(modo):
    try:
        limit = int(request.args.get('limit', 200))
        resultado = scalp_engine.listar_trades_detalhado(DB_FILE, modo, limit)
        if 'erro' in resultado:
            return jsonify(resultado), 400
        return jsonify(resultado)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/scalp/modos_disponiveis', methods=['GET'])
def scalp_modos_disponiveis():
    try:
        return jsonify({'modos': list(scalp_engine.MODOS_SCALP.keys())})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


threading.Thread(target=live_scheduler_loop, daemon=True).start()
