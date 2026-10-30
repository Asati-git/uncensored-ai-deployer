#!/bin/bash
# usage: bash status.sh  -> состояние заказа Clore: сервер, адрес, порты, живость API
cd "$(dirname "$0")"; export PYTHONUTF8=1
python - <<'PY'
import json
from clore import PRICE_PERIOD_HOURS, Clore, order_access, server_brief
from deploy import API_PORT, api_alive, load_env

env = load_env()
if not env.get("CLORE_API_KEY"):
    raise SystemExit("в .env нет CLORE_API_KEY (задайте через ui.bat)")
oid = env.get("ORDER")
clore = Clore(env["CLORE_API_KEY"])
orders = clore.my_orders()
if oid:
    orders = [o for o in orders if str(o.get("id")) == str(oid)] or orders
if not orders:
    raise SystemExit("активных заказов на Clore нет")

servers = {int(s.get("id", -1)): s for s in clore.marketplace()}
rates = clore.currency_rates(list(servers.values()))
for o in orders:
    acc = order_access(o)
    sid = int(o.get("si", -1))
    b = server_brief(servers[sid], rates) if sid in servers else {}
    api = f"http://{acc['host']}:{acc['ports'][API_PORT]}/v1" if acc["host"] and acc["ports"].get(API_PORT) else None
    rate = rates.get(o.get("currency") or "")
    usd = lambda v: f"${float(v) * rate:.3f}" if rate and isinstance(v, (int, float)) else f"{v} {o.get('currency')}"
    # price у заказа Clore — за СУТКИ; раньше эта же цифра печаталась как «/ч» и завышала в 24 раза
    hourly = lambda v: (f"${float(v) * rate / PRICE_PERIOD_HOURS:.3f}"
                        if rate and isinstance(v, (int, float)) else f"{v} {o.get('currency')}")
    print(f"заказ {o.get('id')} | сервер {sid} | {hourly(o.get('price'))}/ч "
          f"({usd(o.get('price'))}/сутки) | потрачено {usd(o.get('spend'))}")
    print(f"  gpu    {b.get('gpu_count')}x {b.get('gpu')} ({b.get('vram_gb')} GiB VRAM)")
    print(f"  хост   {acc['host']} | порты {acc['ports']}")
    print(f"  api    {api or 'порт ещё не проброшен'}")
    if api:
        print(f"  модель {'онлайн' if api_alive(api, env.get('API_KEY') or '') else 'ещё поднимается'}")
    print(f"  ssh    ssh -p {acc['ports'].get(22, 22)} root@{acc['host']}")
PY
