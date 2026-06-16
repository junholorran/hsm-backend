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

# --- REGEX BLINDADAS CONTRA HTML E ESPAÇOS ---
RE_SCORE = re.compile(r'SCORE\s*OPERACIONAL\s*:[^\d]*(\d{1,3})\s*/\s*100', re.IGNORECASE)
RE_SL = re.compile(r'Stop\s*Loss\s*[^:]*:[^\d]*\$?\s*([\d,.]+)', re.IGNORECASE)
RE_TP = re.compile(r'Take\s*Profit\s*\d?\s*[^:]*:[^\d]*\$?\s*([\d,.]+)', re.IGNORECASE)
RE_ENTRY = re.compile(r'Entrada\s*Conservadora\s*[^:]*:[^\d]*\$?\s*([\d,.]+)', re.IGNORECASE)
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

@app.route('/btc_data', methods=['GET'])
def btc_data():
   try:
       # Usar endpoint simples de preco da Binance
       r_price = requests.get('https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT', timeout=10)
       r_24h = requests.get('https://api.binance.com/api/v3/ticker/24hr?symbol=BTCUSDT', timeout=10)
       dp = r_price.json()
       d = r_24h.json()
       data = {
           'price': float(dp['price']),
           'change': float(d['priceChangePercent']),
           'high': float(d['highPrice']),
           'low': float(d['lowPrice'])
       }
       print(f"BTC Data: {data}")
       return jsonify(data)
   except Exception as e:
       print(f"btc_data erro: {str(e)}")
       return jsonify({'error': str(e)}), 500

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
           "9. Killzones & Session Patterns (Asia/London/NY - qual esta activa)\n"
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
           "- Entrada SEMPRE em OB ou FVG identificado — NUNCA fora dessas zonas\n"
           "- Stop Loss SEMPRE acima do OB/FVG seguinte ou swing high/low relevante — NUNCA em zona obvia de liquidez\n"
           "- Identificar BSL/SSL proximos que podem varrer o stop antes da movimentacao\n"
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
           "- <b>D1:</b> [BULLISH/BEARISH] - [justificativa detalhada]\n"
           "- <b>H4:</b> [BULLISH/BEARISH] - [justificativa]\n"
           "- <b>H1:</b> [bias] - [justificativa]\n"
           "- <b>M15/M5:</b> [bias] - [justificativa]\n\n"
           "---\n\n"
           "<b>LIQUIDEZ & ESTRUTURA</b>\n"
           "- <b>BSL (Buy Side Liquidity):</b> [valor] - [contexto - stops dos shorts aqui]\n"
           "- <b>SSL (Sell Side Liquidity):</b> [valor] - [contexto - stops dos longs aqui]\n"
           "- <b>AVISO DE LIQUIDEZ:</b> [BSL/SSL que pode varrer o stop antes da movimentacao]\n"
           "- <b>OB Bearish:</b> [zona exata]\n"
           "- <b>OB Bullish:</b> [zona exata]\n"
           "- <b>FVG Aberto:</b> [zona e status]\n"
           "- <b>IFVG:</b> [zona e status]\n"
           "- <b>CHoCH:</b> [status e nivel exato]\n\n"
           "---\n\n"
           "<b>PARAMETROS DE EXECUCAO</b>\n"
           "- Estilo de Operacao: [Scalp/Intraday/Swing]\n"
           "- Entrada Conservadora: [valor exato - em OB ou FVG]\n"
           "- Stop Loss: [valor exato - acima de OB/FVG/swing relevante, longe de BSL/SSL obvios]\n"
           "- Take Profit 1: [valor exato]\n"
           "- Take Profit 2: [valor exato]\n"
           "- Take Profit 3: [valor exato]\n\n"
           "---\n\n"
           "<b>SCORE OPERACIONAL: [X]/100</b>\n\n"
           "---\n\n"
           "<b>RECOMENDACAO FINAL</b>\n"
           "[narrativa completa]\n\n"
           "REGRAS CRITICAS:\n"
           "- Entrada SEMPRE em OB ou FVG — se nao houver confluencia, NAO entrar\n"
           "- Stop SEMPRE atras de estrutura ICT real, NUNCA em zona obvia de liquidez\n"
           "- Se BSL/SSL esta proximo do stop, AVISAR e alargar o stop ou NAO entrar\n"
           "- D1 bearish = aviso critico obrigatorio em qualquer setup long\n"
           "- Minimo 3 confluencias para recomendar entrada\n"
           "- NUNCA uses markdown (* # [ ])\n"
           "- Usa APENAS tags HTML: <b> <i> <u>\n"
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
           max_tokens=16000,
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
