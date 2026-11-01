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
}

JOB = {"running": False, "log": [], "result": None, "error": None, "started": None, "title": None}
JOB_LOCK = threading.Lock()


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

    def worker():
        try:
            res = deploy.deploy(cfg, logf=job_log)
            with JOB_LOCK:
                JOB["result"] = res
            job_log("задача завершена: " + str(res.get("status")))
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
        return {"model": hfmodels.model_info(ref, ctx=int(ctx) if ctx else None, token=token)}

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
        if updates:
            deploy.save_env_keys(updates)
        return {"ok": True, "saved": sorted(updates)}

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
            "server_id": int(body["server_id"]) if body.get("server_id") else None,
            "gpu_filter": (body.get("gpu_filter") or "").strip() or None,
            "image": (body.get("image") or "").strip() or None,
            "gguf_file": (body.get("gguf_file") or "").strip() or None,
            # эксперты MoE в оперативке: дешевле сервер ценой части скорости
            "cpu_moe": bool(body.get("cpu_moe")),
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

    if path == "/api/cancel":
        oid = body.get("order") or env.get("ORDER")
        if not oid:
            raise ValueError("не указан заказ")
        Clore(env.get("CLORE_API_KEY")).cancel_order(int(oid), body.get("issue"))
        if str(oid) == str(env.get("ORDER")):
            deploy.save_env_keys({"ORDER": "", "API_URL": "", "SSH": ""})
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

    def do_GET(self):
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
