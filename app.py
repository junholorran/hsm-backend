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

def init_db():
   try:
       conn = sqlite3.connect(DB_FILE)
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
       except:
           pass
       conn.commit()
       conn.close()
       print("Base de dados SQLite inicializada!")
   except Exception as e:
       print(f"Erro ao inicializar Base de Dados: {e}")

def send_telegram(message):
   try:
       url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
       resp = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}, timeout=10)
       print(f"Telegram enviado: {resp.status_code}")
   except Exception as e:
       print(f"Telegram erro: {e}")

def extract_trade_info(analysis, timeframes_str):
   direction = "LONG"
   tl = analysis.lower()
   sell_count = len(re.findall(r'short|bearish|sell|vend[ae]', tl))
   buy_count = len(re.findall(r'long|bullish|buy|compra', tl))
   if sell_count > buy_count:
       direction = "SHORT"

   score = 50
   sm = re.search(r'(\d{1,3})\s*/\s*100', analysis)
   if sm:
       score = int(sm.group(1))

   sl = ""
   sl_match = re.search(
       r'Stop Loss:\s*\$?([\d]+[.,][\d]+)',
       analysis, re.IGNORECASE
   )
   if sl_match:
       sl = sl_match.group(1).replace(',', '.')

   tps = []
   tp_matches = re.findall(
       r'Take Profit\s*\d:\s*\$?([\d]+[.,][\d]+)',
       analysis, re.IGNORECASE
   )
   for tp in tp_matches[:3]:
       tps.append(tp.replace(',', '.'))

   tfs = timeframes_str.upper() if timeframes_str else ""

   tf_label = ""
   if re.search(r'scalp', analysis, re.IGNORECASE):
       tf_label = "SCALP"
   elif re.search(r'swing', analysis, re.IGNORECASE):
       tf_label = "SWING"
   elif re.search(r'intraday', analysis, re.IGNORECASE):
       tf_label = "INTRADAY"

   if "D1" in tfs:
       tf_label += " D1"
   elif "H4" in tfs:
       tf_label += " H4"
   elif "H1" in tfs:
       tf_label += " H1"
   elif "M15" in tfs:
       tf_label += " M15"
   elif "M5" in tfs:
       tf_label += " M5"
   elif "M1" in tfs:
       tf_label += " M1"

   return direction, score, sl, tps, tf_label

def price_monitor():
   print("Monitor de Precos Ativo! A ler dados enviados pelo Browser.")
   while True:
       try:
           conn = sqlite3.connect(DB_FILE)
           cursor = conn.cursor()
           cursor.execute("SELECT id, pair, target, analysis, timeframes FROM alerts")
           db_alerts = cursor.fetchall()
           conn.close()

           if not db_alerts:
               time.sleep(2.0)
               continue

           for row in db_alerts:
               alert_id = row[0]
               pair = row[1]
               target = row[2]
               analysis = row[3]
               timeframes_str = row[4] if len(row) > 4 else ""

               current_price = PRECOS_TICKER.get(pair)
               if current_price is None:
                   continue

               distancia = abs(current_price - target)
               margem_tolerancia = target * 0.0015

               if distancia <= margem_tolerancia:
                   conn = sqlite3.connect(DB_FILE)
                   cursor = conn.cursor()
                   cursor.execute("DELETE FROM alerts WHERE id = ?", (alert_id,))
                   linhas_afetadas = cursor.rowcount
                   conn.commit()
                   conn.close()

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
           "ANALISE OBRIGATORIA - 13 CAMADAS ICT:\n"
           "1. HTF Narrative & Daily Bias (D1/W1 - tendencia macro)\n"
           "2. Liquidez Pendente (BSL e SSL identificados com valores exatos)\n"
           "3. Premium vs Discount Zone (Fibonacci 50% do range - onde esta o preco agora)\n"
           "4. Order Blocks (OB bullish e bearish com zonas exatas por TF)\n"
           "5. Fair Value Gaps (FVG com zonas exatas e status: preenchido/aberto)\n"
           "6. CHoCH / MSS (confirmado ou potencial, com nivel exato)\n"
           "7. Liquidity Sweeps (varreduras recentes com valores)\n"
           "8. Mitigation & Breaker Blocks\n"
           "9. Killzones & Session Patterns (Asia/London/NY - qual esta ativa)\n"
           "10. Midnight Open (valor exato e posicao do preco em relacao a ele)\n"
           "11. OTE - Optimal Trade Entry (calcular 61.8%, 70.5%, 79% do swing relevante)\n"
           "12. Score ICT (0-100 baseado em confluencias)\n"
           "13. Wyckoff Phase (identificar fase atual: Acumulacao/Markup/Distribuicao/Markdown + Spring/UTAD se visivel)\n\n"
           "FORMATO OBRIGATORIO DA RESPOSTA:\n\n"
           "<b>ANALISE MULTI-TIMEFRAME " + pair + "</b>\n\n"
           "<b>BIAS DE MERCADO</b>\n"
           "- D1: [BULLISH/BEARISH] - [justificativa]\n"
           "- H4: [BULLISH/BEARISH] - [justificativa]\n"
           "- H1: [bias] - [justificativa]\n"
           "- M15/M5: [bias] - [justificativa]\n\n"
           "<b>LIQUIDEZ & ESTRUTURA</b>\n"
           "- Zona de Liquidez Alta: [valor] - [contexto]\n"
           "- Suportes Criticos: [valores]\n"
           "- OB Relevante: [zona]\n"
           "- FVG Aberto: [zona]\n"
           "- CHoCH: [status e nivel]\n\n"
           "<b>WYCKOFF PHASE</b>\n"
           "- Fase Atual: [Acumulacao/Markup/Distribuicao/Markdown]\n"
           "- Evento Wyckoff: [Spring/UTAD/LPS/SOW se identificavel]\n"
           "- Confluencia ICT: [como alinha com o bias e liquidez atual]\n\n"
           "<b>KILLZONES & MIDNIGHT OPEN</b>\n"
           "- Sessao Ativa: [Asia/London/NY]\n"
           "- Midnight Open: [valor]\n"
           "- Posicao do Preco: [premium/discount/no nivel]\n\n"
           "<b>OTE - OPTIMAL TRADE ENTRY</b>\n"
           "- Swing relevante: de [low] para [high]\n"
           "- OTE 61.8%: [valor]\n"
           "- OTE 70.5%: [valor]\n"
           "- Zona de entrada ideal: [range]\n\n"
           "<b>SETUPS IDENTIFICADOS</b>\n\n"
           "<b>SETUP #1 - [LONG/SHORT] [SCALP/INTRADAY/SWING] ([TFs])</b>\n"
           "- Entrada: [valor] ([contexto ICT])\n"
           "- Stop Loss: [valor] ([referencia ICT])\n"
           "- Take Profit 1: [valor] ([referencia])\n"
           "- Take Profit 2: [valor] ([referencia])\n"
           "- Take Profit 3: [valor] ([referencia])\n"
           "- Razao R/R: [ex: 1:2.5]\n"
           "- Tipo: [Scalp/Intraday/Swing + descricao]\n\n"
           "<b>SETUP #2 - [LONG/SHORT] [SCALP/INTRADAY/SWING] ([TFs])</b>\n"
           "- Entrada: [valor]\n"
           "- Stop Loss: [valor] ([referencia ICT])\n"
           "- Take Profit 1: [valor]\n"
           "- Take Profit 2: [valor]\n"
           "- Take Profit 3: [valor]\n"
           "- Razao R/R: [valor]\n"
           "- Tipo: [descricao]\n\n"
           "<b>SCORE OPERACIONAL: [X]/100</b>\n"
           "- Confluencia Multi-TF: [X]/100\n"
           "- Suporte Estrutural: [X]/100\n"
           "- Momentum: [X]/100\n"
           "- Risk/Reward: [X]/100\n"
           "- Liquidez: [X]/100\n"
           "- Timing (Killzone): [X]/100\n\n"
           "<b>AVISOS CRITICOS</b>\n"
           "- [aviso 1]\n"
           "- [aviso 2]\n\n"
           "<b>RECOMENDACAO FINAL</b>\n"
           "[descricao do melhor setup e condicoes de entrada]\n\n"
           "REGRAS CRITICAS:\n"
           "- Stop Loss SEMPRE com referencia ICT explicada\n"
           "- NUNCA uses markdown (* # [ ])\n"
           "- Usa APENAS tags HTML: <b> <i> <u>\n"
           "- D1 bearish = aviso critico obrigatorio em qualquer setup long\n"
           "- Minimo 2:1 RR para recomendar entrada\n"
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
       data = request.json
       pair = data.get('pair', 'BTCUSD')
       target = float(data.get('target'))
       analysis = data.get('analysis', '')
       timeframes = data.get('timeframes', '')

       current_price = PRECOS_TICKER.get(pair)
       if not current_price:
           current_price = float(data.get('current_price', 0))

       alert_unique_id = f"{pair}_{target}_{int(time.time() * 1000)}"

       conn = sqlite3.connect(DB_FILE)
       cursor = conn.cursor()
       cursor.execute(
           "INSERT INTO alerts (id, pair, target, analysis, timeframes) VALUES (?, ?, ?, ?, ?)",
           (alert_unique_id, pair, target, analysis, timeframes)
       )
       conn.commit()
       conn.close()

       send_telegram(f"<b>Alerta Gravado para {pair}</b>\nAlvo: ${target:,.2f}\nPreco atual: ${current_price:,.2f}")
       return jsonify({'ok': True, 'current_price': current_price})
   except Exception as e:
       return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
   port = int(os.environ.get('PORT', 5000))
   app.run(host='0.0.0.0', port=port, debug=False)
