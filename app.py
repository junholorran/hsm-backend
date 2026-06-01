from flask import Flask, request, jsonify
import anthropic
import base64
import os

app = Flask(__name__, static_folder='.', static_url_path='')

client = anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY'))

@app.route('/')
def index():
    return app.send_static_file('index.html')

@app.route('/analyze', methods=['POST'])
def analyze():
    try:
        data = request.json
        pair = data.get('pair', 'BTCUSD')
        images = data.get('images', {})
        tf_list = list(images.keys())

        if len(tf_list) < 2:
            return jsonify({'error': 'Carrega pelo menos 2 gráficos'}), 400

        content = [
            {
                "type": "text",
                "text": f"""És um mentor profissional de trading especializado em ICT (Inner Circle Trader) e SMC (Smart Money Concepts).

Analisa os gráficos do {pair} que te envio e faz uma análise COMPLETA em português de Portugal/Brasil.

ANALISA OBRIGATORIAMENTE:

1. BIAS por timeframe ({', '.join(tf_list)}):
   - Bullish, Bearish ou Neutro
   - Estrutura de mercado (HH/HL ou LH/LL)
   - PDH/PDL relevantes

2. LIQUIDEZ:
   - BSL (Buy Side Liquidity)
   - SSL (Sell Side Liquidity)
   - Equal Highs/Lows

3. SMART MONEY CONCEPTS:
   - FVGs (Fair Value Gaps) com zonas de preço
   - Order Blocks institucionais
   - CHoCH e BOS

4. ZONAS:
   - Premium vs Discount
   - OTE (62-79%)

5. SETUP Swing + Scalp:
   - Direção: BUY ou SELL
   - Entrada, SL e TP com preços
   - Confluências (3-5)

6. SCORE DE CONFLUÊNCIA (0-100):
   - ENTRA ou AGUARDA

7. CONCLUSÃO FINAL:
   - COMPRA / VENDA / AGUARDA
   - Próximo nível de liquidez alvo

Gráficos enviados: {', '.join(tf_list)} do {pair}"""
            }
        ]

        for tf in tf_list:
            img = images[tf]
            content.append({"type": "text", "text": f"\nGráfico {tf}:"})
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": img['mimeType'],
                    "data": img['base64']
                }
            })

        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=3000,
            messages=[{"role": "user", "content": content}]
        )

        return jsonify({'result': response.content[0].text})

    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
