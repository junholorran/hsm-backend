def get_binance_price(pair):
    try:
        symbol = pair.replace('USD', 'USDT')
        url = f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}"
        r = requests.get(url, timeout=5)
        return float(r.json()['price'])
    except Exception as e:
        print(f"Binance erro: {e}")
        return None

def price_monitor():
    while True:
        try:
            with alerts_lock:
                remaining = []
                for alert in active_alerts:
                    price = get_binance_price(alert['pair'])
                    if price is None:
                        remaining.append(alert)
                        continue
                    triggered = False
                    if alert['direction'] == 'above' and price >= alert['target']:
                        triggered = True
                    elif alert['direction'] == 'below' and price <= alert['target']:
                        triggered = True
                    if triggered:
                        msg = f"🚨 <b>ALERTA {alert['pair']} ATIVADO!</b>\n"
                        msg += f"💰 Preço atual: ${price:,.2f}\n"
                        msg += f"🎯 Preço alvo: ${alert['target']:,.2f}\n\n"
                        msg += f"📋 <b>ANÁLISE COMPLETA:</b>\n"
                        msg += alert['analysis']
                        send_telegram(msg)
                        print(f"Alerta disparado: {alert['pair']} @ {price}")
                    else:
                        remaining.append(alert)
                active_alerts.clear()
                active_alerts.extend(remaining)
        except Exception as e:
            print(f"Monitor erro: {e}")
        time.sleep(30)
