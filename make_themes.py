# -*- coding: utf-8 -*-
"""
make_themes.py — генерирует static/themes.html: галерею вариантов оформления UI.

Палитры собираются из HSL, а не пишутся руками по одному цвету: так фон, панели, границы
и текст внутри одного варианта гарантированно согласованы по тону и контрасту, а между
вариантами меняется ровно то, что задумано (оттенок поверхности, акцент, форма, шрифт).

Запуск: python make_themes.py
"""
import colorsys
import io
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "static", "themes.html")

UI_FONT = '"Segoe UI", Inter, system-ui, sans-serif'
MONO_FONT = 'Consolas, "JetBrains Mono", "Cascadia Mono", monospace'
SERIF_FONT = 'Georgia, "Iowan Old Style", "Times New Roman", serif'
ROUND_FONT = '"Segoe UI Variable Display", ui-rounded, "Segoe UI", system-ui, sans-serif'


def hx(h, s, light):
    """HSL (0-360, 0-100, 0-100) -> #rrggbb."""
    r, g, b = colorsys.hls_to_rgb((h % 360) / 360.0, light / 100.0, s / 100.0)
    return "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))


def dark_palette(hue, sat, accent_hue, accent_sat=70, accent_l=62, lift=0):
    """Тёмная схема: три ступени поверхности, приглушённый текст, яркий акцент."""
    return {
        "bg": hx(hue, sat, 7 + lift),
        "panel": hx(hue, sat, 11 + lift),
        "panel2": hx(hue, sat, 15 + lift),
        "line": hx(hue, max(sat, 8), 22 + lift),
        "text": hx(hue, 18, 92),
        "muted": hx(hue, 12, 62),
        "accent": hx(accent_hue, accent_sat, accent_l),
        "accent2": hx(accent_hue, accent_sat, max(28, accent_l - 26)),
        "ok": hx(140, 50, 55), "warn": hx(38, 70, 58), "bad": hx(5, 70, 60),
    }


def light_palette(hue, sat, accent_hue, accent_sat=70, accent_l=45):
    """Светлая схема: подложка чуть темнее карточек, границы мягкие."""
    return {
        "bg": hx(hue, sat, 96),
        "panel": hx(hue, max(sat - 4, 0), 100),
        "panel2": hx(hue, sat, 94),
        "line": hx(hue, sat, 86),
        "text": hx(hue, 25, 14),
        "muted": hx(hue, 12, 44),
        "accent": hx(accent_hue, accent_sat, accent_l),
        "accent2": hx(accent_hue, accent_sat, max(22, accent_l - 16)),
        "ok": hx(140, 55, 34), "warn": hx(32, 80, 38), "bad": hx(5, 70, 46),
    }


# Десять направлений по десять вариантов. Акценты заданы оттенками: внутри группы
# меняется только он и немного тон поверхности — так видно, что даёт сам цвет.
GROUPS = [
    {"group": "Дев-тулзы", "note": "как Linear, Vercel, Grafana: ровные поверхности, тонкие границы",
     "mode": "dark", "hue": 220, "sat": 16, "radius": 10, "font": UI_FONT,
     "shadow": "0 1px 0 rgba(255,255,255,.03)", "accents": [
         (214, "Сталь"), (190, "Лазурь"), (160, "Мята"), (266, "Индиго"), (320, "Фуксия"),
         (24, "Янтарь"), (0, "Киноварь"), (120, "Хвоя"), (45, "Латунь"), (280, "Аметист")]},
    {"group": "Терминал", "note": "моноширинный шрифт, прямые углы, фосфорный акцент",
     "mode": "dark", "hue": 150, "sat": 10, "radius": 2, "font": MONO_FONT,
     "shadow": "none", "accent_sat": 85, "accent_l": 56, "accents": [
         (120, "Фосфор"), (90, "Кислота"), (45, "Янтарный CRT"), (180, "Циан"), (300, "Магента"),
         (0, "Тревога"), (210, "Синий экран"), (160, "Изумруд"), (60, "Жёлтый"), (330, "Розовый")]},
    {"group": "Мягкая тьма", "note": "глубокие поверхности, скруглённые углы, рассеянная тень",
     "mode": "dark", "hue": 240, "sat": 14, "radius": 16, "font": ROUND_FONT,
     "shadow": "0 8px 24px rgba(0,0,0,.38)", "accent_sat": 62, "accents": [
         (250, "Сумерки"), (200, "Туман"), (170, "Полынь"), (300, "Сирень"), (18, "Персик"),
         (340, "Коралл"), (140, "Шалфей"), (220, "Деним"), (40, "Песок"), (280, "Вереск")]},
    {"group": "Светлый минимализм", "note": "как Stripe: белые карточки, много воздуха",
     "mode": "light", "hue": 220, "sat": 14, "radius": 10, "font": UI_FONT,
     "shadow": "0 1px 2px rgba(16,24,40,.06)", "accents": [
         (224, "Классика"), (200, "Небо"), (160, "Трава"), (265, "Фиалка"), (330, "Малина"),
         (20, "Терракота"), (0, "Алый"), (120, "Лес"), (45, "Горчица"), (290, "Слива")]},
    {"group": "Тёплая бумага", "note": "сепия и кремовые поверхности, спокойный контраст",
     "mode": "light", "hue": 38, "sat": 32, "radius": 8, "font": SERIF_FONT,
     "shadow": "0 1px 2px rgba(80,60,30,.10)", "accent_l": 40, "accents": [
         (25, "Охра"), (12, "Кирпич"), (90, "Олива"), (200, "Чернила"), (340, "Бордо"),
         (160, "Патина"), (260, "Лаванда"), (45, "Мёд"), (0, "Сургуч"), (130, "Мох")]},
    {"group": "Нео-брутализм", "note": "жёсткие границы, смещённая тень, чистый цвет",
     "mode": "light", "hue": 48, "sat": 60, "radius": 0, "font": UI_FONT,
     "shadow": "4px 4px 0 var(--text)", "accent_sat": 85, "accent_l": 50, "accents": [
         (50, "Жёлтый"), (200, "Электрик"), (350, "Красный"), (140, "Зелёный"), (280, "Пурпур"),
         (20, "Оранж"), (170, "Бирюза"), (320, "Розовый"), (90, "Лайм"), (230, "Кобальт")]},
    {"group": "Высокий контраст", "note": "максимальная читаемость: почти чёрный фон, яркий текст",
     "mode": "dark", "hue": 0, "sat": 0, "radius": 6, "font": UI_FONT,
     "shadow": "none", "accent_sat": 95, "accent_l": 64, "accents": [
         (55, "Жёлтый"), (120, "Зелёный"), (195, "Голубой"), (25, "Оранжевый"), (300, "Розовый"),
         (0, "Красный"), (160, "Аква"), (260, "Лиловый"), (85, "Салат"), (220, "Синий")]},
    {"group": "Северный", "note": "приглушённая пастель на холодной подложке",
     "mode": "dark", "hue": 210, "sat": 20, "radius": 12, "font": UI_FONT, "lift": 4,
     "shadow": "0 2px 10px rgba(0,0,0,.25)", "accent_sat": 45, "accent_l": 66, "accents": [
         (205, "Фьорд"), (185, "Лёд"), (150, "Хвоя"), (255, "Ночь"), (10, "Рябина"),
         (35, "Дюна"), (320, "Вереск"), (95, "Тундра"), (270, "Полярный"), (170, "Мох")]},
    {"group": "Неон", "note": "насыщенные акценты на чернильном фоне, свечение у кнопок",
     "mode": "dark", "hue": 265, "sat": 30, "radius": 14, "font": UI_FONT, "glow": True,
     "shadow": "0 0 0 1px rgba(255,255,255,.04)", "accent_sat": 88, "accent_l": 60, "accents": [
         (285, "Ультрафиолет"), (190, "Киан"), (330, "Неон-розовый"), (150, "Токсин"), (45, "Вольфрам"),
         (215, "Плазма"), (0, "Лазер"), (105, "Матрица"), (250, "Виолет"), (170, "Аквамарин")]},
    {"group": "Редакционный", "note": "крупная типографика с засечками, сдержанный акцент",
     "mode": "light", "hue": 30, "sat": 10, "radius": 4, "font": SERIF_FONT,
     "shadow": "none", "accent_sat": 55, "accent_l": 38, "accents": [
         (355, "Киноварь"), (215, "Морской"), (150, "Малахит"), (275, "Пурпур"), (35, "Умбра"),
         (95, "Хаки"), (190, "Сланец"), (320, "Пион"), (10, "Ржавчина"), (240, "Полночь")]},
]


def build():
    themes, n = [], 0
    for g in GROUPS:
        for hue, accent_name in g["accents"]:
            n += 1
            kw = {"accent_sat": g.get("accent_sat", 70)}
            if g["mode"] == "dark":
                kw["accent_l"] = g.get("accent_l", 62)
                kw["lift"] = g.get("lift", 0)
                pal = dark_palette(g["hue"], g["sat"], hue, **kw)
            else:
                kw["accent_l"] = g.get("accent_l", 45)
                pal = light_palette(g["hue"], g["sat"], hue, **kw)
            themes.append({
                "n": n, "name": f"{g['group']} · {accent_name}", "group": g["group"],
                "note": g["note"], "mode": g["mode"], "radius": g["radius"],
                "font": g["font"], "shadow": g["shadow"], "glow": bool(g.get("glow")),
                "vars": pal,
            })
    return themes


HTML = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>UncensoredAI — варианты оформления</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin: 0; background: #0b0d12; color: #e6edf3;
         font: 14px/1.5 "Segoe UI", Inter, system-ui, sans-serif; }
  .head { position: sticky; top: 0; z-index: 5; background: #0b0d12cc;
          backdrop-filter: blur(8px); border-bottom: 1px solid #222b3a; padding: 14px 20px; }
  .head h1 { margin: 0 0 4px; font-size: 18px; }
  .head p { margin: 0; color: #8b97a8; font-size: 12px; }
  .tools { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 10px; }
  .tools button, .tools input {
    background: #161b24; color: #e6edf3; border: 1px solid #273041;
    border-radius: 8px; padding: 6px 12px; font-size: 12px; cursor: pointer; }
  .tools button.on { border-color: #6aa9ff; color: #6aa9ff; }
  .tools input { cursor: text; min-width: 180px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(330px, 1fr));
          gap: 16px; padding: 18px 20px 60px; }
  .cell { border: 1px solid #222b3a; border-radius: 12px; overflow: hidden; background: #11141b; }
  .cell.sel { border-color: #6aa9ff; box-shadow: 0 0 0 1px #6aa9ff; }
  .cap { display: flex; align-items: center; gap: 8px; padding: 9px 12px; font-size: 12px;
         border-bottom: 1px solid #222b3a; }
  .cap b { font-weight: 600; }
  .cap .num { color: #8b97a8; font-variant-numeric: tabular-nums; }
  .cap .spacer { flex: 1; }
  .cap button { background: #1d2430; color: #e6edf3; border: 1px solid #273041;
                border-radius: 6px; padding: 4px 10px; font-size: 11px; cursor: pointer; }
  .cap button:hover { border-color: #6aa9ff; color: #6aa9ff; }
  .note { padding: 6px 12px 0; color: #76829a; font-size: 11px; }

  /* ───── предпросмотр: та же разметка, что у приложения, только мельче ───── */
  .prev { margin: 10px; border-radius: var(--r); overflow: hidden; border: 1px solid var(--line);
          background: var(--bg); color: var(--text); font-family: var(--f); }
  .p-top { display: flex; align-items: center; gap: 8px; padding: 9px 11px;
           background: var(--panel); border-bottom: 1px solid var(--line); }
  .p-top b { font-size: 13px; }
  .p-top b i { color: var(--accent); font-style: normal; }
  .p-top a { color: var(--muted); font-size: 10px; text-decoration: none;
             border-bottom: 1px dashed var(--line); }
  .p-pill { margin-left: auto; font-size: 10px; padding: 2px 8px; border-radius: 999px;
            background: var(--panel2); border: 1px solid var(--line); color: var(--ok); }
  .p-tabs { display: flex; gap: 4px; padding: 8px 11px 0; background: var(--bg); }
  .p-tabs span { font-size: 10px; padding: 5px 9px; color: var(--muted);
                 border: 1px solid transparent; border-radius: var(--r) var(--r) 0 0; }
  .p-tabs span.on { color: var(--text); background: var(--panel);
                    border-color: var(--line); border-bottom-color: var(--panel); }
  .p-body { padding: 11px; background: var(--bg); }
  .p-card { background: var(--panel); border: 1px solid var(--line); border-radius: var(--r);
            padding: 11px; box-shadow: var(--sh); }
  .p-h { font-size: 12px; font-weight: 600; margin-bottom: 3px; }
  .p-m { font-size: 10px; color: var(--muted); }
  .p-row { display: flex; align-items: center; gap: 7px; margin-top: 9px; }
  .p-badge { font-size: 9px; padding: 2px 7px; border-radius: 999px;
             background: var(--panel2); border: 1px solid var(--line); color: var(--muted); }
  .p-price { margin-left: auto; font-weight: 700; font-size: 12px; }
  .p-btn { font-size: 10px; padding: 5px 11px; border-radius: var(--r);
           background: var(--accent); color: var(--btn-fg); border: 1px solid var(--accent2);
           box-shadow: var(--glow); font-family: var(--f); }
  .p-sw { display: flex; gap: 3px; margin-top: 9px; }
  .p-sw i { width: 15px; height: 7px; border-radius: 2px; display: block; }
</style>
</head>
<body>
<div class="head">
  <h1>Варианты оформления — 100 штук</h1>
  <p>Каждый вариант показан на настоящей разметке приложения: шапка со ссылкой на канал, вкладки,
     карточка модели и строка сервера. «Применить» сразу меняет оформление сайта, «Вернуть
     исходное» — откатывает.</p>
  <div class="tools" id="tools"></div>
</div>
<div class="grid" id="grid"></div>
<script>
const THEMES = __THEMES__;

// Тёмный акцент требует светлого текста на кнопке и наоборот — считаем по яркости.
function btnFg(hex) {
  const n = parseInt(hex.slice(1), 16);
  const lum = (0.2126 * (n >> 16) + 0.7152 * ((n >> 8) & 255) + 0.0722 * (n & 255)) / 255;
  return lum > 0.6 ? '#0b0d12' : '#ffffff';
}

function cssFor(t) {
  const v = t.vars;
  return `:root{--bg:${v.bg};--panel:${v.panel};--panel-2:${v.panel2};--line:${v.line};` +
    `--text:${v.text};--muted:${v.muted};--accent:${v.accent};--accent-2:${v.accent2};` +
    `--ok:${v.ok};--warn:${v.warn};--bad:${v.bad};}\\n` +
    `body{font-family:${t.font};}\\n` +
    `.card,.item,.model-card{border-radius:${t.radius}px;box-shadow:${t.shadow};}\\n` +
    `button,input,select,.pill,.badge{border-radius:${Math.min(t.radius, 10)}px;}\\n` +
    `button.primary{background:${v.accent};color:${btnFg(v.accent)};border-color:${v.accent2};` +
    (t.glow ? `box-shadow:0 0 14px ${v.accent}66;` : '') + `}\\n`;
}

function cell(t) {
  const v = t.vars;
  const d = document.createElement('div');
  d.className = 'cell';
  d.dataset.group = t.group;
  d.dataset.name = t.name.toLowerCase();
  d.style.setProperty('--r', t.radius + 'px');
  d.style.setProperty('--f', t.font);
  d.style.setProperty('--sh', t.shadow);
  d.style.setProperty('--glow', t.glow ? `0 0 12px ${v.accent}66` : 'none');
  d.style.setProperty('--btn-fg', btnFg(v.accent));
  for (const [k, val] of Object.entries(v)) d.style.setProperty('--' + k, val);
  d.style.setProperty('--panel2', v.panel2);
  d.innerHTML = `
    <div class="cap"><span class="num">${String(t.n).padStart(3, '0')}</span>
      <b>${t.name}</b><span class="spacer"></span>
      <button data-act="css">CSS</button><button data-act="use">Применить</button></div>
    <div class="note">${t.note}</div>
    <div class="prev">
      <div class="p-top"><b>Uncensored<i>AI</i></b><a>ТГ канал автора</a>
        <span class="p-pill">модель готова</span></div>
      <div class="p-tabs"><span class="on">1 · Модель</span><span>2 · Сервер</span><span>3 · Деплой</span></div>
      <div class="p-body">
        <div class="p-card">
          <div class="p-h">Qwen3.8-27B UNCENSORED</div>
          <div class="p-m">gguf · llama.cpp · 15.8 ГиБ · 74 ток/с</div>
          <div class="p-row"><span class="p-badge">Blackwell</span>
            <span class="p-badge">32 GiB VRAM</span>
            <span class="p-price">$0.562/ч</span>
            <button class="p-btn">Выбрать</button></div>
          <div class="p-sw">
            <i style="background:${v.accent}"></i><i style="background:${v.ok}"></i>
            <i style="background:${v.warn}"></i><i style="background:${v.bad}"></i>
            <i style="background:${v.panel2}"></i><i style="background:${v.line}"></i></div>
        </div>
      </div>
    </div>`;
  d.querySelector('[data-act="css"]').onclick = () => {
    navigator.clipboard.writeText(cssFor(t));
    d.querySelector('[data-act="css"]').textContent = 'скопировано';
    setTimeout(() => { d.querySelector('[data-act="css"]').textContent = 'CSS'; }, 1200);
  };
  d.querySelector('[data-act="use"]').onclick = async () => {
    await fetch('/api/theme', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: t.name, css: cssFor(t) }) });
    document.querySelectorAll('.cell').forEach((c) => c.classList.remove('sel'));
    d.classList.add('sel');
  };
  return d;
}

const grid = document.getElementById('grid');
THEMES.forEach((t) => grid.appendChild(cell(t)));

const groups = [...new Set(THEMES.map((t) => t.group))];
const tools = document.getElementById('tools');
const search = document.createElement('input');
search.placeholder = 'поиск по названию';
let active = 'все';
function apply() {
  const q = search.value.trim().toLowerCase();
  document.querySelectorAll('.cell').forEach((c) => {
    const okGroup = active === 'все' || c.dataset.group === active;
    c.style.display = okGroup && (!q || c.dataset.name.includes(q)) ? '' : 'none';
  });
}
['все', ...groups].forEach((g) => {
  const b = document.createElement('button');
  b.textContent = g;
  if (g === 'все') b.classList.add('on');
  b.onclick = () => {
    active = g;
    tools.querySelectorAll('button').forEach((n) => n.classList.toggle('on', n === b));
    apply();
  };
  tools.appendChild(b);
});
const reset = document.createElement('button');
reset.textContent = '✕ вернуть исходное';
reset.onclick = async () => {
  await fetch('/api/theme', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ reset: true }) });
  document.querySelectorAll('.cell').forEach((c) => c.classList.remove('sel'));
};
tools.appendChild(reset);
tools.appendChild(search);
search.oninput = apply;
</script>
</body>
</html>
"""


def main():
    themes = build()
    html = HTML.replace("__THEMES__", json.dumps(themes, ensure_ascii=False))
    io.open(OUT, "w", encoding="utf-8", newline="\n").write(html)
    print(f"готово: {len(themes)} вариантов -> {OUT}")


if __name__ == "__main__":
    main()
