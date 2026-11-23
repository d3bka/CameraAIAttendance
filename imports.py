"""Bulk import / export helpers (stdlib only): CSV and XLSX tables, ZIP archives of photos.

All parsers are defensive: size limits, row limits, no path handling of archive members,
no external XML entities (xml.etree does not resolve them), zip-bomb protection.
"""
from __future__ import annotations

import csv
import io
import re
import zipfile
from xml.etree import ElementTree

MAX_TABLE_BYTES = 5 * 1024 * 1024
MAX_ROWS = 5000
MAX_ZIP_BYTES = 200 * 1024 * 1024
MAX_ZIP_ENTRIES = 5000
MAX_ZIP_UNCOMPRESSED = 600 * 1024 * 1024
MAX_PHOTO_BYTES = 3 * 1024 * 1024


class ImportError_(ValueError):
    pass


# Column aliases (lower-case, without spaces/punctuation) -> canonical field
ALIASES = {
    'code': ['code', 'код', 'кодученика', 'id', 'studentid', 'tabelnomer', 'табельныйномер', 'raqam', 'kod', 'idraqam',
             'personalcode', 'номер'],
    'full_name': ['fullname', 'fio', 'фио', 'имя', 'name', 'ученик', 'ism', 'ismfamiliya', 'familiyaism', 'fish', 'oquvchi',
                  "o'quvchi", 'учащийся', 'student'],
    'last_name': ['lastname', 'surname', 'фамилия', 'familiya'],
    'first_name': ['firstname', 'имя_', 'ism_', 'givenname'],
    'middle_name': ['middlename', 'отчество', 'otasiningismi', 'sharif', 'patronymic'],
    'class': ['class', 'класс', 'sinf', 'grade', 'group', 'группа', 'guruh', 'классгруппа'],
}
TEMPLATE_CSV = '﻿код;фио;класс\nSTU-0001;Каримов Азиз;5-А\nSTU-0002;Юсупова Мадина;5-А\n'


def _norm(header: str) -> str:
    return re.sub(r"[\s_\-.:'`’]", '', (header or '').strip().lower())


def map_columns(headers):
    """Return {field: column_index}. Raises ImportError_ if no name/class column found."""
    normalized = [_norm(h) for h in headers]
    mapping = {}
    for field, aliases in ALIASES.items():
        keys = {_norm(a) for a in aliases}
        for idx, h in enumerate(normalized):
            if h in keys and idx not in mapping.values():
                mapping[field] = idx
                break
    if 'first_name' not in mapping:  # a bare "Имя" column next to "Фамилия" is a first name, not a full name
        if 'last_name' in mapping and 'full_name' in mapping and normalized[mapping['full_name']] in ('имя', 'ism', 'name'):
            mapping['first_name'] = mapping.pop('full_name')
    if 'full_name' not in mapping and 'last_name' not in mapping:
        raise ImportError_('Не найдена колонка с ФИО (например «ФИО» или «Фамилия» + «Имя»)')
    if 'class' not in mapping:
        raise ImportError_('Не найдена колонка «Класс»')
    return mapping


def read_csv(data: bytes):
    text = None
    for enc in ('utf-8-sig', 'cp1251'):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise ImportError_('Не удалось прочитать CSV (ожидается UTF-8 или Windows-1251)')
    sample = text[:4096]
    delimiter = ';' if sample.count(';') >= sample.count(',') else ','
    return [row for row in csv.reader(io.StringIO(text), delimiter=delimiter)]


def read_xlsx(data: bytes):
    """First worksheet of an .xlsx file as a list of rows (strings)."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ImportError_('Файл не является корректным .xlsx') from None
    ns = {'m': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    names = zf.namelist()
    if sum(i.file_size for i in zf.infolist()) > 50 * 1024 * 1024:
        raise ImportError_('Таблица слишком большая')
    shared = []
    if 'xl/sharedStrings.xml' in names:
        root = ElementTree.fromstring(zf.read('xl/sharedStrings.xml'))
        for si in root.findall('m:si', ns):
            shared.append(''.join(t.text or '' for t in si.iter('{%s}t' % ns['m'])))
    sheets = sorted(n for n in names if re.fullmatch(r'xl/worksheets/sheet\d+\.xml', n))
    if not sheets:
        raise ImportError_('В файле нет листов')
    root = ElementTree.fromstring(zf.read(sheets[0]))
    rows = []
    for row in root.iter('{%s}row' % ns['m']):
        values = {}
        for c in row.findall('m:c', ns):
            ref = c.get('r', '')
            col = 0
            for ch in re.match(r'[A-Z]*', ref).group(0):
                col = col * 26 + (ord(ch) - 64)
            col = max(col - 1, len(values)) if not ref else col - 1
            t = c.get('t')
            v = c.find('m:v', ns)
            if t == 's' and v is not None:
                val = shared[int(v.text)] if v.text and v.text.isdigit() and int(v.text) < len(shared) else ''
            elif t == 'inlineStr':
                val = ''.join(x.text or '' for x in c.iter('{%s}t' % ns['m']))
            else:
                val = v.text if v is not None and v.text else ''
                if re.fullmatch(r'-?\d+\.0', val):
                    val = val[:-2]
            values[col] = val
        if values:
            rows.append([values.get(i, '') for i in range(max(values) + 1)])
        if len(rows) > MAX_ROWS + 1:
            break
    return rows


def read_table(filename: str, data: bytes):
    if len(data) > MAX_TABLE_BYTES:
        raise ImportError_('Файл больше 5 МБ')
    if data[:2] == b'PK' or filename.lower().endswith('.xlsx'):
        rows = read_xlsx(data)
    elif filename.lower().endswith(('.csv', '.txt')) or not data[:2] == b'PK':
        rows = read_csv(data)
    else:
        raise ImportError_('Поддерживаются .xlsx и .csv')
    rows = [[(c or '').strip() for c in r] for r in rows]  # keep blank rows so row numbers match Excel
    while rows and not any(rows[-1]):
        rows.pop()
    if not any(any(r) for r in rows):
        raise ImportError_('Таблица пустая')
    if len(rows) > MAX_ROWS + 1:
        raise ImportError_(f'Не более {MAX_ROWS} строк за один импорт')
    return rows


def parse_students(filename: str, data: bytes):
    """-> list of {'row': n, 'code': str|None, 'full_name': str, 'class': str} and list of row errors."""
    rows = read_table(filename, data)
    header_idx = 0
    mapping = None
    for i, row in enumerate(rows[:5]):  # the header may be below a title line
        try:
            mapping = map_columns(row)
            header_idx = i
            break
        except ImportError_ as exc:
            last = exc
    if mapping is None:
        raise last
    out, errors = [], []
    for n, row in enumerate(rows[header_idx + 1:], start=header_idx + 2):
        get = lambda f: row[mapping[f]].strip() if f in mapping and mapping[f] < len(row) else ''
        name = get('full_name') or ' '.join(x for x in (get('last_name'), get('first_name'), get('middle_name')) if x)
        cls = get('class')
        code = get('code') or None
        if not name and not cls:
            continue
        if not name:
            errors.append({'row': n, 'error': 'нет ФИО'})
            continue
        if not cls:
            errors.append({'row': n, 'error': 'нет класса'})
            continue
        out.append({'row': n, 'code': code, 'full_name': re.sub(r'\s+', ' ', name)[:120], 'class': cls[:40]})
    return out, errors


def photo_entries(data: bytes):
    """Yield (student_code, image_bytes, member_name) from a ZIP of photos.

    Accepted layouts: CODE.jpg, CODE_2.jpg, CODE (1).png, or CODE/anything.jpg.
    """
    if len(data) > MAX_ZIP_BYTES:
        raise ImportError_('Архив больше 200 МБ')
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ImportError_('Файл не является ZIP-архивом') from None
    infos = [i for i in zf.infolist() if not i.is_dir()]
    if len(infos) > MAX_ZIP_ENTRIES:
        raise ImportError_(f'Не более {MAX_ZIP_ENTRIES} файлов в архиве')
    if sum(i.file_size for i in infos) > MAX_ZIP_UNCOMPRESSED:
        raise ImportError_('Архив распаковывается в слишком большой объём')
    for info in infos:
        name = info.filename.replace('\\', '/')
        base = name.rsplit('/', 1)[-1]
        if base.startswith(('.', '__MACOSX')) or '__MACOSX/' in name:
            continue
        if not re.search(r'\.(jpe?g|png)$', base, re.I):
            continue
        if info.file_size > MAX_PHOTO_BYTES:
            yield None, None, name
            continue
        parts = name.split('/')
        if len(parts) >= 2 and parts[-2]:
            code = parts[-2]
        else:
            code = re.sub(r'(\s*\(\d+\)|_\d{1,2})?\.(jpe?g|png)$', '', base, flags=re.I)
        yield code.strip(), zf.read(info), name


def csv_bytes(header, rows) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=';', lineterminator='\r\n')
    w.writerow(header)
    for r in rows:
        w.writerow(['' if v is None else v for v in r])
    return ('﻿' + buf.getvalue()).encode('utf-8')
