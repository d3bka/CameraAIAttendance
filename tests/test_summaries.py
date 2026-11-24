import json
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import core  # noqa: E402
import summaries  # noqa: E402
import telegram_bot  # noqa: E402
from core import TZ, Store, minute_key  # noqa: E402

core.PBKDF2_ITERATIONS = 1000


class SummaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        s = self.store = Store(Path(self.tmp.name) / 'db.sqlite3')
        self.sid = s.create_school({'name': 'Школа 9'})['id']
        s.create_camera(self.sid, {'camera_key': 'room', 'name': 'Каб. 9', 'room': '9', 'kind': 'classroom'})
        cam = s.list_cameras(self.sid)[0]['id']
        cid = s.create_class(self.sid, {'name': '6-В'})['id']
        for code, name in (('A', 'Азизов Азиз'), ('B', 'Бобурова Бахор'), ('C', 'Солиев Сардор')):
            s.create_student(self.sid, {'code': code, 'full_name': name, 'class_id': cid, 'consent_confirmed': True})
        for wd in range(1, 8):
            s.create_lesson(self.sid, {'class_id': cid, 'weekday': wd, 'start_time': '10:00', 'end_time': '10:45',
                                       'subject': 'Алгебра', 'camera_id': cam})
        school_days = [d for d in ((datetime.now(TZ) - timedelta(days=n)).date() for n in range(1, 9)) if d.isoweekday() != 7]
        self.day = school_days[0]
        # three school days of history: A always present, C never seen (camera fully working) -> repeated absence
        for back, d in enumerate(school_days[:3]):
            at = lambda hm, d=d: datetime.combine(d, datetime.strptime(hm, '%H:%M').time(), TZ)
            with s.connect() as db:
                t = at('10:00')
                while t < at('10:45'):
                    db.execute('INSERT OR IGNORE INTO camera_uptime(camera_id,minute) VALUES(?,?)', (cam, minute_key(t)))
                    t += timedelta(minutes=1)
            for code, hm in (('A', '10:02'), ('B', '10:25' if back == 0 else '10:01')):
                s.ingest(self.sid, {'camera_key': 'room', 'event_uid': f'{code}{back}', 'event_type': 'face_match',
                                    'verification_method': 'ai_face', 'person_code': code, 'confidence': 0.7,
                                    'observed_at': at(hm).isoformat()})

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_payload_is_pseudonymised(self):
        payload, names = summaries.collect(self.store, self.sid, self.day.isoformat())
        raw = json.dumps(payload, ensure_ascii=False)
        for real in ('Азизов', 'Бобурова', 'Солиев', 'Школа 9'):
            self.assertNotIn(real, raw)
        self.assertEqual(payload['посещаемость'], '67%')
        self.assertEqual(len(payload['опоздавшие']), 1)
        self.assertEqual(len(payload['не_пришли_совсем']), 1)
        rep = payload['повторяющиеся_пропуски_за_2_недели']
        self.assertEqual((len(rep), rep[0]['дней_пропущено']), (1, 2))
        self.assertEqual(names[rep[0]['ученик']], 'Солиев Сардор')
        self.assertEqual(summaries.restore_names(f"{rep[0]['ученик']} снова не пришёл; Ученик#99", names),
                         'Солиев Сардор снова не пришёл; Ученик#99')

    def test_generate_with_and_without_ai(self):
        day = self.day.isoformat()
        seen = {}

        def fake_llm(payload, key, model):
            seen['payload'] = payload
            absent = payload['не_пришли_совсем'][0]['ученик']
            return f'Посещаемость 67%. {absent} отсутствовал.'
        r = summaries.generate(self.store, self.sid, day, 'sk-test', llm=fake_llm)
        self.assertEqual(r['source'], 'deepseek')
        self.assertIn('Солиев Сардор отсутствовал', r['text'])
        self.assertEqual(self.store.get_summary(self.sid, day)['source'], 'deepseek')

        def broken_llm(payload, key, model):
            raise urllib.error.URLError('down')
        r = summaries.generate(self.store, self.sid, day, 'sk-test', llm=broken_llm)
        self.assertEqual(r['source'], 'template')
        self.assertIn('Солиев Сардор', r['text'])
        self.assertIn('Опоздали: 1', r['text'])
        self.assertEqual(summaries.generate(self.store, self.sid, day, None)['source'], 'template')
        self.store.update_school(self.sid, {'ai_summaries': False})
        self.assertEqual(summaries.generate(self.store, self.sid, day, 'sk-test', llm=fake_llm)['source'], 'template')

    def test_deepseek_request_shape(self):
        captured = {}

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps({'choices': [{'message': {'content': 'Всё хорошо.'}}]}).encode()

        def fake_urlopen(req, timeout):
            captured['url'], captured['auth'] = req.full_url, req.headers['Authorization']
            captured['body'] = json.loads(req.data)
            return Resp()
        with mock.patch.object(summaries.urllib.request, 'urlopen', fake_urlopen):
            self.assertEqual(summaries.deepseek_text({'x': 1}, 'sk-abc'), 'Всё хорошо.')
        self.assertEqual(captured['url'], 'https://api.deepseek.com/chat/completions')
        self.assertEqual(captured['auth'], 'Bearer sk-abc')
        self.assertEqual(captured['body']['model'], 'deepseek-flash')

    def test_daily_summary_sent_once_after_time(self):
        admin = self.store.create_user({'username': 'adm', 'role': 'school_admin', 'school_id': self.sid})['user']
        code = self.store.tg_staff_code(admin)
        sent = []

        class API:
            def call(self, method, http_timeout=20, **p):
                if method == 'getMe':
                    return {'ok': True, 'result': {'username': 'b'}}
                if method == 'sendMessage':
                    sent.append(p['text'])
                return {'ok': True, 'result': {}}
        self.store.set_setting('telegram_enabled', '1')
        self.store.set_setting('telegram_bot_username', 'b')
        calls = []
        bot = telegram_bot.Bot(self.store, lambda: '123456789:' + 'x' * 35, api_factory=lambda t: API(),
                               summary_fn=lambda sid, day: calls.append(day) or summaries.generate(self.store, sid, day, None))
        with mock.patch.object(telegram_bot.time, 'sleep', lambda s: None):
            bot._ensure_api()
            bot.handle_update({'update_id': 1, 'message': {'chat': {'id': 7, 'type': 'private'}, 'text': f'/start {code}', 'from': {}}})
            sent.clear()
            at = lambda hm: datetime.combine(self.day, datetime.strptime(hm, '%H:%M').time(), TZ)
            self.assertEqual(bot.run_notifications(now=at('14:00')), 0)          # before 15:00: nothing
            bot.run_notifications(now=at('15:05'))
            self.assertEqual(sum('Сводка посещаемости' in m for m in sent), 1)
            bot.run_notifications(now=at('16:00'))
            self.assertEqual(sum('Сводка посещаемости' in m for m in sent), 1)   # only once a day
        self.assertEqual(len(calls), 1)


if __name__ == '__main__':
    unittest.main()
