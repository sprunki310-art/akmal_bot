"""
tg_notify.py
============
Лёгкие уведомления в Telegram через HTTP API бота — без зависимости от
telebot и без циклического импорта bot.py <-> webapp.py.

Используется webapp.py, чтобы оповещать пользователей и админов о событиях
(пополнение, покупка, новый реферал и т.д.), пока вся остальная логика теперь
находится в Mini App.
"""

import json
import requests
from config import BOT_TOKEN, logger

API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"


def _is_parse_error(res):
    """Telegram отвечает 400 \"can't parse entities\", если в тексте есть неэкранированные
    _ * ` [ (например username вида my_user_name). Такое сообщение раньше терялось молча."""
    try:
        return res.status_code == 400 and "parse entities" in res.text
    except Exception:
        return False


def send_message(chat_id, text, parse_mode="Markdown", reply_markup=None):
    if not BOT_TOKEN:
        return False
    try:
        payload = {"chat_id": chat_id, "text": text}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup:
            payload["reply_markup"] = reply_markup
        res = requests.post(f"{API_BASE}/sendMessage", json=payload, timeout=10)
        if res.status_code != 200 and parse_mode and _is_parse_error(res):
            # Повторяем без разметки — лучше получить уведомление «сырым», чем не получить вообще
            payload.pop("parse_mode", None)
            payload["text"] = text.replace("*", "").replace("`", "")
            res = requests.post(f"{API_BASE}/sendMessage", json=payload, timeout=10)
        if res.status_code != 200:
            logger.warning(f"tg_notify.send_message failed for {chat_id}: {res.text}")
            return False
        return True
    except Exception as e:
        logger.error(f"tg_notify.send_message error for {chat_id}: {e}")
        return False


def send_photo_bytes(chat_id, photo_bytes, caption=None, parse_mode="Markdown", reply_markup=None):
    """Отправляет фото (raw bytes) в чат или канал. reply_markup — dict инлайн-клавиатуры."""
    if not BOT_TOKEN:
        return False
    try:
        def _do(use_markdown):
            data = {"chat_id": chat_id}
            if caption:
                data["caption"] = caption if use_markdown else caption.replace("*", "").replace("`", "")
                if use_markdown and parse_mode:
                    data["parse_mode"] = parse_mode
            if reply_markup:
                data["reply_markup"] = json.dumps(reply_markup)
            files = {"photo": ("photo.jpg", photo_bytes)}
            return requests.post(f"{API_BASE}/sendPhoto", data=data, files=files, timeout=20)

        res = _do(True)
        if res.status_code != 200 and caption and _is_parse_error(res):
            res = _do(False)
        if res.status_code != 200:
            logger.warning(f"tg_notify.send_photo_bytes failed for {chat_id}: {res.text}")
            return False
        return True
    except Exception as e:
        logger.error(f"tg_notify.send_photo_bytes error for {chat_id}: {e}")
        return False


def edit_message_markup(chat_id, message_id, reply_markup=None):
    """Убирает/меняет инлайн-кнопки под сообщением (например, после одобрения заявки)."""
    if not BOT_TOKEN:
        return False
    try:
        payload = {"chat_id": chat_id, "message_id": message_id, "reply_markup": reply_markup or {"inline_keyboard": []}}
        res = requests.post(f"{API_BASE}/editMessageReplyMarkup", json=payload, timeout=10)
        return res.status_code == 200
    except Exception as e:
        logger.error(f"tg_notify.edit_message_markup error: {e}")
        return False


def notify_admins(admin_ids, text, parse_mode="Markdown", reply_markup=None):
    for admin_id in admin_ids:
        send_message(admin_id, text, parse_mode=parse_mode, reply_markup=reply_markup)


def notify_admins_photo(admin_ids, photo_bytes, caption=None, parse_mode="Markdown", reply_markup=None):
    """Рассылает фото (например, чек/скрин перевода) всем админам.
    Если фото не передано или отправка не удалась — не делает fallback
    на текст, это должен решать вызывающий код."""
    ok = True
    for admin_id in admin_ids:
        sent = send_photo_bytes(admin_id, photo_bytes, caption=caption, parse_mode=parse_mode, reply_markup=reply_markup)
        ok = ok and sent
    return ok
