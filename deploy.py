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
import contextlib
import gzip
import json
import os
import random
import re
import secrets
import subprocess
import sys
import threading
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
# Сколько серверов можно выбрать руками. Выбранные образуют очередь: не поднялся первый —
# деплой идёт ко второму и так далее, не возвращаясь к автоподбору.
MAX_PICK_SERVERS = 10
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
# Сколько ждать, если на сервере ничего не меняется. Меряем не строку прогресса, а «пульс»:
# скачанные байты + рост логов сборки и движка + занятая видеопамять. Сборка llama.cpp и
# загрузка весов в VRAM идут по 10+ минут без смены стадии, но пульс у них есть всё время,
# поэтому 5 минут полной тишины — это зависание, а не долгая работа.
STALL_TIMEOUT = 5 * 60
# Сколько терпим полное отсутствие ответов о прогрессе. Больше застоя: статус-порт
# поднимается только после установки python3, а до того Clore до 10 минут ставит своё
BLIND_TIMEOUT = 20 * 60
# Здоровый хост Clore отвечает по ssh через минуту-две после старта контейнера. Если за
# это время нет ни ssh, ни статус-порта, ни API — контейнер не поднялся, и ждать его час
# бессмысленно: он всё это время тарифицируется.
SILENT_LIMIT = 8 * 60
# Во сколько раз реальная скорость скачивания может быть ниже заявленной Clore, прежде чем
# сервер считается негодным. Хост обещал 335 Mbps, реально качал 20 — веса ехали 1 час 50
# минут вместо шести минут, и деплой упёрся в таймаут.
SPEED_MIN_RATIO = 0.5
SPEED_CLAIM_CAP = 200           # Mbps: выше этого заявленную скорость не требуем
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
_real_sleep = time.sleep


def load_env():
    env = {}
    if not os.path.exists(ENV_PATH):
        return env
    for attempt in range(40):
        try:
            with open(ENV_PATH, encoding="utf-8") as f:
                text = f.read()
            break
        except PermissionError:
            # Windows: файл в этот миг подменяет save_env_keys. Вернуть пустой словарь
            # нельзя — вызывающий решил бы, что ключей и заказов нет
            if attempt == 39:
                raise
            _real_sleep(0.05)
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


_ENV_LOCK = threading.RLock()


@contextlib.contextmanager
def _env_guard():
    """
    Запись .env — это «прочитал, изменил, записал». Без блокировки два писателя (поток
    деплоя и сохранение настроек в UI, либо UI и stop.py) затирали изменения друг друга:
    терялся номер заказа, а с ним и возможность его отменить. Поток — через RLock,
    соседний процесс — через файл-замок.
    """
    with _ENV_LOCK:
        lockp, fd, t0 = ENV_PATH + ".lock", None, time.monotonic()
        while True:
            try:
                fd = os.open(lockp, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError:
                try:
                    if time.time() - os.path.getmtime(lockp) > 10:   # замок упавшего процесса
                        os.remove(lockp)
                        continue
                except OSError:
                    pass
                if time.monotonic() - t0 > 15:
                    break                    # лучше записать без замка, чем не записать
                _real_sleep(0.05)
            except OSError:
                break
        try:
            yield
        finally:
            if fd is not None:
                os.close(fd)
                try:
                    os.remove(lockp)
                except OSError:
                    pass


def save_env_keys(updates: dict):
    with _env_guard():
        _save_env_keys(updates)


def _save_env_keys(updates: dict):
    # Значение — всегда одна строка. Перевод строки внутри него дописал бы в .env
    # посторонний ключ: так через поле настроек подменялся бы, например, API_URL.
    updates = {k: re.sub(r"[\r\n]+", " ", str(v)).strip() for k, v in updates.items()}
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
    # Пишем во временный файл и подменяем: читатель никогда не увидит полфайла, а сбой
    # посреди записи не оставит .env без ключей
    text = "\n".join(lines).rstrip("\n") + "\n"
    tmp = ENV_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    for n in range(20):
        try:
            os.replace(tmp, ENV_PATH)
            return
        except OSError:          # Windows: файл в этот миг читает другой поток
            _real_sleep(0.05)
    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.write(text)
    try:
        os.remove(tmp)
    except OSError:
        pass


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
    # Всё ниже подставляется в текст bash-скрипта, который выполнится от root на
    # арендованном сервере. Часть значений приходит из чужого репозитория (метод
    # квантизации из config.json, имя GGUF-файла), и без проверки автор модели мог
    # вписать туда свои команды: там лежат ваши API_KEY и HF_TOKEN.
    for label, value, rx in (
            ("имя репозитория", info["repo_id"], r"[A-Za-z0-9][\w.-]*/[\w.-]+"),
            ("метод квантизации из config.json", info.get("quantization") or "", r"[A-Za-z0-9_.-]{0,40}"),
            ("парсер вызовов инструментов", info.get("tool_parser") or "", r"[A-Za-z0-9_.-]{0,40}"),
            ("формат весов", info["format"], r"[A-Za-z0-9_.-]{1,20}"),
            ("имя GGUF-файла", gguf, r"(?:[\w.+@=-]+/)*[\w.+@=-]+\.gguf|"),
            ("ключ вашего API", api_key, r"[A-Za-z0-9_.:@=+/-]{1,200}")):
        if not re.fullmatch(rx, str(value), re.ASCII) or ".." in str(value):
            raise SystemExit(f"недопустимое значение — {label}: {str(value)[:80]!r}. Разрешены "
                             f"только латинские буквы, цифры и символы _ . - (в пути ещё /). "
                             f"Такое значение в скрипт сервера не подставляю")
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


def picked_servers(cfg):
    """
    Выбранные вручную серверы в порядке выбора — это очередь попыток, а не список
    равноправных вариантов: первый не поднялся, берём второй. Принимает и server_ids
    (список из UI, строка с запятыми из CLI), и одиночный server_id старого формата.
    """
    raw = cfg.get("server_ids") or cfg.get("server_id") or []
    if isinstance(raw, (str, int)):
        raw = re.split(r"[\s,]+", str(raw))
    out = []
    for v in raw:
        try:
            sid = int(str(v).strip())
        except (TypeError, ValueError):
            continue
        if sid > 0 and sid not in out:   # повтор в очереди бессмыслен
            out.append(sid)
    return out[:MAX_PICK_SERVERS]


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


def wait_order_ready(clore, order_id, logf=log, timeout=ORDER_TIMEOUT, stop=None):
    """Ждёт, пока у заказа появятся адрес и проброшенные порты."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        check_stop(stop)
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
    'echo "beat=$(cat /root/deploy.log /root/download.log 2>/dev/null | wc -c)"; '
    # процессорное время контейнера шагом ~10 с — см. status.py в _common.sh
    "echo cpu=$(cat /proc/[0-9]*/stat 2>/dev/null | awk '{s+=$14+$15} END{print int(s/1000)}'); "
    'if [ "$(cat /root/.stage 2>/dev/null)" = failed ]; then echo failed; el'
    'if pgrep -x llama-server >/dev/null 2>&1 || pgrep -f "[v]llm serve" >/dev/null 2>&1; '
    'then echo engine; '
    'elif pgrep -f "[h]f download" >/dev/null 2>&1; '
    'then echo "download $(du -smc /root/models /root/tabbyAPI/models 2>/dev/null '
    '| tail -n 1 | cut -f1)"; '
    'elif [ -s /root/engine.log ]; then echo engine; '
    'else echo setup; fi'
)


_STAGE_TEXT = {
    "packages": "ставлю пакеты (ждём, пока Clore доустановит свои)",
    "download": "качаю веса",
    "engine": "движок запущен, загружает веса в VRAM",
    "failed": "установка на сервере завершилась ошибкой",
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
        return {"stage": str(data.get("stage") or "setup"), "bytes": int(data.get("bytes") or 0),
                "beat": int(data.get("beat") or 0), "gpu": int(data.get("gpu") or 0),
                "cpu": int(data.get("cpu") or 0), "fail": str(data.get("fail") or "")[:300]}
    except Exception:  # noqa — любой сбой просто означает «пока не знаем»
        return None


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
    pulse = {"beat": 0, "cpu": 0}
    while out and out[0].split("=")[0] in pulse:
        k, _, v = out.pop(0).partition("=")
        try:
            pulse[k] = int(v)
        except ValueError:
            pass
    if not out:
        return None
    if out[0] in ("engine", "setup", "failed"):
        return {"stage": out[0], "bytes": 0, **pulse}
    if out[0] == "download":
        try:
            return {"stage": "download", "bytes": int(out[1]) * 1024 * 1024, **pulse}  # du -sm — МиБ
        except (IndexError, ValueError):
            return {"stage": "download", "bytes": 0, **pulse}
    return None


def remote_tail(host, port, lines=12, timeout=15):
    """Хвост логов сервера перед отменой: после отмены контейнер со всеми логами исчезает."""
    if not host or not port:
        return ""
    cmd = (f"tail -n {int(lines)} /root/engine.log 2>/dev/null; echo ---; "
           f"tail -n {int(lines)} /root/deploy.log 2>/dev/null")
    try:
        return subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
             "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR",
             "-o", f"ConnectTimeout={timeout}", "-p", str(port), f"root@{host}", cmd],
            capture_output=True, text=True, timeout=timeout + 15,
            encoding="utf-8", errors="replace").stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


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


class DeployStopped(Exception):
    """Деплой прерван снаружи: заказ отменили руками, пока он поднимался."""


def check_stop(stop):
    """stop — функция без аргументов; истина означает «заказ отменён, дальше не идём»."""
    if stop and stop():
        raise DeployStopped("деплой остановлен: заказ отменён вручную")


def cancel_order_safe(clore, order_id, reason=None, logf=log, attempts=4, pause=10):
    """
    Отменяет заказ и возвращает None, когда он точно больше не тарифицируется: отмена
    прошла либо заказа уже нет среди активных (Clore гасит их и сам). Иначе возвращает
    последнюю ошибку — и тогда ссылку на заказ стирать НЕЛЬЗЯ: он продолжает списывать
    деньги, а без номера в .env его не найдёт ни UI, ни stop.py.
    """
    err = None
    for n in range(attempts):
        try:
            clore.cancel_order(order_id, reason)
            return None
        except Exception as e:  # noqa — отмена это деньги: ни одна ошибка не обрывает повторы
            err = e
        try:
            order = clore.order(order_id)
            if order is None or order.get("expired"):
                logf(f"заказ {order_id} уже неактивен на стороне Clore — отменять нечего")
                return None
        except Exception:  # noqa
            pass
        if n < attempts - 1:
            logf(f"отмена заказа {order_id} не прошла ({err}) — повтор {n + 1}/{attempts - 1}")
            time.sleep(pause)
    return err


def cancel_or_halt(clore, env, order_id, reason, logf=log):
    """
    Отмена перед переходом к следующему серверу. Не получилось — деплой останавливается:
    арендовать второй сервер, пока первый ещё оплачивается, нельзя.
    """
    err = cancel_order_safe(clore, order_id, reason[:64] if reason else None, logf)
    if err is None:
        return
    msg = (f"заказ {order_id} не удалось отменить ({err}) — он продолжает оплачиваться. "
           f"Номер оставлен в .env: отмените его кнопкой «Отменить заказ» или stop.py. "
           f"К следующему серверу не перехожу")
    tg(env, "⛔ " + msg)
    raise SystemExit(msg)


def drop_server(clore, env, order_id, server_id, reason, logf=log, **extra):
    """Отменить заказ, занести сервер в чёрный список и очистить состояние в .env."""
    logf(f"⛔ сервер {server_id}: {reason} — отменяю заказ {order_id} и беру другой")
    blacklist_add(server_id, reason, **extra)
    cancel_or_halt(clore, env, order_id, reason, logf)
    save_env_keys({"ORDER": "", "SERVER": "", "API_URL": "", "SSH": ""})


def wait_alive(host, ports, url, key, logf=log, limit=SILENT_LIMIT, stop=None):
    """
    Подал ли контейнер хоть какой-то признак жизни: ssh, статус-порт или уже готовый API.
    У здорового хоста Clore это происходит за минуту-две. Молчание дольше limit означает,
    что контейнер не стартовал — такой сервер лучше сразу отдать обратно, чем ждать час,
    платя за него и не имея возможности даже посмотреть логи.
    """
    t0, noted = time.time(), 0
    while time.time() - t0 < limit:
        check_stop(stop)
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
             total_gib=None, status_port=None, net_mbps=None, stop=None, report=None):
    """
    Ждёт готовности модели. Возвращает "ready" / "stalled" / "stalled-engine" / "slow" /
    "failed" (установочный скрипт сам сообщил об ошибке) / "blind" (нет связи с сервером).

    Сдаёмся не по общему времени, а по застою: общий лимит считался от заявленной Clore
    скорости сети, а она врёт. На хосте с обещанными 335 Mbps веса реально качались со
    скоростью 20 Mbps, 1 час 50 минут — деплой объявил таймаут за 45 минут до того, как
    модель поднялась, а сервер всё это время работал и тратил деньги.

    Отдельно следим за самой скоростью: если реальная меньше заявленной в 1/SPEED_MIN_RATIO
    раз, ждать бессмысленно — такой сервер возвращаем и берём другой.
    """
    t0, noted, ssh_checked = time.time(), 0, ssh_host is None
    last_text, last_change, seen = None, time.time(), False
    last_sig = None                  # пульс: (стадия, байты, рост логов, VRAM, CPU)
    last_poll = last_contact = time.time()
    mark = None                      # (время, байт) — точка отсчёта для замера скорости
    prev = None                      # (время, байт) прошлого опроса — для текущей скорости
    while time.time() - t0 < timeout:
        check_stop(stop)
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
            now = time.time()
            if info:
                seen = True
                last_contact = now
                sig = (info.get("stage"), info.get("bytes"), info.get("beat"), info.get("gpu"),
                       info.get("cpu"))
                if sig != last_sig:
                    last_sig, last_change = sig, now
                last_text = text or last_text
                if info.get("stage") == "failed":
                    logf("⛔ установка на сервере завершилась ошибкой"
                         + (f": {info['fail']}" if info.get("fail") else ""))
                    return "failed"
            else:
                # Сервер не ответил на опрос. Это не застой: чаще всего пропала связь с
                # нашей стороны (Wi-Fi, VPN). Время без ответа в застой не засчитываем —
                # раньше пять минут без сети отменяли заказ, а в гонке сразу все.
                last_change += now - last_poll
            last_poll = now
            if report and info:
                now, got = time.time(), info.get("bytes") or 0
                rate = None
                if prev and now > prev[0] and got >= prev[1]:
                    rate = (got - prev[1]) / (now - prev[0])
                prev = (now, got)
                report({"stage": info.get("stage"), "bytes": got, "rate": rate})
            logf(f"ждём модель… {el // 60} мин — " +
                 (text or "установка движка + скачивание весов"))
            noted = el

            # замер реальной скорости на окне SPEED_WINDOW
            if net_mbps and info and info.get("stage") == "download" and info.get("bytes"):
                now, got = time.time(), info["bytes"]
                if mark is None or got < mark[1]:
                    mark = (now, got)
                elif got == mark[1] or (total_gib and got >= total_gib * GIB_BYTES * 0.9):
                    # Байты не растут или почти всё скачано: веса уже на месте, а стадия
                    # «download» держится, пока собирается движок. Скорость тут мерить
                    # нечем — на живом сервере это давало «0 Mbps» и отмену исправного
                    # сервера посреди сборки. Настоящее зависание ловит проверка застоя.
                    mark = (now, got)
                elif now - mark[0] >= SPEED_WINDOW:
                    real = (got - mark[1]) * 8 / (now - mark[0]) / 1e6
                    # Заявленную скорость ограничиваем сверху: на канале в 10 Гбит/с
                    # узкое место — уже сам HuggingFace, и сервер тут ни при чём
                    if real < min(net_mbps, SPEED_CLAIM_CAP) * SPEED_MIN_RATIO:
                        logf(f"⛔ реальная скорость {real:.0f} Mbps против заявленных "
                             f"{net_mbps:.0f} — сервер негодный")
                        return "slow"
                    logf(f"скорость скачивания ≈{real:.0f} Mbps (заявлено {net_mbps:.0f})")
                    mark = (now, got)
        # Застой считаем только когда есть за чем следить: без источника прогресса
        # строка не меняется никогда, и выход по застою был бы ложным.
        if seen and time.time() - last_change > STALL_TIMEOUT:
            logf(f"⛔ за {STALL_TIMEOUT // 60} мин на сервере ничего не изменилось: {last_text}")
            return "stalled-engine" if (last_sig or ("",))[0] == "engine" else "stalled"
        # Сервер совсем не отвечает на опросы: ни статус-порт, ни ssh. Раньше в таком
        # положении деплой ждал до общего лимита — пять часов оплаты вслепую.
        if time.time() - last_contact > BLIND_TIMEOUT:
            logf(f"⛔ сервер {BLIND_TIMEOUT // 60} мин не отвечает на опросы о прогрессе")
            return "blind"
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


# ───────── Claude Code / Claude Desktop ─────────
# Claude Code говорит только на Anthropic Messages (POST /v1/messages). llama-server его
# умеет, vLLM — тоже; TabbyAPI — нет. Обычный `claude` и ~/.claude/settings.json не трогаем:
# рядом кладём отдельную команду claude-uncensored, которая выставляет переменные только
# для своего запуска. Так ваш Claude с подпиской остаётся как был.
CLAUDE_LAUNCHER = "claude-uncensored"
CLAUDE_MIN_CTX = 32768      # системный промпт и инструменты Claude Code — это уже ~20 тыс. токенов
_SAFE_SHELL = re.compile(r"^[A-Za-z0-9_.:/\-]+$")


def anthropic_base(url):
    """Claude ждёт адрес без /v1 — путь /v1/messages он дописывает сам."""
    url = (url or "").rstrip("/")
    return url[:-3] if url.endswith("/v1") else url


def claude_supported(engine):
    if engine == hfmodels.ENGINE_TABBY:
        return False, ("Claude понимает только протокол Anthropic (/v1/messages), а TabbyAPI "
                       "(модели EXL2/EXL3) его не отдаёт. Подойдёт GGUF или safetensors-модель")
    return True, ""


def _claude_env(url, key, ctx):
    ctx = int(ctx or DEFAULT_CTX)
    return {
        "ANTHROPIC_BASE_URL": anthropic_base(url),
        "ANTHROPIC_AUTH_TOKEN": key,         # уходит как Authorization: Bearer — его и ждёт сервер
        "ANTHROPIC_API_KEY": "",             # чтобы не перебил чужой ключ из окружения
        "ANTHROPIC_MODEL": MODEL_NAME,
        # служебные запросы Claude Code шлёт «быстрой» модели — пусть идут туда же
        "ANTHROPIC_DEFAULT_FABLE_MODEL": MODEL_NAME,
        "ANTHROPIC_DEFAULT_OPUS_MODEL": MODEL_NAME,
        "ANTHROPIC_DEFAULT_SONNET_MODEL": MODEL_NAME,
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": MODEL_NAME,
        "CLAUDE_CODE_SUBAGENT_MODEL": MODEL_NAME,
        # иначе Claude Code считает окно в 200 тыс. и упрётся в контекст модели, не успев сжать разговор
        "CLAUDE_CODE_MAX_CONTEXT_TOKENS": str(ctx),
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(max(4096, min(32000, ctx // 4))),
        # бета-поля Anthropic чужой сервер может не понять и ответить 400
        "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
        # без телеметрии и автообновлений: в этом запуске всё идёт только на вашу модель
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    }


def _claude_bin_dir():
    """Каталог, где лежит claude: он заведомо в PATH, значит и claude-uncensored найдётся."""
    import shutil
    exe = shutil.which("claude")
    if exe:
        d = os.path.dirname(os.path.abspath(exe))
        if os.access(d, os.W_OK):
            return d, True
    return os.path.join(os.path.expanduser("~"), ".local", "bin"), bool(exe)


def write_claude_launcher(url, key, ctx=None, title=None, origin="Clore", engine=None, logf=log):
    """Пишет команду claude-uncensored (.cmd для Windows и sh-скрипт). Возвращает dict."""
    ok, why = claude_supported(engine)
    if not ok:
        raise ValueError(why)
    env = _claude_env(url, key, ctx)
    # значения подставляются в скрипт оболочки — пропускаем только безопасные символы
    for k, v in env.items():
        if v and not _SAFE_SHELL.match(v):
            raise ValueError(f"{k}: недопустимые символы — не стану вписывать это в скрипт")
    d, found = _claude_bin_dir()
    os.makedirs(d, exist_ok=True)
    head = f"{title or MODEL_NAME} ({'this PC' if origin == 'мой ПК' else origin})"
    head = re.sub(r"[^A-Za-z0-9 _.()/-]", "", head)
    cmd = ["@echo off", f"rem {head}: Claude Code on your own model. Generated by UncensoredAI.",
           "rem Your normal `claude` is untouched; these variables live only in this run.",
           "setlocal"]
    cmd += [f'set "{k}={v}"' for k, v in env.items()]
    cmd += ["claude %*", ""]
    sh = ["#!/bin/sh", f"# {head}: Claude Code on your own model. Generated by UncensoredAI."]
    sh += [f"export {k}='{v}'" if v else f"unset {k}" for k, v in env.items()]
    sh += ['exec claude "$@"', ""]
    paths = []
    p_cmd = os.path.join(d, CLAUDE_LAUNCHER + ".cmd")
    p_sh = os.path.join(d, CLAUDE_LAUNCHER)
    if os.name == "nt":
        with open(p_cmd, "w", encoding="ascii", newline="\r\n") as f:
            f.write("\n".join(cmd))
        paths.append(p_cmd)
    with open(p_sh, "w", encoding="ascii", newline="\n") as f:
        f.write("\n".join(sh))
    if os.name != "nt":
        os.chmod(p_sh, 0o700)
    paths.append(p_sh)
    logf(f"Claude Code: команда {CLAUDE_LAUNCHER} записана в {d}")
    warn = []
    if not found:
        warn.append("Claude Code не найден в PATH — поставьте его (npm i -g @anthropic-ai/claude-code), "
                    f"а каталог {d} добавьте в PATH")
    if int(ctx or DEFAULT_CTX) < CLAUDE_MIN_CTX:
        warn.append(f"контекст {int(ctx or DEFAULT_CTX)} маловат: одни инструкции Claude Code "
                    f"занимают ~20 тыс. токенов. Нужно хотя бы {CLAUDE_MIN_CTX}")
    for w in warn:
        logf("внимание: " + w)
    return {"paths": paths, "dir": d, "command": CLAUDE_LAUNCHER, "warnings": warn}


DESKTOP_ENTRY = "UncensoredAI"
# Claude Desktop вычёркивает из списка модели, в id которых нет модели Anthropic
# ("is not an Anthropic model and was removed"). Поэтому для него у нашей модели второе имя.
# Sonnet 4.5 выбран сознательно: окно 200 тыс. (а не 1M) и без adaptive thinking —
# меньше параметров, которых llama.cpp не поймёт. llama-server имя модели не сверяет,
# vLLM отдаёт модель под обоими именами (templates/onstart_vllm.sh).
DESKTOP_MODEL_ID = f"{MODEL_NAME}/claude-sonnet-4-5"


def _served_models(url, key):
    req = urllib.request.Request(url.rstrip("/") + "/models", headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            d = json.loads(r.read().decode())
        return {m.get("id") for m in d.get("data") or [] if isinstance(m, dict)}
    except Exception:  # noqa
        return None


def _desktop_library():
    """configLibrary Claude Desktop: сюда же пишет его окно Configure Third-Party Inference."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, "Claude-3p", "configLibrary")
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/Claude-3p/configLibrary")
    return os.path.expanduser("~/.config/Claude-3p/configLibrary")


def _desktop_managed():
    """Политика администратора перекрывает локальную настройку целиком — о ней надо предупредить."""
    if os.name != "nt":
        return None
    import winreg
    for hive, name in ((winreg.HKEY_LOCAL_MACHINE, "HKLM"), (winreg.HKEY_CURRENT_USER, "HKCU")):
        try:
            with winreg.OpenKey(hive, r"SOFTWARE\Policies\Claude") as k:
                if winreg.QueryInfoKey(k)[1]:
                    return name + r"\SOFTWARE\Policies\Claude"
        except OSError:
            pass
    return None


def write_claude_desktop(url, key, title=None, origin="Clore", engine=None, logf=log):
    """
    Прописывает модель в Claude Desktop — в режим стороннего провайдера (Claude Desktop on 3P).
    Пишем то же, что пишет окно Developer → Configure Third-Party Inference: отдельную запись
    UncensoredAI в configLibrary и делаем её применённой. Чужие записи не трогаем. Ключ кладём
    открытым — Desktop при запуске сам шифрует его DPAPI (так описано в его документации).
    """
    import uuid
    ok, why = claude_supported(engine)
    if not ok:
        raise ValueError(why)
    lib = _desktop_library()
    os.makedirs(lib, exist_ok=True)
    meta_path = os.path.join(lib, "_meta.json")
    meta = {}
    if os.path.isfile(meta_path):
        try:
            with open(meta_path, encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, ValueError):
            raise ValueError(f"не могу прочитать {meta_path} — поправьте или удалите его в окне настройки Claude Desktop")
        if not isinstance(meta, dict):
            raise ValueError(f"{meta_path}: неожиданный формат — не трогаю")
        backup = meta_path + ".before-uncensoredai"
        if not os.path.exists(backup):
            with open(backup, "w", encoding="utf-8") as f:
                json.dump(meta, f, indent=2)
    entries = [e for e in (meta.get("entries") or []) if isinstance(e, dict)]
    mine = next((e for e in entries if e.get("name") == DESKTOP_ENTRY
                 and re.fullmatch(r"[a-f0-9-]{36}", str(e.get("id", "")))), None)
    if not mine:
        mine = {"id": str(uuid.uuid4()), "name": DESKTOP_ENTRY}
        entries.append(mine)
    label = f"{title or MODEL_NAME} ({origin})"
    cfg = {
        "inferenceProvider": "gateway",
        "inferenceGatewayBaseUrl": anthropic_base(url),
        "inferenceGatewayApiKey": key,
        "inferenceGatewayAuthScheme": "bearer",
        # без явного списка Desktop покажет только модели с «claude» в id — нашей там не будет
        "inferenceModels": [{"name": DESKTOP_MODEL_ID, "labelOverride": label}],
    }
    cfg_path = os.path.join(lib, mine["id"] + ".json")
    tmp = cfg_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    os.replace(tmp, cfg_path)
    prev = meta.get("appliedId")
    meta["entries"] = entries
    meta["appliedId"] = mine["id"]
    tmp = meta_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)
    os.replace(tmp, meta_path)
    logf(f"Claude Desktop: запись «{DESKTOP_ENTRY}» в {lib} применена")
    warn = []
    if engine == hfmodels.ENGINE_VLLM:
        served = _served_models(url, key)
        if served is not None and DESKTOP_MODEL_ID not in served:
            warn.append("сервер поднят старой версией софта и не знает имени модели для Desktop — "
                        "Desktop получит ошибку 404. Разверните модель заново")
    managed = _desktop_managed()
    if managed:
        warn.append(f"есть политика администратора {managed} — Desktop возьмёт настройки оттуда, а не наши")
    prev_name = next((e.get("name") for e in entries if e.get("id") == prev), None)
    if prev and prev != mine["id"]:
        warn.append(f"до этого была выбрана конфигурация «{prev_name or prev}» — она сохранена, "
                    "вернуть её можно в окне Configure Third-Party Inference")
    return {"dir": lib, "entry": DESKTOP_ENTRY, "label": label, "warnings": warn,
            "base_url": anthropic_base(url), "model": DESKTOP_MODEL_ID}


def remove_claude_desktop(logf=log):
    """Снимает нашу запись: Desktop возвращается к прежней конфигурации (или к обычному входу)."""
    lib = _desktop_library()
    meta_path = os.path.join(lib, "_meta.json")
    try:
        with open(meta_path, encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, ValueError):
        return False
    entries = [e for e in (meta.get("entries") or []) if isinstance(e, dict)]
    mine = [e for e in entries if e.get("name") == DESKTOP_ENTRY]
    if not mine:
        return False
    ids = {e.get("id") for e in mine}
    for i in ids:
        if re.fullmatch(r"[a-f0-9-]{36}", str(i)):
            try:
                os.remove(os.path.join(lib, i + ".json"))
            except OSError:
                pass
    rest = [e for e in entries if e.get("id") not in ids]
    meta["entries"] = rest
    if meta.get("appliedId") in ids:
        backup = meta_path + ".before-uncensoredai"
        old = None
        try:
            with open(backup, encoding="utf-8") as f:
                old = json.load(f).get("appliedId")
        except (OSError, ValueError):
            pass
        keep = old if any(e.get("id") == old for e in rest) else (rest[0]["id"] if rest else None)
        if keep:
            meta["appliedId"] = keep
        else:
            meta.pop("appliedId", None)
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)
    logf("Claude Desktop: запись UncensoredAI снята")
    return True


def update_opencode(url, key, title=None, logf=log, ctx=None, engine=None, origin="Clore"):
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
            # origin — где крутится модель: на сервере Clore или на этом ПК
            prov["name"] = f"{title} ({origin})"
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


def _active_orders(clore):
    return [o for o in clore.my_orders() if not o.get("expired")]


def cancel_pending(clore, logf=log, **kw):
    """
    Заказ, который создавался в момент обрыва: номера нет, есть только сервер
    (PENDING_SERVER). Сервер на Clore сдаётся целиком, так что активный заказ на нём — наш.
    Возвращает True, когда после вызова там точно ничего не оплачивается.
    """
    sid = (load_env().get("PENDING_SERVER") or "").strip()
    if not sid:
        return True
    try:
        mine = [o for o in _active_orders(clore) if str(o.get("si")) == sid]
    except CloreError as e:
        logf(f"⛔ не смог проверить, остался ли заказ на сервере {sid}: {e}")
        return False
    ok = True
    for o in mine:
        if cancel_order_safe(clore, o["id"], None, logf, **kw) is None:
            logf(f"заказ {o['id']} на сервере {sid} (создавался в момент остановки) отменён")
        else:
            ok = False
            logf(f"⛔ заказ {o['id']} на сервере {sid} отменить не удалось — он оплачивается")
    if ok:
        save_env_keys({"PENDING_SERVER": ""})
    return ok


def _check_leftovers(clore, env, force, logf=log):
    """
    В .env могли остаться заказы прошлого деплоя: прерванного Ctrl+C, упавшего, с
    зависшим движком. Раньше новый деплой молча арендовал ещё один сервер и стирал номер
    старого — тот оплачивался дальше, а найти его было уже нечем. Теперь: заказ ещё
    активен — останавливаемся и говорим об этом; уже погашен — чистим ссылку.
    """
    race = [int(x) for x in (env.get("RACE_ORDERS") or "").split(",") if x.strip().isdigit()]
    pending = (env.get("PENDING_SERVER") or "").strip()
    cur = (env.get("ORDER") or "").strip()
    if not (race or pending or (cur and not force)):
        return
    active = _active_orders(clore)
    ids = {int(o.get("id", -1)) for o in active}
    alive = [r for r in race if r in ids]
    if alive:
        raise SystemExit(f"от прошлой параллельной установки остались активные заказы "
                         f"({', '.join(map(str, alive))}) — они оплачиваются. Отмените их: "
                         f"python stop.py или вкладка «Заказы»")
    if race:
        save_env_keys({"RACE_ORDERS": ""})
    if pending:
        mine = [o for o in active if str(o.get("si")) == pending]
        if mine:
            raise SystemExit(f"на сервере {pending} остался заказ {mine[0].get('id')} от "
                             f"прерванного деплоя — он оплачивается. Отмените его: "
                             f"python stop.py или вкладка «Заказы»")
        save_env_keys({"PENDING_SERVER": ""})
    if cur and not force:
        if cur.isdigit() and int(cur) in ids:
            raise SystemExit(f"заказ {cur} ещё активен и оплачивается, но модель на нём не "
                             f"отвечает. Отмените его («Отменить заказ» или python stop.py) "
                             f"либо запустите с галочкой «арендовать ещё один сервер» (--force)")
        logf(f"заказ {cur} из .env уже неактивен — убираю ссылку на него")
        save_env_keys({"ORDER": "", "SERVER": "", "API_URL": "", "SSH": ""})


# ---------- главный сценарий ----------
def deploy(cfg, env=None, logf=log):
    """
    cfg: model (ссылка/repo_id), ctx, max_price, currency, order_type, server_ids
         (очередь выбранных вручную серверов; одиночный server_id тоже принимается),
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
    if not cfg.get("dry_run"):
        _check_leftovers(clore, env, bool(cfg.get("force")), logf)
        fresh = load_env()       # проверка могла убрать из .env ссылки на погашенные заказы
        env = {**env, **{k: fresh.get(k, "") for k in
                         ("ORDER", "SERVER", "API_URL", "SSH", "RACE_ORDERS", "PENDING_SERVER")}}

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
        # Раньше это было предупреждение — и деплой шёл по всей очереди: на каждом
        # сервере скачивание падало, а комиссия за создание заказа списывалась
        if not cfg.get("dry_run"):
            raise SystemExit("репозиторий закрытый (gated), а HF_TOKEN не задан — веса не "
                             "скачаются ни на одном сервере. Впишите токен на вкладке «Настройки»")
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
    picked = picked_servers(cfg)
    if picked:
        known = {int(o["id"]): o for o in offers}
        if not all(sid in known for sid in picked):
            # Выбранный руками сервер мог не пройти фильтры подбора (мало VRAM под этот
            # квант, медленная сеть, чёрный список) — ищем такие отдельно, без ограничений
            wide = find_offers(clore, 0, 0, None, forced_currency, None, order_type,
                               min_net=0, download_gib=info["weights_gib"], limit=10_000,
                               engine=info["engine"], quant=info.get("precision"),
                               allow_old_gpu=True)
            for o in wide:
                if int(o["id"]) in picked:
                    known.setdefault(int(o["id"]), o)
        gone = [sid for sid in picked if sid not in known]
        if gone:
            logf("не нашлись в маркетплейсе (заняты или сняты с продажи): "
                 + ", ".join(str(s) for s in gone))
        # порядок — тот, в котором серверы выбирали в UI: это очередь аренды, а не цены
        offers = [known[sid] for sid in picked if sid in known]
        for o in offers:
            if not o.get("gpu_ok"):
                logf(f"⚠️ сервер {o['id']} выбран вручную, но {o['gpu_note']}")
    if not offers and picked:
        msg = ("ни один из выбранных серверов недоступен: "
               + ", ".join(str(s) for s in picked)
               + ". Их успели арендовать или хост снял их с продажи — "
                 "обновите список на вкладке «Сервер» и выберите другие")
        tg(env, "⛔ " + msg)
        raise SystemExit(msg)
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

    # Выбранные руками серверы пробуем все: это и есть очередь, которую задал пользователь.
    # Автоподбору хватает пяти лучших — дальше по списку цена растёт быстрее, чем шансы.
    candidates = offers if picked else offers[:TRY_OFFERS]
    if picked and len(candidates) > 1:
        logf(f"очередь аренды из {len(candidates)} серверов — пробую по порядку, "
             f"пока один не поднимется")
    for n, o in enumerate(candidates, 1):
        logf(f"кандидат {n}: {o['id']} — {o['gpu_count']}x {o['gpu']}"
             f"{' (' + o['arch'] + ')' if o.get('arch') else ''} ({o['vram_gb']} GiB VRAM) "
             f"${o['price_used']:.3f}/ч, диск {o['disk_gb']} GB, {o['net_down']:.0f} Mbps")
    if cfg.get("dry_run"):
        return {"status": "dry-run", "model": info, "offers": candidates}

    script, order_env = render_onstart(info, env, ctx=info["ctx"], gguf_file=cfg.get("gguf_file"))
    command = build_command(script)
    image = cfg.get("image") or IMAGES[info["engine"]]
    # Раньше паролем были первые 32 символа ключа API, и он печатался в лог: кто видел
    # лог, получал две трети ключа. Теперь это отдельный случайный секрет на каждый
    # деплой, лежит он только в .env.
    ssh_password = cfg.get("ssh_password") or secrets.token_hex(16)
    if not re.fullmatch(r"[A-Za-z0-9_.:@=+-]{8,64}", ssh_password, re.ASCII):
        raise SystemExit("SSH-пароль: от 8 до 64 символов, только латинские буквы, цифры и _ . : @ = + -")
    if not cfg.get("dry_run"):
        save_env_keys({"SSH_PASSWORD": ssh_password})
    # entrypoint Clore читает пароль из переменной SSH_PASSWORD, поля заказа ему мало
    order_env["SSH_PASSWORD"] = ssh_password
    pubkey = ssh_pubkey(env)
    if pubkey:
        order_env["SSH_KEY"] = pubkey
        logf("в заказ добавлен ваш публичный ключ — вход по ssh без пароля")
    else:
        logf("публичного ключа нет, вход по паролю — он сохранён в .env (SSH_PASSWORD)")

    # Заказ могут отменить руками, пока он поднимается. Без этой проверки деплой принимал
    # отменённый сервер за неподнявшийся и молча арендовал следующего кандидата.
    stop = cfg.get("stop")
    race = bool(cfg.get("race")) and len(candidates) > 1

    def plan(offer, lg):
        """Валюта, выгрузка экспертов и контекст под этот сервер. None — сервер не годится."""
        sid = offer["id"]
        currency, amount, usd = pick_currency(offer, balances, rates, forced_currency)
        if not amount:
            lg(f"сервер {sid}: нет цены в валюте {currency} — пропускаю")
            return None
        if balances.get(currency, 0) <= 0:
            lg(f"сервер {sid}: принимает только {currency}, а на этом кошельке пусто — пропускаю")
            return None
        # Выгружаем минимум необходимого: каждый слой в оперативке — это работа для CPU.
        # На большой карте выгрузка может не понадобиться вовсе, даже если включена.
        card_gib = offer["vram_gb"] / max(offer["gpu_count"], 1)
        n_cpu_moe = (hfmodels.cpu_moe_layers(offload, info["vram_gib"], offer["vram_gb"],
                                             card_gib=card_gib) if cpu_moe else 0)
        if n_cpu_moe:
            lg(f"эксперты {n_cpu_moe} слоёв из {offload['max_layers']} уедут в оперативку "
               f"({offer['vram_gb']} GiB VRAM против {info['vram_gib']} нужных)")
        elif cpu_moe:
            lg("карта вмещает модель целиком — выгрузка не нужна")
        # Контекст задают до выбора сервера, поэтому он может в карту и не влезть.
        # Подгоняем под фактическую VRAM: иначе OOM через полчаса после начала скачивания.
        gpu_weights = info["weights_gib"] - (n_cpu_moe * offload["per_layer_gib"]
                                             if n_cpu_moe else 0)
        fit_ctx = hfmodels.ctx_for_vram(info["ctx"], info["kv_gib"], gpu_weights,
                                        offer["vram_gb"] / VRAM_HEADROOM,
                                        info.get("vocab") or 0)
        fit_need = hfmodels.vram_estimate(gpu_weights, info["kv_gib"] * fit_ctx / info["ctx"],
                                          info.get("vocab") or 0)
        if fit_need > offer["vram_gb"]:
            # даже на минимальном контексте не влезает: бывает только при ручном выборе
            # сервера, фильтр такие предложения не пропускает
            lg(f"⚠️ модель не влезает в {offer['vram_gb']} GiB VRAM ни при каком контексте "
               f"(нужно {fit_need} GiB даже при {fit_ctx}) — запуск, скорее всего, упадёт по OOM")
        elif fit_ctx < info["ctx"]:
            lg(f"⚠️ контекст {info['ctx']} не влезает в {offer['vram_gb']} GiB VRAM — "
               f"ставлю {fit_ctx}. Нужен полный контекст — берите карту крупнее")
        oenv = {**order_env, "N_CPU_MOE": str(n_cpu_moe), "CTX": str(fit_ctx)}
        return {"currency": currency, "amount": amount, "usd": usd, "env": oenv,
                "fit_ctx": fit_ctx, "n_cpu_moe": n_cpu_moe}

    def rent(offer, p, lg, track):
        """
        Создаёт заказ и находит его номер. Возвращает номер заказа или None — тогда этот
        сервер пропускаем. track(oid) записывает заказ туда, откуда его найдёт отмена.
        """
        sid = offer["id"]
        lg(f"арендую сервер {sid} ({offer['gpu_count']}x {offer['gpu']}) за ${p['usd']:.3f}/ч "
           f"(списание {p['amount']:g} {p['currency']}/сутки)")
        # Запоминаем, какие заказы на этом сервере уже есть: иначе чужой (или свой прошлый)
        # заказ на том же сервере будет принят за только что созданный
        try:
            before = {int(o.get("id", -1)) for o in clore.my_orders()}
        except CloreError:
            before = set()
        # С этого места заказ может существовать и оплачиваться, а его номера у нас ещё
        # нет. Поэтому сервер помечаем ДО запроса (PENDING_SERVER): что бы ни случилось
        # дальше — Ctrl+C, закрытое окно, сбой сети, — stop.py найдёт заказ по серверу.
        track(None, sid)
        http500 = False
        try:
            clore.create_order(
                server_id=sid, image=image, currency=p["currency"], order_type=order_type,
                ports={22: "tcp", API_PORT: "tcp", STATUS_PORT: "tcp"},
                env=p["env"], command=command,
                ssh_password=ssh_password, ssh_key=pubkey,
                # обычный образ (nvidia/cuda) не содержит sshd и долгоживущего процесса —
                # без entrypoint Clore контейнер не держится и SSH недоступен
                autossh_entrypoint=True,
                spot_price=p["amount"] if order_type == "spot" else None,
                required_price=p["amount"],
            )
            unsure = False
        except CloreError as e:
            if getattr(e, "uncertain", False):
                # Ответ не дошёл, а запрос мог: заказ, возможно, создан и уже
                # тарифицируется. Не уходим к следующему серверу, а ищем его ниже.
                lg(f"сервер {sid}: {e} — ответ Clore не получен, проверяю, не создался ли заказ")
                unsure = True
                http500 = "HTTP 500" in str(e)
            else:
                lg(f"сервер {sid}: {e} — пропускаю")
                track(None, None)
                return None
        polls = 0            # сколько раз список заказов реально прочитался
        for n in range(8):
            if n:
                time.sleep(POLL)
            try:
                orders = [o for o in clore.my_orders()
                          if int(o.get("si", -1)) == int(sid) and not o.get("expired")
                          and int(o.get("id", -1)) not in before]
            except CloreError as e:
                lg(f"не смог прочитать список заказов ({e}) — повторяю")
                continue
            polls += 1
            if orders:
                found = max(orders, key=lambda o: o.get("ct") or 0)
                track(found["id"], sid)
                lg(f"заказ {found['id']} создан на сервере {sid}")
                return found["id"]
            if unsure and polls >= 3:
                break
        if unsure and polls >= 3:
            # список читался, а заказа в нём нет — значит, запрос до Clore не дошёл
            track(None, None)
            if http500:
                # HTTP 500 не зависит от сервера (чаще всего слишком длинная команда
                # заказа) — перебирать остальных кандидатов бессмысленно
                raise SystemExit("Clore отвечает HTTP 500 на создание заказа — проблема "
                                 "не в сервере. Чаще всего это слишком длинная команда заказа")
            lg(f"сервер {sid}: заказ не создался — пропускаю")
            return None
        # Либо Clore подтвердил создание, а заказа в списке нет, либо список так и не
        # удалось прочитать. Идти дальше нельзя: заказ может тарифицироваться, и следующий
        # сервер стал бы вторым оплачиваемым. Пометка PENDING_SERVER остаётся в .env.
        msg = (f"заказ на сервер {sid} мог быть создан, но найти его в списке заказов не "
               f"удалось. Он может тарифицироваться: проверьте вкладку «Заказы» или "
               f"запустите python stop.py — он найдёт заказ по номеру сервера")
        tg(env, "⛔ " + msg)
        raise SystemExit(msg)

    def bring_up(offer, p, oid, lg, stp, report=None, on_access=None):
        """
        Ждёт, пока заказ получит адрес и модель ответит. Ничего не отменяет — это решает
        вызывающий. Возвращает dict: status = ready | fail, плюс подробности.
        """
        ready = wait_order_ready(clore, oid, lg, stop=stp)
        if not ready:
            return {"status": "fail", "why": f"не выдал адрес за {ORDER_TIMEOUT // 60} мин",
                    "reason": "сервер не стартовал"}
        _, acc = ready
        host = acc["host"]
        # регион виден только сейчас — раньше Clore его не сообщает
        hit = host_excluded(host, exclude_hosts)
        if hit:
            return {"status": "fail", "why": f"исключённый регион ({host} содержит «{hit}»)",
                    "reason": "исключённый регион"}
        api_url = f"http://{host}:{acc['ports'][API_PORT]}/v1"
        ssh_port = acc["ports"].get(22, 22)
        ssh = f"ssh -p {ssh_port} root@{host}"
        if on_access:
            on_access(api_url, ssh)
        lg(f"контейнер поднят. API будет на {api_url}; {ssh}"
           + ("" if pubkey else " (пароль — SSH_PASSWORD в .env)"))
        sign = wait_alive(host, acc["ports"], api_url, api_key, lg, stop=stp)
        if not sign:
            return {"status": "fail", "why": f"контейнер молчал {SILENT_LIMIT // 60} мин после аренды",
                    "reason": "контейнер не отвечал", "blacklist": True}
        lg(f"сервер отвечает ({sign})")
        # Фиксированных 90 минут не хватает большим моделям: 150+ ГиБ по реальному каналу
        # качаются полтора часа, и деплой падал по таймауту, уже почти доехав.
        api_timeout = max(API_TIMEOUT, int(offer.get("download_hours", 0) * 3600 * 2.5) + 30 * 60)
        # Сравниваем именно с «ready»: wait_api возвращает непустую строку и при отказе.
        outcome = wait_api(api_url, api_key, lg, timeout=api_timeout,
                           ssh_host=host, ssh_port=ssh_port,
                           total_gib=info.get("weights_gib"),
                           status_port=acc["ports"].get(STATUS_PORT), stop=stp, report=report,
                           net_mbps=net_est_of(offer))
        if outcome == "ready":
            return {"status": "ready", "api_url": api_url, "ssh": ssh}
        if outcome == "slow":
            # канал оказался в разы медленнее заявленного — сервер негодный
            return {"status": "fail", "why": "скачивание в разы медленнее заявленного",
                    "reason": "медленная сеть"}
        # Не поднялся. Логи забираем до отмены: вместе с контейнером исчезнут и они
        tail = remote_tail(host, ssh_port, lines=40 if outcome == "failed" else 12)
        if outcome == "failed":
            # установочный скрипт сам сообщил об ошибке — ждать застоя незачем
            if tail:
                lg("последние строки логов сервера:\n" + tail)
            return {"status": "fail", "reason": "ошибка установки", "setup_failed": True,
                    "why": "установка на сервере завершилась ошибкой"}
        if outcome == "blind":
            return {"status": "fail", "reason": "нет связи",
                    "why": f"сервер {BLIND_TIMEOUT // 60} мин не отвечал на опросы"}
        if tail:
            lg("последние строки логов сервера:\n" + tail)
        return {"status": "fail", "reason": "завис",
                "why": f"за {STALL_TIMEOUT // 60} мин на сервере ничего не изменилось",
                # движок упал на уже скачанных весах — дело, скорее всего, в модели или
                # контексте, и на другом сервере будет то же самое
                "model_problem": outcome == "stalled-engine"}

    def drop(oid, sid, res, lg):
        """Отменить неудачный заказ; сервер с явной поломкой — в чёрный список."""
        if res.get("blacklist"):
            blacklist_add(sid, res["why"])
        # Причина отмены уходит в Clore как жалоба на хост, поэтому передаём её только
        # при настоящей поломке сервера, а не когда он просто проиграл или не подошёл
        return cancel_order_safe(clore, oid, res["reason"][:64] if res.get("blacklist") else None, lg)

    def finish(offer, p, oid, api_url, ssh):
        """Модель поднялась: замер скорости, OpenCode, итог в лог и Telegram."""
        sid, usd, amount, currency = offer["id"], p["usd"], p["amount"], p["currency"]
        save_env_keys({"ORDER": str(oid), "SERVER": str(sid), "API_URL": api_url, "SSH": ssh,
                       "MODEL_REPO": info["repo_id"], "MODEL_NAME": MODEL_NAME,
                       "CTX": str(p["fit_ctx"]), "ENGINE": info["engine"]})
        speed = bench(api_url, api_key)
        if p["n_cpu_moe"]:
            # С экспертами в оперативке первый замер нечестно низкий: файл ещё не весь
            # в страничном кэше, и часть экспертов читается с диска. Замерено на живом
            # сервере: 8 ток/с сразу после старта против 19 через минуту прогрева.
            speed = max(speed or 0, bench(api_url, api_key) or 0) or None
        # при пакетных прогонах переключать OpenCode на каждую проверяемую модель незачем
        if not cfg.get("skip_opencode"):
            update_opencode(api_url, api_key, info["repo_id"].split("/")[-1], logf,
                            ctx=p["fit_ctx"], engine=info["engine"])
        # Баланс нужен только для справочной строки. Разовый сбой сети здесь не должен
        # ронять полностью удавшийся деплой.
        try:
            bals = wallet_balances(clore)
            bal = sum(a * rates.get(cur, 0) for cur, a in bals.items())
        except CloreError as e:
            logf(f"баланс не прочитался ({e}) — на результат деплоя это не влияет")
            bal = 0.0
        hours = f"{bal / usd:.0f} ч" if bal and usd else "?"
        msg = [f"✅ {info['repo_id']} готова",
               f"Base URL: {api_url}", f"Модель в API: {MODEL_NAME}", f"Движок: {info['engine']}"]
        if speed:
            msg.append(f"Скорость: {speed:.0f} ток/с")
        msg += [f"Сервер: {offer['gpu_count']}x {offer['gpu']}, ${usd:.3f}/ч "
                f"(списание {amount:g} {currency})",
                f"SSH: {ssh}", f"Баланс: ≈${bal:.2f} (~{hours})"]
        if not cfg.get("skip_opencode"):
            msg.append("OpenCode переключён (" + ", ".join(OPENCODE_CFG_USED)
                       + ") — перезапустите его.")
        # итог нужен в логе, а не только в Telegram: бот настроен далеко не у всех
        for line in msg:
            logf(line)
        tg(env, "\n".join(msg))
        return {"status": "ready", "order": oid, "server": sid, "api_url": api_url, "ssh": ssh,
                "model": info, "speed": speed, "price_usd": usd, "pay_amount": amount,
                "currency": currency, "balance_usd": round(bal, 2), "offer": offer}

    if race:
        return _deploy_race(candidates, plan, rent, bring_up, drop, finish, clore, env,
                            info, stop, logf)

    # ── по очереди: не поднялся первый — берём следующий ──
    # Что лежало в .env до нас. С «арендовать ещё один» там номер работающего сервера:
    # если наша попытка не удалась, возвращаем его на место, а не стираем.
    prev = {k: env.get(k) or "" for k in ("ORDER", "SERVER", "API_URL", "SSH")}

    def track_seq(oid, sid):
        if oid:
            save_env_keys({"ORDER": str(oid), "SERVER": str(sid), "PENDING_SERVER": ""})
        else:
            save_env_keys({"PENDING_SERVER": str(sid) if sid else ""})

    def release():
        save_env_keys({**prev, "PENDING_SERVER": ""})

    def access_seq(api_url, ssh):
        save_env_keys({"API_URL": api_url, "SSH": ssh, "MODEL_REPO": info["repo_id"],
                       "MODEL_NAME": MODEL_NAME, "ENGINE": info["engine"]})

    tried = setup_fails = 0
    for offer in candidates:
        check_stop(stop)
        tried += 1
        sid = offer["id"]
        if len(candidates) > 1:
            logf(f"— попытка {tried} из {len(candidates)}: сервер {sid}")
        p = plan(offer, logf)
        if not p:
            continue
        oid = None
        try:
            oid = rent(offer, p, logf, track_seq)
            if not oid:
                continue
            check_stop(stop)
            tg(env, f"🚀 Clore: заказ {oid}, {offer['gpu_count']}x {offer['gpu']} "
                    f"({offer['vram_gb']} GiB VRAM), ${p['usd']:.3f}/ч (списание {p['amount']:g} "
                    f"{p['currency']}).\nСтавлю {info['engine']} и качаю {info['repo_id']} "
                    f"({info['weights_gib']} GiB).")
            res = bring_up(offer, p, oid, logf, stop, on_access=access_seq)
        except DeployStopped:
            # Остановили — отменяем заказ, который арендовал ЭТОТ деплой, и только его.
            # Раньше остановку обслуживал обработчик кнопки по номеру из .env: он мог
            # отменить чужой работающий сервер, а заказ, создававшийся в эту секунду,
            # оставался оплачиваться.
            if oid:
                logf(f"деплой остановлен — отменяю заказ {oid}")
                if cancel_order_safe(clore, oid, None, logf) is None:
                    release()
                    logf(f"заказ {oid} отменён")
                else:
                    logf(f"⛔ заказ {oid} отменить не удалось — он оплачивается. Отмените его "
                         f"на вкладке «Заказы» или через stop.py")
            raise
        except KeyboardInterrupt:
            if oid:
                logf(f"⚠️ прервано. Заказ {oid} НЕ отменён и продолжает оплачиваться. "
                     f"Отменить: python stop.py")
            else:
                logf(f"⚠️ прервано во время аренды сервера {sid}: заказ мог успеть создаться. "
                     f"Запустите python stop.py — он проверит и отменит")
            raise
        if res["status"] == "ready":
            return finish(offer, p, oid, res["api_url"], res["ssh"])
        # Заказ могли отменить снаружи (stop.py из соседнего окна): тогда «сервер не
        # поднялся» — это не сбой, и арендовать следующий нельзя
        if str(load_env().get("ORDER") or "") != str(oid):
            raise SystemExit(f"заказ {oid} отменён извне — очередь серверов дальше не идёт")
        logf(f"⛔ сервер {sid}: {res['why']} — отменяю заказ {oid}")
        if res.get("blacklist"):
            blacklist_add(sid, res["why"])
        # отмена не прошла — дальше нельзя: второй сервер оплачивался бы вместе с первым
        cancel_or_halt(clore, env, oid, res["reason"] if res.get("blacklist") else None, logf)
        release()
        if res.get("model_problem"):
            msg = (f"⛔ модель скачалась, но движок не запустился на сервере {sid} — похоже, "
                   f"дело в модели или контексте, а не в сервере. Остальные серверы очереди "
                   f"не пробую: там было бы то же самое. Логи — выше")
            tg(env, msg)
            raise SystemExit(msg)
        if res.get("setup_failed"):
            setup_fails += 1
            if setup_fails >= 2:
                msg = ("⛔ установка упала с ошибкой на двух серверах подряд — похоже, дело "
                       "не в серверах. Остальные не пробую. Логи — выше")
                tg(env, msg)
                raise SystemExit(msg)
        if tried < len(candidates):
            logf("беру следующий сервер очереди")

    tg(env, f"⛔ Не удалось арендовать ни один из {tried} серверов Clore.")
    raise SystemExit(f"не удалось арендовать ни один из {tried} серверов")


# Гонка: сколько серверов держим, даже если они отстают от лидера, — на случай, если
# лидер упадёт уже на запуске движка. И во сколько раз прогноз окончания скачивания может
# быть хуже, чем у лидера, прежде чем сервер отменяем, чтобы не платить за него зря.
RACE_KEEP = 2
RACE_CULL_RATIO = 1.5
RACE_CULL_SLACK = 120           # секунд: разницу меньше этой отставанием не считаем
RACE_CULL_AFTER = 3 * 60        # столько сервер должен прокачать, прежде чем его судить


def _deploy_race(candidates, plan, rent, bring_up, drop, finish, clore, env, info, stop, logf):
    """
    Параллельная установка: арендуем все выбранные серверы сразу, побеждает тот, где модель
    поднялась первой, остальные отменяем. Clore считает аренду поминутно, поэтому проигравшие
    стоят минуты, а не часы. Номера всех заказов гонки лежат в .env (RACE_ORDERS), чтобы
    stop.py отменил их, даже если этот процесс упадёт.
    """
    import threading
    lock = threading.Lock()
    tracked = set()
    total = (info.get("weights_gib") or 0) * GIB_BYTES

    def save_tracked():
        save_env_keys({"RACE_ORDERS": ",".join(str(o) for o in sorted(tracked))})

    def track(oid, sid):
        with lock:
            if oid:
                tracked.add(int(oid))
                save_env_keys({"RACE_ORDERS": ",".join(str(o) for o in sorted(tracked)),
                               "PENDING_SERVER": ""})
            else:
                save_env_keys({"PENDING_SERVER": str(sid) if sid else ""})

    def untrack(oid):
        with lock:
            tracked.discard(int(oid))
            save_tracked()

    logf(f"параллельная установка на {len(candidates)} серверах: побеждает первый, где модель "
         f"поднимется, остальные отменяю. Clore считает аренду поминутно")
    racers = {}           # oid -> состояние
    winner = None
    try:
        # 1. арендуем всех. create_order у Clore не чаще раза в 5 с — это займёт минуту-две
        for offer in candidates:
            check_stop(stop)
            sid = offer["id"]
            lg = (lambda s: (lambda m: logf(f"[{s}] {m}")))(sid)
            p = plan(offer, lg)
            if not p:
                continue
            oid = rent(offer, p, lg, track)
            if oid:
                racers[int(oid)] = {"offer": offer, "p": p, "sid": sid, "lg": lg, "res": None,
                                    "cull": False, "bytes": 0, "rate": None, "stage": None,
                                    "dl0": None, "t0": time.time()}
        if not racers:
            raise SystemExit(f"не удалось арендовать ни один из {len(candidates)} серверов")
        tg(env, f"🚀 Clore: гонка на {len(racers)} серверах — {info['repo_id']} "
                f"({info['weights_gib']} GiB). Оставлю самый быстрый")
        done = threading.Event()

        def run(oid):
            r = racers[oid]
            stp = lambda: bool(stop and stop()) or done.is_set() or r["cull"]  # noqa: E731

            def rep(d):
                # Скорость для отсева — средняя с начала скачивания, а не между двумя
                # соседними опросами: одиночный замер шумит, и сервер, начавший качать за
                # секунды до опроса, выглядел в десять раз медленнее, чем есть
                r["stage"], r["bytes"] = d.get("stage"), d.get("bytes") or 0
                if r["stage"] != "download":
                    return
                now = time.time()
                if r["dl0"] is None or r["bytes"] < r["dl0"][1]:
                    r["dl0"] = (now, r["bytes"])
                elif now > r["dl0"][0]:
                    r["rate"] = (r["bytes"] - r["dl0"][1]) / (now - r["dl0"][0])

            try:
                r["res"] = bring_up(r["offer"], r["p"], oid, r["lg"], stp, report=rep)
            except DeployStopped:
                r["res"] = {"status": "stopped"}
            except Exception as e:  # noqa — сбой одного участника не должен валить гонку
                r["res"] = {"status": "fail", "why": f"ошибка: {e}", "reason": "ошибка деплоя"}

        threads = {oid: threading.Thread(target=run, args=(oid,), daemon=True) for oid in racers}
        for t in threads.values():
            t.start()

        finished = set()
        last_board = 0
        while True:
            if stop and stop():
                raise DeployStopped("деплой остановлен вручную")
            for oid, r in racers.items():
                if oid in finished or r["res"] is None:
                    continue
                finished.add(oid)
                res = r["res"]
                if res["status"] == "ready" and winner is None:
                    winner = oid
                    done.set()
                    r["lg"]("🏁 модель поднялась первой — этот сервер и оставляю")
                elif res["status"] == "fail":
                    r["lg"](f"⛔ {res['why']} — отменяю заказ {oid}")
                    if drop(oid, r["sid"], res, r["lg"]) is None:
                        untrack(oid)
                    if res.get("model_problem"):
                        r["lg"]("движок упал на скачанных весах — похоже, дело в модели; "
                                "на остальных серверах, скорее всего, будет то же")
            if winner is not None:
                break
            if len(finished) == len(racers):
                break
            # Отсев отстающих: по прогнозу окончания скачивания относительно лидера
            live = [(oid, r) for oid, r in racers.items() if oid not in finished and not r["cull"]]
            etas = {}
            for oid, r in live:
                if (r["stage"] == "download" and r["rate"] and total and r["dl0"]
                        and time.time() - r["dl0"][0] >= RACE_CULL_AFTER):
                    # почти всё скачано — сервер уже собирает движок, отставшим его считать
                    # нельзя: средняя скорость после конца скачивания только падает
                    etas[oid] = (0.0 if r["bytes"] >= total * 0.9
                                 else max(0.0, total - r["bytes"]) / r["rate"])
            if len(etas) >= 2 and len(live) > RACE_KEEP:
                best = min(etas.values())
                for oid, eta in sorted(etas.items(), key=lambda x: -x[1]):
                    if len([1 for _, r in live if not r["cull"]]) <= RACE_KEEP:
                        break
                    if eta > best * RACE_CULL_RATIO + RACE_CULL_SLACK:
                        r = racers[oid]
                        r["cull"] = True
                        r["lg"](f"отстаёт: докачает через ~{eta / 60:.0f} мин, лидер — через "
                                f"~{best / 60:.0f} мин. Отменяю, чтобы не платить зря")
                        if cancel_order_safe(clore, oid, None, r["lg"]) is None:
                            untrack(oid)
            # сводка раз в две минуты: кто сколько скачал
            if time.time() - last_board > 120:
                last_board = time.time()
                rows = []
                for oid, r in racers.items():
                    if oid in finished or r["cull"]:
                        continue
                    got = r["bytes"] / GIB_BYTES
                    spd = f", {r['rate'] * 8 / 1e6:.0f} Mbps" if r["rate"] else ""
                    rows.append(f"{r['sid']}: {r['stage'] or 'старт'} {got:.1f} ГиБ{spd}")
                if rows:
                    logf("гонка: " + " · ".join(rows))
            time.sleep(10)
    finally:
        # всё, что не победило, — отменяем, что бы ни случилось выше. В том числе заказ,
        # который создавался в момент остановки и номера ещё не получил
        cancel_pending(clore, logf)
        for oid in sorted(tracked):
            if oid == winner:
                continue
            r = racers.get(oid)
            lg = r["lg"] if r else logf
            if cancel_order_safe(clore, oid, None, lg) is None:
                tracked.discard(oid)
            else:
                lg(f"⛔ заказ {oid} отменить не удалось — он оплачивается. Отмените его на "
                   f"вкладке «Заказы» или через stop.py")
        save_env_keys({"RACE_ORDERS": ",".join(str(o) for o in sorted(tracked - {winner}))})
        if winner is not None and len(racers) > 1:
            logf(f"остальные {len(racers) - 1} заказов гонки отменены")

    if winner is None:
        msg = f"ни на одном из {len(racers)} серверов модель не поднялась"
        tg(env, "⛔ " + msg)
        raise SystemExit(msg)
    r = racers[winner]
    res = r["res"]
    logf(f"победил сервер {r['sid']} за {(time.time() - r['t0']) / 60:.0f} мин")
    return finish(r["offer"], r["p"], winner, res["api_url"], res["ssh"])


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
    ap.add_argument("--server", help=f"арендовать конкретные серверы Clore по id; через "
                                     f"запятую можно указать до {MAX_PICK_SERVERS} — они "
                                     f"образуют очередь: не поднялся первый, берём следующий")
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
    ap.add_argument("--race", action="store_true",
                    help="ставить на все серверы из --server параллельно и оставить самый быстрый")
    ap.add_argument("--force", action="store_true", help="арендовать ещё один сервер")
    ap.add_argument("--dry-run", action="store_true", help="только показать кандидатов")
    args = ap.parse_args()

    env = load_env()
    if not env.get("CLORE_API_KEY"):
        raise SystemExit("в .env нет CLORE_API_KEY — задайте его в UI (ui.bat) или вручную")
    res = deploy({"model": args.model, "ctx": args.ctx, "max_price_usd": args.max_price,
                  "currency": args.currency, "order_type": args.order_type,
                  "server_ids": args.server, "gpu_filter": args.gpu, "image": args.image,
                  "gguf_file": args.gguf_file, "skip_opencode": args.no_opencode,
                  "allow_old_gpu": args.allow_old_gpu, "spread": args.spread,
                  "cpu_moe": args.cpu_moe,
                  "exclude_hosts": args.exclude_hosts, "race": args.race,
                  "force": args.force, "dry_run": args.dry_run}, env)
    return 0 if res.get("status") in ("ready", "already-running", "dry-run") else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        left = load_env()
        if left.get("ORDER") or left.get("PENDING_SERVER") or left.get("RACE_ORDERS"):
            log("⚠️ прервано. В .env остались заказы — проверьте их и отмените: python stop.py")
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
