# -*- coding: utf-8 -*-
"""
clore.py — минимальный клиент REST API Clore.ai (https://api.clore.ai/v1) на одной stdlib.

Авторизация: заголовок `auth: <API_KEY>` (не Bearer!). Ключ берётся в кабинете Clore:
https://clore.ai -> Settings -> API.

Лимиты Clore: 1 запрос/с на большинство эндпоинтов и 1 запрос/5 с на create_order —
клиент сам выдерживает паузы, чтобы не получить code 5.
"""
import json
import re
import threading
import time
import urllib.error
import urllib.request

API_BASE = "https://api.clore.ai/v1"
# валюты списания; в UI всё показывается в USD
CURRENCIES = ["USD-Blockchain", "bitcoin", "CLORE-Blockchain"]
RATES_TTL = 600            # как долго держать курсы валют в кэше, секунд
PRICE_PERIOD_HOURS = 24    # Clore публикует цены за сутки, а не за час

# частота вызовов: path -> минимальный интервал в секундах
RATE_LIMITS = {"create_order": 5.0}
DEFAULT_RATE = 1.1

_ERRORS = {
    1: "общая ошибка API",
    2: "неверный API-ключ (проверьте CLORE_API_KEY)",
    3: "недостаточно средств на балансе Clore",
    4: "сервер недоступен или уже арендован",
    5: "превышен лимит частоты запросов",
    6: "некорректное значение поля запроса",
}


# Clore присылает машинные пометки в поле field/error — переводим их на человеческий,
# иначе «некорректное значение поля запроса (not_enough_balance)» ничего не объясняет.
_DETAILS = {
    "not_enough_balance": "на балансе не хватает средств: Clore списывает оплату "
                          "за сутки вперёд, а не по часам",
    "server_rented": "сервер уже арендован кем-то другим",
    "invalid_image": "docker-образ не найден или недоступен",
}


class CloreError(RuntimeError):
    def __init__(self, code, detail=None):
        self.code = code
        self.detail = detail
        msg = _ERRORS.get(code, f"код ответа {code}")
        if detail:
            msg += f" ({_DETAILS.get(str(detail), detail)})"
        super().__init__(f"Clore API: {msg}")


class Clore:
    def __init__(self, api_key, base=API_BASE, timeout=30):
        if not api_key:
            raise CloreError(2, "ключ не задан")
        self.api_key = api_key.strip()
        self.base = base.rstrip("/")
        self.timeout = timeout
        self._lock = threading.Lock()
        self._last = {}
        self._rates = None  # (время, {валюта: курс в USD})

    # ---------- транспорт ----------
    def _throttle(self, path):
        gap = RATE_LIMITS.get(path, DEFAULT_RATE)
        with self._lock:
            wait = gap - (time.time() - self._last.get(path, 0))
            if wait > 0:
                time.sleep(wait)
            self._last[path] = time.time()

    def _call(self, path, body=None, query=None, retries=5):
        url = f"{self.base}/{path}"
        if query:
            url += "?" + "&".join(f"{k}={v}" for k, v in query.items())
        data = json.dumps(body).encode() if body is not None else None
        headers = {"auth": self.api_key, "User-Agent": "UncensoredAI/1.0"}
        if data:
            headers["Content-Type"] = "application/json"

        for attempt in range(retries):
            self._throttle(path)
            req = urllib.request.Request(url, data=data, headers=headers,
                                         method="POST" if data else "GET")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    out = json.loads(resp.read().decode())
            except urllib.error.HTTPError as e:
                payload = e.read().decode(errors="replace")[:300]
                if e.code in (429, 502, 503, 504) and attempt < retries - 1:
                    # 429 прилетает, когда к аккаунту обращаются сразу несколько деплоев;
                    # пауза по секунде тут не помогает — ждём ощутимо дольше
                    time.sleep((5 if e.code == 429 else 1) * (2 ** attempt))
                    continue
                raise CloreError(1, f"HTTP {e.code}: {payload}")
            except (urllib.error.URLError, TimeoutError) as e:
                if attempt < retries - 1:
                    time.sleep(2 ** attempt)
                    continue
                raise CloreError(1, f"сеть: {e}")

            code = out.get("code", 0)
            if code == 5 and attempt < retries - 1:
                time.sleep(RATE_LIMITS.get(path, DEFAULT_RATE) * (attempt + 2))
                continue
            if code:
                raise CloreError(code, out.get("error") or out.get("field"))
            return out
        raise CloreError(5)

    # ---------- эндпоинты ----------
    def wallets(self):
        return self._call("wallets").get("wallets", [])

    def marketplace(self):
        out = self._call("marketplace")
        return out.get("servers", [])

    def my_orders(self, completed=False):
        q = {"return_completed": "true"} if completed else None
        return self._call("my_orders", query=q).get("orders", [])

    def create_order(self, server_id, image, currency="bitcoin", order_type="on-demand",
                     ports=None, env=None, command=None, ssh_password=None, ssh_key=None,
                     spot_price=None, required_price=None, autossh_entrypoint=False):
        body = {
            "currency": currency,
            "image": image,
            "renting_server": int(server_id),
            "type": order_type,
        }
        if ports:
            body["ports"] = {str(k): v for k, v in ports.items()}
        if env:
            body["env"] = {str(k): str(v) for k, v in env.items()}
        if command:
            body["command"] = command
        if ssh_password:
            body["ssh_password"] = ssh_password
        if ssh_key:
            body["ssh_key"] = ssh_key
        if order_type == "spot":
            body["spotprice"] = float(spot_price)
        if required_price is not None:
            body["required_price"] = float(required_price)
        if autossh_entrypoint:
            body["autossh_entrypoint"] = True
        return self._call("create_order", body=body)

    def spot_marketplace(self, server_id=None):
        q = {"server_id": int(server_id)} if server_id else None
        return self._call("spot_marketplace", query=q)

    def currency_rates(self, servers=None):
        """
        Курсы валют Clore в USD ({'bitcoin': 98000.0, 'CLORE-Blockchain': 0.03}).
        Источник — поле currency_rates_in_usd эндпоинта spot_marketplace; если он недоступен,
        курс выводится из серверов, у которых Clore сам отдал цену и в USD, и в монете.
        """
        now = time.time()
        if self._rates and now - self._rates[0] < RATES_TTL:
            return self._rates[1]
        rates = {}
        try:
            data = self.spot_marketplace()
            raw = (data.get("market") or data).get("currency_rates_in_usd") or {}
            rates = {k: float(v) for k, v in raw.items() if is_price(v)}
        except (CloreError, AttributeError, TypeError, ValueError):
            rates = {}
        if not rates:
            rates = _rates_from_servers(servers if servers is not None else self._safe_marketplace())
        self._rates = (now, rates)
        return rates

    def _safe_marketplace(self):
        try:
            return self.marketplace()
        except CloreError:
            return []

    def cancel_order(self, order_id, issue=None):
        body = {"id": int(order_id)}
        if issue:
            body["issue"] = issue[:2048]
        return self._call("cancel_order", body=body)

    def order(self, order_id):
        oid = int(order_id)
        for o in self.my_orders():
            if int(o.get("id", -1)) == oid:
                return o
        return None

    def balance(self, currency="bitcoin"):
        for w in self.wallets():
            if (w.get("name") or "").lower() == currency.lower():
                return float(w.get("balance") or 0)
        return None


# ---------- разбор данных маркетплейса ----------
_GPU_COUNT = re.compile(r"^\s*(\d+)\s*[xX]\s*(.+)$")


def gpu_info(server):
    """('4x NVIDIA GeForce RTX 4090', 24) -> (4, 'NVIDIA GeForce RTX 4090', 96.0)"""
    specs = server.get("specs") or {}
    raw = str(specs.get("gpu") or "")
    m = _GPU_COUNT.match(raw)
    count, name = (int(m.group(1)), m.group(2).strip()) if m else (1, raw.strip())
    per = float(specs.get("gpuram") or 0)
    if per > 1024:        # некоторые хосты отдают МБ
        per /= 1024.0
    return count, name, round(count * per, 1)


def gpu_models(server):
    """
    Реальные модели карт. У разнородных хостов specs.gpu говорит просто «Mixed GPUs»,
    и по нему архитектуру не определить — настоящие названия лежат в gpu_array.
    """
    arr = [str(g).strip() for g in (server.get("gpu_array") or []) if str(g).strip()]
    if arr:
        return arr
    count, name, _ = gpu_info(server)
    return [name] * max(count, 1)


def price_of(server, currency="bitcoin", order_type="on-demand"):
    p = (server.get("price") or {}).get("on_demand" if order_type == "on-demand" else "spot") or {}
    for k, v in p.items():
        if k.lower() == currency.lower():
            try:
                return float(v)
            except (TypeError, ValueError):
                return None
    return None


def is_price(v):
    """Цена пришла числом и она положительная (строки вида "4.2e-06" тоже считаются)."""
    try:
        return float(v) > 0
    except (TypeError, ValueError):
        return False


def _usd_branch(server, order_type):
    """Цена за сутки в USD, если Clore посчитал её сам (price.usd.on_demand_usd / price.usd.spot)."""
    usd = (server.get("price") or {}).get("usd") or {}
    keys = (("on_demand_usd", "on_demand") if order_type == "on-demand" else ("spot_usd", "spot"))
    for k in keys:
        for key, val in usd.items():
            if key.lower() == k and is_price(val):
                return float(val)
    return None


def _original_usd(server, currency, order_type):
    """
    Цена за сутки в USD, которую хост выставил именно для этой валюты
    (price.original_usd[<валюта>][on_demand|spot]) — у Clore она может отличаться по валютам.
    """
    per_cur = ((server.get("price") or {}).get("original_usd") or {}).get(currency) or {}
    val = per_cur.get("on_demand" if order_type == "on-demand" else "spot")
    return float(val) if is_price(val) else None


def _rates_from_servers(servers, min_samples=3):
    """Запасной способ: курс = USD-цена / цена в монете по тем серверам, где Clore дал обе."""
    buckets = {}
    for s in servers or []:
        usd = _usd_branch(s, "on-demand")
        if not usd:
            continue
        for cur, coin in (((s.get("price") or {}).get("on_demand")) or {}).items():
            if is_price(coin):
                buckets.setdefault(cur, []).append(usd / float(coin))
    rates = {}
    for cur, vals in buckets.items():
        if len(vals) >= min_samples:
            vals.sort()
            rates[cur] = vals[len(vals) // 2]  # медиана устойчивее среднего к выбросам
    return rates


def price_usd(server, order_type="on-demand", rates=None):
    """
    Самая выгодная цена сервера в USD за ЧАС и валюта, в которой она достигается.
    Возвращает (usd_в_час, валюта, сумма_в_валюте_за_сутки) — Clore принимает цены за сутки.
    """
    coins = (server.get("price") or {}).get("on_demand" if order_type == "on-demand" else "spot") or {}
    best = None
    for cur, coin in coins.items():
        if not is_price(coin):
            continue
        rate = (rates or {}).get(cur)
        usd_day = _original_usd(server, cur, order_type)
        if usd_day is None and is_price(rate):
            usd_day = float(coin) * float(rate)
        if usd_day is None:
            continue
        if best is None or usd_day < best[0]:
            best = (usd_day, cur, float(coin))
    if best is None:
        usd_day = _usd_branch(server, order_type)
        if not usd_day:
            return None, None, None
        cur, coin = next(((c, float(v)) for c, v in coins.items() if is_price(v)), (None, None))
        best = (usd_day, cur, coin)
    return best[0] / PRICE_PERIOD_HOURS, best[1], best[2]


def net_down(server):
    return float(((server.get("specs") or {}).get("net") or {}).get("down") or 0)


# История замеров скорости: "S|<время>|<вниз>|<вверх>". Clore показывает только последний
# замер, а он бывает выбросом: у хоста с историей 92 / 91 / 334 Mbps в карточке стоит 334,
# и реальная загрузка весов шла на 20 Mbps. Поэтому для расчётов берём медиану всех
# замеров: она гасит и разовый удачный всплеск, и единственный провал годовой давности
# (строгий минимум забраковал бы 803 сервера из 2568, что уже перебор).
def net_history(server):
    out = []
    for row in (((server.get("specs") or {}).get("net") or {}).get("test_history") or []):
        parts = str(row).split("|")
        if len(parts) >= 3:
            try:
                out.append(float(parts[2]))
            except ValueError:
                pass
    return out


def net_down_est(server):
    """На какую скорость реально рассчитывать: медиана замеров, включая текущий."""
    # текущее значение в историю уже входит — добавлять его ещё раз нельзя: у хоста
    # с замерами 92 / 91 / 335 дубль последнего сдвигал медиану с 92 на 213
    vals = sorted(v for v in (net_history(server) or [net_down(server)]) if v > 0)
    if not vals:
        return 0.0
    mid = len(vals) // 2
    return vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2


# Сколько каналов памяти у процессора. Для выгрузки экспертов MoE в оперативку это
# главный параметр сервера: скорость генерации упирается в пропускную способность RAM,
# а не в карту. Clore каналы не сообщает, но модель процессора известна, а у семейств
# она фиксированная: EPYC и Xeon Scalable — 8, Xeon E5 — 4, десктопные — 2.
_RAM_CHANNELS = (
    (re.compile(r"epyc|xeon.*(platinum|gold|silver|bronze)|threadripper", re.I), 8),
    (re.compile(r"xeon", re.I), 4),
)


def ram_channels(server):
    cpu = str((server.get("specs") or {}).get("cpu") or "")
    for rx, n in _RAM_CHANNELS:
        if rx.search(cpu):
            return n
    return 2


def server_brief(server, rates=None):
    """Карточка сервера. Цены — в USD; валюта списания с баланса идёт отдельным полем."""
    count, name, vram = gpu_info(server)
    specs = server.get("specs") or {}
    usd, cur, coin = price_usd(server, "on-demand", rates)
    spot_usd, spot_cur, spot_coin = price_usd(server, "spot", rates)
    return {
        "id": server.get("id"),
        "gpu": name,
        "gpu_models": gpu_models(server),   # настоящие карты, важно для «Mixed GPUs»
        "gpu_count": count,
        "vram_gb": vram,
        "ram_gb": round(float(specs.get("ram") or 0), 1),
        "disk": specs.get("disk"),
        "disk_speed": specs.get("disk_speed"),
        "cpu": specs.get("cpu"),
        "ram_channels": ram_channels(server),      # важно только при выгрузке в RAM
        "net_down": net_down(server),             # как показывает Clore (последний замер)
        "net_down_est": net_down_est(server),      # на что рассчитывать: медиана замеров
        "net_tests": len(net_history(server)),
        "net_up": float(((specs.get("net") or {}).get("up") or 0)),
        "price_usd": round(usd, 4) if usd else None,            # $/час
        "spot_price_usd": round(spot_usd, 4) if spot_usd else None,
        "pay_currency": cur,
        "pay_amount": coin,                                      # за сутки — так их принимает Clore
        "spot_pay_currency": spot_cur,
        "spot_pay_amount": spot_coin,
        "mrl_hours": server.get("mrl"),
        "rented": bool(server.get("rented")),
        # надёжность хоста по версии Clore: доля удачных аренд. У части хостов она низкая,
        # и именно на таких контейнер поднимается криво (например, без sshd)
        "reliability": float(server.get("reliability") or 0),
        "rating": float((server.get("rating") or {}).get("avg") or 0),
        "rating_count": int((server.get("rating") or {}).get("cnt") or 0),
    }


# Clore отдаёт порты как "<внутренний>:<внешний>", например "5000:2765".
# Поддерживаем и вид с адресом впереди/стрелкой: "1.2.3.4:2765->5000".
_PORT_ARROW = re.compile(r"^(?:(?P<host>[\w.\-]+):)?(?P<ext>\d+)\s*->\s*(?P<int>\d+)$")
_PORT_PAIR = re.compile(r"^(?P<int>\d+):(?P<ext>\d+)$")
_PORT_ONE = re.compile(r"^(?P<ext>\d+)$")


def order_access(order):
    """Достаёт из заказа хост и карту внутренний_порт -> внешний_порт."""
    hosts = [h for h in (order.get("pub_cluster") or []) if h]
    host = hosts[0] if hosts else None
    mapping = {}
    for entry in order.get("tcp_ports") or []:
        raw = str(entry).strip()
        m = _PORT_ARROW.match(raw) or _PORT_PAIR.match(raw) or _PORT_ONE.match(raw)
        if not m:
            continue
        groups = m.groupdict()
        if groups.get("host"):
            host = host or groups["host"]
        ext = int(groups["ext"])
        inner = int(groups["int"]) if groups.get("int") else ext
        mapping[inner] = ext
    http = order.get("http_port")
    http_url = None
    if http:
        http = str(http)
        http_url = http if http.startswith("http") else (f"http://{host}:{http}" if host else None)
    return {"host": host, "ports": mapping, "http_url": http_url}
