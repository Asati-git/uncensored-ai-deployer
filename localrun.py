# -*- coding: utf-8 -*-
"""
localrun.py — запуск модели на своём ПК: скачать llama.cpp и GGUF, проверить, поднять сервер.

Модель — это данные, а не программа: GGUF при загрузке не исполняет ничего из файла
(в отличие от pickle в .bin/.pt). Опасность в другом, и от неё здесь несколько слоёв:

  1. Подмена при скачивании. llama.cpp берётся только из релизов ggml-org на GitHub и
     сверяется с SHA-256 из того же API; веса — только .gguf, со сверкой SHA-256,
     который публикует Hugging Face, и с фиксацией коммита (файл не подменят на середине).
     Скрипты, конфиги и прочее содержимое репозитория не скачивается никогда.
  2. Испорченный GGUF, бьющий в загрузчик. В парсере llama.cpp находили переполнения,
     поэтому до запуска заголовок файла разбирается здесь, на Python, со строгими
     границами — кривой файл отсекается раньше, чем его увидит C++. Плюс берётся свежая
     стабильная сборка, где известные дыры закрыты.
  3. Если что-то всё же прорвалось — изоляция процесса:
       * native (Windows): llama-server с пониженным уровнем целостности (Low IL) —
         писать в ваши файлы, реестр и автозагрузку он не может; в Job Object, где
         запрещено порождать дочерние процессы и трогать буфер обмена и рабочий стол;
         с чистым окружением без ваших ключей. Сервер слушает только 127.0.0.1 и
         требует ключ. Выход в интернет можно перекрыть правилом брандмауэра.
       * docker: контейнер с образом ggml-org, закреплённым по digest, только для
         чтения, без привилегий, модель смонтирована read-only. На Windows и macOS
         Docker живёт в отдельной виртуальной машине.

Всё на stdlib.
"""
import ctypes
import hashlib
import http.client
import io
import json
import os
import platform
import re
import secrets
import shutil
import socket
import struct
import subprocess
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

import hfmodels

GH_API = "https://api.github.com/repos/ggml-org/llama.cpp"
GHCR = "ghcr.io/ggml-org/llama.cpp"
HF = "https://huggingface.co"
UA = {"User-Agent": "UncensoredAI/1.0"}
MODEL_NAME = "uncensored"        # тот же id модели, что и на сервере Clore
CONTAINER = "uncensored-llama"
FIREWALL_RULE = "UncensoredAI llama-server (no internet)"

_WIN = os.name == "nt"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class LocalError(RuntimeError):
    pass


class Stopped(Exception):
    """Пользователь нажал «Остановить» посреди скачивания."""


# ---------- где что лежит ----------
def data_dir(env=None):
    """Каталог под llama.cpp и модели. LOCAL_DIR в .env — если системный диск мал."""
    custom = ((env or {}).get("LOCAL_DIR") or "").strip()
    if custom:
        base = os.path.expanduser(custom)
    elif _WIN:
        base = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "UncensoredAI")
    elif platform.system() == "Darwin":
        base = os.path.expanduser("~/Library/Application Support/UncensoredAI")
    else:
        base = os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"),
                            "UncensoredAI")
    os.makedirs(base, exist_ok=True)
    return base


def _sub(env, *parts):
    p = os.path.join(data_dir(env), *parts)
    os.makedirs(p, exist_ok=True)
    return p


def _state_path(env):
    return os.path.join(data_dir(env), "run.json")


def load_state(env=None):
    try:
        with open(_state_path(env), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(env, st):
    tmp = _state_path(env) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)
    os.replace(tmp, _state_path(env))


# ---------- HTTP и скачивание ----------
def _json(url, headers=None, timeout=30):
    req = urllib.request.Request(url, headers={**UA, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise LocalError(f"{url}: HTTP {e.code}")
    except (urllib.error.URLError, TimeoutError) as e:
        raise LocalError(f"{url}: сеть недоступна ({e})")


def _fmt_gib(n):
    return f"{n / 1024 ** 3:.1f} ГиБ"


def download(url, dest, sha256=None, size=None, headers=None, logf=print, stop=None, label=None):
    """
    Обрыв связи посреди файла на 20–100 ГиБ — обычное дело. Раньше он ронял всю установку,
    и докачка начиналась только после ручного повтора; теперь продолжаем сами.
    """
    name = label or os.path.basename(dest)
    for attempt in range(6):
        try:
            return _download_once(url, dest, sha256, size, headers, logf, stop, label)
        except (OSError, http.client.HTTPException) as e:
            if attempt == 5:
                raise LocalError(f"{name}: связь обрывается ({e.__class__.__name__}: {e}). "
                                 f"Запустите ещё раз — скачанное сохранено, докачка продолжится")
            logf(f"  {name}: обрыв связи ({e.__class__.__name__}) — продолжаю с места обрыва, "
                 f"попытка {attempt + 2} из 6")
            time.sleep(5)


def _download_once(url, dest, sha256=None, size=None, headers=None, logf=print, stop=None, label=None):
    """
    Скачивает с докачкой и сверяет SHA-256. Файл появляется под своим именем только
    после сверки: недокачанный или подменённый никогда не окажется на месте настоящего.
    """
    part = dest + ".part"
    have = os.path.getsize(part) if os.path.exists(part) else 0
    h = hashlib.sha256()
    if have:
        if size and have > size:
            os.remove(part)
            have = 0
        else:
            # докачка: хеш считается по всему файлу, поэтому уже скачанное прогоняем заново
            with open(part, "rb") as f:
                for chunk in iter(lambda: f.read(8 << 20), b""):
                    h.update(chunk)
    hdr = {**UA, **(headers or {})}
    if have:
        hdr["Range"] = f"bytes={have}-"
    req = urllib.request.Request(url, headers=hdr)
    name = label or os.path.basename(dest)
    try:
        resp = urllib.request.urlopen(req, timeout=60)
    except urllib.error.HTTPError as e:
        if e.code == 416 and size and have == size:
            resp = None          # уже докачан целиком, осталось сверить
        else:
            raise LocalError(f"{name}: HTTP {e.code}")
    except (urllib.error.URLError, TimeoutError) as e:
        raise LocalError(f"{name}: сеть недоступна ({e})")

    if resp is not None:
        if have and resp.status != 206:
            # сервер не умеет докачку — начинаем с нуля
            have, h = 0, hashlib.sha256()
        total = size or (have + int(resp.headers.get("Content-Length") or 0))
        mode = "ab" if have else "wb"
        last = time.time()
        got = have
        started, start_got = time.time(), have
        with resp, open(part, mode) as out:
            while True:
                if stop and stop():
                    raise Stopped("скачивание остановлено")
                chunk = resp.read(4 << 20)
                if not chunk:
                    break
                out.write(chunk)
                h.update(chunk)
                got += len(chunk)
                if time.time() - last > 5:
                    speed = (got - start_got) / max(time.time() - started, 0.1) / 1024 ** 2
                    pct = f"{got * 100 // total}% · " if total else ""
                    logf(f"  {name}: {pct}{_fmt_gib(got)}"
                         f"{' из ' + _fmt_gib(total) if total else ''} · {speed:.0f} МБ/с")
                    last = time.time()
    if size and os.path.getsize(part) != size:
        raise LocalError(f"{name}: размер {os.path.getsize(part)} вместо {size} — скачано не целиком")
    digest = h.hexdigest()
    if sha256 and digest != sha256.lower():
        os.remove(part)
        raise LocalError(f"{name}: SHA-256 не совпал (ожидался {sha256[:16]}…, получен "
                         f"{digest[:16]}…) — файл удалён, запускать его нельзя")
    os.replace(part, dest)
    return digest


# ---------- llama.cpp ----------
def _driver_cuda():
    """Какую версию CUDA поддерживает драйвер — из шапки nvidia-smi."""
    import localpc
    exe = localpc._nvidia_smi()
    if not exe:
        return None
    out = localpc._run([exe])
    m = re.search(r"CUDA (?:UMD )?Version:\s*([\d.]+)", out)
    return tuple(int(x) for x in m.group(1).split(".")[:2]) if m else None


def _compute_cap():
    """Старшая compute capability среди карт: 12.x — Blackwell (RTX 50xx)."""
    import localpc
    exe = localpc._nvidia_smi()
    if not exe:
        return None
    caps = []
    for line in localpc._run([exe, "--query-gpu=compute_cap", "--format=csv,noheader"]).splitlines():
        try:
            caps.append(tuple(int(x) for x in line.strip().split(".")[:2]))
        except ValueError:
            pass
    return max(caps) if caps else None


def _release():
    """
    Стабильная сборка: у llama.cpp релизы v* на GitHub пустые и содержат только
    nightly-tag.txt — номер ночной сборки, из которой сделан стабильный выпуск. Берём
    её; если схема опять поменяется — самую свежую ночную.
    """
    try:
        latest = [_json(f"{GH_API}/releases/latest")]
    except LocalError:
        latest = []
    for r in latest:
        if r.get("prerelease") or not str(r.get("tag_name", "")).startswith("v"):
            continue
        tag_asset = next((a for a in r.get("assets", []) if a["name"] == "nightly-tag.txt"), None)
        if not tag_asset:
            continue
        try:
            req = urllib.request.Request(tag_asset["browser_download_url"], headers=UA)
            with urllib.request.urlopen(req, timeout=30) as resp:
                tag = resp.read().decode().strip()
            if re.fullmatch(r"b\d+", tag):
                return _json(f"{GH_API}/releases/tags/{tag}"), r["tag_name"]
        except (urllib.error.URLError, TimeoutError, LocalError):
            break
    for r in _json(f"{GH_API}/releases?per_page=10"):
        if re.fullmatch(r"b\d+", str(r.get("tag_name", ""))):
            return r, None
    raise LocalError("не нашёл сборок llama.cpp на GitHub")


def _platform_key():
    system = platform.system()
    arch = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x64"
    if system == "Windows":
        return "win", arch, ".zip"
    if system == "Darwin":
        return "macos", arch, ".tar.gz"
    return "ubuntu", arch, ".tar.gz"


def pick_assets(release, backend):
    """
    Какие архивы нужны под это железо. Для CUDA — сборка не новее, чем умеет драйвер
    (иначе она не запустится), а для Windows ещё и архив cudart с библиотеками CUDA.
    Видеокарты Blackwell (RTX 50xx) требуют CUDA 12.8+: у старой 12.4 под них нет ядер.
    """
    osname, arch, ext = _platform_key()
    assets = {a["name"]: a for a in release.get("assets", [])}
    tag = release["tag_name"]
    want = []
    variant = backend
    if backend == "cuda" and osname != "macos":
        # CUDA совместима внутри мажорной версии: сборка 13.5 работает с драйвером 13.4.
        # А вот Blackwell (compute 12.x) сборка 12.4 не знает вовсе — нужна 12.8+.
        drv = _driver_cuda() or (0, 0)
        cap = _compute_cap() or (0, 0)
        found = []
        for name in assets:
            m = re.fullmatch(rf"llama-{tag}-bin-{osname}-cuda-([\d.]+)-{arch}{re.escape(ext)}", name)
            if m:
                ver = tuple(int(x) for x in m.group(1).split("."))
                if ver[0] > drv[0]:
                    continue
                if cap >= (12, 0) and ver[:2] < (12, 8):
                    continue
                found.append((ver, name))
        if found:
            ver, name = max(found)
            want.append(assets[name])
            vs = ".".join(map(str, ver))
            rt = next((a for n, a in assets.items()
                       if n.startswith("cudart-") and f"-{osname}-cuda-{vs}-{arch}" in n
                       and n.endswith(ext)), None)
            if rt:
                want.append(rt)
            variant = f"cuda-{vs}"
        else:
            # драйвер слишком старый для любой подходящей CUDA-сборки — Vulkan работает везде
            backend = variant = "vulkan"
    if not want:
        if osname == "macos":
            name = f"llama-{tag}-bin-macos-{arch}{ext}"
            variant = "metal"
        elif backend == "vulkan":
            name = f"llama-{tag}-bin-{osname}-vulkan-{arch}{ext}"
        else:
            name = (f"llama-{tag}-bin-win-cpu-{arch}.zip" if osname == "win"
                    else f"llama-{tag}-bin-ubuntu-{arch}{ext}")
            variant = "cpu"
        if name not in assets:
            raise LocalError(f"в сборке {tag} нет архива {name}")
        want.append(assets[name])
    for a in want:
        if not str(a.get("digest") or "").startswith("sha256:"):
            raise LocalError(f"у {a['name']} нет SHA-256 в GitHub — без проверки ставить не буду")
    return want, variant


def _safe_extract(archive, dest):
    """Распаковка без сюрпризов: ни абсолютных путей, ни «..», ни симлинков наружу."""
    root = os.path.realpath(dest)

    def target(name):
        p = os.path.realpath(os.path.join(dest, name))
        if p != root and not p.startswith(root + os.sep):
            raise LocalError(f"архив пытается писать за пределы каталога: {name}")
        return p

    if archive.endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            for info in z.infolist():
                p = target(info.filename)
                if info.is_dir():
                    os.makedirs(p, exist_ok=True)
                    continue
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with z.open(info) as src, open(p, "wb") as out:
                    shutil.copyfileobj(src, out)
    else:
        with tarfile.open(archive) as t:
            for m in t.getmembers():
                p = target(m.name)
                if m.issym() or m.islnk():
                    # в macOS/Linux-сборках так сделаны ссылки на .dylib/.so — разрешаем только внутрь
                    target(os.path.join(os.path.dirname(m.name), m.linkname))
                    if os.path.lexists(p):
                        os.remove(p)
                    os.symlink(m.linkname, p)
                    continue
                if m.isdir():
                    os.makedirs(p, exist_ok=True)
                    continue
                if not m.isfile():
                    continue
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with t.extractfile(m) as src, open(p, "wb") as out:
                    shutil.copyfileobj(src, out)
                os.chmod(p, 0o755 if (m.mode & 0o111) else 0o644)


def _find_server(root):
    exe = "llama-server.exe" if _WIN else "llama-server"
    for dirpath, _, files in os.walk(root):
        if exe in files:
            return os.path.join(dirpath, exe)
    return None


def install_llama(env, backend, logf=print, stop=None):
    """Ставит llama.cpp (один раз на версию) и возвращает путь к llama-server и описание."""
    release, stable = _release()
    assets, variant = pick_assets(release, backend)
    tag = release["tag_name"]
    dest = _sub(env, "llama", f"{tag}-{variant}")
    marker = os.path.join(dest, ".verified")
    exe = _find_server(dest)
    if exe and os.path.exists(marker):
        return exe, {"tag": tag, "stable": stable, "variant": variant}
    logf(f"ставлю llama.cpp {tag}{f' (стабильный {stable})' if stable else ''}, сборка {variant}")
    tmp = _sub(env, "downloads")
    sums = {}
    for a in assets:
        path = os.path.join(tmp, a["name"])
        sha = a["digest"].split(":", 1)[1]
        download(a["browser_download_url"], path, sha256=sha, size=a.get("size"),
                 logf=logf, stop=stop)
        logf(f"  {a['name']}: SHA-256 совпал с опубликованным в GitHub")
        _safe_extract(path, dest)
        sums[a["name"]] = sha
        os.remove(path)
    exe = _find_server(dest)
    if not exe:
        raise LocalError("в архиве llama.cpp не нашлось llama-server")
    with open(marker, "w", encoding="utf-8") as f:
        json.dump({"tag": tag, "stable": stable, "variant": variant, "sha256": sums}, f, indent=1)
    return exe, {"tag": tag, "stable": stable, "variant": variant}


# ---------- проверка GGUF ----------
class GGUFError(LocalError):
    pass


# Пределы с большим запасом над тем, что бывает у настоящих моделей (словарь — до ~300k
# токенов, merges — до ~500k, шаблон чата — десятки КБ). Всё, что выше, — не модель.
_MAX_KV = 100_000
_MAX_TENSORS = 200_000
_MAX_KEY = 1 << 16
_MAX_STR = 64 << 20
_MAX_ARRAY = 50_000_000
_SCALAR = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1, 10: 8, 11: 8, 12: 8}
_STRING, _ARRAY = 8, 9


def gguf_check(path):
    """
    Строгий разбор заголовка GGUF: магия, версия, метаданные, описания тензоров —
    каждая длина и каждое смещение проверяются против размера файла. Возвращает краткую
    сводку или бросает GGUFError. Данные тензоров не читаются: смотреть там нечего,
    а опасен для загрузчика именно заголовок.
    """
    size = os.path.getsize(path)
    with open(path, "rb") as raw:
        f = io.BufferedReader(raw, 1 << 20)
        pos = 0

        def read(n):
            nonlocal pos
            if n < 0 or pos + n > size:
                raise GGUFError("заголовок выходит за пределы файла")
            b = f.read(n)
            if len(b) != n:
                raise GGUFError("файл обрывается посреди заголовка")
            pos += n
            return b

        def u32():
            return struct.unpack("<I", read(4))[0]

        def u64():
            return struct.unpack("<Q", read(8))[0]

        def string(limit):
            n = u64()
            if n > limit:
                raise GGUFError(f"строка длиной {n} байт — у настоящих моделей таких нет")
            return read(n)

        if read(4) != b"GGUF":
            raise GGUFError("это не GGUF: нет сигнатуры в начале файла")
        version = u32()
        if version not in (2, 3):
            raise GGUFError(f"неизвестная версия GGUF: {version}")
        n_tensors, n_kv = u64(), u64()
        if n_tensors > _MAX_TENSORS or n_kv > _MAX_KV:
            raise GGUFError(f"неправдоподобно много записей: {n_tensors} тензоров, {n_kv} ключей")

        meta = {}
        for _ in range(n_kv):
            try:
                key = string(_MAX_KEY).decode("utf-8", "strict")
            except UnicodeDecodeError:
                raise GGUFError("имя ключа метаданных — не UTF-8")
            t = u32()
            if t in _SCALAR:
                v = read(_SCALAR[t])
            elif t == _STRING:
                v = string(_MAX_STR)
            elif t == _ARRAY:
                it, n = u32(), u64()
                if n > _MAX_ARRAY:
                    raise GGUFError(f"массив {key} из {n} элементов")
                if it in _SCALAR:
                    read(_SCALAR[it] * n)
                elif it == _STRING:
                    for _ in range(n):
                        string(_MAX_STR)
                else:
                    # массивы массивов llama.cpp не использует — это заготовка под переполнение
                    raise GGUFError(f"массив {key} с вложенным типом {it}")
                v = None
            else:
                raise GGUFError(f"ключ {key}: неизвестный тип значения {t}")
            if key in ("general.architecture", "general.name", "general.alignment",
                       "split.count", "split.no"):
                meta[key] = v

        align = struct.unpack("<I", meta["general.alignment"])[0] if "general.alignment" in meta else 32
        if align == 0 or align & (align - 1) or align > 1 << 16:
            raise GGUFError(f"неправдоподобное выравнивание: {align}")

        offsets = []
        for _ in range(n_tensors):
            string(_MAX_KEY)
            nd = u32()
            if not 1 <= nd <= 4:
                raise GGUFError(f"тензор с {nd} измерениями")
            elems = 1
            for _ in range(nd):
                d = u64()
                if d == 0 or d > 1 << 40:
                    raise GGUFError(f"измерение тензора {d}")
                elems *= d
                if elems > 1 << 44:
                    raise GGUFError("тензор неправдоподобного размера")
            gtype = u32()
            if gtype > 255:
                raise GGUFError(f"неизвестный тип тензора {gtype}")
            offsets.append(u64())

    data_start = (pos + align - 1) // align * align
    data_size = size - data_start
    for off in offsets:
        if off % align or off >= max(data_size, 1):
            raise GGUFError("смещение тензора указывает за пределы данных")

    arch = meta.get("general.architecture")
    if n_tensors and not arch:
        raise GGUFError("нет general.architecture — llama.cpp не поймёт, что это за модель")

    return {"version": version, "tensors": n_tensors, "kv": n_kv,
            "arch": arch.decode("utf-8", "replace") if isinstance(arch, bytes) else arch,
            "header_mib": round(pos / 1024 ** 2, 1)}


# ---------- модели ----------
def _hf_headers(token):
    return {"Authorization": f"Bearer {token}"} if token else {}


def model_files(repo, gguf_path, token=None):
    """
    Файлы выбранного кванта (все шарды) на зафиксированном коммите, с размерами и SHA-256.
    Коммит фиксируется, чтобы автор не подменил файл между листингом и скачиванием.
    """
    meta = _json(f"{HF}/api/models/{urllib.parse.quote(repo)}", _hf_headers(token))
    sha = meta.get("sha")
    if not sha:
        raise LocalError("Hugging Face не отдал коммит репозитория")
    tree = _json(f"{HF}/api/models/{urllib.parse.quote(repo)}/tree/{sha}?recursive=true",
                 _hf_headers(token))
    key = hfmodels._SHARD_RE.sub(".gguf", gguf_path)
    files = []
    for e in tree:
        p = str(e.get("path") or "")
        if e.get("type") != "file" or not p.lower().endswith(".gguf"):
            continue
        if hfmodels._SHARD_RE.sub(".gguf", p) != key:
            continue
        lfs = e.get("lfs") or {}
        if not lfs.get("oid"):
            raise LocalError(f"у {p} нет SHA-256 в Hugging Face — без проверки качать не буду")
        files.append({"path": p, "size": int(lfs.get("size") or e.get("size") or 0),
                      "sha256": lfs["oid"]})
    if not files:
        raise LocalError(f"в репозитории нет файла {gguf_path}")
    files.sort(key=lambda x: x["path"])
    return sha, files


def _model_dir(env, repo):
    return _sub(env, "models", repo.replace("/", "__"))


def download_model(env, repo, gguf_path, token=None, logf=print, stop=None):
    """Скачивает квант, сверяет хеши и проверяет заголовки. Возвращает путь к первому файлу."""
    sha, files = model_files(repo, gguf_path, token)
    mdir = _model_dir(env, repo)
    total = sum(x["size"] for x in files)
    need = 0
    for x in files:
        dest = os.path.join(mdir, os.path.basename(x["path"]))
        if os.path.exists(dest):
            continue
        # недокачанная часть уже лежит на диске: раньше её размер требовался повторно,
        # и докачка последних гигабайт упиралась в «нет места»
        part = os.path.getsize(dest + ".part") if os.path.exists(dest + ".part") else 0
        need += max(0, x["size"] - part)
    free = shutil.disk_usage(mdir).free
    if need and free < need + 2 * 1024 ** 3:
        raise LocalError(f"на диске {_fmt_gib(free)} свободно, а нужно {_fmt_gib(need)} — "
                         f"освободите место или укажите другой каталог (LOCAL_DIR)")
    logf(f"модель {repo}: {len(files)} файл(ов), {_fmt_gib(total)}, коммит {sha[:10]}")
    out = []
    for x in files:
        name = os.path.basename(x["path"])
        dest = os.path.join(mdir, name)
        ok = dest + ".verified"
        if os.path.exists(dest) and os.path.exists(ok):
            with open(ok, encoding="utf-8") as f:
                prev = f.read().strip()
            if prev == x["sha256"] and os.path.getsize(dest) == x["size"]:
                logf(f"  {name}: уже скачан и проверен")
                out.append(dest)
                continue
        url = f"{HF}/{repo}/resolve/{sha}/{urllib.parse.quote(x['path'])}"
        download(url, dest, sha256=x["sha256"], size=x["size"], headers=_hf_headers(token),
                 logf=logf, stop=stop, label=name)
        logf(f"  {name}: SHA-256 совпал с опубликованным в Hugging Face")
        out.append(dest)
    for p in out:
        info = gguf_check(p)
        logf(f"  {os.path.basename(p)}: заголовок GGUF v{info['version']} в порядке — "
             f"{info['tensors']} тензоров, {info['kv']} ключей"
             f"{', архитектура ' + info['arch'] if info['arch'] else ''}")
        # хеш записываем только после проверки заголовка: иначе «проверенным» считался бы
        # файл, который наш разбор отверг
        with open(p + ".verified", "w", encoding="utf-8") as f:
            f.write(next(x["sha256"] for x in files if os.path.basename(x["path"]) == os.path.basename(p)))
    return out[0]


def list_models(env=None):
    root = _sub(env, "models")
    out = []
    for d in sorted(os.listdir(root)):
        full = os.path.join(root, d)
        if not os.path.isdir(full):
            continue
        files = [f for f in os.listdir(full) if f.endswith(".gguf")]
        size = sum(os.path.getsize(os.path.join(full, f)) for f in files)
        if files:
            out.append({"repo": d.replace("__", "/", 1), "files": sorted(files),
                        "gib": round(size / 1024 ** 3, 1)})
    return out


def delete_model(env, repo):
    st = load_state(env)
    if st.get("repo") == repo and status(env).get("running"):
        raise LocalError("эта модель сейчас запущена — сначала остановите её")
    d = os.path.join(_sub(env, "models"), repo.replace("/", "__"))
    if not os.path.isdir(d) or os.path.dirname(os.path.realpath(d)) != os.path.realpath(_sub(env, "models")):
        raise LocalError("такой модели нет")
    shutil.rmtree(d)


# ---------- изоляция процесса (Windows) ----------
_LOW_IL = "S-1-16-4096"


def _mark_low_integrity(exe):
    """
    Метка Low на самом exe: Windows запускает процесс с уровнем не выше метки файла.
    Процесс Low IL не может писать туда, где у объекта уровень выше (то есть почти никуда
    в профиле, реестре и системе) — даже если через уязвимость в нём выполнят чужой код.
    """
    out = subprocess.run(["icacls", exe, "/setintegritylevel", "low"], capture_output=True,
                         text=True, creationflags=_NO_WINDOW)
    if out.returncode != 0:
        raise LocalError(f"не удалось понизить уровень целостности: {out.stdout or out.stderr}")


class _JobLimits(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", ctypes.c_uint32), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32)]


class _IoCounters(ctypes.Structure):
    _fields_ = [(n, ctypes.c_uint64) for n in ("r", "w", "o", "rb", "wb", "ob")]


class _ExtLimits(ctypes.Structure):
    _fields_ = [("Basic", _JobLimits), ("Io", _IoCounters), ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t)]


_JOB_HANDLES = []   # держим открытыми: закроешь — ограничения не пропадут, но так нагляднее


def _jail_windows(proc):
    """
    Job Object: запрещено порождать процессы (ActiveProcessLimit = 1) — эксплойту
    не запустить ни cmd, ни powershell; закрыт доступ к буферу обмена, рабочему столу,
    системным настройкам и выходу из системы. Процесс создан приостановленным и
    запускается только после того, как оказался внутри.
    """
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = ctypes.c_void_p
    job = k32.CreateJobObjectW(None, None)
    if not job:
        raise LocalError("CreateJobObject не сработал")
    ext = _ExtLimits()
    ext.Basic.LimitFlags = 0x8 | 0x400   # ACTIVE_PROCESS | DIE_ON_UNHANDLED_EXCEPTION
    ext.Basic.ActiveProcessLimit = 1
    if not k32.SetInformationJobObject(ctypes.c_void_p(job), 9, ctypes.byref(ext), ctypes.sizeof(ext)):
        raise LocalError(f"ограничения Job Object не применились ({ctypes.get_last_error()})")
    ui = ctypes.c_uint32(0x1 | 0x2 | 0x4 | 0x8 | 0x10 | 0x20 | 0x40 | 0x80)  # все UILIMIT_*
    k32.SetInformationJobObject(ctypes.c_void_p(job), 4, ctypes.byref(ui), 4)
    if not k32.AssignProcessToJobObject(ctypes.c_void_p(job), ctypes.c_void_p(int(proc._handle))):
        raise LocalError(f"процесс не помещён в Job Object ({ctypes.get_last_error()})")
    _JOB_HANDLES.append(job)
    ntdll = ctypes.WinDLL("ntdll")
    if ntdll.NtResumeProcess(ctypes.c_void_p(int(proc._handle))) != 0:
        raise LocalError("не удалось запустить процесс после помещения в Job Object")


def isolation_report(pid):
    """
    Что на самом деле получил процесс: уровень целостности и Job Object. Показывается в UI,
    чтобы «изоляция» была проверенным фактом, а не обещанием.
    """
    if not _WIN or not _alive_pid(pid):
        return None
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    k32.OpenProcess.restype = ctypes.c_void_p
    h = k32.OpenProcess(0x1000, False, int(pid))
    if not h:
        return None
    out = {}
    try:
        tok = ctypes.c_void_p()
        if adv.OpenProcessToken(ctypes.c_void_p(h), 0x8, ctypes.byref(tok)):
            buf = ctypes.create_string_buffer(64)
            ret = ctypes.c_uint32()
            if adv.GetTokenInformation(tok, 25, buf, 64, ctypes.byref(ret)):
                sid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
                adv.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
                adv.GetSidSubAuthority.restype = ctypes.POINTER(ctypes.c_uint32)
                n = adv.GetSidSubAuthorityCount(ctypes.c_void_p(sid))[0]
                rid = adv.GetSidSubAuthority(ctypes.c_void_p(sid), n - 1)[0]
                out["integrity"] = {0x0: "untrusted", 0x1000: "low", 0x2000: "medium",
                                    0x2100: "medium+", 0x3000: "high", 0x4000: "system"}.get(rid, hex(rid))
            k32.CloseHandle(tok)
        injob = ctypes.c_int()
        if k32.IsProcessInJob(ctypes.c_void_p(h), None, ctypes.byref(injob)):
            out["in_job"] = bool(injob.value)
    finally:
        k32.CloseHandle(ctypes.c_void_p(h))
    return out


def _clean_env(exe_dir):
    """Окружение без ваших ключей и токенов: только то, без чего не стартуют CUDA и Vulkan."""
    keep = ("SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "TEMP", "TMP", "NUMBER_OF_PROCESSORS",
            "PROCESSOR_ARCHITECTURE", "CUDA_VISIBLE_DEVICES", "HOME", "LANG", "LD_LIBRARY_PATH",
            "DYLD_LIBRARY_PATH", "VK_ICD_FILENAMES")
    env = {k: v for k, v in os.environ.items() if k.upper() in keep}
    base = os.environ.get("SYSTEMROOT", r"C:\Windows")
    env["PATH"] = os.pathsep.join([exe_dir, os.path.join(base, "System32")] if _WIN else
                                  [exe_dir, "/usr/bin", "/bin"])
    return env


def firewall_rule_present(exe):
    if not _WIN or not exe:
        return None
    out = subprocess.run(["netsh", "advfirewall", "firewall", "show", "rule", f"name={FIREWALL_RULE}",
                          "verbose"], capture_output=True, text=True, creationflags=_NO_WINDOW,
                         encoding="oem", errors="replace")   # netsh пишет в кодировке консоли
    return out.returncode == 0 and exe.lower() in out.stdout.lower()


def firewall_block(exe):
    """
    Правило «llama-server не ходит в интернет». Нужны права администратора, поэтому
    netsh запускается через UAC — Windows сама спросит подтверждение. Ответы на 127.0.0.1
    правило не трогает: брандмауэр Windows петлевой интерфейс не фильтрует.
    """
    if not _WIN:
        raise LocalError("правило брандмауэра ставится только на Windows")
    args = (f'advfirewall firewall add rule name="{FIREWALL_RULE}" dir=out action=block '
            f'program="{exe}" enable=yes')
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", "netsh", args, None, 0)
    if rc <= 32:
        raise LocalError("Windows не дал прав администратора — правило не добавлено")


# ---------- docker ----------
def docker_available():
    exe = shutil.which("docker")
    if not exe:
        return {"ok": False, "why": "Docker не установлен"}
    try:
        out = subprocess.run([exe, "info", "--format", "{{json .Runtimes}}|{{.OSType}}"],
                             capture_output=True, text=True, timeout=8, encoding="utf-8",
                             errors="replace", creationflags=_NO_WINDOW if _WIN else 0)
    except (OSError, subprocess.SubprocessError):
        return {"ok": False, "why": "Docker не отвечает (перезапустите Docker Desktop)"}
    if out.returncode != 0:
        return {"ok": False, "why": "Docker установлен, но не запущен (откройте Docker Desktop)"}
    return {"ok": True, "nvidia": "nvidia" in out.stdout or _WIN, "exe": exe}


def _ghcr_digest(tag):
    """Digest образа на момент запуска — запускаем по нему, а не по плавающему тегу."""
    tok = _json("https://ghcr.io/token?scope=repository:ggml-org/llama.cpp:pull")["token"]
    req = urllib.request.Request(
        f"https://ghcr.io/v2/ggml-org/llama.cpp/manifests/{tag}", method="HEAD",
        headers={**UA, "Authorization": f"Bearer {tok}",
                 "Accept": "application/vnd.oci.image.index.v1+json, "
                           "application/vnd.docker.distribution.manifest.list.v2+json, "
                           "application/vnd.docker.distribution.manifest.v2+json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        d = r.headers.get("Docker-Content-Digest")
    if not d or not d.startswith("sha256:"):
        raise LocalError(f"не получил digest образа {tag}")
    return d


def _docker(*args, timeout=None):
    try:
        return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout,
                              creationflags=_NO_WINDOW if _WIN else 0, encoding="utf-8",
                              errors="replace")
    except (OSError, subprocess.SubprocessError) as e:
        # Docker удалили или он завис: это «команда не удалась», а не падение UI
        return subprocess.CompletedProcess(args, 1, "", f"docker недоступен: {e}")


# ---------- запуск ----------
LOCAL_PORT = 8737   # постоянный: OpenCode и claude-uncensored не теряют модель после перезапуска


def _free_port(preferred=LOCAL_PORT):
    """Сначала постоянный порт; занят чем-то чужим — любой свободный."""
    for want in (preferred, 0):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", want))
            except OSError:
                continue
            return s.getsockname()[1]
    raise LocalError("нет свободного порта")


def _api_key(env):
    """Ключ создаётся один раз: иначе после перезапуска модели конфиги OpenCode и Claude устаревают."""
    path = os.path.join(data_dir(env), "api.key")
    try:
        with open(path, encoding="ascii") as f:
            key = f.read().strip()
        if re.fullmatch(r"[0-9a-f]{48}", key):
            return key
    except (OSError, UnicodeDecodeError):
        pass
    key = secrets.token_hex(24)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="ascii") as f:
        f.write(key)
    return key


def server_args(model_in, ctx, port, key, fit=None, host="127.0.0.1"):
    """Те же флаги, что на сервере Clore (templates/onstart_llamacpp.sh), плюс выгрузка под этот ПК."""
    fit = fit or {}
    args = ["-m", model_in, "--alias", MODEL_NAME, "--host", host, "--port", str(port),
            "--api-key", key, "-c", str(ctx), "--parallel", "1", "--metrics",
            "--flash-attn", "on", "--jinja", "--cache-type-k", "q8_0", "--cache-type-v", "q8_0",
            "--temp", "0.7", "--top-p", "0.95", "--min-p", "0.02"]
    mode = fit.get("mode")
    if mode == "cpu":
        args += ["-ngl", "0"]
    elif mode == "partial" and fit.get("ngl") is not None:
        args += ["-ngl", str(fit["ngl"])]
    else:
        args += ["-ngl", "999"]
    if mode == "moe" and fit.get("n_cpu_moe"):
        args += ["--n-cpu-moe", str(fit["n_cpu_moe"])]
    return args


def _alive_pid(pid):
    if not pid:
        return False
    if _WIN:
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, int(pid))   # QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        k32.GetExitCodeProcess(h, ctypes.byref(code))
        k32.CloseHandle(h)
        return code.value == 259                        # STILL_ACTIVE
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def api_ok(port, key, timeout=3):
    req = urllib.request.Request(f"http://127.0.0.1:{port}/health",
                                 headers={**UA, "Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status == 200
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
        return False


def tail(path, n=25):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return "".join(f.readlines()[-n:])
    except OSError:
        return ""


def start(env, repo, model_path, ctx, fit, isolation, backend, exe=None, logf=print, stop=None):
    """Поднимает llama-server и ждёт, пока он ответит. Возвращает состояние для run.json."""
    stop_running(env, logf)
    # остановленный только что сервер отпускает порт не мгновенно — подождём, а не уедем на случайный
    for _ in range(20):
        port = _free_port()
        if port == LOCAL_PORT:
            break
        time.sleep(0.5)
    key = _api_key(env)
    log_path = os.path.join(data_dir(env), "llama.log")
    st = {"repo": repo, "file": os.path.basename(model_path), "ctx": ctx, "port": port, "key": key,
          "isolation": isolation, "mode": (fit or {}).get("mode"), "started": int(time.time()),
          "vram_gib": (fit or {}).get("vram_gib") or 0,
          "log": log_path, "url": f"http://127.0.0.1:{port}/v1"}

    proc = None
    if isolation == "docker":
        d = docker_available()
        if not d["ok"]:
            raise LocalError(d["why"])
        tag = {"cuda": "server-cuda13" if (_driver_cuda() or (0, 0)) >= (13, 0) else "server-cuda",
               "vulkan": "server-vulkan"}.get(backend, "server")
        digest = _ghcr_digest(tag)
        image = f"{GHCR}@{digest}"
        logf(f"образ {GHCR}:{tag} закреплён по digest {digest[:19]}…")
        pull = _docker("pull", image, timeout=3600)
        if pull.returncode != 0:
            raise LocalError("docker pull не удался: " + (pull.stderr or pull.stdout)[-400:])
        _docker("rm", "-f", CONTAINER)
        mdir = os.path.dirname(model_path)
        cmd = ["run", "-d", "--name", CONTAINER,
               "-p", f"127.0.0.1:{port}:8080",
               "-v", f"{mdir}:/models:ro",
               "--read-only", "--tmpfs", "/tmp:rw,size=256m",
               "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
               "--pids-limit", "256"]
        if backend == "cuda":
            cmd += ["--gpus", "all"]
        elif backend == "vulkan" and not _WIN:
            cmd += ["--device", "/dev/dri"]
        cmd += [image] + server_args(f"/models/{os.path.basename(model_path)}", ctx, 8080, key,
                                     fit, host="0.0.0.0")
        run = _docker(*cmd, timeout=120)
        if run.returncode != 0:
            raise LocalError("контейнер не запустился: " + (run.stderr or run.stdout)[-400:])
        st.update(container=CONTAINER, image=image)
    else:
        if not exe:
            raise LocalError("llama-server не установлен")
        if _WIN:
            _mark_low_integrity(exe)
        flags = 0
        if _WIN:
            flags = _NO_WINDOW | 0x4 | 0x200      # CREATE_SUSPENDED | NEW_PROCESS_GROUP
        run_dir = _sub(env, "run")
        with open(log_path, "w", encoding="utf-8", errors="replace") as logf_:
            proc = subprocess.Popen([exe] + server_args(model_path, ctx, port, key, fit),
                                    stdout=logf_, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                    cwd=run_dir, env=_clean_env(os.path.dirname(exe)),
                                    creationflags=flags, start_new_session=not _WIN)
        if _WIN:
            try:
                _jail_windows(proc)
            except Exception:
                proc.kill()
                raise
        st.update(pid=proc.pid, exe=exe)
    save_state(env, st)
    logf(f"llama-server стартует на 127.0.0.1:{port} — загружаю веса в память")

    deadline = time.time() + 1800
    while time.time() < deadline:
        if stop and stop():
            stop_running(env, logf)
            raise Stopped("запуск остановлен")
        if api_ok(port, key):
            st["ready"] = True
            save_state(env, st)
            return st
        # proc.poll() обязателен: на Linux/macOS упавший дочерний процесс остаётся зомби,
        # проверка «жив ли PID» на нём проходит, и запуск висел бы все 30 минут
        if (proc is not None and proc.poll() is not None) or not _running(st):
            text = tail(log_path) if isolation != "docker" else _docker("logs", "--tail", "25", CONTAINER).stdout
            raise LocalError("llama-server завершился при запуске. Последние строки лога:\n" + text)
        time.sleep(3)
    raise LocalError("llama-server не ответил за 30 минут")


def _pid_exe(pid):
    """Имя программы под этим PID; пустая строка — узнать не удалось."""
    try:
        if _WIN:
            k32 = ctypes.windll.kernel32
            h = k32.OpenProcess(0x1000, False, int(pid))   # QUERY_LIMITED_INFORMATION
            if not h:
                return ""
            try:
                buf = ctypes.create_unicode_buffer(32768)
                n = ctypes.c_ulong(len(buf))
                return buf.value if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)) else ""
            finally:
                k32.CloseHandle(h)
        try:
            with open(f"/proc/{int(pid)}/cmdline", "rb") as f:
                return f.read().split(b"\0")[0].decode(errors="replace")
        except OSError:
            return subprocess.run(["ps", "-p", str(int(pid)), "-o", "comm="], capture_output=True,
                                  text=True, timeout=5).stdout.strip()
    except Exception:  # noqa
        return ""


def _foreign_pid(st):
    """
    Истина, если под PID из run.json теперь точно другая программа. После перезагрузки
    номера процессов раздаются заново: без проверки статус показывал «загружается» для
    чужого процесса, а «Остановить» на Linux/macOS посылал ему сигнал завершения.
    """
    name = os.path.basename(_pid_exe(st.get("pid")) or "").lower()
    want = os.path.basename(st.get("exe") or "llama-server").lower()
    return bool(name) and name != want and not name.startswith("llama-server")


def _running(st):
    if st.get("container"):
        r = _docker("inspect", "-f", "{{.State.Running}}", st["container"], timeout=15)
        return r.returncode == 0 and r.stdout.strip() == "true"
    return _alive_pid(st.get("pid")) and not _foreign_pid(st)


def own_vram_gib(env=None):
    """Сколько видеопамяти занимает запущенная нами модель (0 — ничего не запущено)."""
    try:
        st = load_state(env)
        if st and st.get("vram_gib") and _running(st):
            return float(st["vram_gib"])
    except Exception:  # noqa — это справка для подбора, она не должна ничего ронять
        pass
    return 0.0


def status(env=None):
    st = load_state(env)
    if not st:
        return {"running": False}
    running = _running(st)
    if running and st.get("pid"):
        st = {**st, "isolation_check": isolation_report(st["pid"])}
    return {**{k: v for k, v in st.items() if k != "key"}, "running": running,
            "ready": running and api_ok(st["port"], st["key"]),
            "key_hint": st["key"][:4] + "…" + st["key"][-4:] if st.get("key") else ""}


def stop_running(env=None, logf=print):
    st = load_state(env)
    if not st:
        return False
    stopped = False
    if st.get("container"):
        r = _docker("rm", "-f", st["container"])
        stopped = r.returncode == 0
    elif st.get("pid") and _alive_pid(st["pid"]) and not _foreign_pid(st):
        # Убиваем, только если под этим PID всё ещё llama-server, а не чужой процесс. Имя
        # берём у системы напрямую: раньше его искали в выводе tasklist, а на русской
        # Windows тот не читался как UTF-8 — и остановка падала, не убив ничего.
        if _WIN:
            subprocess.run(["taskkill", "/PID", str(st["pid"]), "/F"], capture_output=True,
                           creationflags=_NO_WINDOW)
            stopped = True
        else:
            try:
                os.kill(int(st["pid"]), 15)
                stopped = True
            except OSError:
                pass
    if stopped:
        logf(f"остановил llama-server ({st.get('repo')})")
    try:
        os.remove(_state_path(env))
    except OSError:
        pass
    return stopped


def run_key(env=None):
    return load_state(env).get("key")

