# webapp.py
"""
Flask backend для Telegram Mini App "Akmal's Stars".

Раньше большая часть логики (покупка звёзд/премиума, депозиты, рефералка,
отзывы, админ-команды) жила в bot.py как обработчики чата. Теперь весь этот
функционал — здесь, в виде HTTP API, которым пользуется фронтенд
(webapp_static/index.html), открытый внутри Telegram как Mini App.

Запускается в отдельном потоке внутри bot.py и работает с той же БД
(database.db). Все запросы проверяются через Telegram initData (HMAC),
кроме одного случая (поллинг статуса оплаты, тоже защищён той же проверкой).
"""

import base64
import binascii
import datetime
import hashlib
import hmac
import io
import json
import math
import os
import random
import re
import sqlite3
import threading
import time
from urllib.parse import parse_qsl, unquote

from PIL import Image

from flask import Flask, jsonify, send_from_directory, request, send_file

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder=os.path.join(BASE_DIR, "webapp_static"), static_url_path="")
app.config["MAX_CONTENT_LENGTH"] = 12 * 1024 * 1024  # 12 MB — хватает на сжатый скрин чека

from config import (
    DB_NAME, BOT_TOKEN, BOT_USERNAME, STAR_PRICE, PREMIUM_PRICES, MIN_STARS,
    MIN_DEPOSIT, REFERRAL_BONUS, REVIEW_CHANNEL, MANUAL_CARD_NUMBER,
    MANUAL_PHONE_NUMBER, ADMIN_IDS, TON_DEPOSIT_ADDRESS,
    SMS_WEBHOOK_SECRET, CARD_DEPOSIT_TTL_MINUTES,
    FREEDOM_MERCHANT_ID, FREEDOM_SECRET_KEY, BRAWL_STARS_PRODUCTS,
    ROBLOX_PRODUCTS, STANDOFF_GOLD_PRODUCTS, STANDOFF_RULES_TEXT,
    NFT_ADMIN_USERNAME, NFT_COMMISSION_PERCENT_DEFAULT, MIN_WITHDRAWAL, logger
)
from db import (
    get_user, create_user, update_balance, add_transaction, mark_transaction_status,
    count_successful_purchases, already_rewarded_for, get_referral_stats, get_referral_list,
    set_user_language, get_user_language, get_next_order_id, add_review,
    add_payment, get_payment, update_payment_status, get_ton_rate,
    add_manual_deposit, get_pending_manual_deposits, get_manual_deposit, set_manual_deposit_status,
    search_users, get_recent_users, get_admin_stats,
    expire_stale_card_deposits, create_card_auto_deposit,
    get_card_deposit, confirm_card_deposit, cancel_card_deposit,
    get_pending_card_deposits, find_pending_card_match, get_recent_transactions,
    create_game_order, get_game_order, set_game_order_extra, set_game_order_status,
    get_pending_game_orders, get_active_game_orders, get_user_game_orders,
    get_all_user_ids, get_setting, set_setting,
    create_nft_listing, get_nft_listing, set_nft_listing_status, set_nft_listing_sold,
    get_active_nft_listings, get_pending_nft_listings, get_user_nft_listings, get_user_nft_purchases,
    get_nft_withdrawable_balance,
    create_withdrawal, get_withdrawal, set_withdrawal_status, get_pending_withdrawals, get_user_withdrawals,
    get_daily_card_prizes, add_daily_card_prize, update_daily_card_prize, delete_daily_card_prize,
    get_last_daily_card_claim, get_daily_card_claims_count_since, add_daily_card_claim, get_pending_nft_card_claims,
    get_recent_daily_card_claims, get_daily_card_claims_stats_today,
    get_daily_card_claim_by_id, set_daily_card_claim_status,
    get_last_extra_open_purchase, add_extra_open_purchase,
    add_user_discount, get_active_user_discount, get_active_user_discounts, mark_user_discount_used,
    get_star_balance, update_star_balance, try_deduct_star_balance,
    get_token_settings, calc_cashback_tokens, get_token_balance, add_tokens,
    spend_tokens, get_token_history,
    get_mystery_prizes, add_mystery_prize, update_mystery_prize, delete_mystery_prize,
    add_mystery_opening, get_mystery_opening, get_pending_mystery_openings,
    set_mystery_opening_status, get_recent_mystery_openings,
    list_users_filtered, get_user_history, get_user_history_summary,
    get_top_star_holders, get_stars_given_today, get_top_token_holders,
    get_activity_feed, get_stars_pool_stats,
    try_deduct_balance, try_mark_nft_sold,
    is_banned, get_ban, ban_user, unban_user, get_banned_users,
    add_security_event, count_security_events, get_recent_security_events,
)
from fragment_api import load_fragment_token, authenticate_fragment, send_stars, send_premium
from freedom import create_freedom_payment, check_payment_status
import tg_notify

_bot_username_cache = BOT_USERNAME or None


def get_bot_username():
    """Возвращает username бота: из .env, либо через getMe (с кэшем)."""
    global _bot_username_cache
    if _bot_username_cache:
        return _bot_username_cache
    try:
        import requests
        res = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getMe", timeout=10)
        data = res.json()
        username = data.get("result", {}).get("username")
        if username:
            _bot_username_cache = username
            return username
    except Exception as e:
        logger.warning(f"Не удалось получить username бота: {e}")
    return "bot"


# ==================== ПРОВЕРКА TELEGRAM INIT DATA ====================

def verify_telegram_data(init_data: str) -> dict | None:
    """Проверяет подпись initData от Telegram WebApp. Возвращает dict с user или None."""
    if not init_data or not BOT_TOKEN:
        return None
    try:
        parsed = dict(parse_qsl(init_data, keep_blank_values=True))
        received_hash = parsed.pop("hash", None)
        if not received_hash:
            return None

        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

        if calculated_hash != received_hash:
            return None

        user_json = parsed.get("user")
        if user_json:
            return json.loads(unquote(user_json))
        return None
    except Exception as e:
        logger.error(f"Ошибка проверки initData: {e}")
        return None


def get_user_from_request():
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    return verify_telegram_data(init_data)


def ensure_user(tg_user):
    """Гарантирует, что пользователь есть в БД, возвращает строку users."""
    user_id = tg_user["id"]
    username = tg_user.get("username") or tg_user.get("first_name") or f"id{user_id}"
    db_user = get_user(user_id)
    if not db_user:
        create_user(user_id, username)
        db_user = get_user(user_id)
    return db_user


# ==================== БЕЗОПАСНОСТЬ: БАНЫ, ОПОВЕЩЕНИЯ АДМИНАМ ====================

SECURITY_ALERT_COOLDOWN = 300   # не чаще одного оповещения одного типа по одному пользователю/IP за 5 минут
_alert_last = {}
_alert_lock = threading.Lock()

SECURITY_TITLES = {
    "forged_initdata": "Подделка авторизации Mini App",
    "admin_access": "Попытка доступа к админ-панели",
    "withdraw_abuse": "Попытка вывести больше разрешённого",
    "double_spend": "Попытка двойной траты баланса (гонка запросов)",
    "bad_webhook_secret": "Неверный секрет SMS-вебхука",
}


def _client_ip():
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.remote_addr or "?"


def security_alert(kind, user_id=None, username=None, details="", ban_button=True, min_events=1):
    """Пишет событие в журнал безопасности и оповещает админов в Telegram.
    Оповещение уходит не чаще SECURITY_ALERT_COOLDOWN на пару (тип, пользователь/IP), чтобы
    атакующий не мог завалить админов сообщениями. min_events — слать оповещение только если
    таких событий у пользователя за последние 10 минут набралось не меньше (для «мягких» случаев).
    Кнопка «Забанить» ставится только для подтверждённого пользователя (не для подделанных данных)
    и никогда — для админов."""
    try:
        ip = _client_ip()
        add_security_event(user_id, username, kind, details, ip)
        logger.warning(f"🚨 SECURITY {kind}: user={user_id} @{username} ip={ip} {details}")

        if min_events > 1 and user_id and count_security_events(user_id, 10, kind) < min_events:
            return
        key = (kind, user_id or ip)
        now = time.time()
        with _alert_lock:
            if now - _alert_last.get(key, 0) < SECURITY_ALERT_COOLDOWN:
                return
            _alert_last[key] = now

        who = ""
        if user_id:
            who = f"👤 @{md_escape(username or '—')} · 🆔 `{user_id}`\n"
        text = (
            f"🚨 *{SECURITY_TITLES.get(kind, kind)}*\n\n"
            f"{who}🌐 IP: `{ip}`\n"
            f"📝 {md_escape(str(details)[:300])}"
        )
        markup = None
        if ban_button and user_id and user_id not in ADMIN_IDS and not is_banned(user_id):
            markup = {"inline_keyboard": [[{"text": "🚫 Забанить", "callback_data": f"ban:{user_id}"}]]}
        threading.Thread(
            target=tg_notify.notify_admins, args=(ADMIN_IDS, text), kwargs={"reply_markup": markup}, daemon=True
        ).start()
    except Exception as e:
        logger.error(f"security_alert: ошибка {e}")


def _charge_or_fail(user_id, cost, tg_user=None):
    """Атомарно списывает cost с баланса. Возвращает None, если списано, иначе готовый ответ-ошибку.
    Предыдущая проверка «balance < cost» + отдельное списание позволяли потратить один и тот же баланс
    несколько раз, отправив много запросов одновременно. Теперь проверка и списание — один SQL-запрос."""
    if try_deduct_balance(user_id, cost):
        return None
    row = get_user(user_id)
    balance = row[2] if row else 0
    security_alert(
        "double_spend", user_id, (tg_user or {}).get("username"),
        f"{request.path}: списание {fmt_amount(cost)} сум не прошло после проверки баланса ({fmt_amount(balance)} сум)",
        min_events=2,
    )
    return jsonify({"error": "not_enough_balance", "balance": balance, "cost": cost}), 400


def classify_send_error(err_msg):
    """Превращает «сырой» ответ Fragment в короткий код причины. Пользователю показывается только
    понятный текст по этому коду — технические подробности (кошельки, балансы, JSON) ему не отдаются."""
    t = str(err_msg or "").lower()
    if "not enough funds" in t or "insufficient" in t:
        return "service_funds"
    if "авториз" in t or "unauthorized" in t or "jwt" in t:
        return "service_unavailable"
    if "таймаут" in t or "timeout" in t or "timed out" in t:
        return "timeout"
    if "временно недоступен" in t or "connection" in t:
        return "service_unavailable"
    if any(k in t for k in ("no telegram users", "user not found", "username not found", "not found", "не найден")):
        return "user_not_found"
    return "unknown"


_fragment_alert_last = {}


def notify_fragment_failure(reason, raw_err, context):
    """Админам — настоящая причина сбоя (например, не хватает TON на кошельке Fragment), пользователю — нет."""
    if reason == "user_not_found":
        return
    now = time.time()
    with _alert_lock:
        if now - _fragment_alert_last.get(reason, 0) < 600:
            return
        _fragment_alert_last[reason] = now
    raw = str(raw_err or "")
    if reason == "service_funds":
        bal = re.search(r"balance:\s*'?([\d.]+)\s*TON", raw)
        tot = re.search(r"transaction total:\s*([\d.]+)\s*TON", raw)
        extra = ""
        if bal and tot:
            extra = f"\nНа кошельке: *{bal.group(1)} TON*, нужно для заказа: *{tot.group(1)} TON*"
        text = (f"⛔ *На кошельке Fragment не хватает TON!*{extra}\n\n"
                f"Покупки звёзд и Premium сейчас не проходят (пользователям деньги возвращаются). "
                f"Пополните TON-кошелёк.\n📝 {md_escape(context)}")
    else:
        text = f"⚠️ *Сбой отправки через Fragment* ({md_escape(reason)})\n📝 {md_escape(context)}\n❗ {md_escape(raw[:300])}"
    threading.Thread(target=tg_notify.notify_admins, args=(ADMIN_IDS, text), daemon=True).start()


def require_user(f):
    from functools import wraps

    @wraps(f)
    def wrapper(*args, **kwargs):
        tg_user = get_user_from_request()
        if not tg_user:
            if request.headers.get("X-Telegram-Init-Data"):
                # данные авторизации присланы, но подпись не сходится — подделка либо чужой токен
                security_alert("forged_initdata", details=f"{request.method} {request.path}", ban_button=False)
            return jsonify({"error": "unauthorized"}), 401
        if tg_user["id"] not in ADMIN_IDS and is_banned(tg_user["id"]):
            return jsonify({"error": "banned"}), 403
        request.tg_user = tg_user
        return f(*args, **kwargs)
    return wrapper


def require_admin(f):
    from functools import wraps

    @wraps(f)
    def wrapper(*args, **kwargs):
        tg_user = get_user_from_request()
        if not tg_user:
            if request.headers.get("X-Telegram-Init-Data"):
                security_alert("forged_initdata", details=f"{request.method} {request.path}", ban_button=False)
            return jsonify({"error": "unauthorized"}), 401
        if tg_user["id"] not in ADMIN_IDS:
            if is_banned(tg_user["id"]):
                return jsonify({"error": "banned"}), 403
            security_alert("admin_access", tg_user["id"], tg_user.get("username"), f"{request.method} {request.path}")
            return jsonify({"error": "forbidden"}), 403
        request.tg_user = tg_user
        return f(*args, **kwargs)
    return wrapper


def b64_to_bytes(b64_string):
    if not b64_string:
        return None
    if "," in b64_string and b64_string.strip().startswith("data:"):
        b64_string = b64_string.split(",", 1)[1]
    try:
        return base64.b64decode(b64_string)
    except (binascii.Error, ValueError):
        return None


def fmt_amount(value):
    """Форматирует сумму с разделителем разрядов для текста уведомлений, напр. 1 234 567."""
    try:
        return f"{float(value):,.0f}".replace(",", " ")
    except (TypeError, ValueError):
        return str(value)


def md_escape(text):
    """Экранирует спецсимволы Telegram Markdown (legacy) в пользовательском тексте
    (название NFT, реквизиты вывода и т.п.), чтобы случайная одиночная *, _ или `
    в вводе не ломала парсинг ВСЕГО сообщения на стороне Telegram — без экранирования
    Bot API просто отклоняет отправку целиком, и уведомление тихо теряется."""
    if not text:
        return text
    text = str(text)
    for ch in ("_", "*", "`", "["):
        text = text.replace(ch, "\\" + ch)
    return text


_thumbnail_cache = {}


def compress_photo_thumbnail(b64_photo, max_size=320, quality=60):
    """Пересжимает фото на лету под маленькую миниатюру для списка /api/nft/market.

    Нужно независимо от того, как (и сжимал ли вообще) фото клиент при загрузке —
    старые объявления, созданные до появления клиентского сжатия, всё ещё могут
    нести огромные фото, и именно это делало загрузку маркета медленной: 10-15
    объявлений по 300-500 КБ каждое давали по несколько МБ за один ответ API.

    Результат кэшируется в памяти по хэшу входной строки, чтобы не пересжимать
    одно и то же фото на каждый запрос /api/nft/market заново.
    """
    if not b64_photo:
        return b64_photo
    cache_key = hashlib.md5(b64_photo.encode()).hexdigest()
    if cache_key in _thumbnail_cache:
        return _thumbnail_cache[cache_key]
    try:
        raw = base64.b64decode(b64_photo)
        img = Image.open(io.BytesIO(raw))
        img = img.convert("RGB")
        img.thumbnail((max_size, max_size), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        result = base64.b64encode(buf.getvalue()).decode()
    except Exception:
        # Если фото повреждено/неожиданного формата — отдаём оригинал,
        # чтобы не ронять весь список объявлений из-за одного плохого файла.
        result = b64_photo
    # Кэш не должен расти бесконечно на долгоживущем процессе
    if len(_thumbnail_cache) > 500:
        _thumbnail_cache.clear()
    _thumbnail_cache[cache_key] = result
    return result


# ==================== РЕДАКТИРУЕМЫЕ ЦЕНЫ (админка → «Цены») ====================
#
# Цены по умолчанию живут в config.py (STAR_PRICE, PREMIUM_PRICES,
# BRAWL_STARS_PRODUCTS, ROBLOX_PRODUCTS, STANDOFF_GOLD_PRODUCTS) — это те
# значения, что были в коде на момент деплоя. Админ может переопределить
# любую цену из Mini App; переопределения хранятся в таблице settings
# (переживают редеплой, в отличие от правки config.py вручную) и всегда
# имеют приоритет над дефолтами из кода.

def _extract_price_old(val, default_price=None):
    """Значение переопределения цены может быть числом (старый формат, без
    скидки) или объектом {"price","old_price"} (новый формат — поддерживает
    зачёркнутую «цену без скидки»). Возвращает (price, old_price)."""
    if isinstance(val, dict):
        try:
            price = float(val.get("price"))
        except (TypeError, ValueError):
            price = default_price
        old_price = val.get("old_price")
        try:
            old_price = float(old_price) if old_price not in (None, "") else None
        except (TypeError, ValueError):
            old_price = None
        if old_price is not None and price is not None and old_price <= price:
            old_price = None
        return price, old_price
    try:
        return float(val), None
    except (TypeError, ValueError):
        return default_price, None


def get_effective_star_price():
    raw = get_setting("star_price")
    val = raw
    if isinstance(raw, str):
        try:
            val = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            val = raw  # старый формат — просто число, сохранённое как строка
    price, _ = _extract_price_old(val, STAR_PRICE)
    return price if price is not None else STAR_PRICE


def get_effective_star_old_price():
    raw = get_setting("star_price")
    val = raw
    if isinstance(raw, str):
        try:
            val = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            val = raw
    _, old_price = _extract_price_old(val, STAR_PRICE)
    return old_price


def get_effective_premium_prices():
    v = get_setting("premium_prices")
    result = dict(PREMIUM_PRICES)
    if v:
        try:
            overrides = json.loads(v)
            for months, val in overrides.items():
                price, _ = _extract_price_old(val)
                if price is not None:
                    result[int(months)] = price
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    return result


def get_effective_premium_old_prices():
    v = get_setting("premium_prices")
    result = {}
    if v:
        try:
            overrides = json.loads(v)
            for months, val in overrides.items():
                _, old_price = _extract_price_old(val)
                if old_price is not None:
                    result[int(months)] = old_price
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    return result


def _get_product_price_overrides():
    v = get_setting("product_prices")
    if not v:
        return {}
    try:
        return json.loads(v)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def apply_product_price_overrides(products):
    """Возвращает копию списка товаров (Brawl Stars / Roblox / Standoff) с
    применёнными переопределениями цены (и, если есть, старой ценой для
    отображения скидки) из settings."""
    overrides = _get_product_price_overrides()
    result = []
    for p in products:
        p2 = dict(p)
        p2["old_price"] = None
        if p["id"] in overrides:
            price, old_price = _extract_price_old(overrides[p["id"]])
            if price is not None:
                p2["price"] = price
            p2["old_price"] = old_price
        result.append(p2)
    return result


def get_effective_product_price(products, product_id):
    """Находит товар по id в каталоге и возвращает его с учётом переопределения цены."""
    for p in products:
        if p["id"] == product_id:
            p2 = dict(p)
            overrides = _get_product_price_overrides()
            if product_id in overrides:
                price, _ = _extract_price_old(overrides[product_id])
                if price is not None:
                    p2["price"] = price
            return p2
    return None


def get_effective_nft_commission():
    """Комиссия маркетплейса NFT в процентах (редактируется из админки)."""
    v = get_setting("nft_commission_percent")
    if v is None:
        return NFT_COMMISSION_PERCENT_DEFAULT
    try:
        return float(v)
    except (TypeError, ValueError):
        return NFT_COMMISSION_PERCENT_DEFAULT


STEAM_RATE_DEFAULT = 13000   # сум за $1 (меняется в «Админ → Цены»)
STEAM_MIN_USD = 1
STEAM_MAX_USD = 500
STEAM_QUICK_AMOUNTS = [1, 5, 10]


def get_effective_steam_rate():
    """Курс доллара для пополнения Steam (сум за $1), редактируется из админки."""
    v = get_setting("steam_rate")
    try:
        rate = float(v)
        return rate if rate > 0 else STEAM_RATE_DEFAULT
    except (TypeError, ValueError):
        return STEAM_RATE_DEFAULT


# ==================== СТАТИКА (сам Mini App) ====================

@app.route("/")
def index():
    resp = send_from_directory(app.static_folder, "index.html")
    # Mini App всегда должен получать свежий index.html, иначе Telegram показывает старую версию из кэша
    resp.headers["Cache-Control"] = "no-cache, must-revalidate"
    return resp


# ==================== ПРОФИЛЬ ====================

def _get_formatted_user_discounts(user_id):
    disc_rows = get_active_user_discounts(user_id)
    user_discounts = []
    now_dt = datetime.datetime.now()
    for r in disc_rows:
        disc_id, percent, category, prod_id, prod_name, created_at, expires_at = r
        days_left = None
        hours_left = None
        if expires_at:
            try:
                exp_dt = datetime.datetime.strptime(expires_at, "%Y-%m-%d %H:%M:%S")
                diff = exp_dt - now_dt
                hours_left = max(0, int(diff.total_seconds() // 3600))
                days_left = max(0, diff.days)
            except Exception:
                pass
        user_discounts.append({
            "id": disc_id,
            "percent": percent,
            "category": category,
            "product_id": prod_id or "all",
            "product_name": prod_name or ("Все товары" if prod_id in ("all", None, "") else prod_id),
            "created_at": created_at,
            "expires_at": expires_at,
            "days_left": days_left,
            "hours_left": hours_left,
        })
    return user_discounts


def _award_cashback(user_id, cost, ref, what):
    """Начисляет токены-кэшбэк за успешную покупку. Идемпотентно по ref.
    Возвращает (начислено, новый_баланс_токенов); ошибки токенов не ломают покупку."""
    try:
        tokens = calc_cashback_tokens(cost)
        earned = tokens if tokens > 0 and add_tokens(user_id, tokens, "cashback", f"Кэшбэк: {what}", ref=ref) else 0
        return earned, get_token_balance(user_id)
    except Exception as e:
        logger.warning(f"Не удалось начислить токены user={user_id} ref={ref}: {e}")
        return 0, 0


@app.route("/api/profile")
@require_user
def api_profile():
    tg_user = request.tg_user
    user_id = tg_user["id"]
    username = tg_user.get("username") or tg_user.get("first_name") or f"id{user_id}"

    db_user = ensure_user(tg_user)
    balance = db_user[2] if db_user else 0.0
    star_balance = db_user[5] if db_user and len(db_user) > 5 else 0
    lang = get_user_language(user_id)
    nft_withdrawable = get_nft_withdrawable_balance(user_id)
    discounts = _get_formatted_user_discounts(user_id)

    return jsonify({
        "user_id": user_id,
        "username": username,
        "balance": balance,
        "star_balance": star_balance,
        "nft_withdrawable": nft_withdrawable,
        "currency": "сум",
        "language": lang,
        "is_admin": user_id in ADMIN_IDS,
        "discounts": discounts,
        "token_balance": get_token_balance(user_id),
        "token_cashback_percent": get_token_settings()["cashback_percent"],
        "token_box_price": get_token_settings()["box_price"],
    })


@app.route("/api/user/discounts")
@require_user
def api_user_discounts():
    user_id = request.tg_user["id"]
    return jsonify({"discounts": _get_formatted_user_discounts(user_id)})


@app.route("/api/language", methods=["POST"])
@require_user
def api_set_language():
    user_id = request.tg_user["id"]
    data = request.get_json(force=True) or {}
    lang = data.get("lang")
    if lang not in ("ru", "uz"):
        return jsonify({"error": "invalid_lang"}), 400
    set_user_language(user_id, lang)
    return jsonify({"ok": True, "language": lang})


# ==================== ЦЕНЫ / ИНФО ДЛЯ ДЕПОЗИТА ====================

@app.route("/api/prices")
def api_prices():
    return jsonify({
        "star_price": get_effective_star_price(),
        "star_price_old": get_effective_star_old_price(),
        "premium": get_effective_premium_prices(),
        "premium_old": get_effective_premium_old_prices(),
        "min_stars": MIN_STARS,
        "min_deposit": MIN_DEPOSIT,
        "ton_address": TON_DEPOSIT_ADDRESS or "",
        "ton_rate": get_ton_rate(),
        "manual_card": MANUAL_CARD_NUMBER,
        "card_auto_enabled": bool(MANUAL_CARD_NUMBER) and bool(SMS_WEBHOOK_SECRET),
        "freedom_enabled": bool(FREEDOM_MERCHANT_ID and FREEDOM_SECRET_KEY),
        "card_ttl_minutes": CARD_DEPOSIT_TTL_MINUTES,
        "brawl_stars_products": apply_product_price_overrides(BRAWL_STARS_PRODUCTS),
        "roblox_products": apply_product_price_overrides(ROBLOX_PRODUCTS),
        "standoff_gold_products": apply_product_price_overrides(STANDOFF_GOLD_PRODUCTS),
        "nft_commission_percent": get_effective_nft_commission(),
        "nft_admin_username": NFT_ADMIN_USERNAME,
        "min_withdrawal": MIN_WITHDRAWAL,
        "steam_rate": get_effective_steam_rate(),
        "steam_min_usd": STEAM_MIN_USD,
        "steam_max_usd": STEAM_MAX_USD,
        "steam_quick_amounts": STEAM_QUICK_AMOUNTS,
    })


# ==================== ИСТОРИЯ ЗАКАЗОВ ====================

@app.route("/api/orders")
@require_user
def api_orders():
    user_id = request.tg_user["id"]
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        """SELECT amount, type, status, description, timestamp
           FROM transactions
           WHERE user_id = ? AND type IN ('buy_stars', 'buy_premium')
           ORDER BY id DESC LIMIT 50""",
        (user_id,)
    )
    rows = cursor.fetchall()
    conn.close()

    orders = [
        {"amount": abs(r[0]), "type": r[1], "status": r[2], "description": r[3], "timestamp": r[4]}
        for r in rows
    ]
    return jsonify({"orders": orders})


@app.route("/api/deposits/my")
@require_user
def api_deposits_my():
    """История пополнений (карта/TON/ручной перевод/Freedom) для вкладки «Пополнения» в «Заказах»."""
    user_id = request.tg_user["id"]
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        """SELECT amount, status, description, timestamp
           FROM transactions
           WHERE user_id = ? AND type = 'deposit'
           ORDER BY id DESC LIMIT 50""",
        (user_id,)
    )
    rows = cursor.fetchall()
    conn.close()
    deposits = [
        {"amount": abs(r[0]), "status": r[1], "description": r[2], "timestamp": r[3]}
        for r in rows
    ]
    return jsonify({"deposits": deposits})


# ==================== РЕФЕРАЛЬНАЯ СТАТИСТИКА ====================

@app.route("/api/referrals")
@require_user
def api_referrals():
    user_id = request.tg_user["id"]
    stats = get_referral_stats(user_id)
    recent = get_referral_list(user_id, limit=10)
    bot_username = get_bot_username()
    return jsonify({
        "count": stats["count"],
        "earned": stats["total_earned"],
        "bonus_per_referral": REFERRAL_BONUS,
        "link": f"https://t.me/{bot_username}?start=ref_{user_id}",
        "recent": [{"username": u or "—", "date": str(d)[:10]} for u, d in recent],
    })


# ==================== ПОКУПКА ЗВЁЗД ====================

def _resolve_recipient(tg_user, data):
    """mode: 'self' | 'friend'. Возвращает (target_username, is_self)."""
    mode = data.get("mode", "self")
    if mode == "friend":
        username = (data.get("username") or "").strip().lstrip("@")
        if username.startswith("https://t.me/"):
            username = username.replace("https://t.me/", "")
        if not username or len(username) < 4:
            return None, False
        return username, False
    username = tg_user.get("username")
    return username, True


def _reward_referrer_if_first_purchase(buyer_id, buyer_username):
    try:
        buyer_row = get_user(buyer_id)
        referrer_id = buyer_row[3] if buyer_row else None
        if not referrer_id:
            return
        if count_successful_purchases(buyer_id) <= 1 and not already_rewarded_for(referrer_id, buyer_id):
            update_balance(referrer_id, REFERRAL_BONUS)
            add_transaction(
                referrer_id, REFERRAL_BONUS, "referral_bonus", "success",
                f"Реферальный бонус за первую покупку пользователя {buyer_username} (ID: {buyer_id})"
            )
            tg_notify.send_message(
                referrer_id,
                f"💸 *Реферальный бонус!*\n\nВаш реферал @{buyer_username} совершил свою первую покупку!\n"
                f"💰 Вам начислено: *+{REFERRAL_BONUS:,} сум*",
            )
    except Exception as e:
        logger.error(f"Ошибка начисления реферального бонуса: {e}")


def _announce_order_in_channel(order_id, buyer_username):
    try:
        text = f"🛒 *Новый заказ №{order_id}*\n⏳ Ожидаем отзыв от @{buyer_username}..."
        tg_notify.send_message(REVIEW_CHANNEL, text)
    except Exception as e:
        logger.warning(f"Не удалось отправить уведомление в канал: {e}")


@app.route("/api/buy/stars", methods=["POST"])
@require_user
def api_buy_stars():
    tg_user = request.tg_user
    user_id = tg_user["id"]
    data = request.get_json(force=True) or {}

    try:
        quantity = int(data.get("quantity", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid_quantity"}), 400

    if quantity < MIN_STARS:
        return jsonify({"error": "min_stars", "min_stars": MIN_STARS}), 400

    target_username, is_self = _resolve_recipient(tg_user, data)
    if not target_username:
        return jsonify({"error": "invalid_username"}), 400

    db_user = ensure_user(tg_user)
    balance = db_user[2]
    cost = quantity * get_effective_star_price()
    if balance < cost:
        return jsonify({"error": "not_enough_balance", "balance": balance, "cost": cost}), 400

    _fail = _charge_or_fail(user_id, cost, tg_user)
    if _fail:
        return _fail
    add_transaction(user_id, -cost, "buy_stars", "pending", f"Покупка {quantity} звёзд для @{target_username}")

    token = load_fragment_token() or authenticate_fragment()
    if not token:
        update_balance(user_id, cost)
        mark_transaction_status(user_id, "buy_stars", "pending", "failed")
        add_transaction(user_id, cost, "refund", "success", "Возврат: ошибка авторизации Fragment")
        return jsonify({"error": "fragment_auth_failed", "balance": balance}), 502

    success, err_msg = send_stars(token, target_username, quantity)

    if not success:
        update_balance(user_id, cost)
        mark_transaction_status(user_id, "buy_stars", "pending", "failed")
        add_transaction(user_id, cost, "refund", "success", f"Возврат: {err_msg}")
        new_balance = get_user(user_id)[2]
        reason = classify_send_error(err_msg)
        notify_fragment_failure(reason, err_msg, f"Покупка {quantity} Stars для @{target_username} (user {user_id})")
        return jsonify({"error": "send_failed", "reason": reason, "balance": new_balance}), 502

    mark_transaction_status(user_id, "buy_stars", "pending", "success")
    buyer_username = tg_user.get("username") or f"id{user_id}"
    _reward_referrer_if_first_purchase(user_id, buyer_username)

    order_id = get_next_order_id()
    recipient_display = "себе" if is_self else target_username
    _announce_order_in_channel(order_id, buyer_username)

    tokens_earned, token_balance = _award_cashback(user_id, cost, f"order:{order_id}", f"{quantity} Stars")
    new_balance = get_user(user_id)[2]
    return jsonify({
        "ok": True,
        "tokens_earned": tokens_earned,
        "token_balance": token_balance,
        "stars": quantity,
        "username": target_username,
        "balance": new_balance,
        "order_id": order_id,
        "product": f"{quantity} Stars",
        "recipient": recipient_display,
        "cost": cost,
    })


# ==================== ПОКУПКА PREMIUM ====================

@app.route("/api/buy/premium", methods=["POST"])
@require_user
def api_buy_premium():
    tg_user = request.tg_user
    user_id = tg_user["id"]
    data = request.get_json(force=True) or {}

    try:
        months = int(data.get("months", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid_months"}), 400

    premium_prices = get_effective_premium_prices()
    if months not in premium_prices:
        return jsonify({"error": "invalid_months"}), 400

    target_username, is_self = _resolve_recipient(tg_user, data)
    if not target_username:
        return jsonify({"error": "invalid_username"}), 400

    db_user = ensure_user(tg_user)
    balance = db_user[2]
    cost = premium_prices[months]
    if balance < cost:
        return jsonify({"error": "not_enough_balance", "balance": balance, "cost": cost}), 400

    _fail = _charge_or_fail(user_id, cost, tg_user)
    if _fail:
        return _fail
    add_transaction(user_id, -cost, "buy_premium", "pending", f"Premium {months} мес. для @{target_username}")

    token = load_fragment_token() or authenticate_fragment()
    if not token:
        update_balance(user_id, cost)
        mark_transaction_status(user_id, "buy_premium", "pending", "failed")
        add_transaction(user_id, cost, "refund", "success", "Возврат: ошибка авторизации Fragment")
        return jsonify({"error": "fragment_auth_failed", "balance": balance}), 502

    success, err_msg = send_premium(token, target_username, months)

    if not success:
        update_balance(user_id, cost)
        mark_transaction_status(user_id, "buy_premium", "pending", "failed")
        add_transaction(user_id, cost, "refund", "success", f"Возврат: {err_msg}")
        new_balance = get_user(user_id)[2]
        reason = classify_send_error(err_msg)
        notify_fragment_failure(reason, err_msg, f"Premium {months} мес. для @{target_username} (user {user_id})")
        return jsonify({"error": "send_failed", "reason": reason, "balance": new_balance}), 502

    mark_transaction_status(user_id, "buy_premium", "pending", "success")
    buyer_username = tg_user.get("username") or f"id{user_id}"
    _reward_referrer_if_first_purchase(user_id, buyer_username)

    order_id = get_next_order_id()
    recipient_display = "себе" if is_self else target_username
    _announce_order_in_channel(order_id, buyer_username)

    tokens_earned, token_balance = _award_cashback(user_id, cost, f"order:{order_id}", f"Premium {months} мес.")
    new_balance = get_user(user_id)[2]
    return jsonify({
        "ok": True,
        "tokens_earned": tokens_earned,
        "token_balance": token_balance,
        "months": months,
        "username": target_username,
        "balance": new_balance,
        "order_id": order_id,
        "product": f"Telegram Premium {months} мес.",
        "recipient": recipient_display,
        "cost": cost,
    })


# ==================== ТОКЕНЫ (внутренняя валюта) ====================

MYSTERY_PRIZE_TYPES = ("stars", "game_free", "game_discount", "nft", "premium")
MYSTERY_MANUAL_TYPES = ("nft", "premium")   # выдаёт админ вручную


def _mystery_prize_is_available(prize_type, payload):
    """Приз можно выдать (например, товар для game_free ещё существует в каталоге)."""
    if prize_type == "game_free":
        return bool(_find_any_game_product(payload.get("category"), payload.get("product_id")))
    return True


@app.route("/api/tokens")
@require_user
def api_tokens():
    user_id = request.tg_user["id"]
    ensure_user(request.tg_user)
    st = get_token_settings()
    history = [
        {"amount": a, "type": t, "description": d, "timestamp": ts}
        for a, t, d, ts in get_token_history(user_id, 30)
    ]
    return jsonify({
        "balance": get_token_balance(user_id),
        "cashback_percent": st["cashback_percent"],
        "box_price": st["box_price"],
        "box_available": bool(get_mystery_prizes(only_active=True)),
        "history": history,
    })


@app.route("/api/tokens/box/open", methods=["POST"])
@require_user
def api_tokens_box_open():
    tg_user = request.tg_user
    user_id = tg_user["id"]
    username = tg_user.get("username") or f"id{user_id}"
    ensure_user(tg_user)

    # Пул призов, которые реально можно выдать
    pool = []
    for pid, ptype, plabel, pweight, praw in get_mystery_prizes(only_active=True):
        try:
            payload = json.loads(praw) if praw else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = {}
        if ptype in MYSTERY_PRIZE_TYPES and _mystery_prize_is_available(ptype, payload):
            pool.append((pid, ptype, plabel, float(pweight), payload))
    if not pool:
        return jsonify({"error": "no_prizes"}), 400

    price = get_token_settings()["box_price"]
    if not spend_tokens(user_id, price, "box", "Тайный приз"):
        return jsonify({"error": "not_enough_tokens", "price": price, "token_balance": get_token_balance(user_id)}), 400

    try:
        prize_id, prize_type, prize_label, _w, payload = random.choices(pool, weights=[p[3] for p in pool], k=1)[0]

        if prize_type in MYSTERY_MANUAL_TYPES:
            category_label = "NFT" if prize_type == "nft" else "Telegram Premium"
            result_text = "Администратор скоро свяжется с вами для передачи приза."
            extra = {"icon": "nft" if prize_type == "nft" else "premium"}
            display_title = prize_label
            opening_id = add_mystery_opening(
                user_id, username, prize_id, prize_type, prize_label,
                f"{prize_label} — заявка передана администратору", price, status="pending_admin"
            )
            tg_notify.notify_admins(
                ADMIN_IDS,
                f"🎁 *Тайный приз: {prize_label}*\n\n"
                f"👤 Победитель: @{username}\n🆔 ID: `{user_id}`\n\n"
                f"Заявка №{opening_id}\n"
                f"Свяжитесь с победителем, выдайте приз и подтвердите в «Админ → 🎴 Карточки → Тайный приз»."
            )
        else:
            display_title, result_text, category_label, extra = _deliver_card_prize(
                user_id, username, prize_type, prize_label, payload
            )
            add_mystery_opening(
                user_id, username, prize_id, prize_type, prize_label,
                f"{display_title} — {result_text}", price, status="completed"
            )
    except Exception as e:
        logger.exception(f"Ошибка выдачи тайного приза user={user_id}: {e}")
        add_tokens(user_id, price, "box_refund", "Возврат: ошибка выдачи тайного приза",
                   ref=f"box_refund:{user_id}:{time.time_ns()}")
        return jsonify({"error": "open_failed", "token_balance": get_token_balance(user_id)}), 500

    return jsonify({
        "ok": True,
        "prize_type": prize_type,
        "prize_label": display_title,
        "category_label": category_label,
        "result_text": result_text,
        "extra": extra,
        "token_balance": get_token_balance(user_id),
    })


@app.route("/api/admin/tokens/settings", methods=["GET", "POST"])
@require_admin
def api_admin_tokens_settings():
    if request.method == "POST":
        data = request.get_json(force=True) or {}
        try:
            if "cashback_percent" in data:
                pct = float(data["cashback_percent"])
                if not 0 <= pct <= 50:
                    return jsonify({"error": "invalid_input"}), 400
                set_setting("token_cashback_percent", pct)
            if "box_price" in data:
                price = int(data["box_price"])
                if price < 1:
                    return jsonify({"error": "invalid_input"}), 400
                set_setting("token_box_price", price)
        except (TypeError, ValueError):
            return jsonify({"error": "invalid_input"}), 400
    return jsonify(get_token_settings())


# ---------- тайный приз: админка ----------

@app.route("/api/admin/mystery/prizes")
@require_admin
def api_admin_mystery_prizes():
    rows = get_mystery_prizes()
    prizes = [{"id": r[0], "type": r[1], "label": r[2], "weight": r[3], "payload": json.loads(r[4]) if r[4] else {}} for r in rows]
    return jsonify({"prizes": prizes, "box_price": get_token_settings()["box_price"]})


@app.route("/api/admin/mystery/prizes/save", methods=["POST"])
@require_admin
def api_admin_mystery_prize_save():
    data = request.get_json(force=True) or {}
    prize_type = data.get("type")
    label = (data.get("label") or "").strip()[:100]
    try:
        weight = float(data.get("weight", 0))
    except (TypeError, ValueError):
        weight = 0
    if prize_type not in MYSTERY_PRIZE_TYPES or not label or weight < 0:
        return jsonify({"error": "invalid_input"}), 400
    payload_json = json.dumps(data.get("payload") or {}, ensure_ascii=False)
    prize_id = data.get("id")
    if prize_id:
        update_mystery_prize(int(prize_id), label, weight, payload_json)
        return jsonify({"ok": True, "id": int(prize_id)})
    return jsonify({"ok": True, "id": add_mystery_prize(prize_type, label, weight, payload_json)})


@app.route("/api/admin/mystery/prizes/delete", methods=["POST"])
@require_admin
def api_admin_mystery_prize_delete():
    data = request.get_json(force=True) or {}
    try:
        prize_id = int(data.get("id"))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid_id"}), 400
    delete_mystery_prize(prize_id)
    return jsonify({"ok": True})


@app.route("/api/admin/mystery/pending")
@require_admin
def api_admin_mystery_pending():
    rows = get_pending_mystery_openings()
    return jsonify({"items": [
        {"id": r[0], "user_id": r[1], "username": r[2], "type": r[3], "label": r[4], "created_at": r[7]}
        for r in rows
    ]})


@app.route("/api/admin/mystery/confirm", methods=["POST"])
@require_admin
def api_admin_mystery_confirm():
    data = request.get_json(force=True) or {}
    try:
        opening_id = int(data.get("id"))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid_input"}), 400
    row = get_mystery_opening(opening_id)
    if not row:
        return jsonify({"error": "not_found"}), 404
    if not set_mystery_opening_status(opening_id, "completed", old_status="pending_admin"):
        return jsonify({"error": "already_decided"}), 400
    tg_notify.send_message(row[1], f"🎁 *Ваш тайный приз выдан!*\n\n🏆 {row[4]}\nСпасибо, что играете с нами! 🙌")
    return jsonify({"ok": True})


# ==================== BRAWL STARS (ручное исполнение) ====================
#
# Покупка проходит в 2 шага:
#  1) /api/games/brawlstars/buy — списывает баланс, создаёт заявку со статусом
#     'awaiting_email' и просит пользователя прислать почту от Supercell ID.
#  2) /api/games/order/<id>/email — сохраняет почту, переводит заявку в
#     'pending_admin' и шлёт уведомление админу. Пользователю показывается
#     сообщение, что админ свяжется с ним для получения кода.

def _find_brawl_product(product_id):
    return get_effective_product_price(BRAWL_STARS_PRODUCTS, product_id)


@app.route("/api/games/brawlstars/buy", methods=["POST"])
@require_user
def api_brawlstars_buy():
    tg_user = request.tg_user
    user_id = tg_user["id"]
    username = tg_user.get("username") or f"id{user_id}"
    data = request.get_json(force=True) or {}

    product_id = data.get("product_id")
    product = _find_brawl_product(product_id)
    if not product:
        return jsonify({"error": "invalid_product"}), 400

    db_user = ensure_user(tg_user)
    balance = db_user[2]
    cost, _discount_id = _apply_discount(user_id, "brawl_stars", product["price"], product_id)
    if balance < cost:
        return jsonify({"error": "not_enough_balance", "balance": balance, "cost": cost}), 400

    _fail = _charge_or_fail(user_id, cost, tg_user)
    if _fail:
        return _fail
    if _discount_id:
        mark_user_discount_used(_discount_id)
    add_transaction(user_id, -cost, "game_order", "pending", f"Brawl Stars: {product['name']}")

    order_id = create_game_order(
        user_id, username, "brawlstars", product["id"], product["name"], cost,
        status="awaiting_email"
    )

    new_balance = get_user(user_id)[2]
    return jsonify({
        "ok": True,
        "order_id": order_id,
        "product": product["name"],
        "cost": cost,
        "balance": new_balance,
    })


@app.route("/api/games/order/<int:order_id>/email", methods=["POST"])
@require_user
def api_game_order_email(order_id):
    tg_user = request.tg_user
    user_id = tg_user["id"]
    data = request.get_json(force=True) or {}

    email = (data.get("email") or "").strip()
    if not email or "@" not in email or "." not in email.split("@")[-1]:
        return jsonify({"error": "invalid_email"}), 400

    order = get_game_order(order_id)
    if not order:
        return jsonify({"error": "not_found"}), 404
    o_id, o_user_id, o_username, o_category, o_product_id, o_product_name, o_price, o_extra, o_status = order
    if o_user_id != user_id:
        return jsonify({"error": "forbidden"}), 403
    if o_status != "awaiting_email":
        return jsonify({"error": "invalid_status"}), 400

    set_game_order_extra(order_id, {"email": email}, status="pending_admin")
    mark_transaction_status(user_id, "game_order", "pending", "success")

    caption = (
        f"🟨 *Новая заявка Brawl Stars!*\n\n"
        f"👤 Пользователь: @{o_username}\n🆔 ID: `{o_user_id}`\n"
        f"🛍 Товар: *{o_product_name}*\n💰 Сумма: *{o_price:,.0f} сум*\n"
        f"📧 Email (Supercell ID): `{email}`\n\n"
        f"🧾 Заявка №{order_id}\n"
        f"Когда выполните заказ — нажмите «Выполнено» (или откройте «Админ → Игры»)."
    )
    tg_notify.notify_admins(ADMIN_IDS, caption, reply_markup={"inline_keyboard": [[
        {"text": "✅ Выполнено", "callback_data": f"game_ok:{order_id}"},
        {"text": "❌ Отменить", "callback_data": f"game_no:{order_id}"},
    ]]})

    return jsonify({"ok": True})


# ==================== ROBLOX (ручное исполнение через логин/пароль) ====================
#
# Покупка проходит в 2 шага, аналогично Brawl Stars:
#  1) /api/games/roblox/buy — списывает баланс, создаёт заявку со статусом
#     'awaiting_credentials' и просит логин+пароль аккаунта Roblox.
#  2) /api/games/order/<id>/credentials — сохраняет логин/пароль, переводит
#     заявку в 'pending_admin' и шлёт уведомление админу с этими данными.
#
# ⚠️ Логин и пароль хранятся в поле extra (JSON) заявки в открытом виде —
# так же, как email в Brawl Stars. Это осознанный выбор архитектуры проекта
# (ручное исполнение админом): ограничьте доступ к серверу/БД соответственно.

def _find_roblox_product(product_id):
    return get_effective_product_price(ROBLOX_PRODUCTS, product_id)


@app.route("/api/games/roblox/buy", methods=["POST"])
@require_user
def api_roblox_buy():
    tg_user = request.tg_user
    user_id = tg_user["id"]
    username = tg_user.get("username") or f"id{user_id}"
    data = request.get_json(force=True) or {}

    product_id = data.get("product_id")
    product = _find_roblox_product(product_id)
    if not product:
        return jsonify({"error": "invalid_product"}), 400

    db_user = ensure_user(tg_user)
    balance = db_user[2]
    cost, _discount_id = _apply_discount(user_id, "roblox", product["price"], product_id)
    if balance < cost:
        return jsonify({"error": "not_enough_balance", "balance": balance, "cost": cost}), 400

    _fail = _charge_or_fail(user_id, cost, tg_user)
    if _fail:
        return _fail
    if _discount_id:
        mark_user_discount_used(_discount_id)
    add_transaction(user_id, -cost, "game_order", "pending", f"Roblox: {product['name']}")

    order_id = create_game_order(
        user_id, username, "roblox", product["id"], product["name"], cost,
        status="awaiting_credentials"
    )

    new_balance = get_user(user_id)[2]
    return jsonify({
        "ok": True,
        "order_id": order_id,
        "product": product["name"],
        "cost": cost,
        "balance": new_balance,
    })


@app.route("/api/games/order/<int:order_id>/credentials", methods=["POST"])
@require_user
def api_game_order_credentials(order_id):
    tg_user = request.tg_user
    user_id = tg_user["id"]
    data = request.get_json(force=True) or {}

    login = (data.get("login") or "").strip()[:200]
    password = (data.get("password") or "").strip()[:200]
    if not login or not password:
        return jsonify({"error": "invalid_login"}), 400

    order = get_game_order(order_id)
    if not order:
        return jsonify({"error": "not_found"}), 404
    o_id, o_user_id, o_username, o_category, o_product_id, o_product_name, o_price, o_extra, o_status = order
    if o_user_id != user_id:
        return jsonify({"error": "forbidden"}), 403
    if o_status != "awaiting_credentials":
        return jsonify({"error": "invalid_status"}), 400

    set_game_order_extra(order_id, {"login": login, "password": password}, status="pending_admin")
    mark_transaction_status(user_id, "game_order", "pending", "success")

    caption = (
        f"🎮 *Новая заявка Roblox!*\n\n"
        f"👤 Пользователь: @{o_username}\n🆔 ID: `{o_user_id}`\n"
        f"🛍 Товар: *{o_product_name}*\n💰 Сумма: *{o_price:,.0f} сум*\n"
        f"👤 Логин: {md_escape(login)}\n🔑 Пароль: {md_escape(password)}\n\n"
        f"🧾 Заявка №{order_id}\n"
        f"Когда выполните заказ — нажмите «Выполнено» (или откройте «Админ → Игры»)."
    )
    tg_notify.notify_admins(ADMIN_IDS, caption, reply_markup={"inline_keyboard": [[
        {"text": "✅ Выполнено", "callback_data": f"game_ok:{order_id}"},
        {"text": "❌ Отменить", "callback_data": f"game_no:{order_id}"},
    ]]})

    return jsonify({"ok": True})


# ==================== STEAM: ПОПОЛНЕНИЕ БАЛАНСА АККАУНТА ====================
#
#  Пользователь вводит логин Steam и сумму в долларах, бот считает стоимость по курсу
#  (Админ → Цены → «Курс доллара»). После подтверждения деньги списываются с баланса,
#  создаётся заявка (game_orders, category='steam', статус 'pending_admin') и админам уходит
#  уведомление со всеми данными и кнопками «Выполнено / Отменить». Когда админ пополнит
#  аккаунт и подтвердит заказ, клиенту приходит сообщение об успешном пополнении.

import re as _re_steam


@app.route("/api/games/steam/buy", methods=["POST"])
@require_user
def api_steam_buy():
    tg_user = request.tg_user
    user_id = tg_user["id"]
    username = tg_user.get("username") or f"id{user_id}"
    data = request.get_json(force=True) or {}

    login = (data.get("login") or "").strip()
    if not _re_steam.match(r"^[A-Za-z0-9_.\-]{2,64}$", login):
        return jsonify({"error": "invalid_login"}), 400
    try:
        usd = round(float(data.get("usd")), 2)
    except (TypeError, ValueError):
        return jsonify({"error": "invalid_amount"}), 400
    if usd < STEAM_MIN_USD or usd > STEAM_MAX_USD:
        return jsonify({"error": "invalid_amount", "min": STEAM_MIN_USD, "max": STEAM_MAX_USD}), 400

    rate = get_effective_steam_rate()
    cost = round(usd * rate)

    db_user = ensure_user(tg_user)
    balance = db_user[2]
    if balance < cost:
        return jsonify({"error": "not_enough_balance", "balance": balance, "cost": cost}), 400

    usd_txt = f"{usd:g}"
    _fail = _charge_or_fail(user_id, cost, tg_user)
    if _fail:
        return _fail
    add_transaction(user_id, -cost, "game_order", "success", f"Steam: ${usd_txt} → {login}")
    order_id = create_game_order(
        user_id, username, "steam", "steam_topup", f"Пополнение Steam ${usd_txt}", cost,
        extra={"login": login, "usd": usd, "rate": rate}, status="pending_admin"
    )

    caption = (
        f"🎮 *Новая заявка: пополнение Steam!*\n\n"
        f"👤 Пользователь: @{md_escape(username)} · 🆔 `{user_id}`\n"
        f"🔑 Логин Steam: `{login}`\n"
        f"💵 Сумма пополнения: *${usd_txt}*\n"
        f"💱 Курс: $1 = {fmt_amount(rate)} сум\n"
        f"💰 К оплате: *{fmt_amount(cost)} сум*\n\n"
        f"🧾 Заявка №{order_id}"
    )
    keyboard = {"inline_keyboard": [[
        {"text": "✅ Выполнено", "callback_data": f"game_ok:{order_id}"},
        {"text": "❌ Отменить", "callback_data": f"game_no:{order_id}"},
    ]]}
    for admin_id in ADMIN_IDS:
        tg_notify.send_message(admin_id, caption, reply_markup=keyboard)

    return jsonify({
        "ok": True, "order_id": order_id, "login": login, "usd": usd, "rate": rate,
        "cost": cost, "balance": get_user(user_id)[2],
    })


# ==================== STANDOFF 2 GOLDA (продажа через скин на площадке) ====================
#
# Многошаговая сделка (все статусы живут в game_orders.status):
#  1) /api/games/standoff/buy — списывает баланс, создаёт заявку сразу со
#     статусом 'pending_admin' (доп. данные от пользователя не нужны, ход
#     сразу переходит к админу) и уведомляет админов суммой/количеством.
#  2) /api/admin/standoff/skin — админ вводит название скина и цену
#     выставления, заявка → 'awaiting_screenshot', пользователю уходит
#     инструкция.
#  3) /api/games/order/<id>/screenshot — пользователь прикрепляет скрин
#     выставленного скина, заявка → 'screenshot_review'.
#  4) /api/admin/standoff/screenshot/decide — админ подтверждает (заявка →
#     'in_progress', пользователю уходят обязательные правила сделки) или
#     отклоняет (заявка возвращается в 'awaiting_screenshot', просьба
#     переснять).
#  5) /api/admin/standoff/stage — админ продвигает сделку по этапам:
#     in_progress → bought → transferred → done, пользователь получает
#     уведомление на каждом этапе.
#  Отмена с возвратом средств возможна на любом активном этапе через уже
#  существующий /api/admin/game-orders/cancel (список статусов расширен ниже).

def _find_standoff_product(product_id):
    return get_effective_product_price(STANDOFF_GOLD_PRODUCTS, product_id)


@app.route("/api/games/standoff/buy", methods=["POST"])
@require_user
def api_standoff_buy():
    tg_user = request.tg_user
    user_id = tg_user["id"]
    username = tg_user.get("username") or f"id{user_id}"
    data = request.get_json(force=True) or {}

    product_id = data.get("product_id")
    product = _find_standoff_product(product_id)
    if not product:
        return jsonify({"error": "invalid_product"}), 400

    db_user = ensure_user(tg_user)
    balance = db_user[2]
    cost, _discount_id = _apply_discount(user_id, "standoff_gold", product["price"], product_id)
    if balance < cost:
        return jsonify({"error": "not_enough_balance", "balance": balance, "cost": cost}), 400

    _fail = _charge_or_fail(user_id, cost, tg_user)
    if _fail:
        return _fail
    if _discount_id:
        mark_user_discount_used(_discount_id)
    add_transaction(user_id, -cost, "game_order", "success", f"Standoff Golda: {product['name']}")

    order_id = create_game_order(
        user_id, username, "standoff", product["id"], product["name"], cost,
        status="pending_admin"
    )

    caption = (
        f"🔫 *Новая заявка Standoff Golda!*\n\n"
        f"👤 Пользователь: @{username}\n🆔 ID: `{user_id}`\n"
        f"💎 Количество: *{product['name']}*\n💰 Сумма: *{cost:,.0f} сум*\n\n"
        f"🧾 Заявка №{order_id}\n"
        f"Откройте раздел «Админ → Игры» в мини-аппе и укажите скин, который должен выставить покупатель."
    )
    tg_notify.notify_admins(ADMIN_IDS, caption, reply_markup={"inline_keyboard": [[
        {"text": "❌ Отменить заявку", "callback_data": f"game_no:{order_id}"},
    ]]})

    new_balance = get_user(user_id)[2]
    return jsonify({
        "ok": True,
        "order_id": order_id,
        "product": product["name"],
        "cost": cost,
        "balance": new_balance,
    })


@app.route("/api/admin/standoff/skin", methods=["POST"])
@require_admin
def api_admin_standoff_skin():
    data = request.get_json(force=True) or {}
    try:
        order_id = int(data.get("id"))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid_input"}), 400

    skin_name = (data.get("skin_name") or "").strip()[:200]
    try:
        skin_price = float(data.get("skin_price"))
    except (TypeError, ValueError):
        skin_price = None
    if not skin_name or not skin_price or skin_price <= 0:
        return jsonify({"error": "invalid_input"}), 400

    order = get_game_order(order_id)
    if not order:
        return jsonify({"error": "not_found"}), 404
    o_id, o_user_id, o_username, o_category, o_product_id, o_product_name, o_price, o_extra, o_status = order
    if o_category != "standoff" or o_status != "pending_admin":
        return jsonify({"error": "invalid_status"}), 400

    set_game_order_extra(order_id, {"skin_name": skin_name, "skin_price": skin_price}, status="awaiting_screenshot")

    tg_notify.send_message(
        o_user_id,
        f"✅ *Заявка №{order_id} принята в работу!*\n\n"
        f"Выставьте на продажу в Standoff 2 скин:\n"
        f"🎨 *{md_escape(skin_name)}*\n💰 Цена: *{skin_price:,.0f} SC*\n\n"
        f"После того как выставите — откройте вкладку «Заказы» в приложении, найдите эту заявку и "
        f"прикрепите скриншот страницы продажи."
    )
    return jsonify({"ok": True})


@app.route("/api/games/order/<int:order_id>/screenshot", methods=["POST"])
@require_user
def api_game_order_screenshot(order_id):
    tg_user = request.tg_user
    user_id = tg_user["id"]
    data = request.get_json(force=True) or {}

    photo_b64 = data.get("photo")
    if not photo_b64:
        return jsonify({"error": "missing_screenshot"}), 400

    order = get_game_order(order_id)
    if not order:
        return jsonify({"error": "not_found"}), 404
    o_id, o_user_id, o_username, o_category, o_product_id, o_product_name, o_price, o_extra, o_status = order
    if o_user_id != user_id:
        return jsonify({"error": "forbidden"}), 403
    if o_status != "awaiting_screenshot":
        return jsonify({"error": "invalid_status"}), 400

    try:
        extra = json.loads(o_extra) if o_extra else {}
    except (TypeError, ValueError):
        extra = {}
    extra["screenshot"] = photo_b64
    set_game_order_extra(order_id, extra, status="screenshot_review")

    caption = (
        f"📸 *Скриншот по заявке №{order_id}*\n\n"
        f"👤 @{o_username} · 🆔 `{o_user_id}`\n"
        f"🎨 Скин: *{md_escape(extra.get('skin_name', '—'))}*\n💰 Цена: *{float(extra.get('skin_price', 0)):,.0f} SC*\n\n"
        f"Проверьте скриншот и примите или отклоните его."
    )
    keyboard = {"inline_keyboard": [[
        {"text": "✅ Принять", "callback_data": f"so_ok:{order_id}"},
        {"text": "❌ Отклонить", "callback_data": f"so_no:{order_id}"},
    ]]}
    photo_bytes = b64_to_bytes(photo_b64)
    if photo_bytes:
        sent = tg_notify.notify_admins_photo(ADMIN_IDS, photo_bytes, caption=caption, reply_markup=keyboard)
        if not sent:
            tg_notify.notify_admins(ADMIN_IDS, caption, reply_markup=keyboard)
    else:
        tg_notify.notify_admins(ADMIN_IDS, caption, reply_markup=keyboard)

    return jsonify({"ok": True})


def standoff_screenshot_decide(order_id, action, admin_id):
    """Принимает (confirm) или отклоняет (reject) скриншот выставленного скина по заявке Standoff."""
    order = get_game_order(order_id)
    if not order:
        return False, "not_found"
    o_id, o_user_id, o_username, o_category, o_product_id, o_product_name, o_price, o_extra, o_status = order
    if o_category != "standoff" or o_status != "screenshot_review":
        return False, "invalid_status"
    try:
        extra = json.loads(o_extra) if o_extra else {}
    except (TypeError, ValueError):
        extra = {}

    if action == "confirm":
        set_game_order_status(order_id, "in_progress", admin_id)
        tg_notify.send_message(
            o_user_id,
            f"✅ *Заявка №{order_id} принята на обработку.*\n"
            f"В течение 24 часов ваша голда придёт на ваш аккаунт.\n\n"
            f"{STANDOFF_RULES_TEXT}"
        )
    else:
        extra.pop("screenshot", None)
        set_game_order_extra(order_id, extra, status="awaiting_screenshot")
        tg_notify.send_message(
            o_user_id,
            f"❌ *Скриншот по заявке №{order_id} отклонён.*\n\n"
            f"Проверьте, что скин *{md_escape(extra.get('skin_name', '—'))}* выставлен по цене "
            f"*{float(extra.get('skin_price', 0)):,.0f} SC*, и пришлите новый скриншот во вкладке «Заказы»."
        )
    return True, None


@app.route("/api/admin/standoff/screenshot/decide", methods=["POST"])
@require_admin
def api_admin_standoff_screenshot_decide():
    data = request.get_json(force=True) or {}
    order_id = _json_id(data)
    action = data.get("action")
    if order_id is None or action not in ("confirm", "reject"):
        return jsonify({"error": "invalid_input"}), 400
    return _admin_decision_response(standoff_screenshot_decide(order_id, action, request.tg_user["id"]))


@app.route("/api/admin/standoff/stage", methods=["POST"])
@require_admin
def api_admin_standoff_stage():
    data = request.get_json(force=True) or {}
    try:
        order_id = int(data.get("id"))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid_input"}), 400
    stage = data.get("stage")

    order = get_game_order(order_id)
    if not order:
        return jsonify({"error": "not_found"}), 404
    o_id, o_user_id, o_username, o_category, o_product_id, o_product_name, o_price, o_extra, o_status = order
    if o_category != "standoff":
        return jsonify({"error": "invalid_status"}), 400

    # Строгий порядок этапов — нельзя перепрыгнуть или отступить назад
    next_stage = {"in_progress": "bought", "bought": "transferred", "transferred": "done"}
    if next_stage.get(o_status) != stage:
        return jsonify({"error": "invalid_status"}), 400

    admin_id = request.tg_user["id"]
    set_game_order_status(order_id, stage, admin_id)

    messages = {
        "bought": f"🛒 *Заявка №{order_id}:* ваш скин выкуплен. Голда готовится к передаче.",
        "transferred": f"📦 *Заявка №{order_id}:* заявка передана поставщику голды. Ожидайте зачисления.",
        "done": f"✅ *Заявка №{order_id} выполнена!* 💎 {o_product_name} зачислены на ваш аккаунт. Спасибо за покупку! 🙌",
    }
    tg_notify.send_message(o_user_id, messages[stage])
    return jsonify({"ok": True})


# ==================== ЕЖЕДНЕВНЫЕ КАРТОЧКИ (бесплатный дневной бонус) ====================
#
# Раз в календарный день пользователь открывает одну из 3 карточек и получает
# случайный приз из пула, который настраивает админ (вес/шанс каждого приза,
# 0 = не выпадает). Никакой платной докупки карточек нет — рандом только в
# бесплатной части, поэтому это не азартная механика.
#
# Типы призов (payload — JSON):
#   stars         {"min": int, "max": int}               — реальные Telegram Stars,
#                                                            отправляются через Fragment API
#   game_free     {"category": "...", "product_id": "..."} — бесплатный игровой донат,
#                                                            заводится как обычная заявка
#   game_discount {"percent": float, "category": "any"|"brawl_stars"|"roblox"|"standoff_gold"}
#   nft           {}                                        — заявка админу, вручную выдаёт NFT

def _apply_discount(user_id, category, cost, product_id=None):
    """Если у пользователя есть неиспользованная скидка с ежедневной карточки
    (для этого товара, этой категории или общая), применяет её и возвращает (новая_цена, id_скидки)."""
    d = get_active_user_discount(user_id, category, product_id)
    if not d:
        return cost, None
    discount_id, percent = d[0], d[1]
    new_cost = round(cost * (1 - percent / 100), 2)
    return max(new_cost, 0), discount_id


def _find_any_game_product(category, product_id):
    if category == "brawl_stars":
        return _find_brawl_product(product_id)
    if category == "roblox":
        return _find_roblox_product(product_id)
    if category == "standoff_gold":
        return _find_standoff_product(product_id)
    return None


def _grant_free_game_order(user_id, username, category, product):
    """Заводит бесплатную (цена 0) игровую заявку — точно тот же флоу, что и
    у обычной покупки, просто без списания баланса."""
    cat_key = {"brawl_stars": "brawlstars", "roblox": "roblox", "standoff_gold": "standoff"}[category]
    if cat_key == "brawlstars":
        order_id = create_game_order(user_id, username, "brawlstars", product["id"], product["name"], 0, status="awaiting_email")
    elif cat_key == "roblox":
        order_id = create_game_order(user_id, username, "roblox", product["id"], product["name"], 0, status="awaiting_credentials")
    else:
        order_id = create_game_order(user_id, username, "standoff", product["id"], product["name"], 0, status="pending_admin")
        tg_notify.notify_admins(
            ADMIN_IDS,
            f"🎴 *Приз с ежедневной карточки: Standoff Golda!*\n\n"
            f"👤 Пользователь: @{username}\n🆔 ID: `{user_id}`\n"
            f"💎 Приз: *{product['name']}* (бесплатно, приз с карточки)\n\n"
            f"🧾 Заявка №{order_id}\n"
            f"Откройте раздел «Админ → Игры» и укажите скин, который должен выставить победитель."
        )
    return order_id, cat_key


# Ежедневная карточка: 1 бесплатное открытие в 24 часа (скользящее окно,
# а не календарный день). За EXTRA_OPENS_COST можно докупить ещё
# EXTRA_OPENS_GRANTED открытий, но саму докупку можно совершать не чаще
# раза в EXTRA_OPENS_COOLDOWN_HOURS часов.
DAILY_CARD_WINDOW_HOURS = 24
EXTRA_OPENS_COST = 5000
EXTRA_OPENS_GRANTED = 2
EXTRA_OPENS_COOLDOWN_HOURS = 24


def _window_cutoff(hours):
    return (datetime.datetime.now() - datetime.timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")


def _daily_card_limit(user_id):
    """Сколько раз пользователь может открыть ежедневную карточку за текущее
    24-часовое окно. None означает «без ограничений» (админы)."""
    if user_id in ADMIN_IDS:
        return None
    limit = 1
    last_purchase = get_last_extra_open_purchase(user_id)
    if last_purchase and last_purchase >= _window_cutoff(EXTRA_OPENS_COOLDOWN_HOURS):
        limit += EXTRA_OPENS_GRANTED
    return limit


def _extra_open_cooldown_seconds_left(user_id):
    """Сколько секунд осталось до следующей возможной докупки (0 — можно покупать сейчас)."""
    last_purchase = get_last_extra_open_purchase(user_id)
    if not last_purchase:
        return 0
    last_dt = datetime.datetime.strptime(last_purchase, "%Y-%m-%d %H:%M:%S")
    elapsed = datetime.datetime.now() - last_dt
    remaining = datetime.timedelta(hours=EXTRA_OPENS_COOLDOWN_HOURS) - elapsed
    return max(0, int(remaining.total_seconds()))


@app.route("/api/daily-card/status")
@require_user
def api_daily_card_status():
    tg_user = request.tg_user
    user_id = tg_user["id"]
    cutoff = _window_cutoff(DAILY_CARD_WINDOW_HOURS)
    last = get_last_daily_card_claim(user_id)
    limit = _daily_card_limit(user_id)
    claimed_recent = get_daily_card_claims_count_since(user_id, cutoff)
    already_claimed = (limit is not None) and (claimed_recent >= limit)
    return jsonify({
        "already_claimed": already_claimed,
        "result_text": last[1] if (already_claimed and last) else None,
        "claims_used": claimed_recent,
        "claims_limit": limit,
        "extra_open_cost": EXTRA_OPENS_COST,
        "extra_opens_granted": EXTRA_OPENS_GRANTED,
        "extra_open_cooldown_seconds_left": _extra_open_cooldown_seconds_left(user_id),
    })


@app.route("/api/daily-card/buy-extra", methods=["POST"])
@require_user
def api_daily_card_buy_extra():
    tg_user = request.tg_user
    user_id = tg_user["id"]

    cooldown_left = _extra_open_cooldown_seconds_left(user_id)
    if cooldown_left > 0:
        return jsonify({"error": "cooldown", "seconds_left": cooldown_left}), 400

    user = get_user(user_id)
    balance = user[2] if user else 0
    if balance < EXTRA_OPENS_COST:
        return jsonify({"error": "not_enough_balance", "balance": balance}), 400

    _fail = _charge_or_fail(user_id, EXTRA_OPENS_COST, tg_user)
    if _fail:
        return _fail
    add_extra_open_purchase(user_id)
    add_transaction(user_id, -EXTRA_OPENS_COST, "daily_card_extra", "success",
                     f"Докупка {EXTRA_OPENS_GRANTED} открытий ежедневной карточки")

    return jsonify({
        "ok": True,
        "balance": get_user(user_id)[2],
        "extra_opens_granted": EXTRA_OPENS_GRANTED,
    })


def _deliver_card_prize(user_id, username, prize_type, prize_label, payload):
    """Выдаёт приз автоматических типов (stars / game_free / game_discount).
    Общая логика для ежедневной карточки и тайного приза.
    Возвращает (display_title, result_text, category_label, extra)."""
    result_text = prize_label
    display_title = prize_label
    extra = {}
    category_label = "Приз"
    CAT_TO_ORDER_KEY = {"brawl_stars": "brawlstars", "roblox": "roblox", "standoff_gold": "standoff"}

    if prize_type == "stars":
        qty = random.randint(int(payload.get("min", 1)), int(payload.get("max", 1)))
        update_star_balance(user_id, qty)
        display_title = f"{qty} Stars"
        result_text = "Зачислены на звёздный баланс"
        category_label = "Звёзды"
        extra = {"star_balance": get_star_balance(user_id), "icon": "stars"}

    elif prize_type == "game_free":
        category = payload.get("category")
        product = _find_any_game_product(category, payload.get("product_id"))
        category_label = "Донат"
        if not product:
            display_title = "Приз недоступен"
            result_text = "Товар удалён, обратитесь в поддержку"
            extra = {"icon": "game"}
        else:
            order_id, cat_key = _grant_free_game_order(user_id, username, category, product)
            extra = {"redirect": {"category": cat_key, "order_id": order_id, "product_name": product["name"]}, "icon": "game", "icon_category": cat_key}
            display_title = product["name"]
            result_text = "Бесплатно! Осталось указать данные аккаунта."

    elif prize_type == "game_discount":
        percent = float(payload.get("percent", 0))
        category = payload.get("category", "any")
        product_id = payload.get("product_id", "all")
        product_name = payload.get("product_name", "")
        try:
            duration_days = int(payload.get("duration_days", 7))
        except (TypeError, ValueError):
            duration_days = 7
        if duration_days <= 0:
            duration_days = 7

        expires_at = (datetime.datetime.now() + datetime.timedelta(days=duration_days)).strftime("%Y-%m-%d %H:%M:%S")

        cat_meta = {
            "brawl_stars": {"name": "Brawl Stars", "icon": "/tab-brawlstars.png", "order_key": "brawlstars"},
            "roblox": {"name": "Roblox", "icon": "/tab-roblox.png", "order_key": "roblox"},
            "standoff_gold": {"name": "Standoff 2", "icon": "/tab-standoff.png", "order_key": "standoff"},
            "any": {"name": "Любая игра", "icon": "/cards-icon.png", "order_key": "brawlstars"}
        }
        cm = cat_meta.get(category, {"name": category, "icon": "/cards-icon.png", "order_key": None})
        game_name = cm["name"]
        game_icon = cm["icon"]

        target_product_name = "Все товары"
        target_product_icon = None
        if product_id and product_id != "all":
            prod = _find_any_game_product(category, product_id)
            if prod:
                target_product_name = prod.get("name", product_id)
                target_product_icon = prod.get("icon")
            elif product_name:
                target_product_name = product_name
        else:
            product_id = "all"
            target_product_name = f"Все товары {game_name}" if category != "any" else "Все товары всех игр"

        add_user_discount(user_id, percent, category, product_id, target_product_name, expires_at)

        display_title = f"Скидка {percent:.0f}%"
        result_text = f"{game_name} · {target_product_name} · ⏳ {duration_days} дн."
        category_label = "Скидка"
        extra = {
            "icon": "discount",
            "icon_category": cm.get("order_key"),
            "category": category,
            "game_name": game_name,
            "game_icon": game_icon,
            "product_id": product_id,
            "product_name": target_product_name,
            "product_icon": target_product_icon,
            "percent": percent,
            "duration_days": duration_days,
            "expires_at": expires_at,
        }

    return display_title, result_text, category_label, extra


@app.route("/api/daily-card/claim", methods=["POST"])
@require_user
def api_daily_card_claim():
    tg_user = request.tg_user
    user_id = tg_user["id"]
    username = tg_user.get("username") or f"id{user_id}"
    today = datetime.date.today().isoformat()

    cutoff = _window_cutoff(DAILY_CARD_WINDOW_HOURS)
    limit = _daily_card_limit(user_id)
    claimed_recent = get_daily_card_claims_count_since(user_id, cutoff)
    if limit is not None and claimed_recent >= limit:
        last = get_last_daily_card_claim(user_id)
        return jsonify({"error": "already_claimed", "result_text": last[1] if last else None}), 400

    prizes = [p for p in get_daily_card_prizes(only_active=True) if p[3] > 0]
    if not prizes:
        return jsonify({"error": "no_prizes"}), 400

    weights = [p[3] for p in prizes]
    prize = random.choices(prizes, weights=weights, k=1)[0]
    prize_id, prize_type, prize_label, weight, payload_raw = prize
    try:
        payload = json.loads(payload_raw) if payload_raw else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        payload = {}

    if prize_type != "nft":
        display_title, result_text, category_label, extra = _deliver_card_prize(
            user_id, username, prize_type, prize_label, payload
        )
    else:
        claim_pending_id = add_daily_card_claim(user_id, username, today, prize_id, prize_type, prize_label, "NFT — заявка передана администратору", status="pending_admin")
        tg_notify.notify_admins(
            ADMIN_IDS,
            f"🎴 *Приз с ежедневной карточки: NFT!*\n\n"
            f"👤 Победитель: @{username}\n🆔 ID: `{user_id}`\n\n"
            f"Заявка №{claim_pending_id}\n"
            f"Свяжитесь с победителем и передайте NFT, затем подтвердите в «Админ → 🎴 Карточки»."
        )
        result_text = "Администратор скоро свяжется с вами для передачи приза."
        return jsonify({
            "ok": True, "prize_type": prize_type, "prize_label": prize_label,
            "category_label": "NFT", "result_text": result_text, "extra": {"icon": "nft"},
        })

    stored_result_text = f"{display_title} — {result_text}"
    add_daily_card_claim(user_id, username, today, prize_id, prize_type, prize_label, stored_result_text, status="completed")
    return jsonify({
        "ok": True, "prize_type": prize_type, "prize_label": display_title,
        "category_label": category_label, "result_text": result_text, "extra": extra,
    })


@app.route("/api/admin/daily-card/prizes")
@require_admin
def api_admin_daily_card_prizes():
    rows = get_daily_card_prizes()
    prizes = [{"id": r[0], "type": r[1], "label": r[2], "weight": r[3], "payload": json.loads(r[4]) if r[4] else {}} for r in rows]
    return jsonify({"prizes": prizes})


@app.route("/api/admin/daily-card/prizes/save", methods=["POST"])
@require_admin
def api_admin_daily_card_prize_save():
    data = request.get_json(force=True) or {}
    prize_type = data.get("type")
    label = (data.get("label") or "").strip()[:100]
    try:
        weight = float(data.get("weight", 0))
    except (TypeError, ValueError):
        weight = 0
    if prize_type not in ("stars", "game_free", "game_discount", "nft") or not label or weight < 0:
        return jsonify({"error": "invalid_input"}), 400

    payload = data.get("payload") or {}
    payload_json = json.dumps(payload, ensure_ascii=False)

    prize_id = data.get("id")
    if prize_id:
        update_daily_card_prize(int(prize_id), label, weight, payload_json)
        return jsonify({"ok": True, "id": int(prize_id)})
    new_id = add_daily_card_prize(prize_type, label, weight, payload_json)
    return jsonify({"ok": True, "id": new_id})


@app.route("/api/admin/daily-card/prizes/delete", methods=["POST"])
@require_admin
def api_admin_daily_card_prize_delete():
    data = request.get_json(force=True) or {}
    try:
        prize_id = int(data.get("id"))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid_id"}), 400
    delete_daily_card_prize(prize_id)
    return jsonify({"ok": True})


@app.route("/api/admin/daily-card/claims")
@require_admin
def api_admin_daily_card_claims():
    q = (request.args.get("q") or "").strip()
    rows = get_recent_daily_card_claims(limit=100, q=q or None)
    claims = [{
        "id": r[0], "user_id": r[1], "username": r[2], "prize_type": r[3],
        "prize_label": r[4], "result_text": r[5], "status": r[6], "created_at": r[7],
    } for r in rows]
    return jsonify({"claims": claims, "stats": get_daily_card_claims_stats_today()})


@app.route("/api/admin/daily-card/nft-pending")
@require_admin
def api_admin_daily_card_nft_pending():
    rows = get_pending_nft_card_claims()
    claims = [{"id": r[0], "user_id": r[1], "username": r[2], "label": r[3], "created_at": r[4]} for r in rows]
    return jsonify({"claims": claims})


@app.route("/api/admin/daily-card/nft-confirm", methods=["POST"])
@require_admin
def api_admin_daily_card_nft_confirm():
    data = request.get_json(force=True) or {}
    try:
        claim_id = int(data.get("id"))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid_id"}), 400

    claim = get_daily_card_claim_by_id(claim_id)
    if not claim or claim[3] != "nft" or claim[5] != "pending_admin":
        return jsonify({"error": "not_found"}), 404

    set_daily_card_claim_status(claim_id, "completed")
    buyer_id = claim[1]
    tg_notify.send_message(buyer_id, "🖼 *Ваш NFT-приз с ежедневной карточки отправлен!* Спасибо за участие 🎉")
    return jsonify({"ok": True})


@app.route("/api/daily-card/star-balance")
@require_user
def api_daily_card_star_balance():
    user_id = request.tg_user["id"]
    return jsonify({"star_balance": get_star_balance(user_id), "min_withdrawal": MIN_STAR_WITHDRAWAL})


MIN_STAR_WITHDRAWAL = 50


@app.route("/api/daily-card/withdraw-stars", methods=["POST"])
@require_user
def api_daily_card_withdraw_stars():
    """Вывод накопленных звёзд (с карточек и тайного приза) на аккаунт пользователя через Fragment.

    Звёзды списываются ДО отправки (атомарно) и возвращаются, если отправка не удалась:
    так двойной тап не отправит звёзды дважды, а при сбое Fragment баланс не теряется.
    Причина неудачи пишется в лог и уходит админам, чтобы было видно, почему у пользователя не вышло."""
    tg_user = request.tg_user
    user_id = tg_user["id"]
    tg_username = tg_user.get("username")

    if not tg_username:
        return jsonify({"error": "no_username"}), 400

    qty = get_star_balance(user_id)
    if qty <= 0:
        # кнопка «Вывести» в этом случае неактивна — повторные запросы считаем попыткой обхода
        security_alert("withdraw_abuse", user_id, tg_username, "Вывод звёзд при пустом звёздном балансе", min_events=2)
        return jsonify({"error": "empty_balance"}), 400
    if qty < MIN_STAR_WITHDRAWAL:
        security_alert("withdraw_abuse", user_id, tg_username,
                       f"Вывод звёзд ниже минимума: {qty} < {MIN_STAR_WITHDRAWAL}", min_events=2)
        return jsonify({"error": "min_withdrawal", "min_withdrawal": MIN_STAR_WITHDRAWAL, "star_balance": qty}), 400

    if not try_deduct_star_balance(user_id, qty):
        security_alert("double_spend", user_id, tg_username,
                       f"Вывод {qty} звёзд: параллельные запросы на один звёздный баланс", min_events=2)
        return jsonify({"error": "empty_balance"}), 400

    sent, err = False, "неизвестная ошибка"
    try:
        token = load_fragment_token() or authenticate_fragment()
        if token:
            sent, err = send_stars(token, tg_username, qty)
        else:
            err = "не удалось авторизоваться в Fragment"
    except Exception as e:
        logger.error(f"Вывод звёзд: исключение при отправке user={user_id}: {e}")
        err = str(e)

    if not sent:
        update_star_balance(user_id, qty)  # возвращаем звёзды на баланс
        err_short = str(err or "")[:300]
        logger.error(f"Вывод звёзд не удался: user={user_id} @{tg_username} qty={qty} err={err_short}")
        add_transaction(user_id, 0, "star_withdrawal", "failed", f"Вывод {qty} Stars на @{tg_username} не удался: {err_short}")
        tg_notify.notify_admins(
            ADMIN_IDS,
            f"⚠️ *Не удался вывод звёзд*\n\n👤 @{md_escape(tg_username)} · 🆔 `{user_id}`\n"
            f"⭐ Сумма: *{qty} Stars* (возвращены на звёздный баланс)\n"
            f"❗ Ответ Fragment: {md_escape(err_short)}"
        )
        return jsonify({"error": "delivery_failed"}), 502

    add_transaction(user_id, 0, "star_withdrawal", "success", f"Вывод {qty} Stars на @{tg_username}")
    return jsonify({"ok": True, "sent": qty, "star_balance": get_star_balance(user_id)})


# ==================== МОИ ИГРОВЫЕ ЗАЯВКИ (для вкладки «Заказы» в Mini App) ====================

@app.route("/api/games/my-orders")
@require_user
def api_games_my_orders():
    user_id = request.tg_user["id"]
    rows = get_user_game_orders(user_id)
    orders = []
    for r in rows:
        o_id, o_category, o_product_id, o_product_name, o_price, o_extra, o_status, o_created_at, o_updated_at = r
        try:
            extra = json.loads(o_extra) if o_extra else {}
        except (TypeError, ValueError):
            extra = {}
        extra.pop("screenshot", None)  # скрин не нужен в списке заказов
        extra.pop("password", None)    # пароль пользователю показывать повторно незачем
        orders.append({
            "id": o_id, "category": o_category, "product_id": o_product_id,
            "product_name": o_product_name, "price": o_price, "extra": extra,
            "status": o_status, "created_at": o_created_at, "updated_at": o_updated_at,
        })
    return jsonify({"orders": orders})


# ==================== РАССЫЛКА (админ пишет всем, кто нажимал /start) ====================

_broadcast_lock = threading.Lock()
_broadcast_state = {"running": False, "total": 0, "sent": 0, "failed": 0}


def _broadcast_worker(text, user_ids):
    global _broadcast_state
    sent, failed = 0, 0
    for uid in user_ids:
        ok = tg_notify.send_message(uid, text)
        if ok:
            sent += 1
        else:
            failed += 1
        with _broadcast_lock:
            _broadcast_state["sent"] = sent
            _broadcast_state["failed"] = failed
        # Telegram Bot API допускает ~30 сообщений/сек — держим запас, чтобы не словить 429
        time.sleep(0.05)
    with _broadcast_lock:
        _broadcast_state["running"] = False
    logger.info(f"📢 Рассылка завершена: доставлено {sent}, не удалось {failed} из {len(user_ids)}")


@app.route("/api/admin/broadcast", methods=["POST"])
@require_admin
def api_admin_broadcast():
    with _broadcast_lock:
        if _broadcast_state["running"]:
            return jsonify({"error": "already_running"}), 400

    data = request.get_json(force=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "empty_text"}), 400
    if len(text) > 4000:
        return jsonify({"error": "text_too_long"}), 400

    user_ids = get_all_user_ids()
    if not user_ids:
        return jsonify({"error": "no_users"}), 400

    with _broadcast_lock:
        _broadcast_state.update({"running": True, "total": len(user_ids), "sent": 0, "failed": 0})

    threading.Thread(target=_broadcast_worker, args=(text, user_ids), daemon=True).start()
    return jsonify({"ok": True, "recipients": len(user_ids)})


@app.route("/api/admin/broadcast/status")
@require_admin
def api_admin_broadcast_status():
    with _broadcast_lock:
        return jsonify(dict(_broadcast_state))


# ==================== NFT-МАРКЕТПЛЕЙС ====================
#
# Поток продажи (NFT-подарки Telegram — физически не хранятся у нас, только
# заявка + фото/скрин для проверки):
#
#  1) Продавец создаёт заявку /api/nft/list (название, цена, скрин NFT) →
#     статус 'pending_admin', админ уведомлён.
#  2) Админ одобряет заявку → статус 'awaiting_transfer' → продавцу приходит
#     инструкция отправить сам NFT-подарок аккаунту NFT_ADMIN_USERNAME.
#  3) Продавец жмёт «Я отправил» /api/nft/order/<id>/confirm-sent →
#     статус 'verification' → админ уведомлён, что NFT прислали, нужно сверить.
#  4) Админ подтверждает получение → статус 'active' → объявление появляется
#     на маркете для всех пользователей.
#  5) Покупатель покупает /api/nft/buy → сумма списывается с покупателя,
#     комиссия маркетплейса удерживается, остаток зачисляется продавцу на
#     баланс → статус 'sold' → админ уведомлён, что нужно переслать NFT
#     покупателю (сам NFT остался у админа с шага 3).
#
# На любом промежуточном шаге админ может отклонить заявку — статус 'cancelled'.

def _find_nft_listing_row(listing_id):
    return get_nft_listing(listing_id)


@app.route("/api/nft/list", methods=["POST"])
@require_user
def api_nft_list():
    tg_user = request.tg_user
    user_id = tg_user["id"]
    username = tg_user.get("username") or f"id{user_id}"
    data = request.get_json(force=True) or {}

    nft_name = (data.get("nft_name") or "").strip()[:200]
    photo_b64 = data.get("photo")
    try:
        price = float(data.get("price"))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid_price"}), 400

    if not nft_name or price <= 0:
        return jsonify({"error": "invalid_input"}), 400
    if not photo_b64:
        return jsonify({"error": "missing_photo"}), 400

    listing_id = create_nft_listing(user_id, username, nft_name, photo_b64, price)

    # Заявка уходит админам сразу, с кнопками «Одобрить / Отклонить» прямо в чате.
    # username экранируем: символ «_» в нём раньше ломал Markdown, и уведомление терялось.
    caption = (
        f"🖼 *Новая заявка на публикацию NFT!*\n\n"
        f"👤 Продавец: @{md_escape(username)} · 🆔 `{user_id}`\n"
        f"🏷 Название: *{md_escape(nft_name)}*\n💰 Цена: *{fmt_amount(price)} сум*\n\n"
        f"🧾 Заявка №{listing_id}"
    )
    keyboard = {"inline_keyboard": [[
        {"text": "✅ Одобрить", "callback_data": f"nft_ok:{listing_id}"},
        {"text": "❌ Отклонить", "callback_data": f"nft_no:{listing_id}"},
    ]]}
    photo_bytes = b64_to_bytes(photo_b64)
    for admin_id in ADMIN_IDS:
        sent = False
        if photo_bytes:
            sent = tg_notify.send_photo_bytes(admin_id, photo_bytes, caption=caption, reply_markup=keyboard)
        if not sent:
            tg_notify.send_message(admin_id, caption, reply_markup=keyboard)

    return jsonify({"ok": True, "listing_id": listing_id})


@app.route("/api/nft/order/<int:listing_id>/confirm-sent", methods=["POST"])
@require_user
def api_nft_confirm_sent(listing_id):
    user_id = request.tg_user["id"]
    listing = get_nft_listing(listing_id)
    if not listing:
        return jsonify({"error": "not_found"}), 404
    l_id, seller_id, seller_username, nft_name, nft_photo, price, status, buyer_id, buyer_username, created_at = listing
    if seller_id != user_id:
        return jsonify({"error": "forbidden"}), 403
    if status != "awaiting_transfer":
        return jsonify({"error": "invalid_status"}), 400

    set_nft_listing_status(listing_id, "verification")
    tg_notify.notify_admins(
        ADMIN_IDS,
        f"📬 *Продавец сообщил, что отправил NFT!*\n\n"
        f"👤 @{seller_username} · 🧾 Заявка №{listing_id}\n🏷 {md_escape(nft_name)}\n\n"
        f"Проверьте, пришёл ли подарок на @{NFT_ADMIN_USERNAME}, затем подтвердите публикацию."
        ,
        reply_markup={"inline_keyboard": [[
            {"text": "✅ Опубликовать", "callback_data": f"nv_ok:{listing_id}"},
            {"text": "❌ Не пришёл", "callback_data": f"nv_no:{listing_id}"},
        ]]}
    )
    return jsonify({"ok": True})


@app.route("/api/nft/my")
@require_user
def api_nft_my():
    user_id = request.tg_user["id"]
    rows = get_user_nft_listings(user_id)
    listings = [{
        "id": r[0], "nft_name": r[1], "price": r[3], "status": r[4],
        "buyer_username": r[5], "created_at": r[6], "updated_at": r[7],
    } for r in rows]
    return jsonify({"listings": listings})


@app.route("/api/nft/market")
@require_user
def api_nft_market():
    user_id = request.tg_user["id"]
    rows = get_active_nft_listings()
    listings = [{
        "id": r[0], "seller_id": r[1], "seller_username": r[2], "nft_name": r[3],
        "nft_photo": compress_photo_thumbnail(r[4]), "price": r[5], "created_at": r[6],
        "is_own": r[1] == user_id,
    } for r in rows]
    resp = jsonify({"listings": listings})
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/api/nft/buy", methods=["POST"])
@require_user
def api_nft_buy():
    tg_user = request.tg_user
    buyer_id = tg_user["id"]
    buyer_username = tg_user.get("username") or f"id{buyer_id}"
    data = request.get_json(force=True) or {}

    try:
        listing_id = int(data.get("listing_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid_input"}), 400

    listing = get_nft_listing(listing_id)
    if not listing:
        return jsonify({"error": "not_found"}), 404
    l_id, seller_id, seller_username, nft_name, nft_photo, price, status, _, _, created_at = listing
    if status != "active":
        return jsonify({"error": "not_available"}), 400
    if seller_id == buyer_id:
        return jsonify({"error": "own_listing"}), 400

    buyer = ensure_user(tg_user)
    balance = buyer[2]
    if balance < price:
        return jsonify({"error": "not_enough_balance", "balance": balance, "cost": price}), 400

    commission_pct = get_effective_nft_commission()
    commission = round(price * commission_pct / 100, 2)
    seller_payout = round(price - commission, 2)

    _fail = _charge_or_fail(buyer_id, price, tg_user)
    if _fail:
        return _fail
    # Атомарно «занимаем» лот: если два покупателя нажали одновременно, второй получит деньги обратно
    if not try_mark_nft_sold(listing_id, buyer_id, buyer_username):
        update_balance(buyer_id, price)
        return jsonify({"error": "not_available"}), 400
    add_transaction(buyer_id, -price, "nft_buy", "success", f"Покупка NFT «{nft_name}»")
    update_balance(seller_id, seller_payout)
    add_transaction(seller_id, seller_payout, "nft_sale", "success", f"Продажа NFT «{nft_name}» (комиссия {commission_pct}%)")

    safe_name = md_escape(nft_name)
    tg_notify.send_message(
        seller_id,
        f"🎉 *Ваш NFT «{safe_name}» куплен!*\n\n"
        f"Покупатель: @{buyer_username}\n"
        f"К зачислению: *{fmt_amount(seller_payout)} сум* (комиссия {commission_pct}%: {fmt_amount(commission)} сум) — уже на балансе.\n\n"
        f"Администратор передаст NFT покупателю."
    )
    tg_notify.send_message(
        buyer_id,
        f"✅ *Покупка оформлена!*\n\n"
        f"🖼 {safe_name} · {fmt_amount(price)} сум\n\n"
        f"Администратор свяжется с вами и передаст NFT в ближайшее время."
    )
    tg_notify.notify_admins(
        ADMIN_IDS,
        f"💸 *NFT продан — нужно передать покупателю!*\n\n"
        f"🧾 Заявка №{listing_id} · 🏷 {safe_name}\n"
        f"Продавец: @{seller_username}\nПокупатель: @{buyer_username} · 🆔 `{buyer_id}`\n\n"
        f"NFT сейчас должен быть у @{NFT_ADMIN_USERNAME} — перешлите его покупателю."
    )

    new_balance = get_user(buyer_id)[2]
    return jsonify({"ok": True, "balance": new_balance})


@app.route("/api/nft/my-purchases")
@require_user
def api_nft_my_purchases():
    user_id = request.tg_user["id"]
    rows = get_user_nft_purchases(user_id)
    purchases = [{
        "id": r[0], "nft_name": r[1], "price": r[3], "seller_username": r[4], "updated_at": r[5],
    } for r in rows]
    return jsonify({"purchases": purchases})


# ---- Админ: модерация NFT-заявок ----

@app.route("/api/nft/my/<int:listing_id>/cancel", methods=["POST"])
@require_user
def api_nft_cancel_own(listing_id):
    """Продавец сам снимает своё объявление (на любом этапе, включая опубликованное на маркете)."""
    user_id = request.tg_user["id"]
    listing = get_nft_listing(listing_id)
    if not listing:
        return jsonify({"error": "not_found"}), 404
    l_id, seller_id, seller_username, nft_name, nft_photo, price, status, buyer_id, buyer_username, created_at = listing
    if seller_id != user_id:
        return jsonify({"error": "forbidden"}), 403
    if status not in ("pending_admin", "awaiting_transfer", "verification", "active"):
        return jsonify({"error": "invalid_status"}), 400

    set_nft_listing_status(listing_id, "cancelled", user_id)
    safe_name = md_escape(nft_name)
    warn = ""
    if status in ("awaiting_transfer", "verification", "active"):
        warn = f"\n\n⚠️ NFT мог уже быть у @{NFT_ADMIN_USERNAME} — свяжитесь с продавцом @{seller_username} по поводу возврата подарка."
    tg_notify.notify_admins(
        ADMIN_IDS,
        f"🗑 *Продавец снял объявление с продажи!*\n\n👤 @{seller_username} · 🧾 Заявка №{listing_id}\n🏷 {safe_name}{warn}"
    )
    return jsonify({"ok": True})


@app.route("/api/admin/nft/pending")
@require_admin
def api_admin_nft_pending():
    rows = get_pending_nft_listings()
    listings = [{
        "id": r[0], "seller_id": r[1], "seller_username": r[2], "nft_name": r[3],
        "nft_photo": r[4], "price": r[5], "status": r[6], "created_at": r[7],
    } for r in rows]
    return jsonify({"listings": listings})


def nft_admin_approve(listing_id, admin_id):
    """Одобряет заявку на публикацию NFT. Общая логика для кнопки в мини-аппе
    и для инлайн-кнопки «Одобрить» в уведомлении бота. Возвращает (ok, error_code)."""
    listing = get_nft_listing(listing_id)
    if not listing:
        return False, "not_found"
    l_id, seller_id, seller_username, nft_name, nft_photo, price, status, *_ = listing
    if status != "pending_admin":
        return False, "invalid_status"

    set_nft_listing_status(listing_id, "awaiting_transfer", admin_id)
    tg_notify.send_message(
        seller_id,
        f"✅ *Заявка №{listing_id} одобрена!*\n\n"
        f"Отправьте NFT-подарок «{md_escape(nft_name)}» аккаунту @{NFT_ADMIN_USERNAME} в Telegram "
        f"(долгое нажатие на подарок → Переслать/Send).\n\n"
        f"После отправки вернитесь в мини-апп (вкладка «Заказы» или «Мои NFT») и нажмите «Я отправил NFT»."
    )
    return True, None


def nft_admin_reject(listing_id, admin_id):
    """Отклоняет заявку на публикацию NFT (мини-апп и инлайн-кнопка в боте). Возвращает (ok, error_code)."""
    listing = get_nft_listing(listing_id)
    if not listing:
        return False, "not_found"
    l_id, seller_id, seller_username, nft_name, nft_photo, price, status, *_ = listing
    if status not in ("pending_admin", "awaiting_transfer", "verification"):
        return False, "invalid_status"

    set_nft_listing_status(listing_id, "cancelled", admin_id)
    tg_notify.send_message(seller_id, f"❌ Заявка №{listing_id} на публикацию «{md_escape(nft_name)}» отклонена администратором.")
    return True, None


@app.route("/api/admin/nft/approve", methods=["POST"])
@require_admin
def api_admin_nft_approve():
    data = request.get_json(force=True) or {}
    try:
        listing_id = int(data.get("id"))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid_input"}), 400
    ok, err = nft_admin_approve(listing_id, request.tg_user["id"])
    if not ok:
        return jsonify({"error": err}), 404 if err == "not_found" else 400
    return jsonify({"ok": True})


@app.route("/api/admin/nft/reject", methods=["POST"])
@require_admin
def api_admin_nft_reject():
    data = request.get_json(force=True) or {}
    try:
        listing_id = int(data.get("id"))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid_input"}), 400
    ok, err = nft_admin_reject(listing_id, request.tg_user["id"])
    if not ok:
        return jsonify({"error": err}), 404 if err == "not_found" else 400
    return jsonify({"ok": True})


def nft_admin_verify(listing_id, action, admin_id):
    """Проверка передачи NFT: confirm — публикует объявление, reject — просит продавца отправить подарок ещё раз."""
    listing = get_nft_listing(listing_id)
    if not listing:
        return False, "not_found"
    l_id, seller_id, seller_username, nft_name, nft_photo, price, status, *_ = listing
    nft_name = md_escape(nft_name)
    if status != "verification":
        return False, "invalid_status"

    if action == "confirm":
        set_nft_listing_status(listing_id, "active", admin_id)
        tg_notify.send_message(
            seller_id,
            f"🎉 *NFT «{nft_name}» опубликован на маркете!*\nЦена: {fmt_amount(price)} сум\n\n"
            f"Вы получите уведомление, как только его купят."
        )
    else:
        set_nft_listing_status(listing_id, "awaiting_transfer", admin_id)
        tg_notify.send_message(
            seller_id,
            f"⚠️ Мы не нашли NFT «{nft_name}» (заявка №{listing_id}) у @{NFT_ADMIN_USERNAME}. "
            f"Проверьте, что отправили именно этот подарок, и нажмите «Я отправил NFT» ещё раз."
        )
    return True, None


@app.route("/api/admin/nft/verify", methods=["POST"])
@require_admin
def api_admin_nft_verify():
    data = request.get_json(force=True) or {}
    listing_id = _json_id(data)
    action = data.get("action")
    if listing_id is None or action not in ("confirm", "reject"):
        return jsonify({"error": "invalid_input"}), 400
    return _admin_decision_response(nft_admin_verify(listing_id, action, request.tg_user["id"]))


@app.route("/api/admin/nft/active")
@require_admin
def api_admin_nft_active():
    """Все объявления, уже опубликованные на маркете — для удаления администратором."""
    rows = get_active_nft_listings()
    listings = [{
        "id": r[0], "seller_id": r[1], "seller_username": r[2], "nft_name": r[3],
        "nft_photo": compress_photo_thumbnail(r[4]), "price": r[5], "created_at": r[6],
    } for r in rows]
    return jsonify({"listings": listings})


@app.route("/api/admin/nft/unpublish", methods=["POST"])
@require_admin
def api_admin_nft_unpublish():
    data = request.get_json(force=True) or {}
    try:
        listing_id = int(data.get("id"))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid_input"}), 400

    listing = get_nft_listing(listing_id)
    if not listing:
        return jsonify({"error": "not_found"}), 404
    l_id, seller_id, seller_username, nft_name, nft_photo, price, status, *_ = listing
    nft_name = md_escape(nft_name)
    if status != "active":
        return jsonify({"error": "invalid_status"}), 400

    set_nft_listing_status(listing_id, "cancelled", request.tg_user["id"])
    tg_notify.send_message(seller_id, f"ℹ️ Объявление «{nft_name}» (заявка №{listing_id}) снято с маркета администратором.")
    return jsonify({"ok": True})


@app.route("/api/admin/pricing/nft-commission", methods=["POST"])
@require_admin
def api_admin_pricing_nft_commission():
    data = request.get_json(force=True) or {}
    try:
        percent = float(data.get("percent"))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid_input"}), 400
    if percent < 0 or percent > 100:
        return jsonify({"error": "invalid_input"}), 400
    set_setting("nft_commission_percent", percent)
    return jsonify({"ok": True, "percent": percent})


# ==================== ВЫВОД БАЛАНСА ====================

@app.route("/api/withdraw/create", methods=["POST"])
@require_user
def api_withdraw_create():
    tg_user = request.tg_user
    user_id = tg_user["id"]
    username = tg_user.get("username") or f"id{user_id}"
    data = request.get_json(force=True) or {}

    try:
        amount = float(data.get("amount"))
    except (TypeError, ValueError):
        security_alert("withdraw_abuse", user_id, username, f"Вывод: некорректная сумма {str(data.get('amount'))[:40]!r}")
        return jsonify({"error": "invalid_amount"}), 400
    # NaN/inf/отрицательные суммы проходят обычные сравнения (nan < X == False) — их отсекаем явно
    if not math.isfinite(amount) or amount <= 0:
        security_alert("withdraw_abuse", user_id, username, f"Вывод: недопустимая сумма {amount}")
        return jsonify({"error": "invalid_amount"}), 400
    details = (data.get("details") or "").strip()[:300]

    if amount < MIN_WITHDRAWAL:
        return jsonify({"error": "min_withdrawal", "min_withdrawal": MIN_WITHDRAWAL}), 400
    if not details:
        return jsonify({"error": "missing_details"}), 400

    db_user = ensure_user(tg_user)
    balance = db_user[2]
    if balance < amount:
        security_alert("withdraw_abuse", user_id, username,
                       f"Вывод {fmt_amount(amount)} сум при балансе {fmt_amount(balance)} сум")
        return jsonify({"error": "not_enough_balance", "balance": balance}), 400

    # Выводить можно только деньги, заработанные продажей своих NFT на маркете —
    # остальной баланс (пополнения картой/TON и т.п.) вывести нельзя.
    nft_withdrawable = get_nft_withdrawable_balance(user_id)
    if amount > nft_withdrawable:
        # интерфейс такой запрос не даёт отправить, значит его собрали вручную
        security_alert("withdraw_abuse", user_id, username,
                       f"Вывод {fmt_amount(amount)} сум, а доступно только {fmt_amount(nft_withdrawable)} сум (баланс {fmt_amount(balance)} сум)")
        return jsonify({"error": "nft_only", "nft_withdrawable": nft_withdrawable}), 400

    _fail = _charge_or_fail(user_id, amount, tg_user)
    if _fail:
        return _fail
    add_transaction(user_id, -amount, "withdrawal", "pending", f"Вывод на: {details}")
    withdrawal_id = create_withdrawal(user_id, username, amount, details)

    tg_notify.notify_admins(
        ADMIN_IDS,
        f"💸 *Новая заявка на вывод!*\n\n"
        f"👤 @{username} · 🆔 `{user_id}`\n💰 Сумма: *{fmt_amount(amount)} сум*\n📋 Реквизиты: {md_escape(details)}\n\n"
        f"🧾 Заявка №{withdrawal_id}",
        reply_markup={"inline_keyboard": [[
        {"text": "✅ Подтвердить", "callback_data": f"wd_ok:{withdrawal_id}"},
        {"text": "❌ Отклонить", "callback_data": f"wd_no:{withdrawal_id}"},
    ]]}
    )

    new_balance = get_user(user_id)[2]
    return jsonify({"ok": True, "withdrawal_id": withdrawal_id, "balance": new_balance, "nft_withdrawable": get_nft_withdrawable_balance(user_id)})


@app.route("/api/withdraw/my")
@require_user
def api_withdraw_my():
    user_id = request.tg_user["id"]
    rows = get_user_withdrawals(user_id)
    withdrawals = [{
        "id": r[0], "amount": r[1], "details": r[2], "status": r[3], "created_at": r[4],
    } for r in rows]
    return jsonify({"withdrawals": withdrawals})


@app.route("/api/admin/withdrawals/pending")
@require_admin
def api_admin_withdrawals_pending():
    rows = get_pending_withdrawals()
    withdrawals = [{
        "id": r[0], "user_id": r[1], "username": r[2], "amount": r[3],
        "details": r[4], "created_at": r[5],
    } for r in rows]
    return jsonify({"withdrawals": withdrawals})


def withdrawal_admin_decide(withdrawal_id, action, admin_id):
    """Подтверждает (approve) или отклоняет (reject, деньги возвращаются) заявку на вывод."""
    withdrawal = get_withdrawal(withdrawal_id)
    if not withdrawal:
        return False, "not_found"
    w_id, user_id, username, amount, details, status, created_at = withdrawal
    if status != "pending":
        return False, "already_decided"
    if action == "approve":
        set_withdrawal_status(withdrawal_id, "approved", admin_id)
        mark_transaction_status(user_id, "withdrawal", "pending", "success")
        tg_notify.send_message(user_id, f"✅ Вывод *{fmt_amount(amount)} сум* подтверждён и отправлен на реквизиты: {md_escape(details)}.")
    else:
        set_withdrawal_status(withdrawal_id, "rejected", admin_id)
        mark_transaction_status(user_id, "withdrawal", "pending", "failed")
        update_balance(user_id, amount)  # возврат средств на баланс
        tg_notify.send_message(user_id, f"❌ Заявка на вывод *{fmt_amount(amount)} сум* отклонена, средства возвращены на баланс.")
    return True, None


@app.route("/api/admin/withdrawals/decide", methods=["POST"])
@require_admin
def api_admin_withdrawals_decide():
    data = request.get_json(force=True) or {}
    withdrawal_id = _json_id(data)
    action = data.get("action")
    if withdrawal_id is None or action not in ("approve", "reject"):
        return jsonify({"error": "invalid_input"}), 400
    return _admin_decision_response(withdrawal_admin_decide(withdrawal_id, action, request.tg_user["id"]))


# ==================== ОТЗЫВЫ ====================

@app.route("/api/review", methods=["POST"])
@require_user
def api_review():
    tg_user = request.tg_user
    user_id = tg_user["id"]
    data = request.get_json(force=True) or {}

    order_id = data.get("order_id", "?")
    product = (data.get("product") or "—")[:200]
    text = (data.get("text") or "").strip()[:1000]
    photo_b64 = data.get("photo")

    if not text:
        return jsonify({"error": "empty_text"}), 400

    add_review(user_id, order_id, product, text)

    buyer_username = tg_user.get("username") or f"id{user_id}"
    first_name = tg_user.get("first_name") or buyer_username
    caption = (
        f"⭐ *Отзыв от {first_name}:*\n_{text}_\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📋 *Заказ №{order_id}*\n\n"
        f"👤 Покупатель: {first_name}\n"
        f"🛍️ Покупка: *{product}*"
    )

    photo_bytes = b64_to_bytes(photo_b64)
    if photo_bytes:
        tg_notify.send_photo_bytes(REVIEW_CHANNEL, photo_bytes, caption=caption)
    else:
        tg_notify.send_message(REVIEW_CHANNEL, caption)

    return jsonify({"ok": True})


# ==================== ДЕПОЗИТ: FREEDOM PAY (карта) ====================

@app.route("/api/deposit/freedom/create", methods=["POST"])
@require_user
def api_deposit_freedom_create():
    user_id = request.tg_user["id"]
    data = request.get_json(force=True) or {}
    try:
        amount = int(data.get("amount", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid_amount"}), 400

    if amount < MIN_DEPOSIT:
        return jsonify({"error": "amount_too_small", "min_deposit": MIN_DEPOSIT}), 400

    bot_username = get_bot_username()
    payment_url = create_freedom_payment(amount, user_id, bot_username)
    if not payment_url:
        return jsonify({"error": "payment_creation_failed"}), 502

    # достаём payment_id, который create_freedom_payment только что записал в payments
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT payment_id FROM payments WHERE user_id=? AND amount=? ORDER BY id DESC LIMIT 1",
        (user_id, amount)
    )
    row = cursor.fetchone()
    conn.close()
    payment_id = row[0] if row else None

    return jsonify({"ok": True, "payment_url": payment_url, "payment_id": payment_id, "amount": amount})


@app.route("/api/deposit/freedom/check", methods=["POST"])
@require_user
def api_deposit_freedom_check():
    user_id = request.tg_user["id"]
    data = request.get_json(force=True) or {}
    payment_id = data.get("payment_id")
    if not payment_id:
        return jsonify({"error": "missing_payment_id"}), 400

    payment_row = get_payment(payment_id)
    if not payment_row or payment_row[1] != user_id:
        return jsonify({"error": "not_found"}), 404

    if payment_row[4] == "succeeded":
        # уже зачислено ранее — не начисляем повторно
        return jsonify({"ok": True, "status": "already_credited", "balance": get_user(user_id)[2]})

    status_data = check_payment_status(payment_id)
    if status_data and status_data.get("status") == "succeeded":
        amount = payment_row[2]
        update_balance(user_id, amount)
        add_transaction(user_id, amount, "deposit", "success", f"Freedom Pay пополнение {amount} сум")
        update_payment_status(payment_id, "succeeded")
        return jsonify({"ok": True, "status": "succeeded", "balance": get_user(user_id)[2]})

    return jsonify({"ok": True, "status": "pending"})


# ==================== ДЕПОЗИТ: РУЧНОЙ ПЕРЕВОД (карта/телефон + скрин) ====================

@app.route("/api/deposit/manual", methods=["POST"])
@require_user
def api_deposit_manual():
    tg_user = request.tg_user
    user_id = tg_user["id"]
    data = request.get_json(force=True) or {}

    try:
        amount = int(data.get("amount", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid_amount"}), 400
    if amount < MIN_DEPOSIT:
        return jsonify({"error": "amount_too_small", "min_deposit": MIN_DEPOSIT}), 400

    photo_b64 = data.get("photo")
    if not photo_b64:
        return jsonify({"error": "missing_photo"}), 400

    username = tg_user.get("username") or f"id{user_id}"
    deposit_id = add_manual_deposit(user_id, username, amount, photo_b64)

    caption = (
        f"🏧 *Новая заявка на пополнение (банкомат)!*\n\n"
        f"👤 Пользователь: @{username}\n🆔 ID: `{user_id}`\n💰 Сумма: *{amount:,} сум*\n"
        f"🧾 Заявка №{deposit_id}\n\n"
        f"Откройте раздел «Админ → Депозиты» в мини-аппе, чтобы подтвердить или отклонить."
    )

    keyboard = {"inline_keyboard": [[
        {"text": "✅ Одобрить", "callback_data": f"dep_ok:{deposit_id}"},
        {"text": "❌ Отклонить", "callback_data": f"dep_no:{deposit_id}"},
    ]]}
    photo_bytes = b64_to_bytes(photo_b64)
    if photo_bytes:
        sent = tg_notify.notify_admins_photo(ADMIN_IDS, photo_bytes, caption=caption, reply_markup=keyboard)
        if not sent:
            # если фото по какой-то причине не отправилось (слишком большое,
            # битый base64 и т.п.) — админ всё равно должен узнать о заявке
            tg_notify.notify_admins(ADMIN_IDS, caption, reply_markup=keyboard)
    else:
        tg_notify.notify_admins(ADMIN_IDS, caption, reply_markup=keyboard)

    return jsonify({"ok": True, "deposit_id": deposit_id})


# ==================== АВТОПЛАТЁЖ ЧЕРЕЗ PAYME P2P ====================

@app.route("/api/deposit/card/create", methods=["POST"])
@require_user
def api_card_deposit_create():
    """Создаёт заявку на автоматическое пополнение через карту (Uzcard/Humo).
    Баланс зачисляется автоматически, когда telegram_sms_listener.py
    получает уведомление от @CardXabarBot и вызывает /api/sms/webhook —
    подтверждение идёт только по сумме, комментарий/последние цифры карты
    от пользователя не требуются."""
    tg_user = request.tg_user
    user_id = tg_user["id"]
    data = request.get_json(force=True) or {}

    if not SMS_WEBHOOK_SECRET:
        return jsonify({"error": "card_auto_disabled"}), 503

    try:
        amount = int(data.get("amount", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid_amount"}), 400

    if amount < MIN_DEPOSIT:
        return jsonify({"error": "amount_too_small", "min_deposit": MIN_DEPOSIT}), 400

    expire_stale_card_deposits()
    username = tg_user.get("username") or f"id{user_id}"
    deposit_id, expires_at = create_card_auto_deposit(
        user_id, username, amount, amount, "card", CARD_DEPOSIT_TTL_MINUTES
    )

    expires_at_timestamp = int(
        datetime.datetime.strptime(expires_at, "%Y-%m-%d %H:%M:%S").timestamp() * 1000
    )

    return jsonify({
        "ok": True,
        "deposit_id": deposit_id,
        "amount": amount,
        "expires_at": expires_at_timestamp,
        "ttl_minutes": CARD_DEPOSIT_TTL_MINUTES,
    })


@app.route("/api/deposit/card/status/<int:deposit_id>")
@require_user
def api_card_deposit_status(deposit_id):
    user_id = request.tg_user["id"]
    dep = get_card_deposit(deposit_id)
    if not dep or dep[1] != user_id:
        return jsonify({"error": "not_found"}), 404
    expire_stale_card_deposits()
    dep = get_card_deposit(deposit_id)
    status = dep[6]
    result = {"status": status}
    if status == "confirmed":
        result["balance"] = get_user(user_id)[2]
    return jsonify(result)


@app.route("/api/deposit/card/cancel", methods=["POST"])
@require_user
def api_card_deposit_cancel():
    user_id = request.tg_user["id"]
    data = request.get_json(force=True) or {}
    try:
        deposit_id = int(data.get("id"))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid_input"}), 400
    dep = get_card_deposit(deposit_id)
    if not dep or dep[1] != user_id:
        return jsonify({"error": "not_found"}), 404
    cancel_card_deposit(deposit_id)
    return jsonify({"ok": True})



def parse_amount_from_sms(text, our_card_number=None):
    """
    Парсит уведомление от @CardXabarBot (UzCard) или @HUMOcardbot (Humo).
    Форматы у провайдеров отличаются:

    CardXabarBot:
        🟢 Perevod na kartu
        ➕ 1 000.00 UZS          <- пробел = тысячи, точка = копейки
        💳 ***4031                <- маскировка: 3+ звёздочки

    HUMOcardbot:
        🎉 **Пополнение**
        ➕ **10.000,00 UZS**     <- обёрнуто в markdown-жирный; точка = тысячи, запятая = копейки
        💳 HUMOCARD *8012        <- маскировка: 1 звёздочка

    Правила:
    1. Учитываем ТОЛЬКО пополнения (строка с ➕), списания (➖) игнорируем полностью.
    2. Разделители тысяч/копеек определяются автоматически по тому, какой символ
       (точка или запятая) стоит ПОСЛЕДНИМ в числе — это и есть разделитель копеек.
    3. Если известен номер нашей карты — сверяем последние 4 цифры (💳 *XXXX или ***XXXX).
    """
    if not text:
        return None

    # Только пополнения — если в тексте есть признак списания, сразу выходим
    if '➖' in text or re.search(r'\bspisanie\b|\bсписание\b', text, re.IGNORECASE):
        return None
    if '➕' not in text:
        return None

    # [*_]* — пропускаем возможную markdown-разметку (**жирный**, __курсив__) вокруг суммы
    amount_match = re.search(
        r'➕\s*[*_]*\s*([\d\s.,\u00A0]+)\s*[*_]*\s*(?:UZS|сум|сўм|so\'?m)',
        text, re.IGNORECASE
    )
    if not amount_match:
        return None

    raw = amount_match.group(1).strip().replace('\u00A0', ' ')
    raw = re.sub(r'\s', '', raw)  # убираем пробелы (могут быть разделителем тысяч)
    if not raw:
        return None

    # Определяем разделитель копеек: последний по позиции символ '.' или ',' в числе.
    # "1000.00" -> копейки после точки. "10.000,00" -> копейки после запятой.
    last_dot = raw.rfind('.')
    last_comma = raw.rfind(',')
    if last_comma > last_dot:
        decimal_sep, thousand_sep = ',', '.'
    else:
        decimal_sep, thousand_sep = '.', ','

    cleaned = raw.replace(thousand_sep, '').replace(decimal_sep, '.')
    try:
        amount = float(cleaned)
    except ValueError:
        return None
    if amount <= 0:
        return None

    if our_card_number:
        card_match = re.search(r'\*+\s*(\d{4})', text)
        if card_match:
            our_last4 = re.sub(r'\D', '', our_card_number)[-4:]
            if card_match.group(1) != our_last4:
                return None  # операция по другой карте — не наша

    return amount


@app.route("/api/sms/webhook", methods=["POST"])
def api_sms_webhook():
    """
    Вебхук для telegram_sms_listener.py — сюда прилетают банковские
    уведомления от @CardXabarBot / @HUMOcardbot в сыром виде.

    Комментария/ID перевода в SMS нет, поэтому подтверждение идёт
    строго по СУММЕ: ищем pending-заявку (card_auto_deposits) с такой
    же exact_amount, ещё не истёкшую (CARD_DEPOSIT_TTL_MINUTES).
    """
    if not SMS_WEBHOOK_SECRET:
        return jsonify({"error": "not_configured"}), 503

    data = request.get_json(force=True, silent=True) or {}
    secret = request.headers.get("X-Webhook-Secret") or data.get("secret") or ""
    if not hmac.compare_digest(str(secret), SMS_WEBHOOK_SECRET):
        security_alert("bad_webhook_secret", details="POST /api/sms/webhook с неверным секретом (попытка подделать пополнение)", ban_button=False)
        return jsonify({"error": "forbidden"}), 403

    text = data.get("text", "") or ""
    card_id = data.get("card_id")

    expire_stale_card_deposits()

    amount = parse_amount_from_sms(text, our_card_number=MANUAL_CARD_NUMBER)
    if amount is None:
        logger.warning(f"⚠️ SMS webhook: не удалось распознать сумму в тексте: {text[:200]}")
        return jsonify({"ok": True, "matched": False, "reason": "amount_not_parsed"})

    match = find_pending_card_match(amount, card_id)
    if not match:
        logger.info(f"ℹ️ SMS webhook: сумма {amount} не совпала ни с одной pending-заявкой")
        return jsonify({"ok": True, "matched": False, "reason": "no_pending_match", "amount": amount})

    dep_id, user_id, username, exact_amount, matched_card_id = match

    update_balance(user_id, exact_amount)
    add_transaction(
        user_id, exact_amount, "card_auto_deposit", "success",
        f"Автопополнение картой (заявка №{dep_id})"
    )
    confirm_card_deposit(dep_id, sms_text=text[:500])

    try:
        tg_notify.send_message(
            user_id,
            f"✅ *Баланс пополнен!*\n\nПолучили: *{exact_amount:,.0f} сум*\nБаланс: *{get_user(user_id)[2]:,.0f} сум*"
        )
    except Exception as e:
        logger.warning(f"Не удалось уведомить пользователя {user_id} о пополнении: {e}")

    logger.info(f"✅ SMS webhook: заявка №{dep_id} подтверждена, {exact_amount} сум → user {user_id}")
    return jsonify({"ok": True, "matched": True, "deposit_id": dep_id})


# ==================== АДМИН: БЛОКИРОВКА ПОЛЬЗОВАТЕЛЕЙ ====================

def admin_ban(target_id, admin_id, reason=""):
    """Блокирует пользователя по ID. Возвращает (ok, error). Используется и кнопкой в Mini App, и кнопкой в Telegram."""
    if target_id in ADMIN_IDS:
        return False, "cannot_ban_admin"
    if is_banned(target_id):
        return False, "already_banned"
    ban_user(target_id, reason=reason, banned_by=admin_id)
    add_security_event(target_id, None, "banned", f"Заблокирован админом {admin_id}. {reason}".strip())
    try:
        tg_notify.send_message(target_id, "🚫 *Ваш доступ к магазину заблокирован.*\n\nЕсли это ошибка — свяжитесь с администратором.")
    except Exception:
        pass
    return True, None


def admin_unban(target_id, admin_id):
    if not unban_user(target_id):
        return False, "not_banned"
    add_security_event(target_id, None, "unbanned", f"Разблокирован админом {admin_id}")
    try:
        tg_notify.send_message(target_id, "✅ *Доступ к магазину восстановлен.*")
    except Exception:
        pass
    return True, None


@app.route("/api/admin/ban", methods=["POST"])
@require_admin
def api_admin_ban():
    data = request.get_json(force=True, silent=True) or {}
    try:
        target_id = int(str(data.get("user_id", "")).strip())
    except (TypeError, ValueError):
        return jsonify({"error": "invalid_input"}), 400
    if target_id <= 0:
        return jsonify({"error": "invalid_input"}), 400
    reason = str(data.get("reason") or "").strip()[:200]
    ok, err = admin_ban(target_id, request.tg_user["id"], reason)
    if not ok:
        return jsonify({"error": err}), 400
    return jsonify({"ok": True, "user_id": target_id})


@app.route("/api/admin/unban", methods=["POST"])
@require_admin
def api_admin_unban():
    data = request.get_json(force=True, silent=True) or {}
    try:
        target_id = int(str(data.get("user_id", "")).strip())
    except (TypeError, ValueError):
        return jsonify({"error": "invalid_input"}), 400
    ok, err = admin_unban(target_id, request.tg_user["id"])
    if not ok:
        return jsonify({"error": err}), 400
    return jsonify({"ok": True, "user_id": target_id})


@app.route("/api/admin/bans")
@require_admin
def api_admin_bans():
    bans = [{"user_id": r[0], "username": r[1] or "", "reason": r[2] or "", "banned_by": r[3], "banned_at": r[4]}
            for r in get_banned_users()]
    events = [{"id": r[0], "user_id": r[1], "username": r[2] or "", "kind": r[3], "details": r[4] or "",
               "ip": r[5] or "", "created_at": r[6]} for r in get_recent_security_events(40)
              if r[3] not in ("banned", "unbanned")]
    return jsonify({"bans": bans, "events": events, "titles": SECURITY_TITLES})


# ==================== АДМИН-ПАНЕЛЬ ====================

@app.route("/api/admin/stats")
@require_admin
def api_admin_stats():
    # «Баланс всех» не включает админов (владелец и разработчик): их баланс служебный
    return jsonify(get_admin_stats(exclude_user_ids=ADMIN_IDS))


@app.route("/api/admin/activity")
@require_admin
def api_admin_activity():
    """Единая лента: ВСЕ действия пользователей и админов (пополнения, ручные начисления/списания,
    покупки, игры и Steam, NFT, карточки, токены). ?group=deposit|admin|purchase|game|nft|cards|other."""
    group = request.args.get("group", "").strip() or None
    if group not in (None, "deposit", "admin", "purchase", "game", "nft", "cards", "other"):
        group = None
    try:
        limit = max(1, min(int(request.args.get("limit", 60)), 200))
        offset = max(int(request.args.get("offset", 0)), 0)
    except (TypeError, ValueError):
        limit, offset = 60, 0
    return jsonify({"events": get_activity_feed(limit=limit, offset=offset, group=group)})


@app.route("/api/admin/users")
@require_admin
def api_admin_users():
    """Список пользователей. ?filter=all|high|low (высокий/низкий баланс — только те, у кого баланс > 0),
    ?q= поиск по ID/username (работает вместе с фильтром), ?limit=&offset= для «Показать ещё»."""
    q = request.args.get("q", "").strip()
    mode = request.args.get("filter", "all").strip()
    if mode not in ("all", "high", "low"):
        mode = "all"
    try:
        limit = max(1, min(int(request.args.get("limit", 30)), 100))
        offset = max(0, int(request.args.get("offset", 0)))
    except (TypeError, ValueError):
        limit, offset = 30, 0
    rows, total = list_users_filtered(mode, q, limit=limit, offset=offset)
    banned_ids = {b[0] for b in get_banned_users(100000)}
    return jsonify({
        "users": [
            {"user_id": r[0], "username": r[1] or "—", "balance": r[2], "referrer_id": r[3], "created_at": r[4],
             "banned": r[0] in banned_ids}
            for r in rows
        ],
        "total": total, "filter": mode, "offset": offset, "limit": limit,
    })


@app.route("/api/admin/users/<int:user_id>/history")
@require_admin
def api_admin_user_history(user_id):
    """История одного пользователя: пополнения, траты, игровые донаты, токены."""
    summary = get_user_history_summary(user_id)
    if not summary:
        return jsonify({"error": "not_found"}), 404
    return jsonify({"summary": summary, "events": get_user_history(user_id, limit=200)})


@app.route("/api/admin/stars/monitor")
@require_admin
def api_admin_stars_monitor():
    """Сколько звёзд роздано призами за сегодня + у кого сколько накоплено (без нулевых)."""
    rows, holders, total = get_top_star_holders(limit=50)
    pool = request.args.get("pool", "daily")
    return jsonify({
        "pool": get_stars_pool_stats(pool),
        "given_today": get_stars_given_today(),
        "holders": [{"user_id": r[0], "username": r[1] or "—", "stars": r[2]} for r in rows],
        "holders_count": holders, "stars_total": total,
    })


@app.route("/api/admin/tokens/monitor")
@require_admin
def api_admin_tokens_monitor():
    """Рейтинг пользователей по токенам (без нулевых) + общие цифры."""
    rows, overview = get_top_token_holders(limit=50)
    return jsonify({
        "holders": [{"user_id": r[0], "username": r[1] or "—", "tokens": r[2]} for r in rows],
        "overview": overview,
    })


@app.route("/api/admin/balance/add", methods=["POST"])
@require_admin
def api_admin_balance_add():
    admin_id = request.tg_user["id"]
    data = request.get_json(force=True) or {}
    try:
        target_id = int(data.get("user_id"))
        amount = float(data.get("amount"))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid_input"}), 400
    if amount <= 0:
        return jsonify({"error": "invalid_amount"}), 400

    target = get_user(target_id)
    if not target:
        return jsonify({"error": "user_not_found"}), 404

    update_balance(target_id, amount)
    add_transaction(target_id, amount, "admin_deposit", "success", f"Ручное пополнение администратором {admin_id}")
    new_balance = get_user(target_id)[2]

    tg_notify.send_message(
        target_id,
        f"💰 *Ваш баланс пополнен!*\n\nНачислено: *+{amount:,.0f} сум*\nТекущий баланс: *{new_balance:,.0f} сум*"
    )
    return jsonify({"ok": True, "balance": new_balance})


@app.route("/api/admin/balance/remove", methods=["POST"])
@require_admin
def api_admin_balance_remove():
    admin_id = request.tg_user["id"]
    data = request.get_json(force=True) or {}
    try:
        target_id = int(data.get("user_id"))
        amount = float(data.get("amount"))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid_input"}), 400
    if amount <= 0:
        return jsonify({"error": "invalid_amount"}), 400

    target = get_user(target_id)
    if not target:
        return jsonify({"error": "user_not_found"}), 404

    current_balance = target[2]
    if current_balance < amount:
        return jsonify({"error": "insufficient_balance", "balance": current_balance}), 400

    update_balance(target_id, -amount)
    add_transaction(target_id, amount, "admin_remove", "completed", f"Удалено админом {admin_id}: {amount} сумов")
    new_balance = get_user(target_id)[2]

    lang = get_user_language(target_id)
    if lang == "uz":
        msg = f"⚠️ Balansdan {amount:,.0f} so'm olib tashlandi\n📊 Yangi balans: {new_balance:,.0f} so'm"
    else:
        msg = f"⚠️ С вашего баланса списано {amount:,.0f} сумов\n📊 Новый баланс: {new_balance:,.0f} сумов"
    tg_notify.send_message(target_id, msg)

    return jsonify({"ok": True, "balance": new_balance})


@app.route("/api/admin/deposits/pending")
@require_admin
def api_admin_deposits_pending():
    rows = get_pending_manual_deposits()
    return jsonify({
        "deposits": [
            {
                "id": r[0], "user_id": r[1], "username": r[2] or "—", "amount": r[3],
                "photo": r[4], "created_at": r[5],
            }
            for r in rows
        ]
    })


def deposit_admin_approve(deposit_id, admin_id):
    """Подтверждает заявку на пополнение (банкомат): зачисляет баланс и уведомляет клиента."""
    dep = get_manual_deposit(deposit_id)
    if not dep:
        return False, "not_found"
    if dep[5] != "pending":
        return False, "already_decided"
    _, target_id, username, amount, _, _ = dep
    update_balance(target_id, amount)
    add_transaction(target_id, amount, "manual_deposit", "success", f"Ручное пополнение, подтверждено админом {admin_id}")
    set_manual_deposit_status(deposit_id, "approved", admin_id)
    tg_notify.send_message(target_id, f"✅ *Ваш баланс пополнен!*\n\nПополнено: *+{amount:,.0f} сум*")
    return True, None


def deposit_admin_reject(deposit_id, admin_id):
    """Отклоняет заявку на пополнение и уведомляет клиента."""
    dep = get_manual_deposit(deposit_id)
    if not dep:
        return False, "not_found"
    if dep[5] != "pending":
        return False, "already_decided"
    set_manual_deposit_status(deposit_id, "rejected", admin_id)
    tg_notify.send_message(
        dep[1],
        "❌ *Ваш запрос на пополнение баланса не прошёл проверку.*\n\nДля уточнения деталей обратитесь к администратору."
    )
    return True, None


def _admin_decision_response(result):
    ok, err = result
    if ok:
        return jsonify({"ok": True})
    return jsonify({"error": err}), 404 if err == "not_found" else 400


def _json_id(data, key="id"):
    try:
        return int((data or {}).get(key))
    except (TypeError, ValueError):
        return None


@app.route("/api/admin/deposits/approve", methods=["POST"])
@require_admin
def api_admin_deposit_approve():
    deposit_id = _json_id(request.get_json(force=True))
    if deposit_id is None:
        return jsonify({"error": "invalid_input"}), 400
    return _admin_decision_response(deposit_admin_approve(deposit_id, request.tg_user["id"]))


@app.route("/api/admin/deposits/reject", methods=["POST"])
@require_admin
def api_admin_deposit_reject():
    deposit_id = _json_id(request.get_json(force=True))
    if deposit_id is None:
        return jsonify({"error": "invalid_input"}), 400
    return _admin_decision_response(deposit_admin_reject(deposit_id, request.tg_user["id"]))


@app.route("/api/admin/card-deposits/pending")
@require_admin
def api_admin_card_deposits_pending():
    expire_stale_card_deposits()
    rows = get_pending_card_deposits()
    return jsonify({
        "deposits": [
            {
                "id": r[0], "user_id": r[1], "username": r[2] or "—",
                "requested_amount": r[3], "exact_amount": r[4],
                "card_label": "Карта",
                "created_at": r[6], "expires_at": r[7],
            }
            for r in rows
        ]
    })


@app.route("/api/admin/card-deposits/confirm", methods=["POST"])
@require_admin
def api_admin_card_deposit_confirm():
    admin_id = request.tg_user["id"]
    data = request.get_json(force=True) or {}
    try:
        deposit_id = int(data.get("id"))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid_input"}), 400

    dep = get_card_deposit(deposit_id)
    if not dep:
        return jsonify({"error": "not_found"}), 404
    if dep[6] != "pending":
        return jsonify({"error": "already_decided"}), 400

    _, target_id, username, requested_amount, exact_amount, card_id, _, _ = dep
    update_balance(target_id, exact_amount)
    add_transaction(target_id, exact_amount, "card_auto_deposit", "success", f"Автопополнение картой, подтверждено вручную админом {admin_id}")
    confirm_card_deposit(deposit_id, sms_text="(подтверждено администратором вручную)", admin_id=admin_id)

    tg_notify.send_message(target_id, f"✅ *Ваш баланс пополнен!*\n\nПолучили перевод: *{exact_amount:,.0f} сум*")
    return jsonify({"ok": True})


@app.route("/api/admin/card-deposits/cancel", methods=["POST"])
@require_admin
def api_admin_card_deposit_cancel():
    admin_id = request.tg_user["id"]
    data = request.get_json(force=True) or {}
    try:
        deposit_id = int(data.get("id"))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid_input"}), 400

    dep = get_card_deposit(deposit_id)
    if not dep:
        return jsonify({"error": "not_found"}), 404
    if dep[6] != "pending":
        return jsonify({"error": "already_decided"}), 400

    cancel_card_deposit(deposit_id, admin_id=admin_id)
    return jsonify({"ok": True})


@app.route("/api/admin/export")
@require_admin
def api_admin_export():
    from excel_export import export_database_to_excel
    filename = export_database_to_excel()
    if not filename or not os.path.exists(filename):
        return jsonify({"error": "export_failed"}), 500
    return send_file(filename, as_attachment=True, download_name=os.path.basename(filename))


# ==================== АДМИН: ИГРОВЫЕ ЗАЯВКИ (Brawl Stars и т.п.) ====================

# ==================== АДМИН: ЦЕНЫ (Stars, Premium, игровые товары) ====================

@app.route("/api/admin/pricing")
@require_admin
def api_admin_pricing():
    return jsonify({
        "star_price": get_effective_star_price(),
        "star_price_old": get_effective_star_old_price(),
        "premium": get_effective_premium_prices(),
        "premium_old": get_effective_premium_old_prices(),
        "brawl_stars_products": apply_product_price_overrides(BRAWL_STARS_PRODUCTS),
        "roblox_products": apply_product_price_overrides(ROBLOX_PRODUCTS),
        "standoff_gold_products": apply_product_price_overrides(STANDOFF_GOLD_PRODUCTS),
        "nft_commission_percent": get_effective_nft_commission(),
        "steam_rate": get_effective_steam_rate(),
    })


@app.route("/api/admin/pricing/steam-rate", methods=["POST"])
@require_admin
def api_admin_pricing_steam_rate():
    data = request.get_json(force=True) or {}
    try:
        rate = float(data.get("rate"))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid_input"}), 400
    if rate <= 0 or rate > 1000000:
        return jsonify({"error": "invalid_input"}), 400
    set_setting("steam_rate", rate)
    return jsonify({"ok": True, "rate": rate})


@app.route("/api/admin/pricing/star", methods=["POST"])
@require_admin
def api_admin_pricing_star():
    data = request.get_json(force=True) or {}
    try:
        price = float(data.get("price"))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid_price"}), 400
    if price <= 0:
        return jsonify({"error": "invalid_price"}), 400
    old_price = data.get("old_price")
    try:
        old_price = float(old_price) if old_price not in (None, "") else None
    except (TypeError, ValueError):
        old_price = None
    if old_price is not None and old_price <= price:
        old_price = None
    set_setting("star_price", json.dumps({"price": price, "old_price": old_price}))
    return jsonify({"ok": True, "star_price": price, "star_price_old": old_price})


@app.route("/api/admin/pricing/premium", methods=["POST"])
@require_admin
def api_admin_pricing_premium():
    data = request.get_json(force=True) or {}
    try:
        months = int(data.get("months"))
        price = float(data.get("price"))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid_input"}), 400
    if months not in (3, 6, 12) or price <= 0:
        return jsonify({"error": "invalid_input"}), 400
    old_price = data.get("old_price")
    try:
        old_price = float(old_price) if old_price not in (None, "") else None
    except (TypeError, ValueError):
        old_price = None
    if old_price is not None and old_price <= price:
        old_price = None

    v = get_setting("premium_prices")
    raw = {}
    if v:
        try:
            raw = json.loads(v)
        except (TypeError, ValueError, json.JSONDecodeError):
            raw = {}
    raw[str(months)] = {"price": price, "old_price": old_price}
    set_setting("premium_prices", json.dumps(raw))
    return jsonify({"ok": True, "premium": get_effective_premium_prices()})


@app.route("/api/admin/pricing/product", methods=["POST"])
@require_admin
def api_admin_pricing_product():
    data = request.get_json(force=True) or {}
    product_id = data.get("product_id")
    try:
        price = float(data.get("price"))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid_price"}), 400
    if not product_id or price <= 0:
        return jsonify({"error": "invalid_input"}), 400

    all_ids = {p["id"] for p in BRAWL_STARS_PRODUCTS + ROBLOX_PRODUCTS + STANDOFF_GOLD_PRODUCTS}
    if product_id not in all_ids:
        return jsonify({"error": "unknown_product"}), 400

    old_price = data.get("old_price")
    try:
        old_price = float(old_price) if old_price not in (None, "") else None
    except (TypeError, ValueError):
        old_price = None
    if old_price is not None and old_price <= price:
        old_price = None

    overrides = _get_product_price_overrides()
    overrides[product_id] = {"price": price, "old_price": old_price}
    set_setting("product_prices", json.dumps(overrides))
    return jsonify({"ok": True, "product_id": product_id, "price": price, "old_price": old_price})


@app.route("/api/admin/game-orders/pending")
@require_admin
def api_admin_game_orders_pending():
    category = request.args.get("category")
    rows = get_active_game_orders(category)
    orders = []
    for r in rows:
        o_id, o_user_id, o_username, o_category, o_product_id, o_product_name, o_price, o_extra, o_status, o_created_at = r
        try:
            extra = json.loads(o_extra) if o_extra else {}
        except (TypeError, ValueError):
            extra = {}
        orders.append({
            "id": o_id, "user_id": o_user_id, "username": o_username or "—",
            "category": o_category, "product_id": o_product_id, "product_name": o_product_name,
            "price": o_price, "extra": extra, "status": o_status, "created_at": o_created_at,
        })
    return jsonify({"orders": orders})


def game_order_admin_complete(order_id, admin_id):
    """Подтверждает выполнение игровой заявки (мини-апп и кнопка в боте). Возвращает (ok, error)."""
    order = get_game_order(order_id)
    if not order:
        return False, "not_found"
    o_id, o_user_id, o_username, o_category, o_product_id, o_product_name, o_price, o_extra, o_status = order
    if o_status != "pending_admin":
        return False, "already_decided"

    set_game_order_status(order_id, "done", admin_id)
    tokens_earned, _tb = _award_cashback(o_user_id, o_price, f"game_order:{order_id}", o_product_name)
    token_line = f"\n🪙 Кэшбэк: *+{tokens_earned} токенов*" if tokens_earned else ""
    if o_category == "steam":
        try:
            x = json.loads(o_extra) if o_extra else {}
        except (TypeError, ValueError):
            x = {}
        usd = x.get("usd")
        usd_txt = f" на *${float(usd):g}*" if usd else ""
        tg_notify.send_message(
            o_user_id,
            f"✅ *Заявка №{order_id} выполнена!*\n\n"
            f"🎮 Баланс вашего Steam аккаунта успешно пополнен{usd_txt}.{token_line}\nСпасибо за покупку! 🙌"
        )
    else:
        tg_notify.send_message(
            o_user_id,
            f"✅ *Заявка №{order_id} выполнена!*\n\n🛍 {o_product_name} успешно зачислен(-ы) на ваш аккаунт.{token_line}\nСпасибо за покупку! 🙌"
        )
    return True, None


def game_order_admin_cancel(order_id, admin_id):
    """Отменяет игровую заявку и возвращает деньги (мини-апп и кнопка в боте). Возвращает (ok, error)."""
    order = get_game_order(order_id)
    if not order:
        return False, "not_found"
    o_id, o_user_id, o_username, o_category, o_product_id, o_product_name, o_price, o_extra, o_status = order
    if o_status not in (
        "pending_admin", "awaiting_email", "awaiting_credentials",
        "awaiting_screenshot", "screenshot_review", "in_progress", "bought", "transferred",
    ):
        return False, "already_decided"

    update_balance(o_user_id, o_price)
    add_transaction(o_user_id, o_price, "refund", "success", f"Возврат по заявке №{order_id} ({o_product_name}), отменено админом {admin_id}")
    set_game_order_status(order_id, "cancelled", admin_id)
    tg_notify.send_message(
        o_user_id,
        f"❌ *Заявка №{order_id} отменена*\n\n🛍 {o_product_name}\n💰 Средства возвращены на баланс: *+{o_price:,.0f} сум*"
    )
    return True, None


@app.route("/api/admin/game-orders/complete", methods=["POST"])
@require_admin
def api_admin_game_order_complete():
    data = request.get_json(force=True) or {}
    try:
        order_id = int(data.get("id"))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid_input"}), 400
    ok, err = game_order_admin_complete(order_id, request.tg_user["id"])
    if not ok:
        return jsonify({"error": err}), 404 if err == "not_found" else 400
    return jsonify({"ok": True})


@app.route("/api/admin/game-orders/cancel", methods=["POST"])
@require_admin
def api_admin_game_order_cancel():
    data = request.get_json(force=True) or {}
    try:
        order_id = int(data.get("id"))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid_input"}), 400
    ok, err = game_order_admin_cancel(order_id, request.tg_user["id"])
    if not ok:
        return jsonify({"error": err}), 404 if err == "not_found" else 400
    return jsonify({"ok": True})


def run_webapp(host="0.0.0.0", port=None):
    port = int(port or os.getenv("PORT", os.getenv("WEBAPP_PORT", 5000)))
    logger.info(f"🌐 Запуск Mini App сервера на {host}:{port}")
    app.run(host=host, port=port, debug=False, use_reloader=False)
