import asyncio
import aiohttp
import sqlite3
from config import DB_NAME, TON_API_BASE_URL, TON_API_KEY, TON_DEPOSIT_ADDRESS, logger
from db import set_ton_rate, update_balance, add_transaction, get_ton_rate, get_user

# Исправленный эндпоинт для Toncenter v2 API
ALT_TON_API_URL = "https://toncenter.com/api/v2/getTransactions"

async def get_ton_rate_from_api():
    try:
        url = "https://api.coingecko.com/api/v3/simple/price?ids=the-open-network&vs_currencies=uzs"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=10) as response:
                if response.status == 200:
                    data = await response.json()
                    rate = data.get('the-open-network', {}).get('uzs', 0)
                    if rate > 0:
                        set_ton_rate(rate)
                        return rate
    except Exception as e:
        logger.error(f"Ошибка при получении курса TON из API: {e}")
    return get_ton_rate()

async def monitor_deposits():
    if not TON_DEPOSIT_ADDRESS:
        logger.error("❌ TON_DEPOSIT_ADDRESS не настроен.")
        return

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json"
    }

    params = {
        "address": TON_DEPOSIT_ADDRESS,
        "limit": 10,
        "to_lt": 0,
        "archival": "false"
    }
    # Toncenter принимает API-ключ как query-параметр api_key, а НЕ как заголовок —
    # раньше ключ передавался неправильно (X-API-Key), из-за чего запросы либо
    # игнорировали ключ (и упирались в общий rate-limit анонимных запросов),
    # либо Toncenter отвечал 500/429.
    if TON_API_KEY:
        params["api_key"] = TON_API_KEY

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(ALT_TON_API_URL, headers=headers, params=params, timeout=12) as response:
                if response.status != 200:
                    # Не спамим логи одинаковым предупреждением каждые 30 секунд —
                    # выводим только раз в ~10 минут (20 неудачных попыток подряд при интервале 30с).
                    monitor_deposits._fail_count = getattr(monitor_deposits, "_fail_count", 0) + 1
                    if monitor_deposits._fail_count % 20 == 1:
                        logger.warning(
                            f"⚠️ API TON вернуло статус {response.status} "
                            f"(повторяется, попытка №{monitor_deposits._fail_count})"
                        )
                    return
                monitor_deposits._fail_count = 0

                try:
                    data = await response.json(content_type=None)
                except Exception:
                    return

                if not data.get("ok", False):
                    return

                transactions = data.get('result', [])
                if not transactions:
                    return

                ton_rate = get_ton_rate()

                for tx in transactions:
                    tx_hash = tx.get('transaction_id', {}).get('hash', '')
                    if not tx_hash:
                        continue

                    # Проверка дубликатов в БД
                    conn = sqlite3.connect(DB_NAME)
                    cursor = conn.cursor()
                    cursor.execute("SELECT id FROM transactions WHERE description LIKE ?", (f"%{tx_hash}%",))
                    already_processed = cursor.fetchone()
                    conn.close()

                    if already_processed:
                        continue

                    in_msg = tx.get('in_msg', {})
                    if not in_msg:
                        continue

                    comment = in_msg.get('message', '').strip()
                    if not comment and 'msg_data' in in_msg:
                        comment = in_msg['msg_data'].get('text', '').strip()

                    if not comment:
                        continue

                    try:
                        user_id = int(comment)
                    except ValueError:
                        continue

                    if not get_user(user_id):
                        continue

                    value_nanoton = int(in_msg.get('value', 0))
                    if value_nanoton <= 0:
                        continue

                    amount_ton = value_nanoton / 1_000_000_000
                    amount_uzs = amount_ton * ton_rate

                    update_balance(user_id, amount_uzs)
                    add_transaction(
                        user_id=user_id,
                        amount=amount_uzs,
                        tx_type="deposit",
                        status="success",
                        description=f"Пополнение TON: {amount_ton} TON (hash: {tx_hash})"
                    )
                    logger.info(f"💰 [Railway] Зачислено {amount_uzs} сум пользователю {user_id}")

    except Exception as e:
        logger.error(f"Ошибка в функции мониторинга: {e}")
