````markdown
# 🎁 GiftSniper — телеграм‑юзербот для молниеносной скупки Stars‑подарков

> «Быстрее, чем успевают кончиться лимиты» — **и без риска банов**.  
> Бот не использует Bot API — только MTProto‑сессию реального аккаунта.  
> Работает _тихо_: никаких уведомлений, только покупка ✌️

---

## ✨ Возможности

| Фича | Описание |
|------|----------|
| **MTProto only** | даже при выключенном Bot API бот продолжит работать |
| **«Человеческие» тайминги** | ночной и дневной перерыв, случайные задержки, замедленный режим для редких/дешёвых подарков |
| **Анти‑FloodWait** | автоматически «отсыпается», когда Telegram просит подождать |
| **Защита от PeerFlood** | прекращает работу, если аккаунт попадает под ограничение |
| **Гибкие фильтры** | настраиваются лимиты цены/тиража, макс. кол‑во покупок |
| **Минимальный RAM‑след** | запускается на VPS 0.25 CPU / 512 МБ |

---

## 🚀 Быстрый старт

```bash
git clone https://github.com/Ko-nD/gifts.git
cd gifts
python -m venv venv
source venv/bin/activate   # Windows → venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env       # заполните переменные
python gifts_sniper.py
````

### Переменные окружения (`.env`)

| Переменная             | Что это                                                                                                                           |
| ---------------------- | --------------------------------------------------------------------------------------------------------------------------------- |
| `TG_API_ID`            | ваш **api\_id** из [my.telegram.org](https://my.telegram.org)                                                                     |
| `TG_API_HASH`          | ваш **api\_hash**                                                                                                                 |
| `TG_SESSION`           | строка session‑файла (генерируем через `pyrogram.client`) <br>если оставить пустой, будет использоваться файл `TgAccount.session` |
| `ID_TO_BUY`            | **ID чата/пользователя**, куда *дарим* подарки                                                                                    |
| `PRICE_LIMIT_FROM/TO`  | ценовой диапазон в ⭐                                                                                                              |
| `SUPPLY_LIMIT_FROM/TO` | диапазон остатка (тиража)                                                                                                         |
| `GIFT_COUNT_TO_BUY`    | макс. попыток покупки, если лимит не переопределён логикой                                                                        |
| `BUY_GIFT`             | `true` / `false` — включить ли покупку                                                                                            |

<details>
<summary>Пример .env</summary>

```dotenv
TG_API_ID=1234567
TG_API_HASH=0123456789abcdef0123456789abcdef
TG_SESSION=1AQA...  # строка от @MadelineProtoSessionBot или pyrogram
ID_TO_BUY=987654321
PRICE_LIMIT_FROM=0
PRICE_LIMIT_TO=500
SUPPLY_LIMIT_FROM=1
SUPPLY_LIMIT_TO=50000
GIFT_COUNT_TO_BUY=2
BUY_GIFT=true
```

</details>

---

## 🛠️ Настройка systemd‑сервиса (VPS)

```ini
# /etc/systemd/system/giftsniper.service
[Unit]
Description=GiftSniper Telegram userbot
After=network-online.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/gifts
EnvironmentFile=/home/ubuntu/gifts/.env
ExecStart=/home/ubuntu/gifts/venv/bin/python gifts_sniper.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now giftsniper
journalctl -u giftsniper -f   # смотреть логи
```

---

## 📦 requirements.txt

```text
pyrogram[fast]==2.0.106
tgcrypto==1.2.5
python-dotenv==1.0.0
loguru==0.7.2
```

---

## 🙋‍♂️ FAQ

| Вопрос                              | Ответ                                                                                              |
| ----------------------------------- | -------------------------------------------------------------------------------------------------- |
| **Меня забанят?**                   | Бот имитирует поведение человека, но 100 % гарантии нет. Используйте отдельный «тестовый» аккаунт. |
| **Как сгенерировать `TG_SESSION`?** | `python -m pyrogram.cli` → введите `api_id`, `api_hash`, код из Telegram, затем `session.save()`   |
| **Можно ли добавить уведомления?**  | Да. В примере кода они закомментированы — раскомментируйте метод `_notify`.                        |

---

## 🤝 Contributing

PR‑ы и issue приветствуются!
Перед пулл‑реквестом запустите `flake8` и протестируйте на своём аккаунте‑песочнице.

---

## 📝 License

`GiftSniper` распространяется под лицензией **MIT**. Смотрите файл `LICENSE.md`.

```
```
