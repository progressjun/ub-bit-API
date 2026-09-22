"""Disposable real-quotation integration probe. Never uses private keys/orders."""
import json
import os
import secrets
import tempfile
import threading
import time
import uuid
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
import requests
from http.server import ThreadingHTTPServer
from ubbit.config import load_config
from ubbit.remote import EngineService, RemoteHandler

with tempfile.TemporaryDirectory() as directory:
    cfg=load_config('config.sites.yaml')
    cfg.mode='paper'
    cfg.access_key=cfg.secret_key=''
    cfg.engine.db_path=os.path.join(directory,'paper.db')
    cfg.engine.kill_switch_file=os.path.join(directory,'paper.KILL')
    token=secrets.token_urlsafe(48)
    service=EngineService(cfg,token)
    class Handler(RemoteHandler):
        pass
    Handler.service=service
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    http_thread=threading.Thread(target=server.serve_forever,daemon=True)
    http_thread.start()
    try:
        service.start()
        deadline=time.monotonic()+45
        while not service.last_tick and time.monotonic()<deadline:
            time.sleep(.25)
        assert service.last_tick, service.error or 'No completed evaluation'
        base=f'http://127.0.0.1:{server.server_port}'
        headers={'Authorization':'Bearer '+token}
        snap=requests.get(base+'/v1/snapshot',headers=headers,timeout=5).json()
        assert snap['state']['mode']=='paper' and snap['state']['equity']['total']==1000000
        for action in ['resume','pause']:
            r=requests.post(base+'/v1/control',headers=headers,json={'action':action,'requestId':str(uuid.uuid4())},timeout=5)
            assert r.status_code==200 and r.json()['paused']==(action=='pause')
        assert requests.get(base+'/v1/snapshot',timeout=5).status_code==401
        print(json.dumps({'mode':'paper','real_quotation':True,'markets':list(snap['live']['markets']),'equity':snap['state']['equity']['total'],'pause_resume':'passed','unauthorized':401,'private_upbit_calls':0},ensure_ascii=False))
    finally:
        server.shutdown();server.server_close();http_thread.join();service.close()
