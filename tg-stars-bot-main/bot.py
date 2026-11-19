"""
bot.py
======
Минимальный Telegram-бот.

Раньше здесь были десятки обработчиков (покупка звёзд/премиума, профиль,
депозиты, рефералка, отзывы, админ-команды). Теперь у бота одна задача —
поприветствовать пользователя и открыть Mini App, в котором реализован
весь остальной функционал, включая админ-панель.

Бот всё ещё:
- регистрирует пользователя и реферальную связь по /start;
- запускает Flask-сервер Mini App (webapp.py) в отдельном потоке;
- запускает фоновый мониторинг TON-кошелька (для автопополнений);
- шлёт обычные уведомления (бонус рефереру и т.п.).
Никакой "бизнес-логики" (покупки, депозиты, админ-действия) бот больше не
обрабатывает — за это отвечают HTTP-эндпоинты в webapp.py.
"""

import os
import asyncio
import threading
import time

import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo, Message

from config import BOT_TOKEN, WEBAPP_URL, WELCOME_MESSAGES, ADMIN_IDS, logger
from db import init_db, get_user, create_user, update_username, get_user_language, is_banned

if not BOT_TOKEN:
    logger.critical("❌ BOT_TOKEN не найден в переменных окружения! Бот не может быть запущен.")
    raise ValueError("BOT_TOKEN is missing")

bot = telebot.TeleBot(BOT_TOKEN)


def open_app_keyboard(lang: str) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup()
    if WEBAPP_URL:
        label = "🛍️ Do'konni ochish" if lang == "uz" else "🛍️ Открыть магазин"
        markup.add(InlineKeyboardButton(label, web_app=WebAppInfo(url=WEBAPP_URL)))
    return markup


@bot.message_handler(commands=['start'])
def start_command(message: Message):
    try:
        user_id = message.from_user.id
        username = message.from_user.username or ""

        # Заблокированным магазин не открываем (админов бан не касается)
        if user_id not in ADMIN_IDS and is_banned(user_id):
            bot.send_message(message.chat.id, "🚫 Ваш доступ к магазину заблокирован.\n\nЕсли это ошибка — свяжитесь с администратором.")
            return

        referrer_id = None
        args = message.text.split()
        if len(args) > 1 and args[1].startswith('ref_'):
            try:
                ref_val = int(args[1].split('_')[1])
                if ref_val != user_id:
                    referrer_id = ref_val
            except (IndexError, ValueError):
                pass

        user = get_user(user_id)
        is_new = not user
        if is_new:
            create_user(user_id, username, referrer_id)
            if referrer_id:
                try:
                    bot.send_message(
                        referrer_id,
                        f"🎉 *Новый реферал!*\n\nПо вашей ссылке зарегистрировался пользователь "
                        f"@{username or user_id}.\n💰 Вы получите бонус *3 000 сум*, когда он совершит "
                        f"свою первую покупку!",
                        parse_mode='Markdown'
                    )
                except Exception as e:
                    logger.warning(f"Не удалось уведомить реферера {referrer_id}: {e}")
        else:
            update_username(user_id, username)

        lang = get_user_language(user_id)
        welcome_text = WELCOME_MESSAGES.get(lang, WELCOME_MESSAGES['ru'])

        if not WEBAPP_URL:
            bot.send_message(
                message.chat.id,
                welcome_text + "\n\n⚠️ Магазин временно недоступен (не настроен WEBAPP_URL).",
                parse_mode='Markdown'
            )
            return

        bot.send_message(
            message.chat.id,
            welcome_text,
            reply_markup=open_app_keyboard(lang),
            parse_mode='Markdown'
        )
    except Exception as e:
        logger.error(f"Критическая ошибка обработки команды /start: {e}")


# ==================== NFT: КНОПКИ «ОДОБРИТЬ / ОТКЛОНИТЬ» В УВЕДОМЛЕНИИ ====================

@bot.callback_query_handler(func=lambda call: bool(call.data) and call.data.startswith(("nft_ok:", "nft_no:")))
def nft_moderation_callback(call):
    """Админ решает по заявке на NFT прямо из уведомления в чате с ботом.
    Логика — та же, что у кнопок в «Админ → NFT» (функции из webapp.py)."""
    try:
        if call.from_user.id not in ADMIN_IDS:
            bot.answer_callback_query(call.id, "Нет доступа", show_alert=True)
            return

        action, _, raw_id = call.data.partition(":")
        try:
            listing_id = int(raw_id)
        except ValueError:
            bot.answer_callback_query(call.id, "Некорректная заявка")
            return

        from webapp import nft_admin_approve, nft_admin_reject
        approve = action == "nft_ok"
        ok, err = (nft_admin_approve if approve else nft_admin_reject)(listing_id, call.from_user.id)

        if ok:
            verdict = "✅ Одобрено" if approve else "❌ Отклонено"
            bot.answer_callback_query(call.id, verdict)
        else:
            reasons = {
                "not_found": "Заявка не найдена",
                "invalid_status": "По этой заявке уже принято решение",
            }
            verdict = None
            bot.answer_callback_query(call.id, reasons.get(err, "Ошибка"), show_alert=True)

        # Убираем кнопки (чтобы не нажать дважды) и дописываем итог в подпись
        try:
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
            if verdict:
                who = f"@{call.from_user.username}" if call.from_user.username else str(call.from_user.id)
                new_text = f"{(call.message.caption or call.message.text or '')}\n\n{verdict} · {who}"
                if call.message.content_type == "photo":
                    bot.edit_message_caption(new_text, call.message.chat.id, call.message.message_id)
                else:
                    bot.edit_message_text(new_text, call.message.chat.id, call.message.message_id)
        except Exception as e:
            logger.warning(f"Не удалось обновить сообщение с заявкой NFT: {e}")
    except Exception as e:
        logger.error(f"Ошибка обработки кнопки модерации NFT: {e}")
        try:
            bot.answer_callback_query(call.id, "Ошибка, откройте «Админ → NFT» в мини-аппе")
        except Exception:
            pass


# ==================== ИГРОВЫЕ ЗАЯВКИ (Steam): КНОПКИ «ВЫПОЛНЕНО / ОТМЕНИТЬ» ====================

@bot.callback_query_handler(func=lambda call: bool(call.data) and call.data.startswith(("game_ok:", "game_no:")))
def game_order_callback(call):
    """Админ подтверждает или отменяет игровую заявку прямо из уведомления.
    Та же логика, что в «Админ → Игры» (функции из webapp.py): при выполнении клиенту
    уходит сообщение об успешном пополнении, при отмене деньги возвращаются на баланс."""
    try:
        if call.from_user.id not in ADMIN_IDS:
            bot.answer_callback_query(call.id, "Нет доступа", show_alert=True)
            return
        action, _, raw_id = call.data.partition(":")
        try:
            order_id = int(raw_id)
        except ValueError:
            bot.answer_callback_query(call.id, "Некорректная заявка")
            return

        from webapp import game_order_admin_complete, game_order_admin_cancel
        done = action == "game_ok"
        ok, err = (game_order_admin_complete if done else game_order_admin_cancel)(order_id, call.from_user.id)
        verdict = None
        if ok:
            verdict = "✅ Выполнено" if done else "❌ Отменено, деньги возвращены"
            bot.answer_callback_query(call.id, verdict)
        else:
            reasons = {"not_found": "Заявка не найдена", "already_decided": "По этой заявке уже принято решение"}
            bot.answer_callback_query(call.id, reasons.get(err, "Ошибка"), show_alert=True)

        try:
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
            if verdict:
                who = f"@{call.from_user.username}" if call.from_user.username else str(call.from_user.id)
                bot.edit_message_text(f"{call.message.text or ''}\n\n{verdict} · {who}",
                                      call.message.chat.id, call.message.message_id)
        except Exception as e:
            logger.warning(f"Не удалось обновить сообщение с игровой заявкой: {e}")
    except Exception as e:
        logger.error(f"Ошибка обработки кнопки игровой заявки: {e}")
        try:
            bot.answer_callback_query(call.id, "Ошибка, откройте «Админ → Игры» в мини-аппе")
        except Exception:
            pass


# ==================== ПОПОЛНЕНИЯ, ВЫВОДЫ, СКРИНШОТЫ, ПРОВЕРКА NFT: КНОПКИ В УВЕДОМЛЕНИЯХ ====================

# префикс callback_data -> (функция из webapp.py, аргумент action или None, текст итога)
ADMIN_DECISIONS = {
    "dep_ok": ("deposit_admin_approve", None, "✅ Пополнение зачислено"),
    "dep_no": ("deposit_admin_reject", None, "❌ Отклонено"),
    "wd_ok": ("withdrawal_admin_decide", "approve", "✅ Вывод подтверждён"),
    "wd_no": ("withdrawal_admin_decide", "reject", "❌ Отклонено, деньги возвращены"),
    "so_ok": ("standoff_screenshot_decide", "confirm", "✅ Скриншот принят"),
    "so_no": ("standoff_screenshot_decide", "reject", "❌ Скриншот отклонён"),
    "nv_ok": ("nft_admin_verify", "confirm", "✅ NFT опубликован"),
    "nv_no": ("nft_admin_verify", "reject", "❌ NFT не найден, продавцу отправлен запрос"),
    # кнопки из оповещений безопасности (подозрительная активность)
    "ban": ("admin_ban", None, "🚫 Пользователь заблокирован"),
    "unban": ("admin_unban", None, "✅ Пользователь разблокирован"),
}


@bot.callback_query_handler(func=lambda call: bool(call.data) and call.data.split(":")[0] in ADMIN_DECISIONS)
def admin_decision_callback(call):
    """Единый обработчик кнопок «Одобрить / Отклонить» в уведомлениях админам.
    Логика — та же, что в соответствующих разделах «Админ» мини-аппа (общие функции webapp.py)."""
    try:
        if call.from_user.id not in ADMIN_IDS:
            bot.answer_callback_query(call.id, "Нет доступа", show_alert=True)
            return
        prefix, _, raw_id = call.data.partition(":")
        try:
            item_id = int(raw_id)
        except ValueError:
            bot.answer_callback_query(call.id, "Некорректная заявка")
            return

        import webapp
        func_name, action, verdict = ADMIN_DECISIONS[prefix]
        func = getattr(webapp, func_name)
        ok, err = func(item_id, action, call.from_user.id) if action else func(item_id, call.from_user.id)

        if ok:
            bot.answer_callback_query(call.id, verdict)
        else:
            reasons = {
                "not_found": "Заявка не найдена",
                "already_decided": "По этой заявке уже принято решение",
                "invalid_status": "По этой заявке уже принято решение",
                "already_banned": "Пользователь уже заблокирован",
                "cannot_ban_admin": "Нельзя заблокировать администратора",
                "not_banned": "Пользователь не заблокирован",
            }
            verdict = None
            bot.answer_callback_query(call.id, reasons.get(err, "Ошибка"), show_alert=True)

        # убираем кнопки и дописываем итог (подпись у фото, текст у обычного сообщения)
        try:
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
            if verdict:
                who = f"@{call.from_user.username}" if call.from_user.username else str(call.from_user.id)
                new_text = f"{(call.message.caption or call.message.text or '')}\n\n{verdict} · {who}"
                if call.message.content_type == "photo":
                    bot.edit_message_caption(new_text, call.message.chat.id, call.message.message_id)
                else:
                    bot.edit_message_text(new_text, call.message.chat.id, call.message.message_id)
        except Exception as e:
            logger.warning(f"Не удалось обновить сообщение с заявкой: {e}")
    except Exception as e:
        logger.error(f"Ошибка обработки кнопки администратора: {e}")
        try:
            bot.answer_callback_query(call.id, "Ошибка, откройте нужный раздел «Админ» в мини-аппе")
        except Exception:
            pass


# ==================== ФОНОВЫЙ МОНИТОРИНГ TON ====================

def ton_background_loop():
    """Каждые 30 секунд обновляет курс TON и проверяет новые поступления на кошелёк."""
    from config import TON_DEPOSIT_ADDRESS
    if not TON_DEPOSIT_ADDRESS:
        logger.info("ℹ️ TON_DEPOSIT_ADDRESS не задан — мониторинг TON отключён.")
        return

    from ton_monitor import get_ton_rate_from_api, monitor_deposits

    while True:
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(get_ton_rate_from_api())
                loop.run_until_complete(monitor_deposits())
            finally:
                loop.close()
        except Exception as e:
            logger.error(f"Сбой в фоновом мониторинге TON: {e}")
        time.sleep(30)


if __name__ == '__main__':
    try:
        init_db()
    except Exception as e:
        logger.error(f"Не удалось инициализировать БД: {e}")

    threading.Thread(target=ton_background_loop, daemon=True).start()

    try:
        from webapp import run_webapp
        webapp_port = int(os.getenv("WEBAPP_PORT", os.getenv("PORT", "5000")))
        threading.Thread(target=run_webapp, kwargs={"port": webapp_port}, daemon=True).start()
        logger.info(f"✅ Mini App сервер запущен на порту {webapp_port}")
    except Exception as e:
        logger.error(f"Не удалось запустить Mini App сервер: {e}")

    logger.info("🚀 Бот запущен в режиме бесконечного пуллинга (только /start).")
    bot.infinity_polling()
