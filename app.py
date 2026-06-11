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
        
        # BLINDAGEM: Simula um navegador real para evitar bloqueios IP da Bybit (Cloudflare/Rate Limit)
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        
        resp = requests.get(url, headers=headers, timeout=5)
        
        # Só tenta ler o JSON se o servidor da Bybit responder com status 200 (Sucesso)
        if resp.status_code == 200:
            data = resp.json()
            if data.get('result') and data['result'].get('list') and len(data['result']['list']) > 0:
                price = float(data['result']['list'][0]['lastPrice'])
                latest_prices[pair] = price
                return price
        else:
            print(f"Bybit recusou ligação com status {resp.status_code} para {pair} (Evitou crash de JSON)")
            
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
    # Mantém a memória de notificações fora do loop para não spamar mensagens seguidas
    already_notified = {}
    
    while True:
        triggered_alerts = []
        try:
            # 1. Agrupar os pares com alertas ativos para poupar requisições à API
            with alerts_lock:
                unique_pairs = list(set(alert['pair'] for alert in active_alerts))
            
            for pair in unique_pairs:
                get_bybit_price(pair)
                time.sleep(0.1) # Delay de estabilidade para evitar abusar da API
                
            # 2. Avaliar cruzamentos precisos
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
                    
                    # Deteta o corte milimétrico da linha de preço
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
                            print(f"Cruzamento capturado! {alert['pair']} @ {price}")
            
            # 3. Dispara as mensagens diretas para o Telegram (Fora do Lock)
            for alert_triggered in triggered_alerts:
                analysis_clean = alert_triggered['analysis']
                
                # Fallback de limpeza caso o prompt falhe alguma tag
                analysis_clean = analysis_clean.replace('**', '<b>', 1)
                while '**' in analysis_clean:
                    analysis_clean = analysis_clean.replace('**', '<b>', 1).replace('**', '</b>', 1)
                analysis_clean = analysis_clean.replace('### ', '• <b>').replace('## ', '<b>')
                
                msg = f"🔄 <b>ALERTA DE CRUZAMENTO CONTÍNUO: {alert_triggered['pair']}</b>\n"
                msg += f"💰 Preço atual: ${alert_triggered['triggered_price']:,.2f}\n"
                msg += f"🎯 Alvo cruzado: ${alert_triggered['target']:,.2f}\n"
                msg += f"📊 Movimento: {'Cruzou para CIMA ⬆️' if alert_triggered['direction'] == 'above' else 'Cruzou para BAIXO ⬇️'}\n\n"
                msg += f"📋 <b>ANÁLISE DO MENTOR ICT:</b>\n"
                msg += analysis_clean
                send_telegram(msg)
                
        except Exception as e:
            print(f"Monitor erro: {e}")
            
        # Equilíbrio ideal de frequência (2 segundos impede bloqueios de IP na hospedagem e apanha tudo)
        time.sleep(2) 

# Inicialização e arranque automático do monitor em segundo plano
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
            return jsonify({'error': 'Carrega pelo menos 2 gráficos'}), 400
            
        # CONFIGURAÇÃO DE PROMPT: Entrega HTML nativo e elimina de vez o Markdown cru do Telegram
        prompt_telegram = (
            f"És um mentor ICT/SMC. Analisa os gráficos do {pair} ({', '.join(tf_list)}) em português. "
            "Dá bias, liquidez, FVGs, OBs, CHoCH, setup com entrada/SL/TP e score 0-100.\n\n"
            "REGRAS ESTRITAS DE FORMATAÇÃO PARA O TELEGRAM:\n"
            "1. Formata a resposta EXCLUSIVAMENTE com tags HTML válidas (<b>, <i>, <u>, <code>).\n"
            "2. NUNCA uses cabeçalhos Markdown (#, ##, ###) nem asteriscos (**).\n"
            "3. Para destacar títulos ou nomes de secções, usa apenas texto em negrito (ex: <b>📌 RESUMO EXECUTIVO</b>).\n"
            "4. Para listas de pontos, usa obrigatoriamente o caractere unicode da bola (•) em vez de traços (-) ou asteriscos.\n"
            "5. Certifica-se de fechar todas as tags abertas na mesma linha correspondente."
        )
            
        content = [{"type": "text", "text": prompt_telegram}]
        for tf in tf_list:
            img = images[tf]
            content.append({"type": "text", "text": "Gráfico " + tf + ":"})
            content.append({"type": "image", "source": {"type": "base64", "media_type": img['mimeType'], "data": img['base64']}})
            
        response = client.messages.create(model="claude-haiku-4-5", max_tokens=3000, messages=[{"role": "user", "content": content}])
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
        
        # SINCRO-FIX: Regista o estado inicial no exato milissegundo em que o alerta nasce.
        # Evita falhas ou atrasos se o preço cruzar a linha no mesmo segundo da criação.
        is_above_initial = current_price >= target
        last_known_position[alert_unique_id] = is_above_initial
        
        with alerts_lock:
            active_alerts.append({
                'id': alert_unique_id,
                'pair': pair,
                'target': target,
                'direction': direction,
                'analysis': analysis
            })
            
        send_telegram(f"🎯 <b>Alerta Infinito criado para {pair}</b>\nPreço alvo: ${target:,.2f}\nDireção: {'Acima ⬆️' if direction == 'above' else 'Abaixo ⬇️'}\nPreço atual: ${current_price:,.2f}")
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
