"""Camera Attendance AI platform: HTTP API + web panel (stdlib only).

Run behind an HTTPS reverse proxy for anything except a local test.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import hmac
import json
import logging
import os
import re
import socket
import sys
import threading
import time
import traceback
from collections import deque
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import imports
from core import Forbidden, InputError, NotFound, Store, local_today, TZ_NAME

ROOT = Path(__file__).resolve().parent
MAX_BODY = 64 * 1024
MAX_UPLOAD_BODY = 5 * 1024 * 1024
MAX_IMPORT_BODY = 8 * 1024 * 1024
SESSION_COOKIE = 'cat_sid'
LOG = logging.getLogger('attendance')

STATIC = {'/': 'index.html', '/index.html': 'index.html', '/app.js': 'app.js', '/features.js': 'features.js',
          '/style.css': 'style.css'}
CONTENT_TYPES = {'html': 'text/html; charset=utf-8', 'js': 'text/javascript; charset=utf-8', 'css': 'text/css; charset=utf-8'}
PAGE_CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; "
            "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


class LoginThrottle:
    """In-memory per-IP limiter for failed logins (per-user lockout lives in the DB)."""

    def __init__(self, limit=20, window=900):
        self.limit, self.window = limit, window
        self.failures: dict[str, deque] = {}
        self.lock = threading.Lock()

    def blocked(self, ip):
        with self.lock:
            q = self.failures.get(ip)
            now = time.monotonic()
            while q and now - q[0] > self.window:
                q.popleft()
            return bool(q) and len(q) >= self.limit

    def fail(self, ip):
        with self.lock:
            self.failures.setdefault(ip, deque()).append(time.monotonic())
            if len(self.failures) > 10000:
                self.failures.clear()


class ApiServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64
    # On Windows SO_REUSEADDR lets another process bind the SAME port and hijack traffic.
    allow_reuse_address = os.name != 'nt'

    def server_bind(self):
        if os.name == 'nt' and hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def __init__(self, address, store: Store, secure_cookies=False, trust_proxy=False):
        self.store = store
        self.secure_cookies = secure_cookies
        self.trust_proxy = trust_proxy
        self.throttle = LoginThrottle()
        self.form_throttle = LoginThrottle(limit=5, window=3600)
        super().__init__(address, Handler)


ROUTES = []


def route(method, pattern, auth, limit=None, raw=False):
    """auth: public | session | super | staff | admin | token.
    staff/admin routes start with /api/s/<sid>/ and are restricted to that school.
    limit: max request body; raw=True passes the body as bytes (file uploads) instead of JSON."""
    regex = re.compile('^' + pattern + '$')

    def wrap(fn):
        ROUTES.append((method, regex, auth, fn, {'limit': limit, 'raw': raw}))
        return fn
    return wrap


class Handler(BaseHTTPRequestHandler):
    server_version = 'CameraAttendance/2.0'
    sys_version = ''
    timeout = 30  # drop idle/slow clients; HTTP/1.0 => one request per connection

    # ------------------------------------------------------------- plumbing
    def log_message(self, fmt, *args):
        # Request lines only; tokens travel in headers/cookies and are never logged.
        LOG.info('%s %s', self.client_ip(), fmt % args)

    def client_ip(self):
        if self.server.trust_proxy:
            forwarded = self.headers.get('X-Forwarded-For', '')
            if forwarded:
                return forwarded.split(',')[-1].strip()[:64]
        return self.client_address[0]

    def common_headers(self, csp="default-src 'none'; frame-ancestors 'none'"):
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Cross-Origin-Opener-Policy', 'same-origin')
        self.send_header('Cross-Origin-Resource-Policy', 'same-origin')
        self.send_header('Permissions-Policy', 'camera=(), microphone=(), geolocation=()')
        self.send_header('Content-Security-Policy', csp)
        if self.server.secure_cookies:
            self.send_header('Strict-Transport-Security', 'max-age=31536000')

    def respond(self, status, data, headers=None):
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.common_headers()
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def send_bytes(self, data, content_type, csp=PAGE_CSP, cache='no-store'):
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', cache)
        self.common_headers(csp)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(data)

    def cookie_token(self):
        raw = self.headers.get('Cookie')
        if not raw:
            return None
        try:
            cookie = SimpleCookie()
            cookie.load(raw)
        except Exception:
            return None
        morsel = cookie.get(SESSION_COOKIE)
        return morsel.value if morsel else None

    def session_cookie(self, token, max_age):
        parts = [f'{SESSION_COOKIE}={token}', 'Path=/', 'HttpOnly', 'SameSite=Strict', f'Max-Age={max_age}']
        if self.server.secure_cookies:
            parts.append('Secure')
        return '; '.join(parts)

    def read_body(self, limit, raw=False):
        if self.headers.get('Transfer-Encoding'):
            raise InputError('Chunked bodies are not supported')
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            raise InputError('Invalid Content-Length') from None
        if length < 0 or length > limit:
            raise InputError('Тело запроса слишком большое')
        if raw:
            data = bytearray()
            while len(data) < length:
                chunk = self.rfile.read(min(1 << 20, length - len(data)))
                if not chunk:
                    raise InputError('Загрузка прервана')
                data.extend(chunk)
            return bytes(data)
        if length == 0:
            return {}
        if self.headers.get('Content-Type', '').split(';')[0].strip().lower() != 'application/json':
            raise InputError('Content-Type must be application/json')
        try:
            obj = json.loads(self.rfile.read(length))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise InputError('Invalid JSON') from None
        if not isinstance(obj, dict):
            raise InputError('Expected a JSON object')
        return obj

    def query(self, name, default=None):
        return parse_qs(urlsplit(self.path).query).get(name, [default])[0]

    def query_int(self, name):
        value = self.query(name)
        if value in (None, ''):
            return None
        if not value.isdigit() or len(value) > 9:
            raise InputError(f'{name} must be a positive integer')
        return int(value)

    # ------------------------------------------------------------- dispatch
    def do_GET(self):
        self.dispatch('GET')

    def do_HEAD(self):
        self.dispatch('GET')

    def do_POST(self):
        self.dispatch('POST')

    def do_PUT(self):
        self.dispatch('PUT')

    def do_DELETE(self):
        self.dispatch('DELETE')

    def dispatch(self, method):
        path = urlsplit(self.path).path
        try:
            if method == 'GET' and path in STATIC:
                return self.static(STATIC[path])
            if not path.startswith('/api/'):
                return self.respond(404, {'error': 'Not found'})
            allowed = False
            for m, regex, auth, fn, opts in ROUTES:
                match = regex.match(path)
                if not match:
                    continue
                if m != method:
                    allowed = True
                    continue
                args = [int(g) for g in match.groups()]
                if not self.authorize(auth, method, args):
                    return
                limit = opts['limit'] or (MAX_UPLOAD_BODY if path.endswith('/photos') and method == 'POST' else MAX_BODY)
                self.body = self.read_body(limit, opts['raw']) if method in ('POST', 'PUT') else {}
                result = fn(self, *args)
                if result is not None:
                    self.respond(200, result)
                return
            return self.respond(405 if allowed else 404, {'error': 'Method not allowed' if allowed else 'Not found'})
        except (NotFound, Forbidden, InputError, imports.ImportError_) as exc:
            return self.respond(getattr(exc, 'status', 400), {'error': str(exc)})
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception:
            LOG.error('Unhandled error on %s %s\n%s', method, path, traceback.format_exc())
            try:
                return self.respond(500, {'error': 'Внутренняя ошибка сервера'})
            except OSError:
                return

    def authorize(self, auth, method, args):
        self.user = None
        self.token_sid = None
        if auth == 'public':
            return True
        if auth == 'token':
            sid = self.server.store.token_school(self.headers.get('X-Ingest-Token', ''))
            if sid is None:
                self.respond(401, {'error': 'Invalid device token'})
                return False
            self.token_sid = sid
            return True
        user = self.server.store.session_user(self.cookie_token())
        if user is None:
            self.respond(401, {'error': 'Требуется вход в систему'})
            return False
        if method in ('POST', 'PUT', 'DELETE'):
            supplied = self.headers.get('X-CSRF-Token', '')
            if not supplied or not hmac.compare_digest(supplied, user['csrf']):
                self.respond(403, {'error': 'CSRF token mismatch; обновите страницу'})
                return False
        self.user = user
        role = user['role']
        if auth == 'session':
            return True
        if auth == 'super':
            if role != 'superadmin':
                self.respond(403, {'error': 'Только для супер-администратора'})
                return False
            return True
        # staff/admin: first path arg is the school id
        sid = args[0]
        if role != 'superadmin' and user['school_id'] != sid:
            self.respond(404, {'error': 'Школа не найдена'})
            return False
        if auth == 'admin' and role not in ('superadmin', 'school_admin'):
            self.respond(403, {'error': 'Недостаточно прав'})
            return False
        return True

    def actor(self):
        return self.user

    def static(self, name):
        path = ROOT / 'web' / name
        self.send_bytes(path.read_bytes(), CONTENT_TYPES[path.suffix[1:]])


# ============================================================== auth & profile
@route('POST', r'/api/login', 'public')
def login(h: Handler):
    ip = h.client_ip()
    if h.server.throttle.blocked(ip):
        return h.respond(429, {'error': 'Слишком много попыток входа. Подождите 15 минут.'})
    try:
        user = h.server.store.authenticate(h.body.get('username'), h.body.get('password'), ip)
    except InputError as exc:
        h.server.throttle.fail(ip)
        return h.respond(401, {'error': str(exc)})
    token, csrf = h.server.store.create_session(user['id'], ip)
    h.respond(200, {'user': _me(user), 'csrf': csrf},
              {'Set-Cookie': h.session_cookie(token, 12 * 3600)})


@route('POST', r'/api/logout', 'session')
def logout(h: Handler):
    h.server.store.delete_session(h.cookie_token())
    h.respond(200, {'ok': True}, {'Set-Cookie': h.session_cookie('', 0)})


def _me(user):
    return {k: user.get(k) for k in ('id', 'username', 'full_name', 'role', 'school_id', 'school_name')}


@route('GET', r'/api/me', 'public')
def me(h: Handler):
    user = h.server.store.session_user(h.cookie_token())
    if user is None:
        return {'user': None}
    return {'user': _me(user), 'csrf': user['csrf'], 'timezone': TZ_NAME, 'today': local_today()}


@route('POST', r'/api/me/password', 'session')
def change_password(h: Handler):
    h.server.store.change_own_password(h.user['id'], h.body.get('old_password'), h.body.get('new_password'))
    h.respond(200, {'ok': True, 'relogin': True}, {'Set-Cookie': h.session_cookie('', 0)})


# ============================================================== schools & users
@route('GET', r'/api/schools', 'session')
def schools(h: Handler):
    if h.user['role'] == 'superadmin':
        return {'schools': h.server.store.list_schools()}
    return {'schools': h.server.store.list_schools(only_id=h.user['school_id'])}


@route('POST', r'/api/schools', 'super')
def create_school(h: Handler):
    return h.server.store.create_school(h.body, h.actor())


@route('PUT', r'/api/s/(\d{1,9})', 'admin')
def update_school(h: Handler, sid):
    return h.server.store.update_school(sid, h.body, h.actor())


@route('DELETE', r'/api/s/(\d{1,9})', 'super')
def delete_school(h: Handler, sid):
    return h.server.store.delete_school(sid, h.actor())


@route('GET', r'/api/users', 'session')
def users(h: Handler):
    role = h.user['role']
    if role == 'superadmin':
        return {'users': h.server.store.list_users(h.query_int('school_id'))}
    if role == 'school_admin':
        return {'users': h.server.store.list_users(h.user['school_id'])}
    raise Forbidden('Недостаточно прав')


@route('POST', r'/api/users', 'session')
def create_user(h: Handler):
    if h.user['role'] == 'teacher':
        raise Forbidden('Недостаточно прав')
    return h.server.store.create_user(h.body, h.actor())


@route('PUT', r'/api/users/(\d{1,9})', 'session')
def update_user(h: Handler, uid):
    if h.user['role'] == 'teacher':
        raise Forbidden('Недостаточно прав')
    return h.server.store.update_user(uid, h.body, h.actor())


@route('DELETE', r'/api/users/(\d{1,9})', 'session')
def delete_user(h: Handler, uid):
    if h.user['role'] == 'teacher':
        raise Forbidden('Недостаточно прав')
    return h.server.store.delete_user(uid, h.actor())


@route('GET', r'/api/audit', 'session')
def audit(h: Handler):
    if h.user['role'] == 'superadmin':
        return {'audit': h.server.store.list_audit(h.query_int('school_id'))}
    if h.user['role'] == 'school_admin':
        return {'audit': h.server.store.list_audit(h.user['school_id'])}
    raise Forbidden('Недостаточно прав')


# ============================================================== school-scoped data
@route('GET', r'/api/s/(\d{1,9})/overview', 'staff')
def overview(h, sid):
    return h.server.store.overview(sid, h.query('date') or local_today())


@route('GET', r'/api/s/(\d{1,9})/classes', 'staff')
def classes(h, sid):
    return {'classes': h.server.store.list_classes(sid)}


@route('POST', r'/api/s/(\d{1,9})/classes', 'admin')
def create_class(h, sid):
    return h.server.store.create_class(sid, h.body, h.actor())


@route('DELETE', r'/api/s/(\d{1,9})/classes/(\d{1,9})', 'admin')
def delete_class(h, sid, cid):
    return h.server.store.delete_class(sid, cid, h.actor())


@route('GET', r'/api/s/(\d{1,9})/students', 'staff')
def students(h, sid):
    return {'students': h.server.store.list_students(sid, h.query_int('class_id'))}


@route('POST', r'/api/s/(\d{1,9})/students', 'admin')
def create_student(h, sid):
    return h.server.store.create_student(sid, h.body, h.actor())


@route('PUT', r'/api/s/(\d{1,9})/students/(\d{1,9})', 'admin')
def update_student(h, sid, stid):
    return h.server.store.update_student(sid, stid, h.body, h.actor())


@route('DELETE', r'/api/s/(\d{1,9})/students/(\d{1,9})', 'admin')
def delete_student(h, sid, stid):
    return h.server.store.delete_student(sid, stid, h.actor())


@route('POST', r'/api/s/(\d{1,9})/students/(\d{1,9})/photos', 'admin')
def upload_photo(h, sid, stid):
    data = h.body.get('data_base64')
    if not isinstance(data, str):
        raise InputError('data_base64 is required')
    if data.startswith('data:'):
        data = data.split(',', 1)[-1]
    try:
        raw = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        raise InputError('Некорректные данные изображения') from None
    return h.server.store.add_photo(sid, stid, raw, h.actor())


@route('GET', r'/api/s/(\d{1,9})/photos/(\d{1,9})', 'staff')
def get_photo(h, sid, pid):
    path, mime = h.server.store.photo_file(sid, pid)
    h.send_bytes(path.read_bytes(), mime, csp="default-src 'none'", cache='private, no-store')


@route('DELETE', r'/api/s/(\d{1,9})/photos/(\d{1,9})', 'admin')
def delete_photo(h, sid, pid):
    return h.server.store.delete_photo(sid, pid, h.actor())


@route('GET', r'/api/s/(\d{1,9})/cameras', 'staff')
def cameras(h, sid):
    return {'cameras': h.server.store.list_cameras(sid)}


@route('POST', r'/api/s/(\d{1,9})/cameras', 'admin')
def create_camera(h, sid):
    return h.server.store.create_camera(sid, h.body, h.actor())


@route('DELETE', r'/api/s/(\d{1,9})/cameras/(\d{1,9})', 'admin')
def delete_camera(h, sid, cam):
    return h.server.store.delete_camera(sid, cam, h.actor())


@route('GET', r'/api/s/(\d{1,9})/lessons', 'staff')
def lessons(h, sid):
    return {'lessons': h.server.store.list_lessons(sid, h.query_int('class_id'))}


@route('POST', r'/api/s/(\d{1,9})/lessons', 'admin')
def create_lesson(h, sid):
    return h.server.store.create_lesson(sid, h.body, h.actor())


@route('DELETE', r'/api/s/(\d{1,9})/lessons/(\d{1,9})', 'admin')
def delete_lesson(h, sid, lid):
    return h.server.store.delete_lesson(sid, lid, h.actor())


@route('GET', r'/api/s/(\d{1,9})/attendance', 'staff')
def attendance(h, sid):
    class_id = h.query_int('class_id')
    if class_id is None:
        raise InputError('class_id is required')
    return h.server.store.attendance(sid, class_id, h.query('date') or local_today())


@route('PUT', r'/api/s/(\d{1,9})/marks', 'staff')
def set_mark(h, sid):
    return h.server.store.set_mark(sid, h.body, h.actor())


@route('GET', r'/api/s/(\d{1,9})/events', 'staff')
def events(h, sid):
    return {'events': h.server.store.list_events(sid, h.query('date') or local_today())}


@route('GET', r'/api/s/(\d{1,9})/reviews', 'staff')
def reviews(h, sid):
    return {'reviews': h.server.store.list_reviews(sid)}


@route('POST', r'/api/s/(\d{1,9})/reviews/(\d{1,9})', 'staff')
def review(h, sid, eid):
    return h.server.store.review_event(sid, eid, h.body.get('decision'), h.actor())


@route('GET', r'/api/s/(\d{1,9})/tokens', 'admin')
def tokens(h, sid):
    return {'tokens': h.server.store.list_tokens(sid)}


@route('POST', r'/api/s/(\d{1,9})/tokens', 'admin')
def create_token(h, sid):
    return h.server.store.create_token(sid, h.body, h.actor())


@route('DELETE', r'/api/s/(\d{1,9})/tokens/(\d{1,9})', 'admin')
def revoke_token(h, sid, tid):
    return h.server.store.revoke_token(sid, tid, h.actor())


# ============================================================== onboarding: invitations & applications
def _throttled(h, throttle):
    if throttle.blocked(h.client_ip()):
        h.respond(429, {'error': 'Слишком много попыток. Повторите позже.'})
        return True
    return False


@route('POST', r'/api/invites/info', 'public')
def invite_info(h):
    if _throttled(h, h.server.throttle):
        return None
    try:
        return h.server.store.invite_info(h.body.get('token'))
    except NotFound:
        h.server.throttle.fail(h.client_ip())
        raise


@route('POST', r'/api/invites/accept', 'public')
def invite_accept(h):
    if _throttled(h, h.server.throttle):
        return None
    try:
        user = h.server.store.accept_invite(h.body.get('token'), h.body)
    except NotFound:
        h.server.throttle.fail(h.client_ip())
        raise
    token, csrf = h.server.store.create_session(user['id'], h.client_ip())
    h.respond(200, {'user': _me(user), 'csrf': csrf}, {'Set-Cookie': h.session_cookie(token, 12 * 3600)})


@route('POST', r'/api/invites', 'session')
def invite_create(h):
    if h.user['role'] == 'teacher':
        raise Forbidden('Недостаточно прав')
    return h.server.store.create_invite(h.body, h.actor())


@route('POST', r'/api/users/(\d{1,9})/reset-link', 'session')
def reset_link(h, uid):
    if h.user['role'] == 'teacher':
        raise Forbidden('Недостаточно прав')
    return h.server.store.create_reset_invite(uid, h.actor())


@route('POST', r'/api/applications', 'public')
def application_submit(h):
    if _throttled(h, h.server.form_throttle):
        return None
    h.server.form_throttle.fail(h.client_ip())  # every submission counts: max 5 per hour per IP
    return h.server.store.submit_application(h.body, h.client_ip())


@route('GET', r'/api/applications', 'super')
def applications(h):
    return {'applications': h.server.store.list_applications()}


@route('POST', r'/api/applications/(\d{1,9})', 'super')
def application_decide(h, aid):
    return h.server.store.decide_application(aid, h.body.get('decision'), h.actor())


# ============================================================== bulk import / export / edits
def _decode_upload(h):
    data = h.body.get('data_base64')
    if not isinstance(data, str):
        raise InputError('data_base64 is required')
    try:
        return base64.b64decode(data.split(',', 1)[-1] if data.startswith('data:') else data, validate=True)
    except (binascii.Error, ValueError):
        raise InputError('Некорректные данные файла') from None


@route('GET', r'/api/import/template.csv', 'session')
def import_template(h):
    h.send_bytes(imports.TEMPLATE_CSV.encode('utf-8'), 'text/csv; charset=utf-8', csp="default-src 'none'")


@route('POST', r'/api/s/(\d{1,9})/import/students', 'admin', limit=MAX_IMPORT_BODY)
def import_students(h, sid):
    rows, errors = imports.parse_students(str(h.body.get('filename', '')), _decode_upload(h))
    result = h.server.store.import_students(sid, rows, h.actor(), dry_run=h.body.get('dry_run', True) is not False,
                                            consent=h.body.get('consent_confirmed') is True)
    result['errors'] = errors + result['errors']
    return result


@route('POST', r'/api/s/(\d{1,9})/import/photos', 'admin', limit=imports.MAX_ZIP_BYTES, raw=True)
def import_photos(h, sid):
    if h.headers.get('Content-Type', '').split(';')[0].strip().lower() not in ('application/zip', 'application/x-zip-compressed',
                                                                               'application/octet-stream'):
        raise InputError('Ожидается ZIP-архив')
    return h.server.store.import_photos(sid, imports.photo_entries(h.body), h.actor())


@route('POST', r'/api/s/(\d{1,9})/students/bulk', 'admin')
def students_bulk(h, sid):
    return h.server.store.bulk_students(sid, h.body, h.actor())


@route('POST', r'/api/s/(\d{1,9})/lessons/copy-day', 'admin')
def lessons_copy_day(h, sid):
    return h.server.store.copy_schedule_day(sid, h.body, h.actor())


@route('GET', r'/api/s/(\d{1,9})/export/attendance.csv', 'staff')
def export_attendance(h, sid):
    today = local_today()
    header, rows = h.server.store.export_attendance(sid, h.query('from') or today, h.query('to') or today, h.query_int('class_id'))
    data = imports.csv_bytes(header, rows)
    h.send_response(200)
    h.send_header('Content-Type', 'text/csv; charset=utf-8')
    h.send_header('Content-Disposition', f'attachment; filename="attendance_{h.query("from") or today}.csv"')
    h.send_header('Content-Length', str(len(data)))
    h.send_header('Cache-Control', 'no-store')
    h.common_headers()
    h.end_headers()
    h.wfile.write(data)


# ============================================================== devices / AI worker (school token)
@route('GET', r'/api/health', 'public')
def health(h):
    return {'status': 'ok'}


@route('POST', r'/api/ingest', 'token')
def ingest(h):
    return h.server.store.ingest(h.token_sid, h.body)


@route('POST', r'/api/worker/heartbeat', 'token')
def heartbeat(h):
    return h.server.store.heartbeat(h.token_sid, h.body)


@route('GET', r'/api/worker/roster', 'token')
def worker_roster(h):
    return h.server.store.worker_roster(h.token_sid)


@route('GET', r'/api/worker/schedule', 'token')
def worker_schedule(h):
    return h.server.store.worker_schedule(h.token_sid)


@route('GET', r'/api/worker/photos/(\d{1,9})', 'token')
def worker_photo(h, pid):
    path, mime = h.server.store.photo_file(h.token_sid, pid)
    h.send_bytes(path.read_bytes(), mime, csp="default-src 'none'", cache='no-store')


@route('POST', r'/api/worker/photos/(\d{1,9})/quality', 'token')
def worker_photo_quality(h, pid):
    return h.server.store.set_photo_quality(h.token_sid, pid, h.body.get('face_status'))


# ============================================================== entry point
def main(argv=None):
    parser = argparse.ArgumentParser(description='Camera Attendance AI platform server')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8080)
    parser.add_argument('--db', default=str(ROOT / 'data' / 'platform.sqlite3'))
    parser.add_argument('--allow-remote', action='store_true', help='Bind to a non-loopback address (only behind HTTPS proxy)')
    parser.add_argument('--secure-cookies', action='store_true', help='Mark cookies Secure and send HSTS (HTTPS proxy)')
    parser.add_argument('--trust-proxy', action='store_true', help='Use X-Forwarded-For from a trusted reverse proxy')
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    if args.host not in ('127.0.0.1', 'localhost', '::1') and not args.allow_remote:
        parser.error('Non-loopback binding requires --allow-remote and an HTTPS reverse proxy')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    db = Path(args.db)
    db.parent.mkdir(parents=True, exist_ok=True)
    store = Store(db)
    if store.count_superadmins() == 0:
        print('Нет ни одного супер-администратора. Создайте его:\n'
              '  python manage.py create-superadmin --username superadmin', file=sys.stderr)
        sys.exit(2)
    try:
        server = ApiServer((args.host, args.port), store, args.secure_cookies, args.trust_proxy)
    except OSError as exc:
        print(f'Не удалось открыть порт {args.port}: {exc}. Укажите другой: --port 8080', file=sys.stderr)
        sys.exit(1)
    print(f'Camera Attendance AI → http://{args.host}:{args.port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
