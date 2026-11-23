"""Multi-school attendance platform: storage and business rules (stdlib only).

Every school-scoped method takes ``sid`` (school id) explicitly and filters by it,
so one school can never read or modify another school's records.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import queue
import re
import secrets
import shutil
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _load_tz():
    name = os.environ.get('ATTENDANCE_TZ', 'Asia/Tashkent')
    try:
        return name, ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        # Windows has no system tz database unless the `tzdata` package is installed.
        # Uzbekistan is UTC+5 all year (no DST), so a fixed offset is exact.
        if name == 'Asia/Tashkent':
            return name, timezone(timedelta(hours=5), 'Asia/Tashkent')
        raise


TZ_NAME, TZ = _load_tz()

ROLES = ('superadmin', 'school_admin', 'teacher')
CAMERA_KINDS = ('entry', 'exit', 'classroom', 'observe')
MARK_STATUSES = ('present', 'late', 'absent', 'excused')
EVENT_TYPES = ('motion', 'device_event', 'access_granted', 'access_denied', 'face_match')
METHODS = ('none', 'card', 'device_face', 'ai_face')
PHOTO_STATUSES = ('unchecked', 'ok', 'no_face', 'multiple_faces', 'too_small', 'unreadable', 'demo')
MAX_PHOTOS_PER_STUDENT = 5
MAX_PHOTO_BYTES = 3 * 1024 * 1024
SESSION_TTL = timedelta(hours=12)
LOCK_AFTER_FAILURES = 5
LOCK_DURATION = timedelta(minutes=15)
CAMERA_ONLINE_SECONDS = 180
PRE_LESSON_MINUTES = 10
MIN_CAMERA_COVERAGE = 0.6
AGGREGATE_PENDING_HITS = 2
SECOND_MODEL_ACCEPT = 0.40  # second-opinion model (ArcFace) agrees -> accept
SECOND_MODEL_REJECT = 0.20  # second-opinion model disagrees -> drop the match
ENTRY_WINDOW_BEFORE = timedelta(minutes=90)
ENTRY_WINDOW_AFTER = timedelta(minutes=30)
PBKDF2_ITERATIONS = int(os.environ.get('ATTENDANCE_PBKDF2_ITERATIONS', '600000'))

CODE_RE = re.compile(r'^[a-zA-Z0-9_.-]{1,50}$')
USERNAME_RE = re.compile(r'^[a-zA-Z0-9_.@-]{3,40}$')
HHMM_RE = re.compile(r'^([01]\d|2[0-3]):[0-5]\d$')
LOGIN_FAILED = 'Неверный логин или пароль (после 5 ошибок вход блокируется на 15 минут)'
PASSWORD_ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789'


class InputError(ValueError):
    status = 400


class NotFound(InputError):
    status = 404


class Forbidden(InputError):
    status = 403


class AuthError(InputError):
    status = 401


# ----------------------------------------------------------------------------- helpers

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec='seconds')


def from_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def minute_key(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M')


def local_today() -> str:
    return datetime.now(TZ).date().isoformat()


def parse_time(value) -> datetime:
    if value is None:
        return now_utc()
    if not isinstance(value, str):
        raise InputError('observed_at must be an ISO 8601 string')
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as exc:
        raise InputError('observed_at must be an ISO 8601 datetime') from exc
    if dt.tzinfo is None:
        raise InputError('observed_at must contain a timezone offset')
    dt = dt.astimezone(timezone.utc)
    if not (now_utc() - timedelta(days=30) <= dt <= now_utc() + timedelta(minutes=5)):
        raise InputError('observed_at outside permitted 30-day window')
    return dt


def parse_day(value) -> date:
    try:
        d = date.fromisoformat(value)
        if d.isoformat() != value:
            raise ValueError
        return d
    except (TypeError, ValueError):
        raise InputError('date must use YYYY-MM-DD') from None


def require_code(value, field: str) -> str:
    if not isinstance(value, str) or not CODE_RE.fullmatch(value):
        raise InputError(f'{field}: 1-50 символов (латиница, цифры, _ - .)')
    return value


def require_text(value, field: str, limit: int = 120, allow_empty: bool = False) -> str:
    if value is None and allow_empty:
        return ''
    if not isinstance(value, str):
        raise InputError(f'{field}: ожидается строка')
    value = value.strip()
    if not (0 if allow_empty else 1) <= len(value) <= limit:
        raise InputError(f'{field}: длина 1-{limit} символов')
    if any(ord(ch) < 32 for ch in value):
        raise InputError(f'{field}: недопустимые символы')
    return value


def require_int(value, field: str, low: int, high: int) -> int:
    if isinstance(value, bool):
        raise InputError(f'{field}: ожидается число')
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise InputError(f'{field}: ожидается число') from None
    if not low <= number <= high:
        raise InputError(f'{field}: допустимо {low}-{high}')
    return number


def require_float(value, field: str, low: float, high: float) -> float:
    if isinstance(value, bool):
        raise InputError(f'{field}: ожидается число')
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise InputError(f'{field}: ожидается число') from None
    if not (low <= number <= high):
        raise InputError(f'{field}: допустимо {low}-{high}')
    return number


def require_hhmm(value, field: str) -> str:
    if not isinstance(value, str) or not HHMM_RE.fullmatch(value):
        raise InputError(f'{field}: формат ЧЧ:ММ')
    return value


def require_password(value) -> str:
    if not isinstance(value, str) or not 10 <= len(value) <= 128:
        raise InputError('Пароль должен быть длиной 10-128 символов')
    return value


def generate_password(length: int = 14) -> str:
    return ''.join(secrets.choice(PASSWORD_ALPHABET) for _ in range(length))


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode('ascii')


def hash_password(password: str, iterations: int | None = None) -> str:
    iterations = iterations or PBKDF2_ITERATIONS
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, iterations)
    return f'pbkdf2_sha256${iterations}${_b64(salt)}${_b64(digest)}'


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iterations, salt, digest = stored.split('$')
        if algo != 'pbkdf2_sha256':
            return False
        candidate = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'),
                                        base64.b64decode(salt), int(iterations))
        return hmac.compare_digest(candidate, base64.b64decode(digest))
    except (ValueError, TypeError):
        return False


def sha256_hex(data) -> str:
    if isinstance(data, str):
        data = data.encode('utf-8')
    return hashlib.sha256(data).hexdigest()


def sniff_image(data: bytes) -> str | None:
    if data[:3] == b'\xff\xd8\xff':
        return 'image/jpeg'
    if data[:8] == b'\x89PNG\r\n\x1a\n':
        return 'image/png'
    return None


SCHEMA = '''
CREATE TABLE IF NOT EXISTS schools (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, city TEXT NOT NULL DEFAULT '',
    late_grace_min INTEGER NOT NULL DEFAULT 10,
    auto_absent INTEGER NOT NULL DEFAULT 1,
    face_auto_threshold REAL NOT NULL DEFAULT 0.45,
    face_review_threshold REAL NOT NULL DEFAULT 0.363,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL, full_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('superadmin','school_admin','teacher')),
    school_id INTEGER REFERENCES schools(id) ON DELETE CASCADE,
    active INTEGER NOT NULL DEFAULT 1, failed_logins INTEGER NOT NULL DEFAULT 0,
    locked_until TEXT, last_login_at TEXT, created_at TEXT NOT NULL,
    CHECK ((role = 'superadmin') = (school_id IS NULL))
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    csrf TEXT NOT NULL, ip TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS classes (
    id INTEGER PRIMARY KEY, school_id INTEGER NOT NULL REFERENCES schools(id) ON DELETE CASCADE,
    name TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE (school_id, name)
);
CREATE TABLE IF NOT EXISTS students (
    id INTEGER PRIMARY KEY, school_id INTEGER NOT NULL REFERENCES schools(id) ON DELETE CASCADE,
    class_id INTEGER NOT NULL REFERENCES classes(id), code TEXT NOT NULL,
    full_name TEXT NOT NULL, consent_at TEXT NOT NULL, created_at TEXT NOT NULL,
    UNIQUE (school_id, code)
);
CREATE TABLE IF NOT EXISTS photos (
    id INTEGER PRIMARY KEY, school_id INTEGER NOT NULL REFERENCES schools(id) ON DELETE CASCADE,
    student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    file_name TEXT NOT NULL UNIQUE, sha256 TEXT NOT NULL, mime TEXT NOT NULL, size INTEGER NOT NULL,
    face_status TEXT NOT NULL DEFAULT 'unchecked', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cameras (
    id INTEGER PRIMARY KEY, school_id INTEGER NOT NULL REFERENCES schools(id) ON DELETE CASCADE,
    camera_key TEXT NOT NULL UNIQUE, name TEXT NOT NULL, room TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('entry','exit','classroom','observe')),
    last_seen_at TEXT, last_ok INTEGER, last_stats TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS camera_uptime (
    camera_id INTEGER NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
    minute TEXT NOT NULL, PRIMARY KEY (camera_id, minute)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS lessons (
    id INTEGER PRIMARY KEY, school_id INTEGER NOT NULL REFERENCES schools(id) ON DELETE CASCADE,
    class_id INTEGER NOT NULL REFERENCES classes(id) ON DELETE CASCADE,
    weekday INTEGER NOT NULL CHECK (weekday BETWEEN 1 AND 7),
    start_time TEXT NOT NULL, end_time TEXT NOT NULL, subject TEXT NOT NULL,
    teacher TEXT NOT NULL DEFAULT '',
    camera_id INTEGER REFERENCES cameras(id) ON DELETE SET NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY, school_id INTEGER NOT NULL REFERENCES schools(id) ON DELETE CASCADE,
    event_uid TEXT NOT NULL,
    camera_id INTEGER NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
    student_id INTEGER REFERENCES students(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL, verification_method TEXT NOT NULL, confidence REAL,
    second_score REAL,
    review_status TEXT NOT NULL DEFAULT 'none', verified INTEGER NOT NULL DEFAULT 0,
    reviewed_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    occurred_at TEXT NOT NULL, local_day TEXT NOT NULL, created_at TEXT NOT NULL,
    UNIQUE (school_id, event_uid)
);
CREATE TABLE IF NOT EXISTS marks (
    id INTEGER PRIMARY KEY, school_id INTEGER NOT NULL REFERENCES schools(id) ON DELETE CASCADE,
    student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    lesson_id INTEGER NOT NULL REFERENCES lessons(id) ON DELETE CASCADE,
    day TEXT NOT NULL, status TEXT NOT NULL CHECK (status IN ('present','late','absent','excused')),
    note TEXT NOT NULL DEFAULT '', set_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    set_at TEXT NOT NULL, UNIQUE (student_id, lesson_id, day)
);
CREATE TABLE IF NOT EXISTS api_tokens (
    id INTEGER PRIMARY KEY, school_id INTEGER NOT NULL REFERENCES schools(id) ON DELETE CASCADE,
    name TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE, prefix TEXT NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, last_used_at TEXT
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY, at TEXT NOT NULL, user_id INTEGER, username TEXT NOT NULL DEFAULT '',
    school_id INTEGER, action TEXT NOT NULL, detail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_events_school_day ON events(school_id, local_day, occurred_at);
CREATE INDEX IF NOT EXISTS idx_events_student_day ON events(student_id, local_day);
CREATE INDEX IF NOT EXISTS idx_events_review ON events(school_id, review_status);
CREATE INDEX IF NOT EXISTS idx_students_class ON students(school_id, class_id);
CREATE INDEX IF NOT EXISTS idx_lessons_class ON lessons(school_id, class_id, weekday);
CREATE INDEX IF NOT EXISTS idx_photos_student ON photos(student_id);
CREATE INDEX IF NOT EXISTS idx_audit_school ON audit(school_id, id);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
'''


def _actor(actor):
    return (actor or {}).get('id'), (actor or {}).get('username', 'system')


class Store:
    def __init__(self, path, photo_dir=None):
        self.path = str(path)
        self.photo_dir = Path(photo_dir) if photo_dir else Path(path).resolve().parent / 'photos'
        self._dummy_hash = hash_password(secrets.token_urlsafe(16))
        self._pool: queue.LifoQueue = queue.LifoQueue(maxsize=8)
        self.setup()

    def _open(self):
        conn = sqlite3.connect(self.path, timeout=15, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('PRAGMA busy_timeout=15000')
        conn.execute('PRAGMA synchronous=NORMAL')
        return conn

    @contextmanager
    def connect(self):
        """Transaction on a pooled connection (each connection is used by one thread at a time)."""
        try:
            conn = self._pool.get_nowait()
        except queue.Empty:
            conn = self._open()
        healthy = False
        try:
            with conn:
                yield conn
            healthy = True
        finally:
            if healthy and not conn.in_transaction:
                try:
                    self._pool.put_nowait(conn)
                    conn = None
                except queue.Full:
                    pass
            if conn is not None:
                conn.close()

    def close(self):
        while True:
            try:
                self._pool.get_nowait().close()
            except queue.Empty:
                return

    def setup(self):
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript(SCHEMA)
            # lightweight migrations for databases created by earlier versions
            school_cols = {r['name'] for r in db.execute('PRAGMA table_info(schools)')}
            if 'auto_absent' not in school_cols:
                db.execute('ALTER TABLE schools ADD COLUMN auto_absent INTEGER NOT NULL DEFAULT 1')
            event_cols = {r['name'] for r in db.execute('PRAGMA table_info(events)')}
            if 'second_score' not in event_cols:
                db.execute('ALTER TABLE events ADD COLUMN second_score REAL')

    # ------------------------------------------------------------------ audit
    def _audit(self, db, actor, school_id, action, detail=''):
        uid, uname = _actor(actor)
        db.execute('INSERT INTO audit(at,user_id,username,school_id,action,detail) VALUES(?,?,?,?,?,?)',
                   (iso(now_utc()), uid, uname, school_id, action, str(detail)[:500]))

    def list_audit(self, school_id=None, limit=200):
        with self.connect() as db:
            if school_id is None:
                rows = db.execute('SELECT a.*, s.name AS school_name FROM audit a LEFT JOIN schools s ON s.id=a.school_id '
                                  'ORDER BY a.id DESC LIMIT ?', (limit,))
            else:
                rows = db.execute('SELECT a.*, s.name AS school_name FROM audit a LEFT JOIN schools s ON s.id=a.school_id '
                                  'WHERE a.school_id=? ORDER BY a.id DESC LIMIT ?', (school_id, limit))
            return [dict(r) for r in rows]

    # ------------------------------------------------------------------ schools
    def _school(self, db, sid):
        row = db.execute('SELECT * FROM schools WHERE id=?', (sid,)).fetchone()
        if row is None:
            raise NotFound('Школа не найдена')
        return dict(row)

    def get_school(self, sid):
        with self.connect() as db:
            return self._school(db, sid)

    def list_schools(self, only_id=None):
        day = local_today()
        query = '''SELECT s.*,
            (SELECT COUNT(*) FROM classes c WHERE c.school_id=s.id) AS classes,
            (SELECT COUNT(*) FROM students st WHERE st.school_id=s.id) AS students,
            (SELECT COUNT(*) FROM cameras ca WHERE ca.school_id=s.id) AS cameras,
            (SELECT COUNT(*) FROM users u WHERE u.school_id=s.id) AS users,
            (SELECT COUNT(DISTINCT e.student_id) FROM events e WHERE e.school_id=s.id AND e.local_day=? AND e.verified=1
               AND e.student_id IS NOT NULL) AS seen_today
            FROM schools s'''
        with self.connect() as db:
            if only_id is None:
                rows = db.execute(query + ' ORDER BY s.name', (day,))
            else:
                rows = db.execute(query + ' WHERE s.id=?', (day, only_id))
            return [dict(r) for r in rows]

    def _school_fields(self, payload, current=None):
        cur = current or {'late_grace_min': 10, 'face_auto_threshold': 0.45, 'face_review_threshold': 0.363, 'city': '',
                          'auto_absent': 1}
        name = require_text(payload['name'], 'Название', 120) if 'name' in payload else cur.get('name')
        if name is None:
            raise InputError('Название обязательно')
        city = require_text(payload.get('city', cur['city']), 'Город', 80, allow_empty=True)
        grace = require_int(payload.get('late_grace_min', cur['late_grace_min']), 'Опоздание (мин)', 0, 60)
        auto = require_float(payload.get('face_auto_threshold', cur['face_auto_threshold']), 'Порог авто-отметки', 0.30, 0.95)
        review = require_float(payload.get('face_review_threshold', cur['face_review_threshold']), 'Порог проверки', 0.20, 0.95)
        if review > auto:
            raise InputError('Порог проверки должен быть не выше порога авто-отметки')
        auto_absent = payload.get('auto_absent', cur['auto_absent'])
        if auto_absent not in (True, False, 0, 1):
            raise InputError('auto_absent: true или false')
        return name, city, grace, auto, review, int(auto_absent)

    def create_school(self, payload, actor=None):
        name, city, grace, auto, review, auto_absent = self._school_fields(payload)
        with self.connect() as db:
            try:
                cur = db.execute('INSERT INTO schools(name,city,late_grace_min,auto_absent,face_auto_threshold,face_review_threshold,'
                                 'created_at) VALUES(?,?,?,?,?,?,?)', (name, city, grace, auto_absent, auto, review, iso(now_utc())))
            except sqlite3.IntegrityError:
                raise InputError('Школа с таким названием уже существует') from None
            sid = cur.lastrowid
            self._audit(db, actor, sid, 'school.create', name)
            return self._school(db, sid)

    def update_school(self, sid, payload, actor=None):
        with self.connect() as db:
            current = self._school(db, sid)
            name, city, grace, auto, review, auto_absent = self._school_fields(payload, current)
            try:
                db.execute('UPDATE schools SET name=?,city=?,late_grace_min=?,auto_absent=?,face_auto_threshold=?,face_review_threshold=? '
                           'WHERE id=?', (name, city, grace, auto_absent, auto, review, sid))
            except sqlite3.IntegrityError:
                raise InputError('Школа с таким названием уже существует') from None
            self._audit(db, actor, sid, 'school.update', f'grace={grace} auto={auto} review={review} auto_absent={auto_absent}')
            return self._school(db, sid)

    def delete_school(self, sid, actor=None):
        with self.connect() as db:
            school = self._school(db, sid)
            db.execute('DELETE FROM schools WHERE id=?', (sid,))
            self._audit(db, actor, None, 'school.delete', f"{school['name']} (id={sid})")
        shutil.rmtree(self.photo_dir / str(int(sid)), ignore_errors=True)
        return {'deleted': True}

    # ------------------------------------------------------------------ users & auth
    @staticmethod
    def _public_user(row):
        if row is None:
            return None
        data = dict(row)
        data.pop('password_hash', None)
        return data

    def list_users(self, school_id=None):
        query = ('SELECT u.id,u.username,u.full_name,u.role,u.school_id,u.active,u.last_login_at,u.created_at,'
                 's.name AS school_name FROM users u LEFT JOIN schools s ON s.id=u.school_id')
        with self.connect() as db:
            if school_id is None:
                rows = db.execute(query + ' ORDER BY u.role, s.name, u.username')
            else:
                rows = db.execute(query + ' WHERE u.school_id=? ORDER BY u.role, u.username', (school_id,))
            return [dict(r) for r in rows]

    def count_superadmins(self):
        with self.connect() as db:
            return db.execute("SELECT COUNT(*) FROM users WHERE role='superadmin' AND active=1").fetchone()[0]

    def create_user(self, payload, actor=None):
        """actor=None means trusted CLI. Otherwise role rules are enforced."""
        username = payload.get('username')
        if not isinstance(username, str) or not USERNAME_RE.fullmatch(username):
            raise InputError('Логин: 3-40 символов (латиница, цифры, _ . @ -)')
        full_name = require_text(payload.get('full_name') or username, 'ФИО', 120)
        role = payload.get('role')
        if role not in ROLES:
            raise InputError('Неизвестная роль')
        school_id = payload.get('school_id')
        if role == 'superadmin':
            school_id = None
        else:
            school_id = require_int(school_id, 'Школа', 1, 2**31)
        if actor is not None and actor.get('role') != 'superadmin':
            if actor.get('role') != 'school_admin' or role != 'teacher' or school_id != actor.get('school_id'):
                raise Forbidden('Администратор школы может создавать только учителей своей школы')
        generated = None
        password = payload.get('password')
        if password in (None, ''):
            password = generated = generate_password()
        require_password(password)
        with self.connect() as db:
            if school_id is not None:
                self._school(db, school_id)
            try:
                cur = db.execute('INSERT INTO users(username,password_hash,full_name,role,school_id,created_at) VALUES(?,?,?,?,?,?)',
                                 (username, hash_password(password), full_name, role, school_id, iso(now_utc())))
            except sqlite3.IntegrityError:
                raise InputError('Пользователь с таким логином уже существует') from None
            self._audit(db, actor, school_id, 'user.create', f'{username} ({role})')
            user = self._public_user(db.execute('SELECT * FROM users WHERE id=?', (cur.lastrowid,)).fetchone())
        return {'user': user, 'password': generated}

    def _managed_user(self, db, uid, actor):
        row = db.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()
        if row is None:
            raise NotFound('Пользователь не найден')
        if actor.get('role') != 'superadmin':
            if not (actor.get('role') == 'school_admin' and row['role'] == 'teacher' and row['school_id'] == actor.get('school_id')):
                raise NotFound('Пользователь не найден')
        return row

    def update_user(self, uid, payload, actor):
        with self.connect() as db:
            row = self._managed_user(db, uid, actor)
            result = {}
            if 'full_name' in payload:
                db.execute('UPDATE users SET full_name=? WHERE id=?', (require_text(payload['full_name'], 'ФИО'), uid))
            if 'active' in payload:
                active = bool(payload['active'])
                if not active and uid == actor.get('id'):
                    raise InputError('Нельзя отключить собственную учётную запись')
                if not active and row['role'] == 'superadmin':
                    left = db.execute("SELECT COUNT(*) FROM users WHERE role='superadmin' AND active=1 AND id<>?", (uid,)).fetchone()[0]
                    if left == 0:
                        raise InputError('Нельзя отключить последнего супер-админа')
                db.execute('UPDATE users SET active=? WHERE id=?', (int(active), uid))
                if not active:
                    db.execute('DELETE FROM sessions WHERE user_id=?', (uid,))
                self._audit(db, actor, row['school_id'], 'user.active', f"{row['username']} -> {active}")
            if payload.get('reset_password'):
                password = generate_password()
                db.execute('UPDATE users SET password_hash=?,failed_logins=0,locked_until=NULL WHERE id=?',
                           (hash_password(password), uid))
                db.execute('DELETE FROM sessions WHERE user_id=?', (uid,))
                self._audit(db, actor, row['school_id'], 'user.reset_password', row['username'])
                result['password'] = password
            result['user'] = self._public_user(db.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone())
            return result

    def delete_user(self, uid, actor):
        with self.connect() as db:
            row = self._managed_user(db, uid, actor)
            if uid == actor.get('id'):
                raise InputError('Нельзя удалить собственную учётную запись')
            if row['role'] == 'superadmin':
                left = db.execute("SELECT COUNT(*) FROM users WHERE role='superadmin' AND active=1 AND id<>?", (uid,)).fetchone()[0]
                if left == 0:
                    raise InputError('Нельзя удалить последнего супер-админа')
            db.execute('DELETE FROM users WHERE id=?', (uid,))
            self._audit(db, actor, row['school_id'], 'user.delete', row['username'])
        return {'deleted': True}

    def set_password(self, username, password):
        """Trusted CLI password reset."""
        require_password(password)
        with self.connect() as db:
            cur = db.execute('UPDATE users SET password_hash=?,failed_logins=0,locked_until=NULL,active=1 WHERE username=?',
                             (hash_password(password), username))
            if not cur.rowcount:
                raise NotFound('Пользователь не найден')
            uid = db.execute('SELECT id FROM users WHERE username=?', (username,)).fetchone()[0]
            db.execute('DELETE FROM sessions WHERE user_id=?', (uid,))
            self._audit(db, None, None, 'user.cli_password', username)

    def change_own_password(self, uid, old, new):
        require_password(new)
        with self.connect() as db:
            row = db.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()
            if row is None or not verify_password(old or '', row['password_hash']):
                raise InputError('Текущий пароль неверен')
            if old == new:
                raise InputError('Новый пароль совпадает с текущим')
            db.execute('UPDATE users SET password_hash=? WHERE id=?', (hash_password(new), uid))
            db.execute('DELETE FROM sessions WHERE user_id=?', (uid,))
            self._audit(db, dict(row), row['school_id'], 'user.change_password', row['username'])

    def authenticate(self, username, password, ip=''):
        if not isinstance(username, str) or not isinstance(password, str) or len(password) > 256 or len(username) > 64:
            raise AuthError(LOGIN_FAILED)
        now = now_utc()
        error = None
        # NB: failures must be COMMITTED, so the error is raised only after the transaction ends
        # (raising inside `with connect()` would roll back the lockout counter and the audit record).
        with self.connect() as db:
            row = db.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
            if row is None:
                verify_password(password, self._dummy_hash)  # equalise timing
                self._audit(db, {'username': username[:40]}, None, 'login.unknown_user', ip)
                error = LOGIN_FAILED
            elif row['locked_until'] and from_iso(row['locked_until']) > now:
                verify_password(password, self._dummy_hash)
                self._audit(db, dict(row), row['school_id'], 'login.locked', ip)
                error = LOGIN_FAILED  # same message as a wrong password: no account enumeration
            elif not verify_password(password, row['password_hash']):
                failures = row['failed_logins'] + 1
                locked = iso(now + LOCK_DURATION) if failures >= LOCK_AFTER_FAILURES else None
                db.execute('UPDATE users SET failed_logins=?,locked_until=? WHERE id=?',
                           (0 if locked else failures, locked, row['id']))
                self._audit(db, dict(row), row['school_id'], 'login.failed', ip + (' locked' if locked else ''))
                error = LOGIN_FAILED
            elif not row['active']:
                error = 'Учётная запись отключена'
            else:
                db.execute('UPDATE users SET failed_logins=0,locked_until=NULL,last_login_at=? WHERE id=?', (iso(now), row['id']))
                self._audit(db, dict(row), row['school_id'], 'login.ok', ip)
        if error:
            raise AuthError(error)
        return self._public_user(row)

    def create_session(self, user_id, ip=''):
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(24)
        now = now_utc()
        with self.connect() as db:
            db.execute('DELETE FROM sessions WHERE expires_at<?', (iso(now),))
            db.execute('INSERT INTO sessions(token_hash,user_id,csrf,ip,created_at,expires_at) VALUES(?,?,?,?,?,?)',
                       (sha256_hex(token), user_id, csrf, ip[:64], iso(now), iso(now + SESSION_TTL)))
        return token, csrf

    def session_user(self, token):
        if not token or len(token) > 128:
            return None
        with self.connect() as db:
            row = db.execute('''SELECT u.id,u.username,u.full_name,u.role,u.school_id,u.active,s.csrf,s.expires_at,
                                sc.name AS school_name
                                FROM sessions s JOIN users u ON u.id=s.user_id LEFT JOIN schools sc ON sc.id=u.school_id
                                WHERE s.token_hash=?''', (sha256_hex(token),)).fetchone()
            if row is None:
                return None
            if from_iso(row['expires_at']) <= now_utc() or not row['active']:
                db.execute('DELETE FROM sessions WHERE token_hash=?', (sha256_hex(token),))
                return None
            return dict(row)

    def delete_session(self, token):
        if token:
            with self.connect() as db:
                db.execute('DELETE FROM sessions WHERE token_hash=?', (sha256_hex(token),))

    # ------------------------------------------------------------------ device tokens
    def list_tokens(self, sid):
        with self.connect() as db:
            return [dict(r) for r in db.execute('SELECT id,name,prefix,revoked,created_at,last_used_at FROM api_tokens '
                                                'WHERE school_id=? ORDER BY id DESC', (sid,))]

    def create_token(self, sid, payload, actor=None):
        name = require_text(payload.get('name'), 'Название', 80)
        raw = 'cat_' + secrets.token_urlsafe(32)
        with self.connect() as db:
            self._school(db, sid)
            db.execute('INSERT INTO api_tokens(school_id,name,token_hash,prefix,created_at) VALUES(?,?,?,?,?)',
                       (sid, name, sha256_hex(raw), raw[:10], iso(now_utc())))
            self._audit(db, actor, sid, 'token.create', name)
        return {'token': raw, 'name': name}

    def revoke_token(self, sid, tid, actor=None):
        with self.connect() as db:
            cur = db.execute('UPDATE api_tokens SET revoked=1 WHERE id=? AND school_id=?', (tid, sid))
            if not cur.rowcount:
                raise NotFound('Токен не найден')
            self._audit(db, actor, sid, 'token.revoke', str(tid))
        return {'revoked': True}

    def token_school(self, raw):
        if not raw or not isinstance(raw, str) or len(raw) > 128:
            return None
        with self.connect() as db:
            row = db.execute('SELECT id,school_id,last_used_at FROM api_tokens WHERE token_hash=? AND revoked=0',
                             (sha256_hex(raw),)).fetchone()
            if row is None:
                return None
            now = now_utc()
            if not row['last_used_at'] or from_iso(row['last_used_at']) < now - timedelta(minutes=1):
                db.execute('UPDATE api_tokens SET last_used_at=? WHERE id=?', (iso(now), row['id']))
            return row['school_id']

    # ------------------------------------------------------------------ classes
    def list_classes(self, sid):
        with self.connect() as db:
            return [dict(r) for r in db.execute(
                'SELECT c.id,c.name,(SELECT COUNT(*) FROM students s WHERE s.class_id=c.id) AS students '
                'FROM classes c WHERE c.school_id=? ORDER BY c.name', (sid,))]

    def _class(self, db, sid, cid):
        row = db.execute('SELECT * FROM classes WHERE id=? AND school_id=?', (cid, sid)).fetchone()
        if row is None:
            raise NotFound('Класс не найден')
        return dict(row)

    def create_class(self, sid, payload, actor=None):
        name = require_text(payload.get('name'), 'Название класса', 40)
        with self.connect() as db:
            self._school(db, sid)
            try:
                cur = db.execute('INSERT INTO classes(school_id,name,created_at) VALUES(?,?,?)', (sid, name, iso(now_utc())))
            except sqlite3.IntegrityError:
                raise InputError('Такой класс уже существует') from None
            self._audit(db, actor, sid, 'class.create', name)
            return {'id': cur.lastrowid, 'name': name}

    def delete_class(self, sid, cid, actor=None):
        with self.connect() as db:
            cls = self._class(db, sid, cid)
            if db.execute('SELECT COUNT(*) FROM students WHERE class_id=?', (cid,)).fetchone()[0]:
                raise InputError('Сначала переведите или удалите учеников этого класса')
            db.execute('DELETE FROM classes WHERE id=?', (cid,))
            self._audit(db, actor, sid, 'class.delete', cls['name'])
        return {'deleted': True}

    # ------------------------------------------------------------------ students & photos
    def list_students(self, sid, class_id=None):
        with self.connect() as db:
            if class_id is None:
                rows = db.execute('SELECT s.id,s.code,s.full_name,s.class_id,c.name AS class_name,s.consent_at FROM students s '
                                  'JOIN classes c ON c.id=s.class_id WHERE s.school_id=? ORDER BY c.name,s.full_name', (sid,))
            else:
                rows = db.execute('SELECT s.id,s.code,s.full_name,s.class_id,c.name AS class_name,s.consent_at FROM students s '
                                  'JOIN classes c ON c.id=s.class_id WHERE s.school_id=? AND s.class_id=? ORDER BY s.full_name',
                                  (sid, class_id))
            students = [dict(r) for r in rows]
            photos = {}
            for p in db.execute('SELECT id,student_id,face_status FROM photos WHERE school_id=? ORDER BY id', (sid,)):
                photos.setdefault(p['student_id'], []).append({'id': p['id'], 'face_status': p['face_status']})
        for s in students:
            s['photos'] = photos.get(s['id'], [])
            s['ai_ready'] = any(p['face_status'] == 'ok' for p in s['photos'])
        return students

    def _student(self, db, sid, stid):
        row = db.execute('SELECT * FROM students WHERE id=? AND school_id=?', (stid, sid)).fetchone()
        if row is None:
            raise NotFound('Ученик не найден')
        return dict(row)

    def create_student(self, sid, payload, actor=None):
        code = require_code(payload.get('code'), 'Код ученика')
        full_name = require_text(payload.get('full_name'), 'ФИО', 120)
        class_id = require_int(payload.get('class_id'), 'Класс', 1, 2**31)
        if payload.get('consent_confirmed') is not True:
            raise InputError('Требуется подтверждение согласия родителей/законных представителей')
        with self.connect() as db:
            self._class(db, sid, class_id)
            try:
                cur = db.execute('INSERT INTO students(school_id,class_id,code,full_name,consent_at,created_at) VALUES(?,?,?,?,?,?)',
                                 (sid, class_id, code, full_name, iso(now_utc()), iso(now_utc())))
            except sqlite3.IntegrityError:
                raise InputError('Ученик с таким кодом уже есть в этой школе') from None
            self._audit(db, actor, sid, 'student.create', code)
            return {'id': cur.lastrowid, 'code': code, 'full_name': full_name}

    def update_student(self, sid, stid, payload, actor=None):
        with self.connect() as db:
            st = self._student(db, sid, stid)
            if 'full_name' in payload:
                db.execute('UPDATE students SET full_name=? WHERE id=?', (require_text(payload['full_name'], 'ФИО'), stid))
            if 'class_id' in payload:
                cid = require_int(payload['class_id'], 'Класс', 1, 2**31)
                self._class(db, sid, cid)
                db.execute('UPDATE students SET class_id=? WHERE id=?', (cid, stid))
            self._audit(db, actor, sid, 'student.update', st['code'])
        return {'updated': True}

    def delete_student(self, sid, stid, actor=None):
        with self.connect() as db:
            st = self._student(db, sid, stid)
            files = [r['file_name'] for r in db.execute('SELECT file_name FROM photos WHERE student_id=?', (stid,))]
            db.execute('DELETE FROM students WHERE id=?', (stid,))
            self._audit(db, actor, sid, 'student.delete', st['code'])
        for name in files:
            self._remove_photo_file(sid, name)
        return {'deleted': True, 'deleted_photos': len(files)}

    def _photo_path(self, sid, file_name) -> Path:
        if not re.fullmatch(r'[0-9a-f]{32}\.(jpg|png)', file_name):
            raise NotFound('Фото не найдено')
        return self.photo_dir / str(int(sid)) / file_name

    def _remove_photo_file(self, sid, file_name):
        try:
            self._photo_path(sid, file_name).unlink(missing_ok=True)
        except (OSError, NotFound):
            pass

    def add_photo(self, sid, stid, data: bytes, actor=None, face_status='unchecked'):
        if not isinstance(data, (bytes, bytearray)) or not data:
            raise InputError('Пустой файл')
        if len(data) > MAX_PHOTO_BYTES:
            raise InputError('Фото больше 3 МБ')
        mime = sniff_image(data)
        if mime is None:
            raise InputError('Поддерживаются только JPEG и PNG')
        if face_status not in PHOTO_STATUSES:
            face_status = 'unchecked'
        file_name = uuid.uuid4().hex + ('.jpg' if mime == 'image/jpeg' else '.png')
        path = self._photo_path(sid, file_name)
        with self.connect() as db:
            self._student(db, sid, stid)
            count = db.execute('SELECT COUNT(*) FROM photos WHERE student_id=?', (stid,)).fetchone()[0]
            if count >= MAX_PHOTOS_PER_STUDENT:
                raise InputError(f'Не более {MAX_PHOTOS_PER_STUDENT} фото на ученика')
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix('.tmp')
            tmp.write_bytes(bytes(data))
            try:
                cur = db.execute('INSERT INTO photos(school_id,student_id,file_name,sha256,mime,size,face_status,created_at) '
                                 'VALUES(?,?,?,?,?,?,?,?)',
                                 (sid, stid, file_name, sha256_hex(bytes(data)), mime, len(data), face_status, iso(now_utc())))
                tmp.replace(path)
            except Exception:
                tmp.unlink(missing_ok=True)
                raise
            self._audit(db, actor, sid, 'photo.add', f'student={stid}')
            return {'id': cur.lastrowid, 'face_status': face_status}

    def delete_photo(self, sid, pid, actor=None):
        with self.connect() as db:
            row = db.execute('SELECT * FROM photos WHERE id=? AND school_id=?', (pid, sid)).fetchone()
            if row is None:
                raise NotFound('Фото не найдено')
            db.execute('DELETE FROM photos WHERE id=?', (pid,))
            self._audit(db, actor, sid, 'photo.delete', f"student={row['student_id']}")
        self._remove_photo_file(sid, row['file_name'])
        return {'deleted': True}

    def photo_file(self, sid, pid):
        with self.connect() as db:
            row = db.execute('SELECT file_name,mime FROM photos WHERE id=? AND school_id=?', (pid, sid)).fetchone()
        if row is None:
            raise NotFound('Фото не найдено')
        path = self._photo_path(sid, row['file_name'])
        if not path.is_file():
            raise NotFound('Файл фото отсутствует')
        return path, row['mime']

    def set_photo_quality(self, sid, pid, status):
        if status not in PHOTO_STATUSES or status in ('unchecked', 'demo'):
            raise InputError('Неизвестный статус фото')
        with self.connect() as db:
            cur = db.execute('UPDATE photos SET face_status=? WHERE id=? AND school_id=?', (status, pid, sid))
            if not cur.rowcount:
                raise NotFound('Фото не найдено')
        return {'ok': True}

    def worker_roster(self, sid):
        with self.connect() as db:
            school = self._school(db, sid)
            students = [dict(r) for r in db.execute('SELECT id,code,full_name,class_id FROM students WHERE school_id=?', (sid,))]
            photos = {}
            for p in db.execute("SELECT id,student_id,sha256,face_status FROM photos WHERE school_id=? AND face_status<>'demo'", (sid,)):
                photos.setdefault(p['student_id'], []).append({'id': p['id'], 'sha256': p['sha256'], 'face_status': p['face_status']})
        for s in students:
            s['photos'] = photos.get(s['id'], [])
        return {'school_id': sid, 'face_auto_threshold': school['face_auto_threshold'],
                'face_review_threshold': school['face_review_threshold'],
                'students': [s for s in students if s['photos']]}

    # ------------------------------------------------------------------ cameras
    def list_cameras(self, sid):
        cutoff = now_utc() - timedelta(seconds=CAMERA_ONLINE_SECONDS)
        with self.connect() as db:
            rows = [dict(r) for r in db.execute('SELECT id,camera_key,name,room,kind,last_seen_at,last_ok,last_stats,created_at '
                                                'FROM cameras WHERE school_id=? ORDER BY name', (sid,))]
        for r in rows:
            r['online'] = bool(r['last_seen_at'] and r['last_ok'] and from_iso(r['last_seen_at']) >= cutoff)
            try:
                r['stats'] = json.loads(r.pop('last_stats') or '{}')
            except ValueError:
                r['stats'] = {}
        return rows

    def create_camera(self, sid, payload, actor=None):
        key = require_code(payload.get('camera_key'), 'Идентификатор камеры')
        name = require_text(payload.get('name'), 'Название', 120)
        room = require_text(payload.get('room'), 'Кабинет / зона', 80)
        kind = payload.get('kind')
        if kind not in CAMERA_KINDS:
            raise InputError('Тип камеры: entry, exit, classroom или observe')
        with self.connect() as db:
            self._school(db, sid)
            try:
                cur = db.execute('INSERT INTO cameras(school_id,camera_key,name,room,kind,created_at) VALUES(?,?,?,?,?,?)',
                                 (sid, key, name, room, kind, iso(now_utc())))
            except sqlite3.IntegrityError:
                raise InputError('Камера с таким идентификатором уже существует') from None
            self._audit(db, actor, sid, 'camera.create', key)
            return {'id': cur.lastrowid, 'camera_key': key}

    def delete_camera(self, sid, cam_id, actor=None):
        with self.connect() as db:
            row = db.execute('SELECT camera_key FROM cameras WHERE id=? AND school_id=?', (cam_id, sid)).fetchone()
            if row is None:
                raise NotFound('Камера не найдена')
            db.execute('DELETE FROM cameras WHERE id=?', (cam_id,))
            self._audit(db, actor, sid, 'camera.delete', row['camera_key'])
        return {'deleted': True}

    def heartbeat(self, sid, payload):
        key = require_code(payload.get('camera_key'), 'camera_key')
        ok = payload.get('ok') is True
        idle = payload.get('idle') is True  # worker sleeps outside lesson time: alive, but not watching
        stats = {k: payload[k] for k in ('frames', 'faces', 'matches', 'small_faces', 'gallery', 'fps')
                 if isinstance(payload.get(k), (int, float)) and not isinstance(payload.get(k), bool)}
        if idle:
            stats['idle'] = 1
        now = now_utc()
        with self.connect() as db:
            cam = db.execute('SELECT id FROM cameras WHERE camera_key=? AND school_id=?', (key, sid)).fetchone()
            if cam is None:
                raise InputError('Неизвестная камера; сначала добавьте её в панели')
            db.execute('UPDATE cameras SET last_seen_at=?,last_ok=?,last_stats=? WHERE id=?',
                       (iso(now), int(ok), json.dumps(stats)[:300], cam['id']))
            if ok and not idle:
                db.execute('INSERT OR IGNORE INTO camera_uptime(camera_id,minute) VALUES(?,?)', (cam['id'], minute_key(now)))
        return {'ok': True}

    def worker_schedule(self, sid, day=None):
        """When each camera must be watched today (UTC ISO windows), so workers run unattended
        and only spend CPU during lessons (classroom) or arrival/leaving time (entry/exit)."""
        d = parse_day(day) if day else datetime.now(TZ).date()
        with self.connect() as db:
            self._school(db, sid)
            cameras = [dict(r) for r in db.execute('SELECT id,camera_key,kind FROM cameras WHERE school_id=?', (sid,))]
            lessons = [dict(r) for r in db.execute('SELECT camera_id,start_time,end_time FROM lessons WHERE school_id=? AND weekday=?',
                                                   (sid, d.isoweekday()))]

        def at(hhmm):
            return datetime.combine(d, dtime.fromisoformat(hhmm), TZ)

        first = min((at(l['start_time']) for l in lessons), default=None)
        last = max((at(l['end_time']) for l in lessons), default=None)
        windows = {}
        for cam in cameras:
            spans = []
            if cam['kind'] == 'classroom':
                spans = [(at(l['start_time']) - timedelta(minutes=PRE_LESSON_MINUTES), at(l['end_time']))
                         for l in lessons if l['camera_id'] == cam['id']]
            elif cam['kind'] == 'entry' and first:
                spans = [(first - ENTRY_WINDOW_BEFORE, last + ENTRY_WINDOW_AFTER)]
            elif cam['kind'] == 'exit' and first:
                spans = [(first, last + ENTRY_WINDOW_BEFORE)]
            merged = []
            for a, b in sorted(spans):
                if merged and a <= merged[-1][1]:
                    merged[-1][1] = max(merged[-1][1], b)
                else:
                    merged.append([a, b])
            windows[cam['camera_key']] = [[iso(a), iso(b)] for a, b in merged]
        return {'date': d.isoformat(), 'windows': windows}

    # ------------------------------------------------------------------ lessons (schedule)
    def list_lessons(self, sid, class_id=None):
        query = ('SELECT l.id,l.class_id,c.name AS class_name,l.weekday,l.start_time,l.end_time,l.subject,l.teacher,'
                 'l.camera_id,ca.name AS camera_name,ca.room AS room FROM lessons l JOIN classes c ON c.id=l.class_id '
                 'LEFT JOIN cameras ca ON ca.id=l.camera_id WHERE l.school_id=?')
        with self.connect() as db:
            if class_id is None:
                rows = db.execute(query + ' ORDER BY l.weekday,l.start_time,c.name', (sid,))
            else:
                rows = db.execute(query + ' AND l.class_id=? ORDER BY l.weekday,l.start_time', (sid, class_id))
            return [dict(r) for r in rows]

    def create_lesson(self, sid, payload, actor=None):
        class_id = require_int(payload.get('class_id'), 'Класс', 1, 2**31)
        weekday = require_int(payload.get('weekday'), 'День недели', 1, 7)
        start = require_hhmm(payload.get('start_time'), 'Начало')
        end = require_hhmm(payload.get('end_time'), 'Конец')
        if end <= start:
            raise InputError('Конец урока должен быть позже начала')
        subject = require_text(payload.get('subject'), 'Предмет', 80)
        teacher = require_text(payload.get('teacher', ''), 'Учитель', 120, allow_empty=True)
        camera_id = payload.get('camera_id')
        with self.connect() as db:
            self._class(db, sid, class_id)
            if camera_id in (None, '', 0):
                camera_id = None
            else:
                camera_id = require_int(camera_id, 'Камера', 1, 2**31)
                if db.execute('SELECT 1 FROM cameras WHERE id=? AND school_id=?', (camera_id, sid)).fetchone() is None:
                    raise NotFound('Камера не найдена')
            clash = db.execute('SELECT subject,start_time,end_time FROM lessons WHERE school_id=? AND class_id=? AND weekday=? '
                               'AND start_time<? AND end_time>?', (sid, class_id, weekday, end, start)).fetchone()
            if clash:
                raise InputError(f"Пересекается с уроком «{clash['subject']}» {clash['start_time']}-{clash['end_time']}")
            cur = db.execute('INSERT INTO lessons(school_id,class_id,weekday,start_time,end_time,subject,teacher,camera_id,created_at) '
                             'VALUES(?,?,?,?,?,?,?,?,?)', (sid, class_id, weekday, start, end, subject, teacher, camera_id, iso(now_utc())))
            self._audit(db, actor, sid, 'lesson.create', f'class={class_id} d{weekday} {start}-{end} {subject}')
            return {'id': cur.lastrowid}

    def delete_lesson(self, sid, lid, actor=None):
        with self.connect() as db:
            cur = db.execute('DELETE FROM lessons WHERE id=? AND school_id=?', (lid, sid))
            if not cur.rowcount:
                raise NotFound('Урок не найден')
            self._audit(db, actor, sid, 'lesson.delete', str(lid))
        return {'deleted': True}

    # ------------------------------------------------------------------ events
    def ingest(self, sid, payload):
        """Store a device/worker event for school ``sid``.

        * access_granted from an entry/exit device with card/device_face -> verified.
        * face_match from the AI worker -> verified above the school's auto threshold,
          'pending' review between review and auto thresholds, dropped below.
        * motion/device_event/access_denied never identify a person.
        """
        camera_key = require_code(payload.get('camera_key'), 'camera_key')
        event_uid = require_code(payload.get('event_uid'), 'event_uid')
        event_type = payload.get('event_type')
        if event_type not in EVENT_TYPES:
            raise InputError('Unsupported event_type')
        method = payload.get('verification_method', 'none')
        if method not in METHODS:
            raise InputError('Unsupported verification_method')
        dt = parse_time(payload.get('observed_at'))
        local_day = dt.astimezone(TZ).date().isoformat()
        with self.connect() as db:
            school = self._school(db, sid)
            camera = db.execute('SELECT * FROM cameras WHERE camera_key=? AND school_id=?', (camera_key, sid)).fetchone()
            if camera is None:
                raise InputError('Unknown camera_key; register camera first')
            verified = False
            review = 'none'
            confidence = None
            second = None
            student_id = None
            if event_type == 'access_granted' and method in ('card', 'device_face') and camera['kind'] in ('entry', 'exit'):
                verified, review = True, 'auto'
            elif event_type == 'face_match':
                if method != 'ai_face' or camera['kind'] == 'observe':
                    raise InputError('face_match requires verification_method=ai_face and a non-observe camera')
                confidence = require_float(payload.get('confidence'), 'confidence', 0.0, 1.0)
                if payload.get('second_score') is not None:
                    second = require_float(payload.get('second_score'), 'second_score', 0.0, 1.0)
                if confidence < school['face_review_threshold']:
                    return {'stored': False, 'reason': 'below_review_threshold'}
                if second is not None and second < SECOND_MODEL_REJECT:
                    return {'stored': False, 'reason': 'second_model_reject'}
                if confidence >= school['face_auto_threshold'] or (second is not None and second >= SECOND_MODEL_ACCEPT):
                    verified, review = True, 'auto'
                else:
                    review = 'pending'
            if event_type in ('access_granted', 'face_match') and (verified or review == 'pending'):
                code = require_code(payload.get('person_code'), 'person_code')
                st = db.execute('SELECT id FROM students WHERE code=? AND school_id=?', (code, sid)).fetchone()
                if st is None:
                    raise InputError('Unknown person_code; enrollment is required')
                student_id = st['id']
            else:
                method = 'none'
            try:
                cur = db.execute('INSERT INTO events(school_id,event_uid,camera_id,student_id,event_type,verification_method,confidence,'
                                 'second_score,review_status,verified,occurred_at,local_day,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                                 (sid, event_uid, camera['id'], student_id, event_type, method, confidence, second, review,
                                  int(verified), iso(dt), local_day, iso(now_utc())))
            except sqlite3.IntegrityError:
                return {'stored': False, 'duplicate': True, 'verified': False}
        return {'stored': True, 'duplicate': False, 'id': cur.lastrowid, 'verified': verified,
                'review_status': review, 'local_day': local_day}

    def list_events(self, sid, day, limit=300):
        parse_day(day)
        with self.connect() as db:
            return [dict(r) for r in db.execute(
                '''SELECT e.id,e.event_type,e.verification_method,e.confidence,e.second_score,e.review_status,e.verified,e.occurred_at,
                   s.code AS student_code,s.full_name,cl.name AS class_name,c.name AS camera_name,c.room,c.kind AS camera_kind
                   FROM events e JOIN cameras c ON c.id=e.camera_id LEFT JOIN students s ON s.id=e.student_id
                   LEFT JOIN classes cl ON cl.id=s.class_id
                   WHERE e.school_id=? AND e.local_day=? ORDER BY e.occurred_at DESC LIMIT ?''', (sid, day, limit))]

    def list_reviews(self, sid, limit=200):
        with self.connect() as db:
            rows = [dict(r) for r in db.execute(
                '''SELECT e.id,e.confidence,e.second_score,e.occurred_at,e.local_day,s.id AS student_id,s.code AS student_code,s.full_name,
                   cl.name AS class_name,c.name AS camera_name,c.room
                   FROM events e JOIN cameras c ON c.id=e.camera_id JOIN students s ON s.id=e.student_id
                   JOIN classes cl ON cl.id=s.class_id
                   WHERE e.school_id=? AND e.review_status='pending' ORDER BY e.occurred_at DESC LIMIT ?''', (sid, limit))]
            for r in rows:
                photo = db.execute("SELECT id FROM photos WHERE student_id=? ORDER BY face_status='ok' DESC, id LIMIT 1",
                                   (r['student_id'],)).fetchone()
                r['photo_id'] = photo['id'] if photo else None
            return rows

    def review_event(self, sid, eid, decision, actor=None):
        if decision not in ('confirm', 'reject'):
            raise InputError('decision: confirm или reject')
        with self.connect() as db:
            row = db.execute("SELECT id FROM events WHERE id=? AND school_id=? AND review_status='pending'", (eid, sid)).fetchone()
            if row is None:
                raise NotFound('Событие не найдено или уже проверено')
            db.execute('UPDATE events SET review_status=?,verified=?,reviewed_by=? WHERE id=?',
                       ('confirmed' if decision == 'confirm' else 'rejected', int(decision == 'confirm'), (actor or {}).get('id'), eid))
            self._audit(db, actor, sid, 'review.' + decision, str(eid))
        return {'ok': True}

    # ------------------------------------------------------------------ manual marks
    def set_mark(self, sid, payload, actor=None):
        student_id = require_int(payload.get('student_id'), 'Ученик', 1, 2**31)
        lesson_id = require_int(payload.get('lesson_id'), 'Урок', 1, 2**31)
        day = parse_day(payload.get('day'))
        status = payload.get('status')
        note = require_text(payload.get('note', ''), 'Комментарий', 200, allow_empty=True)
        with self.connect() as db:
            st = self._student(db, sid, student_id)
            lesson = db.execute('SELECT * FROM lessons WHERE id=? AND school_id=?', (lesson_id, sid)).fetchone()
            if lesson is None:
                raise NotFound('Урок не найден')
            if lesson['class_id'] != st['class_id']:
                raise InputError('Ученик не относится к классу этого урока')
            if lesson['weekday'] != day.isoweekday():
                raise InputError('Этот урок не проходит в выбранный день')
            if day > datetime.now(TZ).date():
                raise InputError('Нельзя отмечать будущие даты')
            if status == 'clear':
                db.execute('DELETE FROM marks WHERE student_id=? AND lesson_id=? AND day=?', (student_id, lesson_id, day.isoformat()))
            elif status in MARK_STATUSES:
                db.execute('INSERT INTO marks(school_id,student_id,lesson_id,day,status,note,set_by,set_at) VALUES(?,?,?,?,?,?,?,?) '
                           'ON CONFLICT(student_id,lesson_id,day) DO UPDATE SET status=excluded.status,note=excluded.note,'
                           'set_by=excluded.set_by,set_at=excluded.set_at',
                           (sid, student_id, lesson_id, day.isoformat(), status, note, (actor or {}).get('id'), iso(now_utc())))
            else:
                raise InputError('Статус: present, late, absent, excused или clear')
            self._audit(db, actor, sid, 'mark.set', f"{st['code']} lesson={lesson_id} {day} {status}")
        return {'ok': True}

    # ------------------------------------------------------------------ attendance engine
    def _coverage(self, db, camera_id, start, end):
        minutes = max(1, int((end - start).total_seconds() // 60))
        count = db.execute('SELECT COUNT(*) FROM camera_uptime WHERE camera_id=? AND minute>=? AND minute<?',
                           (camera_id, minute_key(start), minute_key(end))).fetchone()[0]
        return min(1.0, count / minutes)

    def attendance(self, sid, class_id, day, now=None):
        """Per-lesson attendance grid for one class and day.

        Priority: manual mark > AI/ACS evidence > camera-health-aware automatic decision.
        * seen with high confidence, or >= AGGREGATE_PENDING_HITS medium-confidence sightings
          in the lesson window -> present / late (fully automatic);
        * a single medium-confidence sighting -> needs_review;
        * camera verifiably worked (coverage >= MIN_CAMERA_COVERAGE) but child never seen
          -> 'absent' when the school's auto_absent is on, otherwise 'not_seen';
        * camera offline / no evidence source -> 'no_data': the system cannot know, so it never
          guesses (a broken camera must not turn a whole class "absent").
        """
        d = parse_day(day)
        now = now or now_utc()
        with self.connect() as db:
            school = self._school(db, sid)
            cls = self._class(db, sid, class_id)
            students = [dict(r) for r in db.execute('SELECT id,code,full_name FROM students WHERE school_id=? AND class_id=? '
                                                    'ORDER BY full_name', (sid, class_id))]
            lessons = [dict(r) for r in db.execute(
                'SELECT l.id,l.start_time,l.end_time,l.subject,l.teacher,l.camera_id,c.name AS camera_name,c.room '
                'FROM lessons l LEFT JOIN cameras c ON c.id=l.camera_id '
                'WHERE l.school_id=? AND l.class_id=? AND l.weekday=? ORDER BY l.start_time', (sid, class_id, d.isoweekday()))]
            events = db.execute(
                'SELECT e.student_id,e.camera_id,e.occurred_at,e.verified,e.review_status,c.kind FROM events e '
                'JOIN cameras c ON c.id=e.camera_id JOIN students s ON s.id=e.student_id '
                'WHERE e.school_id=? AND e.local_day=? AND s.class_id=?', (sid, d.isoformat(), class_id)).fetchall()
            marks = {(m['student_id'], m['lesson_id']): dict(m) for m in db.execute(
                'SELECT m.student_id,m.lesson_id,m.status,m.note,u.full_name AS set_by_name FROM marks m '
                'LEFT JOIN users u ON u.id=m.set_by JOIN students s ON s.id=m.student_id '
                'WHERE m.school_id=? AND m.day=? AND s.class_id=?', (sid, d.isoformat(), class_id))}
            has_entry_devices = db.execute("SELECT COUNT(*) FROM cameras WHERE school_id=? AND kind='entry'", (sid,)).fetchone()[0] > 0
            # entry devices (turnstiles) send no heartbeat: treat them as working if they reported anyone that day
            entry_worked = db.execute("SELECT COUNT(*) FROM events e JOIN cameras c ON c.id=e.camera_id WHERE e.school_id=? "
                                      "AND e.local_day=? AND c.kind='entry' AND e.verified=1", (sid, d.isoformat())).fetchone()[0] > 0
            auto_absent = bool(school['auto_absent'])
            grace = timedelta(minutes=school['late_grace_min'])
            for lesson in lessons:
                start = datetime.combine(d, dtime.fromisoformat(lesson['start_time']), TZ)
                end = datetime.combine(d, dtime.fromisoformat(lesson['end_time']), TZ)
                lesson['_start'], lesson['_end'] = start, end
                lesson['state'] = 'upcoming' if now < start else ('in_progress' if now < end else 'finished')
                lesson['coverage'] = None
                if lesson['camera_id'] and now >= start:
                    lesson['coverage'] = round(self._coverage(db, lesson['camera_id'], start, min(now, end)), 2)

        by_student = {}
        entry_first = {}
        for e in events:
            t = from_iso(e['occurred_at'])
            by_student.setdefault(e['student_id'], []).append((e['camera_id'], t, e['verified'], e['review_status']))
            if e['verified'] and e['kind'] == 'entry':
                if e['student_id'] not in entry_first or t < entry_first[e['student_id']]:
                    entry_first[e['student_id']] = t

        def cell(lesson, student):
            mark = marks.get((student['id'], lesson['id']))
            if mark:
                return {'status': mark['status'], 'source': 'manual', 'note': mark['note'], 'by': mark['set_by_name']}
            start, end = lesson['_start'], lesson['_end']
            if now < start:
                return {'status': 'upcoming', 'source': None}
            evs = by_student.get(student['id'], [])
            if lesson['camera_id']:
                window = start - timedelta(minutes=PRE_LESSON_MINUTES)
                in_window = [(t, ok, rs) for cam, t, ok, rs in evs if cam == lesson['camera_id'] and window <= t <= end]
                seen = [t for t, ok, _ in in_window if ok]
                pending = [t for t, _, rs in in_window if rs == 'pending']
                if not seen and len(pending) >= AGGREGATE_PENDING_HITS:
                    seen = pending  # repeated medium-confidence sightings of the same child -> accept automatically
                if seen:
                    first = min(seen)
                    return {'status': 'present' if first <= start + grace else 'late', 'source': 'ai', 'first_seen': iso(first)}
                if pending:
                    return {'status': 'needs_review', 'source': 'ai'}
                if now < end:
                    return {'status': 'in_progress', 'source': None}
                if (lesson['coverage'] or 0) >= MIN_CAMERA_COVERAGE:
                    return {'status': 'absent' if auto_absent else 'not_seen', 'source': 'ai'}
                return {'status': 'no_data', 'source': None}
            first_in = entry_first.get(student['id'])
            if first_in and first_in <= start + grace:
                return {'status': 'present', 'source': 'entry', 'first_seen': iso(first_in)}
            if first_in and first_in <= end:
                return {'status': 'late', 'source': 'entry', 'first_seen': iso(first_in)}
            if now < end:
                return {'status': 'in_progress', 'source': None}
            if has_entry_devices and entry_worked:
                return {'status': 'absent' if auto_absent else 'not_seen', 'source': 'entry'}
            return {'status': 'no_data', 'source': None}

        rows = []
        for st in students:
            rows.append({'student': st, 'cells': {str(l['id']): cell(l, st) for l in lessons}})
        out_lessons = []
        for l in lessons:
            counts = {}
            for r in rows:
                s = r['cells'][str(l['id'])]['status']
                counts[s] = counts.get(s, 0) + 1
            out_lessons.append({k: v for k, v in l.items() if not k.startswith('_')} | {'counts': counts})
        return {'date': d.isoformat(), 'weekday': d.isoweekday(), 'timezone': TZ_NAME,
                'class': cls, 'late_grace_min': school['late_grace_min'],
                'lessons': out_lessons, 'rows': rows}

    def overview(self, sid, day=None, now=None):
        day = day or local_today()
        d = parse_day(day)
        now = now or now_utc()
        with self.connect() as db:
            school = self._school(db, sid)
            total = db.execute('SELECT COUNT(*) FROM students WHERE school_id=?', (sid,)).fetchone()[0]
            seen = db.execute('''SELECT COUNT(*) FROM (
                    SELECT student_id FROM events WHERE school_id=? AND local_day=? AND verified=1 AND student_id IS NOT NULL
                    UNION SELECT student_id FROM marks WHERE school_id=? AND day=? AND status IN ('present','late'))''',
                              (sid, day, sid, day)).fetchone()[0]
            pending = db.execute("SELECT COUNT(*) FROM events WHERE school_id=? AND review_status='pending'", (sid,)).fetchone()[0]
            ai_ready = db.execute("SELECT COUNT(DISTINCT student_id) FROM photos WHERE school_id=? AND face_status='ok'", (sid,)).fetchone()[0]
            no_photo = db.execute('SELECT COUNT(*) FROM students s WHERE s.school_id=? AND NOT EXISTS '
                                  '(SELECT 1 FROM photos p WHERE p.student_id=s.id)', (sid,)).fetchone()[0]
            lessons = [dict(r) for r in db.execute(
                'SELECT l.id,l.class_id,c.name AS class_name,l.start_time,l.end_time,l.subject,l.teacher,l.camera_id,'
                'ca.name AS camera_name,ca.room FROM lessons l JOIN classes c ON c.id=l.class_id '
                'LEFT JOIN cameras ca ON ca.id=l.camera_id WHERE l.school_id=? AND l.weekday=? ORDER BY l.start_time,c.name',
                (sid, d.isoweekday()))]
        cameras = self.list_cameras(sid)
        cam_online = {c['id']: c['online'] for c in cameras}
        for l in lessons:
            start = datetime.combine(d, dtime.fromisoformat(l['start_time']), TZ)
            end = datetime.combine(d, dtime.fromisoformat(l['end_time']), TZ)
            l['state'] = 'upcoming' if now < start else ('in_progress' if now < end else 'finished')
            l['camera_online'] = cam_online.get(l['camera_id']) if l['camera_id'] else None
        events = self.list_events(sid, day, limit=12)
        return {'school': school, 'date': day, 'timezone': TZ_NAME,
                'stats': {'students': total, 'seen_today': seen, 'not_seen': max(0, total - seen),
                          'pending_reviews': pending, 'ai_ready': ai_ready, 'no_photo': no_photo,
                          'cameras': len(cameras), 'cameras_online': sum(c['online'] for c in cameras),
                          'lessons_today': len(lessons)},
                'lessons': lessons, 'cameras': cameras, 'events': events}

    # ------------------------------------------------------------------ maintenance
    def purge(self, days: int):
        days = require_int(days, 'days', 7, 3650)
        cutoff = (datetime.now(TZ).date() - timedelta(days=days)).isoformat()
        with self.connect() as db:
            ev = db.execute('DELETE FROM events WHERE local_day<?', (cutoff,)).rowcount
            mk = db.execute('DELETE FROM marks WHERE day<?', (cutoff,)).rowcount
            up = db.execute('DELETE FROM camera_uptime WHERE minute<?', (cutoff,)).rowcount
            au = db.execute('DELETE FROM audit WHERE at<?', (cutoff,)).rowcount
            db.execute('DELETE FROM sessions WHERE expires_at<?', (iso(now_utc()),))
            self._audit(db, None, None, 'maintenance.purge', f'before {cutoff}: events={ev} marks={mk}')
        return {'events': ev, 'marks': mk, 'uptime': up, 'audit': au, 'cutoff': cutoff}
