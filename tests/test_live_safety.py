import base64
import hashlib
import json
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import requests

from ubbit.auth import make_jwt
from ubbit.broker import LiveBroker, Fill
from ubbit.config import Config, EngineParams
from ubbit.engine import TradingEngine
from ubbit.fees import CostModel
from ubbit.remote import EngineService, RemoteHandler
from ubbit.session import SessionParams
from ubbit.state import Store, Position
from ubbit.upbit import UpbitClient, UpbitError
from http.server import ThreadingHTTPServer
from tests.test_engine import FakeClient
from tests.test_strategy import breakout_candles


class LiveSafety(unittest.TestCase):
    def test_no_retry_for_unknown_post(self):
        client=UpbitClient('key','secret',max_retries=4)
        client.session.post=Mock(side_effect=requests.Timeout())
        with self.assertRaises(UpbitError):
            client.market_buy('KRW-BTC',10000,'fixed-id')
        self.assertEqual(client.session.post.call_count,1)

    def test_posts_json_and_generates_fresh_auth_on_read_retry(self):
        client=UpbitClient('key','secret',max_retries=2)
        response=Mock(status_code=200)
        response.json.return_value={'uuid':'order'}
        client.session.post=Mock(return_value=response)
        client.market_buy('KRW-BTC',10000,'id')
        self.assertEqual(client.session.post.call_args.kwargs['json']['price'],'10000')
        self.assertNotIn('data',client.session.post.call_args.kwargs)
        client.session.get=Mock(side_effect=[requests.Timeout(),response])
        with patch('ubbit.upbit.time.sleep'):
            client.accounts()
        calls=client.session.get.call_args_list
        self.assertNotEqual(calls[0].kwargs['headers']['Authorization'],calls[1].kwargs['headers']['Authorization'])

    def test_auth_hash_uses_decoded_query(self):
        token=make_jwt('k','s',{'states':['wait','watch']})
        header,payload,_=token.split('.')
        decode=lambda v:json.loads(base64.urlsafe_b64decode(v+'='*(-len(v)%4)))
        self.assertEqual(decode(header)['alg'],'HS512')
        self.assertEqual(decode(payload)['query_hash'],hashlib.sha512(b'states[]=wait&states[]=watch').hexdigest())

    def test_unknown_is_durable_and_blocks_new_orders(self):
        with tempfile.TemporaryDirectory() as td:
            db=Store(os.path.join(td,'test.db'))
            client=Mock()
            client.market_buy.side_effect=UpbitError(-1,{},'/v1/orders')
            client.order_by_identifier.side_effect=UpbitError(404,{},'/v1/order')
            broker=LiveBroker(client,CostModel(),journal=db)
            fill=broker.buy('KRW-BTC',10000,100)
            self.assertTrue(fill.uncertain)
            self.assertEqual(len(db.pending_intents()),1)
            reborn=LiveBroker(client,CostModel(),journal=db)
            self.assertTrue(reborn.buy('KRW-ETH',10000,100).uncertain)
            self.assertEqual(client.market_buy.call_count,1)
            db.close()

    def test_nonterminal_partial_fill_is_not_final(self):
        client=Mock()
        client.market_sell.return_value={'uuid':'x'}
        client.wait_fill.return_value={'state':'wait','executed_volume':'1','trades':[{'funds':'10000','volume':'1'}]}
        fill=LiveBroker(client,CostModel()).sell('KRW-BTC',2,10000)
        self.assertFalse(fill.ok)
        self.assertTrue(fill.uncertain)

    def test_terminal_partial_sell_keeps_remaining_position(self):
        with tempfile.TemporaryDirectory() as td:
            cfg=Config(engine=EngineParams(db_path=os.path.join(td,'t.db'),kill_switch_file=os.path.join(td,'kill')))
            engine=TradingEngine(cfg)
            pos=Position('KRW-BTC',10000,2,20000,9000,.01,10000)
            engine.store.upsert_position(pos)
            engine.broker.sell=lambda *a:Fill(True,'KRW-BTC','ask',11000,1,10994.5,5.5)
            engine._close_position(pos,11000,'stop','test')
            remaining=engine.store.get_position('KRW-BTC')
            self.assertEqual(remaining.qty,1)
            self.assertEqual(remaining.entry_krw,10000)
            self.assertAlmostEqual(engine.store.recent_trades()[0]['net_pnl'],994.5)
            engine.store.close()

    def test_paper_restart_restores_cash_holdings_and_risk(self):
        with tempfile.TemporaryDirectory() as td:
            cfg=Config(engine=EngineParams(db_path=os.path.join(td,'t.db'),kill_switch_file=os.path.join(td,'kill'),markets=['KRW-TEST'],liquidity_min_value_krw=0),session=SessionParams(enabled=False))
            engine=TradingEngine(cfg)
            engine.client=FakeClient(breakout_candles())
            engine.tick()
            self.assertTrue(engine.broker.holdings)
            cash,holdings,trades=engine.broker.cash,engine.broker.holdings.copy(),engine.risk.state.trades_today
            engine.store.close()
            reborn=TradingEngine(cfg)
            self.assertAlmostEqual(reborn.broker.cash,cash)
            self.assertEqual(reborn.broker.holdings,holdings)
            self.assertEqual(reborn.risk.state.trades_today,trades)
            reborn.store.close()


class RemoteSecurity(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        cfg=Config(engine=EngineParams(db_path=os.path.join(self.tmp.name,'t.db'),kill_switch_file=os.path.join(self.tmp.name,'kill')))
        self.service=EngineService(cfg,'x'*40)
    def tearDown(self):
        self.service.close()
        self.tmp.cleanup()
    def test_second_engine_refused(self):
        with self.assertRaises(RuntimeError):
            EngineService(self.service.cfg,'x'*40)
    def test_duplicate_resume_cannot_undo_later_pause(self):
        self.service.thread=Mock()
        self.service.thread.is_alive.return_value=True
        self.service.last_tick_at=time.time()
        self.assertEqual(self.service.control({'action':'resume','requestId':'r'*20},'owner')[0],200)
        self.assertFalse(self.service.engine.paused)
        self.service.control({'action':'pause','requestId':'p'*20},'owner')
        self.service.control({'action':'resume','requestId':'r'*20},'owner')
        self.assertTrue(self.service.engine.paused)
        self.service.thread=None
    def test_live_requires_explicit_confirmation(self):
        self.service.cfg.mode='live'
        self.service.thread=Mock()
        self.service.thread.is_alive.return_value=True
        self.service.last_tick_at=time.time()
        status,_=self.service.control({'action':'resume','requestId':'r'*20},'owner')
        self.assertEqual(status,409)
        self.assertTrue(self.service.engine.paused)
        self.service.thread=None
    def test_http_rejects_missing_auth_and_hides_secrets(self):
        class Handler(RemoteHandler):
            service=self.service
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        try:
            url=f'http://127.0.0.1:{server.server_port}/v1/snapshot'
            self.assertEqual(requests.get(url,timeout=3).status_code,401)
            r=requests.get(url,headers={'Authorization':'Bearer '+'x'*40},timeout=3)
            self.assertEqual(r.status_code,200)
            self.assertNotIn('x'*40,r.text)
            self.assertFalse(r.json()['running'])
        finally:
            server.shutdown();server.server_close();thread.join()


if __name__=='__main__':
    unittest.main()
