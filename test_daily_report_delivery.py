import copy
import os
import tempfile
import unittest
from unittest.mock import patch

from kairos_daily_report import DAY, build_report
from test_daily_report_candidate import candle, level


class DailyIntegrationTests(unittest.TestCase):
    def api(self):
        import kairos_daily_delivery
        return kairos_daily_delivery

    def test_disabled_hook_never_calls_map_or_sender(self):
        api=self.api()
        with patch.dict(os.environ,{'KAIROS_DAILY_BRIEF_ENABLED':'false'}):
            self.assertIsNone(api.scan_report('BTCUSD',{},DAY,None,{}))
            self.assertFalse(api.deliver({},lambda _:self.fail('sender called'),'/unwritable/no.db'))

    def test_shared_native_data_and_four_pair_allowlist(self):
        api=self.api();api._CACHE.clear()
        class Engine:
            calls=0
            def _kairos_structural_registry(self,tm,cut):
                self.calls+=1;return [level()]
        engine=Engine();tm={'D1':[candle(0),candle(DAY,115,95,112)]};original=copy.deepcopy(tm)
        decision={'context_bias':{'D1':'alta','H4':'baixa','H1':'alta'}}
        with patch.dict(os.environ,{'KAIROS_DAILY_BRIEF_ENABLED':'true'}):
            for pair in ('BTCUSD','ETHUSD','SOLUSD','BNBUSD'):
                r=api.scan_report(pair,tm,2*DAY,engine,decision)
                self.assertEqual(r['structure_context'],decision['context_bias'])
                self.assertEqual(r['pair'],pair)
            self.assertIsNone(api.scan_report('AAVEUSD',tm,2*DAY,engine,decision))
            self.assertEqual(engine.calls,4)
            r=api.scan_report('BTCUSD',tm,2*DAY+300000,engine,decision)
            self.assertEqual(engine.calls,4)
            self.assertEqual(r['asof_ts'],2*DAY)
        self.assertEqual(tm,original)
        self.assertNotIn('daily_brief',decision)

    def report(self):
        return build_report('BTCUSD',[candle(0),candle(DAY,115,95,112)],
                            [level(value=120+i/100) for i in range(200)],2*DAY,2*DAY)

    def test_failed_page_retries_after_restart_without_resending_acknowledged_page(self):
        api=self.api();r=self.report();sent=[]
        def first(message):
            sent.append(message);return len(sent)==1
        with tempfile.TemporaryDirectory() as path, patch.dict(os.environ,{'KAIROS_DAILY_BRIEF_ENABLED':'true'}):
            db=path+'/daily.db'
            self.assertFalse(api.deliver(r,first,db))
            self.assertEqual(len(sent),2)
            retry=[]
            self.assertTrue(api.deliver(r,lambda message:retry.append(message) or True,db))
            self.assertNotIn(sent[0],retry)
            self.assertTrue(retry)
            self.assertTrue(api.deliver(r,lambda _:self.fail('duplicate'),db))

    def test_storage_failure_does_not_call_sender(self):
        api=self.api()
        with patch.dict(os.environ,{'KAIROS_DAILY_BRIEF_ENABLED':'true'}):
            self.assertFalse(api.deliver(self.report(),lambda _:self.fail('sent before durable storage'),'/missing-directory/no.db'))

    def test_only_ready_current_pair_reports_can_send(self):
        api=self.api();r=self.report();r['pair']='AAVEUSD'
        with patch.dict(os.environ,{'KAIROS_DAILY_BRIEF_ENABLED':'true'}):
            self.assertFalse(api.deliver(r,lambda _:self.fail('sender called'),':memory:'))


if __name__=='__main__':
    unittest.main()
