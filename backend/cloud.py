"""Bounded, anonymous visitor workspaces for one free hosting instance.

Each visitor gets a module namespace, SQLite file, engine, and artifact directory.
Owner credentials and owner data are never loaded. Opaque HttpOnly cookie is the
workspace access token; hosted API keys remain in that namespace's memory only.
"""
import asyncio
import importlib.util
import os
import secrets
import shutil
import sys
import time
from collections import deque
from contextlib import asynccontextmanager
from http.cookies import SimpleCookie
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware
from .demo_data import seed_demo
from .showcase_seed import seed_showcase
from .tickets import seed_tickets

CODE = Path(__file__).resolve().parents[1]
SESSIONS = Path(os.environ.get('WORKMATE_CLOUD_DATA', '/tmp/workmate-sessions' if os.name != 'nt' else str(CODE / 'data' / 'cloud'))).resolve()
TTL = 3600
MAX_VISITORS = max(1, min(20, int(os.environ.get('WORKMATE_MAX_VISITORS', '8'))))
VISITORS = {}
LOCK = asyncio.Lock()


def busy(module):
    return any(t.get('status') in ('running', 'queued') for k in ('tasks','uat_runs','hub_runs') for t in module.store.all(k))


def destroy(sid, entry):
    entry['module'].engine.stop()
    entry['module'].planner.key = ''
    entry['module'].m365.close()
    sys.modules.pop(entry['module'].__name__, None)
    target = (SESSIONS / sid).resolve()
    if target.parent == SESSIONS and target.is_dir():
        shutil.rmtree(target)


def make_visitor(sid):
    root = SESSIONS / sid
    for name in ('fixtures', 'examples'):
        source = CODE / 'data' / name
        # Copy published input fixtures only; never traverse development
        # junctions, node_modules or previous verification/output directories.
        folders = [source] + ([source / 'excel_showcase'] if name == 'examples' else [])
        for folder in folders:
            if not folder.is_dir():
                continue
            for file in folder.iterdir():
                if file.is_file() and not file.is_symlink() and file.suffix.lower() in ('.xlsx','.txt','.json','.html','.md'):
                    target = root / 'data' / name / file.relative_to(source)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(file, target)
    name = 'backend.visitor_' + sid
    spec = importlib.util.spec_from_file_location(name, CODE / 'backend' / 'app.py')
    module = importlib.util.module_from_spec(spec)
    module._WORKMATE_ROOT, module._WORKMATE_HOSTED = root, True
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
        module.store.seed()
        seed_demo(root, module.store)
        seed_showcase(root, module.store)
        seed_tickets(module.store)
        from .agent_hub import seed_agent_hub
        seed_agent_hub(module.store)
        module.engine.start()
        return {'module':module, 'touched':time.monotonic(), 'requests':deque(), 'models':deque()}
    except Exception:
        sys.modules.pop(name, None)
        if root.resolve().parent == SESSIONS:
            shutil.rmtree(root)
        raise


@asynccontextmanager
async def lifespan(app):
    SESSIONS.mkdir(parents=True, exist_ok=True)
    yield
    for sid, entry in list(VISITORS.items()):
        # Process exit discards in-memory keys. Do not delete active artifacts
        # while a worker could still be writing them.
        entry['module'].engine.stop()
        entry['module'].planner.key = ''
        entry['module'].m365.close()
    VISITORS.clear()


app = FastAPI(title='Workmate Online', lifespan=lifespan, docs_url=None, openapi_url=None)
host = os.environ.get('RENDER_EXTERNAL_HOSTNAME') or os.environ.get('WORKMATE_PUBLIC_HOST', '')
app.add_middleware(TrustedHostMiddleware, allowed_hosts=['127.0.0.1','localhost','testserver']+([host] if host else []))


@app.middleware('http')
async def headers(request, call_next):
    response = await call_next(request)
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'same-origin'
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    return response


@app.get('/api/health')
def health():
    return {'ok':True, 'service':'workmate', 'version':'0.7.0', 'hosted':True}


class VisitorRouter:
    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return
        headers = dict(scope['headers'])
        origin = headers.get(b'origin', b'').decode('latin1')
        expected = 'https://' + host if host else 'http://testserver'
        if scope['method'] not in ('GET','HEAD','OPTIONS') and origin and origin != expected:
            return await JSONResponse({'detail':'현재 워크스페이스에서 시작한 요청만 허용됩니다.'},403)(scope,receive,send)
        try:
            size = int(headers.get(b'content-length', b'0') or b'0')
        except ValueError:
            return await JSONResponse({'detail':'잘못된 요청 크기입니다.'},400)(scope,receive,send)
        if size > 12*1024*1024:
            return await JSONResponse({'detail':'한 번에 12 MB 이하로 업로드하세요.'},413)(scope,receive,send)
        cookies = SimpleCookie()
        try: cookies.load(headers.get(b'cookie',b'').decode('latin1'))
        except Exception: pass
        token = cookies.get('workmate_session')
        sid = token.value if token else None
        new = False
        async with LOCK:
            now = time.monotonic()
            for old_sid, entry in list(VISITORS.items()):
                if now-entry['touched'] > TTL and not busy(entry['module']):
                    destroy(old_sid, VISITORS.pop(old_sid))
            if sid not in VISITORS:
                if scope['method'] != 'GET' or scope['path'].startswith('/api/'):
                    return await JSONResponse({'detail':'워크스페이스를 다시 열어 연결하세요.'},401)(scope,receive,send)
                if len(VISITORS) >= MAX_VISITORS:
                    return await JSONResponse({'detail':'현재 접속자가 많습니다. 잠시 후 다시 열어주세요.'},503)(scope,receive,send)
                sid = secrets.token_hex(24)
                VISITORS[sid] = await asyncio.to_thread(make_visitor, sid)
                new = True
            entry = VISITORS[sid]
            entry['touched'] = now
            if scope['method']=='POST' and any(x in scope['path'] for x in ('/tasks','/uat','/apps','/workflows','/agent-hub','/chat')):
                active = sum(t.get('status') in ('running','queued') for k in ('tasks','uat_runs','hub_runs') for t in entry['module'].store.all(k))
                if active >= 4:
                    return await JSONResponse({'detail':'진행 중인 실행을 마친 후 다시 요청하세요.'},429)(scope,receive,send)
            model_request = scope['path'] in ('/api/chat','/api/connection/config','/api/connection/test') or (scope['path'].startswith('/api/agent-hub/') and (scope['path'].endswith('/run') or scope['path'].endswith('/missions')))
            lane = entry['models'] if model_request and scope['method']=='POST' else entry['requests']
            while lane and now-lane[0] > 60: lane.popleft()
            if len(lane) >= (10 if lane is entry['models'] else 120):
                return await JSONResponse({'detail':'요청이 많습니다. 1분 후 다시 실행하세요.'},429)(scope,receive,send)
            lane.append(now)
        received = 0
        async def bounded_receive():
            nonlocal received
            message = await receive()
            received += len(message.get('body',b''))
            if received > 12*1024*1024:
                from starlette.exceptions import HTTPException
                raise HTTPException(413, '업로드 크기를 확인하세요.')
            return message
        async def session_send(message):
            if message['type']=='http.response.start':
                cookie = f'workmate_session={sid}; HttpOnly; Path=/; SameSite=Lax; Max-Age=3600' + ('; Secure' if host else '')
                message.setdefault('headers',[]).append((b'set-cookie',cookie.encode()))
            await send(message)
        await entry['module'].app(scope, bounded_receive, session_send)


app.mount('/', VisitorRouter())
