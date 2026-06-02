from flask import Flask, request, jsonify, send_from_directory
import anthropic
import os

app = Flask(__name__)
client = anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY'))

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
        content = [{"type":"text","text":f"És um mentor ICT/SMC. Analisa os gráficos do {pair} ({', '.join(tf_list)}) em português. Dá bias, liquidez, FVGs, OBs, CHoCH, setup com entrada/SL/TP e score 0-100."}]
        for tf in tf_list:
            img = images[tf]
            content.append({"type":"text","text":f"Gráfico {tf}:"})
            content.append({"type":"image","source":{"type":"base64","media_type":img['mimeType'],"data":img['base64']}})
        response = client.messages.create(model="claude-haiku-4-5", max_tokens=3000, messages=[{"role":"user","content":content}])
        return jsonify({'result': response.content[0].text})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)