from flask import Flask, request, jsonify, send_from_directory
import anthropic
import os
import re
import threading
import time
import requests

app = Flask(__name__)
client = anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY'))

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')

# Alertas ativos em memória
active_alerts = []
alerts_lock = threading.Lock()

def send_telegram(message):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        })
    except Exception as e:
        print(f"Telegram erro: {e}")

def get_price(pair):
    try:
        symbol = pair.replace("USD", "USDT")
        url = f"https://api.bybit.com/v5/market/tickers?category=spot&symbol={symbol}"
        r = requests.get(url, timeout=5)
        data = r.json()
        return float(data['result']['list'][0]['lastPrice'])
    except:
        return None

def extract_alert_from_analysis(text, pair):
    alerts = []
    
    # Padrões comuns do HSM
    patterns = [
        r'[Aa]guarda(?:r)? (?:rompimento |confirmação )?(?:acima de |abaixo de )?\$?([\d,\.]+)k?',
        r'[Ee]ntrada[:\s]+\$?([\d,\.]+)k?',
        r'[Ee]ntry[:\s]+\$?([\d,\.]+)k?',
        r'[Zz]ona de entrada[:\s]+\$?([\d,\.]+)k?',
    ]
    
    for pattern in patterns:
        matches = re.findall(pattern, text)
        for match in matches:
            price_str = match.replace(',', '.')
            if 'k' in price_str:
                price = float(price_str.replace('k', '')) * 1000
            else:
                price = float(price_str)
            if price > 100:  # filtro básico
                alerts.append(price)
                break
        if alerts:
            break
    
    return alerts[0] if alerts else None

def price_monitor():
    while True:
        try:
            with alerts_lock:
                remaining = []
                for alert in active_alerts:
                    price = get_price(alert['pair'])
                    if price is None:
                        remaining.append(alert)
                        continue
                    
                    triggered = False
                    if alert['direction'] == 'above' and price >= alert['target']:
                        triggered = True
                    elif alert['direction'] == 'below' and price <= alert['target']:
                        triggered = True
                    
                    if triggered:
                        msg = f"🚨 <b>ALERTA {alert['pair']} ATIVADO</b>\n"
                        msg += f"💰 Preço atual: ${price:,.2f}\n\n"
                        msg += f"📋 <b>O QUE FAZER AGORA:</b>\n"
                        msg += alert['instructions']
                        send_telegram(msg)
                        print(f"Alerta disparado: {alert['pair']} @ {price}")
                    else:
                        remaining.append(alert)
                
                active_alerts.clear()
                active_alerts.extend(remaining)
        except Exception as e:
            print(f"Monitor erro: {e}")
        
        time.sleep(30)

# Inicia monitor em background
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
        
        # Tenta extrair alerta automaticamente
        alert_price = extract_alert_from_analysis(result_text, pair)
        alert_created = False
        
        if alert_price:
            current_price = get_price(pair)
            if current_price:
                direction = 'above' if alert_price > current_price else 'below'
                
                # Monta instruções para o Telegram
                instructions = f"Par: {pair}\n"
                instructions += f"Preço alvo: ${alert_price:,.2f}\n\n"
                instructions += "📊 Verifica o gráfico agora!\n"
                instructions += "✅ Confirma CHoCH\n"
                instructions += "✅ Aguarda candle de confirmação\n"
                instructions += "✅ Entra na zona de entrada\n"
                
                with alerts_lock:
                    active_alerts.append({
                        'pair': pair,
                        'target': alert_price,
                        'direction': direction,
                        'instructions': instructions
                    })
                
                alert_created = True
                send_telegram(f"🎯 <b>Alerta criado para {pair}</b>\nPreço alvo: ${alert_price:,.2f}\nDireção: {'Acima' if direction == 'above' else 'Abaixo'}\nPreço atual: ${current_price:,.2f}")
        
        return jsonify({
            'result': result_text,
            'alert_created': alert_created,
            'alert_price': alert_price
        })
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)