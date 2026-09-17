"""Minimal synchronous harness for this project's JSON API; no hidden httpx dependency.

This exercises ASGI routes, not sockets, a browser proxy, or an external data source.
Unexpected application exceptions still fail the test instead of being hidden.
"""
import asyncio
from dataclasses import dataclass
import json
from urllib.parse import parse_qsl, quote, urlencode, urlsplit


@dataclass
class ASGIResponse:
    status_code: int
    content: bytes

    def json(self):
        return json.loads(self.content)


class ASGITestClient:
    def __init__(self, app):
        self.app = app
        self.loop = None
        self.lifespan = None

    def __enter__(self):
        self.loop = asyncio.new_event_loop()
        self.lifespan = self.app.router.lifespan_context(self.app)
        self.loop.run_until_complete(self.lifespan.__aenter__())
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            self.loop.run_until_complete(self.lifespan.__aexit__(exc_type, exc, tb))
            self.loop.run_until_complete(self.loop.shutdown_asyncgens())
        finally:
            self.loop.close()

    def request(self, method, url, *, params=None, json_body=None):
        if self.loop is None:
            raise RuntimeError('Use ASGITestClient in a context manager')
        parts = urlsplit(url)
        query = parse_qsl(parts.query, keep_blank_values=True)
        if params:
            query.extend(params.items())
        messages = []
        scope = {'type': 'http', 'asgi': {'version': '3.0', 'spec_version': '2.3'},
                 'http_version': '1.1', 'method': method, 'scheme': 'http',
                 'path': parts.path, 'raw_path': quote(parts.path).encode('ascii'),
                 'query_string': urlencode(query).encode('ascii'), 'root_path': '',
                 'headers': [(b'host', b'testserver')], 'client': ('127.0.0.1', 1234),
                 'server': ('testserver', 80)}

        request_body = b'' if json_body is None else json.dumps(json_body, allow_nan=False).encode('utf-8')
        if json_body is not None:
            scope['headers'].append((b'content-type', b'application/json'))

        async def receive():
            return {'type': 'http.request', 'body': request_body, 'more_body': False}

        async def send(message):
            messages.append(message)

        self.loop.run_until_complete(self.app(scope, receive, send))
        status = next(message['status'] for message in messages if message['type'] == 'http.response.start')
        body = b''.join(message.get('body', b'') for message in messages if message['type'] == 'http.response.body')
        return ASGIResponse(status, body)

    def get(self, url, *, params=None):
        return self.request('GET', url, params=params)

    def post(self, url, *, params=None, json_body=None):
        return self.request('POST', url, params=params, json_body=json_body)
