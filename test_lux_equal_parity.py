import random
import unittest
import scalp_engine as engine


def reference_equal_events(candles):
    # Independent literal port of LuxAlgo getCurrentStructure(3, true), ATR default.
    result=[]; leg=0; last={'EQH':None,'EQL':None}; ranges=[]; atr=None
    for index,bar in enumerate(candles):
        previous_close=candles[index-1]['c'] if index else None
        ranges.append(bar['h']-bar['l'] if previous_close is None else
                      max(bar['h']-bar['l'],abs(bar['h']-previous_close),abs(bar['l']-previous_close)))
        if index==199: atr=sum(ranges)/200
        elif index>199: atr=(199*atr+ranges[-1])/200
        if index<3: continue
        pivot=candles[index-3]; next_three=candles[index-2:index+1]
        new_leg=0 if pivot['h']>max(x['h'] for x in next_three) else 1 if pivot['l']<min(x['l'] for x in next_three) else leg
        if new_leg!=leg:
            kind='EQL' if new_leg else 'EQH'; price=pivot['l'] if new_leg else pivot['h']
            if last[kind] is not None and atr is not None and abs(last[kind]-price)<0.1*atr:
                result.append((kind,price,bar['t']))
            last[kind]=price
        leg=new_leg
    return result


class LuxEqualParityTests(unittest.TestCase):
    def fixture(self):
        rng=random.Random(729); cs=[]
        for i in range(420):
            price=100+rng.uniform(-1,1)
            cs.append(dict(t=i*3600000,o=price,c=price,h=price+rng.uniform(0.5,1),l=price-rng.uniform(0.5,1)))
        return cs

    def actual(self,cs):
        return engine._kairos_liquidity_map_tf(cs,'H1')['equal_liquidity']

    def test_equal_levels_match_original_lux_three_bar_confirmation(self):
        cs=self.fixture(); expected=reference_equal_events(cs)[-30:]
        self.assertTrue(expected)
        self.assertEqual([(z['tipo'],z['nivel'],z['confirm_ts']) for z in self.actual(cs)],expected)

    def test_future_volatility_cannot_rewrite_prior_eq_formation(self):
        cs=self.fixture(); original=self.actual(cs)
        future=[dict(t=(420+i)*3600000,o=100,c=100,h=10000,l=0) for i in range(4)]
        after=self.actual(cs+future)
        key=lambda z:(z['tipo'],z['nivel'],z['confirm_ts'],z['origin_ts'])
        self.assertEqual([key(z) for z in after if z['confirm_ts']<=cs[-1]['t']],[key(z) for z in original])

    def test_registry_confirms_equal_level_at_native_close(self):
        cs=self.fixture(); cutoff=cs[-1]['t']+3600000
        equal=[z for z in engine._kairos_structural_registry({'H1':cs},cutoff) if z['type'] in ('EQH','EQL')]
        expected=reference_equal_events(cs)[-30:]
        self.assertTrue(expected)
        self.assertEqual({(z['type'],z['level'],z['confirmed_ts']) for z in equal},
                         {(typ,level,t+3600000) for typ,level,t in expected})


if __name__=='__main__': unittest.main()
