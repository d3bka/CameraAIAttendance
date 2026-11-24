"""Telegram notifications: ONE platform bot shared by all schools (stdlib only, long polling).

* Parents subscribe with a personal deep link  t.me/<bot>?start=<code>  -> only their own child.
* Staff (admins/teachers) subscribe with a one-time link from their profile.
* A school group (e.g. teachers' chat) is bound with  /link <code>  sent in the group.
* The notifier thread turns attendance changes into messages, de-duplicated in the DB.

The bot does nothing until a superadmin enables it in the panel (protects other bots that
might share a token). Nothing here stores photos; messages contain names and statuses only.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

from core import TZ, CAMERA_ONLINE_SECONDS, InputError, NotFound, from_iso, local_today, now_utc

LOG = logging.getLogger('telegram')
TOKEN_RE = re.compile(r'^\d{6,12}:[A-Za-z0-9_-]{30,50}$')

TEXT = {
    'ru': {
        'help': 'Бот посещаемости школы.\n\nЧтобы получать уведомления, откройте персональную ссылку, которую выдала школа.\n'
                '/status — посещаемость сегодня\n/stop — отписаться\n/lang — сменить язык',
        'linked_parent': '✅ Вы подписаны на уведомления о посещаемости: {name} ({cls}, {school}).',
        'linked_staff': '✅ Уведомления школы «{school}» подключены для {name}.',
        'linked_group': '✅ Группа подключена к уведомлениям школы «{school}».',
        'bad_code': '❌ Ссылка недействительна или устарела. Попросите в школе новую.',
        'stopped': 'Вы отписаны от всех уведомлений.',
        'nothing': 'Вы пока ни на что не подписаны.',
        'arrived': '🟢 {name} пришёл(ла) в школу в {time} ({lesson}).',
        'late': '🟡 {name} опоздал(а): {lesson}, отмечен(а) в {time}.',
        'absent_first': '🔴 {name} не отмечен(а) на уроке «{lesson}» ({slot}). Если ребёнок болеет — сообщите классному руководителю.',
        'absent_after': '🟠 {name} был(а) в школе, но отсутствует на уроке «{lesson}» ({slot}).',
        'camera_down': '⚠️ Камера «{camera}» не работает во время урока {cls} «{lesson}» ({slot}). Отметьте посещаемость вручную.',
        'status_line': '{name}: {status}',
        'lang_set': 'Язык: русский',
        'summary': '📊 Сводка посещаемости за {day}\n\n{text}',
        'group_only_admin': 'Команда работает только в группе.',
    },
    'uz': {
        'help': 'Maktab davomat boti.\n\nXabarnomalarni olish uchun maktab bergan shaxsiy havolani oching.\n'
                '/status — bugungi davomat\n/stop — obunani bekor qilish\n/lang — tilni almashtirish',
        'linked_parent': '✅ Davomat xabarnomalariga obuna bo‘ldingiz: {name} ({cls}, {school}).',
        'linked_staff': '✅ «{school}» maktabi xabarnomalari {name} uchun ulandi.',
        'linked_group': '✅ Guruh «{school}» maktabi xabarnomalariga ulandi.',
        'bad_code': '❌ Havola yaroqsiz yoki muddati o‘tgan. Maktabdan yangisini so‘rang.',
        'stopped': 'Barcha xabarnomalardan obuna bekor qilindi.',
        'nothing': 'Hozircha hech narsaga obuna emassiz.',
        'arrived': '🟢 {name} maktabga {time} da keldi ({lesson}).',
        'late': '🟡 {name} kechikdi: {lesson}, {time} da qayd etildi.',
        'absent_first': '🔴 {name} «{lesson}» darsida ({slot}) qayd etilmadi. Farzandingiz kasal bo‘lsa, sinf rahbariga xabar bering.',
        'absent_after': '🟠 {name} maktabda edi, lekin «{lesson}» darsida ({slot}) yo‘q.',
        'camera_down': '⚠️ «{camera}» kamerasi {cls} «{lesson}» darsi ({slot}) vaqtida ishlamayapti. Davomatni qo‘lda belgilang.',
        'status_line': '{name}: {status}',
        'lang_set': 'Til: o‘zbekcha',
        'summary': '📊 {day} davomat hisoboti (rus tilida)\n\n{text}',
        'group_only_admin': 'Buyruq faqat guruhda ishlaydi.',
    },
}
STATUS_WORD = {
    'ru': {'present': 'присутствует', 'late': 'опоздал(а)', 'absent': 'отсутствует', 'excused': 'уважительная причина',
           'not_seen': 'не замечен(а)', 'no_data': 'нет данных', 'needs_review': 'проверяется', 'upcoming': 'уроки ещё не начались',
           'in_progress': 'идёт урок'},
    'uz': {'present': 'darsda', 'late': 'kechikdi', 'absent': 'yo‘q', 'excused': 'sababli', 'not_seen': 'ko‘rinmadi',
           'no_data': 'maʼlumot yo‘q', 'needs_review': 'tekshirilmoqda', 'upcoming': 'darslar boshlanmagan', 'in_progress': 'dars ketmoqda'},
}


def t(lang, key, **kw):
    return TEXT.get(lang, TEXT['ru'])[key].format(**kw)


class TelegramAPI:
    """Minimal Bot API client. Never logs the token."""

    def __init__(self, token):
        if not TOKEN_RE.fullmatch(token or ''):
            raise InputError('Некорректный токен Telegram-бота')
        self._base = f'https://api.telegram.org/bot{token}/'

    def call(self, method, http_timeout=20, **params):
        data = urllib.parse.urlencode({k: (json.dumps(v) if isinstance(v, (dict, list)) else v)
                                       for k, v in params.items() if v is not None}).encode()
        req = urllib.request.Request(self._base + method, data=data)
        try:
            with urllib.request.urlopen(req, timeout=http_timeout) as resp:
                body = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            try:
                body = json.loads(exc.read())
            except ValueError:
                body = {'ok': False, 'error_code': exc.code, 'description': 'HTTP error'}
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ConnectionError(type(exc).__name__) from None
        return body


class Bot:
    def __init__(self, store, token_loader, api_factory=TelegramAPI, summary_fn=None):
        self.store = store
        self.summary_fn = summary_fn  # (school_id, day) -> {'text', ...}; daily report for staff chats
        self.token_loader = token_loader
        self.api_factory = api_factory
        self.api = None
        self.username = None
        self.offset = None
        self.stop = threading.Event()
        self.status = 'disabled'
        self.last_error = ''
        self.conflicts = 0

    # ---------------------------------------------------------------- lifecycle
    def start(self):
        threading.Thread(target=self._poll_loop, name='tg-poll', daemon=True).start()
        threading.Thread(target=self._notify_loop, name='tg-notify', daemon=True).start()

    def _ensure_api(self):
        if not self.store.get_setting('telegram_enabled'):
            self.api, self.status = None, 'disabled'
            return False
        if self.api is None:
            token = self.token_loader()
            if not token:
                self.status, self.last_error = 'error', 'Нет токена (data/telegram_token.txt)'
                return False
            self.api = self.api_factory(token)
            me = self.api.call('getMe')
            if not me.get('ok'):
                self.api, self.status, self.last_error = None, 'error', me.get('description', 'getMe failed')
                return False
            self.username = me['result']['username']
            if self.store.get_setting('telegram_bot_username') not in (None, self.username):
                # token was swapped for another bot: stay off until a superadmin re-enables explicitly
                self.store.set_setting('telegram_enabled', '')
                self.api, self.status, self.last_error = None, 'error', 'Токен принадлежит другому боту — включите заново'
                return False
            self.status = 'starting'
        return True

    def poll_once(self):
        """One getUpdates round. Returns seconds to wait before the next round (0 = immediately)."""
        try:
            if not self._ensure_api():
                return 15
            resp = self.api.call('getUpdates', http_timeout=35, offset=self.offset, timeout=25, allowed_updates=['message'])
            if not resp.get('ok'):
                if resp.get('error_code') == 409:
                    self.conflicts += 1
                    LOG.warning('Telegram 409 Conflict #%d: another program is polling this bot', self.conflicts)
                    self.status, self.last_error = 'error', 'Этот бот уже используется другой программой (409 Conflict)'
                else:
                    self.last_error = resp.get('description', 'getUpdates failed')
                return 30
            if self.status != 'running':
                LOG.info('Telegram bot @%s is polling', self.username)
            self.status, self.last_error = 'running', ''  # (re)covered: notifications resume
            for update in resp.get('result', []):
                self.offset = update['update_id'] + 1
                try:
                    self.handle_update(update)
                except Exception:
                    LOG.exception('update handling failed')
            return 0
        except ConnectionError as exc:
            self.last_error = f'Нет связи с Telegram ({exc})'
            return 10
        except Exception:
            LOG.exception('telegram poll loop error')
            return 10

    def _poll_loop(self):
        while not self.stop.is_set():
            wait = self.poll_once()
            if wait:
                self.stop.wait(wait)

    def _notify_loop(self):
        while not self.stop.is_set():
            self.stop.wait(60)
            try:
                if self.api is not None and self.status == 'running':
                    self.run_notifications()
            except Exception:
                LOG.exception('notification cycle failed')

    # ---------------------------------------------------------------- messaging
    def send(self, chat_id, text):
        if self.api is None:
            return False
        resp = self.api.call('sendMessage', chat_id=chat_id, text=text, disable_web_page_preview='true')
        if not resp.get('ok'):
            code, desc = resp.get('error_code'), str(resp.get('description', '')).lower()
            if code == 403 or (code == 400 and 'chat not found' in desc):
                self.store.tg_deactivate_chat(chat_id)  # user blocked the bot / bot removed from the group
            elif code == 429:
                time.sleep(min(30, resp.get('parameters', {}).get('retry_after', 5)))
            return False
        time.sleep(0.05)  # stay far below Telegram's 30 msg/s limit
        return True

    def handle_update(self, update):
        msg = update.get('message')
        if not msg or 'text' not in msg:
            return
        chat = msg['chat']
        chat_id = chat['id']
        text = msg['text'].strip()
        lang = self.store.tg_chat_lang(chat_id) or ('uz' if (msg.get('from', {}).get('language_code') or '').startswith('uz') else 'ru')
        cmd, _, arg = text.partition(' ')
        cmd = cmd.split('@', 1)[0].lower()
        if cmd in ('/start', '/link'):
            if not arg.strip():
                self.send(chat_id, t(lang, 'help'))
                return
            try:
                info = self.store.tg_redeem(arg.strip(), chat_id, chat.get('type', 'private'), lang)
            except (NotFound, InputError):
                self.send(chat_id, t(lang, 'bad_code'))
                return
            key = {'parent': 'linked_parent', 'staff': 'linked_staff', 'group': 'linked_group'}[info['kind']]
            self.send(chat_id, t(lang, key, **info))
        elif cmd == '/stop':
            n = self.store.tg_unlink_chat(chat_id)
            self.send(chat_id, t(lang, 'stopped' if n else 'nothing'))
        elif cmd == '/lang':
            new = 'ru' if lang == 'uz' else 'uz'
            self.store.tg_set_lang(chat_id, new)
            self.send(chat_id, t(new, 'lang_set'))
        elif cmd == '/status':
            lines = self.store.tg_status_lines(chat_id)
            if not lines:
                self.send(chat_id, t(lang, 'nothing'))
            else:
                self.send(chat_id, '\n'.join(t(lang, 'status_line', name=n, status=STATUS_WORD[lang].get(s, s)) for n, s in lines))
        elif chat.get('type') == 'private':
            self.send(chat_id, t(lang, 'help'))

    # ---------------------------------------------------------------- notifications
    def run_notifications(self, now=None):
        """One pass: turn today's attendance into parent/staff messages exactly once."""
        sent = 0
        items = list(self.store.tg_pending_notifications(now))
        if self.summary_fn is not None:
            items += self.daily_summaries(now)
        for item in items:
            chat_id, lang, key, fields = item
            text = t(lang, key, **fields)
            if self.store.tg_mark_sent(item_key := fields['_dedupe'] + f':{chat_id}'):
                if self.send(chat_id, text):
                    sent += 1
                else:
                    self.store.tg_unmark_sent(item_key)
        return sent

    def daily_summaries(self, now=None):
        """Once per school day, at the school's summary_time, a report for staff chats and groups."""
        now = now or now_utc()
        local = now.astimezone(TZ)
        day = local.date().isoformat()
        out = []
        for sid, chats in self.store.tg_staff_chats().items():
            school = self.store.get_school(sid)
            if local.strftime('%H:%M') < school.get('summary_time', '15:00'):
                continue
            key = f'sum:{sid}:{day}'
            pending = [(chat, lang) for chat, lang in chats if not self.store.tg_was_sent(f'{key}:{chat}')]
            if not pending:
                continue
            summary = self.store.get_summary(sid, day) or self.summary_fn(sid, day)
            if 'уроков по расписанию не было' in summary['text']:
                continue
            for chat, lang in pending:
                out.append((chat, lang, 'summary', {'day': day, 'text': summary['text'], '_dedupe': key}))
        return out
