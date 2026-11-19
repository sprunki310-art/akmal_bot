"""
Fragment API — HTTP версия с прокси и сохранением токена в БД
"""

import json
import os
import time
import requests
import sqlite3
from config import (
    FRAGMENT_API_URL, FRAGMENT_API_KEY, FRAGMENT_PHONE,
    FRAGMENT_MNEMONICS, DB_NAME, logger
)

FRAGMENT_PROXY = os.getenv("FRAGMENT_PROXY")

def get_proxies():
    if FRAGMENT_PROXY:
        logger.info(f"🔀 Используется прокси: {FRAGMENT_PROXY.split('@')[-1]}")
        return {"http": FRAGMENT_PROXY, "https": FRAGMENT_PROXY}
    return None


FRAGMENT_PROXY_FALLBACK_DIRECT = os.getenv("FRAGMENT_PROXY_FALLBACK_DIRECT", "1") != "0"
FRIENDLY_NET_ERROR = "Сервис временно недоступен, попробуйте позже. Баланс возвращён."


def _post(url, **kwargs):
    """POST через прокси; если прокси мёртв — одна попытка напрямую (отключается FRAGMENT_PROXY_FALLBACK_DIRECT=0)."""
    kwargs.setdefault("timeout", (10, 60))  # (connect, read)
    proxies = get_proxies()
    try:
        return requests.post(url, proxies=proxies, **kwargs)
    except (requests.exceptions.ProxyError, requests.exceptions.ConnectTimeout) as e:
        if proxies and FRAGMENT_PROXY_FALLBACK_DIRECT:
            logger.warning(f"⚠️ Прокси недоступен ({type(e).__name__}), пробую напрямую...")
            return requests.post(url, proxies=None, **kwargs)
        raise


def load_fragment_token():
    """Загружает токен из БД."""
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM settings WHERE key = 'fragment_token'")
        res = cursor.fetchone()
        conn.close()
        if res:
            logger.info("✅ Токен Fragment загружен из БД.")
            return res[0]
    except Exception as e:
        logger.error(f"❌ Ошибка чтения токена из БД: {e}")
    return None


def save_fragment_token(token):
    """Сохраняет токен в БД."""
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('fragment_token', ?)",
            (token,)
        )
        conn.commit()
        conn.close()
        logger.info("✅ Токен Fragment сохранён в БД.")
    except Exception as e:
        logger.error(f"❌ Ошибка сохранения токена в БД: {e}")


def authenticate_fragment():
    if not FRAGMENT_API_KEY or not FRAGMENT_PHONE or not FRAGMENT_MNEMONICS:
        logger.error("❌ FRAGMENT_API_KEY / FRAGMENT_PHONE / FRAGMENT_MNEMONICS не заданы.")
        return None

    try:
        mnemonics_list = FRAGMENT_MNEMONICS.strip().split()
        payload = {
            "api_key": FRAGMENT_API_KEY,
            "phone_number": FRAGMENT_PHONE,
            "mnemonics": mnemonics_list,
            "version": "V4R2"
        }

        logger.info("🔐 Авторизация в Fragment API...")

        res = None
        for attempt in range(3):
            try:
                res = _post(
                    f"{FRAGMENT_API_URL}/auth/authenticate/",
                    json=payload,
                    timeout=(10, 60)
                )
                break
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
                logger.warning(f"⚠️ Таймаут авторизации, попытка {attempt + 1}/3...")
                time.sleep(5)

        if res is None:
            logger.error("❌ Все 3 попытки авторизации завершились таймаутом")
            return None

        if res.status_code == 200:
            token = res.json().get("token")
            if token:
                save_fragment_token(token)
                logger.info("✅ Авторизация Fragment успешна.")
                return token
            else:
                logger.error(f"❌ Токен не получен. Ответ: {res.json()}")
                return None
        else:
            logger.error(f"❌ Ошибка авторизации Fragment: {res.status_code} — {res.text}")
            return None

    except Exception as e:
        logger.error(f"❌ Исключение при авторизации Fragment: {e}")
        return None


def send_stars(token, username, quantity):
    for attempt in range(2):
        try:
            headers = {
                "Authorization": f"JWT {token}",
                "Content-Type": "application/json"
            }
            payload = {
                "username": username.lstrip("@"),
                "quantity": int(quantity),
                "show_sender": False
            }

            logger.info(f"🌟 Отправка {quantity} ⭐ → @{username} (попытка {attempt + 1})")
            res = _post(
                f"{FRAGMENT_API_URL}/order/stars/",
                json=payload,
                headers=headers,
                timeout=(10, 60)
            )

            if res.status_code == 200:
                logger.info(f"✅ Звёзды успешно отправлены @{username}")
                return True, "Успешно"

            elif res.status_code == 401 and attempt == 0:
                logger.warning("⚠️ Токен истёк, переавторизация...")
                token = authenticate_fragment()
                if not token:
                    return False, "Ошибка авторизации Fragment"
                continue

            else:
                logger.error(f"❌ Ошибка отправки звёзд: {res.status_code} — {res.text}")
                return False, res.text

        except requests.exceptions.Timeout:
            logger.error("❌ Таймаут при отправке звёзд")
            return False, "Таймаут запроса"
        except requests.exceptions.ConnectionError as e:
            logger.error(f"❌ Нет соединения с Fragment API: {e}")
            return False, FRIENDLY_NET_ERROR
        except Exception as e:
            logger.error(f"❌ Исключение при отправке звёзд: {e}")
            return False, str(e)

    return False, "Не удалось отправить звёзды"


def send_premium(token, username, months):
    for attempt in range(2):
        try:
            headers = {
                "Authorization": f"JWT {token}",
                "Content-Type": "application/json"
            }
            payload = {
                "username": username.lstrip("@"),
                "months": int(months),
                "show_sender": False
            }

            logger.info(f"👑 Отправка Premium {months}м → @{username} (попытка {attempt + 1})")
            res = _post(
                f"{FRAGMENT_API_URL}/order/premium/",
                json=payload,
                headers=headers,
                timeout=(10, 60)
            )

            if res.status_code == 200:
                logger.info(f"✅ Premium успешно отправлен @{username}")
                return True, "Успешно"

            elif res.status_code == 401 and attempt == 0:
                logger.warning("⚠️ Токен истёк, переавторизация...")
                token = authenticate_fragment()
                if not token:
                    return False, "Ошибка авторизации Fragment"
                continue

            else:
                logger.error(f"❌ Ошибка отправки Premium: {res.status_code} — {res.text}")
                return False, res.text

        except requests.exceptions.Timeout:
            logger.error("❌ Таймаут при отправке Premium")
            return False, "Таймаут запроса"
        except requests.exceptions.ConnectionError as e:
            logger.error(f"❌ Нет соединения с Fragment API: {e}")
            return False, FRIENDLY_NET_ERROR
        except Exception as e:
            logger.error(f"❌ Исключение при отправке Premium: {e}")
            return False, str(e)

    return False, "Не удалось отправить Premium"
