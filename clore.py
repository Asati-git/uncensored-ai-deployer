# -*- coding: utf-8 -*-
"""
clore.py — минимальный клиент REST API Clore.ai (https://api.clore.ai/v1) на одной stdlib.

Авторизация: заголовок `auth: <API_KEY>` (не Bearer!). Ключ берётся в кабинете Clore:
https://clore.ai -> Settings -> API.

Лимиты Clore: 1 запрос/с на большинство эндпоинтов и 1 запрос/5 с на create_order —
клиент сам выдерживает паузы, чтобы не получить code 5.
"""
import http.client
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request

# CLORE_API_BASE — только для тестов: локальная заглушка вместо настоящего Clore
API_BASE = os.environ.get("CLORE_API_BASE") or "https://api.clore.ai/v1"
# валюты списания; в UI всё показывается в USD
CURRENCIES = ["USD-Blockchain", "bitcoin", "CLORE-Blockchain"]
RATES_TTL = 600            # как долго держать курсы валют в кэше, секунд
PRICE_PERIOD_HOURS = 24    # Clore публикует цены за сутки, а не за час

# частота вызовов: path -> минимальный интервал в секундах
# Лимит Clore — 1 запрос в секунду НА ТОКЕН, по всем эндпоинтам вместе, и create_order
# не чаще раза в 5 секунд (docs.clore.ai/guidlines/api). Раньше пауза считалась отдельно
# для каждого эндпоинта: marketplace, wallets и my_orders уходили в одну и ту же секунду,
# Clore отвечал HTTP 429, и деплой пропускал все серверы очереди. Теперь между ЛЮБЫМИ
# двумя запросами к Clore — не меньше GLOBAL_GAP, с запасом против лимита. Пауза общая
# и для разных процессов (UI, stop.py, второе окно UI): время последнего запроса лежит
# в файле во временном каталоге.
GLOBAL_GAP = 1.6
RATE_LIMITS = {"create_order": 6.0}
DEFAULT_RATE = GLOBAL_GAP
# Запросы, которые нельзя повторять вслепую. Если create_order дошёл до Clore, а ответ
# потерялся, повтор получит «сервер уже арендован» — нами же: деплой уйдёт к следующему
# серверу, а первый заказ останется тарифицироваться без единой ссылки на него.
NON_IDEMPOTENT = {"create_order"}

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
    def __init__(self, code, detail=None, uncertain=False, not_sent=False, source="Clore API"):
        self.code = code
        # True — запрос точно не выполнен (пауза после сбоя, лимит частоты): его можно
        # повторить позже, и сервер из-за этого пропускать не нужно
        self.not_sent = not_sent or code == 5
        self.detail = detail
        # True — неизвестно, выполнился ли запрос: ответ не дошёл. Для create_order это
        # значит, что заказ мог создаться, и вызывающий обязан проверить это сам.
        self.uncertain = uncertain
        msg = _ERRORS.get(code, f"код ответа {code}")
        if detail:
            msg += f" ({_DETAILS.get(str(detail), detail)})"
        super().__init__(f"{source}: {msg}")


# Состояние, общее для всех клиентов с одним ключом. UI создаёт новый Clore на каждый
# HTTP-запрос, и раньше у каждого были свои паузы и свой кэш курсов: параллельные
# запросы страницы (кошельки, заказы, подбор) били в Clore одновременно, ловили
# code 5 и уходили в повторы — «Найти» висело по минуте без единого слова.
_SHARED = {}
_SHARED_LOCK = threading.Lock()

# Clore стоит за Cloudflare. Если с IP идёт слишком много запросов, Cloudflare блокирует
# его целиком — вместе с сайтом — на минуты: соединение рвётся или висит до таймаута,
# либо приходит HTTP 403/429. Раньше клиент отвечал на это пятью повторами каждого
# запроса, а деплой и UI делали такие запросы по нескольку подряд — блокировка только
# продлевалась, а кнопки висели минутами. Теперь после такого сбоя Clore считается
# недоступным BLOCK_COOLDOWN секунд, и все запросы (кроме отмены заказа) сразу отвечают
# понятной ошибкой, не трогая сеть.
BLOCK_COOLDOWN = 90
# Маркетплейс весит несколько мегабайт. Его спрашивают и подбор серверов, и вкладки
# «Заказы»/«Кошельки», и деплой — кэш убирает повторные скачивания подряд. UI держит
# его тёплым (app.py подгружает список при запуске и раз в пару минут, пока UI открыт),
# поэтому «Найти серверы» отвечает сразу, а не через 15–20 с скачивания. Две минуты —
# не страшно и для деплоя: сервер, который успели занять, create_order просто не отдаст,
# и деплой возьмёт следующий из очереди.
MARKET_TTL = 120
# Пути, ожидание которых можно оборвать кнопкой «Стоп»: это чтение, повторить его потом
# безопасно. my_orders и cancel_order сюда не входят — на них держатся поиск только что
# созданного заказа и его отмена, обрывать их нельзя.
INTERRUPTIBLE = {"marketplace", "wallets", "spot_marketplace"}
# Отмену заказа пробуем всегда, даже когда Clore считается недоступным: это деньги
ALWAYS_TRY = {"cancel_order"}


class _SlotFile:
    """
    Время последних запросов к Clore, общее для всех процессов на этом ПК с этим ключом.
    Файл блокируется на время «дождаться очереди и записать время», так что два окна UI
    или UI и stop.py не отправят запросы в одну секунду. Если файл недоступен (нет прав,
    странная ФС) — работаем по памяти процесса, как раньше: это хуже, но не ломает.
    """
    _mem = {}

    def __init__(self, api_key):
        import hashlib
        import tempfile
        tag = hashlib.sha256(api_key.encode()).hexdigest()[:16]
        self.path = os.path.join(tempfile.gettempdir(), f"uncensoredai-clore-{tag}.json")
        self.key = tag
        self.f = None

    def __enter__(self):
        try:
            # не "a": в режиме дозаписи seek(0) не помогает, всё уходит в конец файла
            self.f = os.fdopen(os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600), "r+b")
            t0 = time.time()
            while True:
                try:
                    if os.name == "nt":
                        import msvcrt
                        self.f.seek(0)
                        msvcrt.locking(self.f.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(self.f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.time() - t0 > 30:   # чужой процесс завис с файлом — не ждём вечно
                        raise
                    time.sleep(0.05)
        except OSError:
            if self.f:
                self.f.close()
            self.f = None
        return self

    def read(self):
        if not self.f:
            return dict(self._mem.get(self.key) or {})
        try:
            self.f.seek(0)
            data = json.loads(self.f.read().decode() or "{}")
            return {k: float(v) for k, v in data.items()}
        except (ValueError, OSError, AttributeError):
            return {}

    def write(self, data):
        self._mem[self.key] = dict(data)
        if not self.f:
            return
        try:
            # Блокировка msvcrt стоит на первом байте — файл не обрезаем, а переписываем
            # содержимое с начала и добиваем пробелами: JSON их допускает
            body = json.dumps(data).encode()
            self.f.seek(0, os.SEEK_END)
            size = self.f.tell()
            self.f.seek(0)
            self.f.write(body + b" " * max(0, size - len(body)))
            self.f.flush()
        except OSError:
            pass

    def __exit__(self, *exc):
        if self.f:
            try:
                if os.name == "nt":
                    import msvcrt
                    self.f.seek(0)
                    msvcrt.locking(self.f.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.f.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            self.f.close()
        return False


class Stopped(Exception):
    """Ожидание ответа Clore прервано снаружи (кнопка «Стоп»)."""


def _shared(api_key):
    with _SHARED_LOCK:
        return _SHARED.setdefault(api_key, {
            "lock": threading.Lock(), "last": {}, "rates": None,
            "market": None, "market_lock": threading.Lock(),
            "down_until": 0.0, "down_why": ""})


def blocked_for(api_key):
    """Сколько секунд ещё считаем Clore недоступным (0 — доступен)."""
    st = _shared((api_key or "").strip())
    return max(0, int(st["down_until"] - time.time()))


class Clore:
    def __init__(self, api_key, base=API_BASE, timeout=20, stop=None, stop_exc=Stopped):
        if not api_key:
            raise CloreError(2, "ключ не задан")
        self.api_key = api_key.strip()
        self.base = base.rstrip("/")
        self.timeout = timeout
        # stop — функция без аргументов; истина означает «пользователь нажал Стоп».
        # Тогда ожидание на путях из INTERRUPTIBLE обрывается исключением stop_exc
        self.stop = stop
        self.stop_exc = stop_exc
        self._state = _shared(self.api_key)
        self._lock = self._state["lock"]
        self._last = self._state["last"]

    @property
    def _rates(self):  # (время, {валюта: курс в USD}) — общий для всех экземпляров
        return self._state["rates"]

    @_rates.setter
    def _rates(self, value):
        self._state["rates"] = value

    # ---------- транспорт ----------
    def _check_stop(self, path):
        if self.stop and path in INTERRUPTIBLE and self.stop():
            raise self.stop_exc("деплой остановлен")

    def _sleep(self, sec, path):
        """Пауза между повторами, которую обрывает «Стоп» — на безопасных для этого путях."""
        end = time.time() + sec
        while True:
            self._check_stop(path)
            left = end - time.time()
            if left <= 0:
                return
            time.sleep(min(left, 0.25))

    def _fetch(self, req, path):
        """
        Сам HTTP-запрос. Для чтения, которое можно бросить (INTERRUPTIBLE), он идёт в
        отдельном потоке, а мы ждём его, поглядывая на «Стоп»: при блокировке IP
        соединение висит до таймаута, и кнопка иначе ждала бы его целиком.
        """
        def go():
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode())
        if not (self.stop and path in INTERRUPTIBLE):
            return go()
        box = {}

        def worker():
            try:
                box["out"] = go()
            except BaseException as e:  # noqa — передаём в вызывающий поток как есть
                box["err"] = e
        th = threading.Thread(target=worker, daemon=True)
        th.start()
        while th.is_alive():
            th.join(0.25)
            self._check_stop(path)
        if "err" in box:
            raise box["err"]
        return box["out"]

    def _throttle(self, path):
        """Ждёт своей очереди: не чаще GLOBAL_GAP на любой запрос и RATE_LIMITS на путь."""
        with self._lock, _SlotFile(self.api_key) as slot:
            last = slot.read()
            now = time.time()
            wait = max(GLOBAL_GAP - (now - last.get("*", 0)),
                       RATE_LIMITS.get(path, 0) - (now - last.get(path, 0)), 0)
            if wait > 0:
                time.sleep(wait)
            now = time.time()
            last["*"] = now
            if path in RATE_LIMITS:
                last[path] = now
            slot.write(last)

    def _mark_down(self, why):
        self._state["down_until"] = time.time() + BLOCK_COOLDOWN
        self._state["down_why"] = why

    def _down_error(self):
        left = int(self._state["down_until"] - time.time())
        return CloreError(1, f"Clore.ai не отвечает ({self._state['down_why']}). Похоже на "
                             f"временную блокировку IP за частые запросы — не нажимайте кнопки "
                             f"повторно, следующая попытка не раньше чем через {max(left, 1)} с",
                          not_sent=True)

    def _call(self, path, body=None, query=None, retries=4):
        url = f"{self.base}/{path}"
        if query:
            url += "?" + "&".join(f"{k}={v}" for k, v in query.items())
        data = json.dumps(body).encode() if body is not None else None
        headers = {"auth": self.api_key, "User-Agent": "UncensoredAI/1.0"}
        if data:
            headers["Content-Type"] = "application/json"

        safe = path not in NON_IDEMPOTENT
        net_fails = 0
        for attempt in range(retries):
            if path not in ALWAYS_TRY and time.time() < self._state["down_until"]:
                raise self._down_error()
            self._check_stop(path)
            self._throttle(path)
            req = urllib.request.Request(url, data=data, headers=headers,
                                         method="POST" if data else "GET")
            try:
                out = self._fetch(req, path)
            except urllib.error.HTTPError as e:
                payload = e.read().decode(errors="replace")[:300]
                # любой 5xx не говорит, выполнился ли запрос на стороне Clore
                gateway = e.code >= 500
                if e.code == 429:
                    # Лимит частоты: запрос отвергнут и не выполнен, повторять можно даже
                    # create_order. Ждём, сколько просит Clore (Retry-After), иначе с запасом.
                    # Это не блокировка IP — общую паузу не включаем
                    if attempt < retries - 1:
                        try:
                            pause = float(e.headers.get("Retry-After") or 0)
                        except (TypeError, ValueError):
                            pause = 0
                        self._sleep(max(pause, 3 * (attempt + 1)), path)
                        continue
                    raise CloreError(5, "HTTP 429 — Clore просит реже; попробуйте через минуту")
                if e.code == 403:
                    # 403 от Cloudflare перед Clore — это уже блокировка: повтор её продлевает
                    self._mark_down("HTTP 403")
                    if path in ALWAYS_TRY and attempt < retries - 1:
                        self._sleep(5 * (attempt + 1), path)
                        continue
                    raise self._down_error()
                # Ошибка шлюза не говорит, дошёл ли запрос до бэкенда, поэтому create_order
                # на ней не повторяем
                if e.code in (502, 503, 504) and safe and attempt < retries - 1:
                    self._sleep(2 ** attempt, path)
                    continue
                raise CloreError(1, f"HTTP {e.code}: {payload}", uncertain=gateway and not safe)
            except (OSError, http.client.HTTPException, ValueError) as e:
                # Сюда попадает всё, что случается уже ПОСЛЕ отправки запроса: сброс
                # соединения на чтении ответа, оборванное тело, HTML вместо JSON. Раньше
                # такие ошибки летели мимо CloreError — и деплой падал, не записав номер
                # только что созданного заказа, а отмена обрывалась на полпути.
                net_fails += 1
                # Разовый сбой повторяем. Два подряд — уже не случайность, а блокировка
                # или лёг сам Clore: дальше не долбим и выдерживаем паузу для всех.
                # Отмену заказа пробуем до конца — от неё зависят деньги
                if safe and attempt < retries - 1 and (net_fails < 2 or path in ALWAYS_TRY):
                    self._sleep(2 * net_fails, path)
                    continue
                if not safe:
                    # create_order: один сбой не повод глушить остальные запросы — сразу
                    # после него деплой ищет, не создался ли заказ, через my_orders
                    raise CloreError(1, f"сеть: {e.__class__.__name__}: {e}", uncertain=True)
                self._mark_down(f"{e.__class__.__name__}: {e}"[:120])
                raise self._down_error()
            if not isinstance(out, dict):
                if safe and attempt < retries - 1:
                    self._sleep(2 ** attempt, path)
                    continue
                raise CloreError(1, "ответ Clore не разобрать", uncertain=not safe)

            # ответ пришёл — значит, Clore доступен
            self._state["down_until"] = 0.0
            code = out.get("code", 0)
            if code == 5 and attempt < retries - 1:
                # тот же лимит, сказанный в теле ответа: запрос не выполнен, повтор безопасен
                self._sleep(max(RATE_LIMITS.get(path, 0), GLOBAL_GAP) * (attempt + 2), path)
                continue
            if code:
                raise CloreError(code, out.get("error") or out.get("field"))
            return out
        raise CloreError(5)

    # ---------- эндпоинты ----------
    def wallets(self):
        return self._call("wallets").get("wallets", [])

    def marketplace(self, fresh=False):
        """
        Список серверов. Кэшируется на MARKET_TTL секунд для всех клиентов с этим ключом:
        параллельные запросы UI ждут одну загрузку, а не качают мегабайты каждый.
        fresh=True — обязательно свежий.
        """
        st = self._state
        with st["market_lock"]:
            cached = st["market"]
            if not fresh and cached and time.time() - cached[0] < MARKET_TTL:
                return list(cached[1])
            if fresh == "if-old" and cached and time.time() - cached[0] < MARKET_TTL - 30:
                return list(cached[1])   # подогрев: другой поток уже успел обновить
            servers = self._call("marketplace").get("servers", [])
            st["market"] = (time.time(), servers)
            return list(servers)

    def market_age(self):
        """Сколько секунд назад скачан маркетплейс (None — ещё ни разу)."""
        cached = self._state["market"]
        return time.time() - cached[0] if cached else None

    def my_orders(self, completed=False):
        q = {"return_completed": "true"} if completed else None
        return self._call("my_orders", query=q).get("orders", [])

    def create_order(self, server_id, image, currency="bitcoin", order_type="on-demand",
                     ports=None, env=None, command=None, ssh_password=None, ssh_key=None,
                     spot_price=None, required_price=None, autossh_entrypoint=False,
                     disk_gb=None):   # disk_gb: Clore диск не заказывает, параметр для других площадок
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
        Выводятся из маркетплейса: у многих серверов Clore сам отдаёт цену и в USD, и в
        монете. spot_marketplace без server_id отвечает пустым market, а раньше его
        спрашивали при каждом нажатии — теперь он только запасной источник.
        Ошибка сети пробрасывается: нулевые курсы показали бы нулевой баланс.
        """
        now = time.time()
        if self._rates and now - self._rates[0] < RATES_TTL:
            return self._rates[1]
        rates = _rates_from_servers(servers if servers is not None else self.marketplace())
        if not rates:
            try:
                data = self.spot_marketplace()
                raw = (data.get("market") or data).get("currency_rates_in_usd") or {}
                rates = {k: float(v) for k, v in raw.items() if is_price(v)}
            except (AttributeError, TypeError, ValueError):
                rates = {}
        if rates:   # пустое не кэшируем: иначе баланс десять минут показывался бы нулём
            self._rates = (now, rates)
        return rates

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
        # пропускная способность памяти карты, ГБ/с, если площадка её сообщает (Clore — нет)
        "mem_bw": float(specs.get("gpu_mem_bw") or 0) or None,
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
