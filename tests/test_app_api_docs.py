import hashlib
import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from pbmc_platform.app.blobs import LocalBlobs
from pbmc_platform.app.service import Service
from pbmc_platform.app.store import LocalStore
from pbmc_platform.app import web


@pytest.fixture
def service(tmp_path):
    return Service(LocalStore(tmp_path/'state.sqlite'),LocalBlobs(tmp_path/'objects'))


def test_swagger_page_schema_assets_and_csrf(service):
    with TestClient(web.create_app(service,local=True)) as client:
        page=client.get('/docs')
        assert page.status_code==200 and 'swagger-ui' in page.text
        assert client.cookies.get('csrf')
        assert "style-src 'self' 'unsafe-inline'" in page.headers['content-security-policy']
        assert "script-src 'self'" in page.headers['content-security-policy']
        schema=client.get('/openapi.json').json()
        assert schema['openapi'].startswith('3.1')
        assert '/api/runs' in schema['paths'] and '/api/runs/{identity}/chat' in schema['paths']
        assert '/docs' not in schema['paths'] and '/openapi.json' not in schema['paths']
        assert schema['paths']['/api/runs']['get']['tags']==['Processing and history']
        paper=schema['paths']['/api/runs/{identity}/papers']['post']
        assert paper['requestBody']['content']['application/pdf']['schema']['format']=='binary'
        upload=schema['paths']['/api/uploads/{identity}/content']['put']
        assert upload['requestBody']['content']['application/octet-stream']['schema']['format']=='binary'
        assert client.get('/api-docs-assets/swagger-ui-bundle.js').status_code==200
        assert client.get('/api-docs-assets/swagger-ui.css').headers['content-type'].startswith('text/css')
        assert client.get('/api-docs-assets/not-a-library.js').status_code==404
        assert 'requestInterceptor' in client.get('/static/api-docs.js').text
        assert client.post('/api/uploads',json={}).status_code==403
        main=client.get('/')
        assert "style-src 'self';" in main.headers['content-security-policy']
        assert 'unsafe-inline' not in main.headers['content-security-policy']


def test_swagger_cloud_access_requires_invited_session(service,monkeypatch):
    monkeypatch.setenv('SCRNA_ALLOWED_EMAILS','invited@example.com')
    monkeypatch.setenv('SCRNA_ORIGIN','https://app.example.com')
    class Auth:
        def verify_session_cookie(self,cookie,check_revoked):
            if cookie=='invited':return {'uid':'invite','email':'invited@example.com','email_verified':True}
            raise ValueError('Not invited')
    monkeypatch.setattr(web,'firebase',lambda:Auth())
    with TestClient(web.create_app(service,local=False),base_url='https://app.example.com') as client:
        for path in ['/docs','/openapi.json','/api-docs-assets/swagger-ui-bundle.js']:
            assert client.get(path).status_code==401
        client.cookies.set('session','invited')
        assert client.get('/docs').status_code==200
        assert client.get('/openapi.json').status_code==200


def test_official_swagger_assets_match_recorded_hashes():
    root=Path(web.__file__).parent/'static'/'swagger'
    manifest=json.loads((root/'version.json').read_text())
    assert manifest['version']=='5.34.1'
    for name,expected in manifest['files_sha256'].items():
        assert hashlib.sha256((root/name).read_bytes()).hexdigest()==expected
