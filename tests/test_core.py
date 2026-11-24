import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import core  # noqa: E402
from core import TZ, AuthError, Forbidden, InputError, NotFound, Store, minute_key  # noqa: E402
from manage import synthetic_avatar  # noqa: E402

core.PBKDF2_ITERATIONS = 1000  # fast hashing in tests
SUPER = {'id': None, 'username': 'root', 'role': 'superadmin', 'school_id': None}  # id filled in setUp


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / 'db.sqlite3')
        SUPER['id'] = self.store.create_user({'username': 'root', 'role': 'superadmin', 'password': 'SuperSecret123'})['user']['id']
        self.a = self.store.create_school({'name': 'School A'})['id']
        self.b = self.store.create_school({'name': 'School B'})['id']
        self.store.create_camera(self.a, {'camera_key': 'a_gate', 'name': 'Gate', 'room': 'Hall', 'kind': 'entry'})
        self.store.create_camera(self.a, {'camera_key': 'a_room', 'name': 'Room', 'room': '101', 'kind': 'classroom'})
        self.store.create_camera(self.a, {'camera_key': 'a_room2', 'name': 'Room2', 'room': '102', 'kind': 'classroom'})
        self.store.create_camera(self.a, {'camera_key': 'a_hall', 'name': 'Hall', 'room': 'Hall', 'kind': 'observe'})
        self.cams = {c['camera_key']: c['id'] for c in self.store.list_cameras(self.a)}
        self.cls = self.store.create_class(self.a, {'name': '5-A'})['id']
        self.students = {}
        for code in ('S1', 'S2', 'S3', 'S4'):
            self.students[code] = self.store.create_student(
                self.a, {'code': code, 'full_name': 'Student ' + code, 'class_id': self.cls, 'consent_confirmed': True})['id']

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def face(self, code, when, conf, cam='a_room', uid=None):
        return self.store.ingest(self.a, {'camera_key': cam, 'event_uid': uid or f'f-{code}-{when.timestamp()}',
                                          'event_type': 'face_match', 'verification_method': 'ai_face',
                                          'person_code': code, 'confidence': conf, 'observed_at': when.isoformat()})


class AuthTests(Base):
    def test_password_hash_roundtrip(self):
        stored = core.hash_password('correct horse battery')
        self.assertTrue(core.verify_password('correct horse battery', stored))
        self.assertFalse(core.verify_password('wrong', stored))
        self.assertFalse(core.verify_password('x', 'garbage'))
        self.assertNotIn('correct', stored)

    def test_login_and_lockout(self):
        self.assertEqual(self.store.authenticate('root', 'SuperSecret123')['role'], 'superadmin')
        self.assertEqual(self.store.authenticate('ROOT', 'SuperSecret123')['username'], 'root')  # case-insensitive login
        for _ in range(core.LOCK_AFTER_FAILURES):
            with self.assertRaises(AuthError):
                self.store.authenticate('root', 'bad-password')
        with self.assertRaises(AuthError):  # locked: even the right password fails
            self.store.authenticate('root', 'SuperSecret123')
        with self.assertRaises(AuthError):
            self.store.authenticate('nobody', 'whatever-123')

    def test_user_payload_never_contains_hash(self):
        res = self.store.create_user({'username': 'teach', 'role': 'teacher', 'school_id': self.a})
        self.assertNotIn('password_hash', res['user'])
        self.assertTrue(res['password'] and len(res['password']) >= 10)
        self.assertTrue(all('password_hash' not in u for u in self.store.list_users()))

    def test_session_lifecycle(self):
        uid = SUPER['id']
        token, csrf = self.store.create_session(uid)
        user = self.store.session_user(token)
        self.assertEqual(user['csrf'], csrf)
        self.assertIsNone(self.store.session_user('forged'))
        with self.store.connect() as db:
            db.execute('UPDATE sessions SET expires_at=?', ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),))
        self.assertIsNone(self.store.session_user(token))
        token, _ = self.store.create_session(uid)
        self.store.delete_session(token)
        self.assertIsNone(self.store.session_user(token))

    def test_password_change_kills_sessions(self):
        uid = SUPER['id']
        token, _ = self.store.create_session(uid)
        with self.assertRaises(InputError):
            self.store.change_own_password(uid, 'wrong', 'NewPassword123')
        self.store.change_own_password(uid, 'SuperSecret123', 'NewPassword123')
        self.assertIsNone(self.store.session_user(token))
        self.assertTrue(self.store.authenticate('root', 'NewPassword123'))

    def test_role_rules(self):
        admin_a = self.store.create_user({'username': 'adm', 'role': 'school_admin', 'school_id': self.a})['user']
        with self.assertRaises(Forbidden):
            self.store.create_user({'username': 'xx1', 'role': 'school_admin', 'school_id': self.a}, admin_a)
        with self.assertRaises(Forbidden):
            self.store.create_user({'username': 'xx2', 'role': 'teacher', 'school_id': self.b}, admin_a)
        with self.assertRaises(Forbidden):
            self.store.create_user({'username': 'xx3', 'role': 'superadmin'}, admin_a)
        teacher = self.store.create_user({'username': 'tt1', 'role': 'teacher', 'school_id': self.a}, admin_a)['user']
        other = self.store.create_user({'username': 'tt2', 'role': 'teacher', 'school_id': self.b})['user']
        self.assertIn('password', self.store.update_user(teacher['id'], {'reset_password': True}, admin_a))
        with self.assertRaises(NotFound):
            self.store.update_user(other['id'], {'reset_password': True}, admin_a)

    def test_last_superadmin_protected(self):
        root = {'id': SUPER['id']}
        helper = {'id': 999, 'role': 'superadmin', 'username': 'h'}
        with self.assertRaises(InputError):
            self.store.delete_user(root['id'], helper)
        with self.assertRaises(InputError):
            self.store.update_user(root['id'], {'active': False}, helper)


class IsolationTests(Base):
    def test_cross_school_access_denied(self):
        sid = self.students['S1']
        pid = self.store.add_photo(self.a, sid, synthetic_avatar('x'))['id']
        with self.assertRaises(NotFound):
            self.store.photo_file(self.b, pid)
        with self.assertRaises(NotFound):
            self.store.delete_student(self.b, sid)
        with self.assertRaises(NotFound):
            self.store.delete_camera(self.b, self.cams['a_room'])
        with self.assertRaises(NotFound):
            self.store.attendance(self.b, self.cls, core.local_today())
        self.assertEqual(self.store.list_students(self.b), [])
        self.assertEqual(self.store.worker_roster(self.b)['students'], [])
        with self.assertRaisesRegex(InputError, 'Unknown camera'):
            self.store.ingest(self.b, {'camera_key': 'a_room', 'event_uid': 'x', 'event_type': 'motion'})
        with self.assertRaises(InputError):
            self.store.heartbeat(self.b, {'camera_key': 'a_room', 'ok': True})

    def test_device_tokens(self):
        raw = self.store.create_token(self.a, {'name': 'worker'})['token']
        self.assertEqual(self.store.token_school(raw), self.a)
        self.assertIsNone(self.store.token_school(raw + 'x'))
        tid = self.store.list_tokens(self.a)[0]['id']
        with self.assertRaises(NotFound):
            self.store.revoke_token(self.b, tid)
        self.store.revoke_token(self.a, tid)
        self.assertIsNone(self.store.token_school(raw))
        with self.store.connect() as db:
            self.assertNotIn(raw, [r[0] for r in db.execute('SELECT token_hash FROM api_tokens')])


class PhotoTests(Base):
    def test_photo_validation_and_cleanup(self):
        sid = self.students['S1']
        with self.assertRaisesRegex(InputError, 'JPEG'):
            self.store.add_photo(self.a, sid, b'<svg onload=alert(1)>')
        with self.assertRaises(InputError):
            self.store.add_photo(self.a, sid, b'\xff\xd8\xff' + b'0' * core.MAX_PHOTO_BYTES)
        for i in range(core.MAX_PHOTOS_PER_STUDENT):
            self.store.add_photo(self.a, sid, synthetic_avatar(str(i)))
        with self.assertRaisesRegex(InputError, 'Не более'):
            self.store.add_photo(self.a, sid, synthetic_avatar('extra'))
        folder = self.store.photo_dir / str(self.a)
        self.assertEqual(len(list(folder.iterdir())), core.MAX_PHOTOS_PER_STUDENT)
        self.store.delete_student(self.a, sid)
        self.assertEqual(list(folder.iterdir()), [])

    def test_delete_school_removes_photos(self):
        self.store.add_photo(self.a, self.students['S1'], synthetic_avatar('z'))
        self.store.delete_school(self.a)
        self.assertFalse((self.store.photo_dir / str(self.a)).exists())

    def test_auto_learned_photos(self):
        sid = self.students['S1']
        for i in range(core.MAX_PHOTOS_PER_STUDENT):
            self.store.add_photo(self.a, sid, synthetic_avatar(f'm{i}'))      # manual quota full ...
        r = self.store.add_auto_photo(self.a, 'S1', synthetic_avatar('auto0'))
        self.assertTrue(r['stored'])                                          # ... camera-learned photos have their own
        self.assertEqual(self.store.add_auto_photo(self.a, 'S1', synthetic_avatar('auto1'))['reason'], 'already_today')
        with self.store.connect() as db:                                      # pretend earlier days
            db.execute("UPDATE photos SET created_at='2026-01-01T08:00:00+00:00' WHERE source='auto'")
        for i in range(1, 4):
            self.assertTrue(self.store.add_auto_photo(self.a, 'S1', synthetic_avatar(f'auto{i}'))['stored'])
            with self.store.connect() as db:
                db.execute("UPDATE photos SET created_at='2026-01-01T08:00:00+00:00' WHERE source='auto'")
        autos = [p for p in self.store.list_students(self.a)[0]['photos'] if p['source'] == 'auto']
        self.assertEqual(len(autos), core.MAX_AUTO_PHOTOS)                     # rotated, never grows
        self.assertNotIn(r['id'], [p['id'] for p in autos])                   # the oldest one was replaced
        files = list((self.store.photo_dir / str(self.a)).iterdir())
        self.assertEqual(len(files), core.MAX_PHOTOS_PER_STUDENT + core.MAX_AUTO_PHOTOS)
        self.store.update_school(self.a, {'auto_learn': False}, SUPER)
        self.assertEqual(self.store.add_auto_photo(self.a, 'S1', synthetic_avatar('x'))['reason'], 'disabled')
        with self.assertRaises(NotFound):
            self.store.add_auto_photo(self.b, 'S1', synthetic_avatar('y'))   # other school's token

    def test_quality_status(self):
        pid = self.store.add_photo(self.a, self.students['S1'], synthetic_avatar('q'))['id']
        self.store.set_photo_quality(self.a, pid, 'ok')
        self.assertTrue(self.store.list_students(self.a)[0]['ai_ready'] or self.store.list_students(self.a)[1]['ai_ready'])
        with self.assertRaises(InputError):
            self.store.set_photo_quality(self.a, pid, 'hacked')


class IngestTests(Base):
    def test_rules(self):
        now = datetime.now(timezone.utc)
        r = self.store.ingest(self.a, {'camera_key': 'a_gate', 'event_uid': 'g1', 'event_type': 'access_granted',
                                       'verification_method': 'card', 'person_code': 'S1', 'observed_at': now.isoformat()})
        self.assertTrue(r['verified'])
        self.assertTrue(self.store.ingest(self.a, {'camera_key': 'a_gate', 'event_uid': 'g1', 'event_type': 'access_granted',
                                                   'verification_method': 'card', 'person_code': 'S1'})['duplicate'])
        self.assertEqual(self.face('S1', now, 0.6)['review_status'], 'auto')
        self.assertEqual(self.face('S2', now, 0.40)['review_status'], 'pending')
        self.assertFalse(self.face('S3', now, 0.2)['stored'])
        with self.assertRaises(InputError):
            self.face('S1', now, 0.9, cam='a_hall')
        with self.assertRaises(InputError):
            self.face('NOPE', now, 0.9)
        m = self.store.ingest(self.a, {'camera_key': 'a_hall', 'event_uid': 'm1', 'event_type': 'motion', 'person_code': 'S1',
                                       'verification_method': 'card'})
        self.assertFalse(m['verified'])
        ev = [e for e in self.store.list_events(self.a, core.local_today()) if e['event_type'] == 'motion'][0]
        self.assertIsNone(ev['student_code'])
        with self.assertRaises(InputError):
            self.store.ingest(self.a, {'camera_key': 'a_gate', 'event_uid': 'old', 'event_type': 'motion',
                                       'observed_at': '2020-01-01T00:00:00+00:00'})
        with self.assertRaises(InputError):
            self.store.ingest(self.a, {'camera_key': 'a_gate', 'event_uid': 'naive', 'event_type': 'motion',
                                       'observed_at': '2026-01-01T00:00:00'})

    def test_second_model_opinion(self):
        now = datetime.now(timezone.utc)
        base = {'camera_key': 'a_room', 'event_type': 'face_match', 'verification_method': 'ai_face',
                'person_code': 'S1', 'observed_at': now.isoformat()}
        # borderline main score, second model agrees -> accepted automatically
        r = self.store.ingest(self.a, {**base, 'event_uid': 's1', 'confidence': 0.40, 'second_score': 0.55})
        self.assertEqual((r['verified'], r['review_status']), (True, 'auto'))
        # second model disagrees -> dropped even with a high main score
        r = self.store.ingest(self.a, {**base, 'event_uid': 's2', 'confidence': 0.70, 'second_score': 0.10})
        self.assertEqual(r, {'stored': False, 'reason': 'second_model_reject'})
        # second model unsure -> old rules
        r = self.store.ingest(self.a, {**base, 'event_uid': 's3', 'confidence': 0.40, 'second_score': 0.30})
        self.assertEqual(r['review_status'], 'pending')
        with self.assertRaises(InputError):
            self.store.ingest(self.a, {**base, 'event_uid': 's4', 'confidence': 0.40, 'second_score': 1.5})
        # anti-spoofing: even a very confident match goes to a human if it may be a photo / phone screen
        r = self.store.ingest(self.a, {**base, 'event_uid': 's5', 'confidence': 0.90, 'second_score': 0.80, 'liveness': 0.2})
        self.assertEqual((r['verified'], r['review_status']), (False, 'pending'))
        r = self.store.ingest(self.a, {**base, 'event_uid': 's6', 'confidence': 0.90, 'liveness': 0.97})
        self.assertEqual((r['verified'], r['review_status']), (True, 'auto'))
        ev = {e['id']: e for e in self.store.list_events(self.a, core.local_today())}
        self.assertEqual(sorted(e['second_score'] for e in ev.values() if e['second_score'] is not None), [0.30, 0.55, 0.80])
        self.assertEqual([r['liveness'] for r in self.store.list_reviews(self.a) if r['liveness'] is not None], [0.2])

    def test_review_flow(self):
        eid = self.face('S2', datetime.now(timezone.utc), 0.40)['id']
        self.assertEqual(len(self.store.list_reviews(self.a)), 1)
        with self.assertRaises(NotFound):
            self.store.review_event(self.b, eid, 'confirm')
        self.store.review_event(self.a, eid, 'confirm', SUPER)
        self.assertEqual(self.store.list_reviews(self.a), [])
        with self.assertRaises(NotFound):
            self.store.review_event(self.a, eid, 'reject')


class AttendanceTests(Base):
    def test_lesson_statuses(self):
        day = datetime.now(TZ).date() - timedelta(days=1)
        wd = day.isoweekday()
        l1 = self.store.create_lesson(self.a, {'class_id': self.cls, 'weekday': wd, 'start_time': '10:00', 'end_time': '10:45',
                                               'subject': 'Math', 'camera_id': self.cams['a_room']})['id']
        l2 = self.store.create_lesson(self.a, {'class_id': self.cls, 'weekday': wd, 'start_time': '11:00', 'end_time': '11:45',
                                               'subject': 'Art', 'camera_id': self.cams['a_room2']})['id']
        l3 = self.store.create_lesson(self.a, {'class_id': self.cls, 'weekday': wd, 'start_time': '12:00', 'end_time': '12:45',
                                               'subject': 'PE'})['id']
        with self.assertRaisesRegex(InputError, 'Пересекается'):
            self.store.create_lesson(self.a, {'class_id': self.cls, 'weekday': wd, 'start_time': '10:30', 'end_time': '11:10', 'subject': 'X'})
        at = lambda hm: datetime.combine(day, datetime.strptime(hm, '%H:%M').time(), TZ)
        # classroom camera 1 worked the whole lesson; camera 2 was dead
        with self.store.connect() as db:
            t = at('10:00')
            while t < at('10:45'):
                db.execute('INSERT INTO camera_uptime(camera_id,minute) VALUES(?,?)', (self.cams['a_room'], minute_key(t)))
                t += timedelta(minutes=1)
        self.face('S1', at('09:55'), 0.7)           # before start (within pre-window) -> present
        self.face('S2', at('10:20'), 0.7)           # after grace -> late
        self.face('S3', at('10:05'), 0.40)          # low confidence -> needs review
        self.store.ingest(self.a, {'camera_key': 'a_gate', 'event_uid': 'gate-s1', 'event_type': 'access_granted',
                                   'verification_method': 'card', 'person_code': 'S1', 'observed_at': at('07:50').isoformat()})
        self.store.ingest(self.a, {'camera_key': 'a_gate', 'event_uid': 'gate-s2', 'event_type': 'access_granted',
                                   'verification_method': 'card', 'person_code': 'S2', 'observed_at': at('12:20').isoformat()})
        grid = self.store.attendance(self.a, self.cls, day.isoformat())
        cell = lambda code, lesson: next(r for r in grid['rows'] if r['student']['code'] == code)['cells'][str(lesson)]['status']
        self.assertEqual(cell('S1', l1), 'present')
        self.assertEqual(cell('S2', l1), 'late')
        self.assertEqual(cell('S3', l1), 'needs_review')
        self.assertEqual(cell('S4', l1), 'absent')      # camera worked all lesson, child never seen -> automatic absent
        self.assertEqual(cell('S4', l2), 'no_data')     # camera offline -> never guessed
        self.assertEqual(cell('S1', l3), 'present')     # no classroom camera -> school entry
        self.assertEqual(cell('S2', l3), 'late')
        self.assertEqual(cell('S4', l3), 'absent')      # turnstile worked that day, child never passed
        self.store.set_mark(self.a, {'student_id': self.students['S4'], 'lesson_id': l2, 'day': day.isoformat(), 'status': 'excused'}, SUPER)
        grid = self.store.attendance(self.a, self.cls, day.isoformat())
        self.assertEqual(cell('S4', l2), 'excused')
        self.store.set_mark(self.a, {'student_id': self.students['S4'], 'lesson_id': l2, 'day': day.isoformat(), 'status': 'clear'}, SUPER)
        grid = self.store.attendance(self.a, self.cls, day.isoformat())
        self.assertEqual(cell('S4', l2), 'no_data')
        # two medium-confidence sightings in one lesson are accepted without a human
        self.face('S3', at('10:12'), 0.41)
        grid = self.store.attendance(self.a, self.cls, day.isoformat())
        self.assertEqual(cell('S3', l1), 'present')
        # schools that prefer human confirmation can switch automatic absence off
        self.store.update_school(self.a, {'auto_absent': False}, SUPER)
        grid = self.store.attendance(self.a, self.cls, day.isoformat())
        self.assertEqual(cell('S4', l1), 'not_seen')
        self.assertEqual(cell('S4', l3), 'not_seen')

    def test_worker_schedule_and_idle_heartbeat(self):
        wd = datetime.now(TZ).isoweekday()
        self.store.create_lesson(self.a, {'class_id': self.cls, 'weekday': wd, 'start_time': '09:00', 'end_time': '09:45',
                                          'subject': 'A', 'camera_id': self.cams['a_room']})
        self.store.create_lesson(self.a, {'class_id': self.cls, 'weekday': wd, 'start_time': '09:50', 'end_time': '10:35',
                                          'subject': 'B', 'camera_id': self.cams['a_room']})
        w = self.store.worker_schedule(self.a)['windows']
        self.assertEqual(len(w['a_room']), 1)           # 08:50-09:45 and 09:40-10:35 merge into one window
        self.assertEqual(w['a_room2'], [])               # no lessons in that room -> camera stays off
        self.assertEqual(w['a_hall'], [])                # observe cameras are never processed
        self.assertEqual(len(w['a_gate']), 1)            # entry: arrival window around the school day
        self.assertEqual(self.store.worker_schedule(self.b)['windows'], {})
        self.store.heartbeat(self.a, {'camera_key': 'a_room', 'ok': True, 'idle': True})
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM camera_uptime').fetchone()[0], 0)
        cams = {c['camera_key']: c for c in self.store.list_cameras(self.a)}
        self.assertTrue(cams['a_room']['online'])        # idle worker is still reported alive in the panel
        self.store.heartbeat(self.a, {'camera_key': 'a_room', 'ok': True})
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM camera_uptime').fetchone()[0], 1)

    def test_migration_adds_auto_absent(self):
        import sqlite3
        path = Path(self.tmp.name) / 'old.sqlite3'
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE schools (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, city TEXT NOT NULL DEFAULT '', "
                     "late_grace_min INTEGER NOT NULL DEFAULT 10, face_auto_threshold REAL NOT NULL DEFAULT 0.45, "
                     "face_review_threshold REAL NOT NULL DEFAULT 0.363, created_at TEXT NOT NULL)")
        conn.execute("INSERT INTO schools(name, created_at) VALUES('Old', '2026-01-01')")
        conn.commit()
        conn.close()
        old = Store(path)
        self.assertEqual(old.list_schools()[0]['auto_absent'], 1)
        old.close()

    def test_mark_validation(self):
        today = datetime.now(TZ).date()
        lid = self.store.create_lesson(self.a, {'class_id': self.cls, 'weekday': today.isoweekday(), 'start_time': '08:00',
                                                'end_time': '08:45', 'subject': 'Math'})['id']
        payload = {'student_id': self.students['S1'], 'lesson_id': lid, 'status': 'present'}
        with self.assertRaisesRegex(InputError, 'будущие'):
            self.store.set_mark(self.a, {**payload, 'day': (today + timedelta(days=7)).isoformat()})
        with self.assertRaisesRegex(InputError, 'не проходит'):
            self.store.set_mark(self.a, {**payload, 'day': (today - timedelta(days=1)).isoformat()})
        with self.assertRaises(InputError):
            self.store.set_mark(self.a, {**payload, 'day': today.isoformat(), 'status': 'teleported'})
        with self.assertRaises(NotFound):
            self.store.set_mark(self.b, {**payload, 'day': today.isoformat()})

    def test_overview(self):
        self.face('S1', datetime.now(timezone.utc), 0.8)
        o = self.store.overview(self.a)
        self.assertEqual(o['stats']['students'], 4)
        self.assertEqual(o['stats']['seen_today'], 1)
        self.assertEqual(o['stats']['no_photo'], 4)

    def test_purge(self):
        self.face('S1', datetime.now(timezone.utc) - timedelta(days=20), 0.8)
        self.face('S1', datetime.now(timezone.utc), 0.8, uid='fresh')
        self.assertEqual(self.store.purge(10)['events'], 1)


class TimezoneTests(unittest.TestCase):
    def test_tz_available(self):
        self.assertEqual(datetime(2026, 1, 1, tzinfo=timezone.utc).astimezone(TZ).hour, 5)


if __name__ == '__main__':
    unittest.main()
