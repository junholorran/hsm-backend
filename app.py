from flask import Flask, request, jsonify, send_from_directory
import anthropic
import os

app = Flask(__name__)
client = anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY'))

PROMPT_TEMPLATE = """És um mentor ICT/SMC de elite com 15 anos de experiência. Analisa os gráficos do {pair} ({timeframes}) em português PT-BR com máxima precisão.

Faz uma análise top-down COMPLETA seguindo esta metodologia:

## 1. BIAS DIRECIONAL (D1/W1)
- Tendência principal: Higher Highs/Higher Lows ou Lower Highs/Lower Lows
- Power of 3 (AMD): fase atual — Accumulation, Manipulation ou Distribution?
- Sessões: contexto Asia, London, NY — qual domina?

## 2. LIQUIDEZ & SMART MONEY
- BSL (Buy Side Liquidity): níveis exatos acima
- SSL (Sell Side Liquidity): níveis exatos abaixo
- Equal Highs/Lows (EQH/EQL): onde estão?
- Quem está a ser hunted — compradores ou vendedores?
- Market Maker Model: fase de acumulação ou distribuição?

## 3. ESTRUTURA DE MERCADO
- CHoCH (Change of Character): último onde e em que preço?
- BOS (Break of Structure): confirmado ou pendente?
- MSS (Market Structure Shift): há inversão em curso?
- Wyckoff: Spring, Upthrust, ou fase de ranging?

## 4. FAIR VALUE GAPS & ORDER BLOCKS
- FVGs ativos: zonas exatas, bullish ou bearish, já mitigados?
- IFVGs (Inverse FVGs): zonas de resistência/suporte invertidas
- Order Blocks: bullish e bearish, com preços exatos
- Breaker Blocks: OBs invertidos que mudaram de função
- Mitigation Blocks: onde o preço já testou

## 5. OTE - OPTIMAL TRADE ENTRY
- Fibonacci 62%-79% do último swing
- Zona OTE exata para long e short
- Confluência com FVG ou OB nessa zona?

## 6. ICT MACROS & TIMING
- Macro atual: 02:33, 04:03, 08:50, 10:10, 14:50, 16:10 NY
- Sessão London Kill Zone: 02:00-05:00 NY
- NY AM Kill Zone: 07:00-11:00 NY
- Melhor janela de entrada para hoje

## 7. ANÁLISE PROBABILÍSTICA
- Probabilidade LONG: X%
- Probabilidade SHORT: X%
- Justificativa baseada em confluências
- Nível de confiança: Baixo/Médio/Alto/Muito Alto

## 8. WYCKOFF + ELLIOTT
- Fase Wyckoff atual: Accumulation/Markup/Distribution/Markdown
- Onda Elliott: em que onda estamos? (1-5 ou A-B-C)
- Implicação para o próximo movimento

## 9. SETUP RECOMENDADO
Cenário 1 (maior probabilidade):
- Direção: LONG ou SHORT
- Entrada: preço exato ou zona
- Stop Loss: preço exato com lógica
- TP1, TP2, TP3: preços exatos
- RR: ratio risco/retorno
- Trigger de entrada: o que confirma?

Cenário 2 (alternativo):
- Mesma estrutura acima

## 10. SCORE DE CONFIANÇA (0-100)
Tabela com:
- Trend Clarity
- Liquidity Setup
- Structure Confirmation
- FVG/OB Quality
- CHoCH/BOS Signal
- OTE Zone
- Timing/Macro
- Wyckoff Phase
- Score Final

## 11. GESTÃO DE RISCO
- % máxima do capital a arriscar
- Tamanho de posição sugerido
- Invalidação do setup: preço exato

## 12. OBSERVAÇÕES FINAIS
- 3 razões principais para entrar
- 3 riscos principais
- Recomendação final em 1 frase

Sê extremamente específico com preços. Cada nível deve ter valor exato."""

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
        
        prompt = PROMPT_TEMPLATE.format(
            pair=pair,
            timeframes=', '.join(tf_list)
        )
        
        content = [{"type": "text", "text": prompt}]
        
        for tf in tf_list:
            img = images[tf]
            content.append({"type": "text", "text": f"📊 Gráfico {tf}:"})
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
