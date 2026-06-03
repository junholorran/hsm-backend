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
            "Voce e um mentor ICT/SMC de elite com 15 anos de experiencia em Smart Money Concepts. "
            "Analisa os graficos do " + pair + " (" + ", ".join(tf_list) + ") em portugues do Brasil com maxima precisao e detalhes.\n\n"
            "Faz uma analise top-down COMPLETA com TODOS estes topicos:\n\n"
            "1. BIAS DIRECIONAL\n"
            "- Tendencia principal: Higher Highs/Higher Lows ou Lower Highs/Lower Lows\n"
            "- Fase AMD atual: Accumulation, Manipulation ou Distribution\n"
            "- Power of 3: qual fase domina agora\n\n"
            "2. LIQUIDEZ E SMART MONEY\n"
            "- BSL Buy Side Liquidity: niveis exatos acima do preco\n"
            "- SSL Sell Side Liquidity: niveis exatos abaixo do preco\n"
            "- Equal Highs EQH e Equal Lows EQL: onde estao\n"
            "- Quem esta sendo hunted: compradores ou vendedores\n"
            "- Market Maker Model: fase atual\n\n"
            "3. ESTRUTURA DE MERCADO\n"
            "- CHoCH Change of Character: ultimo nivel e preco exato\n"
            "- BOS Break of Structure: confirmado ou pendente\n"
            "- MSS Market Structure Shift: ha inversao em curso\n"
            "- Fase Wyckoff: Spring, Upthrust ou Ranging\n\n"
            "4. FAIR VALUE GAPS E ORDER BLOCKS\n"
            "- FVGs ativos: zonas exatas, bullish ou bearish\n"
            "- IFVGs Inverse FVGs: zonas de resistencia invertidas\n"
            "- Order Blocks bullish e bearish com precos exatos\n"
            "- Breaker Blocks: OBs invertidos\n"
            "- Mitigation Blocks: onde o preco ja testou\n\n"
            "5. OTE OPTIMAL TRADE ENTRY\n"
            "- Fibonacci 62 a 79 porcento do ultimo swing\n"
            "- Zona OTE exata para long e short\n"
            "- Confluencia com FVG ou OB nessa zona\n\n"
            "6. ICT MACROS E KILL ZONES\n"
            "- London Kill Zone: 02:00 a 05:00 NY\n"
            "- NY AM Kill Zone: 07:00 a 11:00 NY\n"
            "- Melhor janela de entrada para hoje\n\n"
            "7. ANALISE PROBABILISTICA\n"
            "- Probabilidade LONG: porcentagem\n"
            "- Probabilidade SHORT: porcentagem\n"
            "- Nivel de confianca: Baixo, Medio, Alto ou Muito Alto\n\n"
            "8. WYCKOFF E ELLIOTT\n"
            "- Fase Wyckoff atual: Accumulation, Markup, Distribution ou Markdown\n"
            "- Onda Elliott: em que onda estamos\n"
            "- Implicacao para o proximo movimento\n\n"
            "9. SETUP RECOMENDADO\n"
            "Cenario 1 maior probabilidade:\n"
            "- Direcao: LONG ou SHORT\n"
            "- Entrada: preco exato ou zona\n"
            "- Stop Loss: preco exato com logica\n"
            "- TP1, TP2, TP3: precos exatos\n"
            "- RR: ratio risco retorno\n"
            "- Trigger: o que confirma a entrada\n\n"
            "Cenario 2 alternativo: mesma estrutura\n\n"
            "10. SCORE DE CONFIANCA 0 a 100\n"
            "Tabela com: Trend Clarity, Liquidity Setup, Structure Confirmation, "
            "FVG OB Quality, CHoCH BOS Signal, OTE Zone, Timing Macro, Wyckoff Phase, Score Final\n\n"
            "11. GESTAO DE RISCO\n"
            "- Porcentagem maxima do capital a arriscar\n"
            "- Invalidacao do setup: preco exato\n\n"
            "12. OBSERVACOES FINAIS\n"
            "- 3 razoes principais para entrar\n"
            "- 3 riscos principais\n"
            "- Recomendacao final em 1 frase\n\n"
            "Se extremamente especifico com precos. Cada nivel deve ter valor exato em USD."
        )

        content = [{"type": "text", "text": prompt}]

        for tf in tf_list:
            img = images[tf]
            content.append({"type": "text", "text": "Grafico " + tf + ":"})
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
            max_tokens=4000,
            messages=[{"role": "user", "content": content}]
        )
        return jsonify({'result': response.content[0].text})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
