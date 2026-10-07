import ast
import types
import unittest
from pathlib import Path


class LegacyTelegramGateTests(unittest.TestCase):
    def load(self, name, enabled):
        tree=ast.parse(Path('scalp_engine.py').read_text())
        fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name==name)
        env={'TELEGRAM_TOKEN':'test-token','TELEGRAM_CHAT_ID':'test-chat'}
        if enabled is not None: env['PAPER_TRADING_TELEGRAM_ENABLED']=enabled
        self.calls=[]
        ns={'os':types.SimpleNamespace(environ=env),
            'requests':types.SimpleNamespace(post=lambda *a,**kw:self.calls.append((a,kw))),
            '_garantir_tabela_prealerta_paper_v2':lambda path:self.calls.append(('database',path))}
        exec(compile(ast.Module(body=[fn],type_ignores=[]),'scalp_engine.py','exec'),ns)
        return ns[name]

    def test_disabled_sender_never_calls_telegram(self):
        sender=self.load('_paper_trading_v2_enviar_telegram','0')
        self.assertFalse(sender('old alert'))
        self.assertEqual(self.calls,[])

    def test_default_sender_keeps_existing_behavior(self):
        sender=self.load('_paper_trading_v2_enviar_telegram',None)
        self.assertTrue(sender('old alert'))
        self.assertEqual(len(self.calls),1)

    def test_disabled_prealert_never_reserves_database_key(self):
        prealert=self.load('_paper_v2_tentar_prealerta','0')
        self.assertFalse(prealert('test.db','ONDOUSD',{'failure_reason':'AGUARDANDO_RETESTE_ZONA','prealert_limit':0.46605},0))
        self.assertEqual(self.calls,[])


if __name__=='__main__': unittest.main()
