# -*- coding: utf-8 -*-
"""
localpc.py — режим «Ставить на мой ПК»: что за железо стоит на машине, где запущен
софт, и какие модели на нём поднимутся без аренды сервера.

Железо определяется каждый раз на месте, ничего не зашито: видеокарты NVIDIA — через
nvidia-smi, прочие дискретные — через реестр Windows или sysfs на Linux, Apple Silicon —
как общая память. Если определение ошиблось, его поправляют вручную: PC_VRAM_GB и
PC_RAM_GB в .env (поля на вкладке «Мой ПК»).

Запускать локально будем llama.cpp, поэтому всерьёз оцениваются только GGUF: у него и
выгрузка экспертов в оперативку (--n-cpu-moe), и частичная выгрузка слоёв (-ngl), и
сборки под CUDA, Vulkan и Metal. Всё на stdlib.
"""
import ctypes
import os
import platform
import re
import shutil
import subprocess
import threading
import time

import hfmodels

GIB = 1024 ** 3

# Видеопамять, которую занимает не модель: рабочий стол Windows, браузер, сам драйвер.
# На сервере Clore карта пустая, на домашнем ПК — никогда.
VRAM_RESERVE_GIB = 1.0
# Сколько оперативки оставить системе и открытым программам: модель, отъевшая всю
# память, уводит Windows в своп, и тормозит уже весь компьютер, а не только генерация.
RAM_RESERVE_MIN_GIB = 6.0
RAM_RESERVE_SHARE = 0.12
# Apple Silicon: память общая, Metal по умолчанию отдаёт GPU примерно две трети от неё.
UNIFIED_GPU_SHARE = 0.66
# Запас поверх оценки: она не идеальна, а OOM на своём ПК — это ещё и подвисшая система.
LOCAL_HEADROOM = 1.05
# Ниже этого контекста кодить уже нельзя — не считаем «влезает», даже если по памяти да.
LOCAL_MIN_CTX = 16384
# Контекст по умолчанию для медленных режимов (часть слоёв или всё на CPU).
SLOW_MODE_CTX = 32768
# Буферы llama.cpp в оперативке поверх весов, которые туда уехали.
RAM_RUNTIME_GIB = 1.5
# Если на карту помещается меньше этой доли слоёв, считает по сути CPU.
MIN_GPU_SHARE = 0.25
# Карты с меньшим объёмом — встроенная графика, а не место для модели.
MIN_DGPU_GIB = 3.0
_IGPU_RE = re.compile(r"^(amd )?radeon\(tm\) graphics$|intel.*(uhd|iris|hd graphics)|"
                      r"microsoft basic|remote display|virtual|parsec|meta virtual", re.I)

_WIN = os.name == "nt"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ---------- определение железа ----------
def _run(cmd, timeout=6):
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                             creationflags=_NO_WINDOW if _WIN else 0)
        return out.stdout if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _nvidia_smi():
    exe = shutil.which("nvidia-smi")
    if not exe and _WIN:
        # установщик драйвера кладёт его сюда, но в PATH добавляет не всегда
        for p in (r"C:\Windows\System32\nvidia-smi.exe",
                  r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe"):
            if os.path.exists(p):
                exe = p
                break
    return exe


def _gpus_nvidia():
    exe = _nvidia_smi()
    if not exe:
        return []
    out = _run([exe, "--query-gpu=name,memory.total,memory.used,driver_version",
                "--format=csv,noheader,nounits"])
    gpus = []
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            continue
        try:
            total, used = float(parts[1]) / 1024, float(parts[2]) / 1024
        except ValueError:
            continue
        gpus.append({"name": parts[0], "vendor": "nvidia", "vram_gib": round(total, 1),
                     "used_gib": round(used, 1),
                     "driver": parts[3] if len(parts) > 3 else ""})
    return gpus


def _gpus_windows_registry():
    """Все видеоадаптеры из реестра: там 64-битный объём, а не обрезанный до 4 ГБ из WMI."""
    try:
        import winreg
    except ImportError:
        return []
    base = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
    gpus = []
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base)
    except OSError:
        return []
    i = 0
    while True:
        try:
            sub = winreg.EnumKey(root, i)
        except OSError:
            break
        i += 1
        if not sub.isdigit():
            continue
        try:
            key = winreg.OpenKey(root, sub)
        except OSError:
            continue
        def val(name):
            try:
                return winreg.QueryValueEx(key, name)[0]
            except OSError:
                return None
        name = val("DriverDesc") or ""
        mem = val("HardwareInformation.qwMemorySize") or val("HardwareInformation.MemorySize")
        if isinstance(mem, bytes):
            mem = int.from_bytes(mem[:8], "little")
        if not name or not mem:
            continue
        low = name.lower()
        vendor = ("nvidia" if "nvidia" in low else "amd" if ("radeon" in low or "amd" in low)
                  else "intel" if "intel" in low else "other")
        gpus.append({"name": name, "vendor": vendor, "vram_gib": round(int(mem) / GIB, 1),
                     "used_gib": None, "driver": ""})
    return gpus


def _gpus_linux_sysfs():
    """AMD и Intel на Linux: amdgpu и xe отдают объём видеопамяти в sysfs."""
    gpus = []
    drm = "/sys/class/drm"
    if not os.path.isdir(drm):
        return gpus
    for card in sorted(os.listdir(drm)):
        if not re.fullmatch(r"card\d+", card):
            continue
        dev = os.path.join(drm, card, "device")
        try:
            with open(os.path.join(dev, "mem_info_vram_total")) as f:
                total = int(f.read().strip())
        except (OSError, ValueError):
            continue
        used = None
        try:
            with open(os.path.join(dev, "mem_info_vram_used")) as f:
                used = round(int(f.read().strip()) / GIB, 1)
        except (OSError, ValueError):
            pass
        vendor_id = ""
        try:
            with open(os.path.join(dev, "vendor")) as f:
                vendor_id = f.read().strip()
        except OSError:
            pass
        vendor = {"0x1002": "amd", "0x8086": "intel", "0x10de": "nvidia"}.get(vendor_id, "other")
        gpus.append({"name": f"{vendor.upper()} GPU ({card})", "vendor": vendor,
                     "vram_gib": round(total / GIB, 1), "used_gib": used, "driver": ""})
    return gpus


def _ram_gib():
    if _WIN:
        class MEMSTAT(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        st = MEMSTAT()
        st.dwLength = ctypes.sizeof(MEMSTAT)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            return st.ullTotalPhys / GIB, st.ullAvailPhys / GIB
        return 0.0, 0.0
    if os.path.exists("/proc/meminfo"):
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, v = line.partition(":")
                info[k] = int(v.split()[0]) * 1024 if v.split() else 0
        return info.get("MemTotal", 0) / GIB, info.get("MemAvailable", 0) / GIB
    out = _run(["sysctl", "-n", "hw.memsize"])
    try:
        return int(out.strip()) / GIB, 0.0
    except ValueError:
        return 0.0, 0.0


def _cpu_name():
    if _WIN:
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                 r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
            return str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip()
        except OSError:
            return platform.processor()
    if os.path.exists("/proc/cpuinfo"):
        with open("/proc/cpuinfo", errors="replace") as f:
            for line in f:
                if line.lower().startswith("model name"):
                    return line.split(":", 1)[1].strip()
    out = _run(["sysctl", "-n", "machdep.cpu.brand_string"]).strip()
    return out or platform.processor() or platform.machine()


def _apple_silicon():
    return platform.system() == "Darwin" and platform.machine() in ("arm64", "aarch64")


def _num(v):
    try:
        x = float(str(v).replace(",", ".").strip())
        return x if x > 0 else None
    except (TypeError, ValueError):
        return None


def detect(env=None):
    """
    Железо этой машины и сколько из него реально можно отдать модели.
    env — .env проекта: PC_VRAM_GB / PC_RAM_GB перекрывают определённые значения.
    """
    env = env or {}
    warnings = []
    ram_total, ram_free = _ram_gib()

    unified = _apple_silicon()
    if unified:
        chip = _cpu_name()
        gpus = [{"name": f"{chip} (общая память)", "vendor": "apple",
                 "vram_gib": round(ram_total * UNIFIED_GPU_SHARE, 1), "used_gib": None,
                 "driver": ""}]
        backend = "metal"
    else:
        gpus = _gpus_nvidia()
        if gpus:
            backend = "cuda"
        else:
            found = _gpus_windows_registry() if _WIN else _gpus_linux_sysfs()
            gpus = [g for g in found
                    if g["vram_gib"] >= MIN_DGPU_GIB and not _IGPU_RE.search(g["name"])]
            if any(g["vendor"] == "nvidia" for g in found):
                warnings.append("видеокарта NVIDIA есть, но nvidia-smi не отвечает — "
                                "обновите драйвер, иначе llama.cpp не увидит CUDA")
            backend = "vulkan" if gpus else "cpu"
        if not gpus:
            warnings.append("дискретная видеокарта не найдена — модель будет считать "
                            "процессор, это медленно. Если карта есть, укажите её VRAM вручную")

    vram_total = sum(g["vram_gib"] for g in gpus)
    card_gib = max((g["vram_gib"] for g in gpus), default=0.0)
    used_now = sum(g["used_gib"] or 0 for g in gpus)

    over_vram = _num(env.get("PC_VRAM_GB"))
    over_ram = _num(env.get("PC_RAM_GB"))
    if over_vram:
        vram_total = card_gib = over_vram
        if backend == "cpu":
            backend = "vulkan"
    if over_ram:
        ram_total = over_ram

    if unified:
        # память одна: что отдали GPU, того нет у процессора, и наоборот
        vram_reserve = 0.0
        ram_reserve = ram_total
    else:
        # Сейчас занятая видеопамять — лучшая оценка того, что будет занято и при запуске
        # модели: рабочий стол и браузер никуда не денутся. Но не меньше гигабайта и
        # не больше трети карты — вдруг в момент проверки была открыта игра.
        vram_reserve = (min(max(VRAM_RESERVE_GIB, used_now), vram_total / 3)
                        if vram_total else 0.0)
        ram_reserve = max(RAM_RESERVE_MIN_GIB, ram_total * RAM_RESERVE_SHARE)

    try:
        disk_free = shutil.disk_usage(os.path.expanduser("~")).free / GIB
    except OSError:
        disk_free = 0.0

    return {
        "os": f"{platform.system()} {platform.release()}",
        "cpu": _cpu_name(),
        "cores": os.cpu_count() or 0,
        "gpus": gpus,
        "backend": backend,                     # под какую сборку llama.cpp
        "unified": unified,
        "vram_total_gib": round(vram_total, 1),
        "card_gib": round(card_gib, 1),
        "vram_reserve_gib": round(vram_reserve, 1),
        "vram_usable_gib": round(max(vram_total - vram_reserve, 0), 1),
        "ram_total_gib": round(ram_total, 1),
        "ram_free_gib": round(ram_free, 1),
        "ram_reserve_gib": round(ram_reserve, 1),
        "ram_usable_gib": round(max(ram_total - ram_reserve, 0), 1),
        "disk_free_gib": round(disk_free, 1),
        "override": {"vram": over_vram, "ram": over_ram},
        "warnings": warnings,
    }


_PC = {"at": 0.0, "key": None, "data": None}
_PC_LOCK = threading.Lock()
PC_TTL = 60   # занятая VRAM меняется, но опрашивать nvidia-smi на каждую модель незачем


def pc(env=None, refresh=False):
    env = env or {}
    key = (env.get("PC_VRAM_GB"), env.get("PC_RAM_GB"))
    with _PC_LOCK:
        if refresh or _PC["data"] is None or _PC["key"] != key or time.time() - _PC["at"] > PC_TTL:
            _PC.update(data=detect(env), at=time.time(), key=key)
        return _PC["data"]


# ---------- влезет ли модель ----------
# Режимы от лучшего к худшему. Порядок важен: при выборе кванта сначала смотрим на режим.
MODES = ["gpu", "moe", "partial", "cpu"]
MODE_TEXT = {
    "gpu": ("целиком в видеокарте", "быстро"),
    "moe": ("эксперты в оперативке", "средне: экспертов считает процессор"),
    "partial": ("часть слоёв в оперативке", "медленно: каждый токен ждёт процессор"),
    "cpu": ("на процессоре", "очень медленно"),
}


def _try_modes(w, kv, vocab, moe_off, layers, p):
    """Лучший режим, в котором квант w с KV-кэшем kv помещается в этот ПК, или None."""
    V, R, card = p["vram_usable_gib"], p["ram_usable_gib"], p["card_gib"]
    need = hfmodels.vram_estimate(w, kv, vocab) * LOCAL_HEADROOM

    if V and need <= V:
        return {"mode": "gpu", "vram_gib": round(need, 1), "ram_gib": 0.0}

    if V and moe_off:
        # card — объём одной карты за вычетом занятого: на ней же живут внимание и KV
        n = hfmodels.cpu_moe_layers(moe_off, need, V, min(card, V) if card else None)
        gpu = max(need - n * moe_off["per_layer_gib"], moe_off["vram_gib"] * LOCAL_HEADROOM)
        ram = n * moe_off["per_layer_gib"] * LOCAL_HEADROOM + RAM_RUNTIME_GIB
        if gpu <= V and ram <= R:
            return {"mode": "moe", "vram_gib": round(gpu, 1), "ram_gib": round(ram, 1),
                    "n_cpu_moe": n, "moe_layers": moe_off["max_layers"]}

    if V and w and not p["unified"]:
        fixed = need - w * LOCAL_HEADROOM          # KV и буферы движка, без весов
        share = min((V - fixed) / (w * LOCAL_HEADROOM), 1.0)
        ram = w * (1 - share) * LOCAL_HEADROOM + RAM_RUNTIME_GIB
        if share >= MIN_GPU_SHARE and ram <= R:
            out = {"mode": "partial", "vram_gib": round(V, 1), "ram_gib": round(ram, 1),
                   "gpu_share": round(share, 2)}
            if layers:
                out["ngl"] = int(layers * share)
            return out

    ram = (w + kv) * LOCAL_HEADROOM + RAM_RUNTIME_GIB
    if not p["unified"] and ram <= R:
        return {"mode": "cpu", "vram_gib": 0.0, "ram_gib": round(ram, 1)}
    return None


def _ctx_steps(ctx):
    """Запрошенный контекст и его половины, пока не упрёмся в минимум для кода."""
    out = [ctx]
    while out[-1] // 2 >= LOCAL_MIN_CTX:
        out.append(out[-1] // 2)
    return out


def fit(info, p):
    """
    Что получится с моделью на этом ПК. info — результат hfmodels.model_info.
    Для каждого кванта ищется лучший режим, а если не влезает на полном контексте —
    контекст уменьшается вдвое, пока не дойдёт до LOCAL_MIN_CTX.
    """
    if info.get("engine") != hfmodels.ENGINE_LLAMACPP:
        return {"status": "need_gguf",
                "text": "локально запускается llama.cpp — нужна GGUF-сборка этой модели"}

    ctx0 = int(info.get("ctx") or hfmodels.DEFAULT_CTX)
    kv0 = float(info.get("kv_gib") or 0)
    vocab = int(info.get("vocab") or 0)
    layers = int(info.get("layers") or 0)
    options = info.get("gguf_options") or []
    if not options and info.get("weights_gib"):
        options = [{"path": info.get("gguf_file") or "", "label": info.get("gguf_file") or "",
                    "weights_gib": info["weights_gib"]}]

    rank = {m: i for i, m in enumerate(MODES)}
    rows = []
    for o in options:
        w = float(o["weights_gib"])
        # Режим важнее контекста: эксперты в RAM на 64k в разы быстрее, чем «всё на
        # процессоре» на 128k. Поэтому перебираем все шаги и берём лучший режим, а
        # контекст — наибольший из тех, где этот режим ещё держится.
        tries = []
        for ctx in _ctx_steps(ctx0):
            kv = kv0 * ctx / ctx0 if ctx0 else kv0
            moe_off = hfmodels.cpu_moe_estimate(info.get("moe"), w, kv, vocab)
            res = _try_modes(w, kv, vocab, moe_off, layers, p)
            if res:
                tries.append({**res, "ctx": ctx})
        res = None
        if tries:
            top = min(rank[t["mode"]] for t in tries)
            tries = [t for t in tries if rank[t["mode"]] == top]
            if MODES[top] in ("partial", "cpu"):
                # здесь каждый гигабайт KV отнимает слои у видеокарты — длинный контекст
                # оборачивается вдвое меньшей скоростью, берём рабочий минимум
                short = [t for t in tries if t["ctx"] <= SLOW_MODE_CTX]
                res = max(short, key=lambda t: t["ctx"]) if short else min(tries, key=lambda t: t["ctx"])
            else:
                res = max(tries, key=lambda t: t["ctx"])
        row = {"path": o["path"], "label": o["label"], "weights_gib": w}
        row.update(res or {"mode": None})
        rows.append(row)

    fitting = [r for r in rows if r["mode"]]
    if not fitting:
        smallest = min(rows, key=lambda r: r["weights_gib"]) if rows else None
        return {"status": "no", "options": rows,
                "text": ("не влезет даже самый лёгкий квант"
                         + (f" ({smallest['label']}, {smallest['weights_gib']} ГиБ)" if smallest else "")
                         + f": на этом ПК для модели есть {p['vram_usable_gib']} ГиБ VRAM "
                           f"и {p['ram_usable_gib']} ГиБ RAM")}

    # лучший режим, в нём — самый длинный контекст, а среди равных — привычный приоритет квантов
    top_mode = min(rank[r["mode"]] for r in fitting)
    same = [r for r in fitting if rank[r["mode"]] == top_mode]
    best_ctx = max(r["ctx"] for r in same)
    same = [r for r in same if r["ctx"] == best_ctx]
    if MODES[top_mode] == "gpu":
        # Влезает целиком — свободная видеопамять своего ПК иначе простаивает, поэтому
        # берём самый точный квант из влезающих. Но не f16/bf16: вдвое тяжелее Q8_0 и
        # вдвое медленнее, а разницы в коде не заметить.
        heavy = re.compile(r"(^|[-_./])(b?f16|fp16|f32|fp32)($|[-_.])", re.I)
        pool = [r for r in same if not heavy.search(r["label"])] or same
        picked = max(pool, key=lambda r: r["weights_gib"])
    else:
        # в медленных режимах каждый гигабайт — это скорость, держим привычный Q4_K_M
        picked = hfmodels._pick_gguf(same) or same[-1]

    where, speed = MODE_TEXT[picked["mode"]]
    text = f"{picked['label']}: {where}, контекст {best_ctx // 1024}k — {speed}"
    if best_ctx < ctx0:
        text += f" (на {ctx0 // 1024}k не хватает памяти)"
    return {"status": picked["mode"], "best": picked, "options": rows, "text": text,
            "speed": speed}


# Карточки моделей для массовой проверки списка. model_info — это 3–6 запросов к HF,
# а подборку перепроверяют при каждом переключении вкладки.
_INFO = {}
_INFO_LOCK = threading.Lock()
INFO_TTL = 1800


def model_info_cached(ref, ctx=None, token=None):
    key = (hfmodels.normalize_ref(ref), ctx)
    with _INFO_LOCK:
        hit = _INFO.get(key)
        if hit and time.time() - hit[0] < INFO_TTL:
            return hit[1]
    info = hfmodels.model_info(ref, ctx=ctx, token=token)
    with _INFO_LOCK:
        _INFO[key] = (time.time(), info)
    return info
