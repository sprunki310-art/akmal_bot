import os
import re
import sys
import time
import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    from telethon import TelegramClient, events, errors
    from telethon.sessions import StringSession
except ImportError:
    sys.exit("Установите зависимости: pip install telethon requests python-socks[asyncio]")

API_ID = os.getenv("TG_API_ID", "")
API_HASH = os.getenv("TG_API_HASH", "")
PHONE = os.getenv("TG_PHONE", "")
SESSION_STRING = os.getenv("TG_SESSION_STRING", "") 

WEBAPP_URL = os.getenv("WEBAPP_URL", "").rstrip("/")
SMS_WEBHOOK_SECRET = os.getenv("SMS_WEBHOOK_SECRET", "")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "") or (f"{WEBAPP_URL}/api/sms/webhook" if WEBAPP_URL else "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "") or SMS_WEBHOOK_SECRET

BOT_CARD_MAP = {
    "cardxabarbot": "uzcard",
    "humocardbot": "humo",
}

if not API_ID or not API_HASH or not SESSION_STRING:
    sys.exit("КРИТИЧЕСКАЯ ОШИБКА: Заполните TG_API_ID, TG_API_HASH и TG_SESSION_STRING в Railway!")

if not WEBHOOK_URL or not WEBHOOK_SECRET:
    sys.exit("Не задан вебхук или секрет.")

SMS_PROXY_URL = os.getenv("SMS_PROXY_URL", "").strip()

def _parse_proxy(proxy_url):
    if not proxy_url:
        return None
    m = re.match(r'^(socks5|socks4|http)://(?:([^:@/]+):([^@/]+)@)?([^:@/]+):(\d+)/?$', proxy_url, re.IGNORECASE)
    if not m:
        return None
    scheme, user, password, host, port = m.groups()
    return {
        "proxy_type": scheme.lower(),
        "addr": host,
        "port": int(port),
        "rdns": True,
        "username": user,
        "password": password
    }

_proxy = _parse_proxy(SMS_PROXY_URL)
if _proxy:
    print(f"🌐 Подключение через прокси: {_proxy['addr']}:{_proxy['port']}")

client = TelegramClient(StringSession(SESSION_STRING), int(API_ID), API_HASH, proxy=_proxy)

@client.on(events.NewMessage(from_users=list(BOT_CARD_MAP.keys())))
async def on_bank_notification(event):
    sender = await event.get_sender()
    username = (getattr(sender, "username", "") or "").lower()
    card_id = BOT_CARD_MAP.get(username)
    text = event.message.text or ""

    if not text:
        return

    print(f"📩 Сообщение от @{username}: {text[:100]}")
    try:
        res = requests.post(
            WEBHOOK_URL,
            json={"secret": WEBHOOK_SECRET, "text": text, "card_id": card_id},
            timeout=15,
        )
        print(f"   → вебхук ответил {res.status_code}")
    except Exception as e:
        print(f"   → ОШИБКА вебхука: {e}")

def main():
    print("🚀 Подключаюсь к Telegram через StringSession...")
    print(f"   Вебхук: {WEBHOOK_URL}")
    try:
        client.loop.run_until_complete(client.connect())
        if not client.loop.run_until_complete(client.is_user_authorized()):
            sys.exit("❌ Ошибка: Строка сессии TG_SESSION_STRING невалидна или устарела!")
            
    except errors.AuthKeyDuplicatedError:
        print("❌ AuthKeyDuplicatedError: старый контейнер еще не умер. Сплю 10 сек...")
        time.sleep(10)
        sys.exit(1)

    print("✅ Успешно авторизовано! Слушаю ботов...")
    client.run_until_disconnected()

if __name__ == "__main__":
    main()
