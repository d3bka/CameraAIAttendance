"""Daily attendance summary for the school director - optionally written by DeepSeek.

Privacy: the LLM receives only aggregated numbers, class/subject names and pseudonyms
("Ученик#3"). Real names are substituted back locally, after the answer arrives.
No photos, codes or names ever leave the server. Without an API key (or on any API error)
a deterministic template summary is produced instead.
"""
from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from collections import Counter
from datetime import date, timedelta

LOG = logging.getLogger('summaries')
API_URL = 'https://api.deepseek.com/chat/completions'
DEFAULT_MODEL = 'deepseek-flash'
TOKEN = 'Ученик#{n}'
TOKEN_RE = re.compile(r'Ученик#(\d+)')
FINISHED = ('present', 'late', 'absent', 'excused', 'not_seen', 'no_data', 'needs_review')

SYSTEM_PROMPT = (
    'Ты — аналитик посещаемости школы в Узбекистане. По JSON с данными за день напиши сводку для директора на русском языке:\n'
    '1) главное за день — 1–2 предложения с процентом посещаемости;\n'
    '2) классы, которым нужно внимание (низкая посещаемость, много опозданий);\n'
    '3) ученики с повторяющимися пропусками — используй обозначения ровно как в данных (например «Ученик#3»);\n'
    '4) проблемы с камерами, если есть;\n'
    '5) одна конкретная рекомендация на завтра.\n'
    'Не выдумывай данных и не делай предположений о здоровье, семье или причинах. До 150 слов, без таблиц и markdown-заголовков.'
)


def _day_cells(grid, student_id):
    row = next((r for r in grid['rows'] if r['student']['id'] == student_id), None)
    return [row['cells'][str(l['id'])]['status'] for l in grid['lessons']] if row else []


def collect(store, sid, day: str, history_days: int = 10):
    """Aggregated, pseudonymised facts for one school and day. Returns (payload_for_llm, names_by_token)."""
    d = date.fromisoformat(day)
    classes = store.list_classes(sid)
    pseudonyms: dict[int, str] = {}
    names: dict[str, str] = {}

    def alias(student):
        if student['id'] not in pseudonyms:
            token = TOKEN.format(n=len(pseudonyms) + 1)
            pseudonyms[student['id']] = token
            names[token] = student['full_name']
        return pseudonyms[student['id']]

    per_class, late, absent_today, camera_issues = [], [], [], []
    total = Counter()
    for c in classes:
        grid = store.attendance(sid, c['id'], day)
        if not grid['lessons']:
            continue
        counts = Counter()
        came = 0
        for r in grid['rows']:
            statuses = _day_cells(grid, r['student']['id'])
            done = [s for s in statuses if s in FINISHED]
            counts.update(done)
            if any(s in ('present', 'late') for s in statuses):
                came += 1
            if 'late' in statuses:
                late.append({'ученик': alias(r['student']), 'класс': c['name']})
            if done and all(s == 'absent' for s in done):
                absent_today.append({'ученик': alias(r['student']), 'класс': c['name']})
        for l in grid['lessons']:
            if l['state'] != 'upcoming' and l['camera_id'] and (l['coverage'] or 0) < 0.6:
                camera_issues.append({'класс': c['name'], 'урок': l['subject'], 'время': l['start_time'],
                                      'камера': l.get('camera_name') or '', 'работала': f"{round((l['coverage'] or 0) * 100)}%"})
        total.update(counts)
        per_class.append({'класс': c['name'], 'учеников': len(grid['rows']), 'пришли': came,
                          'посещаемость': f"{round(came / len(grid['rows']) * 100) if grid['rows'] else 0}%",
                          'опоздания': counts['late'], 'пропуски_уроков': counts['absent'], 'нет_данных_камеры': counts['no_data']})

    # repeated whole-day absences over the previous school days
    chronic = Counter()
    for back in range(1, history_days + 1):
        past = d - timedelta(days=back)
        if past.isoweekday() == 7:
            continue
        for c in classes:
            grid = store.attendance(sid, c['id'], past.isoformat())
            if not grid['lessons']:
                continue
            for r in grid['rows']:
                done = [s for s in _day_cells(grid, r['student']['id']) if s in FINISHED]
                if done and all(s == 'absent' for s in done):
                    chronic[(r['student']['id'], r['student']['full_name'], c['name'])] += 1
    repeated = [{'ученик': alias({'id': sid_, 'full_name': name}), 'класс': cls, 'дней_пропущено': n}
                for (sid_, name, cls), n in chronic.most_common(10) if n >= 2]

    students = sum(x['учеников'] for x in per_class)
    came = sum(x['пришли'] for x in per_class)
    payload = {
        'дата': day, 'день_недели': ['', 'понедельник', 'вторник', 'среда', 'четверг', 'пятница', 'суббота', 'воскресенье'][d.isoweekday()],
        'всего_учеников_с_уроками': students, 'пришли_в_школу': came,
        'посещаемость': f'{round(came / students * 100) if students else 0}%',
        'опоздавшие': late[:15], 'не_пришли_совсем': absent_today[:20], 'повторяющиеся_пропуски_за_2_недели': repeated,
        'классы': per_class, 'проблемы_камер': camera_issues[:10],
    }
    return payload, names


def restore_names(text: str, names: dict) -> str:
    return TOKEN_RE.sub(lambda m: names.get(m.group(0), m.group(0)), text)


def template_text(p: dict) -> str:
    if not p['всего_учеников_с_уроками']:
        return f"{p['дата']}: уроков по расписанию не было."
    lines = [f"Посещаемость {p['дата']}: {p['посещаемость']} ({p['пришли_в_школу']} из {p['всего_учеников_с_уроками']})."]
    weak = sorted(p['классы'], key=lambda c: int(c['посещаемость'].rstrip('%')))[:3]
    weak = [c for c in weak if int(c['посещаемость'].rstrip('%')) < 90]
    if weak:
        lines.append('Ниже 90%: ' + ', '.join(f"{c['класс']} — {c['посещаемость']}" for c in weak) + '.')
    if p['опоздавшие']:
        lines.append(f"Опоздали: {len(p['опоздавшие'])} — " + ', '.join(x['ученик'] for x in p['опоздавшие'][:8]) + '.')
    if p['не_пришли_совсем']:
        lines.append(f"Не пришли: {len(p['не_пришли_совсем'])} — " + ', '.join(f"{x['ученик']} ({x['класс']})" for x in p['не_пришли_совсем'][:10]) + '.')
    if p['повторяющиеся_пропуски_за_2_недели']:
        lines.append('Повторяющиеся пропуски: ' + ', '.join(f"{x['ученик']} — {x['дней_пропущено']} дн." for x in p['повторяющиеся_пропуски_за_2_недели']) + '.')
    if p['проблемы_камер']:
        lines.append('Камеры с перебоями: ' + ', '.join(f"{x['класс']} {x['время']} ({x['работала']})" for x in p['проблемы_камер'][:5]) + ' — проверьте журнал вручную.')
    return '\n'.join(lines)


def deepseek_text(payload: dict, api_key: str, model: str = DEFAULT_MODEL, timeout: int = 60) -> str:
    # deepseek-flash is a reasoning model; a short factual report does not need "thinking":
    # disabling it gives a complete answer with ~3x fewer tokens and ~2x lower latency.
    body = json.dumps({'model': model, 'temperature': 0.3, 'max_tokens': 1500, 'thinking': {'type': 'disabled'}, 'messages': [
        {'role': 'system', 'content': SYSTEM_PROMPT},
        {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)}]}).encode('utf-8')
    req = urllib.request.Request(API_URL, data=body, headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + api_key})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read())
    choice = (data.get('choices') or [{}])[0]
    text = (choice.get('message', {}).get('content') or '').strip()
    if not text or choice.get('finish_reason') not in (None, 'stop'):
        raise ValueError(f"incomplete answer ({choice.get('finish_reason')})")  # never send a cut-off report
    return text


def generate(store, sid: int, day: str, api_key: str | None, model: str = DEFAULT_MODEL, llm=deepseek_text):
    """Build, store and return {'text', 'source', 'day'}. Falls back to the template on any AI problem."""
    payload, names = collect(store, sid, day)
    school = store.get_school(sid)
    source, text = 'template', None
    if api_key and school.get('ai_summaries', 1) and payload['всего_учеников_с_уроками']:
        try:
            text, source = llm(payload, api_key, model), 'deepseek'
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError) as exc:
            LOG.warning('DeepSeek summary failed (%s); using template', type(exc).__name__)
    if text is None:
        text = template_text(payload)
    text = restore_names(text, names)[:3500]
    store.save_summary(sid, day, text, source)
    return {'text': text, 'source': source, 'day': day}
