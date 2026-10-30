# -*- coding: utf-8 -*-
"""
stop.py — отменяет текущий заказ Clore из .env и сообщает в Telegram.

В Clore.ai нет «остановки с сохранением диска», как у некоторых других площадок:
отмена заказа освобождает сервер и удаляет контейнер вместе со скачанными весами.
Следующий деплой качает модель заново.

  python stop.py                       # отменить заказ из .env
  python stop.py --order 12345         # отменить конкретный заказ
  python stop.py --issue "GPU падает"  # приложить жалобу на хост
"""
import argparse
import sys
import time

from clore import CloreError, order_access
from deploy import client, load_env, log, save_env_keys, tg, wallet_balances

# Отмену повторяем долго и настойчиво: неотменённый заказ тикает до конца срока
# аренды, а это у Clore до 48 часов.
ATTEMPTS = 6
RETRY_PAUSE = 20


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--order", type=int, help="id заказа Clore (по умолчанию из .env)")
    ap.add_argument("--issue", help="описание проблемы для Clore (необязательно)")
    args = ap.parse_args()

    env = load_env()
    if not env.get("CLORE_API_KEY"):
        log("в .env нет CLORE_API_KEY")
        return 1
    oid = args.order or env.get("ORDER")
    if not oid:
        log("в .env нет ORDER и не передан --order")
        return 1

    clore = client(env)
    # Отмена — денежный путь: если она не прошла, сервер тикает до конца срока аренды
    # (у Clore это до 48 часов). Разовый сбой сети не должен останавливать попытки.
    order, spend, last = None, None, None
    for attempt in range(ATTEMPTS):
        try:
            order = clore.order(oid)
            if not order:
                log(f"заказ {oid} не найден среди активных (уже отменён?)")
                save_env_keys({"ORDER": "", "SERVER": "", "API_URL": "", "SSH": ""})
                return 1
            spend = order.get("spend")
            clore.cancel_order(oid, args.issue)
            break
        except CloreError as e:
            last = e
            if attempt < ATTEMPTS - 1:
                log(f"отмена не прошла ({str(e)[:70]}) — повтор {attempt + 1}/{ATTEMPTS - 1} "
                    f"через {RETRY_PAUSE} с")
                time.sleep(RETRY_PAUSE)
    else:
        e = last
        log(f"⛔ {e}")
        # Clore сам гасит заказ, когда контейнер завершился: отменять уже нечего,
        # но ссылки на мёртвый сервер надо убрать, иначе UI продолжит их показывать
        if "not_active" in str(getattr(e, "detail", "") or "") or e.code == 4:
            log("заказ уже неактивен — чищу ссылки на него в .env")
            save_env_keys({"ORDER": "", "SERVER": "", "API_URL": "", "SSH": ""})
        return 1

    host = order_access(order).get("host") or "?"
    log(f"заказ {oid} отменён (сервер {order.get('si')}, {host})")
    save_env_keys({"ORDER": "", "SERVER": "", "API_URL": "", "SSH": ""})

    # Заказ уже отменён. Справка о балансе — необязательный шаг: разовый сбой сети на нём
    # не должен выглядеть как неудачная отмена, иначе пользователь полезет отменять снова.
    try:
        rates = clore.currency_rates()
        balances = wallet_balances(clore)
        bal_usd = sum(amount * rates.get(cur, 0) for cur, amount in balances.items())
        rate = rates.get(order.get("currency") or "")
        spent = ""
        if isinstance(spend, (int, float)):
            spent = (f"\nПотрачено: ${spend * rate:.2f}" if rate
                     else f"\nПотрачено: {spend:.8f} {order.get('currency')}")
        tg(env, f"🗑 Clore: заказ {oid} отменён, сервер освобождён (веса удалены вместе "
                f"с контейнером).{spent}\nБаланс: ≈${bal_usd:.2f}")
    except CloreError as e:
        log(f"заказ отменён; баланс показать не удалось ({e})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
