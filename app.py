from flask import Flask, request, jsonify, send_from_directory
import anthropic
import os
import threading
import time
import requests
import sqlite3
import socket

app = Flask(__name__)
client = anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY'))

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')
DB_FILE = 'alerts.db'

# ARQUITETURA ANTI-451: Browser envia precos, servidor compara
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
                analysis TEXT
            )
        ''')
        conn.commit()
        conn.close()
        print("Base de dados SQLite inicializada!")
    except Exception as e:
        print(f"Erro ao inicializar Base de Dados: {e}")

def send_telegram(message):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}, timeout=10)
    except Exception as e:
        print(f"Telegram erro: {e}")

def price_monitor():
    print("Monitor de Precos Ativo! A ler dados enviados pelo Browser.")
    while True:
        try:
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            cursor.execute("SELECT id, pair, target, analysis FROM alerts")
            db_alerts = cursor.fetchall()
            conn.close()

            if not db_alerts:
                time.sleep(2.0)
                continue

            for alert_id, pair, target, analysis in db_alerts:
                current_price = PRECOS_TICKER.get(pair)
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
                        msg = f"<b>ALERTA DISPARADO: {pair} ATINGIDO!</b>\n"
                        msg += f"Preco no toque: ${current_price:,.2f}\n"
                        msg += f"Teu alvo era: ${target:,.2f}\n\n"
                        msg += f"<b>ANALISE DO MENTOR ICT:</b>\n"
                        msg += analysis
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
            f"Es um mentor institucional especializado na metodologia ICT (Inner Circle Trader) e SMC.\n"
            f"Analisa rigorosamente os graficos de {pair} fornecidos nos timeframes ({', '.join(valid_tfs)}).\n\n"
            "A tua analise deve obrigatoriamente integrar e cruzar as seguintes 12 CAMADAS ICT:\n"
            "1. HTF Narrative & Daily Bias (Tendencia macro e direcao do dia atual)\n"
            "2. Liquidez Pendente (BSL e SSL remanescentes)\n"
            "3. Premium vs Discount Zone (50% de Fibonacci do range atual)\n"
            "4. Institutional Order Blocks (Zonas de ordens institucionais)\n"
            "5. Fair Value Gaps (FVG) / Imbalances / Ineficiencias de Preco\n"
            "6. Market Structure Shift (MSS) / CHoCH nos timeframes menores\n"
            "7. Liquidity Sweeps / Stop Raids (Manipulacao previa)\n"
            "8. Mitigation & Breaker Blocks (Antigos OBs rompidos)\n"
            "9. Killzones & Session Patterns (Asia, London, NY)\n"
            "10. Midnight Open (Referencia de preco NY para BTC/Alts)\n"
            "11. Optimal Trade Entry (OTE - 61.8%, 70.5%, 79% de recuo)\n"
            "12. ICT Score Geral (0 a 100 com base na confluencia)\n\n"
            "REGRAS DE FORMATACAO PARA TELEGRAM:\n"
            "- Usa EXCLUSIVAMENTE tags HTML validas (<b>, <i>, <u>, <code>).\n"
            "- NUNCA uses markdown (*, **, #, -, [ ]).\n"
            "- Para listas usa o caractere bullet unicode (-).\n"
            "- Finaliza com: <b>ENTRADA:</b>, <b>STOP LOSS:</b>, <b>TAKE PROFIT:</b>.\n"
            "- Fecha todas as tags HTML."
        )

        content = [{"type": "text", "text": prompt}]
        for tf in valid_tfs:
            img = images[tf]
            b64_data = img['base64']
            if "," in b64_data:
                b64_data = b64_data.split(",")[-1]
            mime = img.get('mimeType', 'image/jpeg') or 'image/jpeg'
            content.append({"type": "text", "text": f"Grafico {tf}:"})
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": mime, "data": b64_data}
            })

        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=3000,
            messages=[{"role": "user", "content": content}]
        )
        return jsonify({'result': response.content[0].text})
    except Exception as e:
        return jsonify({'error': f"Erro na API Anthropic: {str(e)}"}), 500

@app.route('/set_alert', methods=['POST'])
def set_alert():
    try:
        data = request.json
        pair = data.get('pair', 'BTCUSD')
        target = float(data.get('target'))
        analysis = data.get('analysis', '')

        current_price = PRECOS_TICKER.get(pair)
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

        send_telegram(f"<b>Alerta Gravado para {pair}</b>\nAlvo: ${target:,.2f}\nPreco atual: ${current_price:,.2f}")
        return jsonify({'ok': True, 'current_price': current_price})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
