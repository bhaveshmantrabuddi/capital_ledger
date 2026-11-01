"""Loopback-only demo server. AWS never enables its development login or controls."""
import argparse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import mimetypes
import os
from pathlib import Path
import secrets
import threading
import time
from urllib.parse import unquote, urlsplit
from .api import dispatch
from .database import Database
from .engine import Ledger, LedgerError
from .projection import LocalProjection

LOG = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[2]


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, service, frontend):
        super().__init__(address, Handler)
        self.service, self.frontend = service, Path(frontend).resolve()
        self.tokens = {}
        self.token_lock = threading.Lock()
        self.stop_worker = threading.Event()


class Handler(BaseHTTPRequestHandler):
    server_version = 'CapitalLedgerLocal/1.0'

    def log_message(self, fmt, *args):
        LOG.info('%s %s %s', self.command, urlsplit(self.path).path, args[1] if len(args) > 1 else '')

    def _allowed_origin(self):
        origin = self.headers.get('Origin')
        port = self.server.server_port
        if origin and origin not in (f'http://127.0.0.1:{port}', f'http://localhost:{port}'):
            raise LedgerError('origin_forbidden', 'Origin is not allowed.', 403)
        return origin

    def _json(self, status, body):
        data = json.dumps(body, allow_nan=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        origin = self.headers.get('Origin')
        port = self.server.server_port
        if origin in (f'http://127.0.0.1:{port}', f'http://localhost:{port}'):
            self.send_header('Access-Control-Allow-Origin', origin)
            self.send_header('Vary', 'Origin')
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        raw_length = self.headers.get('Content-Length', '0')
        try:
            length = int(raw_length)
        except ValueError:
            raise LedgerError('invalid_request', 'Invalid Content-Length.')
        if length < 0 or length > 65536:
            raise LedgerError('payload_too_large', 'Request body is too large.', 413)
        if not length:
            return {}
        if self.headers.get_content_type() != 'application/json':
            raise LedgerError('invalid_request', 'Use application/json.')
        try:
            value = json.loads(self.rfile.read(length), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except (ValueError, UnicodeError):
            raise LedgerError('invalid_json', 'Request body must be a JSON object.')

    def _owner(self):
        auth = self.headers.get('Authorization', '')
        if not auth.startswith('Bearer '):
            raise LedgerError('unauthorized', 'Sign in to continue.', 401)
        with self.server.token_lock:
            session = self.server.tokens.get(auth[7:])
        if not session or session['expires'] < time.time():
            raise LedgerError('unauthorized', 'Invalid or expired session.', 401)
        return session['owner']

    def _handle(self):
        try:
            self._allowed_origin()
            url = urlsplit(self.path)
            if self.command == 'OPTIONS':
                self.send_response(204)
                origin = self._allowed_origin()
                if origin:
                    self.send_header('Access-Control-Allow-Origin', origin)
                self.send_header('Access-Control-Allow-Methods', 'GET,POST,OPTIONS')
                self.send_header('Access-Control-Allow-Headers', 'Authorization,Content-Type,Idempotency-Key')
                self.end_headers()
                return
            if self.command == 'GET' and url.path == '/api/health':
                return self._json(200, {'mode': 'local', 'auth_mode': 'local',
                    'database': self.server.service.db.backend, 'simulation': True,
                    'worker_paused': self.server.service.worker_paused})
            if not url.path.startswith('/api/'):
                return self._static(url.path)
            if url.path == '/api/dev/login' and self.command == 'POST':
                body = self._body()
                user = body.get('user')
                if user not in ('alice', 'bob'):
                    raise LedgerError('invalid_user', 'Choose alice or bob.')
                token = secrets.token_urlsafe(32)
                with self.server.token_lock:
                    self.server.tokens[token] = {'owner': user, 'expires': time.time() + 8 * 3600}
                return self._json(200, {'access_token': token,
                    'user': {'id': user, 'name': user.title()}, 'expires_in': 8 * 3600})
            owner = self._owner()
            body = self._body() if self.command == 'POST' else {}
            if self.command == 'GET' and url.path == '/api/reconciliation':
                return self._json(200, dict(self.server.service.reconcile(owner),
                    worker_paused=self.server.service.worker_paused))
            if self.command == 'POST' and url.path == '/api/dev/worker':
                if type(body.get('paused')) is not bool:
                    raise LedgerError('invalid_input', 'paused must be a boolean.')
                self.server.service.worker_paused = body['paused']
                return self._json(200, {'paused': body['paused']})
            if self.command == 'POST' and url.path == '/api/dev/sync':
                return self._json(200, self.server.service.drain_outbox(limit=500))
            status, result = dispatch(self.server.service, self.command, url.path,
                owner, body, self.headers.get('Idempotency-Key'), url.query)
            return self._json(status, result)
        except LedgerError as exc:
            self._json(exc.status, {'error': {'code': exc.code, 'message': exc.message}})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            LOG.exception('request_failed')
            self._json(500, {'error': {'code': 'internal_error',
                'message': 'The request could not be completed. Retry mutations with the same key.'}})

    def _static(self, path):
        if self.command != 'GET':
            raise LedgerError('not_found', 'Not found.', 404)
        relative = unquote(path).lstrip('/') or 'index.html'
        target = (self.server.frontend / relative).resolve()
        if not target.is_relative_to(self.server.frontend) or not target.is_file():
            raise LedgerError('not_found', 'Not found.', 404)
        content = target.read_bytes()
        self.send_response(200)
        self.send_header('Content-Type', mimetypes.guess_type(str(target))[0] or 'application/octet-stream')
        self.send_header('Content-Length', str(len(content)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'same-origin')
        self.end_headers()
        self.wfile.write(content)

    do_GET = do_POST = do_OPTIONS = _handle


def seed(service):
    for user, name, balance in (('alice','Everyday checking',125000), ('bob','Everyday checking',50000)):
        service.create_account(user, f'seed-{user}-checking-v1',
            {'name': name, 'full_name': f'{user.title()} Example',
             'date_of_birth': '2004-01-15', 'initial_balance_cents': balance})


def main():
    parser = argparse.ArgumentParser(description='Start a loopback-only simulated banking demo.')
    parser.add_argument('--host', default='127.0.0.1', choices=['127.0.0.1','localhost'])
    parser.add_argument('--port', type=int, default=8787)
    parser.add_argument('--database', default=str(ROOT / '.local' / 'ledger.sqlite3'))
    # Local mode never inherits a potentially hosted/paid DATABASE_URL.
    # PostgreSQL must be selected explicitly by a developer with --postgres.
    parser.add_argument('--postgres', default=None)
    parser.add_argument('--frontend', default=str(ROOT / 'frontend'))
    parser.add_argument('--no-seed', action='store_true')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    if args.postgres:
        db = Database('postgres', args.postgres)
    else:
        Path(args.database).resolve().parent.mkdir(parents=True, exist_ok=True)
        db = Database('sqlite', args.database)
    db.bootstrap()
    service = Ledger(db, LocalProjection(db))
    if not args.no_seed:
        seed(service)
    server = Server((args.host, args.port), service, args.frontend)

    def worker():
        while not server.stop_worker.wait(2):
            try:
                service.drain_outbox()
            except Exception:
                LOG.exception('background_worker_failed')
    threading.Thread(target=worker, daemon=True).start()
    print(f'Capital Ledger local simulator: http://{args.host}:{server.server_port}', flush=True)
    print(f'Database: {db.backend}. Local demo identities: Alice and Bob.', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop_worker.set()
        server.server_close()


if __name__ == '__main__':
    main()
