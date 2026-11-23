import base64
import http.client
import io
import json
import sys
import tempfile
import threading
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import core  # noqa: E402
import imports  # noqa: E402
from app import ApiServer  # noqa: E402
from core import TZ, AuthError, Forbidden, InputError, NotFound, Store  # noqa: E402
from manage import synthetic_avatar  # noqa: E402

core.PBKDF2_ITERATIONS = 1000


def make_xlsx(rows):
    """Minimal .xlsx (shared strings + one sheet) like Excel/Google Sheets produce."""
    strings, idx = [], {}
    sheet_rows = []
    for r, row in enumerate(rows, start=1):
        cells = []
        for c, value in enumerate(row):
            ref = chr(65 + c) + str(r)
            if isinstance(value, (int, float)):
                cells.append(f'<c r="{ref}"><v>{value}</v></c>')
            else:
                if value not in idx:
                    idx[value] = len(strings)
                    strings.append(value)
                cells.append(f'<c r="{ref}" t="s"><v>{idx[value]}</v></c>')
        sheet_rows.append(f'<row r="{r}">{"".join(cells)}</row>')
    ns = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        zf.writestr('xl/sharedStrings.xml', f'<sst xmlns="{ns}">' + ''.join(f'<si><t>{s}</t></si>' for s in strings) + '</sst>')
        zf.writestr('xl/worksheets/sheet1.xml', f'<worksheet xmlns="{ns}"><sheetData>{"".join(sheet_rows)}</sheetData></worksheet>')
    return buf.getvalue()


def make_zip(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / 'db.sqlite3')
        self.root = self.store.create_user({'username': 'root', 'role': 'superadmin', 'password': 'SuperSecret123'})['user']
        self.sid = self.store.create_school({'name': 'A'})['id']
        self.other = self.store.create_school({'name': 'B'})['id']

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()


class InviteTests(Base):
    def test_invite_lifecycle(self):
        inv = self.store.create_invite({'role': 'school_admin', 'school_id': self.sid, 'full_name': 'Директор'}, self.root)
        info = self.store.invite_info(inv['token'])
        self.assertEqual((info['role'], info['school_name']), ('school_admin', 'A'))
        with self.assertRaises(InputError):
            self.store.accept_invite(inv['token'], {'username': 'director', 'password': 'short'})
        user = self.store.accept_invite(inv['token'], {'username': 'director', 'password': 'DirectorPass1'})
        self.assertEqual((user['role'], user['school_id']), ('school_admin', self.sid))
        self.assertEqual(self.store.authenticate('director', 'DirectorPass1')['id'], user['id'])
        with self.assertRaises(NotFound):          # one-time
            self.store.accept_invite(inv['token'], {'username': 'director2', 'password': 'DirectorPass1'})
        with self.assertRaises(NotFound):
            self.store.invite_info('inv_forged')
        with self.store.connect() as db:            # nothing secret stored in clear text
            self.assertNotIn(inv['token'], [r[0] for r in db.execute('SELECT token_hash FROM invites')])

    def test_invite_permissions_and_expiry(self):
        admin = self.store.create_user({'username': 'adm', 'role': 'school_admin', 'school_id': self.sid})['user']
        with self.assertRaises(Forbidden):
            self.store.create_invite({'role': 'school_admin', 'school_id': self.sid, 'full_name': 'X'}, admin)
        with self.assertRaises(Forbidden):
            self.store.create_invite({'role': 'teacher', 'school_id': self.other, 'full_name': 'X'}, admin)
        inv = self.store.create_invite({'role': 'teacher', 'school_id': self.sid, 'full_name': 'Учитель'}, admin)
        with self.store.connect() as db:
            db.execute('UPDATE invites SET expires_at=?', ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),))
        with self.assertRaises(NotFound):
            self.store.accept_invite(inv['token'], {'username': 'tch', 'password': 'TeacherPass1'})

    def test_reset_link(self):
        t = self.store.create_user({'username': 'tch', 'role': 'teacher', 'school_id': self.sid, 'password': 'OldPassword1'})['user']
        token, _ = self.store.create_session(t['id'])
        inv = self.store.create_reset_invite(t['id'], self.root)
        self.assertEqual(self.store.invite_info(inv['token'])['username'], 'tch')
        self.store.accept_invite(inv['token'], {'password': 'NewPassword1'})
        self.assertIsNone(self.store.session_user(token))            # old sessions killed
        with self.assertRaises(AuthError):
            self.store.authenticate('tch', 'OldPassword1')
        self.assertTrue(self.store.authenticate('tch', 'NewPassword1'))


class ApplicationTests(Base):
    def test_submit_and_approve(self):
        with self.assertRaises(InputError):
            self.store.submit_application({'school_name': 'Школа 7', 'contact_name': 'Иван'})          # no contact
        with self.assertRaises(InputError):
            self.store.submit_application({'school_name': 'Школа 7', 'contact_name': 'Иван', 'phone': 'call me'})
        self.store.submit_application({'school_name': 'Spam', 'contact_name': 'Bot', 'phone': '1', 'website': 'x'})
        self.assertEqual(self.store.list_applications(), [])                                          # honeypot
        self.store.submit_application({'school_name': 'Школа 7', 'contact_name': 'Иван', 'phone': '+998 90 123-45-67',
                                       'city': 'Ташкент', 'students_estimate': '800'})
        aid = self.store.list_applications()[0]['id']
        res = self.store.decide_application(aid, 'approve', self.root)
        self.assertEqual(res['school']['name'], 'Школа 7')
        self.assertEqual(self.store.invite_info(res['invite']['token'])['role'], 'school_admin')
        with self.assertRaises(NotFound):
            self.store.decide_application(aid, 'approve', self.root)
        self.store.submit_application({'school_name': 'Школа 7', 'contact_name': 'Пётр', 'email': 'p@school.uz'})
        aid2 = self.store.list_applications()[0]['id']
        self.assertNotEqual(self.store.decide_application(aid2, 'approve', self.root)['school']['name'], 'Школа 7')


class ImportTests(Base):
    def test_csv_aliases_and_apply(self):
        data = 'Фамилия;Имя;Класс;Код\nКаримов;Азиз;5-А;K1\nЮсупова;Мадина;5-Б;\n;;;\nБезКласса;Иван;;\n'.encode('cp1251')
        rows, errors = imports.parse_students('list.csv', data)
        self.assertEqual([r['full_name'] for r in rows], ['Каримов Азиз', 'Юсупова Мадина'])
        self.assertEqual(errors, [{'row': 5, 'error': 'нет класса'}])
        preview = self.store.import_students(self.sid, rows, self.root, dry_run=True)
        self.assertEqual((preview['create'], preview['new_classes']), (2, ['5-А', '5-Б']))
        self.assertEqual(self.store.list_students(self.sid), [])                                  # dry run writes nothing
        with self.assertRaises(InputError):
            self.store.import_students(self.sid, rows, self.root, dry_run=False, consent=False)
        self.store.import_students(self.sid, rows, self.root, dry_run=False, consent=True)
        students = {s['code']: s for s in self.store.list_students(self.sid)}
        self.assertIn('K1', students)
        self.assertEqual(len(students), 2)
        # second import: rename + move -> update, same -> untouched
        rows2, _ = imports.parse_students('x.csv', 'код,фио,класс\nK1,Каримов Азиз Б.,5-Б\n'.encode())
        res = self.store.import_students(self.sid, rows2, self.root, dry_run=False, consent=True)
        self.assertEqual(res['update'], 1)
        self.assertEqual(self.store.list_students(self.sid, None)[0]['class_name'], '5-Б')

    def test_xlsx_and_errors(self):
        data = make_xlsx([['Список учеников 2026'], ['ФИО', 'Класс', 'ID'], ['Алиев Тимур', '9-А', 1001],
                          ['Ахмедова Лола', '9-А', 1001], ['Bad Code', '9-А', '../x']])
        rows, errors = imports.parse_students('students.xlsx', data)
        self.assertEqual(len(rows), 3)
        res = self.store.import_students(self.sid, rows, self.root, dry_run=True)
        self.assertEqual(res['create'], 1)
        self.assertEqual(len(res['errors']), 2)                    # duplicate code + illegal code
        with self.assertRaises(imports.ImportError_):
            imports.parse_students('a.csv', 'x;y\n1;2\n'.encode())
        with self.assertRaises(imports.ImportError_):
            imports.parse_students('a.xlsx', b'PK\x03\x04garbage')

    def test_photo_zip(self):
        cid = self.store.create_class(self.sid, {'name': '1'})['id']
        for code in ('K1', 'K2', 'K3'):
            self.store.create_student(self.sid, {'code': code, 'full_name': code, 'class_id': cid, 'consent_confirmed': True})
        a, b, c = synthetic_avatar('a'), synthetic_avatar('b'), synthetic_avatar('c')
        z = make_zip({'K1.png': a, 'K1_2.png': b, 'photos/K2/front.png': c, 'K2.png': a, 'NOPE.png': c,
                      'readme.txt': b'hi', '__MACOSX/K3.png': a, 'K3.png': b'not an image at all'})
        res = self.store.import_photos(self.sid, imports.photo_entries(z), self.root)
        self.assertEqual(res['added'], 3)                           # K1 x2, K2 folder
        self.assertEqual(res['duplicates'], 1)                      # K2.png == already-imported bytes of K1? no: same school sha
        self.assertEqual(res['unknown_code'], ['NOPE.png'])
        self.assertEqual(len(res['rejected']), 1)                   # K3 not an image
        with self.assertRaises(imports.ImportError_):
            list(imports.photo_entries(b'not a zip'))


class BulkTests(Base):
    def test_bulk_move_delete_copy_export(self):
        c1 = self.store.create_class(self.sid, {'name': '1'})['id']
        c2 = self.store.create_class(self.sid, {'name': '2'})['id']
        ids = [self.store.create_student(self.sid, {'code': f'K{i}', 'full_name': f'S{i}', 'class_id': c1,
                                                    'consent_confirmed': True})['id'] for i in range(3)]
        foreign_cls = self.store.create_class(self.other, {'name': 'X'})['id']
        with self.assertRaises(NotFound):
            self.store.bulk_students(self.sid, {'student_ids': ids, 'action': 'move', 'class_id': foreign_cls})
        self.assertEqual(self.store.bulk_students(self.sid, {'student_ids': ids[:2], 'action': 'move', 'class_id': c2})['moved'], 2)
        self.assertEqual(self.store.bulk_students(self.other, {'student_ids': ids, 'action': 'delete'})['deleted'], 0)
        self.assertEqual(self.store.bulk_students(self.sid, {'student_ids': [ids[2]], 'action': 'delete'})['deleted'], 1)
        self.store.create_lesson(self.sid, {'class_id': c2, 'weekday': 1, 'start_time': '08:00', 'end_time': '08:45', 'subject': 'M'})
        self.store.create_lesson(self.sid, {'class_id': c2, 'weekday': 1, 'start_time': '09:00', 'end_time': '09:45', 'subject': 'R'})
        self.store.create_lesson(self.sid, {'class_id': c2, 'weekday': 2, 'start_time': '12:00', 'end_time': '12:45', 'subject': 'Old'})
        res = self.store.copy_schedule_day(self.sid, {'class_id': c2, 'from_weekday': 1, 'to_weekdays': [2, 3, 4, 5, 6]})
        self.assertEqual(res['copied_to'], [2, 3, 4, 5, 6])
        lessons = self.store.list_lessons(self.sid, c2)
        self.assertEqual(len(lessons), 12)
        self.assertNotIn('Old', [l['subject'] for l in lessons])
        today = datetime.now(TZ).date()
        header, rows = self.store.export_attendance(self.sid, (today - timedelta(days=6)).isoformat(), today.isoformat(), c2)
        self.assertEqual(header[0], 'Дата')
        self.assertEqual(len(rows), 2 * 2 * sum(1 for d in range(7) if 1 <= (today - timedelta(days=d)).isoweekday() <= 6))
        with self.assertRaises(InputError):
            self.store.export_attendance(self.sid, '2026-01-01', '2026-12-31')
        self.assertTrue(imports.csv_bytes(header, rows).startswith('﻿'.encode()))


class HttpOnboardingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        s = cls.store = Store(Path(cls.tmp.name) / 't.sqlite3')
        s.create_user({'username': 'root', 'role': 'superadmin', 'password': 'test-fixture-pass-1'})
        cls.sid = s.create_school({'name': 'A'})['id']
        cls.server = ApiServer(('127.0.0.1', 0), s)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.store.close()
        cls.tmp.cleanup()

    def req(self, route, method='GET', body=None, headers=None, raw=None):
        c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        h = dict(headers or {})
        payload = raw
        if body is not None:
            payload = json.dumps(body).encode()
            h['Content-Type'] = 'application/json'
        c.request(method, route, payload, h)
        r = c.getresponse()
        out = (r.status, {k.lower(): v for k, v in r.getheaders()}, r.read())
        c.close()
        return out

    def login(self):
        st, hd, body = self.req('/api/login', 'POST', {'username': 'root', 'password': 'test-fixture-pass-1'})
        return {'Cookie': hd['set-cookie'].split(';')[0], 'X-CSRF-Token': json.loads(body)['csrf']}

    def test_invite_flow_logs_in(self):
        auth = self.login()
        st, _, body = self.req('/api/invites', 'POST', {'role': 'teacher', 'school_id': self.sid, 'full_name': 'T'}, auth)
        token = json.loads(body)['token']
        st, hd, body = self.req('/api/invites/accept', 'POST', {'token': token, 'username': 'newteacher', 'password': 'TeacherPass1'})
        self.assertEqual(st, 200, body)
        self.assertIn('HttpOnly', hd['set-cookie'])
        self.assertEqual(self.req('/api/invites/info', 'POST', {'token': token})[0], 404)

    def test_application_rate_limit(self):
        self.server.form_throttle.failures.clear()
        codes = [self.req('/api/applications', 'POST', {'school_name': f'S{i}', 'contact_name': 'C', 'phone': '+998901234567'})[0]
                 for i in range(7)]
        self.assertEqual(codes[:5], [200] * 5)
        self.assertEqual(codes[-1], 429)
        self.server.form_throttle.failures.clear()
        auth = self.login()
        self.assertEqual(len(json.loads(self.req('/api/applications', headers=auth)[2])['applications']), 5)
        self.assertEqual(self.req('/api/applications')[0], 401)

    def test_import_endpoints(self):
        auth = self.login()
        csv_data = base64.b64encode('фио;класс;код\nИванов Иван;3-В;Z1\n'.encode()).decode()
        st, _, body = self.req(f'/api/s/{self.sid}/import/students', 'POST', {'filename': 'a.csv', 'data_base64': csv_data,
                                                                             'dry_run': False, 'consent_confirmed': True}, auth)
        self.assertEqual(st, 200, body)
        z = make_zip({'Z1.png': synthetic_avatar('z')})
        st, _, body = self.req(f'/api/s/{self.sid}/import/photos', 'POST', None, {**auth, 'Content-Type': 'application/zip'}, raw=z)
        self.assertEqual(json.loads(body)['added'], 1)
        st, hd, body = self.req(f'/api/s/{self.sid}/export/attendance.csv', headers=auth)
        self.assertEqual(st, 200)
        self.assertIn('attachment', hd['content-disposition'])
        st, _, _ = self.req(f'/api/s/{self.sid}/import/photos', 'POST', None, {**auth, 'Content-Type': 'application/zip'},
                            raw=b'x' * 10)
        self.assertEqual(st, 400)


if __name__ == '__main__':
    unittest.main()
