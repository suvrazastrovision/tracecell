"""Upload app: explicit loopback development or allowlisted Firebase sessions."""
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import threading
from datetime import timedelta

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .blobs import LocalBlobs, GCSBlobs

from .contracts import Upload, Submission, MAX_BYTES, now
from .service import Service, valid_id
from .store import LocalStore, FirestoreStore
from .worker import object_read, read_package

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_PORT = 8768
LOCAL_PROCESS = None
LOCAL_MUTEX = threading.Lock()


def make_service():
    if os.getenv('SCRNA_MODE') == 'cloud':
        project = os.environ['GOOGLE_CLOUD_PROJECT']
        return Service(FirestoreStore(project), GCSBlobs(project, os.environ['SCRNA_BUCKET']))
    if os.getenv('K_SERVICE'):
        raise RuntimeError('Cloud Run requires explicit cloud mode; local authentication is forbidden')
    root = Path(os.getenv('SCRNA_LOCAL_ROOT', ROOT / 'artifacts' / 'scrna-app')).resolve()
    return Service(LocalStore(root / 'state.sqlite'), LocalBlobs(root / 'objects'),
                   unlimited_run_owners={'local-researcher'})


def firebase():
    import firebase_admin
    from firebase_admin import auth
    if not firebase_admin._apps:
        firebase_admin.initialize_app(options={'projectId': os.environ['GOOGLE_CLOUD_PROJECT']})
    return auth


def create_app(service=None, local=None):
    service = service or make_service()
    local = os.getenv('SCRNA_MODE', 'local') != 'cloud' if local is None else local
    if local and os.getenv('K_SERVICE'):
        raise RuntimeError('Development mode forbidden on Cloud Run')
    app = FastAPI(title='TraceCell scRNA-seq Knowledge Platform API', docs_url=None, redoc_url=None, openapi_url=None, description='Single-cell sample uploads, QC processing, verified results, Run Evidence questions and cited research-paper comparisons. Local access uses loopback development mode; cloud API calls require an invited-user session. Writes require same-origin CSRF protection.')
    allowed = os.getenv('SCRNA_ALLOWED_EMAILS', '').lower().split(',')
    allowed = {x.strip() for x in allowed if x.strip()}
    if not local and not 0 < len(allowed) <= 5:
        raise RuntimeError('Cloud mode requires one to five invited email addresses')
    origin = os.getenv('SCRNA_ORIGIN', f"http://127.0.0.1:{os.getenv('PORT', str(DEFAULT_PORT))}").rstrip('/')
    from urllib.parse import urlsplit
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=['127.0.0.1', 'localhost', 'testserver'] if local else [urlsplit(origin).hostname])
    app.mount('/static', StaticFiles(directory=Path(__file__).parent / 'static'), name='static')

    @app.middleware('http')
    async def headers(request, call_next):
        if request.method not in ('GET', 'HEAD', 'OPTIONS') and not request.url.path.startswith('/internal/'):
            if request.headers.get('origin') != origin:
                return JSONResponse({'detail': 'Request origin rejected'}, status_code=403)
            cookie, token = request.cookies.get('csrf', ''), request.headers.get('x-csrf-token', '')
            if not cookie or not secrets.compare_digest(cookie, token):
                return JSONResponse({'detail': 'CSRF token rejected'}, status_code=403)
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self' https://www.gstatic.com https://apis.google.com; connect-src 'self' https://*.googleapis.com https://*.firebaseapp.com; frame-src https://*.firebaseapp.com https://accounts.google.com; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        if request.url.path.rstrip('/') == '/docs':
            response.headers['Content-Security-Policy'] = response.headers['Content-Security-Policy'].replace("style-src 'self';", "style-src 'self' 'unsafe-inline';")
        return response

    def owner(request):
        if local:
            return 'local-researcher'
        try:
            claims = firebase().verify_session_cookie(request.cookies.get('session', ''), check_revoked=True)
            if not claims.get('email_verified') or claims.get('email', '').lower() not in allowed:
                raise ValueError('User is not invited')
            return claims['uid']
        except Exception:
            raise HTTPException(401, 'Sign in with an invited Google account')

    from .api_docs import install_docs, group_endpoints
    install_docs(app, owner, local)

    def internal(request):
        if local:
            raise HTTPException(404)
        try:
            from google.oauth2 import id_token
            from google.auth.transport.requests import Request as AuthRequest
            token = request.headers.get('authorization', '').removeprefix('Bearer ')
            claims = id_token.verify_oauth2_token(token, AuthRequest(), audience=origin)
            if claims.get('email') != os.environ['SCRNA_DISPATCH_EMAIL'] or not claims.get('email_verified'):
                raise ValueError('Wrong dispatcher')
        except Exception:
            raise HTTPException(403, 'Invalid internal identity')

    def kick():
        global LOCAL_PROCESS
        if local:
            with LOCAL_MUTEX:
                if LOCAL_PROCESS is None or LOCAL_PROCESS.poll() is not None:
                    # Separate process bounds parsing/processing and survives the web request.
                    LOCAL_PROCESS = subprocess.Popen([sys.executable, '-m', 'pbmc_platform.app.runtime', 'dispatch-local'],
                                                     cwd=ROOT, env={**os.environ, 'PYTHONPATH': str(ROOT / 'src')},
                                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    @app.exception_handler(ValueError)
    async def invalid(request, error):
        return JSONResponse({'detail': str(error)}, status_code=400)

    @app.exception_handler(LookupError)
    async def missing(request, error):
        return JSONResponse({'detail': str(error)}, status_code=404)

    @app.get('/ready')
    def ready():
        return {'status': 'ready', 'mode': 'local' if local else 'cloud'}

    @app.get('/', response_class=HTMLResponse, include_in_schema=False)
    def index():
        response = HTMLResponse((Path(__file__).parent / 'static' / 'index.html').read_text(encoding='utf-8'))
        response.set_cookie('csrf', secrets.token_urlsafe(32), secure=not local, httponly=False, samesite='strict')
        return response

    @app.get('/api/session')
    def session(request: Request):
        try:
            identity = owner(request)
        except HTTPException:
            identity = None
        return {'local': local, 'signed_in': identity is not None, 'firebase': json.loads(os.getenv('SCRNA_FIREBASE_CONFIG', '{}')),
                'run_limit_per_day': service.run_limit(identity),
                'run_quota_exempt': identity is not None and service.run_limit(identity) is None}

    @app.post('/api/session')
    def login(request: Request, payload: dict):
        if local:
            raise HTTPException(400, 'Local demo has no cloud login')
        try:
            token = payload['id_token']
            claims = firebase().verify_id_token(token, check_revoked=True)
            import time
            if time.time() - claims.get('auth_time', 0) > 300 or not claims.get('email_verified') or claims.get('email', '').lower() not in allowed:
                raise ValueError('Not invited or sign-in expired')
            cookie = firebase().create_session_cookie(token, expires_in=timedelta(hours=8))
        except Exception:
            raise HTTPException(403, 'An invited Google account and recent sign-in are required')
        response = JSONResponse({'signed_in': True})
        response.set_cookie('session', cookie, secure=True, httponly=True, samesite='strict', max_age=28800)
        return response

    @app.delete('/api/session')
    def logout(request: Request):
        response = JSONResponse({'signed_in': False})
        response.delete_cookie('session')
        return response

    @app.get('/api/examples')
    def examples(request: Request):
        owner(request)
        path = ROOT / 'examples' / 'app-datasets.json'
        return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}

    @app.post('/api/examples/{example}')
    def use_example(example: str, request: Request):
        identity = owner(request)
        catalog = json.loads((ROOT / 'examples' / 'app-datasets.json').read_text(encoding='utf-8'))
        if example not in catalog:
            raise LookupError('Example not found')
        spec = catalog[example]
        existing = [x for x in service.store.scan('uploads') if x['owner'] == identity
                    and x['expires_at'] > now() and x.get('filename') == spec['prepared_filename']
                    and x['status'] in ('queued_inspection', 'running', 'inspected', 'ready')
                    and x.get('input', {}).get('sha256') == spec['prepared_sha256']]
        if existing:
            kick()
            return max(existing, key=lambda x: x['created_at'])
        if local:
            data = (ROOT / 'data' / 'app-examples' / spec['prepared_filename']).read_bytes()
        else:
            data = service.blobs.read('examples/' + spec['prepared_filename'])
        from .blobs import sha
        if sha(data) != spec['prepared_sha256']:
            raise ValueError('Example bytes do not match the recorded source')
        record = service.upload(identity, Upload(filename=spec['prepared_filename'], size=len(data), public_data=True))
        service.blobs.write(record['key'], data)
        result = service.finalize(identity, record['id'])
        kick()
        return result

    @app.get('/api/uploads')
    def uploads(request: Request):
        identity = owner(request)
        return [x for x in service.store.scan('uploads') if x['owner'] == identity and x['status'] != 'deleted' and x['expires_at'] > now()]

    @app.post('/api/uploads')
    def upload(request: Request, payload: Upload):
        record = service.upload(owner(request), payload)
        target = '/api/uploads/' + record['id'] + '/content'
        if not local:
            target = service.blobs.upload_session(record['key'], record['size'], origin)
        return {'upload': record, 'upload_url': target}

    @app.put('/api/uploads/{identity}/content', openapi_extra={'requestBody': {'required': True, 'content': {'application/octet-stream': {'schema': {'type': 'string', 'format': 'binary'}}}}})
    async def content(identity: str, request: Request):
        if not local:
            raise HTTPException(404)
        record = service.owned('uploads', identity, owner(request))
        if record['status'] != 'uploading':
            raise ValueError('Upload already finalized')
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > record['size'] or size > MAX_BYTES:
                raise ValueError('Upload exceeds declared limit')
            chunks.append(chunk)
        if size != record['size']:
            raise ValueError('Incomplete upload')
        service.blobs.write(record['key'], b''.join(chunks))
        return {'uploaded': True}

    @app.post('/api/uploads/{identity}/finalize')
    def finalize(identity: str, request: Request):
        result = service.finalize(owner(request), valid_id(identity))
        kick()
        return result

    @app.post('/api/runs')
    def submit(request: Request, payload: Submission):
        result = service.submit(owner(request), payload)
        kick()
        return result

    @app.get('/api/runs')
    def runs(request: Request):
        identity = owner(request)
        kick()
        return sorted([x for x in service.store.scan('runs') if x['owner'] == identity and x['status'] != 'deleted' and x['expires_at'] > now()], key=lambda x: x['created_at'], reverse=True)

    @app.post('/api/history/clear')
    def clear_history(request: Request):
        return service.clear_history(owner(request))

    @app.get('/api/queue-status')
    def queue_status(request: Request):
        owner(request)
        slot = service.store.get('control/slot')
        active = service.store.get(slot['kind'] + '/' + slot['owner']) if slot else None
        return {'blocked': bool(slot and (not active or active.get('status') == 'failed')),
                'paused': bool((service.store.get('control/admission') or {}).get('paused'))}

    @app.get('/api/runs/{identity}')
    def run(identity: str, request: Request):
        return service.owned('runs', identity, owner(request))

    @app.post('/api/runs/{identity}/cancel')
    def cancel(identity: str, request: Request):
        service.owned('runs', identity, owner(request))
        key = 'runs/' + identity
        def change(values):
            record = values[key]
            if record['status'] != 'queued':
                raise ValueError('Only queued runs can be cancelled')
            record['status'] = 'cancelled'
            return record, {key: record}
        return service.store.atomic([key], change)

    @app.get('/api/runs/{identity}/results')
    def results(identity: str, request: Request):
        return read_package(service, service.owned('runs', identity, owner(request)))

    @app.get('/api/runs/{identity}/download/{name}')
    def download(identity: str, name: str, request: Request):
        record = service.owned('runs', identity, owner(request))
        read_package(service, record)
        marker = json.loads(object_read(service, record['publication']))
        if name == 'publication.json':
            data = object_read(service, record['publication'])
        elif name in marker['objects']:
            data = object_read(service, marker['objects'][name])
        else:
            raise LookupError('Download not found')
        return Response(data, media_type='application/octet-stream', headers={'Content-Disposition': f'attachment; filename="{name}"'})

    @app.get('/api/runs/{identity}/plots/{kind}.{format}')
    def plot_download(identity: str, kind: str, format: str, request: Request, scale: str = 'linear', preview: bool = False):
        from .plots import KINDS, FORMATS, render_plot
        if kind not in KINDS or format not in FORMATS or scale not in ('linear', 'log'):
            raise HTTPException(404, 'Plot, format or scale not supported')
        record = service.owned('runs', identity, owner(request))
        package = read_package(service, record)
        marker = json.loads(object_read(service, record['publication']))
        payload = object_read(service, marker['objects']['plots.json'])
        data = render_plot(payload, json.dumps(package['configuration'], sort_keys=True), kind, format, scale)
        disposition = 'inline' if format == 'svg' or (preview and format in ('png', 'jpeg')) else 'attachment'
        return Response(data, media_type=FORMATS[format], headers={
            'Content-Disposition': f'{disposition}; filename="qc-{kind}-{identity[:8]}-{scale}.{format}"'})

    @app.get('/api/compare')
    def compare(request: Request, left: str, right: str):
        return service.compare(owner(request), left, right)

    @app.get('/api/runs/{identity}/answer')
    def answer(identity: str, request: Request, topic: str):
        package = read_package(service, service.owned('runs', identity, owner(request)))
        mapping = {'summary': ('metrics',), 'qc': ('metrics',), 'exclusions': ('exclusions',),
                   'policy': ('configuration',), 'dbt': ('dbt',), 'limitations': ('limitations',)}
        if topic not in mapping:
            return {'status': 'unsupported', 'topics': list(mapping)}
        key = mapping[topic][0]
        return {'status': 'supported', 'facts': package[key], 'json_pointer': '/' + key,
                'evidence': package['evidence'], 'limitations': package['limitations']}

    from .knowledge_chat import Question, ExtractionRequest
    from .research import Reference

    @app.get('/api/assistant/status')
    def assistant_status(request: Request):
        owner(request)
        from .knowledge_chat import model_status
        return model_status()

    @app.post('/api/runs/{identity}/chat')
    def chat(identity: str, payload: Question, request: Request):
        from .knowledge_chat import ask
        user = owner(request)
        package = read_package(service, service.owned('runs', identity, user))
        return ask(service, user, package, payload)

    @app.get('/api/runs/{identity}/papers')
    def list_papers(identity: str, request: Request):
        from .research import public_paper
        user = owner(request)
        service.owned('runs', identity, user)
        return [public_paper(x) for x in service.store.scan('papers') if x['run_id'] == identity
                and x['owner'] == user and x['status'] == 'ready' and x['expires_at'] > now()]

    @app.post('/api/runs/{identity}/papers', openapi_extra={'requestBody': {'required': True, 'content': {'application/pdf': {'schema': {'type': 'string', 'format': 'binary'}}}}})
    async def upload_paper(identity: str, request: Request, filename: str = 'research-paper.pdf'):
        """Upload a text-based PDF up to 100 MB (100,000,000 bytes), with at most 40 pages."""
        from .research import MAX_PDF, save_paper
        from starlette.concurrency import run_in_threadpool
        user = owner(request)
        service.owned('runs', identity, user)
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_PDF:
                raise ValueError('Paper exceeds the 100 MB limit')
            chunks.append(chunk)
        return await run_in_threadpool(save_paper, service, user, identity, filename, b''.join(chunks))

    @app.get('/api/runs/{identity}/papers/{paper_id}')
    def view_paper(identity: str, paper_id: str, request: Request):
        from .research import paper_record, paper_pages, public_paper
        record = paper_record(service, owner(request), identity, paper_id)
        return {**public_paper(record), 'pages': paper_pages(service, record)}

    @app.get('/api/runs/{identity}/papers/{paper_id}/pdf')
    def paper_pdf(identity: str, paper_id: str, request: Request):
        from .research import paper_record
        record = paper_record(service, owner(request), identity, paper_id)
        return Response(object_read(service, record['pdf']), media_type='application/pdf',
                        headers={'Content-Disposition': f'inline; filename="{record["filename"]}"'})

    @app.delete('/api/runs/{identity}/papers/{paper_id}')
    def delete_paper(identity: str, paper_id: str, request: Request):
        from .research import remove_paper
        return remove_paper(service, owner(request), identity, paper_id)

    @app.post('/api/runs/{identity}/papers/{paper_id}/suggest')
    def suggest_reference(identity: str, paper_id: str, payload: ExtractionRequest, request: Request):
        from .knowledge_chat import draft_rules
        return draft_rules(service, owner(request), identity, paper_id, payload.allow_external)

    @app.post('/api/runs/{identity}/papers/{paper_id}/compare')
    def compare_paper(identity: str, paper_id: str, payload: Reference, request: Request):
        from .research import compare_reference
        return compare_reference(service, owner(request), identity, paper_id, payload)

    @app.delete('/api/uploads/{identity}')
    def delete_upload(identity: str, request: Request):
        identity = valid_id(identity)
        record = service.owned('uploads', identity, owner(request))
        related = [r for r in service.store.scan('runs') if r['upload_id'] == identity]
        keys = ['uploads/' + identity, 'control/slot', 'control/dispatch', *['runs/' + r['id'] for r in related]]
        def change(values):
            if set(values['uploads/' + identity].get('run_ids', [])) != {r['id'] for r in related}:
                raise ValueError('Run list changed; retry deletion')
            if (values['control/slot'] or {}).get('owner') in {identity, *[r['id'] for r in related]} or (values['control/dispatch'] or {}).get('id') in {identity, *[r['id'] for r in related]}:
                raise ValueError('Processing must be stopped and recovered before deletion')
            updates = {}
            for key in keys:
                if key.startswith(('uploads/', 'runs/')) and values[key]:
                    updates[key] = {**values[key], 'status': 'deleting'}
            return {'status': 'deleting'}, updates
        result = service.store.atomic(keys, change)
        if local:
            from .runtime import cleanup
            cleanup(service)
        return result

    @app.post('/internal/dispatch')
    def dispatch(request: Request):
        internal(request)
        from .runtime import dispatch_cloud
        return dispatch_cloud(service)

    @app.post('/internal/cleanup')
    def cleanup_endpoint(request: Request):
        internal(request)
        from .runtime import cleanup
        return cleanup(service)

    group_endpoints(app)
    return app


def main():
    import uvicorn
    cloud = os.getenv('SCRNA_MODE') == 'cloud'
    uvicorn.run(create_app(), host='0.0.0.0' if cloud else '127.0.0.1', port=int(os.getenv('PORT', str(DEFAULT_PORT))))


if __name__ == '__main__':
    main()
