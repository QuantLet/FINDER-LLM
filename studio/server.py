"""Loopback HTTP routes. Jobs and API keys live in memory, not files."""

from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import threading
from urllib.parse import urlsplit

from .runner import RunCancelled, run_custom, safe_error, validate_connection, validate_workflow

ASSETS = Path(__file__).parent
MAX_BODY = 1_000_000


def redact(value, secret):
    if not secret:
        return value
    if isinstance(value, str):
        return value.replace(secret, '[hidden]')
    if isinstance(value, dict):
        return {redact(key, secret): redact(item, secret) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item, secret) for item in value]
    return value


class StudioServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port):
        super().__init__(('127.0.0.1', port), StudioHandler)
        self.token = secrets.token_urlsafe(32)
        self.jobs = {}
        self.lock = threading.Lock()

    def start_job(self, config, connection, paper=False):
        with self.lock:
            if any(job['status'] in {'queued', 'running'} for job in self.jobs.values()):
                raise ValueError('A run is already active. Wait for it or stop it before starting another.')
            while len(self.jobs) >= 8:
                self.jobs.pop(next(iter(self.jobs)))
            job_id = secrets.token_hex(12)
            self.jobs[job_id] = {'id': job_id, 'status': 'queued', 'events': [], 'result': None, 'error': None, 'cancel': False, 'paper': paper}

        def progress(event):
            with self.lock:
                job = self.jobs[job_id]
                if paper and job['cancel']:
                    raise RunCancelled()
                job['status'] = 'running'
                job['events'].append(redact(event, connection.get('api_key', '')))
                job['events'] = job['events'][-250:]

        def cancelled():
            with self.lock:
                return self.jobs[job_id]['cancel']

        def work():
            result, error, status = None, None, 'completed'
            try:
                with self.lock:
                    self.jobs[job_id]['status'] = 'running'
                if paper:
                    from .paper import run_paper
                    result = run_paper(config, connection, progress)
                else:
                    result = run_custom(config, connection, progress, cancelled)
            except RunCancelled:
                status = 'cancelled'
            except Exception as exc:
                if paper and cancelled():
                    status = 'cancelled'
                else:
                    status, error = 'failed', safe_error(exc)
            with self.lock:
                self.jobs[job_id].update(status=status, result=redact(result, connection.get('api_key', '')), error=redact(error, connection.get('api_key', '')))
            # Credentials are scoped to this job and released when it ends.
            connection.clear()

        threading.Thread(target=work, daemon=True).start()
        return job_id


class StudioHandler(BaseHTTPRequestHandler):
    server_version = 'FINDERStudio'

    def log_message(self, format, *args):
        pass

    def handle_one_request(self):
        self.connection.settimeout(10)
        try:
            super().handle_one_request()
        except (ConnectionError, TimeoutError):
            pass

    def send(self, status, payload, content_type='application/json; charset=utf-8'):
        body = json.dumps(payload, ensure_ascii=False).encode() if not isinstance(payload, bytes) else payload
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(body)

    def allowed(self, authenticated=False):
        port = self.server.server_port
        host = self.headers.get('Host', '')
        if host not in {f'127.0.0.1:{port}', f'localhost:{port}'}:
            self.send(403, {'error': 'Open this app using its local address.'})
            return False
        origin = self.headers.get('Origin')
        if origin and origin != f'http://{host}':
            self.send(403, {'error': 'Requests must come from this app.'})
            return False
        if self.headers.get('Sec-Fetch-Site') == 'cross-site':
            self.send(403, {'error': 'Cross-site requests are blocked.'})
            return False
        if authenticated and not secrets.compare_digest(self.headers.get('X-Studio-Token', ''), self.server.token):
            self.send(403, {'error': 'Reload this page before continuing.'})
            return False
        return True

    def do_GET(self):
        path = urlsplit(self.path).path
        if not self.allowed(authenticated=path.startswith('/api/')):
            return
        if path in {'/', '/index.html'}:
            page = (ASSETS / 'index.html').read_text().replace('__STUDIO_TOKEN__', self.server.token)
            self.send(200, page.encode(), 'text/html; charset=utf-8')
        elif path in {'/app.js', '/style.css'}:
            self.send(200, (ASSETS / path[1:]).read_bytes(), 'text/javascript; charset=utf-8' if path.endswith('.js') else 'text/css; charset=utf-8')
        elif path == '/api/paper-config':
            try:
                from .paper import get_paper_config
                self.send(200, get_paper_config())
            except Exception as exc:
                self.send(400, {'error': safe_error(exc)})
        elif path.startswith('/api/jobs/'):
            with self.server.lock:
                job = deepcopy(self.server.jobs.get(path.removeprefix('/api/jobs/')))
            self.send(200, job) if job else self.send(404, {'error': 'This run is no longer in memory. Start a new run.'})
        else:
            self.send(404, {'error': 'Page not found.'})

    def do_POST(self):
        if not self.allowed(authenticated=True):
            return
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= MAX_BODY or self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                raise ValueError('Send a JSON request smaller than 1 MB.')
            body = json.loads(self.rfile.read(size))
            if not isinstance(body, dict):
                raise ValueError('The request must be a JSON object.')
            path = urlsplit(self.path).path
            if path in {'/api/run', '/api/paper-run'}:
                connection = validate_connection(body.get('connection'))
                paper = path == '/api/paper-run'
                if paper and connection['mode'] != 'live':
                    raise ValueError('Paper execution needs a live API and prepared local data. Use Custom workflow for a free demo.')
                config = body.get('config', {}) if paper else validate_workflow(body.get('config'))
                if not isinstance(config, dict):
                    raise ValueError('The settings must be a JSON object.')
                job_id = self.server.start_job(config, connection, paper)
                self.send(202, {'id': job_id})
            elif path == '/api/cancel':
                with self.server.lock:
                    job = self.server.jobs.get(body.get('id'))
                    if not job:
                        raise ValueError('This run was not found.')
                    job['cancel'] = True
                self.send(200, {'status': 'stopping'})
            else:
                self.send(404, {'error': 'Route not found.'})
        except (ValueError, TypeError, RecursionError):
            # Validation messages are local; malformed JSON does not echo the payload.
            import sys
            exc = sys.exc_info()[1]
            message = str(exc) if type(exc) is ValueError else 'The request is not valid JSON or has invalid fields.'
            self.send(400, {'error': message})
        except Exception:
            self.send(500, {'error': 'The request could not be completed.'})


def serve(port=8765):
    if not 1 <= port <= 65535:
        raise ValueError('Port must be 1–65535.')
    server = StudioServer(port)
    print(f'Open http://127.0.0.1:{server.server_port} in your browser. Press Ctrl+C to stop.')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
