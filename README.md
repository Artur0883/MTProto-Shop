# MTProto Shop

Минимальный магазин доступа к MTProto proxy для VPS.

Сейчас реализованы:

- Milestone 0: рабочий `mtprotoproxy / alexbers`, создание и удаление личных secret через `bot/proxy_manager.py`;
- Milestone 1: Telegram Bot MVP на `aiogram 3.x`, SQLite через `aiosqlite`, ручная выдача доступа администратором.

Контейнер `bot` запускает Telegram Bot MVP, а не keep-alive заглушку.

Оплаты, Web Admin Cabinet, React/Vite/nginx/PostgreSQL в этом этапе нет.

## Как это работает

1 клиент = 1 личный secret = 1 личная proxy-ссылка.

Бот хранит пользователей и подписки в SQLite. `proxy_link` в базе не хранится: ссылка собирается на лету из `SERVER_HOST`, `PROXY_PORT` и `secret`.

Proxy работает в secure/dd режиме `mtprotoproxy / alexbers`: в `proxy/config/config.py` хранится обычный 32-символьный hex secret, а в Telegram-ссылку бот добавляет публичный префикс `dd`.

После создания или удаления secret бот только обновляет `proxy/config/config.py`. Docker socket в контейнер бота не монтируется. Чтобы proxy применил изменения, команду reload/restart нужно выполнить на host-системе:

```bash
docker compose kill -s SIGUSR2 mtproto
```

Fallback:

```bash
docker compose restart mtproto
```

## Переменные окружения

Скопируйте пример:

```bash
cp .env.example .env
```

Заполните:

```env
BOT_TOKEN=123456:telegram_bot_token
ADMIN_ID=123456789
SERVER_HOST=1.2.3.4
PROXY_PORT=443
TLS_DOMAIN=www.google.com
SUPPORT_CONTACT=@your_support
DATABASE_PATH=/app/data/shop.db
```

Все реальные секреты должны быть только в `.env`.

## Локальная проверка

Windows:

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r bot/requirements.txt
python bot/main.py
```

Linux/macOS:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r bot/requirements.txt
python3 bot/main.py
```

Для локального запуска удобно поставить в `.env`:

```env
DATABASE_PATH=data/shop.db
```

## Fresh VPS bootstrap

`proxy/config/config.py` не хранится в GitHub, потому что это runtime-файл с реальными client secrets. При старте `bot/main.py` автоматически создаёт его из безопасного шаблона `proxy/config/config.example.py` и выставляет права `0644`.

Если нужно создать runtime config вручную до общего запуска:

```bash
docker compose run --rm bot python proxy_manager.py list
```

Затем запустите сервисы:

```bash
docker compose up -d
```

## Запуск на VPS

```bash
cp .env.example .env
nano .env
docker compose build
docker compose up -d
docker compose ps
docker compose logs -f bot
```

## Команды Milestone 0

Создать тестовый secret:

```bash
docker compose exec bot python proxy_manager.py create test-client
docker compose kill -s SIGUSR2 mtproto
```

Получить ссылку:

```bash
docker compose exec bot python proxy_manager.py link test-client
```

Удалить secret:

```bash
docker compose exec bot python proxy_manager.py delete test-client
docker compose kill -s SIGUSR2 mtproto
```

Fallback reload:

```bash
docker compose restart mtproto
```

## Клиентские функции бота

Команда `/start` регистрирует пользователя в SQLite и показывает меню:

- 🚀 Купить доступ
- 🔗 Моя ссылка
- 📅 Осталось дней
- 💬 Поддержка

Кнопка «Купить доступ» показывает тарифы на 7, 30 и 90 дней и контакт поддержки. Автоматической оплаты пока нет.

Кнопка «Моя ссылка» показывает личную ссылку только при активной подписке.

Кнопка «Осталось дней» показывает дату окончания в UTC и оставшееся количество дней.

## Админские функции

Команда:

```text
/admin
```

Доступна только пользователю с `ADMIN_ID` из `.env`.

Меню:

- 👥 Пользователи
- ➕ Выдать доступ
- 🔁 Продлить доступ
- ❌ Отключить доступ
- 📊 Статистика

## Как админу выдать доступ

1. Клиент сначала нажимает `/start` в боте.
2. Админ открывает `/admin`.
3. Нажимает «➕ Выдать доступ».
4. Вводит `telegram_id` клиента.
5. Выбирает тариф 7/30/90 дней.
6. Бот создаёт secret, сохраняет подписку в SQLite и отправляет клиенту личную ссылку.
7. На VPS админ применяет изменения proxy:

```bash
docker compose kill -s SIGUSR2 mtproto
```

Fallback:

```bash
docker compose restart mtproto
```

## Как клиент получает ссылку

После выдачи доступа бот отправит ссылку в личные сообщения. Клиент также может нажать «🔗 Моя ссылка» в меню, пока подписка активна.

## Продление доступа

Админ нажимает «🔁 Продлить доступ», вводит `telegram_id` и выбирает тариф.

Если подписка активна, срок продлевается от текущей даты окончания. Если подписка истекла, срок считается от текущего времени UTC. Существующий secret сохраняется, если он уже есть.

## Отключение доступа

Админ нажимает «❌ Отключить доступ» и вводит `telegram_id`.

Бот удаляет secret из proxy config и ставит подписке `status=disabled`. Затем на VPS нужно применить изменения:

```bash
docker compose kill -s SIGUSR2 mtproto
```

Fallback:

```bash
docker compose restart mtproto
```

## Автопроверка подписок

Фоновая задача раз в час:

- переводит истёкшие активные подписки в `expired`;
- удаляет secret из proxy config;
- отправляет напоминания за 3 дня и за 1 день до окончания.

Важно: после автоматического удаления secret proxy тоже должен перечитать config. Бот не имеет доступа к Docker socket, поэтому reload/restart выполняется с host-системы.

## SQLite

База содержит таблицы:

- `users`
- `subscriptions`
- `payments`
- `settings`

Все даты хранятся в UTC.

## Ограничения текущего этапа

- нет автоматической оплаты;
- нет Web Admin Cabinet;
- нет автоматического Docker reload/restart из контейнера бота;
- удаление или создание secret вступает в силу только после `SIGUSR2` reload или restart proxy;
- клиент должен нажать `/start` до ручной выдачи доступа;
- `proxy/config/config.py` локальный runtime-файл и не коммитится.
