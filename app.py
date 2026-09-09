import os
from flask import Flask, jsonify, request
import scalp_engine as kairos_engine

app=Flask(__name__)

@app.get('/health')
def health(): return jsonify({'ok':True,'engine':'kairos_radar_v2_1'})

@app.post('/api/kairos/radar')
def radar():
    secret=os.environ.get('PAPER_TRADING_TICK_SECRET')
    if not secret: return jsonify({'error':'endpoint desabilitado'}),503
    recebido=request.headers.get('X-Paper-Tick-Secret') or request.args.get('token')
    if recebido!=secret: return jsonify({'error':'não autorizado'}),401
    data=request.get_json(silent=True) or {}
    pair=str(data.get('pair') or '').strip().upper()
    candles=data.get('candles_por_tf')
    if not pair or not isinstance(candles,dict): return jsonify({'error':'pair e candles_por_tf obrigatórios'}),400
    out=kairos_engine.avaliar_kairos_radar_v2(candles,pair=pair)
    if out.get('setups'):
        out['telegram_preview']=[kairos_engine.format_telegram_setup(s,pair) for s in out['setups']]
    return jsonify(out),200

if __name__=='__main__': app.run(host='0.0.0.0',port=int(os.environ.get('PORT','8080')))
