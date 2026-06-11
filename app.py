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
latest_prices = {}
last_known_position = {}

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

def get_bybit_price(pair):
    try:
        symbol = SYMBOL_MAP.get(pair, pair.replace('USD', 'USDT'))
        url = f"https://api.bybit.com/v5/market/tickers?category=linear&symbol={symbol}"
        resp = requests.get(url, timeout=5)
        data = resp.json()
        if data.get('result') and data['result'].get('list') and len(data['result']['list']) > 0:
            price = float(data['result']['list'][0]['lastPrice'])
            latest_prices[pair] = price
            return price
        return None
    except Exception as e:
        print(f"Bybit erro para {pair}: {e}")
        return None

def send_telegram(message):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}, timeout=10)
    except Exception as e:
        print(f"Telegram erro: {e}")

def price_monitor():
    already_notified = {}
    while True:
        triggered_alerts = []
        try:
            with alerts_lock:
                unique_pairs = list(set(alert['pair'] for alert in active_alerts))
            for pair in unique_pairs:
                get_bybit_price(pair)
                time.sleep(0.05)
            with alerts_lock:
                for alert in active_alerts:
                    price = latest_prices.get(alert['pair'])
                    if price is None:
                        continue
                    alert_id = alert['id']
                    is_above_now = price >= alert['target']
                    if alert_id not in last_known_position:
                        last_known_position[alert_id] = is_above_now
                        continue
                    was_above = last_known_position[alert_id]
                    triggered = False
                    if alert['direction'] == 'above' and not was_above and is_above_now:
                        triggered = True
                    elif alert['direction'] == 'below' and was_above and not is_above_now:
                        triggered = True
                    last_known_position[alert_id] = is_above_now
                    if triggered:
                        if already_notified.get(alert_id) != is_above_now:
                            alert_copy = alert.copy()
                            alert_copy['triggered_price'] = price
                            triggered_alerts.append(alert_copy)
                            already_notified[alert_id] = is_above_now
                            print(f"Cruzamento! {alert['pair']} @ {price}")
            for alert in triggered_alerts:
                analysis_clean = alert['analysis']
                analysis_clean = analysis_clean.replace('**', '<b>', 1)
                while '**' in analysis_clean:
                    analysis_clean = analysis_clean.replace('**', '<b>', 1).replace('**', '</b>', 1)
                analysis_clean = analysis_clean.replace('### ', '• <b>').replace('## ', '<b>')
                msg = f"🔄 <b>ALERTA DE CRUZAMENTO CONTINUO: {alert['pair']}</b>\n"
                msg += f"💰 Preco atual: ${alert['triggered_price']:,.2f}\n"
                msg += f"🎯 Alvo cruzado: ${alert['target']:,.2f}\n"
                msg += f"📊 Movimento: {'Cruzou para CIMA' if alert['direction'] == 'above' else 'Cruzou para BAIXO'}\n\n"
                msg += f"📋 <b>ANALISE DO MENTOR ICT:</b>\n"
                msg += analysis_clean
                send_telegram(msg)
        except Exception as e:
            print(f"Monitor erro: {e}")
            try:
                send_telegram(f"⚠️ <b>BOT AVISO:</b> Erro no monitor: <code>{str(e)}</code>")
            except:
                pass
        time.sleep(1)

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
        current_price = get_bybit_price(pair)
        if not current_price:
            current_price = float(data.get('current_price'))
        analysis = data.get('analysis', '')
        direction = 'above' if target > current_price else 'below'
        alert_unique_id = f"{pair}_{target}_{direction}_{int(time.time() * 1000)}"
        with alerts_lock:
            active_alerts.append({
                'id': alert_unique_id,
                'pair': pair,
                'target': target,
                'direction': direction,
                'analysis': analysis
            })
        send_telegram(f"🎯 <b>Alerta Infinito criado para {pair}</b>\nPreco alvo: ${target:,.2f}\nDirecao: {'Acima' if direction == 'above' else 'Abaixo'}\nPreco atual: ${current_price:,.2f}")
        return jsonify({'ok': True, 'direction': direction, 'current_price': current_price})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/update_prices', methods=['POST'])
def update_prices():
    try:
        data = request.json
        prices = data.get('prices', {})
        latest_prices.update(prices)
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
