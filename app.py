from flask import Flask, request, jsonify, send_from_directory
import anthropic
import os
import threading
import time
import requests
import sqlite3
import socket
import re

app = Flask(__name__)
client = anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY'))

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')
DB_FILE = 'alerts.db'

PRECOS_TICKER = {}

RE_SCORE = re.compile(r'(\d{1,3})\s*/\s*100')
RE_SL = re.compile(r'Stop\s*(?:<\/b>)?\s*Loss:\s*(?:<\/b>)?\s*\$?([\d]+[.,]?[\d]*)', re.IGNORECASE)
RE_TP = re.compile(r'Take\s*(?:<\/b>)?\s*Profit\s*(?:<\/b>)?\s*\d:\s*(?:<\/b>)?\s*\$?([\d]+[.,]?[\d]*)', re.IGNORECASE)
RE_STYLE = re.compile(r'(scalp|swing|intraday)', re.IGNORECASE)
TIMEFRAMES_MAP = ["D1", "H4", "H1", "M15", "M5", "M1"]

def extract_trade_info(analysis, timeframes_str):
   if not analysis:
       return "LONG", 50, "", [], ""

   tl = analysis.lower()

   sell_count = sum(tl.count(w) for w in ['short', 'bearish', 'sell', 'venda', 'vende'])
   buy_count = sum(tl.count(w) for w in ['long', 'bullish', 'buy', 'compra'])
   direction = "SHORT" if sell_count > buy_count else "LONG"

   sm = RE_SCORE.search(analysis)
   score = int(sm.group(1)) if sm else 50

   sl_match = RE_SL.search(analysis)
   sl = sl_match.group(1).replace(',', '.') if sl_match else ""

   tp_matches = RE_TP.findall(analysis)
   tps = [tp.replace(',', '.') for tp in tp_matches[:3]]

   tfs = timeframes_str.upper() if timeframes_str else ""
   tf_components = []

   style_match = RE_STYLE.search(analysis)
   if style_match:
       tf_components.append(style_match.group(1).upper())

   found_tfs = [tf for tf in TIMEFRAMES_MAP if tf in tfs]
   tf_components.extend(found_tfs)
   tf_label = " ".join(tf_components)

   return direction, score, sl, tps, tf_label


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
           try:
               cursor.execute("ALTER TABLE alerts ADD COLUMN timeframes TEXT")
           except sqlite3.OperationalError:
               pass
           conn.commit()
       print("Base de dados SQLite inicializada com sucesso!")
   except Exception as e:
       print(f"Erro ao inicializar Base de Dados: {e}")

def send_telegram(message):
   try:
       url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
       resp = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}, timeout=10)
       print(f"Telegram enviado: {resp.status_code}")
   except Exception as e:
       print(f"Telegram erro: {e}")

def price_monitor():
   print("Monitor de Precos Ativo! A ler dados enviados pelo Browser.")
   while True:
       try:
           with sqlite3.connect(DB_FILE) as conn:
               cursor = conn.cursor()
               cursor.execute("SELECT id, pair, target, analysis, timeframes FROM alerts")
               db_alerts = cursor.fetchall()

           if not db_alerts:
               time.sleep(2.0)
               continue

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
                       print(f"TOQUE DETETADO! {pair} a ${current_price} (Alvo: {target})")

                       direction, score, sl, tps, tf_label = extract_trade_info(analysis, timeframes_str)
                       arrow = "📈" if direction == "LONG" else "📉"

                       msg = f"🎯 <b>{pair} ATINGIDO!</b>\n"
                       msg += f"{arrow} <b>{direction}</b> | {tf_label}\n"
                       msg += f"💰 Preco: ${current_price:,.2f}\n"
                       msg += f"🎯 Alvo era: ${target:,.2f}\n"
                       if sl:
                           msg += f"🛑 SL: ${sl}\n"
                       for i, tp in enumerate(tps, 1):
                           msg += f"✅ TP{i}: ${tp}\n"
                       msg += f"⭐ Score: {score}/100"

                       send_telegram(msg)

       except Exception as e:
           print(f"Monitor erro: {e}")

       time.sleep(1.5)

init_db()

_lock_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
_lock_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
   _lock_socket.bind(('127.0.0.1', 48484))
   monitor_thread = threading.Thread(target=price_monitor, daemon=True)
   monitor_thread.start()
   print("Worker principal assumiu o monitor de precos.")
except socket.error:
   print("Worker secundario detetado. Monitor ignorado neste processo.")

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

       prompt = (
           "Es um mentor institucional ICT (Inner Circle Trader) e SMC de elite.\n"
           f"Analisa os graficos de {pair} nos timeframes ({', '.join(valid_tfs)}) com maxima precisao.\n\n"
           "ANALISE OBRIGATORIA - 15 CAMADAS ICT:\n"
           "1. HTF Narrative & Daily Bias (D1/W1 - tendencia macro)\n"
           "2. Liquidez Pendente (BSL e SSL identificados com valores exatos)\n"
           "3. Premium vs Discount Zone (Fibonacci 50% do range - onde esta o preco agora)\n"
           "4. Order Blocks (OB bullish e bearish com zonas exatas por TF)\n"
           "5. Fair Value Gaps (FVG com zonas exatas e status: preenchido/aberto)\n"
           "6. CHoCH / MSS (confirmado ou potencial, com nivel exato)\n"
           "7. Liquidity Sweeps (varreduras recentes com valores exatos e qual liquidez foi varrida)\n"
           "8. Mitigation & Breaker Blocks\n"
           "9. Killzones & Session Patterns (Asia/London/NY - qual esta ativa)\n"
           "10. Midnight Open (valor exato e posicao do preco em relacao a ele)\n"
           "11. OTE - Optimal Trade Entry (calcular 61.8%, 70.5%, 79% do swing criado pelo sweep mais recente)\n"
           "12. Score ICT (0-100 baseado em confluencias)\n"
           "13. Wyckoff Phase (identificar fase atual: Acumulacao/Markup/Distribuicao/Markdown + Spring/UTAD se visivel)\n"
           "14. Power of Three - PO3/AMD (identificar fase Accumulation/Manipulation/Distribution intraday)\n"
           "15. IFVG - Inversion Fair Value Gap (FVGs que inverteram de bullish para bearish ou vice-versa)\n\n"
           "NARRATIVA ICT COMPLETA OBRIGATORIA:\n"
           "Cada setup deve seguir esta sequencia logica:\n"
           "1. SWEEP: qual liquidez foi varrida, onde e quando\n"
           "2. CHoCH: confirmacao de reversao com nivel exato\n"
           "3. OTE: tracar fibonacci do swing criado pelo sweep (low para high ou high para low)\n"
           "4. ENTRADA: no retrace para 61.8%, 70.5% ou 79% com trigger exato\n"
           "5. GESTAO POS-ENTRADA: o que esperar depois da entrada\n"
           "6. PROXIMOS ALVOS: onde o mercado vai buscar liquidez apos cada TP\n"
           "7. CENARIOS ALTERNATIVOS: re-sweep, invalidacao, continuacao\n\n"
           "ANALISE DE ENTRADA AVANCADA:\n"
           "- Entrada Agressiva: limit order no nivel OTE (61.8% ou 70.5% do swing)\n"
           "- Entrada Conservadora: apenas apos fechamento de vela M15 confirmando rejeicao no OTE\n"
           "- Trigger Exato: identificar padrao de vela obrigatorio (Engolfo Bearish/Bullish, Pin Bar, Inside Bar)\n"
           "- Nivel OTE: identificar se entrada coincide com 61.8%, 70.5% ou 79% do swing relevante\n"
           "- IFVG: verificar se entrada coincide com zona de IFVG para confluencia maxima\n"
           "- Volume: vela de confirmacao deve ter volume acima da MA20\n"
           "- Invalidacao em Tempo Real: nivel exato que invalida o setup ANTES da entrada\n"
           "- Confluencias Ativas: contar quantas confluencias ICT batem (minimo 3 para entrada)\n"
           "- Probabilidade: 3 confluencias=60%, 4=70%, 5=80%, 6+=90%\n\n"
           "FORMATO OBRIGATORIO DA RESPOSTA:\n\n"
           "<b>ANALISE MULTI-TIMEFRAME " + pair + "</b>\n"
           "Data/Hora: [data e hora UTC]\n\n"
           "---\n\n"
           "<b>BIAS DE MERCADO</b>\n"
           "- <b>D1:</b> [BULLISH/BEARISH] - [justificativa detalhada com MAs, RSI, MACD]\n"
           "- <b>H4:</b> [BULLISH/BEARISH] - [justificativa]\n"
           "- <b>H1:</b> [bias] - [justificativa]\n"
           "- <b>M15/M5:</b> [bias] - [justificativa]\n\n"
           "---\n\n"
           "<b>LIQUIDEZ & ESTRUTURA</b>\n"
           "- <b>Zona de Liquidez Alta (BSL):</b> [valor] - [contexto]\n"
           "- <b>Zona de Liquidez Baixa (SSL):</b> [valor] - [contexto]\n"
           "- <b>Suportes Criticos:</b> [valores]\n"
           "- <b>OB Bearish Relevante:</b> [zona]\n"
           "- <b>OB Bullish:</b> [zona]\n"
           "- <b>FVG Aberto:</b> [zona e status]\n"
           "- <b>IFVG:</b> [zona, direcao invertida e status: ativo/mitigado]\n"
           "- <b>CHoCH:</b> [status e nivel exato]\n\n"
           "---\n\n"
           "<b>NARRATIVA ICT DO MERCADO</b>\n"
           "- <b>Sweep Identificado:</b> [qual liquidez foi varrida, valor exato e TF]\n"
           "- <b>Swing Criado:</b> de [low] para [high] (ou high para low)\n"
           "- <b>CHoCH Confirmado:</b> [nivel exato e TF]\n"
           "- <b>OTE do Swing:</b> 61.8%=[valor] | 70.5%=[valor] | 79%=[valor]\n"
           "- <b>IFVG Relevante:</b> [zona de FVG invertido que coincide com entrada]\n"
           "- <b>Narrativa Atual:</b> [descricao do que o mercado esta a fazer e porque]\n\n"
           "---\n\n"
           "<b>POWER OF THREE - AMD</b>\n"
           "- <b>Fase Atual:</b> [Accumulation / Manipulation / Distribution]\n"
           "- <b>Accumulation:</b> [onde e quando o mercado consolidou - sessao Asia]\n"
           "- <b>Manipulation:</b> [spike identificado - direcao, valor exato, sessao London/NY]\n"
           "- <b>Distribution:</b> [direcao real esperada apos manipulacao - alvo]\n"
           "- <b>Midnight Open:</b> [valor] - preco [acima/abaixo] = manipulacao [bullish/bearish]\n"
           "- <b>Aviso PO3:</b> [se spike de manipulacao ainda em curso - AGUARDAR antes de entrar]\n\n"
           "---\n\n"
           "<b>WYCKOFF PHASE</b>\n"
           "- <b>Fase Atual:</b> [Acumulacao/Markup/Distribuicao/Markdown]\n"
           "- <b>Evento Wyckoff:</b> [Spring/UTAD/LPS/SOW se identificavel]\n"
           "- <b>Confluencia ICT:</b> [como alinha com sweep, CHoCH e OTE]\n\n"
           "---\n\n"
           "<b>KILLZONES & MIDNIGHT OPEN</b>\n"
           "- <b>Sessao Ativa:</b> [Asia/London/NY]\n"
           "- <b>Midnight Open:</b> [valor exato]\n"
           "- <b>Posicao do Preco:</b> [PREMIUM/DISCOUNT/NO NIVEL]\n\n"
           "---\n\n"
           "<b>OTE - OPTIMAL TRADE ENTRY</b>\n"
           "- <b>Swing relevante:</b> de [low] para [high]\n"
           "- <b>Origem do Swing:</b> [sweep que originou este swing]\n"
           "- <b>50% (Equilibrio):</b> [valor]\n"
           "- <b>OTE 61.8%:</b> [valor]\n"
           "- <b>OTE 70.5%:</b> [valor]\n"
           "- <b>OTE 79%:</b> [valor]\n"
           "- <b>IFVG na zona OTE:</b> [sim/nao - zona exata se existir]\n"
           "- <b>Zona de entrada ideal SHORT:</b> [range]\n"
           "- <b>Zona de entrada ideal LONG:</b> [range]\n\n"
           "---\n\n"
           "<b>SETUPS IDENTIFICADOS</b>\n\n"
           "---\n\n"
           "<b>SETUP #1 - [LONG/SHORT] [SCALP/INTRADAY/SWING] ([TFs])</b>\n"
           "- <b>Narrativa:</b> [sweep em X criou swing Y-Z, OTE em W, entrada no retrace]\n"
           "- <b>Fase PO3:</b> [em que fase AMD estamos e se e seguro entrar agora]\n"
           "- <b>Nivel OTE:</b> [61.8% / 70.5% / 79%] = [valor exato do fibonacci]\n"
           "- <b>IFVG Confluente:</b> [zona de IFVG que coincide com entrada - se existir]\n"
           "- <b>Entrada Agressiva:</b> [valor exato] - limit order no OTE\n"
           "- <b>Entrada Conservadora:</b> [valor exato] - apos fechamento M15 confirmado no OTE\n"
           "- <b>Trigger Obrigatorio:</b> [Engolfo/Pin Bar/Inside Bar] no [TF] com fechamento [acima/abaixo] de [nivel]\n"
           "- <b>Volume:</b> vela de confirmacao deve ter volume acima de [valor MA20]\n"
           "- <b>Stop Loss:</b> [valor] ([referencia ICT exata])\n"
           "- <b>Invalidacao Pre-Entrada:</b> fechar acima/abaixo de [valor] cancela o setup\n"
           "- <b>Take Profit 1:</b> [valor] ([referencia])\n"
           "- <b>Take Profit 2:</b> [valor] ([referencia])\n"
           "- <b>Take Profit 3:</b> [valor] ([referencia])\n"
           "- <b>Razao R/R:</b> [ex: 1:2.5]\n"
           "- <b>Confluencias Ativas:</b> [listar: Sweep + CHoCH + OTE + OB + FVG + IFVG + PO3 + Killzone etc]\n"
           "- <b>Probabilidade:</b> [X]% ([N] confluencias identificadas)\n"
           "- <b>Gestao Pos-Entrada:</b> [o que esperar depois de entrar - proximos niveis, re-sweeps possiveis]\n"
           "- <b>Proximos Alvos de Liquidez:</b> [onde o mercado vai apos TP1, TP2, TP3]\n"
           "- <b>Cenario Alternativo:</b> [o que acontece se invalidar - re-sweep, continuacao, novo setup]\n"
           "- <b>Tipo:</b> [Scalp/Intraday/Swing + descricao]\n\n"
           "<b>SETUP #2 - [LONG/SHORT] [SCALP/INTRADAY/SWING] ([TFs])</b>\n"
           "- <b>Narrativa:</b> [sweep e swing que origina este setup]\n"
           "- <b>Fase PO3:</b> [fase AMD e seguranca de entrada]\n"
           "- <b>Nivel OTE:</b> [61.8% / 70.5% / 79%] = [valor]\n"
           "- <b>IFVG Confluente:</b> [zona se existir]\n"
           "- <b>Entrada Agressiva:</b> [valor]\n"
           "- <b>Entrada Conservadora:</b> [valor]\n"
           "- <b>Trigger Obrigatorio:</b> [padrao de vela]\n"
           "- <b>Volume:</b> [referencia]\n"
           "- <b>Stop Loss:</b> [valor] ([referencia ICT])\n"
           "- <b>Invalidacao Pre-Entrada:</b> [nivel]\n"
           "- <b>Take Profit 1:</b> [valor]\n"
           "- <b>Take Profit 2:</b> [valor]\n"
           "- <b>Take Profit 3:</b> [valor]\n"
           "- <b>Razao R/R:</b> [valor]\n"
           "- <b>Confluencias Ativas:</b> [listar]\n"
           "- <b>Probabilidade:</b> [X]%\n"
           "- <b>Gestao Pos-Entrada:</b> [proximos niveis e re-sweeps]\n"
           "- <b>Proximos Alvos de Liquidez:</b> [onde vai apos cada TP]\n"
           "- <b>Cenario Alternativo:</b> [se invalidar]\n"
           "- <b>Tipo:</b> [descricao]\n\n"
           "---\n\n"
           "<b>SCORE OPERACIONAL: [X]/100</b>\n"
           "- Confluencia Multi-TF: [X]/100\n"
           "- Suporte Estrutural: [X]/100\n"
           "- Momentum: [X]/100\n"
           "- Risk/Reward: [X]/100\n"
           "- Liquidez: [X]/100\n"
           "- Timing (Killzone): [X]/100\n\n"
           "---\n\n"
           "<b>AVISOS CRITICOS</b>\n"
           "- [aviso 1]\n"
           "- [aviso 2]\n\n"
           "---\n\n"
           "<b>RECOMENDACAO FINAL</b>\n"
           "[narrativa completa: sweep identificado, fase PO3 atual, CHoCH confirmado, "
           "OTE calculado, IFVG confluente se existir, entrada exata, gestao da posicao e o que esperar depois]\n\n"
           "REGRAS CRITICAS:\n"
           "- Stop Loss SEMPRE com referencia ICT explicada\n"
           "- NUNCA uses markdown (* # [ ])\n"
           "- Usa APENAS tags HTML: <b> <i> <u>\n"
           "- D1 bearish = aviso critico obrigatorio em qualquer setup long\n"
           "- Minimo 3 confluencias para recomendar entrada\n"
           "- Minimo 2:1 RR para recomendar entrada\n"
           "- Entrada conservadora E SEMPRE a preferida\n"
           "- Entrada DEVE referenciar nivel OTE correspondente (61.8%, 70.5% ou 79%)\n"
           "- Se entrada nao coincide com OTE explicar porque e qual alternativa OTE existe\n"
           "- Narrativa ICT obrigatoria: Sweep -> CHoCH -> OTE -> IFVG -> PO3 -> Entrada -> Gestao\n"
           "- Se PO3 Manipulation ainda em curso = AVISO CRITICO para NAO entrar ainda\n"
           "- IFVG confluente com OTE = confluencia maxima = prioridade de entrada\n"
           "- Fecha todas as tags HTML abertas\n"
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
       content.append({"type": "text", "text": prompt})

       response = client.messages.create(
           model="claude-sonnet-4-6",
           max_tokens=4000,
           messages=[{"role": "user", "content": content}]
       )
       result_text = response.content[0].text
       return jsonify({'result': result_text, 'timeframes': ','.join(valid_tfs)})
   except Exception as e:
       print(f"Erro na API: {str(e)}")
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

if __name__ == '__main__':
   port = int(os.environ.get('PORT', 5000))
   app.run(host='0.0.0.0', port=port, debug=False)
