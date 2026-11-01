# -*- coding: utf-8 -*-
"""
hfmodels.py — поиск актуальных uncensored-моделей на Hugging Face, разбор произвольной
ссылки на репозиторий и определение формата квантования -> какой движок поднимать.

Всё на stdlib, HF-токен не обязателен (нужен только для gated-репозиториев).
"""
import concurrent.futures
import datetime
import json
import re
import urllib.error
import urllib.parse
import urllib.request

HF_API = "https://huggingface.co/api"

# По каким словам ищем «расцензуренные» модели. Порядок = приоритет в сводном списке.
UNCENSORED_TERMS = [
    "uncensored", "abliterated", "unfiltered", "amoral",
    "heretic", "josiefied", "dolphin", "tiger-gemma",
    # Линейки 2026 года: dealignai зовёт свой метод «de-align/CRACK», no-refusal —
    # обычное имя для LoRA поверх refusal-direction. Без этих слов они в сводку не попадали.
    "dealign", "no-refusal",
]
# Слова, по которым модель считается uncensored при проверке произвольной ссылки.
_UNCENSORED_RE = re.compile(
    r"uncensor|abliterat|unfilter|amoral|heretic|josiefied|dolphin|tiger-gemma|"
    r"no[-_]?refusal|decensor|orthogonal|neural[-_]?daredevil|lewd|unalign|"
    r"dealign|obliteratus",
    re.I,
)

# Движки: во что превращается формат весов
ENGINE_TABBY = "tabby"        # ExLlamaV3/V2 (.exl3/.exl2) -> TabbyAPI
ENGINE_LLAMACPP = "llamacpp"  # GGUF -> llama.cpp server
ENGINE_VLLM = "vllm"          # safetensors fp16/awq/gptq/fp8 -> vLLM

GIB = 1024 ** 3

# Контекст по умолчанию. Берём нативный из конфига модели, но с потолком: у свежих Qwen
# заявлены 262k, а с YaRN и до 1M, и на таком контексте один KV-кэш требует больше VRAM,
# чем сама модель — подбор уезжает в мультиGPU там, где он не нужен. Потолок снимается
# вручную: поле «контекст» в UI или --ctx в CLI принимают любое значение до ctx_max.
DEFAULT_CTX = 32768
CTX_CAP = 131072


# ---------- HTTP ----------
def _get(path, token=None, timeout=25):
    headers = {"User-Agent": "UncensoredAI/1.0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(f"{HF_API}/{path}", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 404):
            raise RuntimeError("репозиторий не найден, приватный или gated — проверьте ссылку "
                               "и задайте HF_TOKEN в настройках")
        raise RuntimeError(f"Hugging Face HTTP {e.code}: {e.read().decode(errors='replace')[:200]}")
    except (urllib.error.URLError, TimeoutError) as e:
        raise RuntimeError(f"Hugging Face недоступен: {e}")


# ---------- ссылки ----------
def normalize_ref(value):
    """
    Принимает что угодно и возвращает repo_id 'owner/name':
      https://huggingface.co/owner/name/tree/main  -> owner/name
      hf.co/owner/name                             -> owner/name
      owner/name                                  -> owner/name
    """
    s = (value or "").strip()
    if not s:
        raise ValueError("пустая ссылка на модель")
    if "://" in s or s.startswith("hf.co") or s.startswith("huggingface.co"):
        if "://" not in s:
            s = "https://" + s
        parts = [p for p in urllib.parse.urlparse(s).path.split("/") if p]
        if len(parts) < 2:
            raise ValueError(f"не похоже на репозиторий модели: {value}")
        if parts[0] in ("models", "datasets", "spaces"):
            parts = parts[1:]
        s = "/".join(parts[:2])
    s = s.strip("/")
    if s.count("/") != 1:
        raise ValueError(f"ожидался формат owner/name, получено: {value}")
    return s


# ---------- поиск ----------
# Периоды популярности. HF отдаёт только два точных счётчика: downloads (за 30 дней)
# и downloadsAllTime; недельных и дневных нет, поэтому неделя/день — это trendingScore
# (рейтинг HF «что популярно прямо сейчас») среди моделей, обновлённых за N дней.
PERIODS = {
    "all":   {"hf_sort": "downloads",     "key": "downloads_all", "fresh_days": None, "exact": True},
    "month": {"hf_sort": "downloads",     "key": "downloads",     "fresh_days": None, "exact": True},
    "week":  {"hf_sort": "trendingScore", "key": "trending",      "fresh_days": 7,    "exact": False},
    "day":   {"hf_sort": "trendingScore", "key": "trending",      "fresh_days": 2,    "exact": False},
    "new":   {"hf_sort": "lastModified",  "key": "updated",       "fresh_days": None, "exact": True},
}
DEFAULT_PERIOD = "month"


def _search_raw(term, sort="downloads", limit=30, token=None):
    q = urllib.parse.urlencode([
        ("search", term), ("sort", sort), ("direction", -1), ("limit", limit),
        # Фильтра по pipeline_tag здесь нет намеренно: у множества GGUF-репозиториев
        # (в том числе у DavidAU и других авторов расцензуренных сборок) тег не выставлен,
        # и с filter=text-generation они просто не находились — в выдаче оставались чужие
        # зеркала с нулём скачиваний. Лишнее отсекаем на своей стороне по _NON_LLM_RE.
        # без expand HF не отдаёт lastModified, счётчик за всё время, тренд и gated
        ("expand[]", "lastModified"), ("expand[]", "downloads"),
        ("expand[]", "downloadsAllTime"), ("expand[]", "trendingScore"),
        ("expand[]", "createdAt"), ("expand[]", "likes"),
        ("expand[]", "tags"), ("expand[]", "gated"),
    ])
    try:
        return _get(f"models?{q}", token)
    except RuntimeError:
        return []


# Чем HF отвечает на те же ключевые слова помимо языковых моделей: картиночные
# «uncensored»-чекпойнты, эмбеддеры, распознавание речи. Такое в список не нужно.
_NON_LLM_RE = re.compile(
    r"^(text-to-image|image-to-image|text-to-video|image-to-video|diffusers|stable-diffusion|"
    r"sentence-similarity|feature-extraction|automatic-speech-recognition|text-to-speech|"
    r"text-classification|token-classification|fill-mask|image-classification|"
    r"audio-classification|object-detection|controlnet|lora|"
    r"image-generation|video-generation|comfyui|comfyui-gguf|flux|sdxl)$",
    re.I,
)


def _is_llm(card):
    tags = card.get("tags") or []
    if any(_NON_LLM_RE.match(t) for t in tags):
        return False
    return True


def _card(item):
    repo = item.get("id") or item.get("modelId") or ""
    tags = [str(t) for t in (item.get("tags") or [])]
    return {
        "repo_id": repo,
        "url": f"https://huggingface.co/{repo}",
        "downloads": int(item.get("downloads") or 0),            # за последние 30 дней
        "downloads_all": int(item.get("downloadsAllTime") or 0),  # за всё время
        "trending": int(item.get("trendingScore") or 0),          # тренд HF «сейчас»
        "likes": int(item.get("likes") or 0),
        "updated": (item.get("lastModified") or "")[:10],
        "created": (item.get("createdAt") or "")[:10],
        "format": detect_format(repo, tags),
        "bpw": detect_bpw(repo),
        "params_b": detect_params(repo),
        "gated": bool(item.get("gated")),
        "tags": [t for t in tags if not t.startswith(("license:", "region:", "arxiv:"))][:8],
    }


def search(query=None, period=DEFAULT_PERIOD, limit=40, fmt=None, token=None):
    """
    Без query — сводный список актуальных uncensored-моделей по всем ключевым словам.
    С query — обычный поиск по HF (строка идёт в HF как есть).
    period: ключ из PERIODS — all / month / week / day / new.
    fmt: None | 'exl3' | 'exl2' | 'gguf' | 'safetensors' — фильтр по формату весов.
    Возвращает dict: models + как именно посчитан период.
    """
    cfg = PERIODS.get(period) or PERIODS[DEFAULT_PERIOD]
    terms = [query] if query else UNCENSORED_TERMS
    per_term = max(10, limit // max(len(terms), 1) + 5)
    # «за всё время» и фильтр по формату сортируют/отсеивают на нашей стороне — берём выборку шире
    if cfg["key"] == "downloads_all" or fmt or cfg["fresh_days"]:
        per_term = min(100, per_term * 3)

    seen, out = set(), []
    for term in terms:
        for item in _search_raw(term, sort=cfg["hf_sort"], limit=per_term, token=token):
            card = _card(item)
            repo = card["repo_id"]
            if not repo or repo in seen:
                continue
            if not query and not _UNCENSORED_RE.search(repo):
                continue
            if not _is_llm(card):
                continue
            if fmt and card["format"] != fmt:
                continue
            seen.add(repo)
            card["match"] = term
            out.append(card)

    relaxed = False
    if cfg["fresh_days"]:
        cutoff = (datetime.date.today() - datetime.timedelta(days=cfg["fresh_days"])).isoformat()
        fresh = [c for c in out if c["updated"] >= cutoff]
        if fresh:
            out = fresh
        else:  # за такой короткий срок ничего не обновлялось — показываем тренд без ограничения
            relaxed = True

    out.sort(key=lambda c: (c[cfg["key"]], c["downloads"]), reverse=True)
    return {"models": out[:limit], "period": period if cfg is PERIODS.get(period) else DEFAULT_PERIOD,
            "metric": cfg["key"], "exact": cfg["exact"], "relaxed": relaxed,
            "fresh_days": cfg["fresh_days"]}


# ---------- топ моделей для кодинга ----------
# Отобранный вручную список: расцензуренные модели, которые реально годятся писать код,
# а не просто болтать. Порядок = наша рекомендация. Живые счётчики подтягиваются с HF,
# мёртвые/переименованные репозитории выпадают сами.
#
# Карточка = семейство, внутри — его модели. Если модель выложили и в safetensors (vLLM),
# и в GGUF (llama.cpp) — это варианты одной записи, а не два пункта списка: сначала
# выбирают модель, формат — уже под конкретный сервер.
#
# Исключение — группа «Лучшие модели» в самом верху: там оригиналы от лабораторий, с
# цензурой на месте. Они тут не как рабочий вариант для этого проекта, а как точка
# отсчёта: с них снимают отказы все сборки ниже, и по ним видно, сколько стоит потолок.
# Во всех остальных группах — только расцензуренное, даже если рядом есть популярнее.
TOP_CODING = [
    {
        "family": "Лучшие модели",
        "tier": "с цензурой",
        "why": "Оригиналы от самих лабораторий: качество потолочное, но отказы никто не "
               "снимал — модель откажет там же, где откажет облачный сервис. Берите, "
               "если расцензуривание не нужно, а нужны официальные веса и максимум "
               "качества. Нужно без цензуры — ищите ту же базу в группах ниже.",
        "models": [
            {
                "title": "Qwen3.8-Flash-Next",
                "tier": "эталон качества",
                "base": "Qwen3.8-Flash-Next (Qwen), MoE 125B/6B актив., база от 2026-08",
                "why": "Самая востребованная база в подборке (6.6k лайков, 4.2M скачиваний "
                       "за месяц) — из неё сделаны почти все свежие расцензуренные сборки. "
                       "6B активных параметров из 125B: отсюда и отклик, и цена. Контекст "
                       "262k нативно, до 1M с YaRN.\n"
                       "Формат решает всё. Официальные веса — BF16, 335 ГиБ: это 8 карт "
                       "по 48 ГБ под vLLM, потому что vLLM держит всех 512 экспертов в "
                       "VRAM (разреженность MoE экономит вычисления, а не память). "
                       "GGUF от unsloth — те же веса без абляции, но квантованные: "
                       "UD-IQ4_XS 87 ГиБ, UD-Q4_K_XL 104 ГиБ, и к ним применима выгрузка "
                       "экспертов в оперативку — на карте остаётся 5–6 ГиБ при 108–127 ГБ "
                       "RAM. Берите GGUF, если нет восьми больших карт.",
                "variants": ["Qwen/Qwen3.8-Flash-Next",
                             "unsloth/Qwen3.8-Flash-Next-GGUF"],
            },
        ],
    },
    {
        "family": "Qwen",
        "tier": "основной выбор",
        "why": "Главное семейство открытых кодеров: в нём и самая свежая база, и самые "
               "удобные размеры, и больше всего готовых расцензуренных сборок. "
               "Если нет особых причин — берите отсюда.",
        "models": [
            {
                "title": "Qwen3.8-Flash-Next abliterated",
                "tier": "рекомендуем",
                "base": "Qwen3.8-Flash-Next, MoE 125B/6B актив., база от 2026-08",
                "why": "Та же база, что в «Лучших моделях», но с вырезанными отказами — "
                       "и лучшее соотношение качества к цене во всей подборке. Причина в "
                       "MoE: 97.6% веса приходится на экспертов, они уезжают в "
                       "оперативку, и на карте остаётся 5–6 ГиБ. UD-IQ4_XS — 87 ГиБ "
                       "весов (108 ГБ RAM), UD-Q4_K_XL — 104 ГиБ (127 ГБ RAM). То есть "
                       "модель уровня 125B поднимается на самой дешёвой карте, если в "
                       "заказе много памяти. Расплата — генерацию считает CPU, так что "
                       "скорость упирается в каналы памяти, а не в GPU: берите хост на "
                       "EPYC или Xeon Scalable, подбор серверов это уже учитывает.",
                "variants": ["huihui-ai/Huihui-Qwen3.8-Flash-Next-abliterated-GGUF"],
            },
            {
                "title": "Qwen3.8-27B NEO-CODER-MAX",
                "tier": "всё в VRAM",
                "base": "Qwen3.8-27B, плотная 27B, база от 2026-08",
                "why": "Плотная модель, а значит целиком считается на карте: Q4_K_M — "
                       "16.8 ГиБ, Q6_K — 22 ГиБ, влезает в одну 24-гиговую. В отличие от "
                       "Flash-Next выше, скорость тут не зависит от оперативки — берите, "
                       "если важен ровный отклик, а не максимум качества. База свежая "
                       "(2026-08), сборка самая массовая из uncensored. Есть размышление "
                       "и вызовы инструментов. Бенчмарки в карточке автора — его "
                       "собственные замеры.",
                "variants": ["DavidAU/Qwen3.8-27B-TURBO-Fable-Cold-Fusion-735-882-"
                             "Heretic-Uncensored-NEO-CODER-MAX-MTP-GGUF"],
            },
            {
                "title": "Qwen3-Coder-Next",
                "tier": "потолок качества",
                "base": "Qwen3-Coder-Next, MoE 80B/3B актив., база от 2026-01",
                "why": "Профильный кодер от самой Qwen, выше всех на агентных задачах "
                       "(>70% SWE-bench Verified). Но Q4_K_M весит 45 ГиБ, а при нехватке "
                       "VRAM скорость падает в разы. Режима размышления нет.",
                "variants": ["gueraf/Huihui-Qwen3-Coder-Next-abliterated-fp8",
                             "bartowski/huihui-ai_Qwen3-Coder-Next-abliterated-GGUF",
                             "huihui-ai/Huihui-Qwen3-Coder-Next-abliterated"],
            },
            {
                "title": "Qwen3.8-27B Coder390 EfficientThink",
                "tier": "reasoning",
                "base": "Qwen3.8-27B, плотная 27B, база от 2026-08",
                "why": "Та же свежая база, дотюнена под размышление с экономным бюджетом "
                       "токенов — сильнее на алгоритмах и отладке чужого кода. GGUF берите "
                       "от mradermacher: у huihui в репозитории только IQ3, для кода мало.",
                "variants": ["alpha2beta/Huihui-Qwen3.8-27B-Coder390-EfficientThink-"
                             "abliterated-GPTQ-Int4-sym-G128-MTP-BF16",
                             "mradermacher/Huihui-Qwen3.8-27B-Coder390-"
                             "EfficientThink-abliterated-GGUF",
                             "huihui-ai/Huihui-Qwen3.8-27B-Coder390-EfficientThink-abliterated"],
            },
            {
                "title": "Qwen3-Coder-30B-A3B",
                "tier": "быстрая и дешёвая",
                "base": "Qwen3-Coder-30B-A3B, MoE 30B/3B актив., база от 2025-07",
                "why": "Самая старая база в списке, но 3B активных параметров дают отклик, "
                       "которого нет ни у кого выше. Q4_K_M — 17.3 ГиБ.",
                "variants": ["jeffrog/Qwen3-Coder-30B-A3B-abliterated-FP8",
                             "Thaurock/Qwen3-Coder-30B-A3B-Instruct-abliterated-GGUF",
                             "huihui-ai/Huihui-Qwen3-Coder-30B-A3B-Instruct-abliterated"],
            },
            {
                "title": "Qwen2.5-Coder-14B",
                "tier": "дешёвый вход",
                "base": "Qwen2.5-Coder-14B, плотная 14B, база от 2024-11",
                "why": "Q4_K_M — 8.4 ГиБ: влезает даже в 12-гиговую карту, а значит и в "
                       "самый дешёвый заказ. Поколение старое, но для правок и мелких "
                       "задач хватает с запасом.",
                "variants": ["bartowski/Qwen2.5-Coder-14B-Instruct-abliterated-GGUF"],
            },
            {
                "title": "Qwen2.5-Coder-32B",
                "tier": "классика",
                "base": "Qwen2.5-Coder-32B, плотная 32B, база от 2024-11",
                "why": "Прошлое поколение, но до сих пор одна из самых надёжных: меньше "
                       "срывов формата, много квантов, предсказуемое поведение.",
                "variants": ["bartowski/Qwen2.5-Coder-32B-Instruct-abliterated-GGUF"],
            },
        ],
    },
    {
        "family": "GLM",
        "tier": "универсал",
        "why": "Не кодер по профилю, но пишет код на уровне топовых и лучше держит "
               "длинный диалог — когда модель нужна и для кода, и для всего остального.",
        "models": [
            {
                "title": "GLM-5.3 Flash",
                "tier": "одна карта + много RAM",
                "base": "GLM-5.3 Flash (Zhipu), MoE 288 экспертов, контекст до 1M",
                "why": "Самая залайканная abliterated-сборка GLM (337). Кванты крупные — "
                       "UD-IQ4_XS 146 ГиБ, UD-Q4_K_XL 186, UD-Q5_K_XL 224 — но 98.1% "
                       "веса это эксперты, и с выгрузкой в оперативку на карте остаётся "
                       "4.9-6.5 ГиБ. То есть четыре карты по 48 ГБ не нужны: хватит "
                       "одной дешёвой и 177-269 ГБ RAM. Платите скоростью — генерацию "
                       "считает CPU.",
                "variants": ["huihui-ai/Huihui-GLM-5.3-Flash-abliterated-GGUF"],
            },
        ],
    },
    {
        "family": "Mistral / Devstral",
        "tier": "агентный",
        "why": "Альтернатива Qwen: плотные модели без MoE — ведут себя предсказуемее "
               "на длинных правках.",
        "models": [
            {
                "title": "Devstral Small 2 24B",
                "tier": "агентный",
                "base": "Devstral Small 2 (Mistral), плотная 24B",
                "why": "Сделана под агентов в коде: 65.8% SWE-bench Verified при 24B — "
                       "лучший результат на гигабайт в списке.",
                "variants": ["AliBilge/Huihui-Devstral-Small-2-24B-Instruct-2512-abliterated"],
            },
        ],
    },
    {
        "family": "DeepSeek",
        "tier": "самое дорогое",
        "why": "Качество топовое, но расцензуренные сборки гигантские — это самое "
               "дорогое семейство в подборке. Обе модели MoE, так что выгрузка "
               "экспертов работает и тут, только упор переезжает с карт на объём "
               "оперативки, а такие хосты редки. Считайте стоимость заказа заранее.",
        "models": [
            {
                "title": "DeepSeek V4.1 Flash",
                "tier": "предел возможного",
                "base": "DeepSeek V4.1 Flash 1017, MoE 384 эксперта, база от 2026-10",
                "why": "Самая свежая и сильная, но и самая тяжёлая: Q4_K_M — 397 ГиБ, "
                       "FP8 — 475 ГиБ, то есть 8 карт по 80 ГБ. Альтернатива — выгрузка "
                       "экспертов: 18.3 ГиБ VRAM, зато 468 ГБ RAM, и такой хост ещё надо "
                       "найти. Квантов меньше Q4 полно, но для кода они бесполезны — "
                       "IQ1/IQ2 тут не вариант.",
                "variants": ["Adolphsson/DeepSeek-V4.1-Flash-UNCENSORED-GGUF",
                             "dealignai/DeepSeek-V4.1-Flash-1017-UNCENSORED-FP8"],
            },
            {
                "title": "DeepSeek V4 Flash 0731",
                "tier": "подъёмный DeepSeek",
                "base": "DeepSeek V4 Flash, MoE 256 экспертов, база от 2026-07",
                "why": "Прошлая версия, зато влезает в разумный заказ: Q4_K — 153 ГиБ, "
                       "4 карты по 48 ГБ, либо 14.4 ГиБ VRAM и 181 ГБ RAM с выгрузкой "
                       "экспертов. Если нужен именно DeepSeek, а не рекорд по "
                       "качеству — брать стоит эту.",
                "variants": ["huihui-ai/Huihui-DeepSeek-V4-Flash-0731-abliterated-GGUF",
                             "apetersson/DeepSeek-V4-Flash-0731-Abliterated-FP8"],
            },
        ],
    },
]


# Отдельная подборка: тут не «кодеры, с которых сняли отказы», а модели, где цензуру
# сносили агрессивнее всего — дообучением или хирургией по весам, с публичными замерами
# доли отказов. Код они тоже пишут, но выбирают их не за это.
TOP_UNCENSORED = [
    {
        "family": "Хирургия по весам",
        "tier": "максимум расцензуривания",
        "why": "Отказы вырезают прямо из весов, без дообучения. Показатели по отказам "
               "тут самые высокие из всего, что есть на Hugging Face, и сборки выходят "
               "на свежих базах быстрее, чем успевают дообучить. Расплата — модель "
               "неизбежно немного глупеет и может срываться в повторы: если поймали "
               "зацикливание, поднимите квант или возьмите модель из «Топ для кодинга».",
        "models": [
            {
                "title": "Qwen3.8-27B UNCENSORED (dealign)",
                "tier": "рекомендуем",
                "base": "Qwen3.8-27B, плотная 27B, база от 2026-08",
                "why": "Автор заявляет 99.7% на HarmBench — самый высокий показатель "
                       "из найденного. База та же, что у кодеров Qwen, Q4-квант "
                       "15.8 ГиБ — влезает в одну 24-гиговую карту.",
                "variants": ["dealignai/Qwen3.8-27B-UNCENSORED-GGUF"],
            },
            {
                "title": "Qwen3.8-Flash-Next UNCENSORED (orcarouter)",
                "tier": "лучший баланс",
                "base": "Qwen3.8-Flash-Next, MoE 125B/6B актив., база от 2026-08",
                "why": "Самая залайканная расцензуренная сборка этой базы — 1.2k лайков, "
                       "втрое больше ближайшей. Единственная с полной линейкой квантов: "
                       "IQ4_XS 91 ГиБ, Q4_K_M 111, Q5_K_M 125, Q6_K 156, Q8_0 175. "
                       "97.6% веса — эксперты, поэтому с выгрузкой в оперативку хватает "
                       "5-6 ГиБ VRAM при 112-211 ГБ RAM: модель уровня 125B поднимается "
                       "на одной дешёвой карте. Замерено на RTX 5090 с 39 слоями в "
                       "оперативке — 37 ток/с на генерации.",
                "variants": ["orcarouter/Qwen3.8-Flash-Next-Uncensored-GGUF"],
            },
            {
                "title": "GLM-5.3 Flash UNCENSORED",
                "tier": "тяжёлая, но подъёмная",
                "base": "GLM-5.3 Flash (Zhipu), MoE 288 экспертов",
                "why": "Максимум качества без отказов. Формат решает цену сервера. "
                       "FP8 идёт на vLLM и быстрее, но весит 306 ГиБ — это 8 карт по "
                       "48 ГБ. GGUF от orcarouter: Q4_K_M 180 ГиБ, Q6_K 245, Q8_0 318, "
                       "и у него 98.1% веса приходится на экспертов — с выгрузкой в "
                       "оперативку на карте остаётся 5.6-8.3 ГиБ, зато нужно 217-379 ГБ "
                       "RAM. Контекст до 1M. Если RAM на хосте много, GGUF выходит в "
                       "разы дешевле FP8 — но генерацию считает CPU, берите хост "
                       "на EPYC или Xeon Scalable.",
                "variants": ["dealignai/GLM-5.3-Flash-UNCENSORED-FP8",
                             "orcarouter/GLM-5.3-Flash-Uncensored-GGUF"],
            },
        ],
    },
]

# Какой список отдавать UI: ключ приходит из вкладки на странице модели.
TOP_LISTS = {"top": TOP_CODING, "uncensored": TOP_UNCENSORED}
DEFAULT_TOP = "uncensored"


def _meta_card(repo_id, token=None):
    """Живая карточка одного репозитория — тот же формат, что у поиска."""
    q = urllib.parse.urlencode([
        ("expand[]", "lastModified"), ("expand[]", "downloads"),
        ("expand[]", "downloadsAllTime"), ("expand[]", "trendingScore"),
        ("expand[]", "createdAt"), ("expand[]", "likes"),
        ("expand[]", "tags"), ("expand[]", "gated"),
    ])
    item = _get(f"models/{urllib.parse.quote(repo_id)}?{q}", token)
    return _card(item)


def top_coding(fmt=None, token=None, limit=None, kind=DEFAULT_TOP):
    """
    Отобранный топ uncensored-моделей для кодинга с актуальными счётчиками HF.
    Карточка = семейство (Qwen, GLM, DeepSeek…), внутри — его модели, у каждой
    варианты весов (safetensors и/или GGUF).
    fmt — тот же фильтр по формату, что и в поиске: отсекает варианты; модели и
    семейства, у которых не осталось ни одного, выпадают целиком.
    """
    families = TOP_LISTS.get(kind) or TOP_LISTS[DEFAULT_TOP]
    repos = {r for fam in families for m in fam["models"] for r in m["variants"]}
    cards = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        futures = {pool.submit(_meta_card, r, token): r for r in repos}
        for fut in concurrent.futures.as_completed(futures):
            try:
                cards[futures[fut]] = fut.result()
            except RuntimeError:
                pass  # репозиторий удалён/переименован/gated — молча пропускаем

    out = []
    for fam in families:
        models = []
        for entry in fam["models"]:
            variants = [cards[r] for r in entry["variants"] if r in cards]
            if fmt:
                variants = [v for v in variants if v["format"] == fmt]
            if not variants:
                continue
            # счётчики показываем по самому популярному варианту — он же идёт первым
            variants.sort(key=lambda v: v["downloads_all"], reverse=True)
            head = variants[0]
            models.append({
                "title": entry["title"], "tier": entry["tier"], "why": entry["why"],
                "base": entry.get("base", ""), "variants": variants,
                "repo_id": head["repo_id"], "url": head["url"],
                "downloads": head["downloads"], "downloads_all": head["downloads_all"],
                "likes": max(v["likes"] for v in variants),
                "updated": max(v["updated"] for v in variants),
                "params_b": next((v["params_b"] for v in variants if v["params_b"]), None),
                "trending": head["trending"], "format": head["format"],
                "bpw": head["bpw"], "gated": any(v["gated"] for v in variants),
            })
        if not models:
            continue
        out.append({"family": fam["family"], "tier": fam["tier"], "why": fam["why"],
                    "rank": len(out) + 1, "models": models})
        if limit and len(out) >= limit:
            break
    return {"families": out, "period": "top", "metric": "curated",
            "exact": True, "relaxed": False, "fresh_days": None}


# ---------- разбор формата ----------
def detect_format(repo_id, tags=(), files=()):
    """
    Формат весов. Если список файлов известен — верим ему, а не тегам: HF вешает тег
    «GGUF» и на репозитории, где лежат только safetensors (например, когда автор
    выложил GGUF отдельно). Без такой проверки модель уезжала бы на llama.cpp,
    который потом не нашёл бы ни одного .gguf.
    """
    blob = " ".join([repo_id.lower(), " ".join(t.lower() for t in tags)])
    names = [f.lower() for f in files]
    if any(n.endswith(".gguf") for n in names):
        return "gguf"
    if not names and "gguf" in blob:  # дерево файлов недоступно — остаётся верить тегу
        return "gguf"
    if "exl3" in blob or "exllamav3" in blob:
        return "exl3"
    if "exl2" in blob or "exllamav2" in blob:
        return "exl2"
    return "safetensors"


def engine_for(fmt):
    return {"exl3": ENGINE_TABBY, "exl2": ENGINE_TABBY,
            "gguf": ENGINE_LLAMACPP}.get(fmt, ENGINE_VLLM)


def detect_bpw(repo_id):
    m = re.search(r"(\d+(?:[._]\d+)?)\s*bpw", repo_id, re.I)
    if m:
        return float(m.group(1).replace("_", "."))
    m = re.search(r"\b(?:Q|IQ)(\d)(?:_[KSML0-9]+)?\b", repo_id, re.I)
    return float(m.group(1)) + 0.5 if m else None


def detect_params(repo_id):
    m = re.search(r"\b(\d+(?:\.\d+)?)\s*[bB]\b", repo_id.replace("-", " ").replace("_", " "))
    return float(m.group(1)) if m else None


# Какой парсер вызовов инструментов включать в vLLM. Форматы tool-call у семейств
# разные, и универсального нет: с чужим парсером модель «вызывает» инструменты,
# а OpenCode их не видит. hermes — безопасный запасной вариант.
# Шаблоны намеренно узкие: «Qwen3.8-27B-…-CODER-MAX» — это дотюн обычной Qwen3.8,
# а не линейка Qwen3-Coder, и формат вызовов у неё другой. Ошибиться здесь хуже,
# чем не угадать: с неверным парсером вызовы инструментов молча перестают работать.
_TOOL_PARSERS = [
    (r"qwen3[-_.]?coder", "qwen3_coder"),
    (r"devstral|magistral|codestral|mistral", "mistral"),
    (r"deepseek[-_.]?v[34]", "deepseek_v3"),
    (r"glm[-_.]?[45]", "glm45"),
    (r"kimi", "kimi_k2"),
    (r"llama[-_.]?3", "llama3_json"),
]
DEFAULT_TOOL_PARSER = "hermes"


def tool_parser_for(repo_id):
    low = (repo_id or "").lower()
    for pat, parser in _TOOL_PARSERS:
        if re.search(pat, low):
            return parser
    return DEFAULT_TOOL_PARSER


def weight_precision(repo_id):
    """
    В каком низкоразрядном формате лежат веса — нужно, чтобы понять, потянет ли карта.
    Отличается от detect_quant_arg: там значение для флага vLLM, здесь — для проверки железа.
    """
    low = (repo_id or "").lower()
    for key in ("nvfp4", "mxfp4", "mxfp8", "fp8", "awq", "gptq"):
        if key in low:
            return key
    return None


def detect_quant_arg(cfg):
    """
    Значение для --quantization у vLLM. Берём строго из config.json: quant_method — это
    ровно то имя, которое понимает vLLM. По названию репозитория определять нельзя:
    у MXFP8-сборок в имени есть «fp8», а в конфиге лежит формат другого рантайма, и vLLM
    отказывается стартовать с «Quantization method specified in the model config (None)
    does not match the quantization method specified in the `quantization` argument».
    Нет quant_method — не передаём флаг вовсе, vLLM определит формат сам.
    """
    method = ((cfg or {}).get("quantization_config") or {}).get("quant_method")
    return str(method).lower() if method else None


# ---------- детальная информация и оценка железа ----------
def _tree(repo_id, token=None):
    try:
        return _get(f"models/{urllib.parse.quote(repo_id)}/tree/main?recursive=true", token)
    except RuntimeError:
        return []


def _weight_bytes(entries):
    exts = (".safetensors", ".gguf", ".bin", ".pt")
    total = 0
    for e in entries:
        if e.get("type") != "file":
            continue
        path = str(e.get("path") or "")
        if not path.lower().endswith(exts):
            continue
        size = (e.get("lfs") or {}).get("size") or e.get("size") or 0
        total += int(size)
    return total


def _raw_config(repo_id, token=None):
    """
    config.json репозитория. HF в `models/<id>` отдаёт его урезанным — без числа слоёв
    и голов, а они нужны для KV-кэша, поэтому тянем файл напрямую.
    """
    url = f"https://huggingface.co/{repo_id}/resolve/main/config.json"
    headers = {"User-Agent": "UncensoredAI/1.0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=20) as r:
            cfg = json.loads(r.read().decode())
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return {}
    # у свежих мультимодальных архитектур параметры текстовой части лежат отдельно
    if not cfg.get("num_hidden_layers") and isinstance(cfg.get("text_config"), dict):
        merged = dict(cfg)
        merged.update(cfg["text_config"])
        return merged
    return cfg


def _base_repo(meta):
    """Исходная модель, из которой собран квант — у GGUF-репозиториев конфига своего нет."""
    base = (meta.get("cardData") or {}).get("base_model")
    if isinstance(base, list):
        base = base[0] if base else None
    return base if isinstance(base, str) and base.count("/") == 1 else None


# Байт на элемент KV-кэша. llama.cpp мы запускаем с q8_0-кэшем, TabbyAPI — с Q8;
# vLLM оставляем в fp16, там квантование кэша упирается в поддержку конкретных карт.
KV_BYTES = {ENGINE_LLAMACPP: 1, ENGINE_VLLM: 2, ENGINE_TABBY: 1}


def kv_cache_gib(cfg, ctx, engine):
    """
    Размер KV-кэша под заданный контекст. Наивная формула «слои × головы × head_dim»
    годится только для классического внимания; современные архитектуры ломают её сразу
    в двух местах, и без этих поправок оценка завышается в разы:

    * MLA (kv_lora_rank) — в кэш едет сжатое латентное представление, а не полные K и V;
    * гибридные модели — часть слоёв линейные, у них состояние постоянное и от ctx не растёт.

    None — конфиг не дал нужных полей, выше берётся грубая оценка.
    """
    layers = cfg.get("num_hidden_layers") or cfg.get("n_layer")
    if not layers:
        return None
    types = cfg.get("layer_types")
    if isinstance(types, list) and types:
        attn_layers = sum(1 for t in types if "linear" not in str(t).lower())
    else:
        attn_layers = layers
    nbytes = KV_BYTES.get(engine, 2)

    lora = cfg.get("kv_lora_rank")
    if lora:
        per_token = int(lora) + int(cfg.get("qk_rope_head_dim") or 0)
    else:
        heads = cfg.get("num_attention_heads") or cfg.get("n_head")
        kv_heads = cfg.get("num_key_value_heads") or heads
        hidden = cfg.get("hidden_size") or cfg.get("n_embd")
        head_dim = cfg.get("head_dim") or (int(hidden / heads) if hidden and heads else None)
        if not (kv_heads and head_dim):
            return None
        per_token = 2 * int(kv_heads) * int(head_dim)   # K и V
    return round(attn_layers * per_token * ctx * nbytes / GIB, 1)


def model_max_ctx(cfg):
    """
    Нативный контекст модели — из её конфига.

    Раньше тут был отдельный запрос к HF API за полем
    `config.tokenizer_config.max_position_embeddings`, и он ВСЕГДА возвращал None:
    в блоке `config` Hugging Face отдаёт только `architectures`, `model_type`,
    `chat_template_jinja` и `tokenizer_config` с именами спецтокенов. Ни
    `max_position_embeddings`, ни `model_max_length` там нет ни у одного репозитория.
    Из-за этого каждый деплой молча уезжал на DEFAULT_CTX: llama.cpp получал
    `-c 32768` при нативных 262144 у Qwen3.8, и контекст приходилось поднимать руками.

    Конфиг сюда приходит уже разобранный (_raw_config разворачивает text_config,
    _config_with_base достаёт его у предка по base_model) — у GGUF-сборок своего нет.
    """
    for key in ("max_position_embeddings", "model_max_length", "n_positions"):
        val = cfg.get(key)
        if val:
            return int(val)
    return None


# Шард вида «-00001-of-00005.gguf» — часть одного кванта, а не отдельный файл.
_SHARD_RE = re.compile(r"-0*\d+-of-0*\d+\.gguf$", re.I)
# Служебные файлы в GGUF-репозиториях: проектор для картинок, матрицы важности,
# LoRA и «патчи» к чужим квантам — самостоятельной моделью ничего из этого не является.
# Отдельно: `mtp-` ИМЕННО В НАЧАЛЕ имени — это голова для спекулятивного декодирования,
# а не модель. В середине названия «-MTP-» встречается у полноценных сборок (DavidAU),
# поэтому блокировать подстроку целиком нельзя. Туда же вектор направления отказа:
# это несколько мегабайт служебных данных, которые проходили как «рабочий квант».
#
# Слово «draft» в имени — всегда черновая модель для спекулятивного декодирования, где
# бы оно ни стояло. Арифметика из _drop_drafts такие файлы не ловит: она считает число
# параметров как размер/bpw, а у «Qwen3.8-Flash-Next-Uncensored-MTP-draft.gguf» нет
# обозначения кванта вовсе, поэтому bpw=None и запись не попадает в сравнение. В итоге
# 3.9 ГиБ предлагались как рабочий квант рядом со 175-гигабайтным Q8_0: выбрав такой,
# пользователь поднял бы черновую модель вместо настоящей — без единой ошибки, просто
# с бессмысленными ответами.
_AUX_GGUF_RE = re.compile(
    r"(^|/)(mmproj|mtp[-_]|.*imatrix|.*[-_]lora|.*refusal[-_]direction)"
    r"|[-_]draft\b|[-_]patch\.gguf$", re.I)
# Предпочтительный квант для кода: ниже Q4_K_M заметно сыпется генерация.
# Имена в репозиториях разнородные (Q4_K_M, Q4_K-0731, q4km), поэтому ищем регулярками.
_QUANT_RANK = [r"q4[._-]?k[._-]?m", r"q4[._-]?k", r"q5[._-]?k[._-]?m", r"q5[._-]?k",
               r"iq4", r"q6[._-]?k", r"q4", r"q8[._-]?0", r"bf16|f16"]

# Примерная «ширина» кванта в битах на вес — по ней отсекаем мусорные варианты.
_QUANT_BPW = [
    (r"iq1\w*", 1.8), (r"iq2\w*", 2.5), (r"q2[._-]?k\w*", 2.6), (r"q2", 2.6),
    (r"iq3\w*", 3.3), (r"q3[._-]?k\w*", 3.5), (r"q3", 3.5),
    (r"mxfp4", 4.25), (r"iq4[._-]?xs", 4.3), (r"iq4[._-]?nl", 4.5), (r"iq4", 4.3),
    (r"q4[._-]?0", 4.5), (r"q4[._-]?1", 5.0),
    (r"q4[._-]?k[._-]?s", 4.6), (r"q4[._-]?k[._-]?m", 4.85), (r"q4[._-]?k[._-]?l", 5.1),
    (r"q4[._-]?k", 4.85), (r"q4", 4.85),
    (r"q5[._-]?k[._-]?s", 5.5), (r"q5[._-]?k[._-]?m", 5.7), (r"q5[._-]?k[._-]?l", 5.9),
    (r"q5[._-]?k", 5.7), (r"q5", 5.6),
    (r"q6[._-]?k\w*", 6.6), (r"q6", 6.6), (r"q8[._-]?0", 8.5), (r"q8", 8.5),
    (r"fp8", 8.0), (r"bf16", 16.0), (r"fp16", 16.0), (r"f16", 16.0), (r"f32|fp32", 32.0),
]
# Порог = Q4_K_M. Всё, что ниже, для кода не годится и в список не попадает.
MIN_CODE_BPW = 4.85


def _quant_bpw(label):
    """
    Оценка бит на вес по имени файла. Из нескольких совпадений берём самое
    длинное: «q4_k_m» описывает файл точнее, чем «q4». None — не распознали.
    """
    low = label.lower()
    best, best_key = None, (-1, -1)
    for pat, bpw in _QUANT_BPW:
        m = None
        for m in re.finditer(pat, low):     # нас интересует самое правое совпадение
            pass
        if not m:
            continue
        # Имя кванта стоит в конце: «…-qat-q4_0-unquantized-abliterated-Q4_K.gguf» — это
        # Q4_K, а не q4_0. Поэтому сначала сравниваем позицию, и только потом длину.
        key = (m.start(), len(m.group(0)))
        if key > best_key:
            best, best_key = bpw, key
    return best


# ---------- MoE и выгрузка экспертов в оперативку ----------
# У MoE-модели почти весь вес — это эксперты, а на каждый токен работает лишь малая их
# часть. Поэтому их можно держать в обычной оперативке и считать на CPU: llama.cpp умеет
# это флагом --n-cpu-moe. Платим процентами скорости, а экономим в разы на VRAM —
# модель, под которую нужно четыре карты, влезает в одну плюс много RAM.
_EXPERT_KEYS = ("num_experts", "num_local_experts", "n_routed_experts", "moe_num_experts")
_TOPK_KEYS = ("num_experts_per_tok", "num_experts_per_token", "moe_topk", "top_k")
_SHARED_KEYS = ("n_shared_experts", "num_shared_experts", "moe_num_shared_experts")


def _first(cfg, keys, default=0):
    for k in keys:
        v = cfg.get(k)
        if v:
            return int(v)
    return default


def _moe_layers(cfg, layers):
    """Сколько слоёв реально содержат экспертов: часть архитектур чередует их с плотными."""
    dense_head = int(cfg.get("first_k_dense_replace") or 0)      # DeepSeek: первые слои плотные
    n = max(layers - dense_head, 0)
    only = cfg.get("mlp_only_layers")
    if isinstance(only, list):
        n = max(n - len(only), 0)
    step = int(cfg.get("decoder_sparse_step") or 1)              # Qwen: MoE через каждые step
    if step > 1:
        n = n // step
    return n


def moe_profile(cfg):
    """
    Доля веса, которую занимают маршрутизируемые эксперты, и сколько слоёв их содержат.
    Считаем по конфигу в параметрах, а потом переносим долю на реальный размер файла —
    так оценка не зависит от того, в каком кванте скачан репозиторий.
    None — модель не MoE либо конфига не хватает.
    """
    experts = _first(cfg, _EXPERT_KEYS)
    layers = int(cfg.get("num_hidden_layers") or cfg.get("n_layer") or 0)
    hidden = int(cfg.get("hidden_size") or cfg.get("n_embd") or 0)
    if experts < 2 or not layers or not hidden:
        return None
    moe_inter = int(cfg.get("moe_intermediate_size") or cfg.get("intermediate_size") or 0)
    dense_inter = int(cfg.get("intermediate_size") or moe_inter or 0)
    if not moe_inter:
        return None
    moe_layers = _moe_layers(cfg, layers) or layers
    dense_layers = max(layers - moe_layers, 0)
    shared = _first(cfg, _SHARED_KEYS)

    # SwiGLU: три матрицы на эксперта (gate, up, down)
    per_expert = 3 * hidden * moe_inter
    routed = moe_layers * experts * per_expert          # это и уедет в оперативку
    rest = moe_layers * shared * per_expert + dense_layers * 3 * hidden * dense_inter

    heads = int(cfg.get("num_attention_heads") or cfg.get("n_head") or 0)
    kv_heads = int(cfg.get("num_key_value_heads") or heads or 0)
    head_dim = int(cfg.get("head_dim") or (hidden // heads if heads else 0))
    if heads and head_dim:
        attn = layers * (2 * hidden * heads * head_dim + 2 * hidden * kv_heads * head_dim)
    else:
        attn = layers * 4 * hidden * hidden              # грубо, если конфиг нестандартный
    vocab = int(cfg.get("vocab_size") or 0)
    emb = vocab * hidden * (1 if cfg.get("tie_word_embeddings") else 2)

    total = routed + rest + attn + emb
    if total <= 0:
        return None
    return {
        "experts": experts,
        "active": _first(cfg, _TOPK_KEYS),
        "moe_layers": moe_layers,
        "expert_share": round(routed / total, 3),        # какая доля весов — эксперты
    }


def cpu_moe_estimate(moe, weights_gib, kv_gib, vocab=0):
    """
    Во что обходится полная выгрузка экспертов: сколько останется на карте и сколько
    понадобится оперативки. Отдельно — вес экспертов одного слоя: по нему считается,
    сколько слоёв выгружать, если карта чуть-чуть не вмещает модель целиком.
    """
    if not moe or not weights_gib:
        return None
    expert_gib = weights_gib * moe["expert_share"]
    on_gpu = max(weights_gib - expert_gib, 0.2)
    return {
        # буфер логитов остаётся на карте даже когда все эксперты уехали в RAM,
        # поэтому vocab передаём и здесь — иначе оценка занижена вдвое
        "vram_gib": vram_estimate(round(on_gpu, 1), kv_gib, vocab),
        # оперативка: сами эксперты + запас на загрузчик и страничный кэш
        "ram_gb": int(expert_gib * 1.2 + 6),
        "per_layer_gib": round(expert_gib / max(moe["moe_layers"], 1), 3),
        "max_layers": moe["moe_layers"],
    }


def cpu_moe_layers(moe_info, need_vram_gib, have_vram_gib, card_gib=None):
    """
    Сколько слоёв экспертов отправить в оперативку, чтобы модель влезла в карту.
    Выгружаем минимум необходимого: каждый выгруженный слой — это работа для CPU.

    card_gib — объём ОДНОЙ карты. Он важнее суммарного: llama.cpp режет модель по слоям,
    и все слои, чьи эксперты остались на GPU, оказываются подряд — то есть целиком на
    одной карте. Проверено на живом сервере: 2x8 ГиБ, двенадцать слоёв на GPU, попытка
    выделить 12.2 ГиБ на одном устройстве и падение «unable to allocate CUDA1 buffer».
    """
    if not moe_info:
        return 0
    per = moe_info["per_layer_gib"]
    total = moe_info["max_layers"]
    if per <= 0:
        return total if need_vram_gib > have_vram_gib else 0

    by_total = 0
    if need_vram_gib > have_vram_gib:
        # небольшой запас: оценка VRAM не идеальна, а падение по OOM стоит часа ожидания
        by_total = min(total, int((need_vram_gib - have_vram_gib + 1.0) / per) + 1)

    by_card = 0
    if card_gib:
        # на той же карте живут внимание и KV-кэш, поэтому экспертам отдаём не всё
        fits = int(max(card_gib * 0.8, 0) / per)
        by_card = max(0, total - fits)

    return max(0, min(total, max(by_total, by_card)))


# Накладные расходы движка поверх весов и KV-кэша.
# UBATCH — физический батч llama.cpp по умолчанию. Буфер логитов выделяется под него и
# считается как vocab x ubatch x 4 байта: у Qwen3.8 словарь 248 320 токенов, и это 0.47
# ГиБ одним куском — больше, чем вся прежняя константа накладных расходов.
UBATCH = 512
ENGINE_BASE_GIB = 1.0      # CUDA-контекст и графовые буферы
FALLBACK_LOGITS_GIB = 0.3  # когда размер словаря неизвестен


def vram_estimate(weights_gib, kv_gib, vocab=0):
    """
    Сколько VRAM нужно под веса и кэш.

    Прежняя формула (1.5 + веса x 0.02, не больше 8) давала 1.8-1.9 ГиБ накладных для
    моделей на 16-22 ГиБ и этого не хватало: в ней не было ни CUDA-контекста, ни буфера
    логитов, который у моделей с большим словарём сам по себе занимает полгигабайта.
    Модель на 20 ГиБ проходила на карту с ровно 20 ГиБ и падала по OOM.

    Это оценка «сколько займёт», а не «какую карту брать»: запас на то, что оценка
    неточна, добавляет VRAM_HEADROOM при подборе сервера.
    """
    logits = vocab * UBATCH * 4 / GIB if vocab else FALLBACK_LOGITS_GIB
    overhead = min(12.0, ENGINE_BASE_GIB + logits + weights_gib * 0.05)
    return round(weights_gib + kv_gib + overhead, 1)


CTX_FLOOR = 4096        # ниже опускаться незачем: на таком контексте кодить нельзя


def ctx_for_vram(ctx, kv_gib, weights_gib, have_vram_gib, vocab=0):
    """
    Наибольший контекст не выше запрошенного, при котором модель влезает в have_vram_gib.

    Нужен потому, что контекст задают до выбора сервера. Поставил 262144, взял карту на
    24 ГиБ — и деплой падал по OOM через полчаса после начала скачивания весов, без
    единого предупреждения. KV-кэш линеен по контексту (см. kv_cache_gib), поэтому
    пересчитываем его пропорцией от уже известной оценки, не читая конфиг заново.
    Шаг — деление вдвое: так контекст остаётся степенью двойки, как его и ждут движки.
    """
    if ctx <= 0 or not weights_gib or not have_vram_gib:
        return ctx
    per_token = kv_gib / ctx
    out = ctx
    while out > CTX_FLOOR and vram_estimate(weights_gib, per_token * out, vocab) > have_vram_gib:
        out //= 2
    return out


def _gguf_options(entries, ctx, kv_gib, moe=None, vocab=0):
    """
    Группирует GGUF-файлы по квантам (шарды одного кванта — в одну запись)
    и считает для каждого размер и оценку VRAM/диска. Служебные файлы отбрасываются.
    moe — профиль модели из moe_profile(): с ним у каждого кванта появляется вторая
    оценка, «сколько нужно, если эксперты уедут в оперативку».
    """
    groups = {}
    for e in entries:
        if e.get("type") != "file":
            continue
        path = str(e.get("path") or "")
        if not path.lower().endswith(".gguf") or _AUX_GGUF_RE.search(path):
            continue
        key = _SHARD_RE.sub(".gguf", path)
        size = int((e.get("lfs") or {}).get("size") or e.get("size") or 0)
        g = groups.setdefault(key, {"path": path, "bytes": 0, "parts": 0})
        g["bytes"] += size
        g["parts"] += 1
        # для шардированного кванта в деплой уходит первая часть — по ней строится маска
        if path < g["path"]:
            g["path"] = path

    out = []
    for key, g in groups.items():
        weights = round(g["bytes"] / GIB, 1)
        # имя без .gguf; подпапку оставляем — в репозиториях бывают одноимённые файлы
        name = key[:-5] if key.lower().endswith(".gguf") else key
        opt = {
            "path": g["path"], "label": name, "parts": g["parts"],
            "weights_gib": weights, "bpw": _quant_bpw(name),
            "vram_gib": vram_estimate(weights, kv_gib, vocab),
            "disk_gb": int(weights * 1.15 + 20),
        }
        off = cpu_moe_estimate(moe, weights, kv_gib, vocab)
        if off:
            opt["vram_cpu_moe_gib"] = off["vram_gib"]
            opt["ram_gb"] = off["ram_gb"]
        out.append(opt)
    out.sort(key=lambda o: o["weights_gib"])
    return out


def _drop_drafts(options):
    """
    Выбрасывает файлы, которые при своём кванте слишком малы, чтобы быть этой моделью:
    размер и bpw дают оценку числа параметров, и draft-модели для спекулятивного
    декодирования (а также огрызки репозитория) оказываются в разы меньше основных весов.
    Благодаря этому не нужно знать имена вроде «dspark» — считается арифметика.
    """
    sized = [(o, o["weights_gib"] * 8 / o["bpw"]) for o in options if o["bpw"]]
    if len(sized) < 2:
        return options
    biggest = max(params for _, params in sized)
    small = {id(o) for o, params in sized if params < biggest * 0.5}
    return [o for o in options if id(o) not in small]


# GSQ и RCO — это обученные квантования от ISTA-DASLab, а не способ расцензуривания
# (легко спутать по названию). Их IQ2/IQ3 несопоставимы с обычными: при 2.5 bpw KL к
# оригиналу 0.193 и top-1 88%, при 3.5 bpw — 0.075 и 93%. Отсекать их по общему порогу
# Q4_K_M неправильно: для этих сборок низкий бит не означает негодность.
_TRAINED_QUANT_RE = re.compile(r"gsq|[-_]rco[-_]|rco[-_]gguf", re.I)
# Но и у обученных квантов есть предел: по замерам ISTA-DASLab 2.5 bpw даёт top-1 88% —
# для кода это уже ощутимо, а 3.5 bpw держит 93%. Пропускаем IQ3 и выше, IQ2 отсекаем.
TRAINED_MIN_BPW = 3.3


def _usable_quants(options):
    """
    Оставляет только кванты не ниже Q4_K_M — ниже код начинает сыпаться, и держать
    такие варианты в списке незачем. Нераспознанные имена оставляем: лучше показать
    непонятное, чем молча спрятать единственный рабочий файл.
    Возвращает (список, всё_ли_отфильтровано): если годных нет вообще, отдаём
    исходный список — иначе модель станет невозможно развернуть.
    """
    good = [o for o in options
            if o["bpw"] is None or o["bpw"] >= MIN_CODE_BPW
            or (_TRAINED_QUANT_RE.search(o["label"]) and o["bpw"] >= TRAINED_MIN_BPW)]
    if good:
        return _drop_drafts(good), False
    return _drop_drafts(options), True


def _pick_gguf(options):
    """
    Квант по умолчанию: лучший из пригодных для кода, иначе самый крупный.
    Среди одинаково подходящих берём самый большой — маленькие файлы с тем же
    именем кванта обычно draft-модели для спекулятивного декодирования.
    """
    for want in _QUANT_RANK:
        hits = [o for o in options if re.search(want, o["label"], re.I)]
        if hits:
            return max(hits, key=lambda o: o["weights_gib"])
    return options[-1] if options else None


# Сколько шагов по цепочке base_model готовы пройти. Одного не хватает: у квантователей
# принято ссылаться на другой GGUF-репозиторий, а тот уже на исходную модель. Пока шагов
# было один, у таких сборок не считались ни KV-кэш, ни доля экспертов — например,
# SC117 -> ISTA-DASLab -> Qwen/Qwen3.8-Flash-Next.
_BASE_HOPS = 3


def _config_with_base(repo_id, meta, own_cfg, token=None):
    """Конфиг с полями архитектуры: свой, а если его нет — у предка по base_model."""
    if own_cfg.get("num_hidden_layers"):
        return own_cfg
    seen, card = {repo_id}, meta
    for _ in range(_BASE_HOPS):
        base = _base_repo(card)
        if not base or base in seen:
            break
        seen.add(base)
        cfg = _raw_config(base, token)
        if cfg.get("num_hidden_layers"):
            return cfg
        try:
            card = _get(f"models/{base}", token)
        except RuntimeError:
            break
    return own_cfg


def model_info(ref, ctx=None, token=None):
    """Полная карточка модели: формат, движок, размер весов, оценка нужных VRAM и диска."""
    repo_id = normalize_ref(ref)
    meta = _get(f"models/{urllib.parse.quote(repo_id)}", token)
    tags = [str(t) for t in (meta.get("tags") or [])]
    entries = _tree(repo_id, token)
    files = [str(e.get("path")) for e in entries if e.get("type") == "file"]

    fmt = detect_format(repo_id, tags, files)
    engine = engine_for(fmt)
    weights = _weight_bytes(entries)

    # Конфиг читаем до контекста: из него берётся и нативный максимум контекста, и
    # размер словаря для буфера логитов. У GGUF-репозитория своего конфига нет,
    # поэтому берём его у исходной модели, указанной в карточке как base_model
    own_cfg = _raw_config(repo_id, token)   # собственный конфиг: из него берём квантование
    cfg = _config_with_base(repo_id, meta, own_cfg, token)
    ctx_max = model_max_ctx(cfg)
    ctx = int(ctx) if ctx else min(ctx_max or DEFAULT_CTX, CTX_CAP)
    vocab = int(cfg.get("vocab_size") or 0)

    kv_gib = kv_cache_gib(cfg, ctx, engine)
    kv_known = kv_gib is not None

    # GGUF-репозиторий — это набор квантов; рабочий вес задаёт выбранный, а не весь репозиторий
    moe = moe_profile(cfg)
    all_options = (_gguf_options(entries, ctx, kv_gib if kv_known else 0.0, moe, vocab)
                   if fmt == "gguf" else [])
    options, low_only = _usable_quants(all_options)
    low_only = low_only and bool(all_options)  # у не-GGUF репозиториев квантов нет вовсе
    dropped = len(all_options) - len(options)
    chosen = _pick_gguf(options)
    if chosen:
        weights = int(chosen["weights_gib"] * GIB)

    weights_gib = round(weights / GIB, 1) if weights else 0.0
    if not kv_known:
        # конфиг недоступен (бывает, когда базовый репозиторий удалили) — считаем грубо:
        # у плотной модели с GQA KV-кэш это примерно 0.25 ГиБ на миллиард параметров при 32k
        params = detect_params(repo_id) or max(weights_gib, 1.0)
        kv_gib = round(max(0.5, params * 0.25 * (ctx / DEFAULT_CTX)
                           * KV_BYTES.get(engine, 2) / 2), 1)
    vram_gib = vram_estimate(weights_gib, kv_gib, vocab) if weights_gib else 0.0
    # Вторая оценка: эксперты в оперативке. Нужна и UI (показать выгоду), и подбору
    # сервера (одна карта вместо четырёх), и деплою (сколько слоёв выгружать).
    #
    # Считается ТОЛЬКО для llama.cpp: выгрузка — это его флаг --n-cpu-moe, а значит она
    # возможна лишь у GGUF. Раньше оценка выдавалась для любой MoE, и в карточке
    # safetensors-модели стояло «VRAM с экспертами в RAM: 12.9 ГиБ» вместе с галочкой,
    # которую можно было поставить. Обещание, которого vLLM выполнить не может: ровно
    # по нему модель на 335 ГиБ уехала на сервер с 96 ГиБ VRAM и упала по OOM. Движок
    # проверял это и раньше, но только в deploy.py — то есть уже после аренды.
    offload = (cpu_moe_estimate(moe, weights_gib, kv_gib, vocab)
               if engine == ENGINE_LLAMACPP else None)
    # диск: веса + распакованный кэш загрузчика и система
    disk_gb = int(weights_gib * 1.15 + 20) if weights_gib else 60

    return {
        "repo_id": repo_id,
        "url": f"https://huggingface.co/{repo_id}",
        "format": fmt,
        "engine": engine,
        "quantization": detect_quant_arg(own_cfg),
        "precision": weight_precision(repo_id),
        "tool_parser": tool_parser_for(repo_id),
        "bpw": detect_bpw(repo_id),
        "params_b": detect_params(repo_id),
        "uncensored": bool(_UNCENSORED_RE.search(repo_id)),
        "gated": bool(meta.get("gated")),
        "downloads": int(meta.get("downloads") or 0),
        "likes": int(meta.get("likes") or 0),
        "updated": (meta.get("lastModified") or "")[:10],
        "weights_gib": weights_gib,
        "file_count": len(files),
        "gguf_files": sorted(f for f in files if f.lower().endswith(".gguf")),
        "gguf_options": options,                       # кванты: имя, размер, оценка железа
        "gguf_file": chosen["path"] if chosen else "",  # что выбрано по умолчанию
        "gguf_dropped": dropped,                        # сколько квантов ниже Q4_K_M скрыто
        "gguf_low_only": low_only,                      # годных нет вообще — показываем как есть
        "ctx": ctx,
        "ctx_max": ctx_max,     # нативный потолок модели; None — в конфиге не нашлось
        "ctx_capped": bool(ctx_max and ctx_max > CTX_CAP),  # дефолт урезан нашим потолком
        "vocab": vocab,
        "vram_gib": vram_gib,
        # накладные расходы движка отдельной цифрой: в UI они объясняют разницу между
        # «веса + KV» и итогом, и повторять формулу в JS не нужно
        "overhead_gib": round(vram_gib - weights_gib - kv_gib, 1) if weights_gib else 0.0,
        "moe": moe,                                     # эксперты: сколько их и какая доля веса
        "cpu_moe": offload,                             # во что обойдётся выгрузка в RAM
        "vram_cpu_moe_gib": offload["vram_gib"] if offload else None,
        "ram_gb": offload["ram_gb"] if offload else None,
        "kv_gib": kv_gib,
        "kv_known": kv_known,   # посчитан по конфигу, а не прикинут по размеру весов
        "disk_gb": disk_gb,
        "tags": [t for t in tags if not t.startswith(("license:", "region:", "arxiv:"))][:10],
    }
