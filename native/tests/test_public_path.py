"""Path routing exercises the real API; these are supplementary to local E2E."""
import re

import httpx
import pytest
from starlette.responses import Response

from modelmesh.api import create_app
from modelmesh.public_path import app_path, base_path


@pytest.mark.parametrize('prefix', ['', '/modelmesh', '/tools/modelmesh'])
async def test_assets_login_mutations_download_and_logout_under_prefix(runtime, prefix, tmp_path, monkeypatch):
    # Native-only CI does not build Vite. Exercise the HTML/static serving
    # contract here; the actual built frontend is covered by browser/local E2E.
    static = tmp_path / 'static'
    (static / 'assets').mkdir(parents=True)
    (static / 'index.html').write_text('<html><head><!--modelmesh-base-->'
                                     '<script src="./assets/proof.js"></script></head></html>')
    (static / 'assets/proof.js').write_text('/* static serving proof */')
    monkeypatch.setattr('modelmesh.api.STATIC', static)
    runtime.settings.public_url = 'http://testserver' + prefix
    app = create_app(runtime, run_dispatcher=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
        if prefix:
            response = await client.get(prefix + '?tab=1')
            assert response.status_code == 307
            assert response.headers['location'] == prefix + '/?tab=1'
        page = await client.get(prefix + '/')
        assert f'<base href="{prefix}/">' in page.text
        assets = re.findall(r'(?:src|href)="\./(assets/[^\"]+)"', page.text)
        assert assets
        for asset in assets:
            result = await client.get(prefix + '/' + asset)
            assert result.status_code == 200, asset
            assert 'text/html' not in result.headers['content-type']
        denied = await client.post(prefix + '/api/login', headers={'Origin': 'https://other.test'}, json={
            'username': 'admin', 'password': 'test-password-12345',
        })
        assert denied.status_code == 403
        if prefix:
            # A sibling/root ModelMesh deployment's cookie must not shadow this login.
            client.cookies.set('modelmesh_session', 'unrelated-root-cookie', path='/')
        client.headers['Origin'] = 'http://testserver'
        login = await client.post(prefix + '/api/login', json={'username': 'admin', 'password': 'test-password-12345'})
        assert login.status_code == 200
        assert f'Path={prefix or "/"};' in login.headers['set-cookie']
        client.headers['X-ModelMesh-CSRF'] = login.json()['csrf']
        assert (await client.get(prefix + '/api/me')).status_code == 200
        session = await client.post(prefix + '/api/sessions', json={'title': 'Path proof'})
        assert session.status_code < 300
        upload = await client.post(prefix + '/api/uploads?name=proof.txt', content=b'path proof',
                                   headers={'Content-Type': 'text/plain'})
        assert upload.status_code < 300, upload.text
        downloaded = await client.get(prefix + '/api/uploads/' + upload.json()['id'])
        assert downloaded.content == b'path proof'
        logout = await client.post(prefix + '/api/logout')
        assert logout.status_code == 200
        assert f'Path={prefix or "/"};' in logout.headers['set-cookie']
        assert (await client.get(prefix + '/api/me')).status_code == 401
        assert (await client.get('/health')).status_code == 200


@pytest.mark.parametrize('path', ['/modelmesh', '/tools/modelmesh', ''])
def test_application_paths_keep_the_configured_base(path):
    assert app_path('https://gateway.example.com' + path, '/api/me') == path + '/api/me'


@pytest.mark.parametrize('path', ['/../x', '/%2fadmin', '/modelmesh//x', '/modelmesh//', '/modelmesh\n', '/modelmesh;x', '/modelmesh\\x'])
def test_ambiguous_paths_fail_closed_without_echoing_configuration(path):
    with pytest.raises(ValueError) as failure:
        base_path('https://example.test' + path)
    assert 'example.test' not in str(failure.value)


async def test_role_and_stream_paths_are_normalized_before_authentication():
    from modelmesh.public_path import PublicPathMiddleware
    seen = []

    async def downstream(scope, receive, send):
        seen.append((scope['type'], scope['path'], scope['raw_path'], scope['root_path']))
        await send({'type': 'websocket.close', 'code': 1008} if scope['type'] == 'websocket' else {
            'type': 'http.response.start', 'status': 401, 'headers': [],
        })

    async def receive():
        return {'type': 'http.request', 'body': b''}

    async def send(message):
        pass

    app = PublicPathMiddleware(downstream, 'https://gateway.test/modelmesh')
    for kind, path in [('http', '/api/me'), ('websocket', '/api/sessions/session/stream'),
                       ('http', '/health')]:
        external = '/modelmesh' + path if path != '/health' else path
        await app({'type': kind, 'path': external, 'raw_path': external.encode()}, receive, send)
        assert seen[-1] == (kind, path, path.encode(), '')


async def test_redirects_preserve_prefix_query_and_provider_endpoints():
    from modelmesh.public_path import PublicPathMiddleware
    for location, expected in [
        ('/api/sessions/', '/modelmesh/api/sessions/'),
        ('/?connection=success#connections', '/modelmesh/?connection=success#connections'),
        ('/modelmesh/#tasks', '/modelmesh/#tasks'),
        ('https://gateway.test/auth/callback', 'https://gateway.test/modelmesh/auth/callback'),
        ('https://accounts.google.com/o/oauth2/v2/auth', 'https://accounts.google.com/o/oauth2/v2/auth'),
    ]:
        sent = []

        async def downstream(scope, receive, send, target=location):
            await Response(status_code=303, headers={'Location': target})(scope, receive, send)

        async def receive():
            return {'type': 'http.request', 'body': b''}

        async def send(message, messages=sent):
            messages.append(message)

        app = PublicPathMiddleware(downstream, 'https://gateway.test/modelmesh')
        await app({'type': 'http', 'path': '/modelmesh/', 'raw_path': b'/modelmesh/'}, receive, send)
        assert dict(sent[0]['headers'])[b'location'].decode() == expected
