# -*- coding: utf-8 -*-
"""
app.py — локальный веб-UI: выбор uncensored-модели с Hugging Face (из списка или по ссылке),
ключ API Clore.ai, подбор и аренда GPU-сервера, статус и отмена заказа.

Запуск: ui.bat  (или python app.py [--port 8765] [--no-browser])
Слушает только 127.0.0.1 — ключи из .env наружу не уходят.
"""
import argparse
import json
import os
import sys
import threading
import time
import traceback
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import deploy
import hfmodels
import localpc
import localrun
from clore import PRICE_PERIOD_HOURS, Clore, CloreError, order_access, server_brief

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")

for _stream in (sys.stdout, sys.stderr):  # консоль Windows по умолчанию cp1252
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

# ключи .env, которыми управляет UI: имя -> секретный ли
SETTING_KEYS = {
    "CLORE_API_KEY": True,
    "API_KEY": True,
    "HF_TOKEN": True,
    "HF_TOKEN_WRITE": True,
    "TG_BOT_TOKEN": True,
    "TG_CHAT_ID": False,
    "EXCLUDE_HOSTS": False,
    # режим «Мой ПК»: ручная поправка, если железо определилось неверно (пусто = как есть)
    "PC_VRAM_GB": False,
    "PC_RAM_GB": False,
    # куда класть llama.cpp и модели для своего ПК (пусто = профиль пользователя)
    "LOCAL_DIR": False,
}

JOB = {"running": False, "log": [], "result": None, "error": None, "started": None, "title": None}
JOB_LOCK = threading.Lock()
# Поднимается, когда заказ идущего деплоя отменили из UI: поток деплоя видит флаг и
# останавливается, а не арендует следующий сервер вместо «неподнявшегося».
JOB_STOP = threading.Event()

# Имена, под которыми UI открывают законно. Запрос с любым другим Host — это чужая
# страница, дотянувшаяся до локального порта (DNS rebinding), а не наш интерфейс.
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
ALLOWED_HOSTS = set(LOCAL_HOSTS)
OPEN_BIND = False     # слушаем все интерфейсы: имя хоста заранее неизвестно


# ---------- фоновая задача деплоя ----------
def job_log(msg):
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    with JOB_LOCK:
        JOB["log"].append(line)
        JOB["log"][:] = JOB["log"][-500:]
    print(line, flush=True)


def run_deploy(cfg):
    with JOB_LOCK:
        if JOB["running"]:
            raise RuntimeError("деплой уже идёт")
        JOB.update(running=True, log=[], result=None, error=None,
                   started=time.time(), title=cfg.get("model"))
    JOB_STOP.clear()
    cfg = {**cfg, "stop": JOB_STOP.is_set}

    def worker():
        try:
            res = deploy.deploy(cfg, logf=job_log)
            with JOB_LOCK:
                JOB["result"] = res
            job_log("задача завершена: " + str(res.get("status")))
        except deploy.DeployStopped as e:
            # не ошибка: заказ отменил сам пользователь, и деплой послушно встал
            with JOB_LOCK:
                JOB["result"] = {"status": "stopped"}
            job_log(f"⏹ {e}. Очередь серверов дальше не идёт")
        except BaseException as e:  # noqa
            with JOB_LOCK:
                JOB["error"] = str(e) or e.__class__.__name__
            job_log(f"⛔ {e}")
            if not isinstance(e, (SystemExit, CloreError, RuntimeError)):
                job_log(traceback.format_exc(limit=3))
        finally:
            with JOB_LOCK:
                JOB["running"] = False

    threading.Thread(target=worker, daemon=True).start()


# ---------- запуск на своём ПК ----------
# Отдельная задача, а не JOB: аренда на Clore и локальный запуск друг другу не мешают,
# и лог одного не должен затирать лог другого.
LJOB = {"running": False, "log": [], "result": None, "error": None, "started": None, "title": None}
LJOB_LOCK = threading.Lock()
LJOB_STOP = threading.Event()


def ljob_log(msg):
    line = time.strftime("%H:%M:%S ") + str(msg)
    print("[мой ПК] " + line, flush=True)
    with LJOB_LOCK:
        LJOB["log"].append(line)
        del LJOB["log"][:-400]


def run_local(env, body, token):
    """Скачать llama.cpp и модель, проверить, запустить — в фоне, с логом для UI."""
    ref = (body.get("ref") or "").strip()
    if not ref:
        raise ValueError("не выбрана модель")
    isolation = body.get("isolation") or "native"
    if isolation not in ("native", "docker"):
        raise ValueError("неизвестный режим изоляции")
    with LJOB_LOCK:
        if LJOB["running"]:
            raise RuntimeError("уже идёт установка — дождитесь или остановите её")
        LJOB.update(running=True, log=[], result=None, error=None, started=time.time(), title=ref)
    LJOB_STOP.clear()

    def worker():
        try:
            ctx = int(body["ctx"]) if body.get("ctx") else None
            info = localpc.model_info_cached(ref, ctx=ctx, token=token)
            if info["engine"] != hfmodels.ENGINE_LLAMACPP:
                raise localrun.LocalError("на своём ПК запускаются только GGUF-сборки")
            pc = localpc.pc(env)
            fit = localpc.fit(info, pc)
            want = body.get("gguf_file") or (fit.get("best") or {}).get("path")
            row = next((r for r in fit.get("options", []) if r["path"] == want), None)
            if not row or not row.get("mode"):
                raise localrun.LocalError("этот квант на этот ПК не влезает — выберите другой")
            ljob_log(f"{row['label']}: {localpc.MODE_TEXT[row['mode']][0]}, контекст {row['ctx']}")
            exe = None
            if isolation == "native":
                exe, meta = localrun.install_llama(env, pc["backend"], logf=ljob_log,
                                                   stop=LJOB_STOP.is_set)
                ljob_log(f"llama.cpp {meta['tag']} ({meta['variant']}) готов")
            path = localrun.download_model(env, info["repo_id"], row["path"], token=token,
                                           logf=ljob_log, stop=LJOB_STOP.is_set)
            st = localrun.start(env, info["repo_id"], path, row["ctx"], row, isolation,
                                pc["backend"], exe=exe, logf=ljob_log, stop=LJOB_STOP.is_set)
            ljob_log(f"✅ модель отвечает: {st['url']}")
            with LJOB_LOCK:
                LJOB["result"] = {"status": "ready", "url": st["url"]}
        except localrun.Stopped as e:
            ljob_log(f"⏹ {e}")
            with LJOB_LOCK:
                LJOB["result"] = {"status": "stopped"}
        except BaseException as e:  # noqa
            ljob_log(f"⛔ {e}")
            if not isinstance(e, (RuntimeError, ValueError, OSError)):
                ljob_log(traceback.format_exc(limit=3))
            with LJOB_LOCK:
                LJOB["error"] = str(e) or e.__class__.__name__
        finally:
            with LJOB_LOCK:
                LJOB["running"] = False

    threading.Thread(target=worker, daemon=True).start()


def local_status(env):
    st = localrun.status(env)
    exe = st.get("exe")
    return {
        "run": st,
        "docker": localrun.docker_available(),
        "firewall": localrun.firewall_rule_present(exe) if exe else None,
        "models": localrun.list_models(env),
        "dir": localrun.data_dir(env),
        "windows": os.name == "nt",
    }


# ---------- обработчики API ----------
def env_state():
    env = deploy.load_env()
    settings = {}
    for k, secret in SETTING_KEYS.items():
        val = env.get(k) or ""
        # secret нужен и UI: у секретов пустое поле означает «не менять» (показать их
        # нечем), у обычных пустота — это значение, и его надо уметь сохранить
        settings[k] = {"set": bool(val), "secret": secret,
                       "value": "" if secret and val else val,
                       "hint": (val[:4] + "…" + val[-4:]) if secret and len(val) > 10 else ""}
    api_url = env.get("API_URL") or ""
    alive = bool(api_url and env.get("API_KEY") and deploy.api_alive(api_url, env["API_KEY"], timeout=4))
    return {
        "settings": settings,
        "current": {
            "order": env.get("ORDER") or "",
            "server": env.get("SERVER") or "",
            "api_url": api_url,
            "ssh": env.get("SSH") or "",
            "model_repo": env.get("MODEL_REPO") or "",
            "model_name": env.get("MODEL_NAME") or deploy.MODEL_NAME,
            "engine": env.get("ENGINE") or "",
            "ctx": env.get("CTX") or "",
            "alive": alive,
            # заказы прерванной гонки: остаются, только если деплой упал посреди неё
            "race_orders": [] if JOB["running"] else
            [x for x in (env.get("RACE_ORDERS") or "").split(",") if x.strip()],
        },
        "defaults": {"ctx": deploy.DEFAULT_CTX, "images": deploy.IMAGES,
                     "api_port": deploy.API_PORT},
    }


def api_get(path, q):
    env = deploy.load_env()
    token = env.get("HF_TOKEN") or None

    if path == "/api/state":
        return env_state()

    if path == "/api/models":
        return hfmodels.search(
            query=(q.get("q") or [""])[0].strip() or None,
            period=(q.get("period") or [hfmodels.DEFAULT_PERIOD])[0],
            fmt=(q.get("fmt") or [""])[0] or None,
            limit=int((q.get("limit") or ["40"])[0]),
            token=token)

    if path == "/api/top":
        # kind: 'uncensored' — подборка максимально расцензуренных, 'top' — кодеры
        return hfmodels.top_coding(fmt=(q.get("fmt") or [""])[0] or None,
                                   kind=(q.get("kind") or [""])[0] or hfmodels.DEFAULT_TOP,
                                   token=token)

    if path == "/api/model":
        ref = (q.get("ref") or [""])[0]
        ctx = (q.get("ctx") or [""])[0]
        info = localpc.model_info_cached(ref, ctx=int(ctx) if ctx else None, token=token)
        # оценка «на моём ПК» едет вместе с карточкой: железо опрошено и закешировано
        return {"model": {**info, "local": localpc.fit(info, localpc.pc(env))}}

    if path == "/api/local/pc":
        return {"pc": localpc.pc(env, refresh=(q.get("refresh") or [""])[0] == "1")}

    if path == "/api/local/status":
        return local_status(env)

    if path == "/api/local/job":
        with LJOB_LOCK:
            return {"running": LJOB["running"], "log": list(LJOB["log"]), "result": LJOB["result"],
                    "error": LJOB["error"], "title": LJOB["title"],
                    "elapsed": int(time.time() - LJOB["started"]) if LJOB["started"] else 0}

    if path == "/api/local/fit":
        # одна модель за запрос: UI проверяет список по несколько штук параллельно,
        # и отметки появляются по мере готовности, а не после самой медленной
        ref = (q.get("ref") or [""])[0]
        ctx = (q.get("ctx") or [""])[0]
        p = localpc.pc(env)
        try:
            info = localpc.model_info_cached(ref, ctx=int(ctx) if ctx else None, token=token)
        except (RuntimeError, ValueError) as e:
            return {"ref": ref, "fit": {"status": "unknown", "text": str(e)}}
        return {"ref": ref, "fit": localpc.fit(info, p)}

    if path == "/api/wallets":
        clore = Clore(env.get("CLORE_API_KEY"))
        rates = clore.currency_rates()
        out = []
        for w in clore.wallets():
            name = w.get("name") or ""
            try:
                amount = float(w.get("balance") or 0)
            except (TypeError, ValueError):
                amount = 0.0
            rate = rates.get(name)
            out.append({**w, "balance": amount, "rate_usd": rate,
                        "balance_usd": round(amount * rate, 2) if rate else None})
        return {"wallets": out, "total_usd": round(sum(w["balance_usd"] or 0 for w in out), 2)}

    if path == "/api/orders":
        clore = Clore(env.get("CLORE_API_KEY"))
        servers = {int(s.get("id", -1)): s for s in clore.marketplace()}
        rates = clore.currency_rates(list(servers.values()))
        out = []
        for o in clore.my_orders():
            acc = order_access(o)
            sid = int(o.get("si", -1))
            brief = server_brief(servers[sid], rates) if sid in servers else {}
            rate = rates.get(o.get("currency") or "")
            as_usd = lambda v: (round(float(v) * rate, 3) if rate and isinstance(v, (int, float)) else None)  # noqa: E731
            price_day = as_usd(o.get("price"))   # цена заказа у Clore — за сутки
            out.append({
                "id": o.get("id"), "server": sid, "image": o.get("image"),
                "price_usd": round(price_day / PRICE_PERIOD_HOURS, 3) if price_day else None,
                "spend_usd": as_usd(o.get("spend")),
                "price": o.get("price"), "currency": o.get("currency"), "spend": o.get("spend"),
                "spot": bool(o.get("spot")), "created": o.get("ct"), "expired": bool(o.get("expired")),
                "host": acc["host"], "ports": acc["ports"], "http_url": acc["http_url"],
                "gpu": brief.get("gpu"), "gpu_count": brief.get("gpu_count"),
                "vram_gb": brief.get("vram_gb"),
                "api_url": (f"http://{acc['host']}:{acc['ports'][deploy.API_PORT]}/v1"
                            if acc["host"] and acc["ports"].get(deploy.API_PORT) else None),
            })
        out.sort(key=lambda x: x.get("created") or 0, reverse=True)
        return {"orders": out}

    if path == "/api/offers":
        clore = Clore(env.get("CLORE_API_KEY"))
        num = lambda k, d=None: (float(q[k][0]) if q.get(k) and q[k][0] not in ("", "null") else d)  # noqa: E731
        # Оплата идёт по часам (проверено на живом заказе), поэтому баланс ограничивает
        # не цену сервера, а время его работы. Отдаём баланс как есть, чтобы в карточке
        # было видно, на сколько часов хватит денег.
        rates = clore.currency_rates()
        bal_usd = sum(a * rates.get(c, 0) for c, a in deploy.wallet_balances(clore).items())
        hidden = {}   # сколько серверов отсеял каждый фильтр — показываем под списком
        cpu_moe = (q.get("cpu_moe") or [""])[0] == "1"
        return {"balance_usd": round(bal_usd, 2),
                "max_hourly_usd": round(bal_usd, 2) if bal_usd else 0,
                "hidden": hidden,
                # эхом возвращаем режим: по нему UI понимает, что сортировка учитывала
                # каналы памяти, и показывает их в карточке сервера
                "cpu_moe": cpu_moe,
                "offers": deploy.find_offers(
            clore,
            need_vram=num("vram", 0),
            need_disk=num("disk", 0),
            max_price_usd=num("max_price"),
            gpu_filter=(q.get("gpu") or [""])[0] or None,
            order_type=(q.get("type") or ["on-demand"])[0],
            min_net=num("min_net", deploy.MIN_NET_DOWN_MBPS),
            need_ram=num("ram", 0),
            download_gib=num("weights", 0),
            # движок и квант нужны, чтобы не предлагать карты, на которых они не заведутся
            engine=(q.get("engine") or [""])[0] or None,
            quant=(q.get("quant") or [""])[0] or None,
            allow_old_gpu=(q.get("old_gpu") or [""])[0] == "1",
            # эксперты в оперативке: меняет не фильтр, а сортировку — сервер с восемью
            # каналами памяти выигрывает у вдвое более дешёвого двухканального
            cpu_moe=cpu_moe,
            balance_usd=bal_usd or None,
            hidden=hidden,
            limit=int(num("limit", 30)))}

    if path == "/api/blacklist":
        return {"servers": deploy.blacklist_load()}

    if path == "/api/job":
        with JOB_LOCK:
            return {"running": JOB["running"], "log": list(JOB["log"]), "result": JOB["result"],
                    "error": JOB["error"], "title": JOB["title"],
                    "elapsed": int(time.time() - JOB["started"]) if JOB["started"] else 0}

    raise FileNotFoundError(path)


def api_post(path, body):
    env = deploy.load_env()

    if path == "/api/settings":
        updates = {k: str(v).strip() for k, v in (body or {}).items() if k in SETTING_KEYS}
        # перевод строки внутри значения дописал бы в .env посторонний ключ
        bad = sorted(k for k, v in updates.items() if "\n" in v or "\r" in v)
        if bad:
            raise ValueError("значение должно быть одной строкой: " + ", ".join(bad))
        if updates:
            deploy.save_env_keys(updates)
        return {"ok": True, "saved": sorted(updates)}

    if path == "/api/local/run":
        run_local(env, body or {}, env.get("HF_TOKEN") or None)
        return {"ok": True}

    if path == "/api/local/stop":
        # останавливаем и установку (если идёт), и запущенный сервер
        LJOB_STOP.set()
        return {"ok": True, "stopped": localrun.stop_running(env, logf=ljob_log)}

    if path == "/api/local/delete":
        localrun.delete_model(env, str((body or {}).get("repo") or ""))
        return {"ok": True}

    if path == "/api/local/firewall":
        exe = localrun.load_state(env).get("exe")
        if not exe:
            raise ValueError("сначала запустите модель в обычном режиме — правило ставится на её llama-server")
        localrun.firewall_block(exe)
        return {"ok": True}

    if path == "/api/local/opencode":
        st = localrun.load_state(env)
        if not st or not localrun.status(env).get("running"):
            raise ValueError("на этом ПК модель не запущена")
        ok = deploy.update_opencode(st["url"], st["key"], st["repo"].split("/")[-1],
                                    logf=ljob_log, ctx=int(st["ctx"]), engine="llamacpp",
                                    origin="мой ПК")
        return {"ok": ok, "paths": list(deploy.OPENCODE_CFG_USED)}

    if path == "/api/claude/desktop-off":
        return {"ok": True, "removed": deploy.remove_claude_desktop()}

    if path == "/api/claude":
        # куда прописать Claude: target = cli | desktop, where = local | clore
        b = body or {}
        if b.get("where") == "local":
            st = localrun.load_state(env)
            if not st or not localrun.status(env).get("running"):
                raise ValueError("на этом ПК модель не запущена")
            url, key, ctx = st["url"], st["key"], int(st["ctx"])
            title, origin, engine = st["repo"].split("/")[-1], "мой ПК", "llamacpp"
        else:
            current = deploy.load_env()
            if not current.get("API_URL"):
                raise ValueError("нет развёрнутой модели — сначала разверните её")
            url, key = current["API_URL"], current.get("API_KEY", "")
            ctx = int(current.get("CTX") or deploy.DEFAULT_CTX)
            title, origin = (current.get("MODEL_REPO") or "").split("/")[-1], "Clore"
            engine = current.get("ENGINE")
        if b.get("target") == "desktop":
            return {"ok": True, **deploy.write_claude_desktop(url, key, title, origin, engine,
                                                               logf=ljob_log if b.get("where") == "local" else job_log)}
        return {"ok": True, **deploy.write_claude_launcher(url, key, ctx, title, origin, engine,
                                                            logf=ljob_log if b.get("where") == "local" else job_log)}

    if path == "/api/local/bench":
        st = localrun.load_state(env)
        if not st:
            raise ValueError("на этом ПК модель не запущена")
        speed = deploy.bench(st["url"], st["key"], localrun.MODEL_NAME)
        return {"ok": bool(speed), "tokens_per_sec": speed}

    if path == "/api/opencode":
        # Отдельная кнопка: деплой мог пройти до установки OpenCode, и тогда провайдера
        # прописывать было некуда. Заодно показывает точный путь — именно его ищут,
        # когда «модели нет в списке».
        current = deploy.load_env()
        if not current.get("API_URL"):
            raise ValueError("нет развёрнутой модели — сначала разверните её")
        ok = deploy.update_opencode(current["API_URL"], current.get("API_KEY", ""),
                                    (current.get("MODEL_REPO") or "").split("/")[-1],
                                    logf=job_log,
                                    ctx=int(current.get("CTX") or deploy.DEFAULT_CTX),
                                    engine=current.get("ENGINE"))
        return {"ok": ok, "paths": list(deploy.OPENCODE_CFG_USED)}

    if path == "/api/theme":
        # Оформление живёт отдельным файлом поверх style.css: так базовые стили остаются
        # нетронутыми, а выбранная тема — это несколько переопределённых переменных,
        # которые можно снять одной кнопкой.
        css = str((body or {}).get("css") or "")
        if (body or {}).get("reset"):
            css = ""
        if len(css) > 8000 or "</" in css:
            raise ValueError("слишком большая или подозрительная тема")
        name = (body or {}).get("name") or "исходное оформление"
        with open(os.path.join(STATIC, "theme.css"), "w", encoding="utf-8") as f:
            f.write("/* %s */\n%s" % (name, css))
        return {"ok": True, "name": (body or {}).get("name") or ""}

    if path == "/api/blacklist":
        # сервер мог попасть в список по разовой причине (упал Hugging Face, а не хост),
        # поэтому список обязан быть управляемым, а не только пополняемым
        data = deploy.blacklist_load()
        if (body or {}).get("clear"):
            data = {}
        elif (body or {}).get("remove") is not None:
            data.pop(str(body["remove"]), None)
        with open(deploy.BLACKLIST_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1, sort_keys=True)
        return {"ok": True, "servers": data}

    if path == "/api/deploy":
        cfg = {
            "model": (body.get("model") or "").strip(),
            "ctx": int(body["ctx"]) if body.get("ctx") else None,
            "max_price_usd": float(body["max_price"]) if body.get("max_price") else None,
            "currency": body.get("currency") or "auto",   # валюта списания; USD — только отображение
            "order_type": body.get("order_type") or "on-demand",
            # очередь серверов из UI: не поднялся первый — деплой идёт ко второму.
            # Одиночный server_id остаётся ради старых вызовов API
            "server_ids": deploy.picked_servers(body),
            "gpu_filter": (body.get("gpu_filter") or "").strip() or None,
            "image": (body.get("image") or "").strip() or None,
            "gguf_file": (body.get("gguf_file") or "").strip() or None,
            # эксперты MoE в оперативке: дешевле сервер ценой части скорости
            "cpu_moe": bool(body.get("cpu_moe")),
            # параллельная установка на всех выбранных серверах: остаётся самый быстрый
            "race": bool(body.get("race")),
            "ssh_password": (body.get("ssh_password") or "").strip() or None,
            "force": bool(body.get("force")),
            "dry_run": bool(body.get("dry_run")),
        }
        if not cfg["model"]:
            raise ValueError("не выбрана модель")
        if not env.get("CLORE_API_KEY"):
            raise ValueError("не задан API-ключ Clore.ai (вкладка «Настройки»)")
        if not env.get("API_KEY"):
            raise ValueError("не задан ключ вашего API (вкладка «Настройки»)")
        run_deploy(cfg)
        return {"ok": True}

    if path == "/api/deploy/stop":
        with JOB_LOCK:
            running = JOB["running"]
        if not running:
            raise ValueError("деплой не идёт")
        JOB_STOP.set()
        job_log("⏹ остановка по кнопке")
        # Гонка свои заказы отменяет сама (RACE_ORDERS). А в очереди текущий заказ надо
        # отменить отсюда: иначе остановленный деплой оставил бы его оплачиваться
        cur = deploy.load_env()
        oid = cur.get("ORDER")
        if oid and not cur.get("RACE_ORDERS"):
            err = deploy.cancel_order_safe(Clore(env.get("CLORE_API_KEY")), int(oid),
                                           "остановлен вручную", logf=job_log, attempts=3, pause=3)
            if err is not None:
                raise RuntimeError(f"деплой остановлен, но заказ {oid} отменить не удалось — "
                                   f"он оплачивается: {err}")
            deploy.save_env_keys({"ORDER": "", "SERVER": "", "API_URL": "", "SSH": ""})
            job_log(f"заказ {oid} отменён")
        return {"ok": True}

    if path == "/api/cancel":
        oid = body.get("order") or env.get("ORDER")
        if not oid:
            raise ValueError("не указан заказ")
        # Заказ мог погаснуть сам (Clore гасит его, когда контейнер завершился). Тогда
        # cancel_order отвечает ошибкой, и раньше на этом всё заканчивалось: карточка
        # «Текущий сервер» продолжала показывать мёртвый заказ, и убрать её было нечем.
        # Теперь «уже неактивен» считается тем же исходом, что и успешная отмена.
        err = deploy.cancel_order_safe(Clore(env.get("CLORE_API_KEY")), int(oid),
                                       body.get("issue"), logf=job_log, attempts=2, pause=3)
        if err is not None:
            raise RuntimeError(f"заказ {oid} отменить не удалось — он продолжает "
                               f"оплачиваться: {err}")
        # .env читаем заново: пока шла отмена, деплой мог записать туда новый заказ
        fresh = deploy.load_env()
        race = [x for x in (fresh.get("RACE_ORDERS") or "").split(",") if x.strip()]
        if str(oid) in race:
            deploy.save_env_keys({"RACE_ORDERS": ",".join(x for x in race if x != str(oid))})
        current = str(fresh.get("ORDER") or "")
        if str(oid) in (current, str(env.get("ORDER") or "")):
            with JOB_LOCK:
                running = JOB["running"]
            if running:
                # это заказ идущего деплоя — останавливаем его, иначе поток примет
                # отменённый сервер за неподнявшийся и арендует следующий из очереди
                JOB_STOP.set()
                job_log(f"заказ {oid} отменён вручную — останавливаю деплой")
            if str(oid) == current:
                deploy.save_env_keys({"ORDER": "", "SERVER": "", "API_URL": "", "SSH": ""})
        return {"ok": True, "cancelled": oid}

    if path == "/api/bench":
        url = body.get("api_url") or env.get("API_URL")
        if not url:
            raise ValueError("нет API_URL")
        speed = deploy.bench(url, env.get("API_KEY") or "", body.get("model") or deploy.MODEL_NAME)
        return {"ok": bool(speed), "tokens_per_sec": speed}

    if path == "/api/use":
        """Переключить .env и OpenCode на уже существующий заказ Clore."""
        oid = int(body["order"])
        clore = Clore(env.get("CLORE_API_KEY"))
        order = clore.order(oid)
        if not order:
            raise ValueError(f"заказ {oid} не найден")
        acc = order_access(order)
        if not (acc["host"] and acc["ports"].get(deploy.API_PORT)):
            raise ValueError("у заказа ещё нет проброшенного порта API")
        url = f"http://{acc['host']}:{acc['ports'][deploy.API_PORT]}/v1"
        ssh = f"ssh -p {acc['ports'].get(22, 22)} root@{acc['host']}"
        deploy.save_env_keys({"ORDER": str(oid), "SERVER": str(order.get("si")),
                              "API_URL": url, "SSH": ssh})
        deploy.update_opencode(url, env.get("API_KEY") or "")
        return {"ok": True, "api_url": url, "ssh": ssh}

    raise FileNotFoundError(path)


# ---------- HTTP ----------
CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml",
                 ".ico": "image/x-icon", ".json": "application/json; charset=utf-8"}


class Handler(BaseHTTPRequestHandler):
    server_version = "UncensoredAI-UI"

    def log_message(self, fmt, *args):  # тише в консоли
        if "/api/job" not in (self.path or ""):
            print(f"  {self.command} {self.path}", flush=True)

    def _send(self, code, payload=None, raw=None, ctype="application/json; charset=utf-8"):
        data = raw if raw is not None else json.dumps(payload, ensure_ascii=False).encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        except (ConnectionError, OSError):
            # клиент отвалился (браузер отменил запрос) — писать некуда, молчим
            self.close_connection = True

    def _static(self, path):
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        full = os.path.normpath(os.path.join(STATIC, rel))
        # sep обязателен: без него прошёл бы любой каталог-сосед с таким же префиксом
        if not full.startswith(STATIC + os.sep) or not os.path.isfile(full):
            return self._send(404, {"error": "not found"})
        with open(full, "rb") as f:
            data = f.read()
        ext = os.path.splitext(full)[1].lower()
        self._send(200, raw=data, ctype=CONTENT_TYPES.get(ext, "application/octet-stream"))

    def _foreign(self, post=False):
        """
        Причина, по которой запрос пришёл не от нашего UI, либо None.

        Сервер слушает локальный порт, но браузер пускает к нему любую открытую страницу:
        без этих проверок посторонний сайт мог запустить аренду, отменить заказ или
        переписать ключи в .env, а через DNS rebinding — ещё и прочитать лог деплоя.
          * Host — отсекает rebinding: чужой домен, указывающий на 127.0.0.1;
          * Origin / Sec-Fetch-Site — отсекают запросы со страниц других сайтов;
          * Content-Type — JSON с чужой страницы требует preflight, который мы не
            разрешаем; «простой» text/plain прошёл бы без него.
        """
        host = (self.headers.get("Host") or "").strip().lower()
        name = urllib.parse.urlsplit("//" + host).hostname or ""
        if not OPEN_BIND and name not in ALLOWED_HOSTS:
            return f"недопустимый Host: {host or 'пусто'}"
        if not post and not self.path.startswith("/api/"):
            # статика: переход по ссылке с другого сайта — обычное дело, а секретов в ней нет
            return None
        origin = (self.headers.get("Origin") or "").strip().lower()
        if origin and urllib.parse.urlsplit(origin).netloc != host:
            return "запрос с другого сайта"
        if (self.headers.get("Sec-Fetch-Site") or "").lower() in ("cross-site", "same-site"):
            return "запрос с другого сайта"
        if post:
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                return "ожидался Content-Type: application/json"
        return None

    def do_GET(self):
        why = self._foreign()
        if why:
            return self._send(403, {"error": why})
        parsed = urllib.parse.urlparse(self.path)
        if not parsed.path.startswith("/api/"):
            return self._static(parsed.path)
        try:
            self._send(200, api_get(parsed.path, urllib.parse.parse_qs(parsed.query)))
        except FileNotFoundError:
            self._send(404, {"error": "неизвестный эндпоинт"})
        except (CloreError, RuntimeError, ValueError) as e:
            self._send(400, {"error": str(e)})
        except ConnectionError:
            self.close_connection = True
        except Exception as e:  # noqa
            traceback.print_exc()
            self._send(500, {"error": f"{e.__class__.__name__}: {e}"})

    def do_POST(self):
        why = self._foreign(post=True)
        if why:
            return self._send(403, {"error": why})
        parsed = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length).decode() or "{}")
        except ValueError:
            return self._send(400, {"error": "ожидался JSON"})
        except ConnectionError:
            self.close_connection = True
            return
        try:
            self._send(200, api_post(parsed.path, body))
        except FileNotFoundError:
            self._send(404, {"error": "неизвестный эндпоинт"})
        except (CloreError, RuntimeError, ValueError, KeyError, SystemExit) as e:
            self._send(400, {"error": str(e)})
        except ConnectionError:
            self.close_connection = True
        except Exception as e:  # noqa
            traceback.print_exc()
            self._send(500, {"error": f"{e.__class__.__name__}: {e}"})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    global OPEN_BIND
    bind = args.host.strip("[]").lower()
    if bind in ("0.0.0.0", "::", ""):
        # имя, под которым придут из сети, заранее неизвестно — Host проверить нечем;
        # проверки Origin и Content-Type остаются
        OPEN_BIND = True
        print("⚠️ UI слушает все интерфейсы: он доступен из сети без пароля, "
              "а в нём ключи и аренда за деньги", flush=True)
    else:
        ALLOWED_HOSTS.add(bind)

    url = f"http://{args.host}:{args.port}/"
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"UncensoredAI UI: {url}  (Ctrl+C чтобы остановить)", flush=True)
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nостановлено")
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
