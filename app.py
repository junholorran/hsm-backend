from flask import Flask, request, jsonify, send_from_directory
import anthropic
import os
import threading
import time
import requests

app = Flask(__name__)
client = anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY'))

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')

active_alerts = []
alerts_lock = threading.Lock()

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

def get_binance_price(pair):
    try:
        symbol = SYMBOL_MAP.get(pair, pair.replace('USD', 'USDT'))
        url = f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}"
        resp = requests.get(url, timeout=5)
        data = resp.json()
        return float(data['price'])
    except Exception as e:
        print(f"Binance erro para {pair}: {e}")
        return None

def send_telegram(message):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"})
    except Exception as e:
        print(f"Telegram erro: {e}")

def price_monitor():
    while True:
        try:
            with alerts_lock:
                remaining = []
                for alert in active_alerts:
                    price = get_binance_price(alert['pair'])
                    if price is None:
                        print(f"Sem preco para {alert['pair']}, mantendo alerta...")
                        remaining.append(alert)
                        continue
                    print(f"Monitor: {alert['pair']} @ {price} | alvo: {alert['target']} | dir: {alert['direction']}")
                    triggered = False
                    if alert['direction'] == 'above' and price >= alert['target']:
                        triggered = True
                    elif alert['direction'] == 'below' and price <= alert['target']:
                        triggered = True
                    if triggered:
                        msg = f"🚨 <b>ALERTA {alert['pair']} ATIVADO!</b>\n"
                        msg += f"💰 Preco atual: ${price:,.2f}\n"
                        msg += f"🎯 Preco alvo: ${alert['target']:,.2f}\n\n"
                        msg += f"📋 <b>ANALISE COMPLETA:</b>\n"
                        msg += alert['analysis']
                        send_telegram(msg)
                        print(f"Alerta disparado: {alert['pair']} @ {price}")
                    else:
                        remaining.append(alert)
                active_alerts.clear()
                active_alerts.extend(remaining)
        except Exception as e:
            print(f"Monitor erro: {e}")
        time.sleep(30)

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
        content = [{"type":"text","text":"Es um mentor ICT/SMC. Analisa os graficos do " + pair + " (" + ', '.join(tf_list) + ") em portugues. Da bias, liquidez, FVGs, OBs, CHoCH, setup com entrada/SL/TP e score 0-100."}]
        for tf in tf_list:
            img = images[tf]
            content.append({"type":"text","text":"Grafico " + tf + ":"})
            content.append({"type":"image","source":{"type":"base64","media_type":img['mimeType'],"data":img['base64']}})
        response = client.messages.create(model="claude-haiku-4-5", max_tokens=3000, messages=[{"role":"user","content":content}])
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
        current_price = float(data.get('current_price'))
        analysis = data.get('analysis', '')
        direction = 'above' if target > current_price else 'below'
        with alerts_lock:
            active_alerts.append({
                'pair': pair,
                'target': target,
                'direction': direction,
                'analysis': analysis
            })
        send_telegram(f"🎯 <b>Alerta criado para {pair}</b>\nPreco alvo: ${target:,.2f}\nDirecao: {'Acima' if direction == 'above' else 'Abaixo'}\nPreco atual: ${current_price:,.2f}")
        return jsonify({'ok': True, 'direction': direction, 'current_price': current_price})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/update_prices', methods=['POST'])
def update_prices():
    return jsonify({'ok': True})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
