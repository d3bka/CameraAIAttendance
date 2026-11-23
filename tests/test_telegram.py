import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import core  # noqa: E402
import telegram_bot  # noqa: E402
from core import TZ, Store, minute_key  # noqa: E402

core.PBKDF2_ITERATIONS = 1000


class FakeAPI:
    """Stands in for api.telegram.org."""

    def __init__(self, username='test_bot'):
        self.username = username
        self.sent = []
        self.fail_chats = {}

    def call(self, method, http_timeout=20, **params):
        if method == 'getMe':
            return {'ok': True, 'result': {'id': 1, 'username': self.username, 'first_name': 'Test'}}
        if method == 'sendMessage':
            if params['chat_id'] in self.fail_chats:
                return {'ok': False, 'error_code': self.fail_chats[params['chat_id']], 'description': 'Forbidden: bot was blocked'}
            self.sent.append((params['chat_id'], params['text']))
            return {'ok': True, 'result': {}}
        return {'ok': True, 'result': []}


def update(chat_id, text, chat_type='private', lang='ru'):
    return {'update_id': 1, 'message': {'chat': {'id': chat_id, 'type': chat_type}, 'text': text,
                                         'from': {'language_code': lang}}}


class TelegramTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(telegram_bot.time, 'sleep', lambda s: None)  # no rate-limit pauses in tests
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tmp = tempfile.TemporaryDirectory()
        s = self.store = Store(Path(self.tmp.name) / 'db.sqlite3')
        self.sid = s.create_school({'name': 'Школа 1'})['id']
        self.admin = s.create_user({'username': 'adm', 'role': 'school_admin', 'school_id': self.sid})['user']
        s.create_camera(self.sid, {'camera_key': 'room', 'name': 'Каб. 101', 'room': '101', 'kind': 'classroom'})
        self.cam = s.list_cameras(self.sid)[0]['id']
        cid = s.create_class(self.sid, {'name': '5-А'})['id']
        self.a = s.create_student(self.sid, {'code': 'A', 'full_name': 'Алиев Азиз', 'class_id': cid, 'consent_confirmed': True})['id']
        self.b = s.create_student(self.sid, {'code': 'B', 'full_name': 'Бакирова Лола', 'class_id': cid, 'consent_confirmed': True})['id']
        self.day = (datetime.now(TZ) - timedelta(days=1)).date()
        at = lambda hm: datetime.combine(self.day, datetime.strptime(hm, '%H:%M').time(), TZ)
        self.at = at
        s.create_lesson(self.sid, {'class_id': cid, 'weekday': self.day.isoweekday(), 'start_time': '10:00', 'end_time': '10:45',
                                   'subject': 'Математика', 'camera_id': self.cam})
        s.ingest(self.sid, {'camera_key': 'room', 'event_uid': 'e1', 'event_type': 'face_match', 'verification_method': 'ai_face',
                            'person_code': 'A', 'confidence': 0.7, 'observed_at': at('10:03').isoformat()})
        with s.connect() as db:
            t = at('10:00')
            while t < at('10:45'):
                db.execute('INSERT INTO camera_uptime(camera_id,minute) VALUES(?,?)', (self.cam, minute_key(t)))
                t += timedelta(minutes=1)
        s.set_setting('telegram_enabled', '1')
        s.set_setting('telegram_bot_username', 'test_bot')
        self.api = FakeAPI()
        self.bot = telegram_bot.Bot(s, lambda: '123456789:' + 'x' * 35, api_factory=lambda token: self.api)
        self.assertTrue(self.bot._ensure_api())

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def link_parent(self, student_id, chat_id, chat_type='private'):
        code = self.store.tg_parent_codes(self.sid, [student_id])[0]['tg_code']
        self.bot.handle_update(update(chat_id, f'/start {code}', chat_type))
        return code

    def test_parent_links_and_notifications(self):
        code = self.link_parent(self.a, 111)
        self.assertIn('Алиев Азиз', self.api.sent[-1][1])
        self.bot.handle_update(update(222, f'/start {code}'))            # second parent, same link
        self.link_parent(self.b, 333)
        self.api.sent.clear()
        n = self.bot.run_notifications(now=self.at('11:00'))
        msgs = dict(((c, m) for c, m in self.api.sent))
        self.assertEqual(n, 3)
        self.assertIn('пришёл', msgs[111])
        self.assertIn('10:03', msgs[111])
        self.assertIn('пришёл', msgs[222])
        self.assertIn('Бакирова Лола', msgs[333])
        self.assertIn('не отмечен', msgs[333])
        self.assertEqual(self.bot.run_notifications(now=self.at('11:05')), 0)   # sent exactly once
        self.assertEqual(self.bot.run_notifications(now=self.at('18:00')), 0)   # nothing new later either

    def test_parent_link_privacy(self):
        code = self.store.tg_parent_codes(self.sid, [self.a])[0]['tg_code']
        self.bot.handle_update(update(-100, f'/start {code}', 'supergroup'))     # parent link in a group chat
        self.assertIn('недействительна', self.api.sent[-1][1])
        self.assertEqual(self.store.tg_subscribers(self.sid)['parents'], {})
        self.bot.handle_update(update(5, '/start pAAAAAAAAAAAAAAAAAAAAAAAAAAAA'))
        self.assertIn('недействительна', self.api.sent[-1][1])
        for chat in (11, 12, 13, 14):
            self.bot.handle_update(update(chat, f'/start {code}'))
        self.bot.handle_update(update(15, f'/start {code}'))                     # 5th use: limit is 4
        self.assertIn('недействительна', self.api.sent[-1][1])

    def test_staff_group_camera_alert(self):
        staff_code = self.store.tg_staff_code(self.admin)
        self.bot.handle_update(update(444, f'/start {staff_code}'))
        group_code = self.store.tg_group_code(self.sid)
        self.bot.handle_update(update(-500, f'/link@test_bot {group_code}', 'group'))
        self.assertIn('Группа подключена', self.api.sent[-1][1])
        self.bot.handle_update(update(-501, f'/start {staff_code}', 'group'))    # staff link is private-only
        self.assertIn('недействительна', self.api.sent[-1][1])
        self.api.sent.clear()
        self.assertEqual(self.bot.run_notifications(now=self.at('10:20')), 2)   # camera never sent a heartbeat
        self.assertTrue(all('Каб. 101' in m for _, m in self.api.sent))
        self.assertEqual(self.bot.run_notifications(now=self.at('10:30')), 0)

    def test_commands_and_blocking(self):
        self.link_parent(self.a, 111)
        self.bot.handle_update(update(111, '/lang'))
        self.assertIn('o‘zbekcha', self.api.sent[-1][1])
        self.bot.handle_update(update(111, '/status'))
        self.assertIn('Алиев Азиз', self.api.sent[-1][1])
        self.api.fail_chats[111] = 403                                           # parent blocked the bot
        self.bot.run_notifications(now=self.at('11:00'))
        self.assertEqual(self.store.tg_subscribers(self.sid)['parents'], {})
        self.link_parent(self.b, 333)
        self.bot.handle_update(update(333, '/stop'))
        self.assertIn('отписаны', self.api.sent[-1][1])
        self.assertEqual(self.store.tg_subscribers(self.sid)['parents'], {})

    def test_disabled_and_token_swap(self):
        self.store.set_setting('telegram_enabled', '')
        self.assertFalse(self.bot._ensure_api())
        self.store.set_setting('telegram_enabled', '1')
        self.store.set_setting('telegram_bot_username', 'another_bot')          # token now belongs to a different bot
        bot = telegram_bot.Bot(self.store, lambda: '123456789:' + 'x' * 35, api_factory=lambda token: self.api)
        self.assertFalse(bot._ensure_api())
        self.assertIsNone(self.store.get_setting('telegram_enabled'))
        with self.assertRaises(core.InputError):
            telegram_bot.TelegramAPI('not-a-token')


if __name__ == '__main__':
    unittest.main()
