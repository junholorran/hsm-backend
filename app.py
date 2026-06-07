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
            return jsonify({'error': 'Carrega pelo menos 2 graficos'}), 400

        prompt = (
            "Es um mentor ICT/SMC especialista em price action e Smart Money." +
            " Analisa os graficos de " + pair +
            " nos timeframes " + ', '.join(tf_list) + " em portugues." +
            " Segue este fluxo obrigatorio de analise em 12 camadas:" +

            " CAMADA 1 - BIAS E AMD:" +
            " Identifica o bias direcional dominante (bullish bearish neutro)." +
            " Analisa a fase AMD actual - Accumulation Manipulation Distribution." +
            " Identifica o Power of 3 - qual fase estamos agora." +

            " CAMADA 2 - LIQUIDEZ:" +
            " Mapeia todos os pools de BSL Buy Side Liquidity e SSL Sell Side Liquidity visiveis." +
            " Identifica EQH Equal Highs e EQL Equal Lows como alvos." +
            " Define qual o proximo alvo de liquidez mais provavel." +

            " CAMADA 3 - ESTRUTURA DE MERCADO:" +
            " Analisa CHoCH Change of Character confirmado ou pendente em cada timeframe." +
            " Identifica BOS Break of Structure e MSS Market Structure Shift." +
            " Define a tendencia actual em cada timeframe enviado." +

            " CAMADA 4 - FVGs E IFVGs:" +
            " Identifica todos os Fair Value Gaps abertos e fechados relevantes." +
            " Mapeia Inverse FVGs como zonas de suporte e resistencia." +
            " Define qual FVG tem maior confluencia para entrada." +
            " REGRA DE ENTRADA FVG: so considera FVG formado DEPOIS do sweep de liquidez." +

            " CAMADA 5 - ORDER BLOCKS:" +
            " Identifica OBs bearish e bullish validos e activos." +
            " Mapeia Breaker Blocks e Mitigation Blocks." +
            " Define qual OB tem maior probabilidade de reacao." +
            " REGRA DE ENTRADA OB: so considera OB formado DEPOIS do sweep de liquidez." +

            " CAMADA 6 - OTE FIBONACCI:" +
            " Calcula a zona OTE entre 62 e 79 porcento do retracamento do ultimo swing." +
            " Verifica confluencia do OTE com FVG ou OB identificado." +
            " Define o nivel exacto de entrada ideal dentro da zona OTE." +

            " CAMADA 7 - KILL ZONES E MACROS:" +
            " Identifica a sessao actual Asia Londres Nova York." +
            " Verifica ICT Macros activas 2h20 4h00 e 8h50 9h10 e 9h50 10h10 e 10h50 11h10." +
            " Define o timing ideal para entrada baseado na sessao." +

            " CAMADA 8 - ANALISE PROBABILISTICA:" +
            " Calcula probabilidade de movimento bullish vs bearish em percentagem." +
            " Lista minimo 3 razoes concretas para cada cenario." +
            " Baseia o calculo nas confluencias identificadas nas camadas anteriores." +

            " CAMADA 9 - WYCKOFF E ELLIOTT:" +
            " Identifica a fase Wyckoff actual acumulacao distribuicao markup markdown." +
            " Faz contagem Elliott se visivel nos graficos." +
            " Verifica confluencia com o bias ICT identificado na camada 1." +

            " CAMADA 10 - SETUP DE ENTRADA:" +
            " FLUXO OBRIGATORIO: Sweep de liquidez confirmado - CHoCH confirmado - FVG ou OB formado apos o sweep - entrada na zona FVG ou OB." +
            " CENARIO PRINCIPAL: entrada exacta - SL abaixo do sweep para long ou acima do sweep para short - TP1 minimo 2 para 1 - TP2 minimo 3 para 1 - TP3 minimo 4 para 1." +
            " CENARIO SECUNDARIO: se existir outro setup valido apresenta com as mesmas regras." +
            " So apresenta setup se o fluxo completo estiver confirmado." +

            " CAMADA 11 - GESTAO DE RISCO:" +
            " Recomenda tamanho de posicao entre 1 e 3 porcento da conta." +
            " Define o nivel exacto de invalidacao do setup." +
            " Lista alertas de risco especificos para este par e este setup." +
            " Confirma que o RR e minimo 2 para 1 antes de recomendar entrada." +

            " CAMADA 12 - SCORE E DECISAO FINAL:" +
            " Apresenta score de 0 a 100 por categoria: Bias - Liquidez - Estrutura - Confluencia - Timing." +
            " Score geral com decisao obrigatoria: ENTRA AGORA se score acima de 70 e fluxo completo confirmado." +
            " AGUARDA CONFIRMACAO se score entre 50 e 70 ou fluxo incompleto." +
            " FICA DE FORA se score abaixo de 50 ou sem sweep confirmado." +
            " Resumo executivo em 3 linhas com o essencial da analise."
        )

        content = [{"type": "text", "text": prompt}]
        for tf in tf_list:
            img = images[tf]
            content.append({"type": "text", "text": "Grafico " + tf + ":"})
            content.append({"type": "image", "source": {"type": "base64", "media_type": img['mimeType'], "data": img['base64']}})

        response = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=4000,
            messages=[{"role": "user", "content": content}]
        )
        return jsonify({'result': response.content[0].text})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
