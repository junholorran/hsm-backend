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

app = Flask(__name__)
client = anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY'))

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')
DB_FILE = '/data/alerts.db'

PRECOS_TICKER = {}

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
            conn.commit()
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

# ─── PROMPT ICT CACHEADO ─────────────────────────────────────────────────────
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
        max_tokens=16000,
        temperature=0,
        system=[{
            "type": "text",
            "text": system_prompt,
            "cache_control": {"type": "ephemeral"}
        }],
        messages=[{"role": "user", "content": content}]
    )

    raw_text = response.content[0].text

    display_text = raw_text
    if "BLOCO_DADOS_INICIO" in raw_text:
        display_text = raw_text.split("BLOCO_DADOS_INICIO")[0].rstrip()
        display_text = display_text.rstrip("-").rstrip()

    save_cache(cache_key, pair, raw_text, display_text)

    return raw_text, display_text, None


LIVE_SYMBOL_MAP = {
    'BTCUSD': 'BTCUSDT', 'ETHUSD': 'ETHUSDT', 'SOLUSD': 'SOLUSDT', 'XRPUSD': 'XRPUSDT',
    'LINKUSD': 'LINKUSDT', 'ADAUSD': 'ADAUSDT', 'AVAXUSD': 'AVAXUSDT', 'BNBUSD': 'BNBUSDT',
    'AAVEUSD': 'AAVEUSDT', 'ONDOUSD': 'ONDOUSDT', 'INJUSD': 'INJUSDT', 'NEARUSD': 'NEARUSDT',
    'PENDLEUSD': 'PENDLEUSDT', 'SUIUSD': 'SUIUSDT', 'JTOUSD': 'JTOUSDT', 'ETHFIUSD': 'ETHFIUSDT',
    'JUPUSD': 'JUPUSDT', 'ENAUSD': 'ENAUSDT'
}
# PATCH M5: adicionado 'M5':'5' à lista — antes só ia até M15. Isso faz o
# run_live_cycle (que é genérico, usa for tf_label, interval in
# LIVE_TF_INTERVALS.items()) passar a buscar também o candle M5 e mandar
# pro Claude, habilitando gatilho de scalp fino no live automático.
LIVE_TF_INTERVALS = {'D1': 'D', 'H4': '240', 'H1': '60', 'M15': '15', 'M5': '5'}
AUTO_ALERT_SCORE_THRESHOLD = 75


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
        't': int(k[0]), 'o': float(k[1]), 'h': float(k[2]), 'l': float(k[3]), 'c': float(k[4])
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


def render_live_chart_png_base64(candles, pair_label, tf_label):
    W, H = 900, 500
    padL, padR, padT, padB = 60, 20, 40, 30
    img = Image.new('RGB', (W, H), (10, 10, 15))
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.load_default()
    except Exception:
        font = None

    closes = [c['c'] for c in candles]
    highs = [c['h'] for c in candles]
    lows = [c['l'] for c in candles]
    max_p, min_p = max(highs), min(lows)
    rng = (max_p - min_p) or 1
    plot_w = W - padL - padR
    plot_h = H - padT - padB
    cw = plot_w / len(candles)

    def x_for(i):
        return padL + i * cw + cw / 2

    def y_for(p):
        return padT + plot_h - ((p - min_p) / rng) * plot_h

    for i in range(5):
        yy = padT + (plot_h / 4) * i
        draw.line([(padL, yy), (W - padR, yy)], fill=(42, 42, 58), width=1)
        price_at_y = max_p - (rng / 4) * i
        draw.text((4, yy - 5), f"{price_at_y:.2f}", fill=(110, 118, 129), font=font)

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

    last_close = candles[-1]['c']
    draw.text((padL, 10), f"{pair_label} · {tf_label} · ${last_close:,.2f}", fill=(240, 192, 64), font=font)
    stamp = 'GERADO EM: ' + datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M') + ' UTC'
    draw.text((W - padR - 220, 10), stamp, fill=(255, 229, 138), font=font)

    buf = io.BytesIO()
    img.save(buf, format='PNG')
    return base64.b64encode(buf.getvalue()).decode('utf-8')


def run_live_cycle(pair, interval_min):
    symbol = LIVE_SYMBOL_MAP.get(pair, pair.replace('USD', 'USDT'))
    pair_label = pair.replace('USD', '')

    images_by_tf = {}
    for tf_label, interval in LIVE_TF_INTERVALS.items():
        candles = fetch_bybit_klines(symbol, interval, 200)
        base64_png = render_live_chart_png_base64(candles, pair_label, tf_label)
        images_by_tf[tf_label] = {'base64': base64_png, 'mimeType': 'image/png'}

    raw_text, display_text, error = analyze_single_pair(pair, images_by_tf, category='ict')
    if error:
        raise Exception(error)

    direction, score, sl, tps, tf_label_full, entry = extract_trade_info(raw_text, ','.join(LIVE_TF_INTERVALS.keys()))
    tp1 = tps[0] if len(tps) > 0 else ''
    tp2 = tps[1] if len(tps) > 1 else ''

    now = int(time.time())
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE live_watch SET last_run=?, last_direction=?, last_score=?, last_entry=?,
            last_sl=?, last_tp1=?, last_tp2=?, last_result=?, updated_at=? WHERE pair=?
        ''', (now, direction, score, entry, sl, tp1, tp2, display_text, now, pair))
        conn.commit()

        journal_id = f"{pair}_{int(time.time() * 1000)}"
        cursor.execute('''
            INSERT INTO journal (id, pair, created_at, direction, score, entry, sl, tp1, tp2, tp3, timeframes, analysis, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (journal_id, pair, now, direction, score, entry, sl, tp1, tp2,
              tps[2] if len(tps) > 2 else '', ','.join(LIVE_TF_INTERVALS.keys()), raw_text, 'pending'))
        conn.commit()

    if direction and direction != 'NEUTRO' and score >= AUTO_ALERT_SCORE_THRESHOLD and entry:
        signature = f"{direction}|{entry}"
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT last_alerted_signature FROM live_watch WHERE pair=?', (pair,))
            row = cursor.fetchone()
            already = row[0] if row else None
            if already != signature:
                arrow = "📈" if direction == "LONG" else "📉"
                msg = f"🔴 <b>Trade Ao Vivo (servidor) — {pair}</b>\n\n"
                msg += f"{arrow} <b>{direction}</b> | Score {score}/100\n"
                if entry:
                    msg += f"📍 <b>Entrada:</b> ${entry}\n"
                if sl:
                    msg += f"🛑 <b>Stop:</b> ${sl}\n"
                if tp1:
                    msg += f"✅ <b>TP1:</b> ${tp1}\n"
                if tp2:
                    msg += f"✅ <b>TP2:</b> ${tp2}\n"
                msg += f"\n💡 <i>Vigiando sozinho no servidor, a cada {interval_min}min.</i>"
                send_telegram(msg)
                cursor.execute('UPDATE live_watch SET last_alerted_signature=? WHERE pair=?', (signature, pair))
                conn.commit()

    return {'pair': pair, 'direction': direction, 'score': score, 'entry': entry, 'sl': sl, 'tp1': tp1, 'tp2': tp2}


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


# ─── NOTÍCIAS CRIPTO (NOVO) ──────────────────────────────────────────────
# RSS público, gratuito, sem chave de API. Busca pelo servidor (não pelo
# navegador) porque RSS geralmente bloqueia fetch direto do browser (CORS).
# Classificação bullish/bearish é por palavra-chave — simples e de graça,
# não usa a API do Claude (evita gastar créditos a cada notícia).
NEWS_RSS_FEEDS = [
    'https://www.coindesk.com/arc/outboundfeeds/rss/',
    'https://cointelegraph.com/rss',
]

NEWS_BULLISH_WORDS = [
    'rally', 'surge', 'soars', 'approval', 'approved', 'etf approval',
    'record high', 'all-time high', 'breaks', 'bullish', 'gains',
    'adoption', 'inflow', 'buy', 'rate cut', 'cut rates', 'stimulus',
]
NEWS_BEARISH_WORDS = [
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


# NOVO: palavras que marcam uma notícia como MAIS RELEVANTE — prioriza
# essas no topo da lista, em vez de mostrar qualquer manchete de altcoin
# menor. Cobre os ativos que o Kairos já acompanha + eventos macro grandes.
NEWS_RELEVANT_WORDS = [
    'bitcoin', 'btc', 'ethereum', 'eth', 'solana', 'sol',
    'fed', 'federal reserve', 'fomc', 'cpi', 'inflation', 'interest rate',
    'sec', 'etf', 'regulation', 'regulatory', 'powell', 'treasury',
    'binance', 'coinbase', 'stablecoin', 'liquidation', 'whale',
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
    # ordena aproximadamente pelo texto da data (RSS já vem em ordem cronológica na maioria dos feeds)
    # NOVO: ordena por relevância (mais palavras-chave importantes = mais
    # pro topo), mantendo a ordem cronológica original como desempate —
    # assim as notícias que realmente importam aparecem primeiro, mesmo
    # que não sejam as mais recentes dos feeds combinados.
    for i, item in enumerate(items):
        item['_relevance'] = relevance_score(item['title'])
        item['_originalOrder'] = i
    items.sort(key=lambda x: (-x['_relevance'], x['_originalOrder']))
    for item in items:
        del item['_relevance']
        del item['_originalOrder']

    return items[:limit]



@app.route('/news', methods=['GET'])
def get_news():
    try:
        items = fetch_crypto_news(limit=8)
        return jsonify({'news': items})
    except Exception as e:
        return jsonify({'error': str(e), 'news': []}), 500


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


threading.Thread(target=live_scheduler_loop, daemon=True).start()
