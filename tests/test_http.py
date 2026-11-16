import base64
import http.client
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import core  # noqa: E402
from app import ApiServer  # noqa: E402
from core import Store  # noqa: E402
from manage import synthetic_avatar  # noqa: E402

core.PBKDF2_ITERATIONS = 1000


class HttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        s = cls.store = Store(Path(cls.tmp.name) / 'test.sqlite3')
        s.create_user({'username': 'root', 'role': 'superadmin', 'password': 'test-fixture-pass-1'})
        cls.a = s.create_school({'name': 'A'})['id']
        cls.b = s.create_school({'name': 'B'})['id']
        s.create_user({'username': 'teach_a', 'role': 'teacher', 'school_id': cls.a, 'password': 'TeacherPass123'})
        s.create_user({'username': 'admin_b', 'role': 'school_admin', 'school_id': cls.b, 'password': 'AdminPass1234'})
        cls.cls_a = s.create_class(cls.a, {'name': '1-A'})['id']
        cls.st_a = s.create_student(cls.a, {'code': 'A1', 'full_name': 'Kid A', 'class_id': cls.cls_a, 'consent_confirmed': True})['id']
        cls.photo_a = s.add_photo(cls.a, cls.st_a, synthetic_avatar('a'))['id']
        s.create_camera(cls.a, {'camera_key': 'a_room', 'name': 'Room', 'room': '1', 'kind': 'classroom'})
        cls.token_a = s.create_token(cls.a, {'name': 'w'})['token']
        cls.server = ApiServer(('127.0.0.1', 0), s)
        cls.server.throttle.limit = 6
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(2)
        cls.store.close()
        cls.tmp.cleanup()

    def request(self, route, method='GET', body=None, headers=None, raw=None):
        c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        h = dict(headers or {})
        payload = raw
        if body is not None:
            payload = json.dumps(body).encode()
            h.setdefault('Content-Type', 'application/json')
        c.request(method, route, payload, h)
        r = c.getresponse()
        out = (r.status, {k.lower(): v for k, v in r.getheaders()}, r.read())
        c.close()
        return out

    def login(self, user, password):
        status, headers, body = self.request('/api/login', 'POST', {'username': user, 'password': password})
        self.assertEqual(status, 200, body)
        cookie = headers['set-cookie'].split(';')[0]
        return {'Cookie': cookie, 'X-CSRF-Token': json.loads(body)['csrf']}, headers['set-cookie']

    def test_login_cookie_flags_and_logout(self):
        auth, raw_cookie = self.login('root', 'test-fixture-pass-1')
        for flag in ('HttpOnly', 'SameSite=Strict', 'Path=/'):
            self.assertIn(flag, raw_cookie)
        status, _, body = self.request('/api/me', headers=auth)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['user']['role'], 'superadmin')
        self.assertEqual(self.request('/api/logout', 'POST', {}, auth)[0], 200)
        self.assertIsNone(json.loads(self.request('/api/me', headers=auth)[2])['user'])
        self.assertEqual(self.request('/api/schools', headers=auth)[0], 401)

    def test_unauthenticated(self):
        self.assertEqual(self.request('/api/schools')[0], 401)
        self.assertEqual(self.request(f'/api/s/{self.a}/students')[0], 401)
        self.assertEqual(self.request(f'/api/s/{self.a}/photos/{self.photo_a}')[0], 401)
        status, _, body = self.request('/api/login', 'POST', {'username': 'root', 'password': 'nope-nope-nope'})
        self.assertEqual(status, 401)
        self.assertNotIn(b'Traceback', body)

    def test_csrf_required_for_mutations(self):
        auth, _ = self.login('root', 'test-fixture-pass-1')
        no_csrf = {'Cookie': auth['Cookie']}
        self.assertEqual(self.request('/api/schools', 'POST', {'name': 'X'}, no_csrf)[0], 403)
        bad = {**no_csrf, 'X-CSRF-Token': 'forged'}
        self.assertEqual(self.request('/api/schools', 'POST', {'name': 'X'}, bad)[0], 403)

    def test_teacher_scope(self):
        auth, _ = self.login('teach_a', 'TeacherPass123')
        self.assertEqual(self.request(f'/api/s/{self.a}/students', headers=auth)[0], 200)
        self.assertEqual(self.request(f'/api/s/{self.b}/students', headers=auth)[0], 404)
        self.assertEqual(self.request(f'/api/s/{self.a}/students', 'POST', {'code': 'Z'}, auth)[0], 403)
        self.assertEqual(self.request(f'/api/s/{self.a}/tokens', headers=auth)[0], 403)
        self.assertEqual(self.request('/api/users', headers=auth)[0], 403)
        self.assertEqual(self.request('/api/schools', 'POST', {'name': 'Z'}, auth)[0], 403)
        status, _, body = self.request('/api/schools', headers=auth)
        self.assertEqual([s['id'] for s in json.loads(body)['schools']], [self.a])

    def test_school_admin_cannot_touch_other_school(self):
        auth, _ = self.login('admin_b', 'AdminPass1234')
        self.assertEqual(self.request(f'/api/s/{self.a}/photos/{self.photo_a}', headers=auth)[0], 404)
        self.assertEqual(self.request(f'/api/s/{self.b}/photos/{self.photo_a}', headers=auth)[0], 404)
        self.assertEqual(self.request(f'/api/s/{self.a}/students/{self.st_a}', 'DELETE', None, auth)[0], 404)
        self.assertEqual(self.request(f'/api/s/{self.b}/students/{self.st_a}', 'DELETE', None, auth)[0], 404)
        status, _, body = self.request('/api/users', headers=auth)
        self.assertTrue(all(u['school_id'] == self.b for u in json.loads(body)['users']))
        self.assertNotIn(b'password_hash', body)

    def test_photo_upload_and_download(self):
        auth, _ = self.login('root', 'test-fixture-pass-1')
        png = synthetic_avatar('upload')
        status, _, body = self.request(f'/api/s/{self.a}/students/{self.st_a}/photos', 'POST',
                                       {'data_base64': base64.b64encode(png).decode()}, auth)
        self.assertEqual(status, 200, body)
        pid = json.loads(body)['id']
        status, headers, data = self.request(f'/api/s/{self.a}/photos/{pid}', headers=auth)
        self.assertEqual((status, data), (200, png))
        self.assertEqual(headers['content-type'], 'image/png')
        self.assertEqual(headers['x-content-type-options'], 'nosniff')
        self.assertIn('no-store', headers['cache-control'])
        html = base64.b64encode(b'<html><script>alert(1)</script>').decode()
        status, _, _ = self.request(f'/api/s/{self.a}/students/{self.st_a}/photos', 'POST', {'data_base64': html}, auth)
        self.assertEqual(status, 400)

    def test_device_token_endpoints(self):
        tok = {'X-Ingest-Token': self.token_a}
        self.assertEqual(self.request('/api/worker/roster', headers={'X-Ingest-Token': 'cat_bad'})[0], 401)
        status, _, body = self.request('/api/worker/heartbeat', 'POST', {'camera_key': 'a_room', 'ok': True, 'frames': 10}, tok)
        self.assertEqual(status, 200, body)
        status, _, body = self.request('/api/ingest', 'POST', {'camera_key': 'a_room', 'event_uid': 'h1', 'event_type': 'face_match',
                                                              'verification_method': 'ai_face', 'person_code': 'A1', 'confidence': 0.7}, tok)
        self.assertTrue(json.loads(body)['verified'])
        # a session cookie is NOT a device token and vice versa
        auth, _ = self.login('root', 'test-fixture-pass-1')
        self.assertEqual(self.request('/api/ingest', 'POST', {'camera_key': 'a_room', 'event_uid': 'h2', 'event_type': 'motion'}, auth)[0], 401)
        self.assertEqual(self.request(f'/api/s/{self.a}/students', headers=tok)[0], 401)

    def test_security_headers(self):
        status, headers, body = self.request('/')
        self.assertEqual(status, 200)
        self.assertIn("script-src 'self'", headers['content-security-policy'])
        self.assertIn("frame-ancestors 'none'", headers['content-security-policy'])
        self.assertEqual(headers['x-frame-options'], 'DENY')
        status, headers, _ = self.request('/api/health')
        self.assertEqual(headers['cache-control'], 'no-store')
        self.assertNotIn('Python', headers.get('server', ''))

    def test_bad_requests(self):
        auth, _ = self.login('root', 'test-fixture-pass-1')
        self.assertEqual(self.request('/api/schools', 'POST', None, {**auth, 'Content-Type': 'text/plain'}, raw=b'{"name":"x"}')[0], 400)
        self.assertEqual(self.request('/api/schools', 'POST', None, {**auth, 'Content-Type': 'application/json'}, raw=b'{bad')[0], 400)
        big = b'{"name":"' + b'x' * 70000 + b'"}'
        self.assertEqual(self.request('/api/schools', 'POST', None, {**auth, 'Content-Type': 'application/json'}, raw=big)[0], 400)
        self.assertEqual(self.request('/api/nope', headers=auth)[0], 404)
        self.assertEqual(self.request('/api/schools', 'DELETE', None, auth)[0], 405)
        self.assertEqual(self.request('/../core.py')[0], 404)
        self.assertEqual(self.request(f'/api/s/{self.a}/attendance?class_id=abc', headers=auth)[0], 400)

    def test_login_throttle(self):
        server_throttle = self.server.throttle
        server_throttle.failures.clear()
        codes = [self.request('/api/login', 'POST', {'username': 'ghost', 'password': 'wrong-password'})[0] for _ in range(8)]
        self.assertEqual(codes[-1], 429)
        server_throttle.failures.clear()


if __name__ == '__main__':
    unittest.main()
