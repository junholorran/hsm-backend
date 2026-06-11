from flask import Flask, request, jsonify, send_from_directory
import anthropic
import os
import threading
import time
import requests

app = Flask(__name__)

# Corrigido para o modelo oficial Claude 3.5 Sonnet (Rei da visão computacional para SMC)
client = anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY'))

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')

active_alerts = []
alerts_lock = threading.Lock()
latest_prices = {}
# Movido para fora para persistência global de estados de cruzamento
last_known_position = {} 

def send_telegram(message):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}, timeout=10)
    except Exception as e:
        print(f"Telegram erro: {e}")

def price_monitor():
    while True:
        triggered_alerts = []
        try:
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
                        alert_copy = alert.copy()
                        alert_copy['triggered_price'] = price
                        triggered_alerts.append(alert_copy)
                        print(f"Cruzamento detetado! {alert['pair']} cruzou {alert['target']} @ {price}")
            for alert in triggered_alerts:
                msg = f"🔄 <b>ALERTA DE CRUZAMENTO CONTÍNUO: {alert['pair']}</b>\n"
                msg += f"💰 Preço atual: ${alert['triggered_price']:,.2f}\n"
                msg += f"🎯 Alvo cruzado: ${alert['target']:,.2f}\n"
                msg += f"📊 Movimento: {'Cruzou para CIMA ↑' if alert['direction'] == 'above' else 'Cruzou para BAIXO ↓'}\n\n"
                msg += f"📋 <b>ANÁLISE DO MENTOR ICT:</b>\n"
                msg += alert['analysis']
                send_telegram(msg)
        except Exception as e:
            print(f"Monitor erro: {e}")
        time.sleep(5)

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
            
        content = [{
            "type": "text",
            "text": f"És um mentor ICT/SMC profissional. Analisa os gráficos do {pair} ({', '.join(tf_list)}) em português de Portugal. Dá o bias de mercado, liquidez (BSL/SSL), FVGs, Order Blocks, quebras de estrutura (CHoCH/MSS), sugere um setup exato com entrada/SL/TP e atribui um Score de confiança de 0-100."
        }]
        
        for tf in tf_list:
            img = images[tf]
            content.append({"type": "text", "text": f"Grafico {tf}:"})
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": img['mimeType'],
                    "data": img['base64']
                }
            })
            
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
        send_telegram(f"🎯 <b>Alerta Infinito criado para {pair}</b>\nPreço alvo: ${target:,.2f}\nDireção: {'Acima' if direction == 'above' else 'Abaixo'}\nPreço atual: ${current_price:,.2f}")
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
