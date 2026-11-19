"""
payme_monitor.py
================
Мониторит входящие P2P платежи в Payme (на личный номер телефона) и
автоматически подтверждает пополнения в приложении по уникальному
комментарию (ID заявки: dep_5, dep_10 и т.д.).

Использует reverse-engineered API из:
https://github.com/sobirjonovs/automated-p2p-transactions

⚠️ ВАЖНО:
- Это неофициальный API, может периодически ломаться
- Payme может заблокировать частые запросы (добавлены задержки)
- Храни пароль Payme с той же осторожностью, что и банковский пароль

═════════════════════ НАСТРОЙКА ═════════════════════

1. Установи зависимости:
     pip install requests

2. Заполни переменные окружения (или в .env):
     PAYME_PHONE=+998901234567        (номер, на который приходят P2P платежи)
     PAYME_PASSWORD=...               (пароль от приложения Payme)
     WEBHOOK_URL=https://домен/api/sms/webhook
     WEBHOOK_SECRET=...               (тот же SMS_WEBHOOK_SECRET)

3. Запусти скрипт:
     python payme_monitor.py

   Скрипт каждые 30 секунд проверяет входящие платежи, ищет те,
   у которых в комментарии есть "dep_" (название заявки в БД),
   и вызывает вебхук для зачисления баланса.

4. Держи скрипт постоянно запущенным (screen/systemd/pm2).
"""

import os
import sys
import time
import requests
import json
from typing import Optional, Dict, List

PAYME_PHONE = os.getenv("PAYME_PHONE", "")
PAYME_PASSWORD = os.getenv("PAYME_PASSWORD", "")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

# Интервал проверки в секундах (30 сек вполне безопасно)
CHECK_INTERVAL = 30

if not PAYME_PHONE or not PAYME_PASSWORD:
    sys.exit("❌ Заполните PAYME_PHONE и PAYME_PASSWORD")
if not WEBHOOK_URL or not WEBHOOK_SECRET:
    sys.exit("❌ Заполните WEBHOOK_URL и WEBHOOK_SECRET")


class PaymeClient:
    """Клиент для работы с reverse-engineered Payme API."""

    def __init__(self, phone: str, password: str):
        self.phone = phone
        self.password = password
        self.session = requests.Session()
        self.token = None
        self.user_id = None

    def login(self) -> bool:
        """Вход в Payme как мобильное приложение."""
        try:
            # 1. registerDevice — регистрируем устройство
            device_data = {
                "phone_number": self.phone,
                "password": self.password,
                "device_name": "SMSListener",
                "device_id": "sms_listener_1",
            }
            print(f"📱 Регистрирую устройство {self.phone}...")
            r = self.session.post(
                "https://api.payme.uz/v1/mobile/registerDevice",
                json=device_data,
                timeout=10,
            )
            if r.status_code != 200:
                print(f"❌ Ошибка регистрации: {r.status_code} {r.text[:200]}")
                return False

            data = r.json()
            self.token = data.get("token")
            self.user_id = data.get("user_id")
            if not self.token:
                print(f"❌ Нет token в ответе: {data}")
                return False

            print(f"✅ Авторизовано, token: {self.token[:20]}...")
            self.session.headers.update({"Authorization": f"Bearer {self.token}"})
            return True
        except Exception as e:
            print(f"❌ Ошибка входа: {e}")
            return False

    def get_my_cards(self) -> Optional[List[Dict]]:
        """Получить список своих карт."""
        try:
            r = self.session.get(
                "https://api.payme.uz/v1/mobile/getMyCards", timeout=10
            )
            if r.status_code != 200:
                print(f"⚠️ Ошибка getMyCards: {r.status_code}")
                return None
            return r.json().get("cards", [])
        except Exception as e:
            print(f"⚠️ Ошибка получения карт: {e}")
            return None

    def get_card_history(
        self, card_id: str, limit: int = 50
    ) -> Optional[List[Dict]]:
        """Получить историю платежей карты (последние `limit` операций)."""
        try:
            params = {"card_id": card_id, "limit": limit, "offset": 0}
            r = self.session.get(
                "https://api.payme.uz/v1/mobile/getCardHistory",
                params=params,
                timeout=10,
            )
            if r.status_code != 200:
                print(f"⚠️ Ошибка getCardHistory: {r.status_code}")
                return None
            return r.json().get("transactions", [])
        except Exception as e:
            print(f"⚠️ Ошибка получения истории: {e}")
            return None


def extract_deposit_id(comment: str) -> Optional[int]:
    """Извлечь ID заявки из комментария (ищет 'dep_123')."""
    import re

    match = re.search(r"dep[_\s]?(\d+)", comment.lower())
    if match:
        try:
            return int(match.group(1))
        except ValueError:
            pass
    return None


def send_webhook(amount: float, comment: str, deposit_id: int) -> bool:
    """Вызвать webhook для зачисления баланса."""
    try:
        payload = {
            "secret": WEBHOOK_SECRET,
            "text": f"Получен платёж {amount} сум. Комментарий: {comment}",
            "amount": amount,
            "comment": comment,
            "deposit_id": deposit_id,
        }
        r = requests.post(WEBHOOK_URL, json=payload, timeout=10)
        if r.status_code == 200:
            result = r.json()
            print(f"✅ Вебхук OK: {result}")
            return result.get("matched", False)
        else:
            print(f"⚠️ Вебхук статус {r.status_code}: {r.text[:200]}")
            return False
    except Exception as e:
        print(f"⚠️ Ошибка вебхука: {e}")
        return False


def monitor_loop():
    """Основной цикл мониторинга."""
    client = PaymeClient(PAYME_PHONE, PAYME_PASSWORD)

    # Кешируем обработанные транзакции, чтобы не отправлять один платёж дважды
    processed_tx_ids = set()

    while True:
        try:
            print(f"\n⏱️  Проверка {time.strftime('%H:%M:%S')}...")

            # Вход (если необходимо)
            if not client.token:
                if not client.login():
                    print("⏳ Повтор входа через 30 сек...")
                    time.sleep(30)
                    continue

            # Получить карты
            cards = client.get_my_cards()
            if not cards:
                print("⚠️ Нет карт")
                time.sleep(CHECK_INTERVAL)
                continue

            # Проверить историю каждой карты
            for card in cards:
                card_id = card.get("id")
                card_name = card.get("name", "Unknown")
                print(f"📋 Карта: {card_name}")

                transactions = client.get_card_history(card_id, limit=30)
                if not transactions:
                    continue

                # Ищем входящие платежи с комментариями вроде "dep_5"
                for tx in transactions:
                    tx_id = tx.get("id")
                    amount = tx.get("amount")
                    comment = tx.get("comment", "")
                    status = tx.get("status")  # completed, pending и т.д.
                    direction = tx.get(
                        "direction", ""
                    )  # "in" для входящих, "out" для исходящих

                    # Пропускаем уже обработанные или исходящие
                    if tx_id in processed_tx_ids or direction != "in":
                        continue

                    # Ищем "dep_" в комментарии
                    deposit_id = extract_deposit_id(comment)
                    if not deposit_id:
                        continue

                    print(
                        f"   💰 Платёж: {amount} сум, комментарий '{comment}', статус {status}"
                    )

                    # Вызываем вебхук
                    if send_webhook(amount, comment, deposit_id):
                        processed_tx_ids.add(tx_id)
                        print(f"   ✅ Заявка {deposit_id} обработана")

            print(f"⏳ Следующая проверка через {CHECK_INTERVAL} сек...")
            time.sleep(CHECK_INTERVAL)

        except KeyboardInterrupt:
            print("\n🛑 Остановка...")
            break
        except Exception as e:
            print(f"❌ Ошибка цикла: {e}")
            print(f"⏳ Повтор через {CHECK_INTERVAL} сек...")
            time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    print("🚀 Payme Monitor запущен")
    print(f"📱 Номер: {PAYME_PHONE}")
    print(f"🔗 Вебхук: {WEBHOOK_URL}")
    monitor_loop()
