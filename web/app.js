'use strict';
/* Camera Attendance AI — admin panel. DOM is built with textContent only (no innerHTML) to avoid XSS. */

const state = { me: null, csrf: '', today: '', schools: [], sid: null, page: null, cache: {} };
const $ = id => document.getElementById(id);
const WEEKDAYS = ['', 'Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс'];
const WEEKDAYS_FULL = ['', 'Понедельник', 'Вторник', 'Среда', 'Четверг', 'Пятница', 'Суббота', 'Воскресенье'];
const ROLE_NAMES = { superadmin: 'Супер-админ', school_admin: 'Админ школы', teacher: 'Учитель' };
const KIND_NAMES = { entry: 'Вход', exit: 'Выход', classroom: 'Класс (AI)', observe: 'Наблюдение' };
const STATUS = {
  present: ['Присутствует', 'good'], late: ['Опоздал', 'warn'], absent: ['Отсутствует', 'bad'],
  excused: ['Уваж. причина', 'info'], not_seen: ['Не замечен', 'warn soft'], no_data: ['Нет данных', 'dim'],
  needs_review: ['Проверить', 'warn'], upcoming: ['—', 'muted'], in_progress: ['Идёт урок', 'info soft'],
};
const PHOTO_STATUS = {
  ok: ['Лицо OK', 'good'], unchecked: ['Не проверено', 'dim'], no_face: ['Лицо не найдено', 'bad'],
  multiple_faces: ['Несколько лиц', 'bad'], too_small: ['Лицо мелкое', 'bad'], unreadable: ['Ошибка файла', 'bad'],
  demo: ['Демо-аватар', 'muted'],
};
const EVENT_NAMES = { motion: 'Движение', device_event: 'Событие устройства', access_granted: 'Проход СКУД',
  access_denied: 'Отказ СКУД', face_match: 'Распознавание лица' };

/* ------------------------------------------------------------------ helpers */
function h(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
    else if (key === 'text') node.textContent = value;
    else if (value === true) node.setAttribute(key, '');
    else node.setAttribute(key, String(value));
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}
const tag = (text, kind) => h('span', { class: 'tag ' + (kind || '') }, text);
const statusTag = s => tag(...(STATUS[s] || [s, 'dim']));
const fmtTime = v => v ? new Intl.DateTimeFormat('ru-RU', { timeZone: 'Asia/Tashkent', hour: '2-digit', minute: '2-digit' }).format(new Date(v)) : '—';
const fmtDateTime = v => v ? new Intl.DateTimeFormat('ru-RU', { timeZone: 'Asia/Tashkent', day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' }).format(new Date(v)) : '—';
const pct = v => v === null || v === undefined ? '—' : Math.round(v * 100) + '%';
const isAdmin = () => state.me && (state.me.role === 'superadmin' || state.me.role === 'school_admin');
const isSuper = () => state.me && state.me.role === 'superadmin';
const sPath = p => `/api/s/${state.sid}${p}`;
const photoUrl = id => sPath('/photos/' + id);

function toast(message, error = false) {
  const t = $('toast');
  t.textContent = message;
  t.className = error ? 'show error' : 'show';
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { t.className = ''; }, 4500);
}

function store(key, value) {
  try { if (value === undefined) return localStorage.getItem(key); localStorage.setItem(key, value); } catch { return null; }
  return null;
}

async function api(path, method = 'GET', data) {
  const opts = { method, headers: {}, credentials: 'same-origin' };
  if (method !== 'GET') opts.headers['X-CSRF-Token'] = state.csrf;
  if (data !== undefined) { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(data); }
  const response = await fetch(path, opts);
  let json = {};
  try { json = await response.json(); } catch { /* empty */ }
  if (response.status === 401 && path !== '/api/login') { showLogin(); throw new Error('Сессия истекла, войдите снова'); }
  if (!response.ok) throw new Error(json.error || `HTTP ${response.status}`);
  return json;
}

function formData(form) {
  const data = {};
  for (const element of form.elements) {
    if (!element.name) continue;
    if (element.type === 'checkbox') data[element.name] = element.checked;
    else data[element.name] = element.value;
  }
  return data;
}

function submitting(form, handler) {
  form.addEventListener('submit', async event => {
    event.preventDefault();
    const button = form.querySelector('button[type=submit]');
    if (button) button.disabled = true;
    try { await handler(formData(form), form); } catch (error) { toast(error.message, true); }
    finally { if (button) button.disabled = false; }
  });
  return form;
}

function field(label, input) { return h('label', {}, label, input); }
function input(name, attrs = {}) { return h('input', { name, ...attrs }); }
function select(name, options, attrs = {}) {
  return h('select', { name, ...attrs }, options.map(([value, text]) => h('option', { value }, text)));
}
function panel(title, ...children) {
  return h('div', { class: 'panel' }, title ? h('div', { class: 'panel-title' }, typeof title === 'string' ? h('h2', {}, title) : title) : null, ...children);
}
function heading(eyebrow, title, text, ...tools) {
  return h('div', { class: 'heading' }, h('div', {}, h('div', { class: 'eyebrow' }, eyebrow), h('h1', {}, title), text ? h('p', {}, text) : null),
    tools.length ? h('div', { class: 'toolbar' }, tools) : null);
}
function empty(text) { return h('div', { class: 'empty' }, text); }
function table(headers, rows, emptyText = 'Нет данных') {
  return h('div', { class: 'table-wrap' }, h('table', {},
    h('thead', {}, h('tr', {}, headers.map(x => h('th', {}, x)))),
    h('tbody', {}, rows.length ? rows : h('tr', {}, h('td', { class: 'empty', colspan: headers.length }, emptyText)))));
}

function openDialog(title, body, actions) {
  const dialog = $('dialog');
  const container = $('dialog-body');
  container.replaceChildren(h('h2', {}, title), body,
    h('div', { class: 'dialog-actions' }, actions || [], h('button', { class: 'btn secondary', type: 'button', onclick: closeDialog }, 'Закрыть')));
  if (!dialog.open) dialog.showModal();
}
function closeDialog() { const d = $('dialog'); if (d.open) d.close(); }

function secretBox(title, lines) {
  openDialog(title, h('div', {},
    h('p', { class: 'hint' }, 'Показывается один раз. Скопируйте и передайте безопасным способом.'),
    lines.map(([label, value]) => h('div', { class: 'secret' }, h('small', {}, label), h('code', {}, value),
      h('button', { type: 'button', class: 'mini-btn neutral', onclick: () => navigator.clipboard?.writeText(value).then(() => toast('Скопировано')) }, 'Копировать')))));
}

async function confirmDialog(message) { return window.confirm(message); }

/* ------------------------------------------------------------------ auth */
function showLogin() {
  state.me = null;
  $('app-view').hidden = true;
  $('login-view').hidden = false;
  closeDialog();
}

async function boot() {
  try {
    const me = await api('/api/me');
    if (!me.user) { showLogin(); return; }
    state.me = me.user; state.csrf = me.csrf; state.today = me.today;
  } catch { showLogin(); return; }
  $('login-view').hidden = true;
  $('app-view').hidden = false;
  $('user-name').textContent = state.me.full_name || state.me.username;
  $('user-role').textContent = ROLE_NAMES[state.me.role] + (state.me.school_name ? ' · ' + state.me.school_name : '');
  await loadSchools();
  buildNav();
  const initial = (location.hash || '').slice(1);
  navigate(pages[initial] && allowed(initial) ? initial : (isSuper() && !state.sid ? 'schools' : 'overview'));
}

async function loadSchools() {
  const { schools } = await api('/api/schools');
  state.schools = schools;
  const sw = $('school-switch');
  if (isSuper()) {
    const saved = Number(store('cat.school'));
    state.sid = schools.some(s => s.id === saved) ? saved : (schools[0] ? schools[0].id : null);
    sw.replaceChildren(...schools.map(s => h('option', { value: s.id }, s.name)));
    if (state.sid) sw.value = String(state.sid);
    sw.hidden = !schools.length;
  } else {
    state.sid = state.me.school_id;
    sw.hidden = true;
  }
  $('crumb-school').textContent = (schools.find(s => s.id === state.sid) || {}).name || 'Платформа';
}

/* ------------------------------------------------------------------ navigation */
const NAV = [
  ['schools', '🏫', 'Школы', 'super'],
  ['overview', '▦', 'Обзор', 'school'],
  ['attendance', '☷', 'Журнал', 'school'],
  ['schedule', '◷', 'Расписание', 'school'],
  ['students', '♙', 'Классы и ученики', 'school'],
  ['reviews', '◎', 'Проверка AI', 'school'],
  ['cameras', '▣', 'Камеры и AI', 'school'],
  ['events', '≡', 'События', 'school'],
  ['tokens', '⚿', 'Токены устройств', 'admin'],
  ['users', '♟', 'Пользователи', 'admin'],
  ['audit', '⎙', 'Аудит', 'admin'],
];
function allowed(page) {
  const item = NAV.find(n => n[0] === page);
  if (!item) return false;
  if (item[3] === 'super') return isSuper();
  if (item[3] === 'admin') return isAdmin();
  return true;
}
function buildNav() {
  $('nav').replaceChildren(...NAV.filter(n => allowed(n[0])).map(([page, icon, label]) =>
    h('button', { class: 'nav', 'data-page': page, type: 'button', onclick: () => navigate(page) }, h('span', {}, icon), label)));
}
async function navigate(page) {
  if (!allowed(page)) page = 'overview';
  const item = NAV.find(n => n[0] === page);
  if (item[3] !== 'super' && !state.sid) {
    page = isSuper() ? 'schools' : page;
  }
  state.page = page;
  history.replaceState(null, '', '#' + page);
  document.querySelectorAll('.nav').forEach(b => b.classList.toggle('active', b.dataset.page === page));
  $('crumb').textContent = NAV.find(n => n[0] === page)[2];
  const content = $('content');
  content.replaceChildren(h('div', { class: 'loading' }, 'Загрузка…'));
  try {
    const node = await pages[page]();
    if (state.page === page) content.replaceChildren(node);
  } catch (error) {
    content.replaceChildren(panel(null, h('div', { class: 'form-error' }, error.message)));
  }
  window.scrollTo({ top: 0 });
}
const refresh = () => navigate(state.page);

/* ------------------------------------------------------------------ pages */
const pages = {};

pages.schools = async () => {
  await loadSchools();
  const rows = state.schools.map(s => h('tr', {},
    h('td', {}, h('strong', {}, s.name), h('div', { class: 'device-meta' }, s.city || '')),
    h('td', {}, s.classes), h('td', {}, s.students), h('td', {}, s.cameras), h('td', {}, s.users),
    h('td', {}, `${s.seen_today} / ${s.students}`),
    h('td', { class: 'actions' },
      h('button', { class: 'mini-btn neutral', type: 'button', onclick: () => selectSchool(s.id, 'overview') }, 'Открыть'),
      h('button', { class: 'mini-btn', type: 'button', onclick: () => deleteSchool(s) }, 'Удалить'))));
  const form = submitting(h('form', {},
    field('Название школы', input('name', { required: true, maxlength: 120, placeholder: 'Школа №45' })),
    field('Город', input('city', { maxlength: 80, placeholder: 'Ташкент' })),
    field('Опоздание после начала урока, мин', input('late_grace_min', { type: 'number', min: 0, max: 60, value: 10 })),
    h('button', { class: 'btn primary', type: 'submit' }, 'Создать школу')), async data => {
    data.late_grace_min = Number(data.late_grace_min);
    const school = await api('/api/schools', 'POST', data);
    toast('Школа создана');
    await loadSchools();
    selectSchool(school.id, 'schools');
  });
  return h('div', {}, heading('PLATFORM', 'Школы', 'Все подключённые школы. Данные каждой школы изолированы друг от друга.'),
    h('div', { class: 'twocol form-layout' },
      panel('Список школ', table(['Школа', 'Классы', 'Ученики', 'Камеры', 'Польз.', 'Замечены сегодня', ''], rows, 'Школ пока нет')),
      panel('Новая школа', form, h('div', { class: 'note' }, 'После создания добавьте администратора школы в разделе «Пользователи», затем классы, учеников с фото, камеры и расписание.'))));
};

async function deleteSchool(s) {
  const typed = window.prompt(`Удаление школы удалит ВСЕ её данные: учеников, фото, события, пользователей.\nВведите название школы для подтверждения:\n${s.name}`);
  if (typed !== s.name) { if (typed !== null) toast('Название не совпало — удаление отменено', true); return; }
  try { await api(`/api/s/${s.id}`, 'DELETE'); toast('Школа удалена'); if (state.sid === s.id) store('cat.school', ''); await loadSchools(); refresh(); }
  catch (e) { toast(e.message, true); }
}

function selectSchool(id, page) {
  state.sid = id;
  store('cat.school', String(id));
  $('school-switch').value = String(id);
  $('crumb-school').textContent = (state.schools.find(s => s.id === id) || {}).name || '';
  state.cache = {};
  navigate(page || state.page);
}

async function classesList() {
  const { classes } = await api(sPath('/classes'));
  return classes;
}

function classPicker(classes, current, onChange) {
  const sel = h('select', { class: 'datepick', 'aria-label': 'Класс' }, classes.map(c => h('option', { value: c.id }, c.name)));
  if (current) sel.value = String(current);
  sel.addEventListener('change', () => onChange(Number(sel.value)));
  return h('label', {}, 'Класс', sel);
}
function datePicker(value, onChange) {
  const d = h('input', { type: 'date', class: 'datepick', value, max: state.today });
  d.addEventListener('change', () => d.value && onChange(d.value));
  return h('label', {}, 'Дата', d);
}

pages.overview = async () => {
  const date = state.cache.date || state.today;
  const o = await api(sPath('/overview?date=' + date));
  const st = o.stats;
  const stats = h('div', { class: 'stats' },
    statCard('Учеников', st.students, `${st.ai_ready} с фото, готовыми для AI`),
    statCard('Замечены', st.seen_today, 'Камера, СКУД или отметка учителя'),
    statCard('Не замечены', st.not_seen, 'Ни камера, ни СКУД, ни учитель не отметили сегодня'),
    statCard('Камеры онлайн', `${st.cameras_online}/${st.cameras}`, 'По сигналам AI-воркера (3 мин)'));
  const alerts = [];
  if (st.pending_reviews) alerts.push(h('div', { class: 'alert warn' }, `Ожидают проверки учителем: ${st.pending_reviews} распознаваний с низкой уверенностью. `,
    h('button', { class: 'link-btn', type: 'button', onclick: () => navigate('reviews') }, 'Проверить →')));
  if (st.no_photo) alerts.push(h('div', { class: 'alert' }, `У ${st.no_photo} учеников нет фото — AI не сможет их распознать.`));
  const offline = o.cameras.filter(c => !c.online && c.kind === 'classroom');
  if (offline.length) alerts.push(h('div', { class: 'alert bad' }, `Нет сигнала от камер: ${offline.map(c => c.name).join(', ')}. Уроки в этих кабинетах будут помечены «нет данных» — отметьте вручную.`));

  const lessons = o.lessons.map(l => h('div', { class: 'event-item' },
    h('div', { class: 'event-icon ' + (l.state === 'in_progress' ? 'verified' : '') }, l.state === 'in_progress' ? '●' : l.state === 'finished' ? '✓' : '◷'),
    h('div', { class: 'event-body' }, h('strong', {}, `${l.class_name} · ${l.subject}`),
      h('small', {}, `${l.start_time}–${l.end_time} · ${l.teacher || 'учитель не указан'} · ${l.camera_name || 'без камеры'}`)),
    !l.camera_id ? tag('без камеры', 'dim') : l.state === 'finished' ? tag('завершён', 'muted') : tag(l.camera_online ? 'камера онлайн' : 'камера офлайн', l.camera_online ? 'good' : 'bad'),
    h('button', { class: 'mini-btn neutral', type: 'button', onclick: () => { state.cache.classId = l.class_id; state.cache.date = date; navigate('attendance'); } }, 'Журнал')));
  const events = o.events.map(eventItem);
  return h('div', {},
    heading('CONTROL CENTER', o.school.name, `Сводка на ${date} (${o.timezone}).`, datePicker(date, d => { state.cache.date = d; refresh(); }),
      h('button', { class: 'btn secondary', type: 'button', onclick: refresh }, '↻ Обновить')),
    stats, alerts.length ? h('div', { class: 'alerts' }, alerts) : null,
    h('div', { class: 'twocol' },
      panel(`Уроки: ${WEEKDAYS_FULL[new Date(date + 'T12:00:00').getDay() || 7]}`, lessons.length ? lessons : empty('В этот день уроков нет.')),
      panel(h('div', { class: 'panel-title-row' }, h('h2', {}, 'Последние события'),
        h('button', { class: 'link-btn', type: 'button', onclick: () => navigate('events') }, 'Все →')), events.length ? events : empty('Событий нет.'))));
};

function statCard(label, value, hint) {
  return h('div', { class: 'stat' }, h('span', {}, label), h('strong', {}, value), h('small', {}, hint));
}
function eventItem(e) {
  const ok = e.verified;
  return h('div', { class: 'event-item' },
    h('div', { class: 'event-icon ' + (ok ? 'verified' : '') }, ok ? '✓' : e.review_status === 'pending' ? '?' : '◌'),
    h('div', { class: 'event-body' }, h('strong', {}, e.full_name ? `${e.full_name} (${e.class_name})` : EVENT_NAMES[e.event_type] || e.event_type),
      h('small', {}, `${e.camera_name} · ${EVENT_NAMES[e.event_type] || e.event_type}${e.confidence ? ' · ' + pct(e.confidence) : ''}`)),
    h('span', { class: 'time' }, fmtTime(e.occurred_at)));
}

pages.attendance = async () => {
  const classes = await classesList();
  if (!classes.length) return h('div', {}, heading('ATTENDANCE', 'Журнал посещаемости'), panel(null, empty('Сначала создайте классы и расписание.')));
  const classId = classes.some(c => c.id === state.cache.classId) ? state.cache.classId : classes[0].id;
  state.cache.classId = classId;
  const date = state.cache.date || state.today;
  const a = await api(sPath(`/attendance?class_id=${classId}&date=${date}`));
  const legend = h('div', { class: 'legend' }, ['present', 'late', 'absent', 'excused', 'not_seen', 'needs_review', 'no_data'].map(s => statusTag(s)));
  let body;
  if (!a.lessons.length) body = empty(`У класса ${a.class.name} нет уроков в этот день (${WEEKDAYS_FULL[a.weekday]}).`);
  else {
    const head = ['Ученик', ...a.lessons.map(l => h('div', { class: 'lesson-head' }, h('b', {}, `${l.start_time}–${l.end_time}`), h('span', {}, l.subject),
      l.camera_id ? h('small', { class: l.coverage !== null && l.coverage < 0.6 && l.state !== 'upcoming' ? 'bad-text' : '' },
        `${l.camera_name} · покрытие ${pct(l.coverage)}`) : h('small', {}, 'без камеры')))];
    const rows = a.rows.map(r => h('tr', {}, h('td', {}, h('strong', {}, r.student.full_name), h('div', { class: 'device-meta' }, r.student.code)),
      a.lessons.map(l => {
        const c = r.cells[String(l.id)];
        const title = [c.source === 'manual' ? `Отмечено вручную${c.by ? ': ' + c.by : ''}` : c.source === 'ai' ? 'По камере (AI)' : c.source === 'entry' ? 'По входу (СКУД/камера входа)' : '',
          c.first_seen ? 'замечен в ' + fmtTime(c.first_seen) : '', c.note || ''].filter(Boolean).join(' · ');
        return h('td', { class: 'cell' }, h('button', { type: 'button', class: 'cell-btn', title, disabled: c.status === 'upcoming' || date > state.today,
          onclick: () => markDialog(r.student, l, c, date) }, statusTag(c.status), c.source === 'manual' ? h('span', { class: 'manual-dot', title: 'ручная отметка' }, '✎') : null,
          c.first_seen ? h('small', {}, fmtTime(c.first_seen)) : null));
      })));
    body = table(head, rows, 'В классе нет учеников');
  }
  return h('div', {},
    heading('ATTENDANCE', 'Журнал посещаемости', `Порог опоздания: ${a.late_grace_min} мин. Ручная отметка учителя всегда важнее автоматической.`,
      classPicker(classes, classId, id => { state.cache.classId = id; refresh(); }), datePicker(date, d => { state.cache.date = d; refresh(); }),
      h('button', { class: 'btn secondary', type: 'button', onclick: refresh }, '↻')),
    panel(h('div', { class: 'panel-title-row' }, h('h2', {}, `${a.class.name} · ${WEEKDAYS_FULL[a.weekday]}, ${a.date}`), legend), body),
    h('div', { class: 'note' }, h('b', {}, 'Как читать: '), 'отметки ставятся автоматически по камерам и расписанию. «Отсутствует» — камера исправно работала весь урок, а ребёнка в кадре не было. «Нет данных» — камера была офлайн: система не гадает и просит отметить вручную. Любую отметку можно исправить кликом — ручная всегда главнее.'));
};

function markDialog(student, lesson, cell, date) {
  const note = h('input', { maxlength: 200, placeholder: 'Комментарий (необязательно)', value: cell.note || '' });
  const choose = status => async () => {
    try { await api(sPath('/marks'), 'PUT', { student_id: student.id, lesson_id: lesson.id, day: date, status, note: note.value }); closeDialog(); toast('Отметка сохранена'); refresh(); }
    catch (e) { toast(e.message, true); }
  };
  openDialog(`${student.full_name}`, h('div', {},
    h('p', { class: 'hint' }, `${lesson.subject}, ${lesson.start_time}–${lesson.end_time}, ${date}. Сейчас: `, statusTag(cell.status)),
    h('div', { class: 'mark-buttons' }, ['present', 'late', 'absent', 'excused'].map(s =>
      h('button', { type: 'button', class: 'btn secondary', onclick: choose(s) }, STATUS[s][0]))),
    note,
    cell.source === 'manual' ? h('button', { type: 'button', class: 'link-btn', onclick: choose('clear') }, 'Убрать ручную отметку (вернуть автоматическую)') : null));
}

pages.schedule = async () => {
  const [classes, camerasResp] = await Promise.all([classesList(), api(sPath('/cameras'))]);
  if (!classes.length) return h('div', {}, heading('SCHEDULE', 'Расписание'), panel(null, empty('Сначала создайте классы в разделе «Классы и ученики».')));
  const classId = classes.some(c => c.id === state.cache.classId) ? state.cache.classId : classes[0].id;
  state.cache.classId = classId;
  const { lessons } = await api(sPath('/lessons?class_id=' + classId));
  const cams = camerasResp.cameras.filter(c => c.kind === 'classroom');
  const days = [1, 2, 3, 4, 5, 6].concat(lessons.some(l => l.weekday === 7) ? [7] : []);
  const week = h('div', { class: 'week' }, days.map(d => h('div', { class: 'day' }, h('div', { class: 'day-title' }, WEEKDAYS_FULL[d]),
    lessons.filter(l => l.weekday === d).map(l => h('div', { class: 'lesson' },
      h('b', {}, `${l.start_time}–${l.end_time}`), h('span', {}, l.subject),
      h('small', {}, [l.teacher, l.camera_name ? '📷 ' + l.camera_name : 'без камеры'].filter(Boolean).join(' · ')),
      isAdmin() ? h('button', { class: 'x-btn', type: 'button', title: 'Удалить урок', onclick: async () => {
        if (!await confirmDialog(`Удалить урок «${l.subject}» ${WEEKDAYS[l.weekday]} ${l.start_time}?`)) return;
        try { await api(sPath('/lessons/' + l.id), 'DELETE'); toast('Урок удалён'); refresh(); } catch (e) { toast(e.message, true); }
      } }, '×') : null)),
    lessons.some(l => l.weekday === d) ? null : h('div', { class: 'empty small' }, 'нет уроков'))));
  let form = null;
  if (isAdmin()) {
    form = submitting(h('form', { class: 'inline-form' },
      field('День', select('weekday', [1, 2, 3, 4, 5, 6, 7].map(d => [d, WEEKDAYS_FULL[d]]))),
      field('Начало', input('start_time', { type: 'time', required: true, value: '08:00' })),
      field('Конец', input('end_time', { type: 'time', required: true, value: '08:45' })),
      field('Предмет', input('subject', { required: true, maxlength: 80, placeholder: 'Математика' })),
      field('Учитель', input('teacher', { maxlength: 120, placeholder: 'Иванова А.' })),
      field('Камера кабинета', select('camera_id', [['', '— без камеры —'], ...cams.map(c => [c.id, `${c.name} (${c.room})`])])),
      h('button', { class: 'btn primary', type: 'submit' }, '+ Добавить урок')), async data => {
      await api(sPath('/lessons'), 'POST', { ...data, class_id: classId, weekday: Number(data.weekday), camera_id: data.camera_id ? Number(data.camera_id) : null });
      toast('Урок добавлен'); refresh();
    });
  }
  return h('div', {}, heading('SCHEDULE', 'Расписание', 'Камера кабинета нужна, чтобы AI отмечал присутствие на конкретном уроке.',
    classPicker(classes, classId, id => { state.cache.classId = id; refresh(); })),
  form ? panel('Новый урок', form) : null, panel(null, week));
};

pages.students = async () => {
  const classes = await classesList();
  const classId = classes.some(c => c.id === state.cache.classId) ? state.cache.classId : (classes[0] || {}).id;
  state.cache.classId = classId;
  const { students } = classId ? await api(sPath('/students?class_id=' + classId)) : { students: [] };
  const classItems = classes.map(c => h('div', { class: 'person-item' + (c.id === classId ? ' selected' : '') },
    h('button', { class: 'plain', type: 'button', onclick: () => { state.cache.classId = c.id; refresh(); } }, h('strong', {}, c.name), h('small', {}, ` ${c.students} уч.`)),
    isAdmin() ? h('button', { class: 'mini-btn', type: 'button', onclick: async () => {
      if (!await confirmDialog(`Удалить класс ${c.name}?`)) return;
      try { await api(sPath('/classes/' + c.id), 'DELETE'); toast('Класс удалён'); refresh(); } catch (e) { toast(e.message, true); }
    } }, 'Удалить') : null));
  const classForm = isAdmin() ? submitting(h('form', { class: 'row-form' }, input('name', { required: true, maxlength: 40, placeholder: '5-А' }),
    h('button', { class: 'btn primary', type: 'submit' }, '+ Класс')), async (data, f) => { await api(sPath('/classes'), 'POST', data); f.reset(); toast('Класс создан'); refresh(); }) : null;

  const rows = students.map(s => h('tr', {},
    h('td', {}, h('strong', {}, s.full_name), h('div', { class: 'device-meta' }, s.code)),
    h('td', {}, h('div', { class: 'thumbs' }, s.photos.map(p => h('div', { class: 'thumb' },
      h('img', { src: photoUrl(p.id), alt: 'фото', loading: 'lazy' }), tag(...PHOTO_STATUS[p.face_status] || [p.face_status, 'dim']),
      isAdmin() ? h('button', { class: 'x-btn', type: 'button', title: 'Удалить фото', onclick: async () => {
        if (!await confirmDialog('Удалить это фото?')) return;
        try { await api(sPath('/photos/' + p.id), 'DELETE'); toast('Фото удалено'); refresh(); } catch (e) { toast(e.message, true); }
      } }, '×') : null)),
    isAdmin() && s.photos.length < 5 ? uploadButton(s) : null)),
    h('td', {}, s.ai_ready ? tag('Готов', 'good') : s.photos.some(p => p.face_status === 'unchecked') ? tag('Ждёт проверки AI', 'dim') : tag('Нужны фото', 'bad')),
    isAdmin() ? h('td', { class: 'actions' }, h('button', { class: 'mini-btn', type: 'button', onclick: async () => {
      if (!await confirmDialog(`Удалить ${s.full_name}? Будут удалены все фото и события ученика.`)) return;
      try { await api(sPath('/students/' + s.id), 'DELETE'); toast('Ученик и его данные удалены'); refresh(); } catch (e) { toast(e.message, true); }
    } }, 'Удалить')) : null));

  const studentForm = isAdmin() && classId ? submitting(h('form', {},
    field('Код ученика (уникален в школе)', input('code', { required: true, pattern: '[a-zA-Z0-9_.\\-]{1,50}', placeholder: 'STU-0001' })),
    field('ФИО', input('full_name', { required: true, maxlength: 120 })),
    field('Класс', select('class_id', classes.map(c => [c.id, c.name]))),
    h('label', { class: 'checkbox-row' }, h('input', { type: 'checkbox', name: 'consent_confirmed', required: true }),
      h('span', {}, 'Письменное согласие родителей / законных представителей на обработку фото получено и хранится в школе.')),
    h('button', { class: 'btn primary', type: 'submit' }, 'Добавить ученика')), async (data, f) => {
    data.class_id = Number(data.class_id);
    await api(sPath('/students'), 'POST', data); f.reset(); toast('Ученик добавлен'); refresh();
  }) : null;
  if (studentForm) studentForm.querySelector('select').value = String(classId);

  return h('div', {}, heading('DIRECTORY', 'Классы и ученики', 'Для надёжного распознавания загрузите 2–5 чётких фото лица каждого ребёнка (анфас, хороший свет, без очков от солнца).'),
    h('div', { class: 'twocol students-layout' },
      h('div', {}, panel('Классы', classItems.length ? classItems : empty('Классов нет'), classForm), studentForm ? panel('Новый ученик', studentForm) : null),
      panel(classId ? `Ученики класса ${(classes.find(c => c.id === classId) || {}).name}` : 'Ученики',
        table(['Ученик', 'Фото (до 5)', 'AI', isAdmin() ? '' : null].filter(x => x !== null), rows, 'В классе нет учеников'),
        h('div', { class: 'note' }, 'Статус «Лицо OK» ставит AI-воркер после проверки фото. Фото хранятся на сервере школы и доступны только авторизованным сотрудникам.'))));
};

function uploadButton(student) {
  const file = h('input', { type: 'file', accept: 'image/jpeg,image/png', multiple: true, hidden: true });
  file.addEventListener('change', async () => {
    const files = [...file.files].slice(0, 5 - student.photos.length);
    let done = 0;
    for (const f of files) {
      try { await api(sPath(`/students/${student.id}/photos`), 'POST', { data_base64: await prepareImage(f) }); done++; }
      catch (e) { toast(`${f.name}: ${e.message}`, true); }
    }
    if (done) { toast(`Загружено фото: ${done}`); refresh(); }
  });
  return h('button', { class: 'thumb add', type: 'button', title: 'Загрузить фото', onclick: () => file.click() }, '+', file);
}

async function prepareImage(f) {
  if (!/^image\/(jpeg|png)$/.test(f.type)) throw new Error('только JPEG или PNG');
  let blob = f;
  try {
    const bitmap = await createImageBitmap(f);
    const scale = Math.min(1, 1600 / Math.max(bitmap.width, bitmap.height));
    if (scale < 1 || f.size > 2.5 * 1024 * 1024) {
      const canvas = document.createElement('canvas');
      canvas.width = Math.round(bitmap.width * scale); canvas.height = Math.round(bitmap.height * scale);
      canvas.getContext('2d').drawImage(bitmap, 0, 0, canvas.width, canvas.height);
      blob = await new Promise(resolve => canvas.toBlob(resolve, 'image/jpeg', 0.92));
    }
  } catch { /* use original file */ }
  if (blob.size > 3 * 1024 * 1024) throw new Error('файл больше 3 МБ');
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(',')[1]);
    reader.onerror = () => reject(new Error('не удалось прочитать файл'));
    reader.readAsDataURL(blob);
  });
}

pages.reviews = async () => {
  const { reviews } = await api(sPath('/reviews'));
  const cards = reviews.map(r => h('div', { class: 'review' },
    r.photo_id ? h('img', { src: photoUrl(r.photo_id), alt: 'эталонное фото' }) : h('div', { class: 'initial big' }, r.full_name.slice(0, 2)),
    h('div', { class: 'event-body' }, h('strong', {}, `${r.full_name} · ${r.class_name}`),
      h('small', {}, `${r.camera_name} (${r.room}) · ${fmtDateTime(r.occurred_at)} · уверенность ${pct(r.confidence)}`)),
    h('div', { class: 'actions' },
      h('button', { class: 'btn primary small', type: 'button', onclick: () => decide(r.id, 'confirm') }, 'Да, это он/она'),
      h('button', { class: 'btn secondary small', type: 'button', onclick: () => decide(r.id, 'reject') }, 'Нет'))));
  return h('div', {}, heading('AI REVIEW', 'Проверка распознаваний', 'Камера узнала ребёнка с низкой уверенностью (плохой свет, далеко, поворот головы). Подтвердите, был ли ребёнок на уроке.'),
    panel(`Ожидают решения: ${reviews.length}`, cards.length ? cards : empty('Очередь пуста 🎉')));
};
async function decide(id, decision) {
  try { await api(sPath('/reviews/' + id), 'POST', { decision }); toast(decision === 'confirm' ? 'Подтверждено' : 'Отклонено'); refresh(); }
  catch (e) { toast(e.message, true); }
}

pages.cameras = async () => {
  const [{ cameras }, school] = await Promise.all([api(sPath('/cameras')), api('/api/schools').then(r => r.schools.find(s => s.id === state.sid))]);
  const rows = cameras.map(c => h('tr', {},
    h('td', {}, h('strong', {}, c.name), h('div', { class: 'device-meta' }, c.camera_key)),
    h('td', {}, c.room), h('td', {}, KIND_NAMES[c.kind]),
    h('td', {}, c.online ? (c.stats && c.stats.idle ? tag('ждёт урока', 'info') : tag('смотрит', 'good')) : tag(c.last_seen_at ? 'офлайн' : 'нет сигнала', c.kind === 'classroom' ? 'bad' : 'dim'),
      h('div', { class: 'device-meta' }, c.last_seen_at ? 'сигнал ' + fmtDateTime(c.last_seen_at) + (c.online && c.stats && !c.stats.idle && c.stats.faces !== undefined ? ` · лиц/мин: ${c.stats.faces}` : '') : '')),
    isAdmin() ? h('td', { class: 'actions' }, h('button', { class: 'mini-btn', type: 'button', onclick: async () => {
      if (!await confirmDialog(`Удалить камеру ${c.name}? Её события будут удалены.`)) return;
      try { await api(sPath('/cameras/' + c.id), 'DELETE'); toast('Камера удалена'); refresh(); } catch (e) { toast(e.message, true); }
    } }, 'Удалить')) : null));
  const camForm = isAdmin() ? submitting(h('form', {},
    field('Идентификатор (латиница)', input('camera_key', { required: true, pattern: '[a-zA-Z0-9_.\\-]{1,50}', placeholder: 'room201' })),
    field('Название', input('name', { required: true, maxlength: 120, placeholder: 'Камера каб. 201' })),
    field('Кабинет / зона', input('room', { required: true, maxlength: 80, placeholder: 'Кабинет 201' })),
    field('Назначение', select('kind', [['classroom', 'Камера в классе (AI-распознавание)'], ['entry', 'Вход в школу (турникет/камера)'], ['exit', 'Выход'], ['observe', 'Только наблюдение']])),
    h('button', { class: 'btn primary', type: 'submit' }, 'Добавить камеру')), async (data, f) => { await api(sPath('/cameras'), 'POST', data); f.reset(); toast('Камера добавлена'); refresh(); }) : null;
  const settings = isAdmin() && school ? submitting(h('form', {},
    field('Опоздание после начала урока, мин', input('late_grace_min', { type: 'number', min: 0, max: 60, value: school.late_grace_min })),
    field('Авто-отметка при уверенности от', input('face_auto_threshold', { type: 'number', step: '0.01', min: 0.3, max: 0.95, value: school.face_auto_threshold })),
    field('На проверку учителю при уверенности от', input('face_review_threshold', { type: 'number', step: '0.01', min: 0.2, max: 0.95, value: school.face_review_threshold })),
    h('label', { class: 'checkbox-row' }, h('input', { type: 'checkbox', name: 'auto_absent', checked: !!school.auto_absent }),
      h('span', {}, 'Автоматически ставить «Отсутствует», если камера работала весь урок, а ребёнка не было в кадре')),
    h('button', { class: 'btn primary', type: 'submit' }, 'Сохранить')), async data => {
    await api(`/api/s/${state.sid}`, 'PUT', { late_grace_min: Number(data.late_grace_min), face_auto_threshold: Number(data.face_auto_threshold),
      face_review_threshold: Number(data.face_review_threshold), auto_absent: data.auto_absent });
    toast('Настройки сохранены');
  }) : null;
  return h('div', {}, heading('DEVICES & AI', 'Камеры и распознавание', 'Каждая камера в классе обслуживается AI-воркером, который шлёт распознавания и сигнал «я работаю» раз в минуту.'),
    h('div', { class: 'twocol form-layout' },
      h('div', {}, panel('Камеры школы', table(['Камера', 'Кабинет', 'Тип', 'Статус', isAdmin() ? '' : null].filter(x => x !== null), rows, 'Камер нет')),
        panel('Автоматический режим', h('ol', { class: 'steps' },
          h('li', {}, 'Добавьте камеры кабинетов и привяжите их к урокам в «Расписании».'),
          h('li', {}, 'На компьютере в сети камер: ', h('code', {}, `py -3 manage.py worker-config --school-id ${state.sid}`),
            ' — создаст токен и файл со списком камер; впишите в него RTSP-адреса (основной поток 1080p).'),
          h('li', {}, 'Один раз: ', h('code', {}, 'powershell -ExecutionPolicy Bypass -File scripts\\install_autostart.ps1'),
            ' — платформа и AI будут запускаться сами при включении компьютера.')),
          h('div', { class: 'note' }, 'Дальше всё автоматически: AI включает каждую камеру только на время её уроков, сам отмечает присутствие, опоздания и отсутствие, а сигнал «камера работает» приходит раз в минуту.'),
        h('div', { class: 'note' }, h('b', {}, 'Старые камеры: '), 'если лица в кадре меньше ~40 px, AI их пропустит, а урок получит статус «нет данных», а не «отсутствует». Используйте основной поток 1080p, камеру над доской, направленную на парты.'))),
      h('div', {}, camForm ? panel('Новая камера', camForm) : null, settings ? panel('Параметры AI и опозданий', settings,
        h('div', { class: 'note' }, 'Рекомендуемо: авто 0.45, проверка 0.363. Выше порог — меньше ошибок, но больше ручной проверки.')) : null)));
};

pages.events = async () => {
  const date = state.cache.date || state.today;
  const { events } = await api(sPath('/events?date=' + date));
  const rows = events.map(e => h('tr', {}, h('td', {}, fmtTime(e.occurred_at)), h('td', {}, e.camera_name),
    h('td', {}, EVENT_NAMES[e.event_type] || e.event_type), h('td', {}, e.full_name ? `${e.full_name} (${e.class_name})` : '—'),
    h('td', {}, e.confidence ? pct(e.confidence) : '—'),
    h('td', {}, e.verified ? tag('Подтверждено', 'good') : e.review_status === 'pending' ? tag('На проверке', 'warn') : e.review_status === 'rejected' ? tag('Отклонено', 'bad') : tag('Без идентификации', 'dim'))));
  return h('div', {}, heading('AUDIT LOG', 'События', 'Распознавания AI, проходы СКУД и движение. До 300 записей за день.', datePicker(date, d => { state.cache.date = d; refresh(); })),
    panel(null, table(['Время', 'Камера', 'Тип', 'Ученик', 'Уверенность', 'Статус'], rows, 'Событий нет')));
};

pages.tokens = async () => {
  const { tokens } = await api(sPath('/tokens'));
  const rows = tokens.map(t => h('tr', {}, h('td', {}, t.name), h('td', {}, h('code', {}, t.prefix + '…')), h('td', {}, fmtDateTime(t.created_at)),
    h('td', {}, fmtDateTime(t.last_used_at)), h('td', {}, t.revoked ? tag('Отозван', 'bad') : tag('Активен', 'good')),
    h('td', { class: 'actions' }, t.revoked ? null : h('button', { class: 'mini-btn', type: 'button', onclick: async () => {
      if (!await confirmDialog(`Отозвать токен «${t.name}»? Устройства с ним перестанут работать.`)) return;
      try { await api(sPath('/tokens/' + t.id), 'DELETE'); toast('Токен отозван'); refresh(); } catch (e) { toast(e.message, true); }
    } }, 'Отозвать'))));
  const form = submitting(h('form', { class: 'row-form' }, input('name', { required: true, maxlength: 80, placeholder: 'AI-сервер 2 этаж' }),
    h('button', { class: 'btn primary', type: 'submit' }, '+ Создать токен')), async (data, f) => {
    const r = await api(sPath('/tokens'), 'POST', data); f.reset(); refresh(); secretBox('Токен устройства', [['X-Ingest-Token / ATTENDANCE_INGEST_TOKEN', r.token]]);
  });
  return h('div', {}, heading('DEVICES', 'Токены устройств', 'Токен даёт AI-воркеру и мостам СКУД доступ только к данным этой школы. В базе хранится лишь его хеш.'),
    panel('Новый токен', form), panel(null, table(['Название', 'Префикс', 'Создан', 'Использован', 'Статус', ''], rows, 'Токенов нет')));
};

pages.users = async () => {
  const { users } = await api('/api/users');
  const rows = users.map(u => h('tr', {},
    h('td', {}, h('strong', {}, u.full_name), h('div', { class: 'device-meta' }, u.username)),
    h('td', {}, ROLE_NAMES[u.role]), h('td', {}, u.school_name || 'вся платформа'),
    h('td', {}, u.active ? tag('Активен', 'good') : tag('Отключён', 'bad')), h('td', {}, fmtDateTime(u.last_login_at)),
    h('td', { class: 'actions' }, u.id === state.me.id ? h('small', {}, 'это вы') : [
      h('button', { class: 'mini-btn neutral', type: 'button', onclick: () => userAction(u, { reset_password: true }, `Сбросить пароль ${u.username}?`) }, 'Сброс пароля'),
      h('button', { class: 'mini-btn neutral', type: 'button', onclick: () => userAction(u, { active: !u.active }, null) }, u.active ? 'Отключить' : 'Включить'),
      h('button', { class: 'mini-btn', type: 'button', onclick: async () => {
        if (!await confirmDialog(`Удалить пользователя ${u.username}?`)) return;
        try { await api('/api/users/' + u.id, 'DELETE'); toast('Удалён'); refresh(); } catch (e) { toast(e.message, true); }
      } }, 'Удалить')])));
  const roleOptions = isSuper() ? [['school_admin', 'Администратор школы'], ['teacher', 'Учитель'], ['superadmin', 'Супер-админ']] : [['teacher', 'Учитель']];
  const schoolSelect = select('school_id', state.schools.map(s => [s.id, s.name]));
  if (state.sid) schoolSelect.value = String(state.sid);
  const form = submitting(h('form', {},
    field('Логин', input('username', { required: true, pattern: '[a-zA-Z0-9_.@\\-]{3,40}', autocomplete: 'off' })),
    field('ФИО', input('full_name', { required: true, maxlength: 120 })),
    field('Роль', select('role', roleOptions)),
    isSuper() ? field('Школа', schoolSelect) : null,
    h('p', { class: 'hint' }, 'Пароль будет сгенерирован и показан один раз.'),
    h('button', { class: 'btn primary', type: 'submit' }, 'Создать пользователя')), async (data, f) => {
    if (!isSuper()) data.school_id = state.sid;
    data.school_id = data.role === 'superadmin' ? null : Number(data.school_id);
    const r = await api('/api/users', 'POST', data);
    f.reset(); refresh();
    secretBox('Пользователь создан', [['Логин', r.user.username], ['Пароль', r.password]]);
  });
  return h('div', {}, heading('ACCESS', 'Пользователи', isSuper() ? 'Все пользователи платформы.' : 'Учителя вашей школы.'),
    h('div', { class: 'twocol form-layout' }, panel(null, table(['Пользователь', 'Роль', 'Школа', 'Статус', 'Последний вход', ''], rows)), panel('Новый пользователь', form)));
};
async function userAction(u, payload, question) {
  if (question && !await confirmDialog(question)) return;
  try {
    const r = await api('/api/users/' + u.id, 'PUT', payload);
    if (r.password) secretBox('Новый пароль', [['Логин', u.username], ['Пароль', r.password]]); else toast('Сохранено');
    refresh();
  } catch (e) { toast(e.message, true); }
}

pages.audit = async () => {
  const { audit } = await api('/api/audit' + (isSuper() && state.cache.auditAll === false ? `?school_id=${state.sid}` : ''));
  const rows = audit.map(a => h('tr', {}, h('td', {}, fmtDateTime(a.at)), h('td', {}, a.username || '—'), h('td', {}, a.school_name || '—'),
    h('td', {}, h('code', {}, a.action)), h('td', { class: 'wrap' }, a.detail)));
  const toggle = isSuper() ? h('button', { class: 'btn secondary', type: 'button', onclick: () => { state.cache.auditAll = state.cache.auditAll === false; refresh(); } },
    state.cache.auditAll === false ? 'Показать все школы' : 'Только текущая школа') : null;
  return h('div', {}, heading('SECURITY', 'Журнал аудита', 'Входы, неудачные попытки, изменения данных. Последние 200 записей.', toggle),
    panel(null, table(['Время', 'Пользователь', 'Школа', 'Действие', 'Детали'], rows, 'Записей нет')));
};

function profileDialog() {
  const form = submitting(h('form', {},
    field('Текущий пароль', input('old_password', { type: 'password', required: true, autocomplete: 'current-password' })),
    field('Новый пароль (10+ символов)', input('new_password', { type: 'password', required: true, minlength: 10, autocomplete: 'new-password' })),
    h('button', { class: 'btn primary', type: 'submit' }, 'Сменить пароль')), async data => {
    await api('/api/me/password', 'POST', data); closeDialog(); toast('Пароль изменён, войдите снова'); showLogin();
  });
  openDialog(`${state.me.full_name} (${state.me.username})`, form);
}

/* ------------------------------------------------------------------ start */
document.addEventListener('DOMContentLoaded', () => {
  submitting($('login-form'), async (data, form) => {
    $('login-error').textContent = '';
    try {
      const r = await api('/api/login', 'POST', { username: data.username, password: data.password });
      state.csrf = r.csrf; form.reset(); boot();
    } catch (e) { $('login-error').textContent = e.message; }
  });
  $('logout').addEventListener('click', async () => { try { await api('/api/logout', 'POST'); } catch { /* ignore */ } showLogin(); });
  $('user-chip').addEventListener('click', profileDialog);
  $('school-switch').addEventListener('change', e => selectSchool(Number(e.target.value)));
  boot();
});
