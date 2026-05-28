# PLAN.md — Полный перевод MTProto-Shop на TeleMT

> **Назначение:** инструкции Codex для миграции проекта на TeleMT как единственное рабочее ядро proxy.
> **Код этим PLAN.md не пишется.** Codex должен реализовать пункты ниже строго по списку файлов и функций.
> Не делать рефакторинг "заодно", не менять архитектуру/Prisma/маршруты бота сверх описанного.

---

## A. Цель

Полностью перевести MTProto-Shop на **TeleMT**:

- единственное ядро proxy — TeleMT;
- alexbers удалён из runtime установки и меню (исторические упоминания в README допустимы);
- проект ставится одной командой `bash <(curl -fsSL https://raw.githubusercontent.com/Artur0883/MTProto-Shop/main/install.sh)` и после пункта 1 у клиента в Telegram появляется рабочая `tg://proxy?...` ссылка с `ee`-секретом;
- TeleMT поднимается внутри `docker compose` как сервис `mtproto` (без отдельного systemd-юнита);
- bot управляет ключами через TeleMT HTTP API внутри docker-сети.

## B. Критерий готовности

На чистом VPS после `mtp` → `1) 🧙 Первичная установка с нуля`:

1. `docker compose ps` показывает: `mtproto-shop-proxy` (TeleMT) — running, `mtproto-shop-bot` — running, опционально `mtproto-shop-support-bot` — running.
2. `docker compose exec bot curl -fsS http://mtproto:9091/v1/users` отвечает JSON-списком пользователей (минимум `shop_bootstrap`).
3. Bot отвечает на `/start` в Telegram.
4. Клиент после `🎁 Попробовать бесплатно` получает кнопку `🔐 Подключиться` с `tg://proxy?...secret=ee...` ссылкой.
5. `bot/subscriptions.py.expire_subscriptions` корректно удаляет пользователя из TeleMT по окончании подписки.
6. `mtp` → `21) 🧪 Проверка установки` возвращает success.

## C. Приоритеты

| Уровень | Содержимое |
|---------|-----------|
| P0 | docker-compose.yml, telemt/config.toml runtime, bot/proxy_manager.py (HTTP-адаптер), bot/main.py (no alexbers gate), bot/config.py (TeleMT settings), .env.example, manage.sh first_setup_wizard |
| P1 | manage.sh меню (убрать выбор ядра, SIGUSR2, ensure_alexbers, switch_proxy_core), scripts/mtproto-restart-watcher.sh (убрать proxy.reload), backup_now |
| P2 | удаление proxy/, deploy/docker-compose.{alexbers,telemt}.yml, README.md, .gitignore/.dockerignore чистка |

---

## D. Изменения по файлам

### D1. `docker-compose.yml` (полностью переписать) — P0

Создать с нуля. Сервис `mtproto` теперь TeleMT, остальные не меняются по контракту.

```yaml
services:
  mtproto:
    image: ghcr.io/telemt/telemt:latest
    container_name: mtproto-shop-proxy
    restart: unless-stopped
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "5"
    env_file:
      - .env
    environment:
      PROXY_PORT: ${PROXY_PORT:-443}
      TLS_DOMAIN: ${TLS_DOMAIN:-www.cloudflare.com}
    ports:
      - "${PROXY_PORT:-443}:443/tcp"
    expose:
      - "9091"
    volumes:
      - ./telemt:/etc/telemt:rw

  bot:
    build:
      context: .
      dockerfile: bot/Dockerfile
    container_name: mtproto-shop-bot
    restart: unless-stopped
    depends_on:
      - mtproto
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "5"
    healthcheck:
      test: ["CMD-SHELL", "test -f /app/data/heartbeats/bot.beat && [ $$(($$(date +%s) - $$(stat -c %Y /app/data/heartbeats/bot.beat))) -lt 90 ]"]
      interval: 60s
      timeout: 5s
      retries: 3
      start_period: 30s
    env_file:
      - .env
    volumes:
      - ./data:/app/data
      - ./backups:/app/backups
      - ./logs:/app/logs

  support_bot:
    build:
      context: .
      dockerfile: bot/Dockerfile
    container_name: mtproto-shop-support-bot
    restart: unless-stopped
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "5"
    healthcheck:
      test: ["CMD-SHELL", "[ -z \"$$SUPPORT_BOT_TOKEN\" ] || (test -f /app/data/heartbeats/support_bot.beat && [ $$(($$(date +%s) - $$(stat -c %Y /app/data/heartbeats/support_bot.beat))) -lt 90 ])"]
      interval: 60s
      timeout: 5s
      retries: 3
      start_period: 60s
    env_file:
      - .env
    command: ["python", "support_bot.py"]
    volumes:
      - ./data:/app/data
      - ./logs:/app/logs
```

Заметки:
- Удалить healthcheck для `mtproto` (TeleMT image не гарантирует `python3` или `nc`). Достаточно проверки через bot из main.py.
- Удалить `PROXY_CONFIG_PATH` env у `bot` и `./proxy/config` volume.
- Не публиковать 9091 наружу — только `expose`.
- Том `./telemt:/etc/telemt:rw` обязательно директория, не файл.

### D2. `telemt/config.toml` runtime — P0

Этот файл создаётся `first_setup_wizard()` в `manage.sh` при первичной установке. **Не коммитить.** Добавить шаблон `telemt/config.example.toml` в репозиторий для документации и копирования.

`telemt/config.example.toml`:

```toml
[general]
use_middle_proxy = true
log_level = "normal"

[general.modes]
classic = false
secure = false
tls = true

[general.links]
show = "*"
public_host = "__SERVER_HOST__"
public_port = __PROXY_PORT__

[server]
port = 443

[server.api]
enabled = true
listen = "0.0.0.0:9091"
whitelist = ["127.0.0.1/32", "::1/128", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]

[[server.listeners]]
ip = "0.0.0.0"

[censorship]
tls_domain = "__TLS_DOMAIN__"
mask = true
tls_emulation = true
tls_front_dir = "tlsfront"

[access.users]
shop_bootstrap = "__SHOP_BOOTSTRAP_SECRET__"
```

`first_setup_wizard()` подставляет:
- `__SERVER_HOST__` → `$WIZARD_SERVER_HOST`
- `__PROXY_PORT__` → `$WIZARD_PROXY_PORT`
- `__TLS_DOMAIN__` → `$WIZARD_TLS_DOMAIN`
- `__SHOP_BOOTSTRAP_SECRET__` → `openssl rand -hex 16`

### D3. `bot/config.py` — P0

Добавить три поля в `Settings`:

```python
proxy_core: str          # всегда "telemt"
telemt_api_url: str      # http://mtproto:9091
telemt_system_user: str  # shop_bootstrap
```

В `get_settings()`:

```python
proxy_core=os.getenv("PROXY_CORE", "telemt").strip().lower() or "telemt",
telemt_api_url=os.getenv("TELEMT_API_URL", "http://mtproto:9091").strip().rstrip("/"),
telemt_system_user=os.getenv("TELEMT_SYSTEM_USER", "shop_bootstrap").strip() or "shop_bootstrap",
```

**Удалить** `proxy_config_path` из `Settings` и из `get_settings()` (больше нигде не используется после миграции).

### D4. `bot/proxy_manager.py` (полностью переписать) — P0

Сохранить внешний интерфейс:

```python
class ClientNotFoundError(ValueError): ...

def mask_secret(secret: str) -> str
def generate_secret() -> str  # 32 hex
def validate_client_id(client_id: str) -> None
def validate_secret(secret: str) -> None
def build_tls_proxy_link(server_host, proxy_port, secret, tls_domain) -> str
def build_proxy_link(server_host, proxy_port, secret) -> str  # можно оставить, не критично

def create_secret(client_id: str, provided_secret: str | None = None) -> str
def delete_secret(client_id: str) -> str
def rotate_secret(client_id: str) -> str
def get_link(client_id: str) -> str
def list_clients() -> dict[str, str]

async def rotate_telegram_secret(telegram_id: int) -> str   # остаётся, использует rotate_secret + DB
def main() -> int                                            # CLI
```

**Удалить:**
- `SUPPORTED_PROXY_CORE`
- `require_supported_proxy_core()`
- `ensure_runtime_config()`, `get_example_config_path()`, `set_runtime_config_permissions()`
- `load_users()`, `write_config()`, `request_proxy_reload()`
- `import runpy`, `import shutil`, `import tempfile` (если больше не нужны)
- print-строки `apply changes: docker compose kill -s SIGUSR2 mtproto` в `main()` CLI — заменить на `apply changes via TeleMT API` (или просто убрать).

**HTTP-клиент:** использовать `urllib.request` из stdlib, **не добавлять** `httpx`/`aiohttp` в requirements.

Реализация (псевдо-схема, Codex детализирует):

```python
import json
import urllib.error
import urllib.request
from config import get_settings

DEFAULT_TIMEOUT = 5.0

def _api_request(method: str, path: str, body: dict | None = None) -> dict | list | None:
    settings = get_settings()
    url = f"{settings.telemt_api_url}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if body is not None else {}
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:
            raw = resp.read()
            if not raw:
                return None
            return json.loads(raw)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise ClientNotFoundError(f"telemt 404 for {method} {path}")
        raise RuntimeError(f"telemt API {method} {path} failed: {exc.code} {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"telemt API {method} {path} unreachable: {exc.reason}") from exc

def create_secret(client_id: str, provided_secret: str | None = None) -> str:
    validate_client_id(client_id)
    secret = (provided_secret or generate_secret()).lower()
    validate_secret(secret)
    # idempotent: если уже есть — вернуть его
    try:
        existing = _api_request("GET", f"/v1/users/{client_id}")
        if existing:
            current = _extract_secret(existing)
            if current:
                return current
    except ClientNotFoundError:
        pass
    _api_request("POST", "/v1/users", {"username": client_id, "secret": secret})
    return secret

def delete_secret(client_id: str) -> str:
    validate_client_id(client_id)
    settings = get_settings()
    if client_id == settings.telemt_system_user:
        raise ValueError(f"refusing to delete system user '{client_id}'")
    try:
        existing = _api_request("GET", f"/v1/users/{client_id}")
    except ClientNotFoundError:
        raise ClientNotFoundError(f"client '{client_id}' not found")
    secret = _extract_secret(existing) or ""
    try:
        _api_request("DELETE", f"/v1/users/{client_id}")
    except ClientNotFoundError:
        raise ClientNotFoundError(f"client '{client_id}' not found")
    return secret

def rotate_secret(client_id: str) -> str:
    validate_client_id(client_id)
    new_secret = generate_secret()
    try:
        resp = _api_request("POST", f"/v1/users/{client_id}/rotate-secret",
                            {"secret": new_secret})
    except ClientNotFoundError:
        raise ClientNotFoundError(f"client '{client_id}' not found")
    return _extract_secret(resp) or new_secret

def get_link(client_id: str) -> str:
    validate_client_id(client_id)
    settings = get_settings()
    try:
        resp = _api_request("GET", f"/v1/users/{client_id}")
    except ClientNotFoundError:
        raise ClientNotFoundError(f"client '{client_id}' not found")
    tls_links = (((resp or {}).get("user") or resp or {}).get("links") or {}).get("tls") or []
    if tls_links:
        return tls_links[0]
    secret = _extract_secret(resp) or ""
    if not secret:
        raise RuntimeError(f"client '{client_id}' has no secret to build link")
    return build_tls_proxy_link(settings.server_host, settings.proxy_port, secret, settings.tls_domain)

def list_clients() -> dict[str, str]:
    settings = get_settings()
    resp = _api_request("GET", "/v1/users") or {}
    users = resp.get("users") if isinstance(resp, dict) else resp
    result: dict[str, str] = {}
    if isinstance(users, list):
        for entry in users:
            name = entry.get("username") or entry.get("name")
            if not name or name == settings.telemt_system_user:
                continue
            secret = _extract_secret(entry) or ""
            result[name] = secret.lower()
    elif isinstance(users, dict):
        for name, entry in users.items():
            if name == settings.telemt_system_user:
                continue
            if isinstance(entry, str):
                result[name] = entry.lower()
            else:
                result[name] = (_extract_secret(entry) or "").lower()
    return result

def _extract_secret(entry) -> str | None:
    if entry is None:
        return None
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        # подобрать ключ — TeleMT может отдавать "secret", "client_secret", "user.secret" и т.п.
        for key in ("secret", "client_secret"):
            value = entry.get(key)
            if isinstance(value, str):
                return value
        user_node = entry.get("user")
        if isinstance(user_node, dict):
            return _extract_secret(user_node)
    return None
```

**Важно** для Codex:
- если `list_clients()` от TeleMT не отдаёт `secret`, в `ensure_secret()` callsite (admin.py / client.py) при пустой строке — делать `create_secret(client_id, preferred_secret)` (как и сейчас);
- `rotate_telegram_secret()` остаётся как сейчас, но без `require_supported_proxy_core()`;
- CLI `main()` остаётся: команды `create`, `delete`, `rotate`, `rotate-telegram`, `link`, `list` — все используют новые функции;
- системный пользователь `shop_bootstrap` фильтруется из `list_clients()` и блокируется в `delete_secret()`.

### D5. `bot/main.py` — P0

```python
# было:
from proxy_manager import ensure_runtime_config, require_supported_proxy_core
...
require_supported_proxy_core()
ensure_runtime_config(settings.proxy_config_path)
```

Заменить на:

```python
import time
import urllib.request
import urllib.error
...
def _wait_for_telemt(api_url: str, attempts: int = 30, delay: float = 2.0) -> None:
    last_err: Exception | None = None
    for _ in range(attempts):
        try:
            req = urllib.request.Request(f"{api_url}/v1/users", method="GET")
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                if resp.status < 500:
                    return
        except Exception as exc:
            last_err = exc
        time.sleep(delay)
    logging.warning("TeleMT API still not responding after %ss: %s",
                    attempts * delay, last_err)
...
async def main() -> None:
    settings = get_settings()
    ...
    _wait_for_telemt(settings.telemt_api_url)
    # никакого ensure_runtime_config, никакого require_supported_proxy_core
```

Заметки:
- Если TeleMT отдаёт `/v1/health` — допустимо использовать его. `/v1/users` работает всегда и не требует знаний о точном endpoint.
- Логи `Proxy config path: %s` убрать; добавить `logging.info("TeleMT API: %s", settings.telemt_api_url)`.

### D6. `bot/admin.py` и `bot/client.py` — P0

Минимальные правки, интерфейс остаётся:

**admin.py:**
- константа `SIGUSR2_NOTICE` — переименовать в `PROXY_APPLY_NOTICE` и поменять текст на:
  ```python
  PROXY_APPLY_NOTICE = "✅ Ключ применён в TeleMT мгновенно через API."
  ```
  И обновить все её use-site (включая `access_disabled_text`, `admin_card_delete_yes`).
- сообщения вида `"✅ Ключ применится автоматически в течение ~5 секунд."` оставить как пользовательский текст или укоротить (опционально).
- `ensure_secret()` остаётся, но при пустом `secret` в `users` — он сам сделает `create_secret(client_id, preferred_secret)`. Codex не меняет логику, но проверяет, что при пустой строке вызов идёт в create.

**client.py:**
- `ensure_secret()` — то же самое.

**Никакие маршруты, FSM, handlers, тарифы и БД-таблицы не трогать.**

### D7. `bot/subscriptions.py` — P0

Изменений не требуется по контракту: уже использует `delete_secret(client_id)` и `ClientNotFoundError`. После замены `proxy_manager.py` `expire_subscriptions()` автоматически работает через TeleMT API.

### D8. `bot/Dockerfile` — P0 (мелкая правка)

Не требует изменений, **если** `urllib.request` достаточно. Если Codex решит использовать `httpx`, обновить `bot/requirements.txt`. **Рекомендуется stdlib.**

### D9. `.env.example` — P0

```env
BOT_TOKEN=
SUPPORT_BOT_TOKEN=
ADMIN_ID=

SERVER_HOST=
PROXY_PORT=443
TLS_DOMAIN=www.cloudflare.com

PROXY_CORE=telemt
TELEMT_API_URL=http://mtproto:9091
TELEMT_SYSTEM_USER=shop_bootstrap

SUPPORT_CONTACT=
DATABASE_PATH=/app/data/shop.db

PAYMENT_MODE=manual
DEV_AUTO_ISSUE=false
```

Удалить `TELEMT_CONFIG_PATH` (не нужен боту).

### D10. `manage.sh` — P0 / P1

**Меню (P0):** заменить блок `while true` на новые пункты (см. ниже). Удалить пункт `21) 🧩 Выбор / смена ядра proxy`. Текущий пункт `22) 🧪 Проверка установки` становится новым `21)`. Метку proxy → "Логи TeleMT".

```
1) 🧙 Первичная установка с нуля
2) 🚀 Установка / обновление / запуск
3) ✅ Статус контейнеров
4) 📄 Логи бота
5) 📄 Логи TeleMT
6) 📋 Список ключей
7) ➕ Добавить ключ вручную
8) 🔄 Обновить ключ клиента по Telegram ID + синхронизировать SQLite
9) 🔗 Показать ссылку по Telegram ID
10) 🔗 Показать ссылку по client_id
11) ❌ Удалить ключ по client_id
12) ♻️ Проверить TeleMT API / применить изменения
13) ⚙️ Открыть .env
14) 💾 Сделать бэкап
15) 🔁 Пересоздать Telegram-ботов
16) 🔁 Пересоздать TeleMT proxy
17) 🔁 Пересоздать ботов + TeleMT proxy
18) 🖥️ Перезагрузить VPS полностью
19) 📄 Логи бота поддержки
20) 🛡 Установить watcher автоматического рестарта
21) 🧪 Проверка установки
0) 🚪 Выход
```

**Функции `manage.sh` (изменения):**

| Функция | Действие |
|---------|----------|
| `read_proxy_core` | **Удалить**. Wizard не спрашивает выбор ядра. |
| `prepare_compose_for_core` | **Удалить**. `docker-compose.yml` в репо уже TeleMT — копирование из `deploy/` не нужно. |
| `ensure_alexbers_runtime_config` | **Удалить**. |
| `switch_proxy_core` | **Удалить**. Пункт меню снят. |
| `reload_proxy` | Переименовать в `check_telemt_api`. Делает `docker compose exec bot curl -fsS http://mtproto:9091/v1/users -o /dev/null` (или через `wget`). Печатает success/fail. Никаких SIGUSR2. |
| `show_proxy_logs` | Сохранить, только переименовать заголовок на `Логи TeleMT`. |
| `backup_now` | Бэкапить `telemt/` (директорию) вместо `proxy/config`. Шаблон: `tar -czf "$archive" data telemt .env docker-compose.yml 2>/dev/null \|\| true`. |
| `check_installation` | Удалить проверку `PROXY_CORE=alexbers`; добавить проверку, что `http://mtproto:9091/v1/users` отвечает (через `docker compose exec bot`). |
| `first_setup_wizard` | Не вызывать `read_proxy_core`. Не вызывать `prepare_compose_for_core`. Перед `start_and_verify_installation` сгенерировать `telemt/config.toml` из `telemt/config.example.toml` с подстановкой `SERVER_HOST/PROXY_PORT/TLS_DOMAIN` и `openssl rand -hex 16` для `shop_bootstrap`. |
| `write_wizard_env` | Всегда писать `PROXY_CORE=telemt`. Добавить `TELEMT_API_URL=http://mtproto:9091` и `TELEMT_SYSTEM_USER=shop_bootstrap`. Убрать `TELEMT_CONFIG_PATH`. |
| `start_and_verify_installation` | После `docker compose up` подождать TeleMT (loop с `docker compose exec bot curl …` 30×2с), затем проверить контейнер bot. |
| case-роутинг меню | Обновить под новые номера пунктов; `12) check_telemt_api`, `21) check_installation`; убрать `switch_proxy_core`. |

**Генерация `telemt/config.toml` в wizard (sed-подстановка):**

```bash
write_telemt_config() {
  mkdir -p telemt
  local secret
  secret="$(openssl rand -hex 16)"
  sed \
    -e "s|__SERVER_HOST__|${WIZARD_SERVER_HOST}|g" \
    -e "s|__PROXY_PORT__|${WIZARD_PROXY_PORT}|g" \
    -e "s|__TLS_DOMAIN__|${WIZARD_TLS_DOMAIN}|g" \
    -e "s|__SHOP_BOOTSTRAP_SECRET__|${secret}|g" \
    telemt/config.example.toml > telemt/config.toml
  chmod 600 telemt/config.toml
}
```

Вызвать `write_telemt_config` сразу после `write_wizard_env`.

### D11. `scripts/mtproto-restart-watcher.sh` — P1

Удалить блок `if [[ -f "$PROXY_SENTINEL" ]] ... fi` (SIGUSR2-перезагрузка alexbers). Остальная логика рестарта `bot/support_bot/mtproto` через `data/restart.request` остаётся.

### D12. `.gitignore` — P2

Текущее достаточно за исключением `proxy/config/config.py` (директория удаляется целиком). Привести к виду:

```
.env
.env.*
!.env.example
data/
backups/
logs/
telemt/config.toml
telemt/*.tmp
*.db
*.sqlite
*.log
__pycache__/
.venv/

.claude/
```

### D13. `.dockerignore` — P2

То же самое: убрать `proxy/config/config.py`, добавить `telemt/config.toml`, оставить `telemt/config.example.toml` доступным (через белый список не требуется — он не *.toml.tmp). 

```
.env
.env.*
!.env.example
data/
backups/
logs/
.git/
.claude/
__pycache__/
**/__pycache__/
telemt/config.toml
telemt/*.tmp
*.db
*.sqlite
*.log
node_modules/
dist/
server/dist/
coverage/
```

### D14. Удалить файлы / директории — P2

```
proxy/                                    # вся директория, alexbers config
deploy/docker-compose.alexbers.yml
deploy/docker-compose.telemt.yml
```

Если `deploy/` после этого пуст — удалить и его.

### D15. `install.sh` — без изменений (P2 проверка)

Проверить, что:
- Репозиторий `Artur0883/MTProto-Shop` подставлен.
- Команда `mtp` создаётся.
- UFW открывает 80/tcp, 443/tcp, OpenSSH.

Никаких правок. Универсальный установщик, не зависит от ядра.

### D16. `README.md` — P2

Обновить:
- "MTProto proxy через `alexbers/mtprotoproxy`" → "MTProto proxy через TeleMT (`ghcr.io/telemt/telemt:latest`)".
- Блок `Переменные окружения`: убрать `PROXY_CORE=alexbers`, поставить `PROXY_CORE=telemt`, добавить `TELEMT_API_URL`, `TELEMT_SYSTEM_USER`. Удалить `TELEMT_CONFIG_PATH`.
- Главное меню VPS: обновить новый список пунктов 1-21 (без 22, без switch_proxy_core).
- Раздел "Ручные команды": убрать `docker compose kill -s SIGUSR2 mtproto`; заменить на `docker compose exec bot curl -fsS http://mtproto:9091/v1/users`.
- "Быстрая установка с нуля" — оставить как есть, формулировку про "TeleMT показан в выборе ядра, но заблокирован" удалить.
- В разделе "Важные файлы": заменить `proxy/config/config.py` на `telemt/config.toml`.

---

## E. Проверки (Codex запускает в конце)

```bash
bash -n install.sh
bash -n manage.sh
python -m compileall bot
docker compose config
docker compose -f docker-compose.yml config

# runtime-упоминаний alexbers быть не должно (README history допустим):
grep -RIn "alexbers" --exclude-dir=.git --exclude-dir=.claude --exclude=README.md .
grep -RIn "mtprotoproxy" --exclude-dir=.git --exclude-dir=.claude --exclude=README.md .
grep -RIn "SUPPORTED_PROXY_CORE" bot manage.sh
grep -RIn "SIGUSR2" bot manage.sh scripts
grep -RIn "runpy" bot
grep -RIn "proxy/config" bot manage.sh docker-compose.yml
```

Ожидаемые результаты:
- `bash -n` — без ошибок.
- `python -m compileall bot` — без ошибок (нет syntax errors).
- `docker compose config` — печатает валидный merged config; видно `image: ghcr.io/telemt/telemt:latest`.
- `grep alexbers/mtprotoproxy` в runtime коде — **пусто**.
- `grep SUPPORTED_PROXY_CORE/SIGUSR2/runpy/proxy/config` — **пусто** в `bot/` и `manage.sh`.

После применения изменений на чистом VPS:

```bash
cd /opt/mtproto-shop && bash manage.sh   # пункт 1 → пункт 21
docker compose ps                         # mtproto-shop-proxy/-bot running
docker compose exec bot python -c "import urllib.request,json; print(json.loads(urllib.request.urlopen('http://mtproto:9091/v1/users',timeout=3).read()))"
docker compose exec bot python proxy_manager.py create tg_123456
docker compose exec bot python proxy_manager.py link   tg_123456
docker compose exec bot python proxy_manager.py delete tg_123456
```

---

## F. Готовый промт для Codex

> Выполни миграцию проекта `MTProto-Shop` на TeleMT строго по `PLAN.md`. Не делай правок вне списка файлов. Соблюдай порядок:
>
> 1. **P0:** `docker-compose.yml` (D1), `telemt/config.example.toml` (D2), `bot/config.py` (D3), `bot/proxy_manager.py` (D4 — полная замена через `urllib.request`, **без новых зависимостей**), `bot/main.py` (D5), `bot/admin.py` + `bot/client.py` (D6, только переименование `SIGUSR2_NOTICE`), `.env.example` (D9), `manage.sh` `first_setup_wizard`/`write_wizard_env`/новая `write_telemt_config`/`start_and_verify_installation`/`check_installation`/`reload_proxy→check_telemt_api`/`backup_now` (D10).
> 2. **P1:** меню `manage.sh` (D10), `scripts/mtproto-restart-watcher.sh` (D11).
> 3. **P2:** `.gitignore` (D12), `.dockerignore` (D13), удалить `proxy/`, `deploy/docker-compose.alexbers.yml`, `deploy/docker-compose.telemt.yml` (D14), `README.md` (D16).
>
> После каждой группы запускай проверки из раздела E. При расхождениях формата ответа TeleMT API (например, путь к секрету или к `links.tls`) — расширь функцию `_extract_secret` и `get_link`, остальные сигнатуры функций менять нельзя — их вызывают `bot/admin.py`, `bot/client.py`, `bot/subscriptions.py`.
>
> Не добавляй `httpx`/`aiohttp` в `bot/requirements.txt`. Не трогай Prisma/маршруты/тарифы/keyboards/database.py.
>
> Критерий готовности — раздел B `PLAN.md`.

---

## G. Итог

- Документ заменяет старый `PLAN.md` (alexbers-эра). Все runtime-упоминания alexbers удаляются.
- TeleMT поднимается через docker compose как `mtproto`, API доступен только из docker-сети.
- `bot/proxy_manager.py` становится тонким HTTP-адаптером поверх TeleMT API. Интерфейс функций сохранён ⇒ `admin.py`, `client.py`, `subscriptions.py` правятся минимально.
- Wizard ставит проект на чистый VPS одной командой и пунктом 1 в меню.
