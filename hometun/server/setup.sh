#!/usr/bin/env bash
# ============================================================
#  Домашний туннель для universal plugin — установка на сервере
#
#  Создаёт на сервере бота туннельного пользователя, который
#  принимает reverse-туннель от домашнего компьютера владельца.
#  Через этот туннель бот выходит в интернет домашним IP,
#  где YouTube не блокирован.
#
#  Запуск (от root или через sudo):
#    sudo bash setup.sh [--user hometun] [--remote-port 1080]
#
#  --user         логин туннельного пользователя (по умолчанию hometun)
#  --remote-port  порт на сервере, который слушает туннель
#                 (по умолчанию 1080; должен совпадать с remote_port
#                 в настройках клиента и с YT_TUNNEL в конфигурации бота)
#
#  Скрипт печатает:
#    - код безопасности сервера (host_key) — вписать в настройки клиента;
#    - приватный ключ — сохранить и сконвертировать в .ppk для Windows;
#    - строку для authorized_keys.
# ============================================================
set -euo pipefail

USER_NAME="hometun"
REMOTE_PORT="1080"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --user) USER_NAME="$2"; shift 2 ;;
    --remote-port) REMOTE_PORT="$2"; shift 2 ;;
    *) echo "Неизвестный аргумент: $1" >&2; exit 1 ;;
  esac
done

if [[ $EUID -ne 0 ]]; then
  echo "Запусти от root: sudo bash setup.sh" >&2
  exit 1
fi

if ! [[ "$REMOTE_PORT" =~ ^[0-9]+$ ]]; then
  echo "Ошибка: --remote-port должен быть числом" >&2
  exit 1
fi

echo "==> Туннельный пользователь: $USER_NAME"
echo "==> Порт на сервере: $REMOTE_PORT"
echo ""

# ---- 1. Пользователь ----
if ! id "$USER_NAME" &>/dev/null; then
  useradd --system --shell /usr/sbin/nologin \
    --home "/home/$USER_NAME" --create-home "$USER_NAME" 2>/dev/null \
    || useradd --system --shell /usr/sbin/nologin -d "/home/$USER_NAME" "$USER_NAME"
  mkdir -p "/home/$USER_NAME"
  chown "$USER_NAME:$USER_NAME" "/home/$USER_NAME"
  echo "Создан пользователь $USER_NAME"
else
  echo "Пользователь $USER_NAME уже существует"
fi

SSHDIR="/home/$USER_NAME/.ssh"
mkdir -p "$SSHDIR"
chmod 700 "$SSHDIR"

# ---- 2. Ключ ----
KEYFILE="$SSHDIR/tun_key"
if [[ ! -f "$KEYFILE" ]]; then
  ssh-keygen -q -t ed25519 -f "$KEYFILE" -N '' -C "$USER_NAME-reverse"
  echo "Сгенерирован ключ: $KEYFILE"
else
  echo "Ключ уже существует: $KEYFILE"
fi
chmod 600 "$KEYFILE"

# ---- 3. authorized_keys ----
PUB="$(cat "$KEYFILE.pub")"
AUTH="$SSHDIR/authorized_keys"
if ! grep -qF "$USER_NAME-reverse" "$AUTH" 2>/dev/null; then
  echo "restrict,port-forwarding,permitlisten=\"127.0.0.1:${REMOTE_PORT}\" $PUB $USER_NAME-reverse" >> "$AUTH"
  echo "Запись добавлена в authorized_keys"
else
  echo "Запись в authorized_keys уже есть"
fi
chmod 600 "$AUTH"
chown -R "$USER_NAME:$USER_NAME" "/home/$USER_NAME"

# ---- 4. Код безопасности сервера (host_key для клиента) ----
# Fingerprint ed25519-ключа самого сервера — его указывает клиент в host_key.
HOSTKEY="$(ssh-keygen -lf <(ssh-keyscan -t ed25519 localhost 2>/dev/null) 2>/dev/null | awk '{print $2}')"
if [[ -z "$HOSTKEY" ]]; then
  HOSTKEY="(не удалось вычислить автоматически — см. /etc/ssh/ssh_host_ed25519_key.pub)"
fi

echo ""
echo "============================================================"
echo "ГОТОВО. Ниже данные для настройки клиента на домашнем ПК."
echo "============================================================"
echo ""
echo "Адрес сервера:    (домен или IP сервера бота)"
echo "SSH-порт:         22"
echo "Логин:            $USER_NAME"
echo "Код безопасности: $HOSTKEY"
echo "Порт на сервере:  $REMOTE_PORT"
echo ""
echo "Приватный ключ (сохрани его в надёжное место):"
echo "------------------------------------------------------------"
cat "$KEYFILE"
echo "------------------------------------------------------------"
echo ""
echo "На Windows приватный ключ нужно сконвертировать в формат .ppk"
echo "(PuTTY). Проще всего программой puttygen:"
echo "  puttygen tun_key -o hometun.ppk"
echo "Сконвертированный файл положи в папку клиента и укажи его имя"
echo "в настройках (key_file)."
echo ""
echo "Чтобы бот universal plugin сам переключался на этот туннель, задай"
echo "в окружении бота переменную (например, в его .env / секретах):"
echo ""
echo "  YT_TUNNEL=socks5://127.0.0.1:${REMOTE_PORT}"
echo ""
echo "Бот проверяет туннель перед каждым скачиванием YouTube: поднят —"
echo "качает через домашний интернет, закрыт — обычным каналом сервера"
echo "(напрямую, или через YT_PROXY, если ты задал резервный прокси)."
echo "Перезапусти бота после добавления переменной."
echo "============================================================"
