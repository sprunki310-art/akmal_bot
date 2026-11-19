import uuid
import requests
import hashlib
import json
from config import FREEDOM_MERCHANT_ID, FREEDOM_SECRET_KEY, FREEDOM_API_URL, logger
from db import add_payment


def create_freedom_payment(amount, user_id, bot_username):
    """Создает платеж через Freedom Pay (FreedomPay) в узбекских сумах."""
    if not FREEDOM_MERCHANT_ID or not FREEDOM_SECRET_KEY:
        logger.error("❌ Учетные данные Freedom Pay отсутствуют.")
        return None

    order_id = str(uuid.uuid4())
    return_url = f"https://t.me/{bot_username}"

    payload = {
        "merchant_id": FREEDOM_MERCHANT_ID,
        "amount": int(amount),           # Freedom Pay принимает сумму в целых числах (тийины или сумы)
        "currency": "UZS",
        "order_id": order_id,
        "description": f"Пополнение баланса (user_id: {user_id})",
        "success_url": return_url,
        "failure_url": return_url,
        "cancel_url": return_url,
        "extra": {"user_id": str(user_id)}
    }

    # Подпись: md5(merchant_id + amount + currency + order_id + secret_key)
    sign_str = (
        str(FREEDOM_MERCHANT_ID) +
        str(int(amount)) +
        "UZS" +
        order_id +
        FREEDOM_SECRET_KEY
    )
    payload["sign"] = hashlib.md5(sign_str.encode("utf-8")).hexdigest()

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json"
    }

    try:
        logger.info(f"🔄 Создание платежа Freedom Pay: {amount} UZS для пользователя {user_id}")
        response = requests.post(FREEDOM_API_URL, json=payload, headers=headers, timeout=30)

        if response.status_code not in (200, 201):
            logger.error(f"❌ Ошибка Freedom Pay API: {response.status_code} - {response.text}")
            return None

        data = response.json()

        # Freedom Pay возвращает payment_url или redirect_url
        payment_url = data.get("payment_url") or data.get("redirect_url") or data.get("url")
        payment_id = data.get("payment_id") or data.get("transaction_id") or order_id

        if not payment_url:
            logger.error(f"❌ Freedom Pay не вернул URL для оплаты: {data}")
            return None

        # Сохраняем платеж в БД
        add_payment(user_id, amount, payment_id, "pending")

        logger.info(f"✅ Платеж Freedom Pay создан: {payment_id}")
        return payment_url

    except requests.exceptions.Timeout:
        logger.error("❌ Таймаут при создании платежа Freedom Pay")
        return None
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ Ошибка сети при создании платежа Freedom Pay: {str(e)}")
        return None
    except Exception as e:
        logger.error(f"❌ Неожиданная ошибка создания платежа Freedom Pay: {str(e)}")
        return None


def check_payment_status(payment_id):
    """Проверяет статус платежа через Freedom Pay."""
    if not FREEDOM_MERCHANT_ID or not FREEDOM_SECRET_KEY:
        logger.error("❌ Учетные данные Freedom Pay отсутствуют.")
        return None

    sign_str = str(FREEDOM_MERCHANT_ID) + payment_id + FREEDOM_SECRET_KEY
    sign = hashlib.md5(sign_str.encode("utf-8")).hexdigest()

    url = f"{FREEDOM_API_URL}/{payment_id}"
    params = {
        "merchant_id": FREEDOM_MERCHANT_ID,
        "sign": sign
    }

    try:
        response = requests.get(url, params=params, timeout=30)
        response.raise_for_status()
        data = response.json()

        # Нормализуем статус в формат, ожидаемый bot.py
        raw_status = data.get("status", "").lower()
        if raw_status in ("paid", "success", "succeeded", "completed"):
            data["status"] = "succeeded"
        elif raw_status in ("pending", "created", "waiting"):
            data["status"] = "pending"
        else:
            data["status"] = raw_status

        return data

    except Exception as e:
        logger.error(f"❌ Ошибка проверки платежа Freedom Pay: {str(e)}")
        return None
