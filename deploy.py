# -*- coding: utf-8 -*-
"""
deploy.py — арендует GPU-сервер на Clore.ai под выбранную uncensored-модель с Hugging Face,
ждёт, пока startup-скрипт поставит движок и скачает веса, переключает OpenCode на новый адрес
и шлёт отчёт в Telegram.

Запуск через UI (`ui.bat`) или из консоли:
  python deploy.py                                   # модель из .env (MODEL_REPO)
  python deploy.py --model owner/name                # конкретный репозиторий HF
  python deploy.py --model https://huggingface.co/owner/name --ctx 65536
  python deploy.py --dry-run                         # только показать кандидатов
  python deploy.py --force                           # арендовать ещё один сервер
"""
import argparse
import base64
import gzip
import json
import os
import random
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

import hfmodels
from clore import (CURRENCIES, PRICE_PERIOD_HOURS, Clore, CloreError, is_price,
                   order_access, price_of, server_brief)

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATES = os.path.join(HERE, "templates")
# Пути к состоянию переопределяемы: иначе два одновременных деплоя затирают друг другу
# .env (ORDER/API_URL) и сгенерированный startup-скрипт. Для параллельных запусков
# задайте UNCENSORED_ENV и UNCENSORED_ONSTART на свои файлы.
ENV_PATH = os.environ.get("UNCENSORED_ENV") or os.path.join(HERE, ".env")
GENERATED = (os.environ.get("UNCENSORED_ONSTART")
             or os.path.join(HERE, "onstart.generated.sh"))  # содержит ключи — в .gitignore
OPENCODE_CFG = os.path.expanduser(os.path.join("~", ".config", "opencode", "opencode.json"))
OPENCODE_CFG_USED = [OPENCODE_CFG]   # куда записали на самом деле — для итоговой сводки

API_PORT = 5000                 # внутренний порт движка в контейнере
# Отдельный порт под прогресс деплоя: движок на API_PORT поднимается последним, а знать,
# что происходит, надо раньше — и раньше, чем на сервере заработает sshd.
STATUS_PORT = 5001
GIB_BYTES = 1024 ** 3
MODEL_NAME = "uncensored"       # id модели в OpenAI-совместимом API (не меняется при смене весов)
DEFAULT_CTX = hfmodels.DEFAULT_CTX   # запасной вариант: реальный берётся из конфига модели
DEFAULT_CURRENCY = "bitcoin"

IMAGES = {
    hfmodels.ENGINE_TABBY: "nvidia/cuda:12.8.1-runtime-ubuntu24.04",
    hfmodels.ENGINE_VLLM: "nvidia/cuda:12.8.1-runtime-ubuntu24.04",
    hfmodels.ENGINE_LLAMACPP: "nvidia/cuda:12.8.1-devel-ubuntu24.04",
}
SCRIPTS = {
    hfmodels.ENGINE_TABBY: "onstart_tabby.sh",
    hfmodels.ENGINE_VLLM: "onstart_vllm.sh",
    hfmodels.ENGINE_LLAMACPP: "onstart_llamacpp.sh",
}

# Жёсткий порог по сети был 500 Mbps и в одиночку прятал больше тысячи серверов, включая
# почти все 5090. Время скачивания и так входит в score (price * (1 + dl_hours)), поэтому
# медленный хост проигрывает сам — отсекаем только совсем безнадёжные.
MIN_NET_DOWN_MBPS = 200
TRY_OFFERS = 5                  # сколько кандидатов показываем и столько же реально пробуем
# Доля удачных аренд по версии Clore. У большинства хостов она 0.999, но примерно у каждого
# шестого ниже 0.9 — и именно там контейнер поднимается криво (например, вовсе без sshd).
# Экономия в пару центов в час того не стоит.
MIN_RELIABILITY = 0.90
# Насколько дороже лучшего предложения сервер всё ещё считается равнозначным при --spread
SPREAD_RATIO = 1.25
SPREAD_MIN = 10                 # минимум кандидатов в перемешивании при --spread
SPREAD_HARD_CAP = 2.0           # дороже лучшего предложения вдвое — не берём даже при --spread
# Во сколько раз больше VRAM требовать от vLLM-сервера, если число карт не степень двойки
VLLM_UNEVEN_HEADROOM = 1.4
# Запас поверх оценки VRAM. Раньше его не было вовсе: карта с ровно 20.0 ГиБ проходила
# фильтр для модели, которой насчитали 20.0 — то есть впритык, без права на ошибку в
# оценке. Запас нужен всем трём движкам, но по разным причинам:
#   * vLLM стартует с --gpu-memory-utilization 0.92, то есть модель обязана уложиться
#     в 92% карты; 1/0.92 это уже 1.087;
#   * llama.cpp докладывает CUDA-контекст и графовые буферы на каждое устройство;
#   * TabbyAPI с gpu_split_auto оставляет себе место под активации сам.
VRAM_HEADROOM = 1.1
# Предел длины поля command у Clore. Найден перебором: 16384 Б проходит, 16465 — уже
# HTTP 500 без пояснений. Поэтому startup-скрипт едет сжатым.
COMMAND_LIMIT = 16384
# Clore не отдаёт страну сервера в маркетплейсе — регион виден только в имени хоста,
# которое появляется уже после аренды (n1.msk.cloreai.ru, n1.de.clorecloud.net…).
# Поэтому нежелательные регионы отсеиваются так: заказ создаётся, имя проверяется,
# и при совпадении заказ сразу отменяется, а деплой идёт к следующему кандидату.
EXCLUDE_HOSTS_DEFAULT = ""
ORDER_TIMEOUT = 20 * 60         # ждать, пока Clore выдаст IP и порты
# Общий предел ожидания. Он намеренно щедрый: решение «деплой не состоялся» принимается
# по застою прогресса (STALL_TIMEOUT), а не по общему времени — см. wait_api.
API_TIMEOUT = 5 * 3600
STALL_TIMEOUT = 30 * 60         # прогресс не менялся столько — считаем, что всё встало
# Здоровый хост Clore отвечает по ssh через минуту-две после старта контейнера. Если за
# это время нет ни ssh, ни статус-порта, ни API — контейнер не поднялся, и ждать его час
# бессмысленно: он всё это время тарифицируется.
SILENT_LIMIT = 5 * 60
# Во сколько раз реальная скорость скачивания может быть ниже заявленной Clore, прежде чем
# сервер считается негодным. Хост обещал 335 Mbps, реально качал 20 — веса ехали 1 час 50
# минут вместо шести минут, и деплой упёрся в таймаут.
SPEED_MIN_RATIO = 0.5
SPEED_WINDOW = 4 * 60           # на каком окне мерить скорость, чтобы не ловить шум
BLACKLIST_PATH = os.environ.get("UNCENSORED_BLACKLIST") or os.path.join(HERE, "blacklist.json")
POLL = 20
# через сколько после старта проверять sshd: он поднимается не сразу,
# скрипт сначала пропускает вперёд установку пакетов самого Clore
SSH_GRACE = 8 * 60
# как часто писать в лог, что происходит на сервере: пять минут тишины на этапе
# «качаю веса» неотличимы от зависшего деплоя
WAIT_NOTE = 60

os.environ["PYTHONUTF8"] = "1"
for _stream in (sys.stdout, sys.stderr):  # консоль Windows по умолчанию cp1252
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


# ---------- чёрный список серверов ----------
def blacklist_load():
    """{id сервера: {reason, at, ...}}. Файл может отсутствовать или быть битым — не беда."""
    try:
        with open(BLACKLIST_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return {str(k): v for k, v in data.items()} if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def blacklist_ids():
    return {int(k) for k in blacklist_load() if str(k).isdigit()}


def blacklist_add(server_id, reason, **extra):
    """Запомнить плохой сервер навсегда: в следующих подборах он даже не появится."""
    data = blacklist_load()
    data[str(server_id)] = {"reason": reason, "at": time.strftime("%Y-%m-%d %H:%M:%S"), **extra}
    try:
        with open(BLACKLIST_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1, sort_keys=True)
    except OSError as e:
        log(f"не удалось записать чёрный список: {e}")
    return data


def host_excluded(host, patterns):
    """Попадает ли имя хоста под один из фильтров (подстроки через запятую)."""
    low = (host or "").lower()
    for p in patterns:
        p = p.strip().lower()
        if p and p in low:
            return p
    return None


# ---------- .env ----------
def load_env():
    env = {}
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
    return env


def save_env_keys(updates: dict):
    lines, seen = [], set()
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, encoding="utf-8") as f:
            for line in f:
                raw = line.rstrip("\n")
                if "=" in raw and not raw.startswith("#"):
                    k = raw.split("=", 1)[0].strip()
                    if k in updates:
                        lines.append(f"{k}={updates[k]}")
                        seen.add(k)
                        continue
                lines.append(raw)
    for k, v in updates.items():
        if k not in seen:
            lines.append(f"{k}={v}")
    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines).rstrip("\n") + "\n")


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


# Кто выложил сборку — в названии модели не нужен: «Huihui-GLM-5.3-Flash-abliterated»
# это та же GLM-5.3, а в уведомлении важно, что именно поднимается.
_VENDOR_RE = re.compile(r"^(huihui[-_]?ai|huihui|bartowski|mradermacher|dealignai|"
                        r"cognitivecomputations|unsloth|thedrummer|dphn|nerkyor)[-_]", re.I)
_VERSION_RE = re.compile(r"^v?\d+(\.\d+)*$")


def short_model(repo_id):
    """
    Короткое имя модели для уведомлений: «dealignai/Qwen3.8-27B-UNCENSORED-GGUF» -> «Qwen3.8».
    Версию оставляем, если она отдельным куском: «Huihui-GLM-5.3-Flash» -> «GLM-5.3».
    """
    name = _VENDOR_RE.sub("", (repo_id or "").split("/")[-1])
    parts = [p for p in name.split("-") if p]
    if not parts:
        return repo_id or "модель"
    if len(parts) > 1 and _VERSION_RE.match(parts[1]):
        return f"{parts[0]}-{parts[1]}"
    return parts[0]


def short_gpu(name, count=1):
    """«1x NVIDIA GeForce RTX 5090» -> «GeForce RTX 5090»: слово NVIDIA ничего не добавляет."""
    clean = re.sub(r"^NVIDIA\s+", "", (name or "").strip())
    return f"{count}x {clean}" if count and count > 1 else clean


def tg(env, text):
    token, chat = env.get("TG_BOT_TOKEN"), env.get("TG_CHAT_ID")
    if not token or not chat:
        return
    data = json.dumps({"chat_id": chat, "text": text, "disable_web_page_preview": True}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=20).read()
    except Exception as e:  # noqa
        log(f"TG ошибка: {e}")


def client(env):
    return Clore(env.get("CLORE_API_KEY") or "")


# ---------- startup-скрипт ----------
def render_onstart(info, env, ctx=None, gguf_file=None, save=True):
    """Готовит текст startup-скрипта и env-переменные заказа под движок модели."""
    engine = info["engine"]
    path = os.path.join(TEMPLATES, SCRIPTS[engine])
    with open(path, encoding="utf-8") as f:
        tpl = f.read()
    # общие для всех движков функции (ожидание apt, проверки, скачивание) живут отдельно,
    # чтобы правка не разъезжалась по трём шаблонам
    with open(os.path.join(TEMPLATES, "_common.sh"), encoding="utf-8") as f:
        tpl = tpl.replace("__COMMON__", f.read())
    api_key = env.get("API_KEY") or ""
    if not api_key:
        raise SystemExit("в .env нет API_KEY — задайте его в UI (вкладка «Настройки») или вручную")
    ctx = int(ctx or info.get("ctx") or DEFAULT_CTX)
    # без явного выбора берём квант, который hfmodels счёл лучшим для кода,
    # а не первый по алфавиту — тем часто оказывается IQ1 или draft-модель
    gguf = gguf_file or info.get("gguf_file") or (info.get("gguf_files") or [""])[0]
    repl = {
        "__MODEL_REPO__": info["repo_id"],
        "__MODEL_NAME__": MODEL_NAME,
        "__API_KEY__": api_key,
        "__CTX__": str(ctx),
        "__PORT__": str(API_PORT),
        "__STATUS_PORT__": str(STATUS_PORT),
        "__FORMAT__": info["format"],
        "__QUANT__": info.get("quantization") or "",
        "__TOOL_PARSER__": info.get("tool_parser") or "hermes",
        "__GGUF_FILE__": gguf,
        # значение по умолчанию: реальное приедет в env заказа,
        # когда станет известна VRAM выбранного сервера
        "__N_CPU_MOE__": "0",
    }
    body = tpl
    for k, v in repl.items():
        body = body.replace(k, v)
    if save:
        with open(GENERATED, "w", encoding="utf-8", newline="\n") as f:
            f.write(body)

    order_env = {
        "MODEL_REPO": info["repo_id"],
        "MODEL_NAME": MODEL_NAME,
        "API_KEY": api_key,
        "CTX": str(ctx),
        "WEIGHT_FORMAT": info["format"],
        "QUANT": info.get("quantization") or "",
        "TOOL_PARSER": info.get("tool_parser") or "hermes",
        "GGUF_FILE": gguf,
    }
    if env.get("HF_TOKEN"):
        order_env["HF_TOKEN"] = env["HF_TOKEN"]
    return body, order_env


def ssh_pubkey(env):
    """Локальный публичный ключ — с ним вход на сервер не зависит от пароля."""
    explicit = env.get("SSH_PUBKEY")
    if explicit:
        return explicit.strip()
    for name in ("id_ed25519.pub", "id_ecdsa.pub", "id_rsa.pub"):
        path = os.path.expanduser(os.path.join("~", ".ssh", name))
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                key = f.read().strip()
            if key:
                return key
    return None


def build_command(script):
    """
    Clore выполняет одну строку startup-команды — везём скрипт в base64.

    Два важных ограничения площадки:
    * саму команду Clore кладёт в /root/onstart.sh и запускает оттуда, поэтому писать
      свой скрипт надо в ДРУГОЙ файл: перезапись исполняемого файла ломает bash;
    * команда должна завершиться быстро, иначе entrypoint Clore не дойдёт до sshd —
      установка уезжает в фон, а логи в /root/deploy.log.
    """
    # Скрипт едет сжатым: Clore отвечает HTTP 500 на слишком длинную команду, а по мере
    # обрастания проверками текст перевалил за 10 КБ (в base64 — 14+ КБ) и заказы молча
    # перестали создаваться. gzip сжимает его примерно втрое.
    b64 = base64.b64encode(gzip.compress(script.encode(), 9)).decode()
    # Порог Clore измерен экспериментально: на 16384 байтах поле command ещё принимается,
    # на 16465 уже отдаёт HTTP 500 без объяснений. Предупреждаем заранее.
    if len(b64) > COMMAND_LIMIT - 200:
        log(f"⚠️ команда заказа {len(b64)} Б при лимите Clore {COMMAND_LIMIT} Б — "
            f"startup-скрипт пора сокращать, иначе аренда начнёт падать с HTTP 500")
    # в /root/deploy.log пишет сам скрипт через tee; сюда уводим только то, что могло
    # вывалиться до его старта — иначе два писателя в один файл перемешивают вывод
    return (f"echo {b64} | base64 -d | gzip -d > /root/deploy_model.sh && "
            f"chmod +x /root/deploy_model.sh && "
            f"setsid nohup bash /root/deploy_model.sh > /root/onstart_wrapper.log 2>&1 < /dev/null &")


# ---------- API модели ----------
def api_alive(url, key, timeout=10):
    try:
        req = urllib.request.Request(url + "/models", headers={"Authorization": f"Bearer {key}"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            d = json.loads(resp.read().decode())
        return bool(d.get("data"))
    except Exception:
        return False


def bench(url, key, model=MODEL_NAME):
    """
    Скорость генерации, токенов в секунду.

    llama.cpp отдаёт точную цифру в timings.predicted_per_second — её и берём: деление
    токенов на время запроса завышало бы вклад сети и обсчёта промпта. У vLLM такого поля
    нет, там остаётся делить, но хотя бы по времени ответа.

    Прогревочный запрос намеренно отличается от замерочного: одинаковые промпты попадают
    в кэш (prompt_tokens_details.cached_tokens), и второй замер получается нечестным.
    """
    prompts = ["Say hello in one short sentence.",                      # прогрев ядер
               "Write a Python function that parses a .env file into a dict. Code only."]
    out = None
    for i, prompt in enumerate(prompts):
        body = json.dumps({"model": model, "max_tokens": 256 if i else 32,
                           "temperature": 0,
                           "messages": [{"role": "user", "content": prompt}]}).encode()
        req = urllib.request.Request(url + "/chat/completions", data=body,
                                     headers={"Authorization": f"Bearer {key}",
                                              "Content-Type": "application/json"})
        try:
            t0 = time.time()
            with urllib.request.urlopen(req, timeout=600) as resp:
                d = json.loads(resp.read().decode())
        except Exception as e:  # noqa
            log(f"bench ошибка: {e}")
            return None
        if not i:
            continue
        spent = time.time() - t0
        tps = ((d.get("timings") or {}).get("predicted_per_second")
               or (d.get("usage") or {}).get("completion_tokens_per_sec"))
        if not tps:
            done = (d.get("usage") or {}).get("completion_tokens")
            tps = done / max(spent, 0.001) if done else None
        out = tps
    return out


# ---------- совместимость GPU ----------
# Clore сдаёт и очень старые карты. Дешевизна обманчива: Volta (V100) не умеет bfloat16
# и FP8, а vLLM с версии V1 требует Ampere и новее — такой заказ просто не поднимется.
# Сопоставляем название карты с архитектурой и её compute capability.
GPU_ARCH = [
    (r"blackwell|\bb[12]00\b|gb\d{3}|rtx\s*50\d0|5090|5080|5070", "Blackwell", 10.0),
    (r"h100|h200|h800|gh200", "Hopper", 9.0),
    (r"\bada\b|rtx\s*40\d0|4090|4080|4070|\bl40s?\b|\bl4\b", "Ada", 8.9),
    (r"a100|a800|cmp\s*170hx", "Ampere", 8.0),
    (r"\ba(?:2|10|16|30|40)\b|rtx\s*30\d0|3090|3080|3070|3060|"
     r"a6000|a5000|a4500|a4000|a2000", "Ampere", 8.6),
    # CMP HX и GTX 16xx — тот же Turing, просто без видеовыхода или урезанные
    (r"rtx\s*20\d0|2080|2070|2060|\bt4\b|titan\s*rtx|quadro\s*rtx|"
     r"gtx\s*16\d0|cmp\s*(?:30|40|50|70|90)hx", "Turing", 7.5),
    (r"v100|titan\s*v", "Volta", 7.0),
    (r"p100|\bp40\b|\bp4\b|g[tx]{1,2}\s*10\d0|1080|1070|1060|p10[246][-_ ]?100", "Pascal", 6.1),
    (r"\bm40\b|\bm60\b|gtx\s*9\d0|tesla\s*m\d", "Maxwell", 5.2),
]
# Минимальная compute capability под движок и формат весов.
MIN_CC_ENGINE = {
    hfmodels.ENGINE_VLLM: (8.0, "vLLM начиная с движка V1 требует Ampere (CC 8.0) и новее"),
    hfmodels.ENGINE_TABBY: (8.0, "ExLlamaV3 требует Ampere (CC 8.0) и новее"),
    hfmodels.ENGINE_LLAMACPP: (6.1, "llama.cpp работает, но на старых картах заметно медленнее"),
}
# Формат весов -> (без этой CC не запустится, ниже этой — работает, но хуже, чем на пометку).
# FP8 на Ampere vLLM тянет через Marlin: нативных ядер нет, веса распаковываются на лету.
# А вот FP4 эмулировать нечем — там нужны ядра Hopper или Blackwell.
MIN_CC_PRECISION = {
    "fp8":   (8.0, 8.9, "нативных FP8-ядер нет, vLLM эмулирует их через Marlin — "
                        "медленнее, чем на Ada и новее"),
    "mxfp8": (8.0, 8.9, "нативных FP8-ядер нет, vLLM эмулирует их через Marlin — "
                        "медленнее, чем на Ada и новее"),
    "mxfp4": (9.0, 9.0, "MXFP4 требует Hopper (CC 9.0) и новее"),
    "nvfp4": (10.0, 10.0, "NVFP4 требует Blackwell (CC 10.0)"),
}
# Ниже этого стоит предупредить даже там, где формально заведётся
RECOMMENDED_CC = 8.0


def gpu_arch(name):
    """('8x Tesla V100SXM232GB') -> ('Volta', 7.0); неизвестная карта -> (None, None)."""
    low = (name or "").lower()
    for pat, arch, cc in GPU_ARCH:
        if re.search(pat, low):
            return arch, cc
    return None, None


def gpu_compat(name, engine=None, quant=None):
    """
    Потянет ли карта выбранный движок и формат весов.
    Возвращает (совместима, пометка). Неизвестные карты считаем совместимыми —
    лучше показать лишнее, чем спрятать новую модель GPU, которой нет в таблице.
    """
    if "mixed" in (name or "").lower():
        # Clore так помечает хосты с разными картами: модель придётся делить между
        # неодинаковыми GPU, и движок обычно равняется на самую слабую из них
        return True, "разные GPU на одном хосте — движок будет равняться на слабейшую карту"
    arch, cc = gpu_arch(name)
    if cc is None:
        return True, ""
    need, why = MIN_CC_ENGINE.get(engine, (0.0, ""))
    if cc < need:
        return False, f"{arch}: {why}"

    hard, soft, note = MIN_CC_PRECISION.get(str(quant or "").lower(), (0.0, 0.0, ""))
    if cc < hard:
        return False, f"{arch}: {note}"
    if cc < soft:
        return True, f"{arch}: {note}"
    if cc < RECOMMENDED_CC:
        return True, f"{arch} — старая архитектура, скорость будет заметно ниже"
    return True, ""


# ---------- подбор сервера ----------
# specs.disk у Clore — строка вида "CT120BX500SSD1x20x20x 71.8852GB": размер всегда в хвосте,
# а числа в начале — часть названия накопителя, брать их нельзя.
_DISK = re.compile(r"([\d.]+)\s*(GB|TB|MB)\s*$", re.I)


def disk_gb(server):
    raw = str((server.get("specs") or {}).get("disk") or "").strip()
    m = _DISK.search(raw)
    if not m:
        return 0.0
    val = float(m.group(1))
    unit = m.group(2).upper()
    return val * 1024 if unit == "TB" else val / 1024 if unit == "MB" else val


def net_est_of(brief):
    """Ожидаемая скорость сети: медиана замеров, если она есть, иначе последний замер."""
    return brief.get("net_down_est") or brief.get("net_down") or 0


def gpu_matches(query, brief):
    """
    Поиск по серверу: все слова запроса должны найтись в названии карт, их количестве
    или id сервера. Раньше сравнение было одной подстрокой, и запрос из двух слов
    («rtx 5090», «2x 5090») не находил ничего, хотя такие серверы в списке были.
    """
    hay = f"{brief.get('gpu_count')}x {brief.get('gpu') or ''} id {brief.get('id')}".lower()
    terms = [t for t in re.split(r"[\s,]+", (query or "").lower()) if t]
    return all(t in hay for t in terms)


def find_offers(clore, need_vram, need_disk, max_price_usd=None, pay_currency=None,
                gpu_filter=None, order_type="on-demand", min_net=MIN_NET_DOWN_MBPS,
                download_gib=0, limit=25, engine=None, quant=None, allow_old_gpu=False,
                balance_usd=None, min_reliability=MIN_RELIABILITY, spread=False,
                hidden=None, blacklist=None, need_ram=0, cpu_moe=False):
    """
    Серверы, которые вместят модель, отсортированные по цене в USD с учётом времени скачивания.
    engine/quant — чтобы отсеять карты, на которых выбранный движок не заведётся
    (например, vLLM на Tesla V100). allow_old_gpu=True оставляет их в списке с пометкой.
    balance_usd — остаток на балансе. Clore списывает оплату по часам (проверено на живом
    заказе: $0.11 за 0.3 часа при цене $8/сутки), поэтому дорогой сервер арендовать можно —
    он просто проработает меньше. Раньше тут стоял потолок «баланс делить на сутки», и он
    молча прятал две трети маркетплейса: с $10 на балансе не показывалось ничего дороже
    $0.41/ч, хотя заказ за $1/ч создаётся без проблем. Теперь отсеиваем только то,
    на что не хватает даже на час, а остальным считаем, насколько хватит денег.
    cpu_moe — режим «эксперты в оперативке»: тогда скорость определяется не картой,
    а пропускной способностью RAM, и в сортировке сервер с восемью каналами памяти
    выигрывает у вдвое более дешёвого двухканального.
    need_ram — сколько нужно обычной оперативки. Нужен для выгрузки экспертов MoE в RAM:
    модель, под которую иначе требуется четыре карты, влезает в одну, но упирается уже
    не в VRAM, а в объём RAM сервера.
    blacklist — id серверов, которые уже показали себя плохо (не поднялись или качали
    вдесятеро медленнее заявленного). По умолчанию берётся сохранённый список с диска.
    hidden — необязательный словарь: туда складывается, сколько серверов отсеял каждый
    фильтр. Без этого список выглядел необъяснимо коротким (из полутора тысяч свободных
    серверов до пользователя доходило семьдесят), и понять, какой фильтр ослабить,
    было нельзя.
    spread — перемешать кандидатов с близкой ценой. Нужно, когда деплоев несколько сразу:
    иначе все выбирают один и тот же самый дешёвый сервер, дерутся за него и получают
    отказ «уже арендован». Без флага перемешиваются только точные совпадения по цене.
    """
    servers = clore.marketplace()
    rates = clore.currency_rates(servers)
    banned = blacklist_ids() if blacklist is None else {int(x) for x in blacklist}
    skip = hidden if hidden is not None else {}
    bump = lambda k: skip.__setitem__(k, skip.get(k, 0) + 1)  # noqa: E731
    out = []
    for s in servers:
        if s.get("rented"):
            continue
        if int(s.get("id", -1)) in banned:
            bump("в чёрном списке")
            continue
        b = server_brief(s, rates)
        spot = order_type == "spot"
        price = b["spot_price_usd"] if spot else b["price_usd"]
        if price is None or price <= 0:
            continue
        if max_price_usd and price > max_price_usd:
            bump("дороже заданной цены")
            continue
        if balance_usd and price > balance_usd:
            bump("не хватает даже на час")
            continue
        # запас на неточность оценки: карта «ровно под размер» гарантированно падает по OOM
        need = need_vram * VRAM_HEADROOM if need_vram else 0
        # vLLM шардирует веса ровно только при tensor-parallel, а он требует числа карт,
        # кратного степени двойки. На 3 или 5 картах остаётся чистый pipeline-параллелизм:
        # слои ложатся неравномерно, первая карта переполняется и загрузка падает по OOM.
        # Такие хосты не запрещаем, но требуем запас по памяти.
        if need and engine == hfmodels.ENGINE_VLLM and b["gpu_count"] & (b["gpu_count"] - 1):
            need *= VLLM_UNEVEN_HEADROOM
        if need and b["vram_gb"] < need:
            bump("мало VRAM")
            continue
        if need_ram and b["ram_gb"] and b["ram_gb"] < need_ram:
            bump("мало оперативки")
            continue
        if need_disk and disk_gb(s) and disk_gb(s) < need_disk:
            bump("мало места на диске")
            continue
        if min_net and net_est_of(b) and net_est_of(b) < min_net:
            bump("медленная сеть")
            continue
        if gpu_filter and not gpu_matches(gpu_filter, b):
            bump("не подходит под поиск")
            continue
        if min_reliability and b["reliability"] and b["reliability"] < min_reliability:
            bump("низкая надёжность хоста")
            continue
        # у разнородного хоста смотрим каждую карту: движок упрётся в слабейшую
        checks = [gpu_compat(g, engine, quant) for g in (b.get("gpu_models") or [b["gpu"]])]
        ok = all(c[0] for c in checks)
        note = next((c[1] for c in checks if not c[0]), "") or next((c[1] for c in checks if c[1]), "")
        if not ok and not allow_old_gpu:
            bump("старая архитектура GPU")
            continue
        archs = [gpu_arch(g) for g in (b.get("gpu_models") or [b["gpu"]])]
        known = [a for a in archs if a[1] is not None]
        b["arch"], b["gpu_cc"] = min(known, key=lambda a: a[1]) if known else (None, None)
        if len({a[0] for a in archs}) > 1:
            b["arch"] = "+".join(sorted({a[0] or "?" for a in archs}))
        b["gpu_ok"] = ok
        b["gpu_note"] = note
        if pay_currency:  # пользователь жёстко задал валюту списания
            coin = price_of(s, pay_currency, order_type)
            rate = rates.get(pay_currency)
            if not coin or not rate:
                continue
            price = round(float(coin) * float(rate) / PRICE_PERIOD_HOURS, 4)
            if max_price_usd and price > max_price_usd:
                continue
            b["pay_currency"], b["pay_amount"] = pay_currency, float(coin)
            b["spot_pay_currency"], b["spot_pay_amount"] = pay_currency, float(coin)
        b["price_used"] = price                                  # USD/ч
        b["pay_currency_used"] = b["spot_pay_currency"] if spot else b["pay_currency"]
        b["pay_amount_used"] = b["spot_pay_amount"] if spot else b["pay_amount"]
        # цены во всех валютах — чтобы при пустом кошельке можно было заплатить другой
        b["prices_coin"] = {cur: float(v) for cur, v in
                            (((s.get("price") or {}).get("spot" if spot else "on_demand")) or {}).items()
                            if is_price(v)}
        b["disk_gb"] = round(disk_gb(s), 0)
        # по медиане замеров, а не по последнему: именно из-за разового всплеска 335 Mbps
        # у хоста с типичными 92 деплой рассчитывал скачивание на 6 минут вместо двух часов
        net_est = b.get("net_down_est") or b["net_down"] or 100
        dl_hours = (download_gib * 1024 * 8 / max(net_est, 1)) / 3600 if download_gib else 0
        b["download_hours"] = round(dl_hours, 2)
        b["hours_affordable"] = round(balance_usd / price, 1) if balance_usd else None
        # При выгрузке экспертов генерация упирается в память: двухканальный сервер
        # выдаст вдвое меньше токенов, чем четырёхканальный, и дешевизна его не спасает.
        # Приводим цену к «за токен»: четыре канала считаем единицей отсчёта.
        ram_factor = 4.0 / max(b.get("ram_channels") or 2, 1) if cpu_moe else 1.0
        b["score"] = price * (1 + dl_hours) * ram_factor
        out.append(b)
    out.sort(key=lambda x: x["score"])
    if out:
        # «почти та же цена» считаем равнозначной и тасуем: одинаковые запуски
        # перестают ломиться на один хост, а переплата остаётся в пределах SPREAD_RATIO
        best = out[0]["score"]
        band = best * (SPREAD_RATIO if spread else 1.0) + 1e-9
        count = len([o for o in out if o["score"] <= band])
        if spread:
            # Тасуем не меньше SPREAD_MIN кандидатов, иначе десяток параллельных запусков
            # столкнётся на тех же двух-трёх серверах. Но расширение ограничено жёстким
            # потолком: раньше SPREAD_MIN молча отменял SPREAD_RATIO, и выбирался сервер
            # в разы дороже нужного — при наличии подходящего за копейки.
            affordable = len([o for o in out if o["score"] <= best * SPREAD_HARD_CAP + 1e-9])
            count = min(max(count, SPREAD_MIN), max(affordable, 1))
        near = out[:count]
        random.shuffle(near)
        out = near + out[count:]
    return out[:limit]


def wallet_balances(clore):
    """{валюта: баланс} по кошелькам Clore."""
    out = {}
    for w in clore.wallets():
        name = w.get("name") or ""
        try:
            out[name] = float(w.get("balance") or 0)
        except (TypeError, ValueError):
            out[name] = 0.0
    return out


def pick_currency(offer, balances, rates, forced=None):
    """
    Чем платить за сервер. В UI всё в USD, поэтому валюту выбираем сами: самая дешёвая
    в пересчёте на USD, на кошельке которой хватает хотя бы на сутки аренды.
    Возвращает (валюта, сумма в этой валюте за сутки — так их принимает Clore, цена в $/ч).
    """
    coins = offer.get("prices_coin") or {}
    if forced:
        amount = coins.get(forced)
        if not amount:
            return forced, None, None
        return forced, amount, amount * rates.get(forced, 0) / PRICE_PERIOD_HOURS
    ranked = sorted(((cur, amt, amt * rates[cur] / PRICE_PERIOD_HOURS)
                     for cur, amt in coins.items() if rates.get(cur)), key=lambda x: x[2])
    if not ranked:
        cur = offer.get("pay_currency_used") or DEFAULT_CURRENCY
        return cur, offer.get("pay_amount_used"), offer.get("price_used")
    affordable = [r for r in ranked if balances.get(r[0], 0) >= r[1]]
    if affordable:
        return affordable[0]
    # Денег не хватает ни в одной валюте. Берём ту, где средства вообще есть: иначе Clore
    # ответит not_enough_balance про монету, которой у пользователя нет в принципе,
    # и причина отказа становится совершенно непонятной.
    held = [r for r in ranked if balances.get(r[0], 0) > 0]
    return (held or ranked)[0]


def wait_order_ready(clore, order_id, logf=log, timeout=ORDER_TIMEOUT):
    """Ждёт, пока у заказа появятся адрес и проброшенные порты."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            o = clore.order(order_id)
        except CloreError as e:
            logf(f"Clore: {e}")
            o = None
        if o:
            acc = order_access(o)
            if o.get("expired"):
                logf(f"заказ {order_id} завершён на стороне Clore")
                return None
            if acc["host"] and acc["ports"].get(API_PORT):
                return o, acc
            logf(f"[{order_id}] ждём адрес и порты… {acc['host'] or 'ip ещё нет'}")
        else:
            logf(f"[{order_id}] заказ ещё не появился в my_orders")
        time.sleep(POLL)
    return None


def ssh_alive(host, port, timeout=8):
    """Отвечает ли sshd баннером. Порт у Clore проброшен всегда, а демон за ним — нет."""
    import socket
    try:
        with socket.create_connection((host, int(port)), timeout) as s:
            s.settimeout(timeout)
            return s.recv(32).startswith(b"SSH-")
    except OSError:
        return False


# Что делает сервер, пока мы ждём: стадия + сколько весов уже скачано. Одна команда,
# BatchMode — чтобы ssh не завис на запросе пароля, если ключ не подхватился.
#
# Шаблоны для pgrep -f написаны как "[v]llm" намеренно: иначе pgrep находит сам себя —
# строка "vllm serve" есть в командной строке оболочки, которая эту проверку и выполняет,
# и стадия определялась как «движок запущен» ещё во время сборки движка.
#
# Сборка движка и скачивание весов идут параллельно, поэтому стадию выбираем по
# самой поздней активности: работает движок -> ещё качаем веса -> только собираем.
_PROGRESS_CMD = (
    'if pgrep -x llama-server >/dev/null 2>&1 || pgrep -f "[v]llm serve" >/dev/null 2>&1; '
    'then echo engine; '
    'elif pgrep -f "[h]f download" >/dev/null 2>&1; '
    'then echo "download $(du -sm /root/models 2>/dev/null | cut -f1)"; '
    'elif [ -s /root/engine.log ]; then echo engine; '
    'else echo setup; fi'
)


_STAGE_TEXT = {
    "packages": "ставлю пакеты (ждём, пока Clore доустановит свои)",
    "download": "качаю веса",
    "engine": "движок запущен, загружает веса в VRAM",
}


def http_progress(host, port, timeout=8):
    """
    Прогресс от самого контейнера: стадия и объём уже скачанного. Работает с первых
    секунд — в отличие от ssh, который на Clore первые минуты просто сбрасывает
    соединение, из-за чего в логе и была тишина ровно на время скачивания весов.
    """
    if not host or not port:
        return None
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/", timeout=timeout) as r:
            data = json.loads(r.read().decode())
    except Exception:  # noqa — любой сбой просто означает «пока не знаем»
        return None
    return {"stage": data.get("stage") or "setup", "bytes": int(data.get("bytes") or 0)}


def progress_text(info, total_gib=None):
    """Человеческая строка из сырых данных о прогрессе."""
    if not info:
        return None
    stage = _STAGE_TEXT.get(info.get("stage"), "готовлю сервер")
    if info.get("stage") != "download":
        return stage
    gib = (info.get("bytes") or 0) / float(GIB_BYTES)
    if total_gib:
        return f"{stage}: {gib:.1f} из {total_gib:.1f} ГиБ ({min(100, int(gib / total_gib * 100))}%)"
    return f"{stage}: {gib:.1f} ГиБ"


def remote_progress(host, port, timeout=20):
    """
    Короткая строка о том, на какой стадии сервер. Нужна, чтобы ожидание не выглядело
    зависанием: сборка движка и скачивание весов занимают десятки минут, а раньше в лог
    в это время не попадало ничего, кроме счётчика минут.
    Любая ошибка ssh — это не повод падать: возвращаем None и пишем как раньше.
    """
    if not host or not port:
        return None
    try:
        out = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
             "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR",
             "-o", f"ConnectTimeout={timeout}", "-p", str(port), f"root@{host}",
             _PROGRESS_CMD],
            capture_output=True, text=True, timeout=timeout + 15).stdout.split()
    except (OSError, subprocess.SubprocessError):
        return None
    if not out:
        return None
    if out[0] in ("engine", "setup"):
        return {"stage": out[0], "bytes": 0}
    if out[0] == "download":
        try:
            return {"stage": "download", "bytes": int(out[1]) * 1024 * 1024}  # du -sm отдаёт МиБ
        except (IndexError, ValueError):
            return {"stage": "download", "bytes": 0}
    return None


def wait_sshd(host, port, timeout=600):
    """
    Ждёт sshd. Двух минут не хватает: startup-скрипт сначала пропускает вперёд чужой apt
    (Clore доустанавливает свои пакеты, это до десяти минут), и только потом поднимается
    sshd. С коротким ожиданием предупреждение было ложным почти всегда.
    """
    t0 = time.time()
    while time.time() - t0 < timeout:
        if ssh_alive(host, port):
            return True
        time.sleep(10)
    return False


def drop_server(clore, order_id, server_id, reason, logf=log, **extra):
    """Отменить заказ, занести сервер в чёрный список и очистить состояние в .env."""
    logf(f"⛔ сервер {server_id}: {reason} — отменяю заказ {order_id} и беру другой")
    for _ in range(3):
        try:
            clore.cancel_order(order_id, reason[:64])
            break
        except CloreError as e:
            logf(f"отмена не прошла ({e}) — повтор")
            time.sleep(10)
    blacklist_add(server_id, reason, **extra)
    save_env_keys({"ORDER": "", "SERVER": "", "API_URL": "", "SSH": ""})


def wait_alive(host, ports, url, key, logf=log, limit=SILENT_LIMIT):
    """
    Подал ли контейнер хоть какой-то признак жизни: ssh, статус-порт или уже готовый API.
    У здорового хоста Clore это происходит за минуту-две. Молчание дольше limit означает,
    что контейнер не стартовал — такой сервер лучше сразу отдать обратно, чем ждать час,
    платя за него и не имея возможности даже посмотреть логи.
    """
    t0, noted = time.time(), 0
    while time.time() - t0 < limit:
        el = int(time.time() - t0)
        if el - noted >= 60:      # иначе первые пять минут деплой выглядит зависшим
            logf(f"жду отклика контейнера… {el // 60} мин из {limit // 60}")
            noted = el
        if http_progress(host, ports.get(STATUS_PORT), timeout=5):
            return "статус-порт"
        if ssh_alive(host, ports.get(22, 22), timeout=5):
            return "ssh"
        if api_alive(url, key, timeout=5):
            return "API"
        time.sleep(15)
    return None


def wait_api(url, key, logf=log, timeout=API_TIMEOUT, ssh_host=None, ssh_port=None,
             total_gib=None, status_port=None, net_mbps=None):
    """
    Ждёт готовности модели. Возвращает "ready" / "stalled" / "slow".

    Сдаёмся не по общему времени, а по застою: общий лимит считался от заявленной Clore
    скорости сети, а она врёт. На хосте с обещанными 335 Mbps веса реально качались со
    скоростью 20 Mbps, 1 час 50 минут — деплой объявил таймаут за 45 минут до того, как
    модель поднялась, а сервер всё это время работал и тратил деньги.

    Отдельно следим за самой скоростью: если реальная меньше заявленной в 1/SPEED_MIN_RATIO
    раз, ждать бессмысленно — такой сервер возвращаем и берём другой.
    """
    t0, noted, ssh_checked = time.time(), 0, ssh_host is None
    last_text, last_change, seen = None, time.time(), False
    mark = None                      # (время, байт) — точка отсчёта для замера скорости
    while time.time() - t0 < timeout:
        if api_alive(url, key):
            return "ready"
        el = int(time.time() - t0)
        if not ssh_checked and el >= SSH_GRACE:
            ssh_checked = True
            if not ssh_alive(ssh_host, ssh_port):
                logf("⚠️ sshd не отвечает — если модель не поднимется, посмотреть "
                     "/root/deploy.log будет нечем")
        if el - noted >= WAIT_NOTE:
            # сначала спрашиваем сам контейнер — он отвечает раньше, чем поднимется sshd;
            # ssh остаётся запасным путём на случай, если статус-порт не пробросился
            info = http_progress(ssh_host, status_port)
            if info is None and ssh_host and ssh_alive(ssh_host, ssh_port, timeout=4):
                info = remote_progress(ssh_host, ssh_port)
            text = progress_text(info, total_gib)
            if text:
                seen = True
                if text != last_text:
                    last_text, last_change = text, time.time()
            logf(f"ждём модель… {el // 60} мин — " +
                 (text or "установка движка + скачивание весов"))
            noted = el

            # замер реальной скорости на окне SPEED_WINDOW
            if net_mbps and info and info.get("stage") == "download" and info.get("bytes"):
                now, got = time.time(), info["bytes"]
                if mark is None or got < mark[1]:
                    mark = (now, got)
                elif now - mark[0] >= SPEED_WINDOW:
                    real = (got - mark[1]) * 8 / (now - mark[0]) / 1e6
                    if real < net_mbps * SPEED_MIN_RATIO:
                        logf(f"⛔ реальная скорость {real:.0f} Mbps против заявленных "
                             f"{net_mbps:.0f} — сервер негодный")
                        return "slow"
                    logf(f"скорость скачивания ≈{real:.0f} Mbps (заявлено {net_mbps:.0f})")
                    mark = (now, got)
        # Застой считаем только когда есть за чем следить: без источника прогресса
        # строка не меняется никогда, и выход по застою был бы ложным.
        if seen and time.time() - last_change > STALL_TIMEOUT:
            logf(f"⛔ за {STALL_TIMEOUT // 60} мин ничего не изменилось: {last_text}")
            return "stalled"
        time.sleep(30)
    return "stalled"


# Конфиг OpenCode можно положить в десяток мест, и они не заменяют друг друга, а
# СКЛАДЫВАЮТСЯ. Поэтому мало найти один файл: если в другом остался провайдер со старым
# адресом, после слияния может победить именно он. Обновляем все, какие есть.
# Источник: https://opencode.ai/docs/config/
# Имена, которые OpenCode перебирает в каталоге конфигов. config.json — старое имя,
# оно до сих пор в списке поиска и может перебить новое.
_OPENCODE_NAMES = ("opencode.json", "opencode.jsonc", "config.json")
# Комментарии в .jsonc: убираем только вне строк, иначе «https://…» режется пополам
_JSONC_RE = re.compile(r'"(?:\\.|[^"\\])*"|//[^\n]*|/\*.*?\*/', re.S)

# Идентификатор провайдера в конфиге. Совпадает с MODEL_NAME намеренно: в списке моделей
# OpenCode это выглядит как «uncensored/uncensored», и искать нужно одно слово.
OPENCODE_PROVIDER = MODEL_NAME
# Драйвер OpenAI-совместимого API. В схеме v1 поле называется npm, в v2 — package, и
# пакеты AI SDK там идут с префиксом «aisdk:» (https://opencode.ai/v2/docs/migrate-v1/).
OPENCODE_NPM = "@ai-sdk/openai-compatible"
OPENCODE_PKG_V2 = f"aisdk:{OPENCODE_NPM}"


def opencode_primary():
    """
    Файл, который OpenCode ТОЧНО прочитает. Именно его создаём, если конфига ещё нет.

    Важно: на Windows это НЕ %APPDATA%. В самом OpenCode путь считается как
    XDG_CONFIG_HOME или ~/.config на всех системах, ветки для %APPDATA% там нет —
    в AppData/Roaming/opencode живут плагины. Мы раньше создавали конфиг именно там,
    и у тех, у кого своего конфига ещё не было, модель в OpenCode просто не появлялась.
    """
    explicit = os.environ.get("OPENCODE_CONFIG")
    if explicit:
        return explicit
    # OPENCODE_CONFIG_DIR в OpenCode заменяет глобальный каталог целиком, а не дополняет
    base = os.environ.get("OPENCODE_CONFIG_DIR")
    if base:
        return os.path.join(base, "opencode.json")
    xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser(os.path.join("~", ".config"))
    return os.path.join(xdg, "opencode", "opencode.json")


def _opencode_dirs():
    """Каталоги, где может лежать конфиг. Нужны, чтобы подчистить устаревшие копии."""
    out, home = [], os.path.expanduser("~")
    explicit_dir = os.environ.get("OPENCODE_CONFIG_DIR")
    if explicit_dir:
        out.append(explicit_dir)
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        out.append(os.path.join(xdg, "opencode"))
    out.append(os.path.join(home, ".config", "opencode"))          # основной путь везде
    out.append(home)                                               # ~/opencode.json тоже читается
    if sys.platform == "darwin":
        out.append(os.path.join(home, "Library", "Application Support", "opencode"))
    for var in ("APPDATA", "LOCALAPPDATA"):      # каталог плагинов: сам OpenCode сюда не ходит,
        base = os.environ.get(var)               # но люди кладут конфиг и туда — обновим, чтобы
        if base:                                 # старый адрес не остался в чужом файле
            out.append(os.path.join(base, "opencode"))
    seen, uniq = set(), []
    for d in out:
        key = os.path.normcase(os.path.abspath(d))
        if key not in seen:
            seen.add(key)
            uniq.append(d)
    return uniq


def opencode_configs(create=True):
    """
    Какие файлы править. Первым всегда идёт тот, который OpenCode читает гарантированно
    (его и создаём, если его нет), дальше — все прочие найденные копии: конфиги
    складываются, и забытый провайдер со старым адресом может пережить обновление.
    """
    primary = opencode_primary()
    found = [primary] if (create or os.path.isfile(primary)) else []
    for d in _opencode_dirs():
        for name in _OPENCODE_NAMES:
            path = os.path.join(d, name)
            if os.path.isfile(path) and not any(os.path.normcase(os.path.abspath(path))
                                                == os.path.normcase(os.path.abspath(f))
                                                for f in found):
                found.append(path)
    # Проектные конфиги (opencode.json рядом с кодом) намеренно не трогаем: текущий
    # каталог у нашего UI — это папка самого софта, и туда уехал бы API-ключ, а до
    # проекта пользователя, где он реально запускает OpenCode, мы всё равно не дотянемся.
    return found


def opencode_config_path():
    """Главный конфиг — первый из найденных. Оставлено для совместимости и сообщений."""
    return opencode_configs()[0]


def _read_config(path, logf=log):
    """Читает .json или .jsonc. Из .jsonc комментарии придётся потерять — предупреждаем."""
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
    except OSError as e:
        logf(f"конфиг OpenCode {path} не читается ({e}) — переписываю заново")
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        pass
    cleaned = _JSONC_RE.sub(lambda m: m.group(0) if m.group(0).startswith('"') else "", raw)
    # висячая запятая в jsonc допустима: убираем её, скобку возвращаем на место
    cleaned = re.sub(r",(\s*[}\]])", r"\1", cleaned)
    try:
        cfg = json.loads(cleaned)
        logf(f"{os.path.basename(path)}: комментарии будут потеряны при записи")
        return cfg
    except ValueError as e:
        logf(f"конфиг OpenCode {path} не разобрать ({e}) — переписываю заново")
        return {}


def _opencode_managed():
    """
    Каталог «управляемого» конфига (для админов организации). OpenCode накладывает его
    ПОВЕРХ пользовательского, то есть он главнее нашего, а прав на запись туда у нас нет.
    Если там что-то лежит — можем только предупредить.
    """
    if sys.platform == "win32":
        base = os.environ.get("ProgramData")
        return os.path.join(base, "opencode") if base else None
    if sys.platform == "darwin":
        return "/Library/Application Support/opencode"
    return "/etc/opencode"


def _unhide_provider(cfg):
    """
    Снимает блокировки, из-за которых OpenCode не покажет провайдера, даже когда он
    прописан правильно. Возвращает список того, что пришлось починить.

    В схеме оба списка помечены deprecated, но пока действуют:
      * enabled_providers — когда задан, включены ТОЛЬКО перечисленные, все остальные
        игнорируются молча, без единого сообщения;
      * disabled_providers — перечисленные выключены.
    Сами мы эти ключи не ставим, так что сработает только у того, кто правил конфиг
    руками или по чужому гайду. Это третья по частоте причина «провайдера не видно»
    после неверного пути к конфигу и отсутствия limit у модели.
    """
    fixed = []
    dis = cfg.get("disabled_providers")
    if isinstance(dis, list) and OPENCODE_PROVIDER in dis:
        cfg["disabled_providers"] = [x for x in dis if x != OPENCODE_PROVIDER]
        fixed.append(f"«{OPENCODE_PROVIDER}» был в disabled_providers — убрал")
    en = cfg.get("enabled_providers")
    if isinstance(en, list) and OPENCODE_PROVIDER not in en:
        cfg["enabled_providers"] = [*en, OPENCODE_PROVIDER]
        fixed.append(f"задан enabled_providers без «{OPENCODE_PROVIDER}» — дописал, "
                     f"иначе включены только перечисленные")
    return fixed


def _sync_v2_provider(cfg, url, key, title, ctx):
    """
    Если конфиг уже переехал на схему v2 (ключ `providers`), обновляем и её.

    Сами такую секцию НЕ создаём. Формы нельзя смешивать: в v2-блоке с полем `npm`
    OpenCode молча теряет и npm, и baseURL, и запуск падает на «Unsupported package»
    (anomalyco/opencode#56816). Пишем v1-форму — её показывают официальные доки
    провайдеров, и она работает. Но если OpenCode сам мигрировал наш блок, оставить
    там прошлый адрес нельзя: `providers` — родной ключ v2, при расхождении победит он.

    Старый v1-блок при этом не удаляем: после синхронизации оба несут одинаковые адрес
    и ключ, так что чья бы форма ни победила при слиянии, результат верный. Удалить —
    значит сломать конфиг тому, кто откатится на прошлую версию OpenCode.
    """
    v2 = cfg.get("providers")
    prov = v2.get(OPENCODE_PROVIDER) if isinstance(v2, dict) else None
    if not isinstance(prov, dict):
        return False
    prov["package"] = OPENCODE_PKG_V2
    if title:
        prov["name"] = f"{title} (Clore)"
    settings = prov.setdefault("settings", {})
    settings["baseURL"] = url
    settings["apiKey"] = key
    prov.pop("disabled", None)
    # только поля, которые знает схема v2: лишние ключи она не переживает
    prov["models"] = {
        MODEL_NAME: {
            "name": title or MODEL_NAME,
            "limit": {"context": ctx, "output": max(4096, ctx // 4)},
        }
    }
    return True


def update_opencode(url, key, title=None, logf=log, ctx=None, engine=None):
    """
    Прописывает провайдера во все конфиги OpenCode, какие есть в системе. Именно во все:
    OpenCode складывает конфиги из нескольких мест, и забытый провайдер со старым адресом
    в соседнем файле может пережить обновление главного.
    """
    paths = opencode_configs()
    ctx = int(ctx or DEFAULT_CTX)
    # OPENCODE_CONFIG_DIR не дополняет глобальный конфиг, а заменяет его целиком:
    # если переменную выставил кто-то другой, правки в ~/.config молча не применятся
    shadow = os.environ.get("OPENCODE_CONFIG_DIR")
    if shadow:
        logf(f"внимание: задан OPENCODE_CONFIG_DIR={shadow} — OpenCode читает только его, "
             f"остальные конфиги игнорируются")
    # Конфиг в каталоге проекта перебивает глобальный. Проверить чужие проекты мы не можем,
    # но про файл в текущем каталоге предупредить обязаны: именно он чаще всего и побеждает.
    for name in _OPENCODE_NAMES:
        local = os.path.join(os.getcwd(), name)
        if os.path.isfile(local) and os.path.abspath(os.getcwd()) != os.path.abspath(HERE):
            logf(f"внимание: рядом с вами лежит {local} — OpenCode возьмёт провайдера "
                 f"оттуда, если запускать его в этом каталоге")
    # управляемый конфиг накладывается поверх пользовательского, а записать туда нечем
    managed = _opencode_managed()
    if managed and any(os.path.isfile(os.path.join(managed, n)) for n in _OPENCODE_NAMES):
        logf(f"внимание: есть управляемый конфиг в {managed} — он накладывается поверх "
             f"вашего и может перекрыть провайдера. Правится только с правами админа")
    written, unhidden, v2_synced = [], [], []
    for path in paths:
        cfg = {}
        if os.path.isfile(path):
            cfg = _read_config(path, logf)
        else:
            logf(f"конфига OpenCode не было — создаю {path}")
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
        prov = cfg.setdefault("provider", {}).setdefault(OPENCODE_PROVIDER, {})
        prov.setdefault("npm", OPENCODE_NPM)
        if title:
            prov["name"] = f"{title} (Clore)"
        opts = prov.setdefault("options", {})
        opts["baseURL"] = url
        opts["apiKey"] = key
        prov.pop("disabled", None)      # мог остаться от прошлой правки руками
        # Без секции models OpenCode не показывает провайдера вообще: baseURL и ключ он
        # принимает, но список моделей не запрашивает — их надо перечислить явно.
        # Идентификатор здесь тот же, под которым модель отдаёт сам движок.
        # limit обязателен: без context и output кастомный провайдер падает на валидации.
        prov["models"] = {
            MODEL_NAME: {
                "name": title or MODEL_NAME,
                "limit": {"context": ctx, "output": max(4096, ctx // 4)},
                "tool_call": True,
            }
        }
        # мало прописать провайдера — надо ещё снять то, что его прячет
        for msg in _unhide_provider(cfg):
            if msg not in unhidden:
                unhidden.append(msg)
        if _sync_v2_provider(cfg, url, key, title, ctx):
            v2_synced.append(path)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2, ensure_ascii=False)
        except OSError as e:
            logf(f"не смог записать {path}: {e}")
            continue
        written.append(path)
    if not written:
        logf("⚠️ ни один конфиг OpenCode не удалось обновить — пропишите провайдера вручную")
        return False
    OPENCODE_CFG_USED[:] = written
    logf(f"OpenCode переключён — его основной конфиг: {written[0]}")
    if len(written) > 1:
        logf("заодно обновлены другие найденные копии: " + ", ".join(written[1:]))
    for msg in unhidden:
        logf("провайдер был скрыт настройками: " + msg)
    if v2_synced:
        logf("конфиг на схеме v2 — секция providers обновлена тоже: " + ", ".join(v2_synced))
    logf(f"перезапустите OpenCode: провайдер «{OPENCODE_PROVIDER}», "
         f"модель «{OPENCODE_PROVIDER}/{MODEL_NAME}»")
    return True


# ---------- главный сценарий ----------
def deploy(cfg, env=None, logf=log):
    """
    cfg: model (ссылка/repo_id), ctx, max_price, currency, order_type, server_id,
         gpu_filter, image, gguf_file, force, dry_run, ssh_password
    Возвращает dict с результатом; бросает SystemExit/CloreError при фатальной ошибке.
    """
    env = env or load_env()
    clore = client(env)
    api_key = env.get("API_KEY") or ""
    # цены везде в USD; валюта списания выбирается автоматически, если не задана явно
    forced_currency = cfg.get("currency") if cfg.get("currency") in CURRENCIES else None
    exclude_hosts = [p for p in (cfg.get("exclude_hosts")
                                 or env.get("EXCLUDE_HOSTS")
                                 or EXCLUDE_HOSTS_DEFAULT).split(",") if p.strip()]
    if exclude_hosts:
        logf("исключённые регионы (по имени хоста): " + ", ".join(exclude_hosts))
    order_type = cfg.get("order_type") or "on-demand"

    # уже есть живой заказ?
    if not cfg.get("force") and env.get("ORDER") and env.get("API_URL"):
        if api_alive(env["API_URL"], api_key):
            logf(f"заказ {env['ORDER']} уже работает: {env['API_URL']} (--force чтобы арендовать ещё)")
            return {"status": "already-running", "api_url": env["API_URL"], "order": env["ORDER"]}

    ref = cfg.get("model") or env.get("MODEL_REPO")
    if not ref:
        raise SystemExit("не выбрана модель: укажите её в UI или передайте --model owner/name")
    logf(f"разбираю модель {ref}…")
    info = hfmodels.model_info(ref, ctx=cfg.get("ctx"), token=env.get("HF_TOKEN") or None)
    # пользователь мог выбрать не тот квант, что стоит по умолчанию, — тогда и размер весов,
    # и требования к серверу другие. Без этого сервер подбирался бы под дефолтный квант.
    chosen = cfg.get("gguf_file")
    if chosen:
        opt = next((o for o in info.get("gguf_options") or [] if o["path"] == chosen), None)
        if opt:
            info.update(weights_gib=opt["weights_gib"], vram_gib=opt["vram_gib"],
                        disk_gb=opt["disk_gb"], gguf_file=opt["path"])
            logf(f"квант выбран вручную: {opt['label'].split('/')[-1]}")
        else:
            logf(f"⚠️ кванта {chosen} нет в репозитории — беру по умолчанию")
            cfg = {**cfg, "gguf_file": None}
    logf(f"{info['repo_id']}: формат {info['format']}, движок {info['engine']}, "
         f"веса {info['weights_gib']} GiB, нужно ~{info['vram_gib']} GiB VRAM, контекст {info['ctx']}")
    if info["gated"] and not env.get("HF_TOKEN"):
        logf("⚠️ репозиторий gated, а HF_TOKEN не задан — скачивание упадёт")

    rates = clore.currency_rates()
    balances = wallet_balances(clore)
    bal_usd = sum(amount * rates.get(cur, 0) for cur, amount in balances.items())
    logf("баланс Clore: " + (", ".join(f"{amount:g} {cur}" for cur, amount in balances.items() if amount)
                             or "пусто") + f" (≈ ${bal_usd:.2f})")

    # Выгрузка экспертов в оперативку: работает только у llama.cpp и только для MoE.
    # Требование к карте падает в разы, зато появляется требование к объёму RAM.
    offload = info.get("cpu_moe") if info["engine"] == hfmodels.ENGINE_LLAMACPP else None
    cpu_moe = bool(cfg.get("cpu_moe")) and bool(offload)
    need_vram, need_ram = info["vram_gib"], 0
    if cpu_moe:
        need_vram, need_ram = offload["vram_gib"], offload["ram_gb"]
        logf(f"эксперты можно держать в оперативке: VRAM {info['vram_gib']} -> "
             f"{need_vram} GiB, зато нужно {need_ram} GB RAM "
             f"({info['moe']['experts']} экспертов, активны {info['moe']['active']})")
    elif cfg.get("cpu_moe"):
        logf("выгрузка экспертов не применима: нужна MoE-модель в GGUF (движок llama.cpp)")

    offers = find_offers(clore, need_vram, info["disk_gb"], cfg.get("max_price_usd"),
                         forced_currency, cfg.get("gpu_filter"), order_type,
                         download_gib=info["weights_gib"],
                         engine=info["engine"], quant=info.get("precision"),
                         allow_old_gpu=bool(cfg.get("allow_old_gpu")),
                         balance_usd=bal_usd or None, spread=bool(cfg.get("spread")),
                         need_ram=need_ram, cpu_moe=cpu_moe)
    if cfg.get("server_id"):
        sid = int(cfg["server_id"])
        offers = [o for o in offers if int(o["id"]) == sid] or \
                 find_offers(clore, 0, 0, None, forced_currency, None, order_type,
                             min_net=0, download_gib=info["weights_gib"], limit=10_000,
                             engine=info["engine"], quant=info.get("precision"),
                             allow_old_gpu=True)
        offers = [o for o in offers if int(o["id"]) == sid]
        for o in offers:
            if not o.get("gpu_ok"):
                logf(f"⚠️ сервер {sid} выбран вручную, но {o['gpu_note']}")
    if not offers:
        ceiling = bal_usd or 0
        msg = (f"на Clore нет подходящих серверов: нужно ≥{info['vram_gib']} GiB VRAM, "
               f"≥{info['disk_gb']} GB диска" +
               (f", цена ≤ ${cfg['max_price_usd']}/ч" if cfg.get("max_price_usd") else "") +
               (f", GPU содержит «{cfg['gpu_filter']}» — снимите фильтр, если таких машин нет"
                if cfg.get("gpu_filter") else "") +
               (". Карты, на которых vLLM не запускается (старее Ampere), отсеяны"
                if info["engine"] == hfmodels.ENGINE_VLLM else "") + ". "
               f"Баланс ${bal_usd:.2f}: оплата идёт по часам, поэтому отсеяны только "
               f"серверы дороже ${ceiling:.2f}/ч — на них не хватит и на час")
        tg(env, "⛔ " + msg)
        raise SystemExit(msg)

    for o in offers[:TRY_OFFERS]:
        logf(f"кандидат {o['id']}: {o['gpu_count']}x {o['gpu']}"
             f"{' (' + o['arch'] + ')' if o.get('arch') else ''} ({o['vram_gb']} GiB VRAM) "
             f"${o['price_used']:.3f}/ч, диск {o['disk_gb']} GB, {o['net_down']:.0f} Mbps")
    if cfg.get("dry_run"):
        return {"status": "dry-run", "model": info, "offers": offers}

    script, order_env = render_onstart(info, env, ctx=info["ctx"], gguf_file=cfg.get("gguf_file"))
    command = build_command(script)
    image = cfg.get("image") or IMAGES[info["engine"]]
    ssh_password = cfg.get("ssh_password") or api_key[:32]
    # entrypoint Clore читает пароль из переменной SSH_PASSWORD, поля заказа ему мало
    order_env["SSH_PASSWORD"] = ssh_password
    pubkey = ssh_pubkey(env)
    if pubkey:
        order_env["SSH_KEY"] = pubkey
        logf("в заказ добавлен ваш публичный ключ — вход по ssh без пароля")
    else:
        logf(f"публичного ключа нет, вход по паролю: {ssh_password}")

    tried = 0
    for offer in offers[:TRY_OFFERS]:
        tried += 1
        sid = offer["id"]
        currency, amount, usd = pick_currency(offer, balances, rates, forced_currency)
        if not amount:
            logf(f"сервер {sid}: нет цены в валюте {currency} — беру следующий")
            continue
        if balances.get(currency, 0) <= 0:
            logf(f"сервер {sid}: принимает только {currency}, а на этом кошельке пусто — "
                 f"беру следующий")
            continue
        # Выгружаем минимум необходимого: каждый слой в оперативке — это работа для CPU.
        # На большой карте выгрузка может не понадобиться вовсе, даже если включена.
        card_gib = offer["vram_gb"] / max(offer["gpu_count"], 1)
        n_cpu_moe = (hfmodels.cpu_moe_layers(offload, info["vram_gib"], offer["vram_gb"],
                                             card_gib=card_gib) if cpu_moe else 0)
        order_env["N_CPU_MOE"] = str(n_cpu_moe)
        if n_cpu_moe:
            logf(f"эксперты {n_cpu_moe} слоёв из {offload['max_layers']} уедут в оперативку "
                 f"({offer['vram_gb']} GiB VRAM против {info['vram_gib']} нужных)")
        elif cpu_moe:
            logf(f"карта вмещает модель целиком — выгрузка не нужна")
        # Контекст задают до выбора сервера, поэтому он может в карту и не влезть.
        # Подгоняем под фактическую VRAM: иначе OOM через полчаса после начала скачивания.
        gpu_weights = info["weights_gib"] - (n_cpu_moe * offload["per_layer_gib"]
                                             if n_cpu_moe else 0)
        fit_ctx = hfmodels.ctx_for_vram(info["ctx"], info["kv_gib"], gpu_weights,
                                        offer["vram_gb"] / VRAM_HEADROOM,
                                        info.get("vocab") or 0)
        order_env["CTX"] = str(fit_ctx)
        fit_need = hfmodels.vram_estimate(gpu_weights, info["kv_gib"] * fit_ctx / info["ctx"],
                                          info.get("vocab") or 0)
        if fit_need > offer["vram_gb"]:
            # даже на минимальном контексте не влезает: бывает только при ручном выборе
            # сервера, фильтр такие предложения не пропускает
            logf(f"⚠️ модель не влезает в {offer['vram_gb']} GiB VRAM ни при каком контексте "
                 f"(нужно {fit_need} GiB даже при {fit_ctx}) — запуск, скорее всего, упадёт по OOM")
        elif fit_ctx < info["ctx"]:
            logf(f"⚠️ контекст {info['ctx']} не влезает в {offer['vram_gb']} GiB VRAM — "
                 f"ставлю {fit_ctx}. Нужен полный контекст — берите карту крупнее")
        logf(f"арендую сервер {sid} ({offer['gpu_count']}x {offer['gpu']}) за ${usd:.3f}/ч "
             f"(списание {amount:g} {currency}/сутки)")
        # Запоминаем, какие заказы на этом сервере уже есть: иначе чужой (или свой прошлый)
        # заказ на том же сервере будет принят за только что созданный
        try:
            before = {int(o.get("id", -1)) for o in clore.my_orders()}
        except CloreError:
            before = set()
        try:
            clore.create_order(
                server_id=sid, image=image, currency=currency, order_type=order_type,
                ports={22: "tcp", API_PORT: "tcp", STATUS_PORT: "tcp"},
                env=order_env, command=command,
                ssh_password=ssh_password, ssh_key=pubkey,
                # обычный образ (nvidia/cuda) не содержит sshd и долгоживущего процесса —
                # без entrypoint Clore контейнер не держится и SSH недоступен
                autossh_entrypoint=True,
                spot_price=amount if order_type == "spot" else None,
                required_price=amount,
            )
        except CloreError as e:
            logf(f"сервер {sid}: {e} — беру следующий")
            # HTTP 500 не зависит от сервера (чаще всего слишком длинная команда заказа) —
            # перебирать остальных кандидатов бессмысленно, только плодим мусорные попытки
            if getattr(e, "code", None) == 1 and "HTTP 500" in str(e):
                raise SystemExit("Clore отвечает HTTP 500 на создание заказа — проблема не "
                                 "в сервере. Чаще всего это слишком длинная команда заказа")
            continue

        # Заказ уже создан и тарифицируется. Любая сетевая ошибка дальше не должна
        # оставить его сиротой: записываем номер сервера в .env сразу, ещё до опроса,
        # чтобы stop.py смог отменить заказ даже если всё остальное упадёт.
        save_env_keys({"SERVER": str(sid)})
        time.sleep(POLL)
        found = None
        for _ in range(6):
            try:
                orders = [o for o in clore.my_orders()
                          if int(o.get("si", -1)) == int(sid) and not o.get("expired")
                          and int(o.get("id", -1)) not in before]
            except CloreError as e:
                logf(f"не смог прочитать список заказов ({e}) — повторяю")
                time.sleep(POLL)
                continue
            if orders:
                found = max(orders, key=lambda o: o.get("ct") or 0)
                save_env_keys({"ORDER": str(found["id"])})
                break
            time.sleep(POLL)
        if not found:
            logf(f"⚠️ заказ на сервер {sid} создан, но не появился в my_orders. "
                 f"Он может тарифицироваться: проверьте вкладку «Заказы» и отмените вручную")
            continue

        oid = found["id"]
        logf(f"заказ {oid} создан на сервере {sid}")
        tg(env, f"🚀 Clore: заказ {oid}, {offer['gpu_count']}x {offer['gpu']} "
                f"({offer['vram_gb']} GiB VRAM), ${usd:.3f}/ч (списание {amount:g} {currency}).\n"
                f"Ставлю {info['engine']} и качаю {info['repo_id']} ({info['weights_gib']} GiB).")

        ready = wait_order_ready(clore, oid, logf)
        if not ready:
            logf(f"заказ {oid} не выдал адрес за {ORDER_TIMEOUT // 60} мин — отменяю")
            try:
                clore.cancel_order(oid, "сервер не стартовал")
            except CloreError as e:
                logf(f"отмена не удалась: {e}")
            continue

        order, acc = ready
        host = acc["host"]
        # регион виден только сейчас — раньше Clore его не сообщает
        hit = host_excluded(host, exclude_hosts)
        if hit:
            logf(f"сервер {sid} оказался в исключённом регионе ({host} содержит «{hit}») — "
                 f"отменяю заказ {oid} и беру следующий")
            for _ in range(3):
                try:
                    clore.cancel_order(oid)
                    break
                except CloreError as e:
                    logf(f"отмена не прошла ({e}) — повтор")
                    time.sleep(10)
            save_env_keys({"ORDER": "", "SERVER": "", "API_URL": "", "SSH": ""})
            continue
        api_url = f"http://{host}:{acc['ports'][API_PORT]}/v1"
        ssh = f"ssh -p {acc['ports'].get(22, 22)} root@{host}"
        save_env_keys({"ORDER": str(oid), "SERVER": str(sid), "API_URL": api_url, "SSH": ssh,
                       "MODEL_REPO": info["repo_id"], "MODEL_NAME": MODEL_NAME,
                       "CTX": str(fit_ctx), "ENGINE": info["engine"]})
        logf(f"контейнер поднят. API будет на {api_url}; {ssh} (пароль: {ssh_password})")

        sign = wait_alive(host, acc["ports"], api_url, api_key, logf)
        if not sign:
            drop_server(clore, oid, sid,
                        f"контейнер молчал {SILENT_LIMIT // 60} мин после аренды", logf)
            continue
        logf(f"сервер отвечает ({sign})")

        # Фиксированных 90 минут не хватает большим моделям: 150+ ГиБ по реальному каналу
        # качаются полтора часа, и деплой падал по таймауту, уже почти доехав.
        api_timeout = max(API_TIMEOUT, int(offer.get("download_hours", 0) * 3600 * 2.5) + 30 * 60)
        if api_timeout > API_TIMEOUT:
            logf(f"модель большая — жду до {api_timeout // 60} мин вместо {API_TIMEOUT // 60}")
        # Сравниваем именно с «ready». Тут стояло `if not wait_api(...)`, а функция
        # возвращает строку: и "ready", и "slow", и "stalled" — непустые, то есть
        # истинные. Ветка отказа не исполнялась ни разу с самого первого коммита:
        # любой деплой рапортовал успех, прописывал мёртвый адрес в OpenCode и не слал
        # ни одного предупреждения в Telegram. Снаружи это выглядело как «✅ модель
        # готова» сразу после «⛔ за 30 мин ничего не изменилось».
        outcome = wait_api(api_url, api_key, logf, timeout=api_timeout,
                           ssh_host=host, ssh_port=acc["ports"].get(22, 22),
                           total_gib=info.get("weights_gib"),
                           status_port=acc["ports"].get(STATUS_PORT))
        if outcome == "slow":
            # канал оказался в разы медленнее заявленного — сервер негодный,
            # его не разбирают, а меняют на следующего кандидата
            drop_server(clore, oid, sid, "скачивание в разы медленнее заявленного", logf)
            continue
        if outcome != "ready":
            tg(env, f"⛔ Заказ {oid}: деплой остановлен — прогресс встал более чем на "
                    f"{STALL_TIMEOUT // 60} мин (или вышел общий лимит {api_timeout // 60} мин). "
                    f"Сервер оставлен для разбора: {ssh}\nЛоги: /root/deploy.log, /root/download.log, /root/engine.log")
            logf(f"⛔ деплой не удался ({outcome}). Сервер оставлен для разбора: {ssh}")
            return {"status": "timeout", "order": oid, "api_url": api_url, "ssh": ssh}

        speed = bench(api_url, api_key)
        if n_cpu_moe:
            # С экспертами в оперативке первый замер нечестно низкий: файл ещё не весь
            # в страничном кэше, и часть экспертов читается с диска. Замерено на живом
            # сервере: 8 ток/с сразу после старта против 19 через минуту прогрева.
            speed = max(speed or 0, bench(api_url, api_key) or 0) or None
        # при пакетных прогонах переключать OpenCode на каждую проверяемую модель незачем
        if not cfg.get("skip_opencode"):
            update_opencode(api_url, api_key, info["repo_id"].split("/")[-1], logf,
                            ctx=fit_ctx, engine=info["engine"])
        # Баланс нужен только для справочной строки. Раньше разовый сбой сети здесь ронял
        # полностью удавшийся деплой: модель уже работала, а в лог уходило «⛔ ошибка API»
        # вместо адреса и скорости.
        try:
            balances = wallet_balances(clore)
            bal_usd = sum(amount * rates.get(cur, 0) for cur, amount in balances.items())
        except CloreError as e:
            logf(f"баланс не прочитался ({e}) — на результат деплоя это не влияет")
            bal_usd = 0.0
        hours = f"{bal_usd / usd:.0f} ч" if bal_usd and usd else "?"
        msg = [f"✅ {info['repo_id']} готова",
               f"Base URL: {api_url}", f"Модель в API: {MODEL_NAME}", f"Движок: {info['engine']}"]
        if speed:
            msg.append(f"Скорость: {speed:.0f} ток/с")
        msg += [f"Сервер: {offer['gpu_count']}x {offer['gpu']}, ${usd:.3f}/ч "
                f"(списание {amount:g} {currency})",
                f"SSH: {ssh}", f"Баланс: ≈${bal_usd:.2f} (~{hours})"]
        if not cfg.get("skip_opencode"):
            msg.append("OpenCode переключён (" + ", ".join(OPENCODE_CFG_USED)
                       + ") — перезапустите его.")
        # итог нужен в логе, а не только в Telegram: бот настроен далеко не у всех,
        # а без него от всего прогона оставалось одно слово «готово»
        for line in msg:
            logf(line)
        tg(env, "\n".join(msg))
        return {"status": "ready", "order": oid, "server": sid, "api_url": api_url, "ssh": ssh,
                "model": info, "speed": speed, "price_usd": usd, "pay_amount": amount,
                "currency": currency, "balance_usd": round(bal_usd, 2), "offer": offer}

    tg(env, f"⛔ Не удалось арендовать ни один из {tried} серверов Clore.")
    raise SystemExit(f"не удалось арендовать ни один из {tried} серверов")


# ---------- CLI ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", help="ссылка на HF или owner/name")
    ap.add_argument("--ctx", type=int, help="контекст (по умолчанию из конфига модели)")
    ap.add_argument("--max-price", type=float, help="потолок цены в долларах за час, например 1.5")
    ap.add_argument("--currency", default="auto",
                    choices=["auto"] + CURRENCIES,
                    help="валюта списания с баланса Clore; auto — самая дешёвая из доступных")
    ap.add_argument("--order-type", default="on-demand", choices=["on-demand", "spot"])
    ap.add_argument("--server", type=int, help="арендовать конкретный сервер Clore по id")
    ap.add_argument("--gpu", help="фильтр по названию GPU, например 4090")
    ap.add_argument("--gguf-file", help="какой квант брать из GGUF-репозитория "
                                        "(путь внутри репозитория; по умолчанию Q4_K_M)")
    ap.add_argument("--image", help="переопределить docker-образ")
    ap.add_argument("--no-opencode", action="store_true",
                    help="не переключать OpenCode на эту модель (для пакетных проверок)")
    ap.add_argument("--allow-old-gpu", action="store_true",
                    help="не отсеивать карты старее Ampere")
    ap.add_argument("--exclude-hosts",
                    help="не брать серверы, в имени хоста которых есть эти подстроки "
                         "(через запятую, например «.ru,msk»). Регион Clore сообщает "
                         "только после аренды, поэтому такой заказ сразу отменяется")
    ap.add_argument("--spread", action="store_true",
                    help="брать случайный сервер из близких по цене, а не всегда самый "
                         "дешёвый — нужно, когда деплоев несколько одновременно")
    ap.add_argument("--cpu-moe", action="store_true",
                    help="у MoE-модели держать экспертов в оперативке (llama.cpp): "
                         "карта нужна в разы скромнее, скорость ниже на проценты")
    ap.add_argument("--force", action="store_true", help="арендовать ещё один сервер")
    ap.add_argument("--dry-run", action="store_true", help="только показать кандидатов")
    args = ap.parse_args()

    env = load_env()
    if not env.get("CLORE_API_KEY"):
        raise SystemExit("в .env нет CLORE_API_KEY — задайте его в UI (ui.bat) или вручную")
    res = deploy({"model": args.model, "ctx": args.ctx, "max_price_usd": args.max_price,
                  "currency": args.currency, "order_type": args.order_type,
                  "server_id": args.server, "gpu_filter": args.gpu, "image": args.image,
                  "gguf_file": args.gguf_file, "skip_opencode": args.no_opencode,
                  "allow_old_gpu": args.allow_old_gpu, "spread": args.spread,
                  "cpu_moe": args.cpu_moe,
                  "exclude_hosts": args.exclude_hosts,
                  "force": args.force, "dry_run": args.dry_run}, env)
    return 0 if res.get("status") in ("ready", "already-running", "dry-run") else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except (CloreError, SystemExit) as e:
        if isinstance(e, SystemExit) and e.code in (0, None):
            raise
        log(f"⛔ {e}")
        sys.exit(1)
    except Exception as e:  # noqa
        try:
            tg(load_env(), f"⛔ deploy.py упал: {e!r}")
        finally:
            raise
