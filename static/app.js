'use strict';

const $ = (id) => document.getElementById(id);
const el = (tag, cls, html) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (html !== undefined) n.innerHTML = html;
  return n;
};
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

// period: 'uncensored' — при открытии показываем подборку без цензуры, а не рейтинг HF.
// Вместе с 'top' это две ручные подборки: обе идут в /api/top, различаются параметром kind.
// Сколько серверов тянем с Clore за раз и сколько рисуем: выборка нужна широкая,
// иначе поиск по имени не найдёт 5090 среди трёх десятков самых дешёвых предложений.
const OFFERS_POOL = 400;
const OFFERS_SHOWN = 30;
const TOP_KINDS = { uncensored: 'uncensored', top: 'top' };
const state = { model: null, offer: null, env: null, polling: null, period: 'uncensored', gguf_file: '' };

const PERIOD_NOTE = {
  uncensored: 'Модели, где цензуру сносили агрессивнее всего — дообучением либо хирургией по весам, ' +
       'с публичными замерами доли отказов. Абляция, на которой построен «Топ для кодинга», снимает ' +
       'большинство отказов, но не все. Это сборки одиночек: бенчмарки в карточках — замеры самих авторов.',
  top: 'Ручная подборка моделей, которые реально пишут код (не рейтинг HF): ' +
       'счётчики ниже — живые, порядок — наша рекомендация. Все группы, кроме первой, — ' +
       'расцензуренные сборки; «Лучшие модели» в самом верху — оригиналы от лабораторий, ' +
       'с цензурой на месте. ' +
       '<b>safetensors</b> поднимаются на vLLM и быстрее, <b>GGUF</b> дают выбор кванта под VRAM сервера.',
  all:'Сортировка по скачиваниям за всё время (<code>downloadsAllTime</code>) — точный счётчик Hugging Face.',
  month: 'Сортировка по скачиваниям за последние 30 дней — точный счётчик Hugging Face.',
  week: 'Недельных счётчиков Hugging Face не публикует: это рейтинг тренда среди моделей, обновлённых за 7 дней.',
  day: 'Дневных счётчиков Hugging Face не публикует: это рейтинг тренда среди моделей, обновлённых за 2 дня.',
  new: 'Сортировка по дате последнего обновления репозитория.',
};

// ───────── сеть ─────────
async function api(path, body) {
  const opts = body ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : {};
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({ error: 'плохой ответ сервера' }));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

let toastTimer;
function toast(msg, bad) {
  const t = $('toast');
  t.textContent = msg;
  t.className = 'show' + (bad ? ' bad' : '');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.className = ''), 4200);
}

const busy = (btn, on, label) => {
  btn.disabled = on;
  if (on) { btn.dataset.text = btn.textContent; btn.textContent = label || '…'; }
  else if (btn.dataset.text) btn.textContent = btn.dataset.text;
};

// ───────── форматирование ─────────
const fmtUsd = (v) => v == null ? '—' : `$${Number(v).toFixed(Number(v) < 1 ? 3 : 2)}/ч`;
const COIN = { bitcoin: 'BTC', 'CLORE-Blockchain': 'CLORE', 'USD-Blockchain': 'USDC' };
const fmtRate = (v) => {
  const n = Number(v);
  return n >= 1 ? '$' + n.toLocaleString('ru-RU', { maximumFractionDigits: 2 })
    : '$' + n.toFixed(Math.max(2, 2 - Math.floor(Math.log10(n))));
};
const fmtCoin = (v, cur) => v == null ? '' :
  `${Number(v).toFixed(cur === 'bitcoin' ? 8 : 2)} ${COIN[cur] || cur}`;
const fmtNum = (n) => n == null ? '—' : Number(n).toLocaleString('ru-RU');
const fmtK = (n) => !n ? '0' : n >= 1e6 ? (n / 1e6).toFixed(1) + 'M' : n >= 1e3 ? (n / 1e3).toFixed(1) + 'k' : String(n);
const fmtCtx = (c) => !c ? '—' : c >= 1024 ? Math.round(c / 1024) + 'k' : String(c);

// ───────── вкладки ─────────
document.querySelectorAll('.tab').forEach((tab) => {
  tab.onclick = () => {
    document.querySelectorAll('.tab').forEach((t) => t.classList.toggle('active', t === tab));
    document.querySelectorAll('.page').forEach((p) => p.classList.toggle('active', p.id === 'page-' + tab.dataset.tab));
    if (tab.dataset.tab === 'orders') { loadOrders(); loadWallets(); }
  };
});

// ───────── состояние ─────────
async function loadState() {
  const s = await api('/api/state');
  state.env = s;

  if (!$('model-ctx').placeholder.includes('по умолчанию')) $('model-ctx').placeholder = `контекст (${s.defaults.ctx})`;

  const cur = s.current;
  const pill = $('status-pill');
  if (!s.settings.CLORE_API_KEY.set) { pill.className = 'pill warn'; pill.textContent = 'нет ключа Clore'; }
  else if (cur.alive) { pill.className = 'pill ok'; pill.textContent = `модель онлайн · заказ ${cur.order}`; }
  else if (cur.order) { pill.className = 'pill warn'; pill.textContent = `заказ ${cur.order} · API молчит`; }
  else { pill.className = 'pill'; pill.textContent = 'сервер не арендован'; }

  for (const [k, v] of Object.entries(s.settings)) {
    const input = $('s-' + k);
    if (!input) continue;
    if (v.set && input.type === 'password') input.placeholder = `сохранён (${v.hint}) — пусто = не менять`;
    else if (!v.set && input.type === 'password') input.placeholder = 'не задан';
    // Обычное поле показывает ровно то, что лежит в .env, включая пустоту. Раньше
    // пустое значение поле не трогало, и после очистки оно могло показывать старое —
    // то есть расходиться с тем, что реально применяется при деплое.
    if (input.type !== 'password') input.value = v.value || '';
  }

  renderCurrent();
  if (!state.model && cur.model_repo) $('model-ref').value = cur.model_repo;
  renderPlan();
}

function renderCurrent() {
  const c = state.env?.current || {};
  const box = $('current');
  if (!c.order) { box.innerHTML = '<p class="muted">Активного заказа нет.</p>'; return; }
  box.innerHTML = `<div class="grid">
    <div class="kv"><b>${esc(c.order)}</b><span>заказ Clore</span></div>
    <div class="kv"><b>${esc(c.server || '—')}</b><span>сервер</span></div>
    <div class="kv"><b>${c.alive ? 'онлайн' : 'поднимается'}</b><span>состояние API</span></div>
    <div class="kv"><b>${esc(c.engine || '—')}</b><span>движок</span></div>
    <div class="kv"><b>${fmtCtx(Number(c.ctx))}</b><span>контекст</span></div>
  </div>
  <div class="muted" style="margin-top:10px">
    Модель: <code>${esc(c.model_repo || '—')}</code> · id в API: <code>${esc(c.model_name)}</code><br>
    Base URL: <code class="copy" data-copy="${esc(c.api_url)}">${esc(c.api_url || '—')}</code><br>
    SSH: <code class="copy" data-copy="${esc(c.ssh)}">${esc(c.ssh || '—')}</code>
  </div>`;
  box.querySelectorAll('.copy').forEach((n) => (n.onclick = () => {
    navigator.clipboard.writeText(n.dataset.copy || ''); toast('скопировано');
  }));
}

// ───────── модели ─────────
function metricLine(m, period) {
  const base = `♥ ${fmtK(m.likes)} · обновлён ${esc(m.updated)}` +
    `${m.params_b ? ' · ~' + m.params_b + 'B' : ''}${m.bpw ? ' · ' + m.bpw + ' bpw' : ''}`;
  if (period === 'top') return `⬇ ${fmtK(m.downloads)} за 30 дней · ⬇ ${fmtK(m.downloads_all)} за всё время · ${base}`;
  if (period === 'all') return `⬇ ${fmtK(m.downloads_all)} за всё время · ⬇ ${fmtK(m.downloads)} за 30 дней · ${base}`;
  if (period === 'week' || period === 'day') return `🔥 тренд ${fmtK(m.trending)} · ⬇ ${fmtK(m.downloads)} за 30 дней · ${base}`;
  return `⬇ ${fmtK(m.downloads)} за 30 дней · ${base}`;
}

const ENGINE_OF = { gguf: 'llama.cpp', exl3: 'TabbyAPI', exl2: 'TabbyAPI' };

// обычная строка результатов поиска
function modelItem(m) {
  const item = el('div', 'item');
  item.innerHTML = `<div class="main">
      <div class="name">${esc(m.repo_id)}</div>
      <div class="meta">${metricLine(m, state.period)}</div>
    </div>
    <div class="side">
      <span class="badge ${esc(m.format)}">${esc(m.format)}</span>
      ${m.gated ? '<span class="badge gated">gated</span>' : ''}
      <a class="muted" href="${esc(m.url)}" target="_blank" rel="noreferrer">HF ↗</a>
      <button class="primary">Выбрать</button>
    </div>`;
  item.querySelector('button').onclick = () => { $('model-ref').value = m.repo_id; pickModel(); };
  return item;
}

// блок одной модели внутри карточки семейства: описание + строка на каждый формат весов
function topModel(m) {
  const box = el('div', 'tmodel');
  box.innerHTML = `<div class="name">${esc(m.title)}
      <span class="badge tier">${esc(m.tier)}</span>
      ${m.gated ? '<span class="badge gated">gated</span>' : ''}</div>
    <div class="meta">${m.base ? esc(m.base) + ' · ' : ''}${metricLine(m, 'top')}</div>
    <div class="why">${esc(m.why)}</div>
    <div class="variants"></div>`;
  const vbox = box.querySelector('.variants');
  m.variants.forEach((v) => {
    const wrap = el('div', 'vwrap');
    const row = el('div', 'variant');
    row.innerHTML = `<span class="badge ${esc(v.format)}">${esc(v.format)}</span>
      <span class="muted">${esc(ENGINE_OF[v.format] || 'vLLM')}</span>
      <code class="repo">${esc(v.repo_id)}</code>
      <a class="muted" href="${esc(v.url)}" target="_blank" rel="noreferrer">HF ↗</a>
      <button class="primary">Выбрать</button>`;
    const slot = el('div', 'picked hidden');
    row.querySelector('button').onclick = async () => {
      $('model-ref').value = v.repo_id;
      await pickModel();
      // раскрываем выбор кванта на месте — иначе за ним пришлось бы листать вверх
      document.querySelectorAll('.picked').forEach((n) => { n.innerHTML = ''; n.classList.add('hidden'); });
      if (!state.model || state.model.repo_id !== v.repo_id) return;
      slot.classList.remove('hidden');
      slot.appendChild(el('div', 'qsummary', esc(quantSummary(state.model))));
      const sel = quantSelect(state.model);
      if (sel) slot.appendChild(sel);
    };
    wrap.appendChild(row);
    wrap.appendChild(slot);
    vbox.appendChild(wrap);
  });
  return box;
}

// карточка подборки = семейство. Первая модель раскрыта, остальные свёрнуты.
function topItem(f) {
  const item = el('div', 'item top');
  item.innerHTML = `<div class="main">
      <div class="name"><span class="rank">${f.rank}</span>${esc(f.family)}
        <span class="badge tier">${esc(f.tier)}</span>
        <span class="muted count">${f.models.length} ${plural(f.models.length, 'модель', 'модели', 'моделей')}</span></div>
      <div class="why fam">${esc(f.why)}</div>
      <div class="tmodels"></div>
    </div>`;
  const box = item.querySelector('.tmodels');
  f.models.forEach((m, i) => {
    if (i === 0) { box.appendChild(topModel(m)); return; }
    const d = el('details', 'tfold');
    d.innerHTML = `<summary><b>${esc(m.title)}</b>
      <span class="badge tier">${esc(m.tier)}</span>
      <span class="muted">${esc(m.base)}</span></summary>`;
    d.appendChild(topModel(m));
    box.appendChild(d);
  });
  return item;
}

function plural(n, one, few, many) {
  const m10 = n % 10, m100 = n % 100;
  if (m10 === 1 && m100 !== 11) return one;
  if (m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14)) return few;
  return many;
}

async function searchModels() {
  const box = $('models');
  const top = Boolean(TOP_KINDS[state.period]);
  box.innerHTML = '<p class="muted">ищу на Hugging Face…</p>';
  busy($('btn-search'), true, 'ищу…');
  // в режиме топа свой поисковый запрос не участвует — подборка фиксированная
  $('q').disabled = top;
  try {
    const q = top
      ? new URLSearchParams({ fmt: $('fmt').value, kind: TOP_KINDS[state.period] })
      : new URLSearchParams({ q: $('q').value.trim(), period: state.period, fmt: $('fmt').value, limit: '40' });
    const data = await api((top ? '/api/top?' : '/api/models?') + q);
    const models = top ? data.families : data.models;
    $('period-note').innerHTML = (PERIOD_NOTE[state.period] || '') +
      (data.relaxed ? ' <b>За такой срок обновлений не нашлось — показан тренд без ограничения по свежести.</b>' : '');
    box.innerHTML = '';
    if (!models.length) {
      box.innerHTML = top
        ? '<p class="muted">в подборке нет моделей в этом формате — снимите фильтр по формату</p>'
        : '<p class="muted">ничего не нашлось — попробуйте другой запрос</p>';
      return;
    }
    models.forEach((m) => {
      box.appendChild(top ? topItem(m) : modelItem(m));
    });
  } catch (e) { box.innerHTML = `<p class="muted">ошибка: ${esc(e.message)}</p>`; }
  finally { busy($('btn-search'), false); }
}

async function pickModel() {
  const ref = $('model-ref').value.trim();
  if (!ref) return toast('вставьте ссылку или выберите модель из списка', true);
  const card = $('model-card');
  card.classList.remove('hidden');
  card.innerHTML = '<p class="muted">читаю репозиторий…</p>';
  busy($('btn-pick'), true, '…');
  try {
    const q = new URLSearchParams({ ref });
    if ($('model-ctx').value) q.set('ctx', $('model-ctx').value);
    const { model } = await api('/api/model?' + q);
    state.model = model;
    state.gguf_file = model.gguf_file || '';
    state.offer = null;
    renderModelCard(model);
    applyNeed();
    $('f-disk').value = model.disk_gb;
    $('model-ctx').value = model.ctx;
    renderPlan();
    $('offers').innerHTML = '';
    toast(`${model.repo_id}: ${model.format} → ${model.engine}`);
  } catch (e) {
    card.innerHTML = `<p class="muted">не получилось: ${esc(e.message)}</p>`;
    state.model = null;
  } finally { busy($('btn-pick'), false); }
}

// Выбор кванта нужен в двух местах: в карточке модели наверху и прямо в списке подборки,
// чтобы после «Выбрать» не приходилось листать страницу вверх. Узел строится один раз
// и вставляется куда попросят; источник правды — state.gguf_file.
function quantSelect(m) {
  const opts = m.gguf_options || [];
  if (!opts.length) return null;
  // кванты ниже Q4_K_M отфильтрованы на сервере; выбран лучший из оставшихся
  const note = m.gguf_low_only
    ? '<span class="qnote warn-text">в репозитории нет квантов Q4_K_M и выше — для кода этого мало</span>'
    : m.gguf_dropped
      ? `<span class="qnote">${m.gguf_dropped} ${plural(m.gguf_dropped, 'квант', 'кванта', 'квантов')} ниже Q4_K_M скрыт${m.gguf_dropped === 1 ? '' : 'ы'}: для кода не годятся</span>`
      : '<span class="qnote">все кванты репозитория пригодны для кода</span>';
  const node = el('label', 'muted quant', `Квант
    <select>${opts.map((o) => `
      <option value="${esc(o.path)}"${o.path === state.gguf_file ? ' selected' : ''}>
        ${esc(o.label)} — ${o.weights_gib} ГиБ, нужно ≈${o.vram_gib} ГиБ VRAM${o.parts > 1 ? ` (${o.parts} частей)` : ''}
      </option>`).join('')}</select>${note}`);
  node.querySelector('select').onchange = (e) => applyQuant(opts.find((x) => x.path === e.target.value));
  return node;
}

// смена кванта меняет и размер весов, и требования к серверу — пересчитываем везде сразу
function applyQuant(o) {
  if (!o || !state.model) return;
  state.gguf_file = o.path;
  state.model = { ...state.model, weights_gib: o.weights_gib, vram_gib: o.vram_gib, disk_gb: o.disk_gb,
    vram_cpu_moe_gib: o.vram_cpu_moe_gib ?? state.model.vram_cpu_moe_gib,
    ram_gb: o.ram_gb ?? state.model.ram_gb };
  applyNeed();
  $('f-disk').value = o.disk_gb;
  if ($('kv-weights')) $('kv-weights').textContent = `${o.weights_gib} GiB`;
  if ($('kv-vram')) $('kv-vram').textContent = `≈ ${o.vram_gib} GiB`;
  if ($('kv-vram-moe') && o.vram_cpu_moe_gib) $('kv-vram-moe').textContent = `≈ ${o.vram_cpu_moe_gib} GiB`;
  if ($('kv-ram') && o.ram_gb) $('kv-ram').textContent = `${o.ram_gb} GB`;
  if ($('kv-disk')) $('kv-disk').textContent = `${o.disk_gb} GB`;
  // селекты в карточке и в списке должны показывать одно и то же
  document.querySelectorAll('.quant select').forEach((s) => { s.value = o.path; });
  document.querySelectorAll('.qsummary').forEach((n) => (n.textContent = quantSummary(state.model)));
  renderPlan();
}

const quantSummary = (m) =>
  `${m.weights_gib} ГиБ весов · нужно ≈${m.vram_gib} ГиБ VRAM и ${m.disk_gb} GB диска`;

function renderModelCard(m) {
  const engines = { tabby: 'TabbyAPI (ExLlamaV3)', llamacpp: 'llama.cpp server', vllm: 'vLLM' };
  $('model-card').innerHTML = `
    <div class="row" style="justify-content:space-between">
      <div class="name"><a href="${esc(m.url)}" target="_blank" rel="noreferrer">${esc(m.repo_id)}</a></div>
      <div class="side">
        <span class="badge ${esc(m.format)}">${esc(m.format)}</span>
        ${m.uncensored ? '<span class="badge">uncensored</span>' : '<span class="badge warn">не похоже на uncensored</span>'}
        ${m.gated ? '<span class="badge gated">нужен HF-токен</span>' : ''}
      </div>
    </div>
    <div class="grid">
      <div class="kv"><b>${esc(engines[m.engine] || m.engine)}</b><span>движок</span></div>
      <div class="kv"><b id="kv-weights">${m.weights_gib} GiB</b><span>веса (скачать)</span></div>
      <div class="kv"><b id="kv-vram">≈ ${m.vram_gib} GiB</b><span>нужно VRAM</span></div>
      ${m.cpu_moe ? `<div class="kv"><b id="kv-vram-moe">≈ ${m.vram_cpu_moe_gib} GiB</b>
        <span>VRAM с экспертами в RAM</span></div>
      <div class="kv"><b id="kv-ram">${m.ram_gb} GB</b><span>нужно RAM для этого</span></div>` : ''}
      <div class="kv"><b>${m.kv_gib} GiB</b><span>KV-кэш${m.kv_known ? '' : ' (оценка)'}</span></div>
      <div class="kv"><b id="kv-disk">${m.disk_gb} GB</b><span>нужно диска</span></div>
      <div class="kv"><b>${fmtCtx(m.ctx)}</b><span>контекст${
        m.ctx_max && m.ctx_max > m.ctx ? ` (модель умеет ${fmtCtx(m.ctx_max)})` : ''}</span></div>
      <div class="kv"><b>${fmtK(m.downloads)}</b><span>скачиваний</span></div>
    </div>
    <div class="quant-slot"></div>
    <p class="muted" style="margin:10px 0 0">VRAM = веса + KV-кэш под выбранный контекст + ~${m.overhead_gib} GiB на буферы движка
      (CUDA-контекст, графы и буфер логитов${m.vocab ? ` — словарь ${fmtK(m.vocab)} токенов` : ''}).
      ${m.kv_known ? 'KV посчитан по конфигу модели' : 'Конфиг модели недоступен — KV прикинут по числу параметров'};
      у llama.cpp кэш в q8_0, у vLLM в fp16. При подборе сервера к этой цифре добавляется 10% запаса.</p>
    ${m.ctx_max && m.ctx_max > m.ctx ? `<p class="muted" style="margin:6px 0 0">Модель держит
      ${fmtCtx(m.ctx_max)} контекста, по умолчанию берём ${fmtCtx(m.ctx)}: на полном KV-кэш
      требует больше VRAM, чем сама модель. Поднять — поле «контекст» выше, пересчитается сразу.</p>` : ''}
    ${m.cpu_moe ? `<p class="muted" style="margin:6px 0 0">Это MoE: ${m.moe.experts} экспертов,
      на токен работают ${m.moe.active}. Поэтому их можно держать в обычной оперативке и считать
      на CPU: карта нужна в разы скромнее. Но и скорость падает в разы — на одной и той же
      RTX 3090 замерено 155 ток/с против 24 при полной выгрузке. Это способ поднять модель,
      которая иначе не влезает вообще, а не способ сэкономить на той, что влезает.
      Включается галочкой на вкладке «Сервер».</p>` : ''}
    ${m.moe && !m.cpu_moe ? `<p class="muted" style="margin:6px 0 0">Это MoE:
      ${m.moe.experts} экспертов, на токен работают ${m.moe.active} — <b>но на памяти это не
      экономит</b>. Разреженность сокращает вычисления, а не вес: какие эксперты понадобятся,
      заранее неизвестно, поэтому ${esc(ENGINE_OF[m.format] || 'vLLM')} держит в VRAM всех.
      Выгрузить их в оперативку умеет только llama.cpp — то есть нужна GGUF-сборка этой же
      модели, там на карте останутся единицы гигабайт вместо ${m.vram_gib}.</p>` : ''}`;

  const sel = quantSelect(m);
  if (sel) $('model-card').querySelector('.quant-slot').appendChild(sel);
}

// ───────── серверы ─────────
// vLLM требует, чтобы tensor-parallel был степенью двойки и делил число GPU; остаток
// добирается pipeline-параллелизмом — он медленнее, поэтому такие серверы помечаем.
function vllmSplit(gpuCount) {
  if (state.model?.engine !== 'vllm' || !gpuCount) return '';
  const tp = [8, 4, 2, 1].find((n) => n <= gpuCount && gpuCount % n === 0) || 1;
  const pp = gpuCount / tp;
  return pp > 1 ? `<span class="badge warn" title="tensor-parallel ${tp} × pipeline-parallel ${pp}: карты задействованы все, но pipeline медленнее">TP${tp}×PP${pp}</span>` : '';
}

// Поиск по уже загруженному списку: те же правила, что на сервере (gpu_matches),
// чтобы результат не расходился в зависимости от того, где фильтровали.
function offerMatches(query, o) {
  const hay = `${o.gpu_count}x ${o.gpu || ''} id ${o.id}`.toLowerCase();
  return query.toLowerCase().split(/[\s,]+/).filter(Boolean).every((t) => hay.includes(t));
}

// Рисует список из того, что уже скачано. Фильтр по имени применяется здесь, а не
// на сервере: иначе каждая буква в поле «Поиск» дёргала бы маркетплейс Clore заново.
function renderOffers() {
  const box = $('offers');
  const { offers = [], balance_usd, max_hourly_usd, hidden, cpu_moe: byRam }
    = state.offersData || {};
  const query = $('f-gpu').value.trim();
  const shown = query ? offers.filter((o) => offerMatches(query, o)) : offers;

  // Оплата идёт по часам, поэтому баланс ограничивает не цену сервера, а время работы
  const limitNote = balance_usd
    ? `<p class="muted">Баланс $${balance_usd}: оплата списывается по часам, поэтому брать можно
       любой сервер — вопрос лишь в том, насколько хватит денег. Скрыты только те,
       на которые не хватает и на час (дороже <b>$${max_hourly_usd}/ч</b>).</p>` : '';
  // Почему список такой короткий: из полутора тысяч свободных серверов до списка
  // доходят десятки, и без этой строки догадаться, какой фильтр ослабить, было нельзя
  const cut = Object.entries(hidden || {}).sort((a, b) => b[1] - a[1]);
  const hiddenNote = cut.length
    ? `<p class="muted">Скрыто фильтрами: ${cut.map(([k, v]) => `<b>${v}</b> ${esc(k)}`).join(' · ')}.</p>`
    : '';
  // Чёрный список — не приговор: сервер мог попасть туда из-за разового сбоя,
  // поэтому даём его посмотреть и почистить прямо отсюда
  const banned = (hidden || {})['в чёрном списке'] || 0;
  const banNote = banned
    ? `<p class="muted">В чёрном списке <b>${banned}</b> ${banned === 1 ? 'сервер' : 'серверов'} —
       не поднялись или качали вдвое медленнее заявленного.
       <a href="#" id="ban-clear">очистить список</a></p>`
    : '';
  const foundNote = query
    ? `<p class="muted">По запросу «${esc(query)}» — <b>${shown.length}</b> из ${offers.length} загруженных.</p>`
    : '';
  // С экспертами в оперативке карта почти не участвует в генерации, поэтому порядок тут
  // другой: цена приведена «к токену» через каналы памяти. Без этой строки список
  // выглядит так, будто он сортирован неправильно — дешёвый сервер стоит не первым.
  const ramNote = byRam
    ? `<p class="muted">Эксперты в оперативке: скорость определяет не карта, а пропускная
       способность RAM, поэтому порядок — по цене <b>за токен</b>, а не за час.
       Восьмиканальный EPYC обгоняет вдвое более дешёвый двухканальный десктоп.</p>` : '';

  box.innerHTML = limitNote + hiddenNote + banNote + ramNote + foundNote;
  const clearLink = $('ban-clear');
  if (clearLink) {
    clearLink.onclick = async (e) => {
      e.preventDefault();
      await api('/api/blacklist', { clear: true });
      toast('чёрный список очищен');
      loadOffers();
    };
  }
  if (!shown.length) {
    box.insertAdjacentHTML('beforeend', query
      ? '<p class="muted">под запрос ничего не подошло — очистите поле «Поиск» или смягчите фильтры и нажмите «Найти серверы»</p>'
      : '<p class="muted">подходящих серверов нет — ослабьте фильтры (VRAM, цена, сеть) или пополните баланс</p>');
    return;
  }
  shown.slice(0, OFFERS_SHOWN).forEach((o, i) => {
    const item = el('div', 'item');
    if (state.offer && state.offer.id === o.id) item.classList.add('sel');
    const pay = o.pay_amount_used ? `списание ${fmtCoin(o.pay_amount_used, o.pay_currency_used)}/сутки` : '';
    const split = vllmSplit(o.gpu_count);   // пусто, если модель не на vLLM или раскладка обычная
    item.innerHTML = `<div class="main">
        <div class="name">${o.gpu_count}× ${esc(o.gpu)}
          ${o.arch ? `<span class="badge ${o.gpu_ok === false ? 'gated' : o.gpu_note ? 'warn' : ''}">${esc(o.arch)}</span>` : ''}
          <span class="muted">· id ${o.id}</span></div>
        <div class="meta">${o.vram_gb} GiB VRAM · ${o.ram_gb} GB RAM${
            // Каналы памяти показываем только в режиме выгрузки: там от них напрямую
            // зависит скорость генерации, в остальных случаях это лишняя цифра
            byRam && o.ram_channels ? ` <b>${o.ram_channels}-канальная</b>` : ''
          } · диск ${o.disk_gb || '?'} GB ·
          ↓${Math.round(o.net_down)} Mbps${
            // Clore показывает последний замер, а он бывает разовым всплеском: считаем по
            // медиане истории и показываем её, когда она заметно ниже заявленного
            o.net_down_est && o.net_down > o.net_down_est * 1.3
              ? ` <span class="warn-text">(по истории ~${Math.round(o.net_down_est)})</span>` : ''
          }${o.download_hours ? ` · качать ~${o.download_hours} ч` : ''} ·
          макс. аренда ${o.mrl_hours ?? '?'} ч${pay ? ' · ' + pay : ''}${
            o.hours_affordable ? ` · <b>баланса хватит на ~${o.hours_affordable} ч</b>` : ''}<br>
          надёжность хоста ${(o.reliability * 100).toFixed(1)}%${o.rating_count ? ` · рейтинг ${o.rating.toFixed(1)} (${o.rating_count})` : ''}</div>
        ${o.gpu_note ? `<div class="why ${o.gpu_ok === false ? 'warn-text' : ''}">${esc(o.gpu_note)}</div>` : ''}
      </div>
      <div class="side">
        ${i === 0 ? '<span class="badge">лучшая цена</span>' : ''}
        ${split}
        <b>${fmtUsd(o.price_used)}</b>
        <button>Выбрать</button>
      </div>`;
    item.querySelector('button').onclick = () => {
      state.offer = o;
      box.querySelectorAll('.item').forEach((n) => n.classList.remove('sel'));
      item.classList.add('sel');
      renderPlan();
      toast(`сервер ${o.id} выбран`);
    };
    box.appendChild(item);
  });
  if (shown.length > OFFERS_SHOWN) {
    box.insertAdjacentHTML('beforeend',
      `<p class="muted">показаны ${OFFERS_SHOWN} самых выгодных из ${shown.length} —
       уточните запрос в поле «Поиск»</p>`);
  }
}

// Выгрузка экспертов в оперативку доступна только у MoE-моделей на llama.cpp.
// Галочка меняет и требование к карте, и требование к объёму RAM сервера.
function cpuMoeOn() {
  const box = $('f-cpu-moe');
  return Boolean(box && box.checked && state.model?.cpu_moe);
}

function applyNeed() {
  const m = state.model;
  if (!m) return;
  const box = $('f-cpu-moe');
  if (box) {
    box.closest('label').style.display = m.cpu_moe ? '' : 'none';
    if (!m.cpu_moe) box.checked = false;
  }
  $('f-vram').value = Math.ceil(cpuMoeOn() ? m.vram_cpu_moe_gib : m.vram_gib);
  $('f-ram').value = cpuMoeOn() ? m.ram_gb : '';
  renderPlan();
}

async function loadOffers() {
  const box = $('offers');
  box.innerHTML = '<p class="muted">запрашиваю маркетплейс Clore…</p>';
  busy($('btn-offers'), true, 'ищу…');
  try {
    // Поле «Поиск» сюда не уходит намеренно: берём широкую выборку один раз и дальше
    // фильтруем её на месте, чтобы ввод имени карты не требовал нового запроса к Clore
    const q = new URLSearchParams({
      vram: $('f-vram').value || '0', disk: $('f-disk').value || '0',
      type: $('order-type').value,
      min_net: $('f-net').value || '0', ram: $('f-ram').value || '0',
      weights: String(state.model?.weights_gib || 0), limit: String(OFFERS_POOL),
      // без модели движок неизвестен — тогда показываем всё, включая старые карты
      engine: state.model?.engine || '', quant: state.model?.precision || '',
      old_gpu: $('f-old-gpu')?.checked ? '1' : '',
      // С экспертами в оперативке скорость определяет не карта, а пропускная способность
      // RAM, и сортировать предложения надо иначе. Без этого флага список показывал
      // «лучшую цену» на двухканальном десктопе, а деплой брал восьмиканальный EPYC —
      // список и автоподбор расходились на ровном месте.
      cpu_moe: cpuMoeOn() ? '1' : '',
    });
    if ($('f-price').value.trim()) q.set('max_price', $('f-price').value.trim());
    state.offersData = await api('/api/offers?' + q);
    renderOffers();
  } catch (e) { box.innerHTML = `<p class="muted">ошибка: ${esc(e.message)}</p>`; }
  finally { busy($('btn-offers'), false); }
}

// ───────── план деплоя ─────────
function renderPlan() {
  const m = state.model, o = state.offer;
  if (!m) { $('plan').innerHTML = '<p class="muted">Шаг 1: выберите модель на вкладке «Модель».</p>'; return; }
  $('need').innerHTML = `Модель <code>${esc(m.repo_id)}</code>: нужно ≈${m.vram_gib} GiB VRAM и ${m.disk_gb} GB диска, качать ${m.weights_gib} GiB.`;
  $('plan').innerHTML = `
    <div><b>${esc(m.repo_id)}</b> · ${esc(m.format)} → ${esc(m.engine)} · контекст ${fmtCtx(m.ctx)}</div>
    <div class="muted" style="margin-top:6px">${o
      ? `сервер ${o.id}: ${o.gpu_count}× ${esc(o.gpu)}, ${o.vram_gb} GiB VRAM, ${fmtUsd(o.price_used)}`
      : 'сервер не выбран — возьму самый выгодный из подходящих автоматически'}</div>
    <div class="muted">тип заказа: ${esc($('order-type').value)} · оплата: автоматически USDC, BTC или CLORE по курсу</div>`;
}

// ───────── деплой ─────────
async function startDeploy(dryRun) {
  if (!state.model) return toast('сначала выберите модель', true);
  const btn = dryRun ? $('btn-dry') : $('btn-deploy');
  busy(btn, true, dryRun ? 'проверяю…' : 'запускаю…');
  try {
    await api('/api/deploy', {
      model: state.model.repo_id,
      ctx: $('model-ctx').value || state.model.ctx,
      server_id: state.offer?.id || null,
      order_type: $('order-type').value,
      max_price: $('f-price').value.trim() || null,
      gpu_filter: $('f-gpu').value.trim(),
      image: $('d-image').value.trim(),
      ssh_password: $('d-ssh').value.trim(),
      gguf_file: state.gguf_file || '',
      cpu_moe: cpuMoeOn(),
      force: $('d-force').checked,
      dry_run: !!dryRun,
    });
    document.querySelector('.tab[data-tab="deploy"]').click();
    pollJob();
    toast(dryRun ? 'проверка запущена' : 'деплой запущен — следите за логом');
  } catch (e) { toast(e.message, true); }
  finally { busy(btn, false); }
}

function pollJob() {
  clearInterval(state.polling);
  const tick = async () => {
    try {
      const j = await api('/api/job');
      $('job-log').textContent = j.log.length ? j.log.join('\n') : '— ещё не запускалось —';
      $('job-log').scrollTop = $('job-log').scrollHeight;
      $('job-elapsed').textContent = j.running ? `· идёт ${Math.floor(j.elapsed / 60)} мин ${j.elapsed % 60} с` : '';
      $('btn-deploy').disabled = j.running;
      if (!j.running) {
        clearInterval(state.polling);
        state.polling = null;
        await loadState();
        if (j.error) toast('деплой не завершён: ' + j.error, true);
        else if (j.result?.status === 'ready') toast('модель готова: ' + j.result.api_url);
      }
    } catch (e) { /* UI не падает из-за одного опроса */ }
  };
  tick();
  state.polling = setInterval(tick, 2500);
}

// ───────── заказы и кошельки ─────────
async function loadOrders() {
  const box = $('orders');
  box.innerHTML = '<p class="muted">загружаю…</p>';
  try {
    const { orders } = await api('/api/orders');
    box.innerHTML = '';
    if (!orders.length) { box.innerHTML = '<p class="muted">активных заказов нет</p>'; return; }
    orders.forEach((o) => {
      const item = el('div', 'item');
      item.innerHTML = `<div class="main">
          <div class="name">заказ ${o.id} <span class="muted">· сервер ${o.server}${o.gpu ? ` · ${o.gpu_count}× ${esc(o.gpu)}` : ''}</span></div>
          <div class="meta">${esc(o.image || '')} · ${fmtUsd(o.price_usd)}${o.price != null ? ` (${fmtCoin(o.price, o.currency)}/сутки)` : ''}${o.spot ? ' · spot' : ''}
            ${o.spend_usd != null ? ` · потрачено $${o.spend_usd.toFixed(2)}` : (o.spend != null ? ` · потрачено ${fmtCoin(o.spend, o.currency)}` : '')}<br>
            ${o.api_url ? `API: <code>${esc(o.api_url)}</code>` : 'порты ещё не проброшены'}</div>
        </div>
        <div class="side">
          ${o.api_url ? '<button data-act="use">Использовать</button>' : ''}
          <button class="danger" data-act="cancel">Отменить</button>
        </div>`;
      const use = item.querySelector('[data-act="use"]');
      if (use) use.onclick = async () => {
        busy(use, true); try { const r = await api('/api/use', { order: o.id }); toast('переключено на ' + r.api_url); await loadState(); }
        catch (e) { toast(e.message, true); } finally { busy(use, false); }
      };
      item.querySelector('[data-act="cancel"]').onclick = () => cancelOrder(o.id);
      box.appendChild(item);
    });
  } catch (e) { box.innerHTML = `<p class="muted">ошибка: ${esc(e.message)}</p>`; }
}

async function loadWallets() {
  const box = $('wallets');
  box.innerHTML = '<p class="muted">загружаю…</p>';
  try {
    const { wallets, total_usd } = await api('/api/wallets');
    box.innerHTML = '';
    wallets.forEach((w) => {
      const item = el('div', 'item');
      item.innerHTML = `<div class="main"><div class="name">${esc(w.name)}</div>
          <div class="meta">${fmtCoin(w.balance, w.name)}${w.rate_usd ? ` · курс ${fmtRate(w.rate_usd)}` : ''}
            · депозит: <code>${esc(w.deposit || '—')}</code></div></div>
        <div class="side"><b>${w.balance_usd != null ? '$' + w.balance_usd.toFixed(2) : '—'}</b></div>`;
      box.appendChild(item);
    });
    if (!wallets.length) box.innerHTML = '<p class="muted">кошельков нет</p>';
    else box.appendChild(el('p', 'muted', `Всего на балансе ≈ $${(total_usd ?? 0).toFixed(2)}`));
  } catch (e) { box.innerHTML = `<p class="muted">ошибка: ${esc(e.message)}</p>`; }
}

async function cancelOrder(order) {
  const id = order || state.env?.current?.order;
  if (!id) return toast('нет активного заказа', true);
  if (!confirm(`Отменить заказ ${id}? Сервер освободится, скачанная модель будет удалена.`)) return;
  try {
    await api('/api/cancel', { order: id });
    toast(`заказ ${id} отменён`);
    await loadState();
    loadOrders();
  } catch (e) { toast(e.message, true); }
}

// ───────── настройки ─────────
// Список полей берём из ответа сервера, а не из записанного здесь перечня: раньше он был
// захардкожен пятью ключами, и EXCLUDE_HOSTS в него не попал — поле показывало значение
// из .env, но при сохранении не отправлялось никогда, то есть было декоративным.
// Второе: пустое значение раньше отбрасывалось целиком. Для секретов это правильно
// (показать их нечем, поэтому пусто = «не менять»), а для обычных полей пустота — это
// значение: именно так очищают список исключённых регионов.
async function saveSettings() {
  const body = {};
  let changed = false;
  for (const [k, meta] of Object.entries(state.env?.settings || {})) {
    const input = $('s-' + k);
    if (!input) continue;                       // ключ есть на сервере, но поля в UI нет
    const v = input.value.trim();
    // Тип поля — источник правды наравне с флагом сервера. Если страницу обновили, а
    // app.py ещё старый, флага в ответе нет, и без этой страховки все ключи ушли бы
    // как обычные поля, то есть сохранились бы пустыми — с потерей API-ключей.
    if (input.type === 'password' || meta.secret) {
      if (v) { body[k] = v; changed = true; }
    } else {
      body[k] = v;
      if (v !== (meta.value || '')) changed = true;
    }
  }
  if (!changed) return toast('нечего сохранять', true);
  busy($('btn-save'), true, 'сохраняю…');
  try {
    const r = await api('/api/settings', body);
    // чистим только поля-пароли: остальные показывают текущее значение, и стирать его
    // после сохранения нельзя — иначе очищенным выглядит то, что мы только что записали
    Object.keys(body).forEach((k) => { if ($('s-' + k).type === 'password') $('s-' + k).value = ''; });
    $('save-note').textContent = 'сохранено: ' + r.saved.join(', ');
    await loadState();
    toast('настройки записаны в .env');
  } catch (e) { toast(e.message, true); }
  finally { busy($('btn-save'), false); }
}

// ───────── привязка ─────────
$('btn-pick').onclick = pickModel;
$('model-ref').addEventListener('keydown', (e) => { if (e.key === 'Enter') pickModel(); });
// Контекст меняет и KV-кэш, и требование к VRAM, и список подходящих серверов. Без этого
// обработчика поле было декоративным: введённое значение не применялось ничем, а pickModel
// при следующем нажатии «Выбрать» ещё и затирал его обратно.
$('model-ctx').onchange = () => { if (state.model) pickModel(); };
$('btn-search').onclick = searchModels;
// Справочник по форматам лежит в карточке выше, свёрнутым. Ссылка из подсказки под фильтром
// раскрывает его и подводит к нему — иначе про него просто не узнают.
$('to-formats').onclick = (e) => {
  e.preventDefault();
  const d = $('explain-formats');
  d.open = true;
  d.scrollIntoView({ behavior: 'smooth', block: 'center' });
};
$('q').addEventListener('keydown', (e) => { if (e.key === 'Enter') searchModels(); });
$('fmt').onchange = searchModels;
document.querySelectorAll('#periods button').forEach((b) => {
  b.onclick = () => {
    state.period = b.dataset.period;
    document.querySelectorAll('#periods button').forEach((n) => n.classList.toggle('on', n === b));
    searchModels();
  };
});
$('btn-opencode').onclick = async () => {
  busy($('btn-opencode'), true, 'пишу…');
  try {
    const { paths } = await api('/api/opencode', {});
    // путь показываем прямо в карточке: при проблеме «модели нет в списке» ищут именно его
    $('current').insertAdjacentHTML('beforeend',
      `<p class="muted">Провайдер записан в <code>${esc(paths[0])}</code>${
        paths.length > 1 ? ` и ещё ${paths.length - 1} копии` : ''}. Перезапустите OpenCode
        и выберите провайдера «uncensored».</p>`);
    toast('конфиг OpenCode обновлён');
  } catch (e) { toast(e.message, true); }
  finally { busy($('btn-opencode'), false); }
};
$('btn-offers').onclick = loadOffers;
$('f-cpu-moe').onchange = () => { applyNeed(); loadOffers(); };
// Фильтр применяется к уже загруженному списку — поэтому мгновенно и без запроса к Clore
$('f-gpu').oninput = () => { if (state.offersData) renderOffers(); };
$('f-gpu').onkeydown = (e) => { if (e.key === 'Enter') loadOffers(); };
$('order-type').onchange = renderPlan;
$('btn-deploy').onclick = () => startDeploy(false);
$('btn-dry').onclick = () => startDeploy(true);
$('btn-cancel').onclick = () => cancelOrder();
$('btn-orders').onclick = loadOrders;
$('btn-wallets').onclick = loadWallets;
$('btn-save').onclick = saveSettings;
$('btn-refresh').onclick = () => loadState().then(() => toast('обновлено'));
$('btn-genkey').onclick = () => {
  const b = new Uint8Array(24);
  crypto.getRandomValues(b);
  $('s-API_KEY').value = Array.from(b, (x) => x.toString(16).padStart(2, '0')).join('');
  $('s-API_KEY').type = 'text';
  toast('ключ сгенерирован — не забудьте «Сохранить»');
};
$('btn-bench').onclick = async () => {
  busy($('btn-bench'), true, 'гоняю…');
  try {
    const r = await api('/api/bench', {});
    toast(r.tokens_per_sec ? `${r.tokens_per_sec.toFixed(0)} ток/с` : 'замер не удался — API не ответил', !r.tokens_per_sec);
  } catch (e) { toast(e.message, true); }
  finally { busy($('btn-bench'), false); }
};

loadState().then(searchModels).catch((e) => toast(e.message, true));
pollJob();
