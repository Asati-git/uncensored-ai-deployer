'use strict';
// ───────── язык интерфейса ─────────
// Исходные тексты интерфейса — русские, и в коде они остаются русскими. Английский
// накладывается поверх: наблюдатель за DOM переводит каждый появившийся текст по словарю
// (точные строки + шаблоны с подстановками). Так перевод не размазан по логике app.js,
// а сообщения сервера (ошибки, тосты) переводятся тем же словарём, когда попадают на экран.
//
// Словари: static/i18n-ui.js (тексты страницы) и static/i18n-errors.js (сообщения сервера).
// Формат записи: I18N_EN.exact['русский текст'] = 'English text';
//                I18N_EN.patterns.push([/^шаблон (\d+)$/, 'template $1']);
// Ключ — текст с пробелами, схлопнутыми до одного, без пробелов по краям.

const I18N_EN = window.I18N_EN || (window.I18N_EN = { exact: {}, patterns: [] });

const I18N = {
  lang: localStorage.getItem('lang') || ((navigator.language || '').toLowerCase().startsWith('ru') ? 'ru' : 'en'),
  // тексты, которые не переводим: лог деплоя и установки, код, пользовательские данные
  skip: 'script, style, code, pre, textarea, #job-log, #local-log, .repo, [data-no-i18n]',
  attrs: ['placeholder', 'title', 'aria-label'],
  orig: new WeakMap(),       // текстовый узел -> исходный русский текст
  mine: new WeakMap(),       // текстовый узел -> текст, который поставили мы сами
  busy: false,
};

const norm = (s) => String(s).replace(/\s+/g, ' ').trim();
const HAS_CYR = /[А-Яа-яЁё]/;

// Перевод целой строки. Нет перевода — вернётся исходная строка.
function tr(text) {
  if (I18N.lang !== 'en' || text == null) return text;
  const src = String(text);
  if (!HAS_CYR.test(src)) return src;
  const key = norm(src);
  let out = I18N_EN.exact[key];
  if (out === undefined) {
    for (const [rx, rep] of I18N_EN.patterns) {
      if (rx.test(key)) {
        out = key.replace(rx, (...m) => (typeof rep === 'function' ? rep(...m)
          : rep.replace(/\$(\d)/g, (_, n) => trPart(m[Number(n)] ?? ''))));
        break;
      }
    }
  }
  if (out === undefined) return src;
  // сохраняем пробелы по краям — от них зависит вёрстка соседних инлайн-элементов
  const lead = src.match(/^\s*/)[0], trail = src.match(/\s*$/)[0];
  return lead + out + trail;
}

// Подставленный фрагмент сам может быть русской фразой (например, причина ошибки) —
// переводим и его, если в словаре есть
function trPart(s) {
  const t = tr(s);
  return t === undefined ? s : t;
}

// Исходный (русский) текст элемента, даже если на экране он уже переведён. Нужен коду,
// который читает надпись, чтобы потом вернуть её (busy) или вставить в другую фразу
// (панель этапов): иначе в RU-режим просачивался бы английский текст
function i18nSource(el) {
  if (!el) return '';
  let out = '';
  const w = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
  let n;
  while ((n = w.nextNode())) {
    const o = I18N.orig.get(n);
    out += (o !== undefined && I18N.mine.get(n) === n.data) ? o : n.data;
  }
  return out;
}
window.i18nSource = i18nSource;

function skipNode(node) {
  const el = node.nodeType === 1 ? node : node.parentElement;
  return !el || Boolean(el.closest(I18N.skip));
}

function translateText(node) {
  if (skipNode(node)) return;
  const cur = node.data;
  if (I18N.mine.get(node) !== cur) {
    // текст поставило приложение (или узел видим впервые)
    if (!HAS_CYR.test(cur)) {
      // без русского (цифры, адрес) — переводить нечего, прежний оригинал не действует
      I18N.orig.delete(node);
      I18N.mine.set(node, cur);
      return;
    }
    I18N.orig.set(node, cur);
  }
  const src = I18N.orig.get(node);
  if (src === undefined) return;
  const want = I18N.lang === 'en' ? tr(src) : src;
  if (want !== cur) node.data = want;
  I18N.mine.set(node, want);
}

function translateAttrs(el) {
  if (el.closest(I18N.skip)) return;
  for (const a of I18N.attrs) {
    if (!el.hasAttribute(a)) continue;
    const cur = el.getAttribute(a);
    const key = 'i18n' + a.replace(/-./g, (x) => x[1].toUpperCase());
    if (HAS_CYR.test(cur)) el.dataset[key] = cur;
    const src = el.dataset[key];
    if (src === undefined) continue;
    const want = I18N.lang === 'en' ? tr(src) : src;
    if (want !== cur) el.setAttribute(a, want);
  }
}

function translateTree(root) {
  if (!root) return;
  if (root.nodeType === 3) return translateText(root);
  if (root.nodeType !== 1) return;
  translateAttrs(root);
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT);
  let n = walker.nextNode();
  while (n) {
    if (n.nodeType === 3) translateText(n); else translateAttrs(n);
    n = walker.nextNode();
  }
}

function applyLang() {
  I18N.busy = true;
  try {
    document.documentElement.lang = I18N.lang;
    translateTree(document.body);
    if (!I18N.titleRu) I18N.titleRu = document.title;
    document.title = I18N.lang === 'en' ? tr(I18N.titleRu) : I18N.titleRu;
    document.querySelectorAll('#lang button').forEach((b) => b.classList.toggle('on', b.dataset.lang === I18N.lang));
  } finally { I18N.busy = false; }
}

function setLang(lang) {
  I18N.lang = lang === 'en' ? 'en' : 'ru';
  localStorage.setItem('lang', I18N.lang);
  applyLang();
}

// Всё, что приложение дорисует потом (списки, карточки, тосты), переводится по ходу
new MutationObserver((records) => {
  if (I18N.busy) return;
  I18N.busy = true;
  try {
    for (const r of records) {
      if (r.type === 'characterData') translateText(r.target);
      else if (r.type === 'attributes') translateAttrs(r.target);
      else r.addedNodes.forEach(translateTree);
    }
  } finally { I18N.busy = false; }
}).observe(document.documentElement, {
  subtree: true, childList: true, characterData: true,
  attributes: true, attributeFilter: I18N.attrs,
});

// confirm() показывает текст в системном окне — его DOM-наблюдатель не видит
const nativeConfirm = window.confirm.bind(window);
window.confirm = (msg) => nativeConfirm(tr(msg));

document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('#lang button').forEach((b) => { b.onclick = () => setLang(b.dataset.lang); });
  applyLang();
});
