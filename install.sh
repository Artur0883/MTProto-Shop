#!/usr/bin/env bash
set -Eeuo pipefail

INSTALL_DIR="/opt/mtproto-shop"
REPOSITORY_URL="${MTP_REPOSITORY_URL:-https://github.com/Artur0883/MTProto-Shop.git}"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m'

require_root() {
  if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    echo -e "${RED}Установщик нужно запускать от root: sudo bash ...${NC}"
    exit 1
  fi
}

require_supported_os() {
  if [[ ! -r /etc/os-release ]]; then
    echo -e "${RED}Не удалось определить операционную систему.${NC}"
    exit 1
  fi
  # shellcheck disable=SC1091
  . /etc/os-release
  case "${ID:-}" in
    ubuntu|debian) ;;
    *)
      echo -e "${RED}Поддерживается только Ubuntu или Debian. Найдено: ${PRETTY_NAME:-unknown}.${NC}"
      exit 1
      ;;
  esac
}

install_dependencies() {
  echo -e "${BLUE}Устанавливаю системные зависимости...${NC}"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y curl git nano unzip jq ufw ca-certificates openssl
}

ensure_docker() {
  if ! command -v docker >/dev/null 2>&1; then
    echo -e "${BLUE}Устанавливаю Docker...${NC}"
    export DEBIAN_FRONTEND=noninteractive
    apt-get install -y docker.io
  fi
  systemctl enable --now docker >/dev/null 2>&1 || service docker start >/dev/null 2>&1 || true
  if ! docker version >/dev/null 2>&1; then
    echo -e "${RED}Docker daemon недоступен. Проверьте systemctl status docker.${NC}"
    exit 1
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
    echo -e "${RED}Не удалось запустить 'docker compose'. Установите Docker Compose plugin и повторите установку.${NC}"
    exit 1
  fi
}

download_project() {
  mkdir -p /opt
  if [[ -d "$INSTALL_DIR/.git" ]]; then
    echo -e "${BLUE}Обновляю существующий проект в ${INSTALL_DIR}...${NC}"
    if ! git -C "$INSTALL_DIR" pull --ff-only origin main; then
      echo -e "${RED}git pull --ff-only не выполнен. Проверьте локальные изменения в ${INSTALL_DIR}.${NC}"
      exit 1
    fi
    return
  fi

  if [[ -e "$INSTALL_DIR" ]] && [[ -n "$(find "$INSTALL_DIR" -mindepth 1 -print -quit 2>/dev/null)" ]]; then
    echo -e "${RED}Каталог ${INSTALL_DIR} уже существует и не является git-репозиторием.${NC}"
    echo "Переместите его содержимое или укажите корректный git checkout, затем повторите установку."
    exit 1
  fi

  echo -e "${BLUE}Скачиваю проект в ${INSTALL_DIR}...${NC}"
  if ! git clone --branch main "$REPOSITORY_URL" "$INSTALL_DIR"; then
    echo -e "${RED}Не удалось скачать репозиторий: ${REPOSITORY_URL}${NC}"
    echo "Если форк опубликован под другим владельцем, передайте URL через MTP_REPOSITORY_URL."
    exit 1
  fi
}

install_mtp_command() {
  cat > /usr/local/bin/mtp <<'EOF'
#!/usr/bin/env bash
cd /opt/mtproto-shop || exit 1
chmod +x manage.sh
./manage.sh "$@"
EOF
  chmod +x "$INSTALL_DIR/manage.sh" "$INSTALL_DIR/install.sh" /usr/local/bin/mtp
}

configure_firewall() {
  echo -e "${BLUE}Настраиваю firewall...${NC}"
  ufw allow OpenSSH >/dev/null 2>&1 || ufw allow 22/tcp >/dev/null
  ufw allow 80/tcp >/dev/null
  ufw allow 443/tcp >/dev/null
  ufw --force enable >/dev/null
  echo "Открыты OpenSSH, 80/tcp и 443/tcp. Нестандартный proxy-порт будет открыт мастером."
}

main() {
  require_root
  require_supported_os
  install_dependencies
  ensure_docker
  download_project
  install_mtp_command
  configure_firewall

  echo
  echo -e "${GREEN}Проект установлен в ${INSTALL_DIR}. Команда управления: mtp${NC}"
  echo -e "${YELLOW}Выберите пункт 1 в открывшемся меню, чтобы завершить первичную настройку и запустить бота.${NC}"
  echo
  cd "$INSTALL_DIR"
  exec ./manage.sh
}

main "$@"
