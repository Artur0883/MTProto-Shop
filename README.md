# MTProto Shop

Минимальный каркас для проверки Milestone 0: запуск `mtprotoproxy / alexbers`, создание secret, выдача proxy-ссылки, удаление secret и отключение доступа конкретного клиента.

## Milestone 0

### Что входит

- `mtproto` container на базе `alexbers/mtprotoproxy:stable`;
- минимальный `bot` container без Telegram-магазина, оплат, базы данных и web-админки;
- CLI `bot/proxy_manager.py` для управления `USERS` в runtime-файле `proxy/config/config.py`;
- безопасный шаблон `proxy/config/config.example.py` для GitHub;
- secure/dd режим MTProto proxy;
- применение изменений без Docker socket внутри bot-контейнера.

### Режим proxy

Milestone 0 использует secure/dd режим:

```python
MODES = {
    "classic": False,
    "secure": True,
    "tls": False,
}
```

В `mtprotoproxy / alexbers` в `USERS` хранится обычный 32-символьный hex secret. Для Telegram-ссылки `proxy_manager.py` публикует secret с префиксом `dd`:

```text
tg://proxy?server=SERVER_HOST&port=PROXY_PORT&secret=ddSECRET
```

### Безопасность proxy config

`proxy/config/config.example.py` - безопасный шаблон без реальных client secrets. Его можно хранить в GitHub.

`proxy/config/config.py` - локальный runtime-файл. В нем будут реальные client secrets, поэтому он добавлен в `.gitignore` и не должен коммититься или отправляться в GitHub.

Если `proxy/config/config.py` отсутствует, `bot/proxy_manager.py` создаст его из `proxy/config/config.example.py` при первой команде `create`, `delete`, `list` или `link`.

Создать runtime config вручную:

```bash
cp proxy/config/config.example.py proxy/config/config.py
```

Или через CLI:

```bash
python bot/proxy_manager.py list
```

Для первого Docker-запуска, если `config.py` еще нет:

```bash
docker compose run --rm bot python proxy_manager.py list
docker compose up -d
```

### Подготовка `.env`

```bash
cp .env.example .env
```

В `.env` укажите внешний адрес сервера:

```env
SERVER_HOST=1.2.3.4
PROXY_PORT=443
TLS_DOMAIN=www.google.com
```

### VPS live-test

Запустить контейнеры:

```bash
docker compose up -d
```

Создать тестовый secret:

```bash
docker compose exec bot python proxy_manager.py create test-client
```

Применить reload proxy:

```bash
docker compose kill -s SIGUSR2 mtproto
```

Получить ссылку:

```bash
docker compose exec bot python proxy_manager.py link test-client
```

Удалить secret:

```bash
docker compose exec bot python proxy_manager.py delete test-client
```

Снова применить reload:

```bash
docker compose kill -s SIGUSR2 mtproto
```

Fallback, если reload недоступен:

```bash
docker compose restart mtproto
```

### Полезные команды

Проверить контейнеры:

```bash
docker compose ps
```

Посмотреть логи:

```bash
docker compose logs -f
docker compose logs -f mtproto
docker compose logs -f bot
```

Остановить:

```bash
docker compose down
```

### Проверка в Telegram

1. Откройте выданную `tg://proxy?...` ссылку на устройстве с Telegram.
2. Подтвердите подключение proxy.
3. Проверьте, что Telegram работает через proxy.
4. После удаления secret и reload/restart переподключите proxy в Telegram. Старый secret больше не должен проходить аутентификацию.

### Как proxy перечитывает config

`mtprotoproxy / alexbers` загружает `config.py` при старте. В коде proxy есть обработчик `SIGUSR2`, который перечитывает конфиг через `init_config()` и пишет `Config reloaded`.

Bot-контейнер не управляет Docker и не получает доступ к Docker socket. Reload/restart выполняется вручную с host-системы.

Проверить, что reload прошел:

```bash
docker compose logs --tail=50 mtproto
```

Проверено по исходникам и документации alexbers:

- Docker Hub: https://hub.docker.com/r/alexbers/mtprotoproxy
- GitHub README: https://github.com/alexbers/mtprotoproxy
- GitHub source: https://github.com/alexbers/mtprotoproxy/blob/stable/mtprotoproxy.py

### Ограничения Milestone 0

- нет оплаты;
- нет Telegram Bot MVP;
- нет web admin cabinet;
- нет PostgreSQL и SQLite;
- нет nginx;
- нет автоматического Docker restart/reload из bot-контейнера;
- `proxy/config/config.py` локальный и не коммитится;
- удаление secret вступает в силу только после reload через `SIGUSR2` или restart proxy;
- проверка реального подключения зависит от публичного `SERVER_HOST`, открытого `PROXY_PORT` и доступности Telegram.

## Локальная CLI-проверка без Docker

```bash
python bot/proxy_manager.py list
python bot/proxy_manager.py create test-client
python bot/proxy_manager.py link test-client
python bot/proxy_manager.py delete test-client
```
