"""The configured public URL: its browser origin, optional application path and the matching middleware."""

import hashlib
import re
from urllib.parse import SplitResult, urlsplit

from starlette.responses import RedirectResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


def base_path(public_url: str) -> str:
    if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in public_url):
        raise ValueError('Public URLs cannot contain whitespace or control characters.')
    path = urlsplit(public_url).path.removesuffix('/')
    # A literal, canonical path is safe in HTML, cookies, proxy configs and URLs.
    if path and not re.fullmatch(r'(?:/[A-Za-z0-9_-]+)+', path):
        raise ValueError('Use a public URL path containing only letters, digits, underscores and hyphens.')
    return path


def origin(public_url: str) -> str:
    parsed = urlsplit(public_url)
    return parsed._replace(path='', query='', fragment='').geturl()


def app_path(public_url: str, path: str = '/') -> str:
    """Prefix an application-owned absolute path (never an arbitrary redirect)."""
    if not path.startswith('/') or path.startswith('//'):
        raise ValueError('Expected an application path.')
    return base_path(public_url) + path


def cookie_name(name: str, public_url: str) -> str:
    """Avoid ambiguity when a browser also sends a root deployment's cookie."""
    path = base_path(public_url)
    return name + '_' + hashlib.sha256(path.encode()).hexdigest()[:16] if path else name


class PublicPathMiddleware:
    def __init__(self, app: ASGIApp, public_url: str):
        self.app, self.prefix = app, base_path(public_url)
        self.origin = origin(public_url)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self.prefix and scope['type'] in {'http', 'websocket'}:
            path = scope['path']
            if path == self.prefix and scope['type'] == 'http':
                query = scope.get('query_string', b'').decode('latin-1')
                target = self.prefix + '/' + ('?' + query if query else '')
                await RedirectResponse(target, status_code=307)(scope, receive, send)
                return
            scope = dict(scope)
            if path.startswith(self.prefix + '/'):
                scope['path'] = path[len(self.prefix):]
                if raw := scope.get('raw_path'):
                    scope['raw_path'] = raw[len(self.prefix.encode()):]
            # Health checks retain their unprefixed paths.
            # Ingress must forward only this deployment's configured path.
            # Keep the internal path and mount root in the same coordinate system.
            # StaticFiles appends /assets to root_path when matching its mount.
            scope['root_path'] = ''
        async def external_redirect(message: Message) -> None:
            if self.prefix and message['type'] == 'http.response.start':
                headers = []
                for name, value in message.get('headers', []):
                    if name.lower() == b'location':
                        location = value.decode('latin-1')
                        parsed: SplitResult = urlsplit(location)
                        local = not parsed.netloc and location.startswith('/') and not location.startswith('//')
                        if local or origin(location) == self.origin:
                            path = parsed.path
                            if path != self.prefix and not path.startswith(self.prefix + '/'):
                                value = parsed._replace(path=self.prefix + path).geturl().encode('latin-1')
                    headers.append((name, value))
                message = {**message, 'headers': headers}
            await send(message)
        await self.app(scope, receive, external_redirect)
