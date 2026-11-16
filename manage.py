"""Administrative CLI: superadmin bootstrap, password reset, demo data, retention purge.

    python manage.py create-superadmin --username superadmin
    python manage.py set-password --username superadmin
    python manage.py seed-demo [--reset]
    python manage.py purge --days 365
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import random
import struct
import sys
import zlib
from datetime import datetime, timedelta
from pathlib import Path

from core import (TZ, Store, InputError, generate_password, iso, minute_key, now_utc)

ROOT = Path(__file__).resolve().parent
DEMO_SUFFIX = ' (тест)'


def open_store(args):
    path = Path(args.db)
    path.parent.mkdir(parents=True, exist_ok=True)
    return Store(path)


def ask_password(args):
    if args.password_env:
        value = os.environ.get(args.password_env)
        if not value:
            raise SystemExit(f'Переменная окружения {args.password_env} пуста')
        return value, False
    if args.prompt:
        first = getpass.getpass('Новый пароль (10+ символов): ')
        if first != getpass.getpass('Повторите пароль: '):
            raise SystemExit('Пароли не совпадают')
        return first, False
    return generate_password(16), True


def cmd_create_superadmin(args):
    store = open_store(args)
    password, generated = ask_password(args)
    result = store.create_user({'username': args.username, 'full_name': args.full_name or 'Супер-администратор',
                                'role': 'superadmin', 'password': password})
    print(f"Супер-админ создан: {result['user']['username']}")
    if generated:
        print(f'Пароль (показывается один раз, сохраните): {password}')


def cmd_set_password(args):
    store = open_store(args)
    password, generated = ask_password(args)
    store.set_password(args.username, password)
    print(f'Пароль пользователя {args.username} обновлён, все его сессии завершены.')
    if generated:
        print(f'Новый пароль (показывается один раз): {password}')


def cmd_worker_config(args):
    """Create a device token + a ready-to-fill config with every camera of the school."""
    store = open_store(args)
    school = store.get_school(args.school_id)
    cameras = [c for c in store.list_cameras(args.school_id) if c['kind'] != 'observe']
    if not cameras:
        raise SystemExit('У школы нет камер (кроме «наблюдения»). Добавьте их в панели: «Камеры и AI».')
    out = Path(args.out) if args.out else ROOT / 'data' / f'cameras-school-{args.school_id}.json'
    if out.exists() and not args.force:
        raise SystemExit(f'{out} уже существует. Используйте --force, чтобы перезаписать (старый токен останется активным).')
    token = store.create_token(args.school_id, {'name': f'AI worker ({out.name})'})['token']
    config = {
        'api': args.api,
        'token': token,
        'cameras': {c['camera_key']: 'rtsp://USER:PASSWORD@CAMERA_IP:554/Streaming/Channels/101' for c in cameras},
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f"Школа: {school['name']}")
    print(f'Конфиг: {out}  (в .gitignore; содержит токен — не пересылайте его)')
    print('Впишите RTSP-адрес каждой камеры (основной поток 1080p), затем:')
    print(f'  .venv\\Scripts\\python face_worker.py --config "{out}"')
    print('  или автозапуск: powershell -ExecutionPolicy Bypass -File scripts\\install_autostart.ps1')


def cmd_purge(args):
    print(open_store(args).purge(args.days))


# ----------------------------------------------------------------------------- synthetic avatars
def _png(width, height, rows):
    raw = b''.join(b'\x00' + bytes(r) for r in rows)

    def chunk(tag, data):
        return struct.pack('>I', len(data)) + tag + data + struct.pack('>I', zlib.crc32(tag + data) & 0xffffffff)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(raw, 6)) + chunk(b'IEND', b''))


def synthetic_avatar(seed: str, size=128):
    """Cartoon placeholder (NOT a real face). Lets the UI be tested without photos of real children."""
    h = hashlib.sha256(seed.encode()).digest()
    bg = (60 + h[0] % 120, 80 + h[1] % 120, 110 + h[2] % 120)
    skin = [(241, 205, 170), (224, 180, 140), (198, 150, 110), (160, 115, 85)][h[3] % 4]
    hair = [(40, 30, 25), (90, 60, 35), (20, 20, 20), (150, 100, 50)][h[4] % 4]
    shirt = (h[5] % 200 + 30, h[6] % 200 + 30, h[7] % 200 + 30)
    c = size / 2
    rows = []
    for y in range(size):
        row = bytearray()
        for x in range(size):
            px = bg
            dx, dy = x - c, y - size * 0.44
            if (x - c) ** 2 + (y - size * 1.05) ** 2 < (size * 0.42) ** 2:
                px = shirt
            if dx * dx / (size * 0.27) ** 2 + dy * dy / (size * 0.32) ** 2 < 1:
                px = skin
                if dy < -size * 0.13:
                    px = hair
                if ((x - c + size * 0.1) ** 2 + (y - size * 0.42) ** 2 < (size * 0.035) ** 2 or
                        (x - c - size * 0.1) ** 2 + (y - size * 0.42) ** 2 < (size * 0.035) ** 2):
                    px = (30, 30, 40)
                if size * 0.55 < y < size * 0.58 and abs(dx) < size * 0.08:
                    px = (170, 70, 70)
            row.extend(px)
        rows.append(row)
    return _png(size, size, rows)


# ----------------------------------------------------------------------------- demo seed
FIRST = ['Азиз', 'Мадина', 'Тимур', 'Севара', 'Жасур', 'Нилуфар', 'Бобур', 'Дилноза', 'Рустам', 'Камила',
         'Шерзод', 'Малика', 'Отабек', 'Зарина', 'Санжар', 'Лола', 'Фаррух', 'Гулнора', 'Акмаль', 'Сабина']
LAST = ['Каримов', 'Юсупова', 'Рахимов', 'Алиева', 'Назаров', 'Хасанова', 'Турсунов', 'Абдуллаева', 'Исмоилов',
        'Ахмедова', 'Сафаров', 'Мирзаева', 'Холматов', 'Усмонова', 'Эргашев']
SUBJECTS = ['Математика', 'Родной язык', 'Английский язык', 'История', 'Физика', 'Биология', 'Литература',
            'Информатика', 'География', 'Физкультура', 'Химия', 'Русский язык']
BELLS = [('08:00', '08:45'), ('08:55', '09:40'), ('09:50', '10:35'), ('10:55', '11:40'), ('11:50', '12:35'), ('12:45', '13:30')]

DEMO_SCHOOLS = [
    {'name': 'Школа №1' + DEMO_SUFFIX, 'city': 'Ташкент', 'slug': 'school1',
     'classes': {'5-А': 'room201', '5-Б': 'room202', '9-А': 'room305'},
     'cameras': [('school1_gate', 'Турникет главного входа', 'Главный вход', 'entry'),
                 ('school1_room201', 'Камера каб. 201', 'Кабинет 201', 'classroom'),
                 ('school1_room202', 'Камера каб. 202', 'Кабинет 202', 'classroom'),
                 ('school1_room305', 'Камера каб. 305 (старая)', 'Кабинет 305', 'classroom'),
                 ('school1_hall', 'Коридор 1 этаж', 'Коридор', 'observe')],
     'flaky': {'school1_room305'}},
    {'name': 'Школа №2' + DEMO_SUFFIX, 'city': 'Самарканд', 'slug': 'school2',
     'classes': {'7-А': 'room12', '10-Б': 'room14'},
     'cameras': [('school2_room12', 'Камера каб. 12', 'Кабинет 12', 'classroom'),
                 ('school2_room14', 'Камера каб. 14', 'Кабинет 14', 'classroom')],
     'flaky': set()},
    {'name': 'Школа №3' + DEMO_SUFFIX, 'city': 'Бухара', 'slug': 'school3',
     'classes': {'3-В': None}, 'cameras': [], 'flaky': set()},
]


def cmd_seed_demo(args):
    store = open_store(args)
    rnd = random.Random(args.random_seed)
    existing = {s['name']: s for s in store.list_schools()}
    demo_existing = [s for s in existing.values() if s['name'].endswith(DEMO_SUFFIX)]
    if demo_existing and not args.reset:
        raise SystemExit('Тестовые школы уже существуют. Запустите с --reset, чтобы пересоздать их.')
    for s in demo_existing:
        store.delete_school(s['id'])  # cascades to classes, students, photos, users, events
    credentials = []
    now = now_utc()
    today = datetime.now(TZ).date()
    for spec in DEMO_SCHOOLS:
        school = store.create_school({'name': spec['name'], 'city': spec['city'], 'late_grace_min': 10})
        sid = school['id']
        cam_ids = {}
        for key, name, room, kind in spec['cameras']:
            cam_ids[key] = store.create_camera(sid, {'camera_key': key, 'name': name, 'room': room, 'kind': kind})['id']
        for role, suffix in (('school_admin', 'admin'), ('teacher', 'teacher')):
            username = f"{spec['slug']}_{suffix}"
            for u in store.list_users():
                if u['username'].lower() == username:
                    store.delete_user(u['id'], {'id': 0, 'role': 'superadmin', 'username': 'seed'})
            res = store.create_user({'username': username, 'role': role, 'school_id': sid,
                                     'full_name': ('Администратор ' if role == 'school_admin' else 'Учитель ') + spec['name']})
            credentials.append((spec['name'], role, username, res['password']))
        token = store.create_token(sid, {'name': 'AI worker (demo)'})['token']
        credentials.append((spec['name'], 'device_token', 'X-Ingest-Token', token))

        class_students = {}
        counter = 1
        for cname, room in spec['classes'].items():
            cid = store.create_class(sid, {'name': cname})['id']
            ids = []
            for _ in range(rnd.randint(7, 10)):
                fn, ln = rnd.choice(FIRST), rnd.choice(LAST)
                if fn.endswith('а') and not ln.endswith('а'):
                    ln += 'а'
                elif not fn.endswith('а') and ln.endswith('а'):
                    ln = ln[:-1]
                code = f"{spec['slug'].upper()}-{counter:03d}"
                counter += 1
                st = store.create_student(sid, {'code': code, 'full_name': f'{ln} {fn}', 'class_id': cid,
                                                'consent_confirmed': True})
                for i in range(rnd.randint(1, 2)):
                    store.add_photo(sid, st['id'], synthetic_avatar(f'{code}-{i}'), face_status='demo')
                ids.append((st['id'], code))
            class_students[cid] = (ids, f"{spec['slug']}_{room}" if room else None)
            for weekday in range(1, 7):
                subjects = rnd.sample(SUBJECTS, 6)
                for n, (start, end) in enumerate(BELLS[:rnd.randint(4, 6)]):
                    store.create_lesson(sid, {'class_id': cid, 'weekday': weekday, 'start_time': start, 'end_time': end,
                                              'subject': subjects[n], 'teacher': rnd.choice(LAST) + ' ' + rnd.choice('АБВГДМНРС') + '.',
                                              'camera_id': cam_ids.get(f"{spec['slug']}_{room}") if room else None})
        _simulate_history(store, sid, spec, cam_ids, class_students, today, now, rnd)

    print('\n=== Тестовые школы созданы ===')
    print('Фото учеников — синтетические мультяшные аватары (не реальные дети); для AI они помечены как «демо».')
    print('Сохраните пароли — они показываются один раз:\n')
    for school, role, user, pw in credentials:
        print(f'  {school:22} {role:13} {user:20} {pw}')


def _simulate_history(store, sid, spec, cam_ids, class_students, today, now, rnd):
    """Fake camera uptime + recognitions for the last 6 days so dashboards have data."""
    uptime = set()
    for days_back in range(0, 7):
        day = today - timedelta(days=days_back)
        if day.isoweekday() == 7:
            continue
        gate = cam_ids.get(f"{spec['slug']}_gate")
        for cid, (students, cam_key) in class_students.items():
            cam_id = cam_ids.get(cam_key) if cam_key else None
            flaky = cam_key in spec['flaky']
            lessons = [l for l in store.list_lessons(sid, cid) if l['weekday'] == day.isoweekday()]
            absent = {s for s, _ in students if rnd.random() < 0.08}
            for stid, code in students:
                if gate and stid not in absent:
                    t = datetime.combine(day, datetime.strptime('07:35', '%H:%M').time(), TZ) + timedelta(minutes=rnd.randint(0, 35))
                    if t <= now:
                        store.ingest(sid, {'camera_key': f"{spec['slug']}_gate", 'event_uid': f'seed-gate-{code}-{day}',
                                           'event_type': 'access_granted', 'verification_method': 'card',
                                           'person_code': code, 'observed_at': t.isoformat()})
            for lesson in lessons:
                start = datetime.combine(day, datetime.strptime(lesson['start_time'], '%H:%M').time(), TZ)
                end = datetime.combine(day, datetime.strptime(lesson['end_time'], '%H:%M').time(), TZ)
                if start > now or not cam_id:
                    continue
                t = start
                while t < min(end, now):
                    if not flaky or rnd.random() < 0.2:
                        uptime.add((cam_id, minute_key(t)))
                    t += timedelta(minutes=1)
                if flaky:
                    continue  # old camera: mostly offline -> lessons show "no data", not "absent"
                for stid, code in students:
                    if stid in absent:
                        continue
                    roll = rnd.random()
                    if roll < 0.06:
                        continue  # camera did not recognise this child -> "not seen"
                    offset = rnd.randint(-4, 6) if roll < 0.92 else rnd.randint(12, 25)
                    seen = start + timedelta(minutes=offset)
                    if seen > now:
                        continue
                    conf = rnd.uniform(0.38, 0.44) if roll > 0.97 else rnd.uniform(0.48, 0.78)
                    store.ingest(sid, {'camera_key': cam_key, 'event_uid': f"seed-face-{code}-{lesson['id']}-{day}",
                                       'event_type': 'face_match', 'verification_method': 'ai_face',
                                       'person_code': code, 'confidence': round(conf, 3), 'observed_at': seen.isoformat()})
    with store.connect() as db:
        db.executemany('INSERT OR IGNORE INTO camera_uptime(camera_id,minute) VALUES(?,?)', sorted(uptime))
        for key, cam_id in cam_ids.items():
            if key not in spec['flaky'] and not key.endswith('_hall'):
                db.execute('UPDATE cameras SET last_seen_at=?,last_ok=1 WHERE id=?', (iso(now), cam_id))
        db.execute('INSERT INTO audit(at,user_id,username,school_id,action,detail) VALUES(?,?,?,?,?,?)',
                   (iso(now), None, 'seed', sid, 'demo.seed', spec['name']))


def main(argv=None):
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description='Camera Attendance AI administration')
    parser.add_argument('--db', default=str(ROOT / 'data' / 'platform.sqlite3'))
    sub = parser.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('create-superadmin')
    p.add_argument('--username', required=True)
    p.add_argument('--full-name')
    p.add_argument('--prompt', action='store_true', help='Ввести пароль вручную (иначе генерируется)')
    p.add_argument('--password-env', help='Взять пароль из переменной окружения')
    p.set_defaults(fn=cmd_create_superadmin)
    p = sub.add_parser('set-password')
    p.add_argument('--username', required=True)
    p.add_argument('--prompt', action='store_true')
    p.add_argument('--password-env')
    p.set_defaults(fn=cmd_set_password)
    p = sub.add_parser('seed-demo', help='Создать тестовые школы, классы, учеников, расписание и события')
    p.add_argument('--reset', action='store_true', help='Удалить и пересоздать ранее созданные тестовые школы')
    p.add_argument('--random-seed', type=int, default=None)
    p.set_defaults(fn=cmd_seed_demo)
    p = sub.add_parser('worker-config', help='Создать токен и конфиг AI-воркера со всеми камерами школы')
    p.add_argument('--school-id', type=int, required=True)
    p.add_argument('--api', default='http://127.0.0.1:8080')
    p.add_argument('--out')
    p.add_argument('--force', action='store_true')
    p.set_defaults(fn=cmd_worker_config)
    p = sub.add_parser('purge', help='Удалить события/отметки старше N дней')
    p.add_argument('--days', type=int, default=365)
    p.set_defaults(fn=cmd_purge)
    args = parser.parse_args(argv)
    try:
        args.fn(args)
    except InputError as exc:
        raise SystemExit(f'Ошибка: {exc}')


if __name__ == '__main__':
    main()
