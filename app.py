from flask import Flask, request, jsonify, send_from_directory
import anthropic
import os
import threading
import time
import requests
import sqlite3

app = Flask(__name__)
client = anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY'))

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')
DB_FILE = 'alerts.db'

SYMBOL_MAP = {
    'BTCUSD': 'BTCUSDT',
    'ETHUSD': 'ETHUSDT',
    'SOLUSD': 'SOLUSDT',
    'XRPUSD': 'XRPUSDT',
    'LINKUSD': 'LINKUSDT',
    'ADAUSD': 'ADAUSDT',
    'AVAXUSD': 'AVAXUSDT',
    'BNBUSD': 'BNBUSDT',
    'AAVEUSD': 'AAVEUSDT',
}

def init_db():
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS alerts (
                id TEXT PRIMARY KEY,
                pair TEXT,
                target REAL,
                analysis TEXT
            )
        ''')
        conn.commit()
        conn.close()
        print("Base de dados SQLite inicializada!")
    except Exception as e:
        print(f"Erro ao inicializar Base de Dados: {e}")

def get_binance_price(pair):
    try:
        symbol = SYMBOL_MAP.get(pair, pair.replace('USD', 'USDT'))
        url = f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}"
        resp = requests.get(url, timeout=5)
        if resp.status_code == 200:
            return float(resp.json()['price'])
        else:
            print(f"Binance status {resp.status_code} para {pair}")
    except Exception as e:
        print(f"Binance erro para {pair}: {e}")
    return None

def send_telegram(message):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}, timeout=10)
    except Exception as e:
        print(f"Telegram erro: {e}")

def price_monitor():
    print("Monitor de Precos Ativo!")
    while True:
        try:
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            cursor.execute("SELECT id, pair, target, analysis FROM alerts")
            db_alerts = cursor.fetchall()
            conn.close()

            if not db_alerts:
                time.sleep(3.0)
                continue

            unique_pairs = list(set(row[1] for row in db_alerts))
            latest_prices = {}
            for pair in unique_pairs:
                price = get_binance_price(pair)
                if price is not None:
                    latest_prices[pair] = price
                time.sleep(0.1)

            for alert_id, pair, target, analysis in db_alerts:
                current_price = latest_prices.get(pair)
                if current_price is None:
                    continue

                distancia = abs(current_price - target)
                margem_tolerancia = target * 0.0005

                if distancia <= margem_tolerancia:
                    conn = sqlite3.connect(DB_FILE)
                    cursor = conn.cursor()
                    cursor.execute("DELETE FROM alerts WHERE id = ?", (alert_id,))
                    linhas_afetadas = cursor.rowcount
                    conn.commit()
                    conn.close()

                    if linhas_afetadas > 0:
                        print(f"TOQUE DETETADO! {pair} a ${current_price} (Alvo: {target})")
                        msg = f"🎯 <b>ALERTA DISPARADO: {pair} ATINGIDO!</b>\n"
                        msg += f"💰 Preco no toque: ${current_price:,.2f}\n"
                        msg += f"📍 Teu alvo era: ${target:,.2f}\n\n"
                        msg += f"📋 <b>ANALISE DO MENTOR ICT:</b>\n"
                        msg += analysis
                        send_telegram(msg)

        except Exception as e:
            print(f"Monitor erro: {e}")

        time.sleep(2.5)

# Execucao autonoma para Gunicorn em producao no Railway
init_db()
monitor_thread = threading.Thread(target=price_monitor, daemon=True)
monitor_thread.start()

@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

@app.route('/analyze', methods=['POST'])
def analyze():
    try:
        data = request.json
        pair = data.get('pair', 'BTCUSD')
        images = data.get('images', {})
        tf_list = list(images.keys())
        if len(tf_list) < 2:
            return jsonify({'error': 'Carrega pelo menos 2 graficos'}), 400

        prompt_telegram = (
            f"Es um mentor institucional especializado na metodologia ICT (Inner Circle Trader) e SMC.\n"
            f"Analisa rigorosamente os graficos de {pair} fornecidos nos timeframes ({', '.join(tf_list)}).\n\n"
            "A tua analise deve obrigatoriamente integrar e cruzar as seguintes 12 CAMADAS ICT:\n"
            "1. HTF Narrative & Daily Bias (Tendencia macro e direcao do dia atual)\n"
            "2. Liquidez Pendente (Identificar Buy-side Liquidity [BSL] e Sell-side Liquidity [SSL] remanescentes)\n"
            "3. Premium vs Discount Zone (Preco acima ou abaixo do 50% de Fibonacci do range atual)\n"
            "4. Institutional Order Blocks (Zonas cruciais de abertura de ordens institucionais)\n"
            "5. Fair Value Gaps (FVG) / Imbalances / Ineficiencias de Preco a mitigar\n"
            "6. Market Structure Shift (MSS) / CHoCH nos timeframes menores (M15 a M1)\n"
            "7. Liquidity Sweeps / Stop Raids (Manipulacao previa antes da distribuicao)\n"
            "8. Mitigation & Breaker Blocks (Antigos OBs rompidos que atuam como suporte/resistencia)\n"
            "9. Killzones & Session Patterns (Asia Accumulation, London Manipulation, NY Distribution)\n"
            "10. Midnight Open (Referencia essencial de preco aberto de Nova Iorque para BTC/Alts)\n"
            "11. Optimal Trade Entry (OTE - Niveis 61.8%, 70.5%, 79% de recuo)\n"
            "12. ICT Score Geral (Score de vies de 0 a 100 com base na confluencia destas camadas)\n\n"
            "REGRAS ESTRITAS DE FORMATACAO NATIVA DO TELEGRAM:\n"
            "- Usa EXCLUSIVAMENTE tags HTML validas (<b>, <i>, <u>, <code>).\n"
            "- NUNCA uses markdown (*, **, #, ##, ###, -, [ ]).\n"
            "- Para titulos, usa texto em maiusculas com negrito (ex: <b>1. DIARIO BIAS & NARRATIVA</b>).\n"
            "- Para listas, usa estritamente o caractere da bola unicode (•).\n"
            "- Finaliza com: <b>ENTRADA:</b>, <b>STOP LOSS:</b>, <b>TAKE PROFIT:</b>.\n"
            "- Garante o fecho de cada tag HTML na mesma linha."
        )

        content = [{"type": "text", "text": prompt_telegram}]
        for tf in tf_list:
            img = images[tf]
            content.append({"type": "text", "text": f"Grafico {tf}:"})
            content.append({"type": "image", "source": {"type": "base64", "media_type": img['mimeType'], "data": img['base64']}})

        response = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=3000,
            messages=[{"role": "user", "content": content}]
        )
        result_text = response.content[0].text
        return jsonify({'result': result_text})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/set_alert', methods=['POST'])
def set_alert():
    try:
        data = request.json
        pair = data.get('pair', 'BTCUSD')
        target = float(data.get('target'))
        analysis = data.get('analysis', '')

        current_price = get_binance_price(pair)
        if not current_price:
            current_price = float(data.get('current_price', 0))

        alert_unique_id = f"{pair}_{target}_{int(time.time() * 1000)}"

        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO alerts (id, pair, target, analysis) VALUES (?, ?, ?, ?)",
            (alert_unique_id, pair, target, analysis)
        )
        conn.commit()
        conn.close()

        send_telegram(f"💾 <b>Alerta Gravado para {pair}</b>\nAlvo: ${target:,.2f}\nPreco atual: ${current_price:,.2f}")
        return jsonify({'ok': True, 'current_price': current_price})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/update_prices', methods=['POST'])
def update_prices():
    return jsonify({'ok': True})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
