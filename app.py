from flask import Flask, request, jsonify, send_from_directory
import anthropic
import os
import time
import requests
import sqlite3
import re

app = Flask(__name__)
client = anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY'))

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')
DB_FILE = 'alerts.db'

PRECOS_TICKER = {}

# --- REGEX BLINDADAS CONTRA HTML E ESPAÇOS ---
RE_SCORE = re.compile(r'SCORE\s*OPERACIONAL\s*:[^\d]*(\d{1,3})\s*/\s*100', re.IGNORECASE)
RE_SL = re.compile(r'Stop\s*Loss\s*[^:]*:[^\d]*\$?\s*([\d,.]+)', re.IGNORECASE)
RE_TP = re.compile(r'Take\s*Profit\s*\d?\s*[^:]*:[^\d]*\$?\s*([\d,.]+)', re.IGNORECASE)
RE_ENTRY = re.compile(r'Entrada\s*Conservadora\s*[^:\n]{0,30}:[^\d]*\$?\s*([\d,.]+)', re.IGNORECASE)
RE_STYLE = re.compile(r'(scalp|swing|intraday)', re.IGNORECASE)
TIMEFRAMES_MAP = ["D1", "H4", "H1", "M15", "M5", "M1"]

def extract_trade_info(analysis, timeframes_str):
    if not analysis:
        return "LONG", 50, "", [], "", ""
    tl = analysis.lower()
    sell_count = sum(tl.count(w) for w in ['short', 'bearish', 'sell', 'venda', 'vende'])
    buy_count = sum(tl.count(w) for w in ['long', 'bullish', 'buy', 'compra'])
    direction = "SHORT" if sell_count > buy_count else "LONG"
    sm = RE_SCORE.search(analysis)
    score = int(sm.group(1)) if sm else 50
    if score > 100: score = 100
    setup1_block = analysis
    if "SETUP #2" in analysis:
        setup1_block = analysis.split("SETUP #2")[0]
    sl_match = RE_SL.search(setup1_block)
    sl = sl_match.group(1).replace(',', '.') if sl_match else ""
    tp_matches = RE_TP.findall(setup1_block)
    tps = [tp.replace(',', '.') for tp in tp_matches[:3]]
    entry_match = RE_ENTRY.search(setup1_block)
    entry = entry_match.group(1).replace(',', '.') if entry_match else ""
    tfs = timeframes_str.upper() if timeframes_str else ""
    tf_components = []
    style_match = RE_STYLE.search(setup1_block)
    if style_match:
        tf_components.append(style_match.group(1).upper())
    found_tfs = [tf for tf in TIMEFRAMES_MAP if tf in tfs]
    tf_components.extend(found_tfs)
    tf_label = " ".join(tf_components)
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
            if timeframes_str is None: timeframes_str = ""
            
            current_price = PRECOS_TICKER.get(pair)
            if current_price is None: continue
            
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
                    if entry: msg += f"📍 <b>Entrada Conservadora:</b> ${entry}\n"
                    if sl: msg += f"🛑 <b>Stop Loss (SL):</b> ${sl}\n"
                    if tps:
                        for i, tp in enumerate(tps, 1): msg += f"✅ <b>Take Profit {i} (TP{i}):</b> ${tp}\n"
                    else: msg += f"✅ <b>Take Profit (TP):</b> N/A\n"
                    msg += f"-------------------------------------\n"
                    msg += f"{emoji_score} <b>Score Operacional:</b> {score}/100\n\n"
                    msg += f"💡 <i>Aguardar mitigação de FVG ou Order Block se aplicável.</i>"
                    send_telegram(msg)
    except Exception as e:
        print(f"Erro ao processar alertas inline: {e}")

init_db()

# ─── PROMPT ICT CACHEADO ─────────────────────────────────────────────────────
ICT_SYSTEM_PROMPT = (
    "Es um mentor institucional ICT (Inner Circle Trader) e SMC de elite. Analistas de topo mundial. Zero tolerancia para analises vagas ou ficticias.\n\n"

    "REGRA DE OURO ABSOLUTA:\n"
    "- NUNCA recomendar LONG quando o preco esta no topo do range diario ou em resistencia forte\n"
    "- NUNCA recomendar SHORT quando o preco esta no fundo do range diario ou em suporte forte\n"
    "- NUNCA inventar niveis — todos os valores devem ser visiveis nos graficos fornecidos\n"
    "- Se nao ha setup claro = dizer FORA DO MERCADO sem hesitar\n"
    "- Stop Loss SEMPRE atras de estrutura ICT real — NUNCA arbitrario\n"
    "- TP SEMPRE onde o mercado vai buscar liquidez real — BSL/SSL identificados\n\n"

    "ANALISE OBRIGATORIA - 15 CAMADAS ICT:\n"
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
    "12. Score ICT (0-100 baseado em confluencias reais)\n"
    "13. Wyckoff Phase (Acumulacao/Markup/Distribuicao/Markdown + Spring/UTAD)\n"
    "14. Power of Three - PO3/AMD (Accumulation/Manipulation/Distribution intraday)\n"
    "15. IFVG - Inversion Fair Value Gap (FVGs invertidos)\n\n"

    "ANALISE TECNICA OBRIGATORIA POR TIMEFRAME:\n"
    "Para cada TF disponivel (D1, H4, H1, M15, M5) identificar:\n"
    "- RSI: valor exato + sobrecomprado (>70) / sobrevendido (<30) / neutro\n"
    "- MACD: DIF vs DEA — cruzamento bullish/bearish + divergencia se existir\n"
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

    "FORMATACAO:\n"
    "- NUNCA uses markdown (* # [ ])\n"
    "- Usa APENAS tags HTML: <b> <i> <u>\n"
    "- Fecha todas as tags HTML abertas\n"
)

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

        dynamic_prompt = (
            f"Analisa os graficos de {pair} nos timeframes ({', '.join(valid_tfs)}) com maxima precisao e objetividade.\n\n"
            "FORMATO OBRIGATORIO DA RESPOSTA — SEGUIR EXATAMENTE:\n\n"

            "<b>⚡ KAIROS MENTOR — " + pair + "</b>\n"
            "Data/Hora: [data e hora UTC]\n\n"
            "---\n\n"

            "<b>🧭 SENTIDO DO MERCADO</b>\n"
            "<b>Dominante:</b> [📈 LONG / 📉 SHORT / ⏸ NEUTRO — FORA]\n"
            "<b>Tipo de Setup:</b> [TENDENCIA / REVERSAO / CONTINUIDADE]\n"
            "<b>Proximo Passo Logico:</b> [1 frase direta — o que o mercado vai fazer]\n"
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

            "<b>📊 ANALISE COMPLETA — 15 CAMADAS ICT</b>\n\n"

            "<b>BIAS DE MERCADO</b>\n"
            "- <b>D1:</b> [BULLISH/BEARISH] — RSI:[valor] MACD:[bull/bear] Estoc:[valor]\n"
            "- <b>H4:</b> [BULLISH/BEARISH] — RSI:[valor] MACD:[bull/bear] Estoc:[valor]\n"
            "- <b>H1:</b> [bias] — RSI:[valor] MACD:[bull/bear] Estoc:[valor]\n"
            "- <b>M15/M5:</b> [bias] — RSI:[valor] MACD:[bull/bear] Estoc:[valor]\n\n"

            "<b>LIQUIDEZ & ESTRUTURA</b>\n"
            "- <b>BSL:</b> $[valor] — [contexto]\n"
            "- <b>SSL:</b> $[valor] — [contexto]\n"
            "- <b>AVISO LIQUIDEZ:</b> [BSL/SSL que pode varrer stop]\n"
            "- <b>OB Bearish:</b> [zona exata]\n"
            "- <b>OB Bullish:</b> [zona exata]\n"
            "- <b>FVG Aberto:</b> [zona e status]\n"
            "- <b>IFVG:</b> [zona e status]\n"
            "- <b>Breaker Block:</b> [zona se existir]\n"
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
            "- Confluencias ativas: [lista]\n"
            "- Penalizacoes: [lista]\n\n"

            "<b>RECOMENDACAO FINAL</b>\n"
            "[narrativa completa mas objetiva — maximo 5 linhas]"
        )

        content = []
        for tf in valid_tfs:
            img = images[tf]
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
            system=[{
                "type": "text",
                "text": ICT_SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"}
            }],
            messages=[{"role": "user", "content": content}]
        )

        result_text = response.content[0].text

        direction, score, sl, tps, tf_label, entry = extract_trade_info(result_text, ','.join(valid_tfs))
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
                    ','.join(valid_tfs), result_text, 'pending'
                ))
                conn.commit()
        except Exception as e:
            print(f"Erro ao guardar no journal: {e}")

        return jsonify({'result': result_text, 'timeframes': ','.join(valid_tfs), 'journal_id': journal_id})
    except Exception as e:
        return jsonify({'error': f"Erro na API Anthropic: {str(e)}"}), 500

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

        score_brackets = {'75-100': {'w':0,'l':0}, '60-74': {'w':0,'l':0}, '50-59': {'w':0,'l':0}}
        for r in rows:
            s = r[2]
            if s >= 75: bracket = '75-100'
            elif s >= 60: bracket = '60-74'
            else: bracket = '50-59'
            if r[0] == 'win': score_brackets[bracket]['w'] += 1
            else: score_brackets[bracket]['l'] += 1

        pair_stats = {}
        for r in rows:
            p = r[3]
            if p not in pair_stats: pair_stats[p] = {'w':0,'l':0,'pnl':0}
            pair_stats[p]['pnl'] += r[1]
            if r[0] == 'win': pair_stats[p]['w'] += 1
            else: pair_stats[p]['l'] += 1

        return jsonify({
            'total_trades': total_trades, 'wins': wins, 'losses': losses,
            'total_pnl': round(total_pnl, 2), 'win_rate': win_rate,
            'score_brackets': score_brackets, 'pair_stats': pair_stats
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500
