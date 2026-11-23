'use strict';
/* Onboarding & convenience features layered on top of app.js (shares its globals: h, api, pages, state, ...). */

const inviteUrl = token => `${location.origin}/#invite=${token}`;
const fmtDate = v => v ? new Intl.DateTimeFormat('ru-RU', { timeZone: 'Asia/Tashkent', day: '2-digit', month: '2-digit', year: 'numeric' }).format(new Date(v)) : '—';

function linkBox(title, url, hint) {
  const text = `Ссылка для входа в Camera Attendance: ${url}`;
  openDialog(title, h('div', {},
    h('p', { class: 'hint' }, hint || 'Ссылка одноразовая и действует 72 часа. Отправьте её человеку лично.'),
    h('div', { class: 'secret' }, h('small', {}, 'Ссылка-приглашение'), h('code', {}, url),
      h('div', { class: 'share-row' },
        h('button', { type: 'button', class: 'mini-btn neutral', onclick: () => navigator.clipboard?.writeText(url).then(() => toast('Скопировано')) }, 'Копировать'),
        h('a', { class: 'mini-btn neutral', href: 'https://t.me/share/url?url=' + encodeURIComponent(url) + '&text=' + encodeURIComponent('Приглашение в Camera Attendance'), target: '_blank', rel: 'noopener noreferrer' }, 'Telegram'),
        h('a', { class: 'mini-btn neutral', href: 'https://wa.me/?text=' + encodeURIComponent(text), target: '_blank', rel: 'noopener noreferrer' }, 'WhatsApp')))));
}

/* ------------------------------------------------------------------ public views: invite & application */
function publicCard(...children) {
  const view = $('login-view');
  const card = h('div', { class: 'login-card public-card' },
    h('div', { class: 'brand' }, h('span', { class: 'brand-icon' }, '◎'), h('div', {}, 'Camera', h('span', { class: 'accent' }, 'AI'), h('small', {}, 'ATTENDANCE PLATFORM'))),
    ...children);
  $('login-form').hidden = true;
  view.querySelectorAll('.public-card').forEach(n => n.remove());
  view.append(card);
  view.hidden = false;
  $('app-view').hidden = true;
}
function backToLogin() {
  history.replaceState(null, '', location.pathname);
  $('login-view').querySelectorAll('.public-card').forEach(n => n.remove());
  $('login-form').hidden = false;
}

async function showInvite(token) {
  let info;
  try { info = await api('/api/invites/info', 'POST', { token }); }
  catch (e) {
    publicCard(h('h1', {}, 'Приглашение'), h('div', { class: 'form-error' }, e.message),
      h('button', { class: 'btn secondary', type: 'button', onclick: backToLogin }, 'Ко входу'));
    return;
  }
  const reset = info.kind === 'reset';
  const err = h('div', { class: 'form-error', role: 'alert' });
  const form = submitting(h('form', {},
    reset ? h('p', { class: 'hint' }, `Логин: ${info.username}`) :
      field('Придумайте логин (латиница)', input('username', { required: true, pattern: '[a-zA-Z0-9_.@\\-]{3,40}', autocomplete: 'username' })),
    field('Пароль (не короче 10 символов)', input('password', { type: 'password', required: true, minlength: 10, autocomplete: 'new-password' })),
    field('Повторите пароль', input('password2', { type: 'password', required: true, minlength: 10, autocomplete: 'new-password' })),
    h('button', { class: 'btn primary', type: 'submit' }, reset ? 'Сохранить пароль' : 'Создать учётную запись'), err), async data => {
    err.textContent = '';
    if (data.password !== data.password2) { err.textContent = 'Пароли не совпадают'; return; }
    try {
      const r = await api('/api/invites/accept', 'POST', { token, username: data.username, password: data.password });
      state.csrf = r.csrf;
      history.replaceState(null, '', location.pathname);
      $('login-view').querySelectorAll('.public-card').forEach(n => n.remove());
      $('login-form').hidden = false;
      toast('Добро пожаловать!');
      boot();
    } catch (e) { err.textContent = e.message; }
  });
  publicCard(h('h1', {}, reset ? 'Новый пароль' : 'Добро пожаловать'),
    h('p', { class: 'hint' }, reset ? `${info.full_name}, задайте новый пароль.` :
      `${info.full_name}, вас пригласили как «${ROLE_NAMES[info.role]}» в «${info.school_name}».`), form);
}

function showApplication() {
  const done = h('div');
  const form = submitting(h('form', {},
    field('Название школы', input('school_name', { required: true, maxlength: 120, placeholder: 'Школа №45' })),
    field('Город / район', input('city', { maxlength: 80 })),
    field('Контактное лицо', input('contact_name', { required: true, maxlength: 120, placeholder: 'ФИО директора или завуча' })),
    field('Телефон', input('phone', { type: 'tel', maxlength: 20, placeholder: '+998 90 123 45 67' })),
    field('E-mail', input('email', { type: 'email', maxlength: 200 })),
    field('Примерное число учеников', input('students_estimate', { type: 'number', min: 1, max: 20000 })),
    field('Комментарий', input('message', { maxlength: 1000 })),
    h('input', { name: 'website', class: 'hp', tabindex: '-1', autocomplete: 'off', 'aria-hidden': 'true' }),
    h('button', { class: 'btn primary', type: 'submit' }, 'Отправить заявку')), async (data, f) => {
    await api('/api/applications', 'POST', data);
    f.replaceChildren(h('p', {}, 'Спасибо! Заявка отправлена. Мы свяжемся с вами и пришлём ссылку для входа.'));
  });
  publicCard(h('h1', {}, 'Подключить школу'), h('p', { class: 'hint' }, 'Оставьте контакты — после проверки директор получит ссылку для входа в кабинет школы.'),
    form, done, h('button', { class: 'link-btn', type: 'button', onclick: backToLogin }, '← У меня уже есть вход'));
}

const _baseShowLogin = showLogin;
showLogin = function () {
  _baseShowLogin();
  if (location.hash.startsWith('#invite=')) { showInvite(decodeURIComponent(location.hash.slice(8))); return; }
  if (!$('apply-link')) {
    $('login-form').append(h('button', { id: 'apply-link', class: 'link-btn', type: 'button', onclick: showApplication }, 'Подключить свою школу →'));
  }
};

/* ------------------------------------------------------------------ applications (superadmin) */
NAV.splice(1, 0, ['applications', '✉', 'Заявки школ', 'super']);
pages.applications = async () => {
  const { applications } = await api('/api/applications');
  const decide = (a, decision) => async () => {
    if (decision === 'reject' && !await confirmDialog(`Отклонить заявку «${a.school_name}»?`)) return;
    try {
      const r = await api('/api/applications/' + a.id, 'POST', { decision });
      if (r.invite) {
        await loadSchools();
        linkBox(`Школа «${r.school.name}» создана`, inviteUrl(r.invite.token),
          `Отправьте ссылку: ${r.contact.contact_name} ${r.contact.phone || ''} ${r.contact.email || ''}. По ней директор сам задаст логин и пароль.`);
      } else toast('Заявка отклонена');
      if (decision === 'reject') refresh();
    } catch (e) { toast(e.message, true); }
  };
  const rows = applications.map(a => h('tr', {},
    h('td', {}, h('strong', {}, a.school_name), h('div', { class: 'device-meta' }, [a.city, a.students_estimate ? a.students_estimate + ' уч.' : ''].filter(Boolean).join(' · '))),
    h('td', {}, a.contact_name, h('div', { class: 'device-meta' }, [a.phone, a.email].filter(Boolean).join(' · '))),
    h('td', { class: 'wrap' }, a.message || '—'), h('td', {}, fmtDateTime(a.created_at)),
    h('td', {}, a.status === 'new' ? tag('Новая', 'warn') : a.status === 'approved' ? tag('Одобрена', 'good') : tag('Отклонена', 'dim')),
    h('td', { class: 'actions' }, a.status === 'new' ? [
      h('button', { class: 'mini-btn neutral', type: 'button', onclick: decide(a, 'approve') }, 'Одобрить'),
      h('button', { class: 'mini-btn', type: 'button', onclick: decide(a, 'reject') }, 'Отклонить')] : null)));
  return h('div', {}, heading('PLATFORM', 'Заявки школ', 'Заявки с формы «Подключить свою школу» на странице входа. Одобрение создаёт школу и ссылку для директора.'),
    panel(null, table(['Школа', 'Контакт', 'Комментарий', 'Дата', 'Статус', ''], rows, 'Заявок пока нет')));
};

/* ------------------------------------------------------------------ users: invite by link instead of passwords */
pages.users = async () => {
  const { users } = await api('/api/users');
  const rows = users.map(u => h('tr', {},
    h('td', {}, h('strong', {}, u.full_name), h('div', { class: 'device-meta' }, u.username)),
    h('td', {}, ROLE_NAMES[u.role]), h('td', {}, u.school_name || 'вся платформа'),
    h('td', {}, u.active ? tag('Активен', 'good') : tag('Отключён', 'bad')), h('td', {}, fmtDateTime(u.last_login_at)),
    h('td', { class: 'actions' }, u.id === state.me.id ? h('small', {}, 'это вы') : [
      h('button', { class: 'mini-btn neutral', type: 'button', onclick: async () => {
        try { const r = await api(`/api/users/${u.id}/reset-link`, 'POST', {}); linkBox(`Сброс пароля: ${u.username}`, inviteUrl(r.token)); }
        catch (e) { toast(e.message, true); }
      } }, 'Ссылка для пароля'),
      h('button', { class: 'mini-btn neutral', type: 'button', onclick: () => userAction(u, { active: !u.active }, null) }, u.active ? 'Отключить' : 'Включить'),
      h('button', { class: 'mini-btn', type: 'button', onclick: async () => {
        if (!await confirmDialog(`Удалить пользователя ${u.username}?`)) return;
        try { await api('/api/users/' + u.id, 'DELETE'); toast('Удалён'); refresh(); } catch (e) { toast(e.message, true); }
      } }, 'Удалить')])));
  const roleOptions = isSuper() ? [['school_admin', 'Администратор школы'], ['teacher', 'Учитель']] : [['teacher', 'Учитель']];
  const schoolSelect = select('school_id', state.schools.map(s => [s.id, s.name]));
  if (state.sid) schoolSelect.value = String(state.sid);
  const form = submitting(h('form', {},
    field('ФИО', input('full_name', { required: true, maxlength: 120 })),
    field('Роль', select('role', roleOptions)),
    isSuper() ? field('Школа', schoolSelect) : null,
    h('p', { class: 'hint' }, 'Человек получит одноразовую ссылку и сам придумает логин и пароль. Пароли никто не пересылает.'),
    h('button', { class: 'btn primary', type: 'submit' }, 'Создать ссылку-приглашение')), async (data, f) => {
    const r = await api('/api/invites', 'POST', { full_name: data.full_name, role: data.role, school_id: isSuper() ? Number(data.school_id) : state.sid });
    f.reset();
    linkBox(`Приглашение: ${data.full_name}`, inviteUrl(r.token));
  });
  return h('div', {}, heading('ACCESS', 'Пользователи', isSuper() ? 'Все пользователи платформы.' : 'Учителя вашей школы.'),
    h('div', { class: 'twocol form-layout' }, panel(null, table(['Пользователь', 'Роль', 'Школа', 'Статус', 'Последний вход', ''], rows)),
      panel('Пригласить', form)));
};

/* ------------------------------------------------------------------ students: search, bulk actions, import */
function readFileB64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(',')[1]);
    reader.onerror = () => reject(new Error('не удалось прочитать файл'));
    reader.readAsDataURL(file);
  });
}

function importStudentsDialog() {
  const file = h('input', { type: 'file', accept: '.xlsx,.csv,text/csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' });
  const out = h('div', { class: 'import-out' });
  const show = (...nodes) => out.replaceChildren(...nodes.filter(Boolean));
  let payload = null;
  const consent = h('input', { type: 'checkbox' });
  const apply = h('button', { class: 'btn primary', type: 'button', disabled: true, onclick: async () => {
    if (!consent.checked) { toast('Подтвердите наличие согласий родителей', true); return; }
    apply.disabled = true;
    try {
      const r = await api(sPath('/import/students'), 'POST', { ...payload, dry_run: false, consent_confirmed: true });
      closeDialog(); toast(`Готово: добавлено ${r.create}, обновлено ${r.update}`); refresh();
    } catch (e) { toast(e.message, true); apply.disabled = false; }
  } }, 'Импортировать');
  file.addEventListener('change', async () => {
    const f = file.files[0];
    if (!f) return;
    show(h('div', { class: 'loading' }, 'Проверка файла…'));
    try {
      payload = { filename: f.name, data_base64: await readFileB64(f) };
      const r = await api(sPath('/import/students'), 'POST', { ...payload, dry_run: true });
      const actionTag = a => a === 'create' ? tag('новый', 'good') : a === 'update' ? tag('изменится', 'warn') : tag('без изменений', 'dim');
      show(
        h('div', { class: 'import-summary' }, tag(`новых: ${r.create}`, 'good'), tag(`изменится: ${r.update}`, 'warn'), tag(`без изменений: ${r.same}`, 'dim'),
          r.new_classes.length ? tag(`новые классы: ${r.new_classes.join(', ')}`, 'info') : null, r.errors.length ? tag(`ошибок: ${r.errors.length}`, 'bad') : null),
        r.errors.length ? h('div', { class: 'form-error' }, r.errors.slice(0, 8).map(e => h('div', {}, `Строка ${e.row}: ${e.error}`))) : null,
        table(['Код', 'ФИО', 'Класс', ''], r.preview.slice(0, 30).map(p => h('tr', {}, h('td', {}, p.code), h('td', {}, p.full_name), h('td', {}, p.class), h('td', {}, actionTag(p.action))))),
        r.total > 30 ? h('p', { class: 'hint' }, `…и ещё ${r.total - 30}`) : null);
      apply.disabled = r.total === 0;
    } catch (e) { show(h('div', { class: 'form-error' }, e.message)); apply.disabled = true; }
  });
  openDialog('Импорт учеников из Excel / CSV', h('div', {},
    h('p', { class: 'hint' }, 'Нужны колонки «ФИО» (или «Фамилия» и «Имя») и «Класс». «Код» — по желанию: без него код создастся сам. Ученик с уже существующим кодом обновится. Классы создаются автоматически.'),
    h('a', { class: 'link-btn', href: '/api/import/template.csv', download: 'shablon_uchenikov.csv' }, '⬇ Скачать шаблон'),
    file, out, h('label', { class: 'checkbox-row' }, consent, h('span', {}, 'Письменные согласия родителей на обработку фото получены и хранятся в школе.'))), [apply]);
}

function importPhotosDialog() {
  const file = h('input', { type: 'file', accept: '.zip,application/zip' });
  const out = h('div', { class: 'import-out' });
  const show = (...nodes) => out.replaceChildren(...nodes.filter(Boolean));
  file.addEventListener('change', async () => {
    const f = file.files[0];
    if (!f) return;
    if (f.size > 200 * 1024 * 1024) { show(h('div', { class: 'form-error' }, 'Архив больше 200 МБ — разделите на части')); return; }
    show(h('div', { class: 'loading' }, `Загрузка ${(f.size / 1048576).toFixed(1)} МБ…`));
    try {
      const resp = await fetch(sPath('/import/photos'), { method: 'POST', credentials: 'same-origin', body: f,
        headers: { 'Content-Type': 'application/zip', 'X-CSRF-Token': state.csrf } });
      const r = await resp.json();
      if (!resp.ok) throw new Error(r.error || 'Ошибка загрузки');
      show(h('div', { class: 'import-summary' }, tag(`добавлено: ${r.added}`, 'good'), r.duplicates ? tag(`уже были: ${r.duplicates}`, 'dim') : null,
        r.unknown_code.length ? tag(`неизвестный код: ${r.unknown_code.length}`, 'warn') : null, r.rejected.length ? tag(`отклонено: ${r.rejected.length}`, 'bad') : null),
        r.unknown_code.length ? h('p', { class: 'hint' }, 'Нет ученика с таким кодом: ' + r.unknown_code.slice(0, 10).join(', ')) : null,
        r.rejected.length ? h('div', { class: 'form-error' }, r.rejected.slice(0, 8).map(x => h('div', {}, x))) : null,
        h('p', { class: 'hint' }, 'AI-воркер проверит лица на фото в течение 5 минут — статусы появятся в списке учеников.'));
      state.cache.photosImported = true;
    } catch (e) { show(h('div', { class: 'form-error' }, e.message)); }
  });
  openDialog('Фото учеников архивом', h('div', {},
    h('p', { class: 'hint' }, 'ZIP-архив, где каждое фото названо кодом ученика: STU-0001.jpg, STU-0001_2.jpg — или папки по коду: STU-0001/фото.jpg. До 5 фото на ученика, каждое до 3 МБ.'),
    file, out), [h('button', { class: 'btn secondary', type: 'button', onclick: () => { closeDialog(); refresh(); } }, 'Обновить список')]);
}

function editStudentDialog(s, classes) {
  const form = submitting(h('form', {},
    field('ФИО', input('full_name', { required: true, maxlength: 120, value: s.full_name })),
    field('Класс', select('class_id', classes.map(c => [c.id, c.name]))),
    h('button', { class: 'btn primary', type: 'submit' }, 'Сохранить')), async data => {
    await api(sPath('/students/' + s.id), 'PUT', { full_name: data.full_name, class_id: Number(data.class_id) });
    closeDialog(); toast('Сохранено'); refresh();
  });
  form.querySelector('select').value = String(s.class_id);
  openDialog(`${s.full_name} · ${s.code}`, form);
}

pages.students = async () => {
  const classes = await classesList();
  const classId = state.cache.classId === 'all' ? 'all' : (classes.some(c => c.id === state.cache.classId) ? state.cache.classId : (classes[0] || {}).id);
  state.cache.classId = classId;
  const { students } = classId ? await api(sPath('/students' + (classId === 'all' ? '' : '?class_id=' + classId))) : { students: [] };
  const selected = new Set();
  const query = state.cache.studentQuery || '';
  const classItems = [h('div', { class: 'person-item' + (classId === 'all' ? ' selected' : '') },
    h('button', { class: 'plain', type: 'button', onclick: () => { state.cache.classId = 'all'; refresh(); } }, h('strong', {}, 'Все ученики'))),
  ...classes.map(c => h('div', { class: 'person-item' + (c.id === classId ? ' selected' : '') },
    h('button', { class: 'plain', type: 'button', onclick: () => { state.cache.classId = c.id; refresh(); } }, h('strong', {}, c.name), h('small', {}, ` ${c.students} уч.`)),
    isAdmin() ? h('button', { class: 'mini-btn', type: 'button', onclick: async () => {
      if (!await confirmDialog(`Удалить класс ${c.name}?`)) return;
      try { await api(sPath('/classes/' + c.id), 'DELETE'); toast('Класс удалён'); refresh(); } catch (e) { toast(e.message, true); }
    } }, 'Удалить') : null))];
  const classForm = isAdmin() ? submitting(h('form', { class: 'row-form' }, input('name', { required: true, maxlength: 40, placeholder: '5-А' }),
    h('button', { class: 'btn primary', type: 'submit' }, '+ Класс')), async (data, f) => { await api(sPath('/classes'), 'POST', data); f.reset(); toast('Класс создан'); refresh(); }) : null;

  const bulkBar = h('div', { class: 'bulk-bar', hidden: true });
  const updateBulk = () => {
    bulkBar.hidden = selected.size === 0;
    if (!selected.size) return;
    const target = select('class_id', classes.map(c => [c.id, c.name]));
    bulkBar.replaceChildren(h('strong', {}, `Выбрано: ${selected.size}`), target,
      h('button', { class: 'btn secondary small', type: 'button', onclick: async () => {
        try { const r = await api(sPath('/students/bulk'), 'POST', { action: 'move', student_ids: [...selected], class_id: Number(target.value) }); toast(`Переведено: ${r.moved}`); refresh(); }
        catch (e) { toast(e.message, true); }
      } }, 'Перевести в класс'),
      h('button', { class: 'mini-btn', type: 'button', onclick: async () => {
        if (!await confirmDialog(`Удалить ${selected.size} учеников вместе с их фото и событиями?`)) return;
        try { const r = await api(sPath('/students/bulk'), 'POST', { action: 'delete', student_ids: [...selected] }); toast(`Удалено: ${r.deleted}`); refresh(); }
        catch (e) { toast(e.message, true); }
      } }, 'Удалить'));
  };
  const rowFor = s => {
    const check = isAdmin() ? h('input', { type: 'checkbox', class: 'row-check', 'aria-label': 'выбрать' }) : null;
    if (check) check.addEventListener('change', () => { check.checked ? selected.add(s.id) : selected.delete(s.id); updateBulk(); });
    return h('tr', { 'data-search': `${s.full_name} ${s.code} ${s.class_name}`.toLowerCase() },
      isAdmin() ? h('td', { class: 'check-cell' }, check) : null,
      h('td', {}, h('strong', {}, s.full_name), h('div', { class: 'device-meta' }, `${s.code}${classId === 'all' ? ' · ' + s.class_name : ''}`)),
      h('td', {}, h('div', { class: 'thumbs' }, s.photos.map(p => h('div', { class: 'thumb' },
        h('img', { src: photoUrl(p.id), alt: 'фото', loading: 'lazy' }), tag(...PHOTO_STATUS[p.face_status] || [p.face_status, 'dim']),
        isAdmin() ? h('button', { class: 'x-btn', type: 'button', title: 'Удалить фото', onclick: async () => {
          if (!await confirmDialog('Удалить это фото?')) return;
          try { await api(sPath('/photos/' + p.id), 'DELETE'); toast('Фото удалено'); refresh(); } catch (e) { toast(e.message, true); }
        } }, '×') : null)),
      isAdmin() && s.photos.length < 5 ? uploadButton(s) : null)),
      h('td', {}, s.ai_ready ? tag('Готов', 'good') : s.photos.some(p => p.face_status === 'unchecked') ? tag('Ждёт проверки AI', 'dim') : tag('Нужны фото', 'bad')),
      isAdmin() ? h('td', { class: 'actions' },
        h('button', { class: 'mini-btn neutral', type: 'button', onclick: () => editStudentDialog(s, classes) }, 'Изменить'),
        h('button', { class: 'mini-btn', type: 'button', onclick: async () => {
          if (!await confirmDialog(`Удалить ${s.full_name}? Будут удалены все фото и события ученика.`)) return;
          try { await api(sPath('/students/' + s.id), 'DELETE'); toast('Ученик и его данные удалены'); refresh(); } catch (e) { toast(e.message, true); }
        } }, 'Удалить')) : null);
  };
  const tbody = students.map(rowFor);
  const search = h('input', { type: 'search', class: 'search', placeholder: 'Поиск по имени или коду', value: query, 'aria-label': 'Поиск' });
  const applyFilter = () => {
    const q = search.value.trim().toLowerCase();
    state.cache.studentQuery = search.value;
    tbody.forEach(tr => { tr.hidden = q && !tr.dataset.search.includes(q); });
  };
  search.addEventListener('input', applyFilter);
  const selectAll = isAdmin() ? h('input', { type: 'checkbox', 'aria-label': 'выбрать всех' }) : null;
  if (selectAll) selectAll.addEventListener('change', () => {
    tbody.forEach(tr => { if (!tr.hidden) { const c = tr.querySelector('.row-check'); c.checked = selectAll.checked; c.dispatchEvent(new Event('change')); } });
  });
  const realClassId = classId === 'all' ? (classes[0] || {}).id : classId;
  const studentForm = isAdmin() && classes.length ? submitting(h('form', {},
    field('ФИО', input('full_name', { required: true, maxlength: 120 })),
    field('Класс', select('class_id', classes.map(c => [c.id, c.name]))),
    field('Код (необязательно — создастся сам)', input('code', { pattern: '[a-zA-Z0-9_.\\-]{1,50}', placeholder: 'STU-0001' })),
    h('label', { class: 'checkbox-row' }, h('input', { type: 'checkbox', name: 'consent_confirmed', required: true }),
      h('span', {}, 'Письменное согласие родителей на обработку фото получено.')),
    h('button', { class: 'btn primary', type: 'submit' }, 'Добавить ученика')), async (data, f) => {
    data.class_id = Number(data.class_id);
    if (!data.code) delete data.code;
    await api(sPath('/students'), 'POST', data); f.reset(); toast('Ученик добавлен'); refresh();
  }) : null;
  if (studentForm && realClassId) studentForm.querySelector('select').value = String(realClassId);
  const tools = isAdmin() ? h('div', { class: 'toolbar wrap' },
    h('button', { class: 'btn primary', type: 'button', onclick: importStudentsDialog }, '⬆ Импорт из Excel'),
    h('button', { class: 'btn secondary', type: 'button', onclick: importPhotosDialog }, '🗂 Фото архивом')) : null;
  const tbl = table([isAdmin() ? selectAll : null, 'Ученик', 'Фото (до 5)', 'AI', isAdmin() ? '' : null].filter(x => x !== null), tbody,
    classes.length ? 'Нет учеников' : 'Создайте класс или импортируйте учеников из Excel');
  setTimeout(applyFilter);
  return h('div', {}, heading('DIRECTORY', 'Классы и ученики', 'Импортируйте список из Excel за минуту. Для распознавания нужно 1–5 чётких фото лица каждого ребёнка.', tools),
    h('div', { class: 'twocol students-layout' },
      h('div', {}, panel('Классы', classItems, classForm), studentForm ? panel('Новый ученик', studentForm) : null),
      panel(h('div', { class: 'panel-title-row' }, h('h2', {}, classId === 'all' ? `Все ученики (${students.length})` :
        `Ученики класса ${(classes.find(c => c.id === classId) || {}).name || ''} (${students.length})`), search),
      bulkBar, tbl,
      h('div', { class: 'note' }, 'Статус «Лицо OK» ставит AI-воркер после проверки фото. Фото хранятся на сервере и доступны только сотрудникам школы.'))));
};

/* ------------------------------------------------------------------ schedule: copy a day to the whole week */
const _baseSchedule = pages.schedule;
pages.schedule = async () => {
  const node = await _baseSchedule();
  if (!isAdmin() || !state.cache.classId) return node;
  const src = select('from_weekday', [1, 2, 3, 4, 5, 6, 7].map(d => [d, WEEKDAYS_FULL[d]]));
  const boxes = [1, 2, 3, 4, 5, 6, 7].map(d => h('label', { class: 'day-check' }, h('input', { type: 'checkbox', value: d, checked: d >= 2 && d <= 6 }), WEEKDAYS[d]));
  const copy = h('div', { class: 'copy-day' }, h('span', {}, 'Скопировать'), src, h('span', {}, 'на'), boxes,
    h('button', { class: 'btn secondary small', type: 'button', onclick: async () => {
      const to = boxes.map(b => b.querySelector('input')).filter(i => i.checked).map(i => Number(i.value));
      if (!to.length) { toast('Отметьте дни', true); return; }
      if (!await confirmDialog(`Расписание выбранных дней (${to.map(d => WEEKDAYS[d]).join(', ')}) будет заменено копией дня «${WEEKDAYS_FULL[src.value]}». Продолжить?`)) return;
      try { const r = await api(sPath('/lessons/copy-day'), 'POST', { class_id: state.cache.classId, from_weekday: Number(src.value), to_weekdays: to }); toast(`Скопировано уроков: ${r.lessons} × ${r.copied_to.length} дн.`); refresh(); }
      catch (e) { toast(e.message, true); }
    } }, 'Копировать'));
  node.insertBefore(panel('Быстрое заполнение недели', copy, h('p', { class: 'hint' }, 'Заполните один день и размножьте его на остальные — затем поправьте отличающиеся предметы.')), node.children[1] || null);
  return node;
};

/* ------------------------------------------------------------------ attendance: export + phone mode */
const _baseAttendance = pages.attendance;
pages.attendance = async () => {
  if (window.matchMedia('(max-width: 760px)').matches) return mobileAttendance();
  const node = await _baseAttendance();
  const date = state.cache.date || state.today;
  const from = h('input', { type: 'date', class: 'datepick', value: date, max: state.today });
  const to = h('input', { type: 'date', class: 'datepick', value: date, max: state.today });
  const onlyClass = h('label', { class: 'checkbox-row' }, h('input', { type: 'checkbox', checked: true }), h('span', {}, 'только этот класс'));
  const exportBtn = h('button', { class: 'btn secondary', type: 'button', onclick: () => {
    const q = new URLSearchParams({ from: from.value, to: to.value });
    if (onlyClass.querySelector('input').checked && state.cache.classId) q.set('class_id', state.cache.classId);
    window.location.href = sPath('/export/attendance.csv?' + q);
  } }, '⬇ Скачать для Excel');
  node.append(panel('Выгрузка в Excel', h('div', { class: 'export-row' }, h('label', {}, 'С', from), h('label', {}, 'По', to), onlyClass, exportBtn),
    h('p', { class: 'hint' }, 'CSV открывается в Excel. Буквы: П — присутствовал, О — опоздал, Н — отсутствовал, У — уважительная причина. Период до 62 дней.')));
  return node;
};

async function mobileAttendance() {
  const classes = await classesList();
  if (!classes.length) return h('div', { class: 'mobile-att' }, heading('ATTENDANCE', 'Журнал'), panel(null, empty('Нет классов')));
  const classId = classes.some(c => c.id === state.cache.classId) ? state.cache.classId : classes[0].id;
  state.cache.classId = classId;
  const date = state.cache.date || state.today;
  const a = await api(sPath(`/attendance?class_id=${classId}&date=${date}`));
  if (!a.lessons.length) return h('div', { class: 'mobile-att' }, h('div', { class: 'toolbar wrap' },
    classPicker(classes, classId, id => { state.cache.classId = id; state.cache.lessonId = null; refresh(); }),
    datePicker(date, d => { state.cache.date = d; refresh(); })), panel(null, empty('В этот день у класса нет уроков')));
  const current = a.lessons.find(l => l.state === 'in_progress') || [...a.lessons].reverse().find(l => l.state === 'finished') || a.lessons[0];
  const lid = a.lessons.some(l => l.id === state.cache.lessonId) ? state.cache.lessonId : current.id;
  state.cache.lessonId = lid;
  const lesson = a.lessons.find(l => l.id === lid);
  const chips = h('div', { class: 'chips' }, a.lessons.map((l, i) => h('button', { type: 'button', class: 'chip' + (l.id === lid ? ' active' : ''),
    onclick: () => { state.cache.lessonId = l.id; refresh(); } }, `${i + 1}. ${l.start_time}`)));
  const quick = [['present', 'П'], ['late', 'О'], ['absent', 'Н'], ['excused', 'У']];
  const list = a.rows.map(r => {
    const c = r.cells[String(lid)];
    return h('div', { class: 'm-row' },
      h('div', { class: 'm-name' }, h('strong', {}, r.student.full_name), h('div', {}, statusTag(c.status), c.first_seen ? h('small', {}, ' ' + fmtTime(c.first_seen)) : null)),
      h('div', { class: 'm-actions' }, quick.map(([s, letter]) => h('button', { type: 'button', class: 'q-btn' + (c.source === 'manual' && c.status === s ? ' on' : ''),
        title: STATUS[s][0], disabled: c.status === 'upcoming' || date > state.today, onclick: async () => {
          try { await api(sPath('/marks'), 'PUT', { student_id: r.student.id, lesson_id: lid, day: date, status: s }); refresh(); }
          catch (e) { toast(e.message, true); }
        } }, letter))));
  });
  const counts = lesson.counts || {};
  return h('div', { class: 'mobile-att' },
    h('div', { class: 'toolbar wrap' }, classPicker(classes, classId, id => { state.cache.classId = id; state.cache.lessonId = null; refresh(); }),
      datePicker(date, d => { state.cache.date = d; state.cache.lessonId = null; refresh(); })),
    chips,
    panel(h('div', {}, h('h2', {}, `${lesson.subject} · ${lesson.start_time}–${lesson.end_time}`),
      h('small', { class: 'hint' }, `присутствуют ${(counts.present || 0) + (counts.late || 0)} из ${a.rows.length}`)), list),
    h('p', { class: 'hint' }, 'П — пришёл, О — опоздал, Н — нет, У — уважительная. Отметки камеры ставятся сами; кнопкой можно исправить.'));
}

/* ------------------------------------------------------------------ overview: setup checklist for new schools */
const _baseOverview = pages.overview;
pages.overview = async () => {
  const node = await _baseOverview();
  if (!isAdmin() || !state.sid) return node;
  try {
    const [cls, st, cams, lessons, users, tokens] = await Promise.all([api(sPath('/classes')), api(sPath('/students')), api(sPath('/cameras')),
      api(sPath('/lessons')), api('/api/users'), api(sPath('/tokens'))]);
    const steps = [
      ['Классы и ученики (импорт из Excel)', st.students.length > 0, 'students'],
      ['Фото учеников', st.students.length > 0 && st.students.filter(s => s.photos.length).length >= st.students.length * 0.8, 'students'],
      ['Камеры кабинетов', cams.cameras.some(c => c.kind === 'classroom'), 'cameras'],
      ['Расписание уроков с камерами', lessons.lessons.some(l => l.camera_id), 'schedule'],
      ['Учителя приглашены', users.users.some(u => u.school_id === state.sid && u.role === 'teacher'), 'users'],
      ['AI-воркер подключён', tokens.tokens.some(t => t.last_used_at) && cams.cameras.some(c => c.last_seen_at), 'cameras'],
    ];
    const done = steps.filter(s => s[1]).length;
    if (done === steps.length) return node;
    const box = panel(`Подключение школы: ${done} из ${steps.length}`, h('div', { class: 'checklist' }, steps.map(([label, ok, page]) =>
      h('button', { type: 'button', class: 'check-item' + (ok ? ' ok' : ''), onclick: () => navigate(page) }, h('span', {}, ok ? '✓' : '○'), label))),
      h('div', { class: 'progress' }, h('div', { class: 'progress-bar', 'data-pct': Math.round(done / steps.length * 100) })));
    const bar = box.querySelector('.progress-bar');
    bar.style.width = bar.dataset.pct + '%';
    node.insertBefore(box, node.children[1] || null);
  } catch { /* checklist is a convenience only */ }
  return node;
};
