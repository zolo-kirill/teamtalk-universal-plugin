#!/usr/bin/env bash
# Auto-installer for the TeamTalk Music Bot (Ubuntu/Debian).
# Usage: bash install.sh
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

echo "== TeamTalk Music Bot — установка =="

# Бот ставится под обычным пользователем (не root). Установщик использует
# sudo только для системных пакетов; сам сервис — пользовательский.
if [ "$(id -u)" -eq 0 ]; then
    echo "!! Запущен под root. Установщик ставит пользовательский systemd-сервис."
    echo "   Выйди из root и запусти от обычного пользователя с sudo:"
    echo "   sudo bash install.sh"
    exit 1
fi

SUDO=""
if command -v sudo >/dev/null 2>&1; then
    SUDO="sudo"
else
    echo "Нужен sudo. Запусти: sudo bash install.sh"
    exit 1
fi

# --- 1. Системные пакеты ---
echo
echo "== 1/5 Системные пакеты: ffmpeg, python3, pip, venv =="
$SUDO apt-get update -y
$SUDO apt-get install -y ffmpeg python3 python3-pip python3-venv

# --- 2. Python-окружение (venv) ---
echo
echo "== 2/5 Python-окружение (.venv): yt-dlp, yandex-music =="
if [ ! -d ".venv" ]; then
    python3 -m venv .venv
fi
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install --upgrade yt-dlp yandex-music

# --- 3. TeamTalk SDK ---
echo
echo "== 3/5 Проверка TeamTalk SDK =="
SDK_DIR="$(ls -d sdk/tt5sdk* 2>/dev/null | head -1 || true)"
SDK_LIB=""
if [ -n "$SDK_DIR" ]; then
    SDK_LIB="$SDK_DIR/Library/TeamTalk_DLL/libTeamTalk5.so"
fi
if [ -z "$SDK_DIR" ] || [ ! -f "$SDK_LIB" ]; then
    echo "!! TeamTalk SDK не найден."
    echo "   SDK входит в репозиторий (папка sdk/). Убедись, что клонировал его полностью:"
    echo "   git clone https://github.com/zolo-kirill/teamtalk-music-bot.git"
    exit 1
fi
echo "   SDK найден: $SDK_DIR"

# --- 4. Конфигурация (.secrets/.env) ---
echo
echo "== 4/5 Конфигурация .secrets/.env =="
SECRETS="$DIR/../.secrets"
mkdir -p "$SECRETS"
ENV="$SECRETS/.env"
if [ -f "$ENV" ]; then
    echo "   $ENV уже существует — оставляю как есть."
else
    cat > "$ENV" <<'EOT'
# ---- TeamTalk (обязательно) ----
TEAMTALK_HOST=example.com
TEAMTALK_TCP_PORT=10333
TEAMTALK_UDP_PORT=10333
TEAMTALK_USERNAME=example
TEAMTALK_PASSWORD=example
TEAMTALK_NICKNAME=MusicBot
# TEAMTALK_CHANNEL=/root/music

# ---- Яндекс.Музыка (опционально) ----
# Положи OAuth-токен в ../.secrets/ym_token.txt (или раскомментируй):
# TEAMTALK_YM_TOKEN=...

# ---- Телеграм-реле (опционально) ----
# Токен твоего телеграм-бота от @BotFather:
# TG_TOKEN=1234567890:AA...

# ---- Cookies для YouTube (опционально) ----
# TEAMTALK_COOKIES=/путь/к/cookies.txt
EOT
    echo "   Создан $ENV. Отредактируй его: поставь TEAMTALK_PASSWORD и, при желании, токены."
fi

# --- 5. Пользовательский systemd-сервис ---
echo
echo "== 5/5 Пользовательский systemd-сервис (автозапуск) =="
UNIT_SRC="teamtalk-music-bot.service"
if [ ! -f "$UNIT_SRC" ]; then
    echo "!! Не найден $UNIT_SRC — пропускаю systemd. Запуск вручную: bash run.sh"
else
    UNIT_DIR="$HOME/.config/systemd/user"
    mkdir -p "$UNIT_DIR"
    sed -e "s|__DIR__|$DIR|g" "$UNIT_SRC" > "$UNIT_DIR/$UNIT_SRC"
    systemctl --user daemon-reload
    systemctl --user enable teamtalk-music-bot.service
    echo "   Запускаю сервис..."
    systemctl --user start teamtalk-music-bot.service

    # Автозапуск после перезагрузки даже без входа в систему (linger)
    if command -v loginctl >/dev/null 2>&1; then
        $SUDO loginctl enable-linger "$(id -un)" 2>/dev/null || echo "   (не смог включить linger — после перезагрузки сервис стартует только после входа)"
    fi
fi

echo
echo "== Готово =="
echo "   Статус:   systemctl --user status teamtalk-music-bot"
echo "   Логи:     journalctl --user -u teamtalk-music-bot -f"
echo "   Ручной запуск (тест): bash run.sh"
