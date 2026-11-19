import os
import logging
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Константы магазина ---
STAR_PRICE = 230
CURRENCY = "сум"
CURRENCY_CODE = "UZS"

PREMIUM_PRICES = {3: 162000, 6: 215000, 12: 415000}

# --- Каталог Brawl Stars (ручное исполнение через Supercell ID) ---
# icon — эмодзи-плейсхолдер вместо картинки (реальные арты Brawl Stars — IP Supercell)
BRAWL_STARS_PRODUCTS = [
    {"id": "brawl_pass",      "name": "Brawl Pass",      "icon": "/brawl-pass.png",      "price": 90000},
    {"id": "brawl_pass_plus", "name": "Brawl Pass Plus", "icon": "/brawl-pass-plus.png", "price": 115000},
    {"id": "pro_pass",        "name": "Pro Pass",        "icon": "/pro-pass.png",        "price": 190000},
    {"id": "gem_30",          "name": "30 Gems",         "icon": "/gems.png", "price": 25000},
    {"id": "gem_80",          "name": "80 Gems",         "icon": "/gems.png", "price": 45000},
    {"id": "gem_170",         "name": "170 Gems",        "icon": "/gems.png", "price": 85000},
    {"id": "gem_360",         "name": "360 Gems",        "icon": "/gems.png", "price": 150000},
    {"id": "gem_950",         "name": "950 Gems",        "icon": "/gems.png", "price": 365000},
    {"id": "gem_2000",        "name": "2000 Gems",       "icon": "/gems.png", "price": 690000},
]

# --- Каталог Roblox (Robux, ручное исполнение через логин/пароль аккаунта) ---
ROBLOX_PRODUCTS = [
    {"id": "robux_40",   "name": "40 Robux",   "icon": "/robux.png", "amount": 40,   "price": 10000},
    {"id": "robux_80",   "name": "80 Robux",   "icon": "/robux.png", "amount": 80,   "price": 20000},
    {"id": "robux_160",  "name": "160 Robux",  "icon": "/robux.png", "amount": 160,  "price": 37000},
    {"id": "robux_400",  "name": "400 Robux",  "icon": "/robux.png", "amount": 400,  "price": 80000},
    {"id": "robux_800",  "name": "800 Robux",  "icon": "/robux.png", "amount": 800,  "price": 135000},
    {"id": "robux_1700", "name": "1700 Robux", "icon": "/robux.png", "amount": 1700, "price": 260000},
]

# --- Каталог Standoff 2 Golda (ручное исполнение через продажу скина на площадке) ---
STANDOFF_GOLD_PRICE_PER_UNIT = 140  # 14 000 сум за каждые 100 golda
STANDOFF_GOLD_PRODUCTS = [
    {"id": f"gold_{n}", "name": f"{n} golda", "icon": "/golda.png", "amount": n, "price": n * STANDOFF_GOLD_PRICE_PER_UNIT}
    for n in range(100, 1001, 100)
]

# Правила, которые покупатель обязан соблюдать до завершения сделки Standoff Golda
STANDOFF_RULES_TEXT = (
    "❗️ *Обязательные правила до завершения сделки:*\n"
    "• не менять аватар;\n"
    "• не менять ник;\n"
    "• не снимать скин с продажи;\n"
    "• не изменять цену;\n"
    "• соблюдать условия до завершения сделки."
)

MIN_STARS = 50
MIN_DEPOSIT = 1000

# --- NFT-маркетплейс ---
# Кому продавец физически отправляет NFT-подарок в Telegram для публикации на маркете.
NFT_ADMIN_USERNAME = os.getenv("NFT_ADMIN_USERNAME", "akmal_adm")
# Комиссия маркетплейса по умолчанию (%). Реальное значение хранится в БД
# (settings.nft_commission_percent) и редактируется из админки — это только
# стартовое значение, если в БД ещё ничего не настроено.
NFT_COMMISSION_PERCENT_DEFAULT = 10
# Минимальная сумма для вывода баланса
MIN_WITHDRAWAL = 20000
REFERRAL_BONUS = 3000  # начисляется рефереру за первую покупку приглашённого

# Канал, куда публикуются отзывы покупателей
REVIEW_CHANNEL = os.getenv("REVIEW_CHANNEL", "@akmal_otzivi")

# Реквизиты для ручного пополнения (карта/телефон) — резервный способ,
# проверяется администратором вручную в админ-панели мини-аппа
MANUAL_CARD_NUMBER = os.getenv("MANUAL_CARD_NUMBER", "5614683586724031")
MANUAL_PHONE_NUMBER = os.getenv("MANUAL_PHONE_NUMBER", "90 023 50 01")

# ── Автоплатёж через @CardXabarBot / @HUMOcardbot ──
# Пользователь вводит только сумму (без комментария). Бот-скрипт
# telegram_sms_listener.py слушает уведомления банка в вашем личном
# Telegram-аккаунте и шлёт их сюда, на /api/sms/webhook, где ищется
# pending-заявка с такой же суммой (в пределах CARD_DEPOSIT_TTL_MINUTES).
SMS_WEBHOOK_SECRET = os.getenv("SMS_WEBHOOK_SECRET", "")  # секрет для /api/sms/webhook
CARD_DEPOSIT_TTL_MINUTES = int(os.getenv("CARD_DEPOSIT_TTL_MINUTES", "5"))

WELCOME_MESSAGES = {
    'uz': (
        "👋 *Assalomu alaykum!*\n\n"
        "🌟 *Telegram Stars va Premium sotib olishga xush kelibsiz!*\n\n"
        "👇 Quyidagi tugmani bosib do'konni oching:"
    ),
    'ru': (
        "👋 *Привет!*\n\n"
        "🌟 *Добро пожаловать в Akmal's Stars для покупки Telegram Stars!*\n\n"
        "👇 Нажмите кнопку ниже, чтобы открыть магазин:"
    )
}

# --- Telegram ---
BOT_TOKEN = os.getenv('8535219602:AAGb0a0Qk1ESo-TPkOHTxFO1y8YJZ11KVEc')
BOT_USERNAME = os.getenv('akmal_starsbot')
ADMIN_ID = os.getenv('8101022024', '1353187194')
ADMIN_IDS = [int(i.strip()) for i in os.getenv('ADMIN_ID', '').split(',') if i.strip()]

# URL мини-аппа (Telegram WebApp), который открывается из бота
WEBAPP_URL = os.getenv("WEBAPP_URL", "")

# --- Путь к базе данных ---
if os.path.exists('/mnt/data'):
    DB_NAME = '/mnt/data/database.db'
elif os.path.exists('/data'):
    DB_NAME = '/data/database.db'
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    PERSISTENT_DIR = os.path.join(BASE_DIR, 'persistent_data')
    os.makedirs(PERSISTENT_DIR, exist_ok=True)
    DB_NAME = os.path.join(PERSISTENT_DIR, 'database.db')

# --- Freedom Pay ---
FREEDOM_MERCHANT_ID = os.getenv('FREEDOM_MERCHANT_ID')
FREEDOM_SECRET_KEY = os.getenv('FREEDOM_SECRET_KEY')
FREEDOM_API_URL = os.getenv('FREEDOM_API_URL', 'https://api.freedompay.uz/v1/payments')

# --- TON ---
TON_DEPOSIT_ADDRESS = os.getenv('UQCkJv7AqXONIOlIcgC_JhaVLgleLNsmghZ5Az-tRVzDhBDU')
TON_API_KEY = os.getenv('993dc59bdef2ba7c3905b65acee6577fad79e1060327116cc516cb99a08f620b')
TON_API_BASE_URL = 'https://toncenter.com'

# --- Fragment API ---
FRAGMENT_API_URL = "https://api.fragment-api.com/v1"
FRAGMENT_API_KEY = os.getenv("17882fc2-62c4-4c6f-b2f4-12770f7300e8")
FRAGMENT_PHONE = os.getenv("998900235001")
FRAGMENT_MNEMONICS = os.getenv("FRAGMENT_MNEMONICS")

# --- Проверки конфигурации ---
if not BOT_TOKEN:
    logger.error("❌ BOT_TOKEN не найден в переменных окружения.")

if not WEBAPP_URL:
    logger.warning("⚠️ WEBAPP_URL не задан — кнопка открытия магазина не будет показана.")

if not FREEDOM_MERCHANT_ID or not FREEDOM_SECRET_KEY:
    logger.warning("⚠️ Учётные данные Freedom Pay не найдены.")

if not FRAGMENT_API_KEY or not FRAGMENT_PHONE or not FRAGMENT_MNEMONICS:
    logger.warning("⚠️ Учётные данные Fragment API не найдены.")

if MANUAL_CARD_NUMBER and not SMS_WEBHOOK_SECRET:
    logger.warning("⚠️ SMS_WEBHOOK_SECRET не установлен — автоподтверждение карточных платежей работать не будет.")
