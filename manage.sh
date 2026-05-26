#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

COMPOSE_CMD="docker compose"
if ! docker compose version >/dev/null 2>&1; then
  if docker-compose version >/dev/null 2>&1; then
    COMPOSE_CMD="docker-compose"
  fi
fi

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m'

pause() {
  echo
  read -r -p "Нажмите Enter, чтобы вернуться в меню..." _
}

print_header() {
  clear || true
  echo "========================================"
  echo "        MTProto Shop — меню управления"
  echo "========================================"
  echo
}

need_env() {
  if [[ ! -f .env ]]; then
    echo -e "${YELLOW}.env не найден. Создаю из .env.example...${NC}"
    cp .env.example .env
    chmod 600 .env
    echo -e "${RED}Заполните .env: BOT_TOKEN, ADMIN_ID и SERVER_HOST. SUPPORT_BOT_TOKEN нужен только для отдельного бота поддержки.${NC}"
    return 1
  fi
}

require_root() {
  if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    echo -e "${RED}Эта операция должна выполняться от root. Запустите: sudo mtp${NC}"
    return 1
  fi
}

require_supported_os() {
  if [[ ! -r /etc/os-release ]]; then
    echo -e "${RED}Не удалось определить операционную систему.${NC}"
    return 1
  fi

  # shellcheck disable=SC1091
  . /etc/os-release
  case "${ID:-}" in
    ubuntu|debian) return 0 ;;
    *)
      echo -e "${RED}Поддерживается только Ubuntu или Debian. Найдено: ${PRETTY_NAME:-unknown}.${NC}"
      return 1
      ;;
  esac
}

install_required_packages() {
  echo -e "${BLUE}Проверяю системные зависимости...${NC}"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y curl git nano unzip jq ufw ca-certificates openssl
}

ensure_docker_installed() {
  if ! command -v docker >/dev/null 2>&1; then
    echo -e "${BLUE}Docker не найден. Устанавливаю Docker...${NC}"
    export DEBIAN_FRONTEND=noninteractive
    apt-get install -y docker.io
  fi

  systemctl enable --now docker >/dev/null 2>&1 || service docker start >/dev/null 2>&1 || true

  if ! docker version >/dev/null 2>&1; then
    echo -e "${RED}Docker установлен, но daemon недоступен. Проверьте: systemctl status docker${NC}"
    return 1
  fi

  if ! docker compose version >/dev/null 2>&1; then
    echo -e "${BLUE}Устанавливаю Docker Compose plugin...${NC}"
    export DEBIAN_FRONTEND=noninteractive
    apt-get install -y docker-compose-plugin >/dev/null 2>&1 \
      || apt-get install -y docker-compose-v2 >/dev/null 2>&1 \
      || true
  fi
  if ! docker compose version >/dev/null 2>&1; then
    local docker_installer
    docker_installer="$(mktemp)"
    echo -e "${BLUE}Устанавливаю официальный Docker Engine с Compose plugin...${NC}"
    curl -fsSL https://get.docker.com -o "$docker_installer"
    sh "$docker_installer"
    rm -f "$docker_installer"
    systemctl enable --now docker >/dev/null 2>&1 || true
  fi

  if ! docker compose version >/dev/null 2>&1; then
    echo -e "${RED}Команда 'docker compose' недоступна. Установите Docker Compose plugin и повторите настройку.${NC}"
    return 1
  fi

  COMPOSE_CMD="docker compose"
}

get_env_value() {
  local key="$1"
  [[ -f .env ]] || return 0
  sed -n "s/^${key}=//p" .env | tail -n 1
}

mask_token() {
  local token="$1"
  if [[ ${#token} -le 12 ]]; then
    printf '***hidden'
  else
    printf '%s...hidden' "${token:0:12}"
  fi
}

check_telegram_token() {
  local token="$1"
  local response
  response="$(mktemp)"

  if curl -fsS --max-time 15 "https://api.telegram.org/bot${token}/getMe" -o "$response" \
    && jq -e '.ok == true and (.result.username | type == "string")' "$response" >/dev/null 2>&1; then
    TELEGRAM_BOT_USERNAME="$(jq -r '.result.username' "$response")"
    rm -f "$response"
    return 0
  fi

  rm -f "$response"
  return 1
}

is_port_listening() {
  local port="$1"
  if command -v ss >/dev/null 2>&1; then
    ss -H -ltn "sport = :${port}" 2>/dev/null | grep -q .
    return
  fi

  (echo >"/dev/tcp/127.0.0.1/${port}") >/dev/null 2>&1
}

check_proxy_port() {
  local port="$1"
  if ! is_port_listening "$port"; then
    return 0
  fi

  if docker ps --filter "name=^/mtproto-shop-proxy$" --format '{{.Ports}}' 2>/dev/null \
    | grep -q ":${port}->"; then
    echo -e "${YELLOW}Порт ${port} уже использует существующий контейнер mtproto-shop-proxy; он будет пересоздан.${NC}"
    return 0
  fi

  echo -e "${RED}Порт ${port} уже занят не контейнером проекта. Освободите порт или выберите другой.${NC}"
  return 1
}

read_bot_token() {
  while true; do
    echo "Введите BOT_TOKEN основного Telegram-бота:"
    read -r -s -p "> " WIZARD_BOT_TOKEN
    echo
    if [[ -z "$WIZARD_BOT_TOKEN" ]]; then
      echo -e "${RED}BOT_TOKEN не может быть пустым.${NC}"
      continue
    fi
    if check_telegram_token "$WIZARD_BOT_TOKEN"; then
      WIZARD_BOT_USERNAME="$TELEGRAM_BOT_USERNAME"
      echo -e "${GREEN}✅ Telegram API проверен: @${TELEGRAM_BOT_USERNAME}${NC}"
      return 0
    fi
    echo -e "${RED}BOT_TOKEN не прошёл проверку Telegram API. Проверьте токен и повторите ввод.${NC}"
  done
}

read_admin_id() {
  while true; do
    echo "Введите ваш Telegram ADMIN_ID:"
    read -r -p "> " WIZARD_ADMIN_ID
    if [[ "$WIZARD_ADMIN_ID" =~ ^[0-9]+$ ]]; then
      return 0
    fi
    echo -e "${RED}ADMIN_ID должен содержать только цифры.${NC}"
  done
}

read_server_host() {
  local external_ip answer
  external_ip="$(curl -4 -fsS --max-time 10 ifconfig.me 2>/dev/null || true)"
  if [[ "$external_ip" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
    echo "Внешний IP сервера найден: ${external_ip}"
    read -r -p "Использовать его? [Y/n] " answer
    if [[ -z "$answer" || "$answer" =~ ^[Yy]$ ]]; then
      WIZARD_SERVER_HOST="$external_ip"
      return 0
    fi
  else
    echo -e "${YELLOW}Не удалось автоматически определить внешний IPv4.${NC}"
  fi

  while true; do
    echo "Введите внешний IP или домен сервера:"
    read -r -p "> " WIZARD_SERVER_HOST
    if [[ "$WIZARD_SERVER_HOST" =~ ^[A-Za-z0-9._:-]+$ ]]; then
      return 0
    fi
    echo -e "${RED}Укажите непустой IP или домен без пробелов.${NC}"
  done
}

read_proxy_port() {
  while true; do
    echo "Введите порт MTProto proxy [443]:"
    read -r -p "> " WIZARD_PROXY_PORT
    WIZARD_PROXY_PORT="${WIZARD_PROXY_PORT:-443}"
    if [[ "$WIZARD_PROXY_PORT" =~ ^[0-9]+$ ]] \
      && (( WIZARD_PROXY_PORT >= 1 && WIZARD_PROXY_PORT <= 65535 )); then
      check_proxy_port "$WIZARD_PROXY_PORT" && return 0
    else
      echo -e "${RED}Порт должен быть числом от 1 до 65535.${NC}"
    fi
  done
}

read_tls_domain() {
  local choice
  while true; do
    echo "Выберите TLS_DOMAIN для маскировки:"
    echo
    echo "1) protovich.ru — рекомендовано, как в успешной установке TeleMT"
    echo "2) www.cloudflare.com"
    echo "3) Ввести свой домен"
    echo
    read -r -p "Ваш выбор [1]: " choice
    choice="${choice:-1}"
    case "$choice" in
      1) WIZARD_TLS_DOMAIN="protovich.ru" ;;
      2) WIZARD_TLS_DOMAIN="www.cloudflare.com" ;;
      3)
        echo "Введите свой TLS_DOMAIN для маскировки:"
        read -r -p "> " WIZARD_TLS_DOMAIN
        ;;
      *)
        echo -e "${RED}Выберите пункт от 1 до 3.${NC}"
        continue
        ;;
    esac
    if [[ "$WIZARD_TLS_DOMAIN" =~ ^[A-Za-z0-9.-]+$ ]]; then
      return 0
    fi
    echo -e "${RED}TLS_DOMAIN должен быть доменным именем без пробелов.${NC}"
  done
}

read_support_settings() {
  while true; do
    echo "Введите контакт поддержки, например @username или ссылку [можно оставить пустым]:"
    read -r -p "> " WIZARD_SUPPORT_CONTACT
    if [[ -z "$WIZARD_SUPPORT_CONTACT" || "$WIZARD_SUPPORT_CONTACT" =~ ^[^[:space:]#=]+$ ]]; then
      break
    fi
    echo -e "${RED}Контакт не должен содержать пробелы, # или =.${NC}"
  done

  while true; do
    echo "Введите SUPPORT_BOT_TOKEN отдельного бота поддержки [можно оставить пустым]:"
    read -r -s -p "> " WIZARD_SUPPORT_BOT_TOKEN
    echo
    [[ -n "$WIZARD_SUPPORT_BOT_TOKEN" ]] || return 0
    if [[ "$WIZARD_SUPPORT_BOT_TOKEN" == "$WIZARD_BOT_TOKEN" ]]; then
      echo -e "${RED}Для поддержки нужен отдельный бот, токены не должны совпадать.${NC}"
      continue
    fi
    if check_telegram_token "$WIZARD_SUPPORT_BOT_TOKEN"; then
      echo -e "${GREEN}✅ Токен support bot проверен через Telegram API.${NC}"
      return 0
    fi
    echo -e "${RED}SUPPORT_BOT_TOKEN не прошёл проверку Telegram API. Повторите ввод или оставьте пустым.${NC}"
  done
}

read_payment_settings() {
  local choice answer
  while true; do
    echo "Выберите режим оплаты:"
    echo
    echo "1) manual — ручная выдача / ручная оплата"
    echo "2) auto_free — автоматическая выдача без оплаты для теста"
    echo "3) stars — Telegram Stars, если реализовано"
    echo "4) crypto — крипто, если реализовано"
    echo
    read -r -p "Ваш выбор [1]: " choice
    choice="${choice:-1}"
    case "$choice" in
      1) WIZARD_PAYMENT_MODE="manual"; break ;;
      2) WIZARD_PAYMENT_MODE="auto_free"; break ;;
      3|4)
        echo -e "${YELLOW}Этот режим оплаты ещё не реализован. Выберите manual или auto_free.${NC}"
        ;;
      *) echo -e "${RED}Выберите пункт от 1 до 4.${NC}" ;;
    esac
  done

  read -r -p "Включить автоматическую тестовую выдачу ключа без оплаты? [y/N]: " answer
  if [[ "$answer" =~ ^[Yy]$ ]]; then
    WIZARD_DEV_AUTO_ISSUE="true"
  else
    WIZARD_DEV_AUTO_ISSUE="false"
  fi
}

write_wizard_env() {
  local backup=""
  if [[ -f .env ]]; then
    backup=".env.backup-$(date +%Y%m%d-%H%M%S)"
    cp -p .env "$backup"
    chmod 600 "$backup"
    echo -e "${YELLOW}Предыдущий .env сохранён: ${backup}${NC}"
  fi

  umask 077
  cat > .env <<EOF
BOT_TOKEN=${WIZARD_BOT_TOKEN}
SUPPORT_BOT_TOKEN=${WIZARD_SUPPORT_BOT_TOKEN}
ADMIN_ID=${WIZARD_ADMIN_ID}

SERVER_HOST=${WIZARD_SERVER_HOST}
PROXY_PORT=${WIZARD_PROXY_PORT}
TLS_DOMAIN=${WIZARD_TLS_DOMAIN}

PROXY_CORE=telemt
TELEMT_API_URL=http://mtproto:9091
TELEMT_SYSTEM_USER=shop_bootstrap

SUPPORT_CONTACT=${WIZARD_SUPPORT_CONTACT}
DATABASE_PATH=/app/data/shop.db

PAYMENT_MODE=${WIZARD_PAYMENT_MODE}
DEV_AUTO_ISSUE=${WIZARD_DEV_AUTO_ISSUE}
EOF
  chmod 600 .env
}

write_telemt_config() {
  mkdir -p telemt data backups logs
  if [[ ! -f telemt/config.example.toml ]]; then
    echo -e "${RED}Не найден шаблон telemt/config.example.toml.${NC}"
    return 1
  fi
  local secret server_host proxy_port tls_domain
  server_host="$(get_env_value SERVER_HOST)"
  proxy_port="$(get_env_value PROXY_PORT)"
  tls_domain="$(get_env_value TLS_DOMAIN)"
  if [[ -z "$server_host" || -z "$proxy_port" || -z "$tls_domain" ]]; then
    echo -e "${RED}В .env должны быть заполнены SERVER_HOST, PROXY_PORT и TLS_DOMAIN.${NC}"
    return 1
  fi
  secret="$(openssl rand -hex 16)"
  sed \
    -e "s|__SERVER_HOST__|${server_host}|g" \
    -e "s|__PROXY_PORT__|${proxy_port}|g" \
    -e "s|__TLS_DOMAIN__|${tls_domain}|g" \
    -e "s|__SHOP_BOOTSTRAP_SECRET__|${secret}|g" \
    telemt/config.example.toml > telemt/config.toml
  chown 65532:65532 telemt telemt/config.toml
  chmod 700 telemt
  chmod 600 telemt/config.toml
  echo "Создан runtime config TeleMT: telemt/config.toml"
}

container_is_running() {
  local name="$1"
  [[ "$(docker inspect -f '{{.State.Status}}' "$name" 2>/dev/null || true)" == "running" ]]
}

container_is_healthy() {
  local name="$1"
  [[ "$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$name" 2>/dev/null || true)" == "healthy" ]]
}

open_custom_proxy_port() {
  local port="$1"
  if command -v ufw >/dev/null 2>&1 && [[ "$port" != "443" ]]; then
    ufw allow "${port}/tcp" >/dev/null
    echo "Firewall: открыт порт ${port}/tcp."
  fi
}

start_and_verify_installation() {
  local attempt
  echo -e "${BLUE}Этап 1/2: запускаю TeleMT...${NC}"
  $COMPOSE_CMD up -d --force-recreate mtproto

  if ! container_is_running "mtproto-shop-proxy"; then
    echo -e "${RED}Контейнер mtproto-shop-proxy не запущен. Установка не завершена.${NC}"
    $COMPOSE_CMD logs --tail=100 mtproto || true
    return 1
  fi

  echo -e "${BLUE}Собираю образ bot для проверки TeleMT из compose-сети...${NC}"
  $COMPOSE_CMD build bot

  echo -e "${BLUE}Ожидаю готовности TeleMT API...${NC}"
  for attempt in {1..30}; do
    if $COMPOSE_CMD run --rm --no-deps bot python -c \
      "import urllib.request; urllib.request.urlopen('http://mtproto:9091/v1/users', timeout=2).read()" \
      >/dev/null 2>&1; then
      echo -e "${GREEN}✅ TeleMT запущен${NC}"
      echo -e "${GREEN}✅ TeleMT API работает${NC}"
      echo -e "${GREEN}✅ TLS/ee ссылка создаётся${NC}"
      break
    fi
    if [[ "$attempt" -eq 30 ]]; then
      echo -e "${RED}TeleMT API не ответил за 60 секунд. Последние строки логов:${NC}"
      $COMPOSE_CMD logs --tail=100 mtproto || true
      return 1
    fi
    sleep 2
  done

  echo -e "${BLUE}Этап 2/2: запускаю Telegram-ботов...${NC}"
  $COMPOSE_CMD up -d --build bot support_bot

  echo -e "${BLUE}Ожидаю healthy-статус основного Telegram-бота...${NC}"
  for attempt in {1..60}; do
    if container_is_running "mtproto-shop-bot" && container_is_healthy "mtproto-shop-bot"; then
      echo -e "${GREEN}✅ Основной Telegram-бот запущен и healthy.${NC}"
      break
    fi
    if [[ "$attempt" -eq 60 ]]; then
      echo -e "${RED}Основной Telegram-бот не получил healthy-статус. Последние строки логов:${NC}"
      $COMPOSE_CMD logs --tail=100 bot || true
      return 1
    fi
    sleep 2
  done

  if [[ -n "$WIZARD_SUPPORT_BOT_TOKEN" ]] && ! container_is_running "mtproto-shop-support-bot"; then
    echo -e "${RED}Токен поддержки указан, но контейнер support_bot не запущен.${NC}"
    $COMPOSE_CMD logs --tail=100 support_bot || true
    return 1
  fi

  echo -e "${BLUE}Проверяю запуск polling в логах бота...${NC}"
  for attempt in {1..15}; do
    if $COMPOSE_CMD logs --tail=100 bot 2>&1 | grep -q "Start polling"; then
      echo -e "${GREEN}✅ В логах bot найден Start polling.${NC}"
      return 0
    fi
    if [[ "$attempt" -eq 15 ]]; then
      echo -e "${RED}В логах bot не найден Start polling. Последние строки логов:${NC}"
      $COMPOSE_CMD logs --tail=100 bot || true
      return 1
    fi
    sleep 2
  done
}

first_setup_wizard() {
  echo -e "${BLUE}Первичная установка MTProto Shop${NC}"
  require_root || return 1
  require_supported_os || return 1
  install_required_packages || return 1
  ensure_docker_installed || return 1

  read_bot_token
  read_admin_id
  read_server_host
  read_proxy_port
  read_tls_domain
  read_support_settings
  read_payment_settings

  write_wizard_env
  write_telemt_config || return 1
  open_custom_proxy_port "$WIZARD_PROXY_PORT"
  start_and_verify_installation || return 1

  if ! check_telegram_token "$WIZARD_BOT_TOKEN"; then
    echo -e "${RED}После запуска не удалось повторно проверить BOT_TOKEN через Telegram API.${NC}"
    echo "Контейнер запущен, но установка требует проверки сети и токена через пункт 21."
    return 1
  fi
  WIZARD_BOT_USERNAME="$TELEGRAM_BOT_USERNAME"
  echo -e "${GREEN}✅ Telegram API проверен: @${WIZARD_BOT_USERNAME}${NC}"
  echo
  $COMPOSE_CMD ps
  echo
  echo "=================================================="
  echo "✅ Установка завершена"
  echo "✅ TeleMT работает"
  echo "✅ Telegram-бот работает"
  echo "✅ Откройте Telegram-бота и нажмите /start один раз"
  echo "✅ После этого появятся кнопки меню"
  echo "=================================================="
  echo
  echo "Проект установлен в:"
  echo "/opt/mtproto-shop"
  echo
  echo "Команда управления:"
  echo "mtp"
  echo
  echo "Ядро proxy:"
  echo "telemt"
  echo
  echo "Сервер:"
  echo "${WIZARD_SERVER_HOST}:${WIZARD_PROXY_PORT}"
  echo
  echo "BOT_TOKEN=$(mask_token "$WIZARD_BOT_TOKEN")"
  echo
  echo "Контейнеры:"
  echo "✅ mtproto-shop-proxy"
  echo "✅ mtproto-shop-bot"
  if [[ -n "$WIZARD_SUPPORT_BOT_TOKEN" ]]; then
    echo "✅ mtproto-shop-support-bot"
  else
    echo "✅ support bot отключён"
  fi
  echo
  echo "Теперь открой Telegram:"
  echo "1. Найди своего бота"
  echo "2. Нажми /start один раз, чтобы открыть постоянное клиентское меню"
  echo "3. Для админки отдельно напиши /admin"
  echo
  echo "Если бот не отвечает:"
  echo "mtp"
  echo "→ 23) 🧪 Проверка установки"
  echo "→ 4) 📄 Логи бота"
  echo
  echo "=================================================="
}

run_bot_cmd() {
  if $COMPOSE_CMD ps -q bot >/dev/null 2>&1 && [[ -n "$($COMPOSE_CMD ps -q bot 2>/dev/null || true)" ]]; then
    $COMPOSE_CMD exec -T bot python proxy_manager.py "$@"
  else
    $COMPOSE_CMD run --rm bot python proxy_manager.py "$@"
  fi
}

check_telemt_api() {
  echo
  echo -e "${BLUE}Проверяю TeleMT API из контейнера бота...${NC}"
  if $COMPOSE_CMD exec -T bot python -c \
    "import urllib.request; urllib.request.urlopen('http://mtproto:9091/v1/users', timeout=3).read()" \
    >/dev/null 2>&1; then
    echo -e "${GREEN}✅ TeleMT API доступен; изменения ключей применяются через API.${NC}"
  else
    echo -e "${RED}TeleMT API недоступен из контейнера бота.${NC}"
    return 1
  fi
}

require_telemt_compose_service() {
  if ! command -v docker >/dev/null 2>&1 || ! docker version >/dev/null 2>&1; then
    echo -e "${RED}Docker недоступен. Проверка TeleMT невозможна.${NC}"
    return 1
  fi
  if [[ ! -f docker-compose.yml ]]; then
    echo -e "${RED}Файл docker-compose.yml не найден.${NC}"
    return 1
  fi

  local services rc=0 errlog
  errlog="$(mktemp)"
  services="$($COMPOSE_CMD config --services 2>"$errlog")" || rc=$?

  if [[ "$rc" -ne 0 ]]; then
    echo -e "${RED}docker compose config завершился с ошибкой (rc=${rc}).${NC}"
    echo "Вывод stderr:"
    cat "$errlog" || true
    rm -f "$errlog"
    return 1
  fi
  rm -f "$errlog"

  if ! printf '%s\n' "$services" | grep -qx "mtproto"; then
    echo -e "${RED}Сервис mtproto не найден в docker compose.${NC}"
    echo "Доступные сервисы:"
    printf '%s\n' "$services"
    return 1
  fi

  if ! docker inspect mtproto-shop-proxy >/dev/null 2>&1; then
    echo -e "${RED}Контейнер mtproto-shop-proxy не найден.${NC}"
    return 1
  fi
}

show_telemt_version_info() {
  if ! TELEMT_CURRENT_VERSION="$(docker exec mtproto-shop-proxy /app/telemt --version 2>/dev/null)"; then
    echo -e "${RED}Не удалось получить версию работающего TeleMT командой /app/telemt --version.${NC}"
    return 1
  fi
  if ! TELEMT_CURRENT_IMAGE="$(docker inspect mtproto-shop-proxy --format '{{.Config.Image}}' 2>/dev/null)" \
    || ! TELEMT_OLD_IMAGE_ID="$(docker inspect mtproto-shop-proxy --format '{{.Image}}' 2>/dev/null)"; then
    echo -e "${RED}Не удалось получить Docker image или Image ID контейнера TeleMT.${NC}"
    return 1
  fi

  echo "Текущая версия TeleMT: ${TELEMT_CURRENT_VERSION}"
  echo "Текущий Docker image tag: ${TELEMT_CURRENT_IMAGE}"
  echo "Текущий Image ID контейнера: ${TELEMT_OLD_IMAGE_ID}"
}

pull_telemt_available_image() {
  local available_version

  echo
  echo "Проверяю доступный образ TeleMT без перезапуска контейнера..."
  if ! $COMPOSE_CMD pull mtproto; then
    echo -e "${RED}Не удалось скачать информацию об образе TeleMT.${NC}"
    return 1
  fi
  if ! TELEMT_NEW_IMAGE_ID="$(docker image inspect "$TELEMT_CURRENT_IMAGE" --format '{{.Id}}' 2>/dev/null)"; then
    echo -e "${RED}Не удалось определить Image ID доступного образа TeleMT.${NC}"
    return 1
  fi
  if available_version="$(docker run --rm "$TELEMT_CURRENT_IMAGE" /app/telemt --version 2>/dev/null)"; then
    TELEMT_NEW_VERSION="$available_version"
  else
    TELEMT_NEW_VERSION="не определена"
    echo "Версия нового образа командой не выводится. Сравнение выполнено по Docker Image ID."
  fi
  echo "Доступный Image ID: ${TELEMT_NEW_IMAGE_ID}"
}

check_telemt_version() {
  echo
  echo -e "${BLUE}Проверка версии TeleMT${NC}"
  require_telemt_compose_service || return 1
  show_telemt_version_info || return 1
  pull_telemt_available_image || return 1
  echo "Доступная версия TeleMT: ${TELEMT_NEW_VERSION}"

  if [[ "$TELEMT_OLD_IMAGE_ID" == "$TELEMT_NEW_IMAGE_ID" ]]; then
    echo -e "${GREEN}✅ TeleMT уже актуален. Обновление не требуется.${NC}"
  else
    echo -e "${YELLOW}⚠️ Доступно обновление.${NC}"
    echo "Чтобы применить обновление, выберите пункт 7) 🔄 Обновить TeleMT."
  fi
}

backup_telemt_update_config() {
  local archive
  local -a backup_items=(docker-compose.yml .env)

  if [[ -d telemt ]]; then
    backup_items+=(telemt)
  fi
  mkdir -p backups
  archive="backups/telemt-update-$(date +%Y%m%d-%H%M%S).tar.gz"
  if ! tar -czf "$archive" "${backup_items[@]}"; then
    echo -e "${RED}Не удалось создать backup конфигурации TeleMT. Обновление отменено.${NC}"
    return 1
  fi
  chmod 600 "$archive"
  TELEMT_UPDATE_BACKUP="$archive"
  echo -e "${GREEN}✅ Backup сохранён: ${archive}${NC}"
}

rollback_telemt_update() {
  local configured_image="$1"
  local target_image="$2"
  local previous_image_id="$3"
  local compose_changed="$4"

  echo -e "${YELLOW}Пробую вернуть предыдущую конфигурацию TeleMT...${NC}"
  if [[ "$compose_changed" == "true" ]]; then
    if ! tar -xzf "$TELEMT_UPDATE_BACKUP" docker-compose.yml; then
      echo -e "${RED}Не удалось восстановить docker-compose.yml из backup: ${TELEMT_UPDATE_BACKUP}${NC}"
      return 1
    fi
  elif [[ -n "$previous_image_id" ]]; then
    docker tag "$previous_image_id" "$configured_image" || true
  fi
  $COMPOSE_CMD up -d --force-recreate mtproto || true
  echo "Backup для ручного отката: ${TELEMT_UPDATE_BACKUP}"
}

update_telemt() {
  local answer
  TELEMT_UPDATE_BACKUP=""

  echo
  echo -e "${BLUE}Обновление TeleMT${NC}"
  require_telemt_compose_service || return 1
  [[ -f .env ]] || {
    echo -e "${RED}Файл .env не найден. Обновление отменено.${NC}"
    return 1
  }
  show_telemt_version_info || return 1
  pull_telemt_available_image || return 1
  echo "Новая версия TeleMT: ${TELEMT_NEW_VERSION}"

  if [[ "$TELEMT_OLD_IMAGE_ID" == "$TELEMT_NEW_IMAGE_ID" ]]; then
    echo -e "${GREEN}✅ TeleMT уже актуален. Обновление не требуется.${NC}"
    return 0
  fi

  echo -e "${YELLOW}⚠️ Найдена новая версия TeleMT.${NC}"
  read -r -p "Применить обновление? y/N " answer
  if [[ ! "$answer" =~ ^[Yy]$ ]]; then
    echo "Обновление TeleMT отменено."
    return 0
  fi

  backup_telemt_update_config || return 1
  echo -e "${BLUE}Перезапускаю только контейнер TeleMT...${NC}"
  if ! $COMPOSE_CMD up -d --no-deps mtproto; then
    echo -e "${RED}Обновление TeleMT не удалось при пересоздании контейнера.${NC}"
    echo "Backup для ручного восстановления: ${TELEMT_UPDATE_BACKUP}"
    return 1
  fi

  echo
  echo "Статус TeleMT:"
  $COMPOSE_CMD ps mtproto || true
  if TELEMT_CURRENT_VERSION="$(docker exec mtproto-shop-proxy /app/telemt --version 2>/dev/null)"; then
    echo "Текущая версия TeleMT после обновления: ${TELEMT_CURRENT_VERSION}"
  else
    echo -e "${YELLOW}Не удалось получить версию TeleMT после обновления.${NC}"
  fi
  echo "Последние строки логов TeleMT:"
  $COMPOSE_CMD logs --tail=50 mtproto || true
}

update_source_code() {
  local changes remaining_changes timestamp status_backup diff_backup manage_backup stash_name

  if [[ ! -d .git ]]; then
    echo -e "${YELLOW}Git-репозиторий не найден, пропускаю загрузку обновлений.${NC}"
    return 0
  fi

  echo -e "${BLUE}Загружаю обновления из GitHub...${NC}"
  changes="$(git status --short --untracked-files=all)"
  if [[ -n "$changes" ]]; then
    timestamp="$(date +%Y-%m-%d_%H-%M-%S)"
    mkdir -p backups
    status_backup="backups/git-status-before-update-${timestamp}.txt"
    diff_backup="backups/git-diff-before-update-${timestamp}.diff"
    printf '%s\n' "$changes" > "$status_backup"
    git diff --binary HEAD -- > "$diff_backup"

    echo -e "${YELLOW}⚠️ Найдены локальные изменения.${NC}"
    if [[ -n "$(git status --short -- manage.sh)" ]] && [[ -f manage.sh ]]; then
      manage_backup="backups/manage.sh-before-update-${timestamp}"
      cp -p manage.sh "$manage_backup"
    fi
    stash_name="Автобэкап перед обновлением MTProto-Shop ${timestamp}"
    if ! git stash push -u -m "$stash_name" -- . \
      ':(exclude).env' \
      ':(exclude)data' \
      ':(exclude)data/**' \
      ':(exclude)backups' \
      ':(exclude)backups/**' \
      ':(exclude)logs' \
      ':(exclude)logs/**' \
      ':(exclude)telemt/config.toml' \
      ':(exclude)telemt/*.tmp'; then
      echo -e "${RED}❌ Не удалось сохранить локальные изменения в git stash. Обновление отменено.${NC}"
      echo "Backup-файлы:"
      echo "  ${status_backup}"
      echo "  ${diff_backup}"
      [[ -z "${manage_backup:-}" ]] || echo "  ${manage_backup}"
      return 1
    fi
    echo -e "${GREEN}✅ Локальные изменения сохранены в backup и git stash.${NC}"
    echo "Backup-файлы:"
    echo "  ${status_backup}"
    echo "  ${diff_backup}"
    [[ -z "${manage_backup:-}" ]] || echo "  ${manage_backup}"

    remaining_changes="$(git status --short --untracked-files=no)"
    if [[ -n "$remaining_changes" ]]; then
      echo -e "${RED}❌ После сохранения остались защищённые локальные изменения. Автоматическое обновление отменено.${NC}"
      printf '%s\n' "$remaining_changes"
      echo "Проверьте сохранённые изменения командой: git stash list"
      return 1
    fi
  fi

  if ! git fetch origin main || ! git pull --ff-only origin main; then
    echo -e "${RED}❌ Не удалось обновить код из GitHub.${NC}"
    git status --short
    if [[ -n "${status_backup:-}" ]]; then
      echo "Backup-файлы:"
      echo "  ${status_backup}"
      echo "  ${diff_backup}"
      [[ -z "${manage_backup:-}" ]] || echo "  ${manage_backup}"
    else
      echo "Перед обновлением локальных изменений не было, backup не создавался."
    fi
    echo "Для ручной проверки сохранённых изменений выполните: git stash list"
    return 1
  fi

  if ! chmod +x manage.sh install.sh; then
    echo -e "${RED}❌ Код обновлён, но не удалось назначить права запуска manage.sh и install.sh.${NC}"
    return 1
  fi
  echo -e "${GREEN}✅ Код успешно обновлён из GitHub.${NC}"
}

install_or_update() {
  if ! update_source_code; then
    return 0
  fi

  need_env || {
    echo
    echo "После заполнения .env снова запустите: ./manage.sh"
    return 0
  }
  echo -e "${BLUE}Собираю и запускаю контейнеры...${NC}"
  $COMPOSE_CMD up -d --build --force-recreate
  $COMPOSE_CMD ps
}

show_status() {
  $COMPOSE_CMD ps
}

show_bot_logs() {
  echo "Выход из логов: Ctrl+C"
  $COMPOSE_CMD logs -f --tail=120 bot
}

show_proxy_logs() {
  echo "Выход из логов: Ctrl+C"
  $COMPOSE_CMD logs -f --tail=120 mtproto
}

show_support_bot_logs() {
  echo "Выход из логов: Ctrl+C"
  $COMPOSE_CMD logs -f --tail=120 support_bot
}

restart_bot() {
  echo
  echo -e "${BLUE}Пересоздаю Telegram-ботов...${NC}"
  $COMPOSE_CMD up -d --build --force-recreate bot support_bot
  echo -e "${GREEN}Готово: основной бот и бот поддержки пересозданы.${NC}"
  echo
  $COMPOSE_CMD ps bot support_bot
}

restart_proxy_container() {
  echo
  echo -e "${BLUE}Пересоздаю MTProto proxy...${NC}"
  $COMPOSE_CMD up -d --force-recreate mtproto
  echo -e "${GREEN}Готово: MTProto proxy пересоздан.${NC}"
  echo
  $COMPOSE_CMD ps mtproto
}

restart_all_services() {
  echo
  echo -e "${BLUE}Пересоздаю ботов и MTProto proxy...${NC}"
  $COMPOSE_CMD up -d --build --force-recreate bot support_bot mtproto
  echo -e "${GREEN}Готово: боты и MTProto proxy пересозданы.${NC}"
  echo
  $COMPOSE_CMD ps
}

reboot_vps() {
  echo -e "${RED}Внимание: VPS будет полностью перезагружен.${NC}"
  read -r -p "Точно перезагрузить сервер? Напишите REBOOT: " answer
  [[ "$answer" == "REBOOT" ]] || { echo "Отменено"; return 0; }
  echo -e "${YELLOW}Перезагружаю сервер... SSH-сессия сейчас отключится.${NC}"
  reboot
}

install_restart_watcher() {
  echo
  echo -e "${BLUE}Устанавливаю watcher автоматического рестарта...${NC}"
  if ! cp scripts/mtproto-restart-watcher.service /etc/systemd/system/; then
    echo -e "${RED}Не удалось скопировать systemd unit watcher.${NC}"
    return 1
  fi
  if ! chmod +x scripts/mtproto-restart-watcher.sh \
    || ! systemctl daemon-reload \
    || ! systemctl enable --now mtproto-restart-watcher.service; then
    echo -e "${RED}Не удалось установить или запустить watcher.${NC}"
    return 1
  fi
  echo -e "${GREEN}✅ Watcher установлен и запущен${NC}"
}

list_keys() {
  run_bot_cmd list
}

add_key() {
  echo "Для клиента Telegram лучше использовать формат: tg_TELEGRAM_ID"
  read -r -p "Введите client_id: " client_id
  [[ -n "$client_id" ]] || { echo "client_id пустой"; return 1; }
  run_bot_cmd create "$client_id"
  check_telemt_api
}

rotate_key_by_telegram_id() {
  read -r -p "Введите telegram_id клиента: " telegram_id
  [[ "$telegram_id" =~ ^[0-9]+$ ]] || { echo "telegram_id должен быть числом"; return 1; }
  run_bot_cmd rotate-telegram "$telegram_id"
  check_telemt_api
}

show_link_by_telegram_id() {
  read -r -p "Введите telegram_id клиента: " telegram_id
  [[ "$telegram_id" =~ ^[0-9]+$ ]] || { echo "telegram_id должен быть числом"; return 1; }
  run_bot_cmd link "tg_${telegram_id}"
}

show_link_by_client_id() {
  read -r -p "Введите client_id: " client_id
  [[ -n "$client_id" ]] || { echo "client_id пустой"; return 1; }
  run_bot_cmd link "$client_id"
}

delete_key() {
  read -r -p "Введите client_id для удаления: " client_id
  [[ -n "$client_id" ]] || { echo "client_id пустой"; return 1; }
  read -r -p "Точно удалить ключ ${client_id}? Напишите YES: " answer
  [[ "$answer" == "YES" ]] || { echo "Отменено"; return 0; }
  run_bot_cmd delete "$client_id"
  check_telemt_api
}

edit_env() {
  ${EDITOR:-nano} .env
}

backup_now() {
  mkdir -p backups
  archive="backups/mtproto-shop-backup-$(date +%Y%m%d-%H%M%S).tar.gz"
  tar -czf "$archive" data telemt .env docker-compose.yml 2>/dev/null || true
  echo "Бэкап создан: $archive"
}

check_installation() {
  local token admin_id server_host proxy_port tls_domain proxy_core support_token
  local telemt_api_url telemt_system_user
  local -a errors=()

  if ! command -v docker >/dev/null 2>&1; then
    errors+=("Docker не установлен.")
  elif ! docker version >/dev/null 2>&1; then
    errors+=("Docker daemon недоступен.")
  fi
  if ! docker compose version >/dev/null 2>&1; then
    errors+=("Команда docker compose не работает.")
  else
    COMPOSE_CMD="docker compose"
  fi
  if [[ ! -f .env ]]; then
    errors+=("Файл .env не найден. Запустите пункт 1.")
  else
    token="$(get_env_value BOT_TOKEN)"
    admin_id="$(get_env_value ADMIN_ID)"
    server_host="$(get_env_value SERVER_HOST)"
    proxy_port="$(get_env_value PROXY_PORT)"
    tls_domain="$(get_env_value TLS_DOMAIN)"
    proxy_core="$(get_env_value PROXY_CORE)"
    support_token="$(get_env_value SUPPORT_BOT_TOKEN)"
    telemt_api_url="$(get_env_value TELEMT_API_URL)"
    telemt_system_user="$(get_env_value TELEMT_SYSTEM_USER)"

    [[ -n "$token" ]] || errors+=("BOT_TOKEN не заполнен.")
    if [[ -n "$token" ]] && ! check_telegram_token "$token"; then
      errors+=("BOT_TOKEN не проходит Telegram API getMe.")
    fi
    [[ "$admin_id" =~ ^[0-9]+$ ]] || errors+=("ADMIN_ID отсутствует или не является числом.")
    [[ -n "$server_host" ]] || errors+=("SERVER_HOST не заполнен.")
    [[ "$proxy_port" =~ ^[0-9]+$ ]] || errors+=("PROXY_PORT отсутствует или указан неверно.")
    [[ -n "$tls_domain" ]] || errors+=("TLS_DOMAIN не заполнен.")
    [[ "$proxy_core" == "telemt" ]] || errors+=("PROXY_CORE должен быть telemt.")
    [[ "$telemt_api_url" == "http://mtproto:9091" ]] \
      || errors+=("TELEMT_API_URL должен быть http://mtproto:9091.")
    [[ "$telemt_system_user" == "shop_bootstrap" ]] \
      || errors+=("TELEMT_SYSTEM_USER должен быть shop_bootstrap.")
  fi

  [[ -f docker-compose.yml ]] || errors+=("Файл docker-compose.yml не найден.")
  [[ -f telemt/config.toml ]] || errors+=("Файл telemt/config.toml не найден. Запустите пункт 1.")
  if [[ -f docker-compose.yml ]] && docker compose version >/dev/null 2>&1 \
    && ! docker compose config >/dev/null 2>&1; then
    errors+=("docker compose config завершился с ошибкой.")
  fi

  if command -v docker >/dev/null 2>&1 && docker version >/dev/null 2>&1; then
    container_is_running "mtproto-shop-proxy" || errors+=("Контейнер mtproto-shop-proxy не запущен.")
    if ! container_is_running "mtproto-shop-bot"; then
      errors+=("Контейнер mtproto-shop-bot не запущен.")
      if docker compose version >/dev/null 2>&1 && [[ -f docker-compose.yml ]]; then
        echo -e "${YELLOW}Последние 50 строк логов bot:${NC}"
        docker compose logs --tail=50 bot || true
      fi
    elif ! container_is_healthy "mtproto-shop-bot"; then
      errors+=("Контейнер mtproto-shop-bot не имеет healthy-статуса.")
    fi
    if [[ -n "${support_token:-}" ]] && ! container_is_running "mtproto-shop-support-bot"; then
      errors+=("SUPPORT_BOT_TOKEN задан, но контейнер mtproto-shop-support-bot не запущен.")
    fi
    if container_is_running "mtproto-shop-bot" && ! $COMPOSE_CMD exec -T bot python -c \
      "import urllib.request; urllib.request.urlopen('http://mtproto:9091/v1/users', timeout=3).read()" \
      >/dev/null 2>&1; then
      errors+=("TeleMT API недоступен из контейнера mtproto-shop-bot.")
    fi
  fi
  if [[ "${proxy_port:-}" =~ ^[0-9]+$ ]] && ! is_port_listening "$proxy_port"; then
    errors+=("Порт proxy ${proxy_port} не слушается.")
  fi

  echo
  echo "Docker Compose:"
  if docker compose version >/dev/null 2>&1 && [[ -f docker-compose.yml ]]; then
    docker compose ps || true
  else
    echo "Статус недоступен."
  fi
  echo

  if (( ${#errors[@]} > 0 )); then
    echo -e "${RED}Проверка обнаружила ошибки:${NC}"
    printf '❌ %s\n' "${errors[@]}"
    echo
    echo "Исправьте указанные настройки или снова выполните пункт 1."
    return 1
  fi

  echo -e "${GREEN}✅ Проверка завершена успешно.${NC}"
  echo -e "${GREEN}✅ Telegram-бот запущен.${NC}"
  echo -e "${GREEN}✅ Откройте Telegram-бота и нажмите /start один раз.${NC}"
  echo -e "${GREEN}✅ После этого используйте постоянные кнопки меню.${NC}"
  echo -e "${GREEN}✅ Для админки напишите /admin.${NC}"
}

while true; do
  print_header
  echo "1) 🧙 Первичная установка с нуля"
  echo "2) 🚀 Установка / обновление / запуск"
  echo "3) ✅ Статус контейнеров"
  echo "4) 📄 Логи бота"
  echo "5) 📄 Логи TeleMT"
  echo "6) 🔍 Проверить версию TeleMT"
  echo "7) 🔄 Обновить TeleMT"
  echo "8) 📋 Список ключей"
  echo "9) ➕ Добавить ключ вручную"
  echo "10) 🔄 Обновить ключ клиента по Telegram ID + синхронизировать SQLite"
  echo "11) 🔗 Показать ссылку по Telegram ID"
  echo "12) 🔗 Показать ссылку по client_id"
  echo "13) ❌ Удалить ключ по client_id"
  echo "14) ♻️ Проверить TeleMT API / применить изменения"
  echo "15) ⚙️ Открыть .env"
  echo "16) 💾 Сделать бэкап"
  echo "17) 🔁 Пересоздать Telegram-ботов"
  echo "18) 🔁 Пересоздать MTProto proxy"
  echo "19) 🔁 Пересоздать ботов + MTProto proxy"
  echo "20) 🖥️ Перезагрузить VPS полностью"
  echo "21) 📄 Логи бота поддержки"
  echo "22) 🛡 Установить watcher автоматического рестарта"
  echo "23) 🧪 Проверка установки"
  echo "0) 🚪 Выход"
  echo
  read -r -p "Выберите действие: " choice
  echo
  case "$choice" in
    1) first_setup_wizard || true; pause ;;
    2) install_or_update || true; pause ;;
    3) show_status || true; pause ;;
    4) show_bot_logs || true ;;
    5) show_proxy_logs || true ;;
    6) check_telemt_version || true; pause ;;
    7) update_telemt || true; pause ;;
    8) list_keys || true; pause ;;
    9) add_key || true; pause ;;
    10) rotate_key_by_telegram_id || true; pause ;;
    11) show_link_by_telegram_id || true; pause ;;
    12) show_link_by_client_id || true; pause ;;
    13) delete_key || true; pause ;;
    14) check_telemt_api || true; pause ;;
    15) edit_env || true; pause ;;
    16) backup_now || true; pause ;;
    17) restart_bot || true; pause ;;
    18) restart_proxy_container || true; pause ;;
    19) restart_all_services || true; pause ;;
    20) reboot_vps ;;
    21) show_support_bot_logs || true ;;
    22) install_restart_watcher || true; pause ;;
    23) check_installation || true; pause ;;
    0) exit 0 ;;
    *) echo "Неверный пункт"; pause ;;
  esac
done
