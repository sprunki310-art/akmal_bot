import sqlite3
import datetime
import json
import os
from config import DB_NAME, logger


def _conn():
    conn = sqlite3.connect(DB_NAME)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    conn = _conn()
    cursor = conn.cursor()

    # 1. Пользователи
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            balance REAL DEFAULT 0.0,
            referrer_id INTEGER,
            language TEXT DEFAULT 'ru',
            created_at TEXT
        )
    ''')

    # 2. Транзакции (покупки, начисления, возвраты)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            amount REAL,
            type TEXT,
            status TEXT,
            description TEXT,
            timestamp TEXT
        )
    ''')

    # 3. Платежи Freedom Pay
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            amount REAL,
            payment_id TEXT UNIQUE,
            status TEXT,
            timestamp TEXT
        )
    ''')

    # 4. Сессии (используются по минимуму — для совместимости)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS sessions (
            user_id INTEGER PRIMARY KEY,
            data TEXT
        )
    ''')

    # 5. Настройки / курс TON / счётчик заказов
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    ''')

    # 6. Заявки на ручное пополнение (карта/телефон + скрин), теперь модерируются
    #    из админ-панели мини-аппа, а не в чате с ботом.
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS manual_deposits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            amount REAL,
            photo TEXT,
            status TEXT DEFAULT 'pending',
            admin_id INTEGER,
            created_at TEXT,
            decided_at TEXT
        )
    ''')

    # 7. Отзывы, оставленные после покупки
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            order_id INTEGER,
            product TEXT,
            text TEXT,
            created_at TEXT
        )
    ''')

    # 8. Заявки на автоплатёж по картам (Uzcard/Humo/Visa) — подтверждаются
    #    автоматически по входящей банковской SMS через /api/sms/webhook
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS card_auto_deposits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            requested_amount REAL,
            exact_amount REAL,
            card_id TEXT,
            status TEXT DEFAULT 'pending',
            sms_text TEXT,
            admin_id INTEGER,
            created_at TEXT,
            expires_at TEXT,
            confirmed_at TEXT
        )
    ''')

    # 9. Заявки на игровые товары (Brawl Stars, Roblox, Standoff и т.п.) —
    #    оплачиваются с баланса, исполняются администратором вручную.
    #    extra — JSON с доп. данными по категории (email, логин/пароль, скин...).
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS game_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            category TEXT,
            product_id TEXT,
            product_name TEXT,
            price REAL,
            extra TEXT,
            status TEXT DEFAULT 'new',
            admin_id INTEGER,
            created_at TEXT,
            updated_at TEXT
        )
    ''')

    # 9. NFT-маркетплейс: заявки на публикацию NFT (продавец отправляет NFT
    #    администратору вручную, после проверки объявление появляется на маркете).
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS nft_listings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_id INTEGER,
            seller_username TEXT,
            nft_name TEXT,
            nft_photo TEXT,
            price REAL,
            status TEXT DEFAULT 'pending_admin',
            buyer_id INTEGER,
            buyer_username TEXT,
            admin_id INTEGER,
            created_at TEXT,
            updated_at TEXT
        )
    ''')

    # 10. Заявки на вывод баланса.
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS withdrawals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            amount REAL,
            details TEXT,
            status TEXT DEFAULT 'pending',
            admin_id INTEGER,
            created_at TEXT,
            updated_at TEXT
        )
    ''')

    # 11. Ежедневные карточки: настраиваемый пул призов (админ задаёт вес/шанс,
    # 0 = не выпадает) и история открытий по пользователям.
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS daily_card_prizes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL,
            label TEXT NOT NULL,
            weight INTEGER NOT NULL DEFAULT 0,
            payload TEXT,
            created_at TEXT
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS daily_card_claims (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            claim_date TEXT,
            prize_id INTEGER,
            prize_type TEXT,
            prize_label TEXT,
            result_text TEXT,
            status TEXT DEFAULT 'completed',
            created_at TEXT
        )
    ''')

    # Докупка доп. открытий ежедневной карточки за баланс — не чаще
    # 1 раза в 24 часа (проверяем по last purchased_at, а не по календарной дате).
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS daily_card_extra_purchases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            purchased_at TEXT
        )
    ''')

    # Активные скидки, выпавшие с карточки (одноразовые, на конкретную игру
    # или на любую) — применяются автоматически при следующей покупке.
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_discounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            percent REAL,
            category TEXT,
            used INTEGER DEFAULT 0,
            created_at TEXT
        )
    ''')

    # Безопасный фикс структуры, если таблица users уже существовала без поля language
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN language TEXT DEFAULT 'ru'")
    except sqlite3.OperationalError:
        pass
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN star_balance INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    try:
        cursor.execute("ALTER TABLE user_discounts ADD COLUMN product_id TEXT DEFAULT 'all'")
    except sqlite3.OperationalError:
        pass
    try:
        cursor.execute("ALTER TABLE user_discounts ADD COLUMN product_name TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    try:
        cursor.execute("ALTER TABLE user_discounts ADD COLUMN expires_at TEXT")
    except sqlite3.OperationalError:
        pass

    # Миграция: weight в daily_card_prizes должен быть REAL (хранить дробные %)
    # SQLite не поддерживает ALTER COLUMN, поэтому пересоздаём таблицу через RENAME
    try:
        cursor.execute("SELECT weight FROM daily_card_prizes LIMIT 1")
        col_type = cursor.execute(
            "SELECT type FROM pragma_table_info('daily_card_prizes') WHERE name='weight'"
        ).fetchone()
        if col_type and col_type[0].upper() == 'INTEGER':
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS daily_card_prizes_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    type TEXT NOT NULL,
                    label TEXT NOT NULL,
                    weight REAL NOT NULL DEFAULT 0,
                    payload TEXT,
                    created_at TEXT
                )
            """)
            cursor.execute("""
                INSERT INTO daily_card_prizes_new (id, type, label, weight, payload, created_at)
                SELECT id, type, label, CAST(weight AS REAL), payload, created_at FROM daily_card_prizes
            """)
            cursor.execute("DROP TABLE daily_card_prizes")
            cursor.execute("ALTER TABLE daily_card_prizes_new RENAME TO daily_card_prizes")
    except Exception:
        pass

    # Внутренняя валюта (токены): баланс в users + журнал операций
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN token_balance INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS token_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            amount INTEGER,
            type TEXT,
            description TEXT,
            ref TEXT,
            timestamp TEXT
        )
    ''')
    # Тайный приз (бокс за токены): пул призов, который задаёт админ, и журнал открытий
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS mystery_box_prizes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL,
            label TEXT NOT NULL,
            weight REAL NOT NULL DEFAULT 0,
            payload TEXT,
            created_at TEXT
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS mystery_box_openings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            prize_id INTEGER,
            prize_type TEXT,
            prize_label TEXT,
            result_text TEXT,
            price INTEGER,
            status TEXT DEFAULT 'completed',
            created_at TEXT
        )
    ''')
    # 3x. Блокировки пользователей (бан по ID) и журнал событий безопасности
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS banned_users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            reason TEXT,
            banned_by INTEGER,
            banned_at TEXT
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS security_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            kind TEXT,
            details TEXT,
            ip TEXT,
            created_at TEXT
        )
    ''')
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sec_events_user ON security_events (user_id, kind, id DESC)")

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_token_tx_user ON token_transactions (user_id, id DESC)")
    cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_token_tx_ref ON token_transactions (type, ref) WHERE ref IS NOT NULL")

    conn.commit()
    conn.close()
    logger.info("✅ База данных успешно инициализирована, все таблицы проверены.")


# ==================== ПОЛЬЗОВАТЕЛИ ====================

def get_user(user_id):
    conn = _conn()
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, username, balance, referrer_id, language, star_balance FROM users WHERE user_id = ?", (user_id,))
    res = cursor.fetchone()
    conn.close()
    return res


def create_user(user_id, username, referrer_id=None):
    conn = _conn()
    cursor = conn.cursor()
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute(
        "INSERT OR IGNORE INTO users (user_id, username, referrer_id, created_at) VALUES (?, ?, ?, ?)",
        (user_id, username, referrer_id, now)
    )
    conn.commit()
    conn.close()


def update_username(user_id, username):
    conn = _conn()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET username = ? WHERE user_id = ?", (username, user_id))
    conn.commit()
    conn.close()


def set_user_language(user_id, lang):
    conn = _conn()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET language = ? WHERE user_id = ?", (lang, user_id))
    conn.commit()
    conn.close()


def get_user_language(user_id):
    conn = _conn()
    cursor = conn.cursor()
    cursor.execute("SELECT language FROM users WHERE user_id = ?", (user_id,))
    res = cursor.fetchone()
    conn.close()
    return res[0] if res and res[0] else 'ru'


def update_balance(user_id, amount):
    conn = _conn()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user_id))
    conn.commit()
    conn.close()


# ==================== АТОМАРНОЕ СПИСАНИЕ БАЛАНСА ====================

def try_deduct_balance(user_id, amount):
    """Атомарно списывает amount с основного баланса, только если денег хватает.
    True — списано. Защищает от гонки запросов (десять одновременных покупок на один и тот же баланс):
    проверка «хватает ли денег» и списание выполняются одним SQL-запросом."""
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return False
    if amount <= 0:
        return False
    conn = _conn()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE users SET balance = balance - ? WHERE user_id = ? AND balance >= ?",
        (amount, user_id, amount)
    )
    ok = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return ok


def try_mark_nft_sold(listing_id, buyer_id, buyer_username):
    """Атомарно помечает NFT проданным, только если он ещё active. True — этот покупатель успел первым."""
    conn = _conn()
    cursor = conn.cursor()
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute(
        "UPDATE nft_listings SET status = 'sold', buyer_id = ?, buyer_username = ?, updated_at = ? "
        "WHERE id = ? AND status = 'active'",
        (buyer_id, buyer_username, now, listing_id)
    )
    ok = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return ok


# ==================== БЛОКИРОВКИ ПОЛЬЗОВАТЕЛЕЙ ====================

def is_banned(user_id):
    try:
        conn = _conn()
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM banned_users WHERE user_id = ?", (user_id,))
        res = cursor.fetchone()
        conn.close()
        return res is not None
    except Exception:
        return False


def get_ban(user_id):
    conn = _conn()
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, username, reason, banned_by, banned_at FROM banned_users WHERE user_id = ?", (user_id,))
    res = cursor.fetchone()
    conn.close()
    return res


def ban_user(user_id, username=None, reason=None, banned_by=None):
    """Блокирует пользователя по ID (работает и для тех, кого ещё нет в таблице users)."""
    conn = _conn()
    cursor = conn.cursor()
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if not username:
        cursor.execute("SELECT username FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        username = row[0] if row else None
    cursor.execute(
        "INSERT OR REPLACE INTO banned_users (user_id, username, reason, banned_by, banned_at) VALUES (?, ?, ?, ?, ?)",
        (user_id, username, (reason or "")[:300], banned_by, now)
    )
    conn.commit()
    conn.close()


def unban_user(user_id):
    conn = _conn()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM banned_users WHERE user_id = ?", (user_id,))
    ok = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return ok


def get_banned_users(limit=200):
    conn = _conn()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT user_id, username, reason, banned_by, banned_at FROM banned_users ORDER BY banned_at DESC LIMIT ?",
        (limit,)
    )
    rows = cursor.fetchall()
    conn.close()
    return rows


# ==================== ЖУРНАЛ СОБЫТИЙ БЕЗОПАСНОСТИ ====================

def add_security_event(user_id, username, kind, details, ip=None):
    try:
        conn = _conn()
        cursor = conn.cursor()
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute(
            "INSERT INTO security_events (user_id, username, kind, details, ip, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, username, kind, (details or "")[:500], ip, now)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Не удалось записать событие безопасности: {e}")


def count_security_events(user_id, since_minutes=60, kind=None):
    """Сколько событий безопасности было у пользователя за последние N минут."""
    since = (datetime.datetime.now() - datetime.timedelta(minutes=since_minutes)).strftime("%Y-%m-%d %H:%M:%S")
    conn = _conn()
    cursor = conn.cursor()
    if kind:
        cursor.execute("SELECT COUNT(*) FROM security_events WHERE user_id = ? AND kind = ? AND created_at >= ?", (user_id, kind, since))
    else:
        cursor.execute("SELECT COUNT(*) FROM security_events WHERE user_id = ? AND created_at >= ?", (user_id, since))
    n = cursor.fetchone()[0]
    conn.close()
    return n


def get_recent_security_events(limit=50):
    conn = _conn()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, user_id, username, kind, details, ip, created_at FROM security_events ORDER BY id DESC LIMIT ?",
        (limit,)
    )
    rows = cursor.fetchall()
    conn.close()
    return rows


# ==================== ТОКЕНЫ (внутренняя валюта) ====================
# Токены начисляются кэшбэком за успешные покупки и обмениваются на баланс
# (1 токен = TOKEN_RATE сум, по умолчанию 1:1). Все числа — целые.

TOKEN_DEFAULT_CASHBACK_PERCENT = 3.0   # % от суммы покупки
TOKEN_DEFAULT_BOX_PRICE = 50000        # цена тайного приза в токенах


def get_token_settings():
    """Настройки токенов (редактируются из админки через settings)."""
    def _num(key, default, cast):
        try:
            return cast(get_setting(key, default))
        except (TypeError, ValueError):
            return default
    percent = _num("token_cashback_percent", TOKEN_DEFAULT_CASHBACK_PERCENT, float)
    price = _num("token_box_price", TOKEN_DEFAULT_BOX_PRICE, int)
    return {
        "cashback_percent": max(0.0, min(percent, 50.0)),
        "box_price": max(1, price),
    }


def calc_cashback_tokens(cost):
    """Сколько токенов положено за покупку на сумму cost (сум). Округление вниз."""
    try:
        cost = float(cost)
    except (TypeError, ValueError):
        return 0
    if cost <= 0:
        return 0
    return int(cost * get_token_settings()["cashback_percent"] / 100.0)


def get_token_balance(user_id):
    conn = _conn()
    cursor = conn.cursor()
    cursor.execute("SELECT COALESCE(token_balance, 0) FROM users WHERE user_id = ?", (user_id,))
    res = cursor.fetchone()
    conn.close()
    return int(res[0]) if res and res[0] else 0


def add_tokens(user_id, amount, tx_type, description, ref=None):
    """Начисляет токены. ref + tx_type уникальны: повторный вызов с тем же ref
    ничего не делает (защита от двойного начисления). Возвращает True, если начислено."""
    amount = int(amount)
    if amount <= 0:
        return False
    conn = _conn()
    cursor = conn.cursor()
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        cursor.execute(
            "INSERT INTO token_transactions (user_id, amount, type, description, ref, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, amount, tx_type, description, ref, now)
        )
    except sqlite3.IntegrityError:
        conn.close()
        return False
    cursor.execute("UPDATE users SET token_balance = COALESCE(token_balance, 0) + ? WHERE user_id = ?", (amount, user_id))
    conn.commit()
    conn.close()
    return True


def spend_tokens(user_id, amount, tx_type, description):
    """Атомарно списывает токены. Возвращает True, если хватило."""
    amount = int(amount)
    if amount <= 0:
        return False
    conn = _conn()
    cursor = conn.cursor()
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute(
        "UPDATE users SET token_balance = token_balance - ? WHERE user_id = ? AND COALESCE(token_balance, 0) >= ?",
        (amount, user_id, amount)
    )
    if cursor.rowcount != 1:
        conn.rollback()
        conn.close()
        return False
    cursor.execute(
        "INSERT INTO token_transactions (user_id, amount, type, description, ref, timestamp) VALUES (?, ?, ?, ?, NULL, ?)",
        (user_id, -amount, tx_type, description, now)
    )
    conn.commit()
    conn.close()
    return True


def get_token_history(user_id, limit=30):
    conn = _conn()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT amount, type, description, timestamp FROM token_transactions WHERE user_id = ? ORDER BY id DESC LIMIT ?",
        (user_id, limit)
    )
    rows = cursor.fetchall()
    conn.close()
    return rows


# ==================== ТАЙНЫЙ ПРИЗ ====================

def get_mystery_prizes(only_active=False):
    conn = _conn()
    cursor = conn.cursor()
    q = "SELECT id, type, label, weight, payload FROM mystery_box_prizes"
    if only_active:
        q += " WHERE weight > 0"
    q += " ORDER BY id ASC"
    cursor.execute(q)
    rows = cursor.fetchall()
    conn.close()
    return rows


def add_mystery_prize(prize_type, label, weight, payload):
    conn = _conn()
    cursor = conn.cursor()
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute(
        "INSERT INTO mystery_box_prizes (type, label, weight, payload, created_at) VALUES (?, ?, ?, ?, ?)",
        (prize_type, label, weight, payload, now)
    )
    conn.commit()
    new_id = cursor.lastrowid
    conn.close()
    return new_id


def update_mystery_prize(prize_id, label, weight, payload):
    conn = _conn()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE mystery_box_prizes SET label=?, weight=?, payload=? WHERE id=?",
        (label, weight, payload, prize_id)
    )
    conn.commit()
    conn.close()


def delete_mystery_prize(prize_id):
    conn = _conn()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM mystery_box_prizes WHERE id=?", (prize_id,))
    conn.commit()
    conn.close()


def add_mystery_opening(user_id, username, prize_id, prize_type, prize_label, result_text, price, status="completed"):
    conn = _conn()
    cursor = conn.cursor()
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute(
        "INSERT INTO mystery_box_openings (user_id, username, prize_id, prize_type, prize_label, result_text, price, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (user_id, username, prize_id, prize_type, prize_label, result_text, price, status, now)
    )
    conn.commit()
    new_id = cursor.lastrowid
    conn.close()
    return new_id


def get_mystery_opening(opening_id):
    conn = _conn()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, user_id, username, prize_type, prize_label, result_text, status, created_at FROM mystery_box_openings WHERE id = ?",
        (opening_id,)
    )
    res = cursor.fetchone()
    conn.close()
    return res


def get_pending_mystery_openings():
    conn = _conn()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, user_id, username, prize_type, prize_label, result_text, status, created_at "
        "FROM mystery_box_openings WHERE status = 'pending_admin' ORDER BY id ASC"
    )
    rows = cursor.fetchall()
    conn.close()
    return rows


def set_mystery_opening_status(opening_id, new_status, old_status=None):
    conn = _conn()
    cursor = conn.cursor()
    if old_status:
        cursor.execute("UPDATE mystery_box_openings SET status=? WHERE id=? AND status=?", (new_status, opening_id, old_status))
    else:
        cursor.execute("UPDATE mystery_box_openings SET status=? WHERE id=?", (new_status, opening_id))
    changed = cursor.rowcount
    conn.commit()
    conn.close()
    return changed == 1


def get_recent_mystery_openings(limit=30):
    conn = _conn()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, user_id, username, prize_type, prize_label, result_text, status, created_at "
        "FROM mystery_box_openings ORDER BY id DESC LIMIT ?",
        (limit,)
    )
    rows = cursor.fetchall()
    conn.close()
    return rows


def search_users(query, limit=20):
    """Ищет пользователей по user_id (точное совпадение/префикс) или username (подстрока). Для админ-панели."""
    conn = _conn()
    cursor = conn.cursor()
    query = (query or "").strip().lstrip('@')
    if query.isdigit():
        cursor.execute(
            "SELECT user_id, username, balance, referrer_id, created_at FROM users WHERE CAST(user_id AS TEXT) LIKE ? ORDER BY user_id LIMIT ?",
            (f"{query}%", limit)
        )
    else:
        cursor.execute(
            "SELECT user_id, username, balance, referrer_id, created_at FROM users WHERE username LIKE ? ORDER BY created_at DESC LIMIT ?",
            (f"%{query}%", limit)
        )
    rows = cursor.fetchall()
    conn.close()
    return rows


def get_all_user_ids():
    """Все user_id из таблицы users — для рассылки сообщений всем, кто нажимал /start."""
    conn = _conn()
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM users")
    rows = [r[0] for r in cursor.fetchall()]
    conn.close()
    return rows


def get_recent_users(limit=20):
    conn = _conn()
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, username, balance, referrer_id, created_at FROM users ORDER BY created_at DESC LIMIT ?", (limit,))
    rows = cursor.fetchall()
    conn.close()
    return rows


# ==================== ТРАНЗАКЦИИ ====================

def add_transaction(user_id, amount, tx_type, status, description):
    conn = _conn()
    cursor = conn.cursor()
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute(
        "INSERT INTO transactions (user_id, amount, type, status, description, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, amount, tx_type, status, description, now)
    )
    conn.commit()
    last_id = cursor.lastrowid
    conn.close()
    return last_id


def mark_transaction_status(user_id, tx_type, old_status, new_status):
    conn = _conn()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE transactions SET status=? WHERE user_id=? AND type=? AND status=? "
        "AND id = (SELECT id FROM transactions WHERE user_id=? AND type=? AND status=? ORDER BY id DESC LIMIT 1)",
        (new_status, user_id, tx_type, old_status, user_id, tx_type, old_status)
    )
    conn.commit()
    conn.close()


def get_recent_transactions(limit=50, tx_type=None, offset=0):
    """Общая лента событий для мониторинга в админке: покупки, пополнения,
    ручные корректировки баланса — всё, что попадает в таблицу transactions,
    вместе с username пользователя."""
    conn = _conn()
    cursor = conn.cursor()
    if tx_type:
        cursor.execute(
            "SELECT t.id, t.user_id, u.username, t.amount, t.type, t.status, t.description, t.timestamp "
            "FROM transactions t LEFT JOIN users u ON u.user_id = t.user_id "
            "WHERE t.type = ? ORDER BY t.id DESC LIMIT ? OFFSET ?",
            (tx_type, limit, offset)
        )
    else:
        cursor.execute(
            "SELECT t.id, t.user_id, u.username, t.amount, t.type, t.status, t.description, t.timestamp "
            "FROM transactions t LEFT JOIN users u ON u.user_id = t.user_id "
            "ORDER BY t.id DESC LIMIT ? OFFSET ?",
            (limit, offset)
        )
    rows = cursor.fetchall()
    conn.close()
    return rows


def count_successful_purchases(user_id):
    conn = _conn()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT COUNT(*) FROM transactions WHERE user_id = ? AND status = 'success' AND type IN ('buy_stars', 'buy_premium')",
        (user_id,)
    )
    n = cursor.fetchone()[0]
    conn.close()
    return n


def already_rewarded_for(referrer_id, buyer_id):
    conn = _conn()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT COUNT(*) FROM transactions WHERE user_id = ? AND type = 'referral_bonus' AND description LIKE ?",
        (referrer_id, f"%{buyer_id}%")
    )
    n = cursor.fetchone()[0]
    conn.close()
    return n > 0


# ==================== РЕФЕРАЛЫ ====================

def get_referral_stats(user_id):
    conn = _conn()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM users WHERE referrer_id = ?", (user_id,))
    count = cursor.fetchone()[0] or 0
    cursor.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE user_id = ? AND type = 'referral_bonus'",
        (user_id,)
    )
    earned = cursor.fetchone()[0] or 0.0
    conn.close()
    return {"count": count, "total_earned": float(earned)}


def get_referral_list(user_id, limit=20):
    conn = _conn()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT username, created_at FROM users WHERE referrer_id = ? ORDER BY created_at DESC LIMIT ?",
        (user_id, limit)
    )
    rows = cursor.fetchall()
    conn.close()
    return rows


# ==================== СЕССИИ (оставлено для совместимости, в мини-аппе почти не нужно) ====================

def set_session_data(user_id, data):
    conn = _conn()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT OR REPLACE INTO sessions (user_id, data) VALUES (?, ?)",
        (user_id, json.dumps(data))
    )
    conn.commit()
    conn.close()


def get_session_data(user_id):
    conn = _conn()
    cursor = conn.cursor()
    cursor.execute("SELECT data FROM sessions WHERE user_id = ?", (user_id,))
    res = cursor.fetchone()
    conn.close()
    if res:
        try:
            return json.loads(res[0])
        except Exception:
            return {}
    return {}


def delete_session_data(user_id):
    conn = _conn()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


# ==================== FREEDOM PAY ====================

def add_payment(user_id, amount, payment_id, status="pending"):
    conn = _conn()
    cursor = conn.cursor()
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute(
        "INSERT OR IGNORE INTO payments (user_id, amount, payment_id, status, timestamp) VALUES (?, ?, ?, ?, ?)",
        (user_id, amount, payment_id, status, now)
    )
    conn.commit()
    conn.close()


def get_payment(payment_id):
    conn = _conn()
    cursor = conn.cursor()
    cursor.execute("SELECT id, user_id, amount, payment_id, status FROM payments WHERE payment_id = ?", (payment_id,))
    res = cursor.fetchone()
    conn.close()
    return res


def update_payment_status(payment_id, status):
    conn = _conn()
    cursor = conn.cursor()
    cursor.execute("UPDATE payments SET status = ? WHERE payment_id = ?", (status, payment_id))
    conn.commit()
    conn.close()


# ==================== РУЧНЫЕ ПОПОЛНЕНИЯ (модерация в админ-панели мини-аппа) ====================

def add_manual_deposit(user_id, username, amount, photo_b64):
    conn = _conn()
    cursor = conn.cursor()
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute(
        "INSERT INTO manual_deposits (user_id, username, amount, photo, status, created_at) VALUES (?, ?, ?, ?, 'pending', ?)",
        (user_id, username, amount, photo_b64, now)
    )
    conn.commit()
    new_id = cursor.lastrowid
    conn.close()
    return new_id


def get_pending_manual_deposits():
    conn = _conn()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, user_id, username, amount, photo, created_at FROM manual_deposits WHERE status = 'pending' ORDER BY id ASC"
    )
    rows = cursor.fetchall()
    conn.close()
    return rows


def get_manual_deposit(deposit_id):
    conn = _conn()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, user_id, username, amount, photo, status FROM manual_deposits WHERE id = ?",
        (deposit_id,)
    )
    res = cursor.fetchone()
    conn.close()
    return res


def set_manual_deposit_status(deposit_id, status, admin_id):
    conn = _conn()
    cursor = conn.cursor()
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute(
        "UPDATE manual_deposits SET status = ?, admin_id = ?, decided_at = ? WHERE id = ?",
        (status, admin_id, now, deposit_id)
    )
    conn.commit()
    conn.close()


# ==================== TON КУРС / СЧЁТЧИК ЗАКАЗОВ ====================

def set_ton_rate(rate):
    conn = _conn()
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('ton_rate', ?)", (str(rate),))
    conn.commit()
    conn.close()


def get_ton_rate():
    conn = _conn()
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM settings WHERE key = 'ton_rate'")
    res = cursor.fetchone()
    conn.close()
    return float(res[0]) if res else 75000.0


def get_setting(key, default=None):
    """Универсальный доступ к таблице settings — для редактируемых из админки цен
    (star_price, premium_prices, product_prices и т.п.). Значение хранится как TEXT
    (обычно JSON), парсит сам вызывающий код."""
    conn = _conn()
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
    res = cursor.fetchone()
    conn.close()
    return res[0] if res else default


def set_setting(key, value):
    conn = _conn()
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
    conn.commit()
    conn.close()


def get_next_order_id():
    conn = _conn()
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM settings WHERE key = 'order_counter'")
    res = cursor.fetchone()
    current = int(res[0]) if res else 0
    next_id = current + 1
    cursor.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('order_counter', ?)", (str(next_id),))
    conn.commit()
    conn.close()
    return next_id


# ==================== ОТЗЫВЫ ====================

def add_review(user_id, order_id, product, text):
    conn = _conn()
    cursor = conn.cursor()
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute(
        "INSERT INTO reviews (user_id, order_id, product, text, created_at) VALUES (?, ?, ?, ?, ?)",
        (user_id, order_id, product, text, now)
    )
    conn.commit()
    conn.close()


# ==================== СТАТИСТИКА ДЛЯ АДМИН-ПАНЕЛИ ====================

def get_admin_stats(exclude_user_ids=None):
    """Сводка для админки.

    exclude_user_ids — id, чей баланс не входит в «Баланс всех» (админы/разработчик):
    их баланс — служебный, а не деньги клиентов.
    Выручка и число заказов за сегодня считают не только звёзды/Premium,
    но и донаты в игры (game_orders; отменённые/возвращённые не учитываются).
    """
    conn = _conn()
    cursor = conn.cursor()
    exclude_user_ids = [int(i) for i in (exclude_user_ids or [])]

    cursor.execute("SELECT COUNT(*) FROM users")
    total_users = cursor.fetchone()[0]

    if exclude_user_ids:
        ph = ",".join("?" * len(exclude_user_ids))
        cursor.execute(f"SELECT COALESCE(SUM(balance),0) FROM users WHERE user_id NOT IN ({ph})", exclude_user_ids)
    else:
        cursor.execute("SELECT COALESCE(SUM(balance),0) FROM users")
    total_balance = cursor.fetchone()[0]

    today = datetime.datetime.now().strftime("%Y-%m-%d")
    cursor.execute(
        "SELECT COUNT(*), COALESCE(SUM(-amount),0) FROM transactions "
        "WHERE type IN ('buy_stars','buy_premium') AND status='success' AND timestamp LIKE ?",
        (f"{today}%",)
    )
    orders_stars, revenue_stars = cursor.fetchone()

    # Игровые донаты: платные заказы (price > 0), кроме отменённых (по ним деньги вернули)
    cursor.execute(
        "SELECT COUNT(*), COALESCE(SUM(price),0) FROM game_orders "
        "WHERE price > 0 AND status != 'cancelled' AND created_at LIKE ?",
        (f"{today}%",)
    )
    orders_games, revenue_games = cursor.fetchone()

    cursor.execute("SELECT COUNT(*) FROM manual_deposits WHERE status='pending'")
    pending_deposits = cursor.fetchone()[0]

    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute("SELECT COUNT(*) FROM card_auto_deposits WHERE status='pending' AND expires_at > ?", (now,))
    pending_card_auto = cursor.fetchone()[0]

    conn.close()
    return {
        "total_users": total_users or 0,
        "total_balance": float(total_balance or 0),
        "orders_today": (orders_stars or 0) + (orders_games or 0),
        "revenue_today": float(revenue_stars or 0) + float(revenue_games or 0),
        "revenue_stars_today": float(revenue_stars or 0),
        "revenue_games_today": float(revenue_games or 0),
        "pending_deposits": pending_deposits or 0,
        "pending_card_auto": pending_card_auto or 0,
    }


# ==================== АВТОПЛАТЁЖ ПО КАРТАМ (SMS-подтверждение) ====================

def expire_stale_card_deposits():
    conn = _conn()
    cursor = conn.cursor()
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute("UPDATE card_auto_deposits SET status='expired' WHERE status='pending' AND expires_at < ?", (now,))
    conn.commit()
    conn.close()


def get_active_card_amounts(card_id):
    """Суммы всех ещё не истёкших pending-заявок на эту карту — чтобы не сгенерировать дубликат."""
    conn = _conn()
    cursor = conn.cursor()
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute(
        "SELECT exact_amount FROM card_auto_deposits WHERE card_id=? AND status='pending' AND expires_at > ?",
        (card_id, now)
    )
    rows = cursor.fetchall()
    conn.close()
    return {r[0] for r in rows}


def create_card_auto_deposit(user_id, username, requested_amount, exact_amount, card_id, ttl_minutes):
    conn = _conn()
    cursor = conn.cursor()
    now_dt = datetime.datetime.now()
    created_at = now_dt.strftime("%Y-%m-%d %H:%M:%S")
    expires_at = (now_dt + datetime.timedelta(minutes=ttl_minutes)).strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute(
        "INSERT INTO card_auto_deposits (user_id, username, requested_amount, exact_amount, card_id, status, created_at, expires_at) "
        "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)",
        (user_id, username, requested_amount, exact_amount, card_id, created_at, expires_at)
    )
    conn.commit()
    new_id = cursor.lastrowid
    conn.close()
    return new_id, expires_at


def get_card_deposit(deposit_id):
    conn = _conn()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, user_id, username, requested_amount, exact_amount, card_id, status, expires_at FROM card_auto_deposits WHERE id = ?",
        (deposit_id,)
    )
    res = cursor.fetchone()
    conn.close()
    return res


def find_pending_card_match(amount, card_id=None):
    """Ищет pending-заявку с такой точной суммой. Если известна карта — приоритет ей."""
    conn = _conn()
    cursor = conn.cursor()
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if card_id:
        cursor.execute(
            "SELECT id, user_id, username, exact_amount, card_id FROM card_auto_deposits "
            "WHERE status='pending' AND expires_at > ? AND exact_amount = ? AND card_id = ? ORDER BY id ASC LIMIT 1",
            (now, amount, card_id)
        )
        res = cursor.fetchone()
        if res:
            conn.close()
            return res
    cursor.execute(
        "SELECT id, user_id, username, exact_amount, card_id FROM card_auto_deposits "
        "WHERE status='pending' AND expires_at > ? AND exact_amount = ? ORDER BY id ASC LIMIT 1",
        (now, amount)
    )
    res = cursor.fetchone()
    conn.close()
    return res


def confirm_card_deposit(deposit_id, sms_text=None, admin_id=None):
    conn = _conn()
    cursor = conn.cursor()
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute(
        "UPDATE card_auto_deposits SET status='confirmed', sms_text=?, admin_id=?, confirmed_at=? WHERE id=?",
        (sms_text, admin_id, now, deposit_id)
    )
    conn.commit()
    conn.close()


def cancel_card_deposit(deposit_id, admin_id=None):
    conn = _conn()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE card_auto_deposits SET status='cancelled', admin_id=? WHERE id=? AND status='pending'",
        (admin_id, deposit_id)
    )
    conn.commit()
    conn.close()


def get_pending_card_deposits():
    conn = _conn()
    cursor = conn.cursor()
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute(
        "SELECT id, user_id, username, requested_amount, exact_amount, card_id, created_at, expires_at "
        "FROM card_auto_deposits WHERE status='pending' AND expires_at > ? ORDER BY id ASC",
        (now,)
    )
    rows = cursor.fetchall()
    conn.close()
    return rows


# ==================== ИГРОВЫЕ ЗАЯВКИ (Brawl Stars, Roblox, Standoff...) ====================

def create_game_order(user_id, username, category, product_id, product_name, price, extra=None, status="new"):
    conn = _conn()
    cursor = conn.cursor()
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    extra_json = json.dumps(extra, ensure_ascii=False) if extra is not None else None
    cursor.execute(
        "INSERT INTO game_orders (user_id, username, category, product_id, product_name, price, extra, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (user_id, username, category, product_id, product_name, price, extra_json, status, now, now)
    )
    conn.commit()
    new_id = cursor.lastrowid
    conn.close()
    return new_id


def get_game_order(order_id):
    conn = _conn()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, user_id, username, category, product_id, product_name, price, extra, status "
        "FROM game_orders WHERE id = ?",
        (order_id,)
    )
    res = cursor.fetchone()
    conn.close()
    return res


def set_game_order_extra(order_id, extra, status):
    conn = _conn()
    cursor = conn.cursor()
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    extra_json = json.dumps(extra, ensure_ascii=False)
    cursor.execute(
        "UPDATE game_orders SET extra = ?, status = ?, updated_at = ? WHERE id = ?",
        (extra_json, status, now, order_id)
    )
    conn.commit()
    conn.close()


def set_game_order_status(order_id, status, admin_id=None):
    conn = _conn()
    cursor = conn.cursor()
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute(
        "UPDATE game_orders SET status = ?, admin_id = ?, updated_at = ? WHERE id = ?",
        (status, admin_id, now, order_id)
    )
    conn.commit()
    conn.close()


def get_pending_game_orders(category=None):
    """Заявки, ожидающие решения администратора (уже с доп. данными от юзера)."""
    conn = _conn()
    cursor = conn.cursor()
    if category:
        cursor.execute(
            "SELECT id, user_id, username, category, product_id, product_name, price, extra, created_at "
            "FROM game_orders WHERE status = 'pending_admin' AND category = ? ORDER BY id ASC",
            (category,)
        )
    else:
        cursor.execute(
            "SELECT id, user_id, username, category, product_id, product_name, price, extra, created_at "
            "FROM game_orders WHERE status = 'pending_admin' ORDER BY id ASC"
        )
    rows = cursor.fetchall()
    conn.close()
    return rows


def get_active_game_orders(category=None):
    """Заявки на любом активном этапе (не done/cancelled) — для админ-панели «Игры».
    В отличие от get_pending_game_orders (только 'pending_admin'), сюда попадают
    и промежуточные статусы многошаговых сделок (например, Standoff Golda:
    awaiting_screenshot -> screenshot_review -> in_progress -> bought -> transferred)."""
    statuses = ('pending_admin', 'screenshot_review', 'in_progress', 'bought', 'transferred')
    conn = _conn()
    cursor = conn.cursor()
    placeholders = ",".join("?" for _ in statuses)
    if category:
        cursor.execute(
            f"SELECT id, user_id, username, category, product_id, product_name, price, extra, status, created_at "
            f"FROM game_orders WHERE status IN ({placeholders}) AND category = ? ORDER BY id ASC",
            (*statuses, category)
        )
    else:
        cursor.execute(
            f"SELECT id, user_id, username, category, product_id, product_name, price, extra, status, created_at "
            f"FROM game_orders WHERE status IN ({placeholders}) ORDER BY id ASC",
            statuses
        )
    rows = cursor.fetchall()
    conn.close()
    return rows


def get_user_game_orders(user_id, limit=50):
    """Все игровые заявки конкретного пользователя, для вкладки «Заказы» в Mini App —
    позволяет пользователю вернуться к незавершённому шагу (ввод логина, скриншот),
    даже если он закрыл приложение между шагами."""
    conn = _conn()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, category, product_id, product_name, price, extra, status, created_at, updated_at "
        "FROM game_orders WHERE user_id = ? ORDER BY id DESC LIMIT ?",
        (user_id, limit)
    )
    rows = cursor.fetchall()
    conn.close()
    return rows


# ==================== NFT-МАРКЕТПЛЕЙС ====================

def create_nft_listing(seller_id, seller_username, nft_name, nft_photo, price):
    conn = _conn()
    cursor = conn.cursor()
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute(
        "INSERT INTO nft_listings (seller_id, seller_username, nft_name, nft_photo, price, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, 'pending_admin', ?, ?)",
        (seller_id, seller_username, nft_name, nft_photo, price, now, now)
    )
    conn.commit()
    new_id = cursor.lastrowid
    conn.close()
    return new_id


def get_nft_listing(listing_id):
    conn = _conn()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, seller_id, seller_username, nft_name, nft_photo, price, status, buyer_id, buyer_username, created_at "
        "FROM nft_listings WHERE id = ?",
        (listing_id,)
    )
    res = cursor.fetchone()
    conn.close()
    return res


def set_nft_listing_status(listing_id, status, admin_id=None):
    conn = _conn()
    cursor = conn.cursor()
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute(
        "UPDATE nft_listings SET status = ?, admin_id = ?, updated_at = ? WHERE id = ?",
        (status, admin_id, now, listing_id)
    )
    conn.commit()
    conn.close()


def set_nft_listing_sold(listing_id, buyer_id, buyer_username):
    conn = _conn()
    cursor = conn.cursor()
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute(
        "UPDATE nft_listings SET status = 'sold', buyer_id = ?, buyer_username = ?, updated_at = ? WHERE id = ?",
        (buyer_id, buyer_username, now, listing_id)
    )
    conn.commit()
    conn.close()


def get_active_nft_listings():
    """Опубликованные объявления — видны всем на маркете."""
    conn = _conn()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, seller_id, seller_username, nft_name, nft_photo, price, created_at "
        "FROM nft_listings WHERE status = 'active' ORDER BY id DESC"
    )
    rows = cursor.fetchall()
    conn.close()
    return rows


def get_pending_nft_listings():
    """Заявки, которые ждут действия администратора (новые + присланные на проверку)."""
    conn = _conn()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, seller_id, seller_username, nft_name, nft_photo, price, status, created_at "
        "FROM nft_listings WHERE status IN ('pending_admin', 'awaiting_transfer', 'verification') "
        "ORDER BY id ASC"
    )
    rows = cursor.fetchall()
    conn.close()
    return rows


def get_user_nft_listings(user_id, limit=50):
    """Заявки конкретного продавца — для вкладки «Заказы» (резюме незавершённого шага)."""
    conn = _conn()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, nft_name, nft_photo, price, status, buyer_username, created_at, updated_at "
        "FROM nft_listings WHERE seller_id = ? ORDER BY id DESC LIMIT ?",
        (user_id, limit)
    )
    rows = cursor.fetchall()
    conn.close()
    return rows


def get_user_nft_purchases(user_id, limit=50):
    """История покупок NFT конкретным пользователем — для вкладки «Заказы»."""
    conn = _conn()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, nft_name, nft_photo, price, seller_username, updated_at "
        "FROM nft_listings WHERE buyer_id = ? AND status = 'sold' ORDER BY id DESC LIMIT ?",
        (user_id, limit)
    )
    rows = cursor.fetchall()
    conn.close()
    return rows


def get_nft_withdrawable_balance(user_id):
    """Сколько денег пользователь может вывести — только то, что он реально
    заработал продажей своих NFT на маркете, за вычетом уже выведенных (в том
    числе ещё не подтверждённых, но уже отправленных на модерацию) сумм.
    Деньги, пополненные картой/TON или полученные другим способом, вывести
    нельзя — вывод доступен только на сумму заработка с продаж NFT."""
    conn = _conn()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE user_id = ? AND type = 'nft_sale' AND status = 'success'",
        (user_id,)
    )
    earned = cursor.fetchone()[0] or 0.0
    cursor.execute(
        "SELECT COALESCE(SUM(-amount), 0) FROM transactions WHERE user_id = ? AND type = 'withdrawal' AND status IN ('pending', 'success')",
        (user_id,)
    )
    withdrawn = cursor.fetchone()[0] or 0.0
    conn.close()
    return max(0.0, earned - withdrawn)


# ==================== ВЫВОД БАЛАНСА ====================

def create_withdrawal(user_id, username, amount, details):
    conn = _conn()
    cursor = conn.cursor()
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute(
        "INSERT INTO withdrawals (user_id, username, amount, details, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, 'pending', ?, ?)",
        (user_id, username, amount, details, now, now)
    )
    conn.commit()
    new_id = cursor.lastrowid
    conn.close()
    return new_id


def get_withdrawal(withdrawal_id):
    conn = _conn()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, user_id, username, amount, details, status, created_at FROM withdrawals WHERE id = ?",
        (withdrawal_id,)
    )
    res = cursor.fetchone()
    conn.close()
    return res


def set_withdrawal_status(withdrawal_id, status, admin_id=None):
    conn = _conn()
    cursor = conn.cursor()
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute(
        "UPDATE withdrawals SET status = ?, admin_id = ?, updated_at = ? WHERE id = ?",
        (status, admin_id, now, withdrawal_id)
    )
    conn.commit()
    conn.close()


def get_pending_withdrawals():
    conn = _conn()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, user_id, username, amount, details, created_at FROM withdrawals "
        "WHERE status = 'pending' ORDER BY id ASC"
    )
    rows = cursor.fetchall()
    conn.close()
    return rows


def get_user_withdrawals(user_id, limit=50):
    conn = _conn()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, amount, details, status, created_at FROM withdrawals "
        "WHERE user_id = ? ORDER BY id DESC LIMIT ?",
        (user_id, limit)
    )
    rows = cursor.fetchall()
    conn.close()
    return rows


# ==================== ЕЖЕДНЕВНЫЕ КАРТОЧКИ ====================

def get_daily_card_prizes(only_active=False):
    conn = _conn()
    cursor = conn.cursor()
    q = "SELECT id, type, label, weight, payload FROM daily_card_prizes"
    if only_active:
        q += " WHERE weight > 0"
    q += " ORDER BY id ASC"
    cursor.execute(q)
    rows = cursor.fetchall()
    conn.close()
    return rows


def add_daily_card_prize(prize_type, label, weight, payload):
    conn = _conn()
    cursor = conn.cursor()
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute(
        "INSERT INTO daily_card_prizes (type, label, weight, payload, created_at) VALUES (?, ?, ?, ?, ?)",
        (prize_type, label, weight, payload, now)
    )
    conn.commit()
    new_id = cursor.lastrowid
    conn.close()
    return new_id


def update_daily_card_prize(prize_id, label, weight, payload):
    conn = _conn()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE daily_card_prizes SET label=?, weight=?, payload=? WHERE id=?",
        (label, weight, payload, prize_id)
    )
    conn.commit()
    conn.close()


def delete_daily_card_prize(prize_id):
    conn = _conn()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM daily_card_prizes WHERE id=?", (prize_id,))
    conn.commit()
    conn.close()


def get_last_daily_card_claim(user_id):
    conn = _conn()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT claim_date, result_text FROM daily_card_claims WHERE user_id=? ORDER BY id DESC LIMIT 1",
        (user_id,)
    )
    row = cursor.fetchone()
    conn.close()
    return row


def get_daily_card_claims_count_since(user_id, since_dt_str):
    """Сколько раз пользователь открывал ежедневную карточку с указанного момента
    времени (используется для скользящего окна в 24 часа, а не календарного дня)."""
    conn = _conn()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT COUNT(*) FROM daily_card_claims WHERE user_id=? AND created_at>=?",
        (user_id, since_dt_str)
    )
    count = cursor.fetchone()[0]
    conn.close()
    return count


def get_last_extra_open_purchase(user_id):
    """Время последней докупки доп. открытий ежедневной карточки (или None)."""
    conn = _conn()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT purchased_at FROM daily_card_extra_purchases WHERE user_id=? ORDER BY id DESC LIMIT 1",
        (user_id,)
    )
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None


def add_extra_open_purchase(user_id):
    conn = _conn()
    cursor = conn.cursor()
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute(
        "INSERT INTO daily_card_extra_purchases (user_id, purchased_at) VALUES (?, ?)",
        (user_id, now)
    )
    conn.commit()
    conn.close()


def add_daily_card_claim(user_id, username, claim_date, prize_id, prize_type, prize_label, result_text, status="completed"):
    conn = _conn()
    cursor = conn.cursor()
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute(
        "INSERT INTO daily_card_claims (user_id, username, claim_date, prize_id, prize_type, prize_label, "
        "result_text, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (user_id, username, claim_date, prize_id, prize_type, prize_label, result_text, status, now)
    )
    conn.commit()
    new_id = cursor.lastrowid
    conn.close()
    return new_id


def get_recent_daily_card_claims(limit=100, q=None):
    """Журнал открытий ежедневной карточки для админки: кто, что получил, когда.
    q — необязательный поиск по username или user_id."""
    conn = _conn()
    cursor = conn.cursor()
    if q:
        like = f"%{q}%"
        cursor.execute(
            "SELECT id, user_id, username, prize_type, prize_label, result_text, status, created_at "
            "FROM daily_card_claims WHERE username LIKE ? OR CAST(user_id AS TEXT) LIKE ? "
            "ORDER BY id DESC LIMIT ?",
            (like, like, limit)
        )
    else:
        cursor.execute(
            "SELECT id, user_id, username, prize_type, prize_label, result_text, status, created_at "
            "FROM daily_card_claims ORDER BY id DESC LIMIT ?",
            (limit,)
        )
    rows = cursor.fetchall()
    conn.close()
    return rows


def get_daily_card_claims_stats_today():
    """Сколько открытий сегодня всего и по типам приза — для шапки мониторинга в админке."""
    conn = _conn()
    cursor = conn.cursor()
    today = datetime.date.today().isoformat()
    cursor.execute("SELECT COUNT(*) FROM daily_card_claims WHERE claim_date=?", (today,))
    total_today = cursor.fetchone()[0]
    cursor.execute(
        "SELECT prize_type, COUNT(*) FROM daily_card_claims WHERE claim_date=? GROUP BY prize_type",
        (today,)
    )
    by_type = dict(cursor.fetchall())
    conn.close()
    return {"total_today": total_today, "by_type": by_type}


def get_pending_nft_card_claims(limit=50):
    conn = _conn()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, user_id, username, prize_label, created_at FROM daily_card_claims "
        "WHERE prize_type = 'nft' AND status = 'pending_admin' ORDER BY id ASC LIMIT ?",
        (limit,)
    )
    rows = cursor.fetchall()
    conn.close()
    return rows


def get_daily_card_claim_by_id(claim_id):
    conn = _conn()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, user_id, username, prize_type, prize_label, status FROM daily_card_claims WHERE id=?",
        (claim_id,)
    )
    row = cursor.fetchone()
    conn.close()
    return row


def set_daily_card_claim_status(claim_id, status):
    conn = _conn()
    cursor = conn.cursor()
    cursor.execute("UPDATE daily_card_claims SET status=? WHERE id=?", (status, claim_id))
    conn.commit()
    conn.close()


def add_user_discount(user_id, percent, category, product_id="all", product_name="", expires_at=None):
    conn = _conn()
    cursor = conn.cursor()
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute(
        "INSERT INTO user_discounts (user_id, percent, category, product_id, product_name, used, created_at, expires_at) "
        "VALUES (?, ?, ?, ?, ?, 0, ?, ?)",
        (user_id, percent, category, product_id or "all", product_name or "", now, expires_at)
    )
    conn.commit()
    conn.close()


def get_active_user_discount(user_id, category, product_id=None):
    """Возвращает лучшую подходящую активную неиспользованную скидку пользователя.
    Сначала проверяются скидки конкретно на этот товар (product_id),
    затем скидки на все товары категории ('all'), затем общие ('any')."""
    conn = _conn()
    cursor = conn.cursor()
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 1. Точное совпадение по товару в категории или 'any'
    if product_id:
        cursor.execute(
            "SELECT id, percent, category, product_id, product_name, expires_at FROM user_discounts "
            "WHERE user_id=? AND used=0 AND (expires_at IS NULL OR expires_at > ?) "
            "AND (category=? OR category='any') AND product_id=? "
            "ORDER BY percent DESC LIMIT 1",
            (user_id, now, category, product_id)
        )
        row = cursor.fetchone()
        if row:
            conn.close()
            return row

    # 2. Скидка на все товары в категории или 'any'
    cursor.execute(
        "SELECT id, percent, category, product_id, product_name, expires_at FROM user_discounts "
        "WHERE user_id=? AND used=0 AND (expires_at IS NULL OR expires_at > ?) "
        "AND (category=? OR category='any') AND (product_id='all' OR product_id IS NULL OR product_id='') "
        "ORDER BY percent DESC LIMIT 1",
        (user_id, now, category)
    )
    row = cursor.fetchone()
    conn.close()
    return row


def get_active_user_discounts(user_id):
    """Возвращает список всех активных неиспользованных и неистекших скидок пользователя."""
    conn = _conn()
    cursor = conn.cursor()
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute(
        "SELECT id, percent, category, product_id, product_name, created_at, expires_at FROM user_discounts "
        "WHERE user_id=? AND used=0 AND (expires_at IS NULL OR expires_at > ?) "
        "ORDER BY percent DESC, id DESC",
        (user_id, now)
    )
    rows = cursor.fetchall()
    conn.close()
    return rows


def mark_user_discount_used(discount_id):
    conn = _conn()
    cursor = conn.cursor()
    cursor.execute("UPDATE user_discounts SET used=1 WHERE id=?", (discount_id,))
    conn.commit()
    conn.close()


# ==================== ЗВЁЗДНЫЙ БАЛАНС (отдельно от денежного) ====================

def get_star_balance(user_id):
    conn = _conn()
    cursor = conn.cursor()
    cursor.execute("SELECT star_balance FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row and row[0] is not None else 0


def try_deduct_star_balance(user_id, qty):
    """Атомарно списывает qty звёзд, только если их хватает. True — списано.
    Защищает от двойного вывода (двойной тап / два запроса одновременно)."""
    conn = _conn()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE users SET star_balance = COALESCE(star_balance, 0) - ? "
        "WHERE user_id = ? AND COALESCE(star_balance, 0) >= ?", (qty, user_id, qty)
    )
    ok = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return ok


def update_star_balance(user_id, delta):
    conn = _conn()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET star_balance = COALESCE(star_balance, 0) + ? WHERE user_id = ?", (delta, user_id))
    conn.commit()
    conn.close()


# ==================== АДМИН: ФИЛЬТРЫ ПОЛЬЗОВАТЕЛЕЙ, ИСТОРИЯ, МОНИТОРИНГ ====================

def list_users_filtered(mode="all", q=None, limit=30, offset=0):
    """Список пользователей для «Админ → Пользователи».

    mode:
      all  — все пользователи (новые сверху);
      high — только с балансом > 0, от самого большого к меньшему;
      low  — только с балансом > 0, от самого маленького к большему.
    q — необязательный поиск по ID (префикс) или username (подстрока), работает вместе с фильтром.
    Возвращает (rows, total): rows — (user_id, username, balance, referrer_id, created_at).
    """
    where, params = [], []
    q = (q or "").strip().lstrip("@")
    if q:
        if q.isdigit():
            where.append("CAST(user_id AS TEXT) LIKE ?")
            params.append(f"{q}%")
        else:
            where.append("username LIKE ?")
            params.append(f"%{q}%")
    if mode in ("high", "low"):
        where.append("COALESCE(balance, 0) > 0")
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    if mode == "high":
        order = "ORDER BY balance DESC, user_id"
    elif mode == "low":
        order = "ORDER BY balance ASC, user_id"
    else:
        order = "ORDER BY created_at DESC, user_id DESC"

    conn = _conn()
    cursor = conn.cursor()
    cursor.execute(f"SELECT COUNT(*) FROM users {where_sql}", params)
    total = cursor.fetchone()[0]
    cursor.execute(
        f"SELECT user_id, username, balance, referrer_id, created_at FROM users {where_sql} {order} LIMIT ? OFFSET ?",
        params + [int(limit), int(offset)]
    )
    rows = cursor.fetchall()
    conn.close()
    return rows, total


GAME_CATEGORY_TITLES = {"brawlstars": "Brawl Stars", "roblox": "Roblox", "standoff": "Standoff 2", "steam": "Steam"}


def get_user_history(user_id, limit=200):
    """Полная история активности одного пользователя: пополнения, покупки, игровые
    донаты (с названием игры и товара), NFT, выводы, ручные корректировки, возвраты,
    заявки на пополнение «в ожидании/отклонено» и операции с токенами.
    Возвращает список событий, новые сверху:
      {kind, title, detail, amount, unit, status, timestamp}
    kind — группа для фильтра: deposit | spend | game | other.
    """
    conn = _conn()
    cursor = conn.cursor()
    events = []

    # Деньги: таблица transactions. Игровые заказы берём из game_orders (там видна игра и статус),
    # поэтому type='game_order' здесь пропускаем, чтобы не дублировать.
    cursor.execute(
        "SELECT type, amount, status, description, timestamp FROM transactions "
        "WHERE user_id = ? AND type != 'game_order' ORDER BY id DESC LIMIT ?",
        (user_id, limit)
    )
    for tx_type, amount, status, description, ts in cursor.fetchall():
        if tx_type in ("deposit", "manual_deposit", "card_auto_deposit", "admin_deposit"):
            kind = "deposit"
        elif tx_type in ("buy_stars", "buy_premium", "nft_buy", "daily_card_extra", "withdrawal", "admin_remove"):
            kind = "spend"
        else:
            kind = "other"
        events.append({
            "kind": kind, "type": tx_type, "title": _TX_ACTIVITY.get(tx_type, (None, None))[1], "detail": description or "",
            "amount": float(amount or 0), "unit": "сум", "status": status, "timestamp": ts or "",
        })

    cursor.execute(
        "SELECT id, category, product_name, price, status, created_at FROM game_orders "
        "WHERE user_id = ? ORDER BY id DESC LIMIT ?",
        (user_id, limit)
    )
    for order_id, category, product_name, price, status, created_at in cursor.fetchall():
        game = GAME_CATEGORY_TITLES.get(category, category or "Игра")
        price = float(price or 0)
        events.append({
            "kind": "game", "type": "game_order", "title": f"{game}: {product_name}",
            "detail": f"Заявка №{order_id}" + (" · приз с карточки (бесплатно)" if price == 0 else ""),
            "amount": -price, "unit": "сум", "status": status, "timestamp": created_at or "",
        })

    # Заявки на пополнение, которые ещё не превратились в зачисление
    cursor.execute(
        "SELECT amount, status, created_at FROM manual_deposits WHERE user_id = ? AND status != 'approved' "
        "ORDER BY id DESC LIMIT ?",
        (user_id, limit)
    )
    for amount, status, created_at in cursor.fetchall():
        events.append({
            "kind": "deposit", "type": "manual_deposit_request", "title": "Заявка на пополнение (банкомат)",
            "detail": "Скрин чека отправлен на проверку", "amount": float(amount or 0), "unit": "сум",
            "status": "pending" if status == "pending" else "rejected", "timestamp": created_at or "",
        })

    # Токены
    cursor.execute(
        "SELECT amount, type, description, timestamp FROM token_transactions WHERE user_id = ? ORDER BY id DESC LIMIT ?",
        (user_id, limit)
    )
    for amount, tx_type, description, ts in cursor.fetchall():
        events.append({
            "kind": "other", "type": "token_" + (tx_type or ""), "title": "Токены",
            "detail": description or "", "amount": float(amount or 0), "unit": "токенов",
            "status": "success", "timestamp": ts or "",
        })

    conn.close()
    events.sort(key=lambda e: e["timestamp"], reverse=True)
    return events[:limit]


def get_user_history_summary(user_id):
    """Шапка истории: баланс, звёзды, токены и итоговые суммы пополнений/трат."""
    conn = _conn()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT username, COALESCE(balance,0), COALESCE(star_balance,0), COALESCE(token_balance,0), created_at "
        "FROM users WHERE user_id = ?", (user_id,)
    )
    row = cursor.fetchone()
    if not row:
        conn.close()
        return None
    username, balance, stars, tokens, created_at = row

    cursor.execute(
        "SELECT COALESCE(SUM(amount),0) FROM transactions WHERE user_id=? AND status='success' "
        "AND type IN ('deposit','manual_deposit','card_auto_deposit','admin_deposit')", (user_id,)
    )
    deposited = cursor.fetchone()[0]
    cursor.execute(
        "SELECT COALESCE(SUM(-amount),0) FROM transactions WHERE user_id=? AND status='success' "
        "AND type IN ('buy_stars','buy_premium')", (user_id,)
    )
    spent_stars = cursor.fetchone()[0]
    cursor.execute(
        "SELECT COALESCE(SUM(price),0) FROM game_orders WHERE user_id=? AND status != 'cancelled'", (user_id,)
    )
    spent_games = cursor.fetchone()[0]
    conn.close()
    return {
        "user_id": user_id, "username": username or "—", "balance": float(balance),
        "star_balance": int(stars), "token_balance": int(tokens), "created_at": created_at,
        "total_deposited": float(deposited or 0),
        "total_spent_stars": float(spent_stars or 0),
        "total_spent_games": float(spent_games or 0),
    }


def get_top_star_holders(limit=50):
    """Пользователи с накопленными звёздами (star_balance > 0), от большего к меньшему."""
    conn = _conn()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT user_id, username, star_balance FROM users WHERE COALESCE(star_balance,0) > 0 "
        "ORDER BY star_balance DESC, user_id LIMIT ?", (limit,)
    )
    rows = cursor.fetchall()
    cursor.execute("SELECT COUNT(*), COALESCE(SUM(star_balance),0) FROM users WHERE COALESCE(star_balance,0) > 0")
    holders, total = cursor.fetchone()
    conn.close()
    return rows, int(holders or 0), int(total or 0)


def get_stars_given_today():
    """Сколько звёзд роздано призами за сегодня: ежедневная карточка + тайный приз.
    В БД число звёзд лежит только в тексте результата («25 Stars — Зачислены…»),
    поэтому достаём его оттуда — так работает и для старых записей."""
    import re
    today = datetime.date.today().isoformat()
    conn = _conn()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT result_text FROM daily_card_claims WHERE claim_date = ? AND prize_type = 'stars'", (today,)
    )
    daily_rows = [r[0] or "" for r in cursor.fetchall()]
    cursor.execute(
        "SELECT result_text FROM mystery_box_openings WHERE prize_type = 'stars' AND status = 'completed' "
        "AND created_at LIKE ?", (f"{today}%",)
    )
    mystery_rows = [r[0] or "" for r in cursor.fetchall()]
    conn.close()

    def _sum(rows):
        total = 0
        for text in rows:
            m = re.match(r"\s*(\d+)\s*Stars", text)
            if m:
                total += int(m.group(1))
        return total

    daily, mystery = _sum(daily_rows), _sum(mystery_rows)
    return {
        "total": daily + mystery, "daily_card": daily, "mystery_box": mystery,
        "claims": len(daily_rows) + len(mystery_rows),
    }


def get_top_token_holders(limit=50):
    """Рейтинг по токенам: у кого больше всего (token_balance > 0) и ниже по убыванию."""
    conn = _conn()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT user_id, username, token_balance FROM users WHERE COALESCE(token_balance,0) > 0 "
        "ORDER BY token_balance DESC, user_id LIMIT ?", (limit,)
    )
    rows = cursor.fetchall()
    cursor.execute("SELECT COUNT(*), COALESCE(SUM(token_balance),0) FROM users WHERE COALESCE(token_balance,0) > 0")
    holders, total = cursor.fetchone()
    today = datetime.date.today().isoformat()
    cursor.execute(
        "SELECT COALESCE(SUM(CASE WHEN amount > 0 THEN amount END),0), COALESCE(SUM(CASE WHEN amount < 0 THEN -amount END),0) "
        "FROM token_transactions WHERE timestamp LIKE ?", (f"{today}%",)
    )
    earned_today, spent_today = cursor.fetchone()
    conn.close()
    return rows, {
        "holders": int(holders or 0), "total": int(total or 0),
        "earned_today": int(earned_today or 0), "spent_today": int(spent_today or 0),
    }



# ==================== АДМИН: ЕДИНАЯ ЛЕНТА АКТИВНОСТИ ====================

_TX_ACTIVITY = {
    # type: (группа, подпись)
    "deposit": ("deposit", "Пополнение (шлюз)"),
    "manual_deposit": ("deposit", "Пополнение (банкомат)"),
    "card_auto_deposit": ("deposit", "Пополнение (перевод)"),
    "admin_deposit": ("admin", "Начислено админом"),
    "admin_remove": ("admin", "Списано админом"),
    "buy_stars": ("purchase", "Покупка звёзд"),
    "buy_premium": ("purchase", "Покупка Premium"),
    "daily_card_extra": ("purchase", "Докупка открытий карточки"),
    "withdrawal": ("purchase", "Вывод средств"),
    "nft_buy": ("nft", "Покупка NFT"),
    "nft_sale": ("nft", "Продажа NFT"),
    "star_withdrawal": ("cards", "Вывод звёзд"),
    "refund": ("other", "Возврат"),
    "referral_bonus": ("other", "Реферальный бонус"),
}

_NFT_STATUS_TITLES = {
    "pending_admin": "ожидает проверки",
    "awaiting_transfer": "одобрена, ждёт передачи NFT",
    "verification": "проверка передачи",
    "active": "в продаже",
    "sold": "продан",
    "cancelled": "отклонена / отменена",
}


def _sign(amount):
    return "+" if amount > 0 else ("-" if amount < 0 else "")


def get_activity_feed(limit=60, offset=0, group=None):
    """Единая лента «Админ → Активность»: ВСЕ действия пользователей и админов.

    Источники: transactions (пополнения, покупки, ручные начисления/списания админом,
    возвраты, NFT, выводы…), game_orders (донаты в игры и Steam, включая бесплатные призы),
    заявки на пополнение (банкомат), заявки на публикацию NFT, призы карточек и тайного приза,
    операции с токенами. Игровые 'game_order' из transactions пропускаются: их показывает
    game_orders (там видны игра, товар и статус).

    group: deposit | admin | purchase | game | nft | cards | other | None (всё).
    Возвращает события новые сверху: id, user_id, username, group, type, title, detail,
    amount, sign, unit, status, timestamp.
    """
    SRC_LIMIT = 500
    conn = _conn()
    cursor = conn.cursor()
    ev = []

    cursor.execute(
        "SELECT t.id, t.user_id, u.username, t.amount, t.type, t.status, t.description, t.timestamp "
        "FROM transactions t LEFT JOIN users u ON u.user_id = t.user_id "
        "WHERE t.type != 'game_order' ORDER BY t.id DESC LIMIT ?", (SRC_LIMIT,)
    )
    for tid, uid, uname, amount, tx_type, status, desc, ts in cursor.fetchall():
        grp, title = _TX_ACTIVITY.get(tx_type, ("other", tx_type or "Операция"))
        amount = float(amount or 0)
        ev.append({
            "id": f"t{tid}", "user_id": uid, "username": uname or "—", "group": grp, "type": tx_type,
            "title": title, "detail": desc or "", "amount": amount, "sign": _sign(amount),
            "unit": "сум", "status": status or "", "timestamp": ts or "",
        })

    cursor.execute(
        "SELECT id, user_id, username, category, product_name, price, extra, status, created_at "
        "FROM game_orders ORDER BY id DESC LIMIT ?", (SRC_LIMIT,)
    )
    for oid, uid, uname, cat, pname, price, extra, status, created in cursor.fetchall():
        price = float(price or 0)
        game = GAME_CATEGORY_TITLES.get(cat, cat or "Игра")
        detail = f"Заявка №{oid}"
        if cat == "steam":
            try:
                x = json.loads(extra) if extra else {}
            except (TypeError, ValueError):
                x = {}
            if x.get("login"):
                detail += f" · логин: {x['login']}"
        if price == 0:
            detail += " · приз с карточки (бесплатно)"
        ev.append({
            "id": f"g{oid}", "user_id": uid, "username": uname or "—", "group": "game", "type": "game_order",
            "title": f"{game}: {pname}", "detail": detail, "amount": -price, "sign": _sign(-price),
            "unit": "сум", "status": status or "", "timestamp": created or "",
        })

    cursor.execute(
        "SELECT id, user_id, username, amount, status, created_at FROM manual_deposits "
        "WHERE status != 'approved' ORDER BY id DESC LIMIT ?", (SRC_LIMIT,)
    )
    for did, uid, uname, amount, status, created in cursor.fetchall():
        amount = float(amount or 0)
        ev.append({
            "id": f"d{did}", "user_id": uid, "username": uname or "—", "group": "deposit",
            "type": "manual_deposit_request", "title": "Заявка на пополнение (банкомат)",
            "detail": f"Заявка №{did} · скрин чека отправлен на проверку", "amount": amount, "sign": _sign(amount),
            "unit": "сум", "status": "pending" if status == "pending" else "rejected", "timestamp": created or "",
        })

    cursor.execute(
        "SELECT id, seller_id, seller_username, nft_name, price, status, created_at FROM nft_listings "
        "ORDER BY id DESC LIMIT ?", (SRC_LIMIT,)
    )
    for lid, uid, uname, name, price, status, created in cursor.fetchall():
        ev.append({
            "id": f"n{lid}", "user_id": uid, "username": uname or "—", "group": "nft", "type": "nft_listing",
            "title": f"NFT «{name}»: заявка на публикацию",
            "detail": f"Заявка №{lid} · цена {int(price or 0):,} сум · {_NFT_STATUS_TITLES.get(status, status)}".replace(",", " "),
            "amount": 0.0, "sign": "", "unit": "сум",
            "status": "pending" if status in ("pending_admin", "awaiting_transfer", "verification") else
                      ("rejected" if status == "cancelled" else "success"),
            "timestamp": created or "",
        })

    cursor.execute(
        "SELECT id, user_id, username, prize_label, result_text, created_at FROM daily_card_claims "
        "ORDER BY id DESC LIMIT ?", (SRC_LIMIT,)
    )
    for cid, uid, uname, label, result, created in cursor.fetchall():
        ev.append({
            "id": f"c{cid}", "user_id": uid, "username": uname or "—", "group": "cards", "type": "daily_card",
            "title": "Ежедневная карточка", "detail": result or label or "", "amount": 0.0, "sign": "",
            "unit": "сум", "status": "success", "timestamp": created or "",
        })

    cursor.execute(
        "SELECT id, user_id, username, prize_label, result_text, price, status, created_at FROM mystery_box_openings "
        "ORDER BY id DESC LIMIT ?", (SRC_LIMIT,)
    )
    for mid, uid, uname, label, result, price, status, created in cursor.fetchall():
        try:
            price = float(price or 0)
        except (TypeError, ValueError):
            price = 0.0
        ev.append({
            "id": f"m{mid}", "user_id": uid, "username": uname or "—", "group": "cards", "type": "mystery_box",
            "title": "Тайный приз", "detail": result or label or "", "amount": -price, "sign": _sign(-price),
            "unit": "сум", "status": "pending" if status == "pending_admin" else "success", "timestamp": created or "",
        })

    cursor.execute(
        "SELECT tt.id, tt.user_id, u.username, tt.amount, tt.type, tt.description, tt.timestamp "
        "FROM token_transactions tt LEFT JOIN users u ON u.user_id = tt.user_id ORDER BY tt.id DESC LIMIT ?",
        (SRC_LIMIT,)
    )
    for tid, uid, uname, amount, tx_type, desc, ts in cursor.fetchall():
        amount = float(amount or 0)
        ev.append({
            "id": f"k{tid}", "user_id": uid, "username": uname or "—", "group": "other", "type": "token",
            "title": "Токены", "detail": desc or "", "amount": amount, "sign": _sign(amount),
            "unit": "токенов", "status": "success", "timestamp": ts or "",
        })
    conn.close()

    if group:
        ev = [e for e in ev if e["group"] == group]
    ev.sort(key=lambda e: e["timestamp"], reverse=True)
    return ev[offset:offset + limit]


# ==================== АДМИН: ЗВЁЗДЫ ПО ПУЛАМ КАРТОЧЕК ====================

def get_stars_pool_stats(pool="daily", top=20):
    """Звёзды, розданные призами ОДНОГО пула: 'daily' (ежедневная карточка) или 'mystery' (тайный приз).
    Число звёзд хранится только в тексте результата («25 Stars — …»), поэтому парсим его.
    Возвращает today/all (сумма и число выпадений) и топ получателей за всё время."""
    import re
    today = datetime.date.today().isoformat()
    conn = _conn()
    cursor = conn.cursor()
    if pool == "mystery":
        cursor.execute(
            "SELECT user_id, username, result_text, created_at FROM mystery_box_openings "
            "WHERE prize_type = 'stars' AND status = 'completed'"
        )
    else:
        cursor.execute(
            "SELECT user_id, username, result_text, claim_date FROM daily_card_claims WHERE prize_type = 'stars'"
        )
    rows = cursor.fetchall()
    conn.close()

    today_total = today_count = all_total = all_count = 0
    per_user = {}
    for uid, uname, text, day in rows:
        m = re.match(r"\s*(\d+)\s*Stars", text or "")
        if not m:
            continue
        n = int(m.group(1))
        all_total += n
        all_count += 1
        if (day or "").startswith(today):
            today_total += n
            today_count += 1
        u = per_user.setdefault(uid, {"user_id": uid, "username": uname or "—", "stars": 0, "count": 0})
        u["stars"] += n
        u["count"] += 1
    winners = sorted(per_user.values(), key=lambda x: (-x["stars"], x["user_id"]))[:top]
    return {
        "pool": "mystery" if pool == "mystery" else "daily",
        "today_total": today_total, "today_count": today_count,
        "all_total": all_total, "all_count": all_count, "winners": winners,
    }
