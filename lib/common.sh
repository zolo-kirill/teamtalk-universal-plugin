#!/usr/bin/env bash
# Интерактивный установщик TeamTalk Universal Plugin.
# Меню: установка / удаление / выход. Запрашивает все параметры,
# клонирует репозиторий, ставит зависимости (apt, venv, SDK),
# при необходимости поднимает обход блокировки Telegram из VPN-ключа,
# создаёт .secrets/.env, настраивает systemd-сервис и запускает бота.
#
# Подключается остальными скриптами: source lib/common.sh
# Тестовый режим (без реальных действий): SETUP_DRY_RUN=1 bash install.sh
set -euo pipefail

CREATOR_LINE="Проект: TeamTalk Universal Plugin"
REPO_URL_DEFAULT="${REPO_URL_DEFAULT:-}"
UNIT_NAME="tt-plugin.service"
UNIT_DIR="$HOME/.config/systemd/user"
XRAY_CONF="${XRAY_CONF:-/usr/local/etc/xray/config.json}"
DRY_RUN="${SETUP_DRY_RUN:-0}"

say() { printf '%s\n' "$*"; }

run() {
    if [ "$DRY_RUN" = "1" ]; then
        say "    [проверка] $*"
        return 0
    fi
    "$@"
}

# ask VARNAME "Текст вопроса" [значение по умолчанию] — читает строку в переменную.
ask() {
    local var="$1" prompt="$2" default="${3:-}" val
    if [ -n "$default" ]; then
        read -r -p "$prompt [$default]: " val || { say ""; exit 130; }
        [ -z "$val" ] && val="$default"
    else
        read -r -p "$prompt: " val || { say ""; exit 130; }
    fi
    printf -v "$var" '%s' "$val"
}

ask_secret() {
    local var="$1" prompt="$2" val
    read -r -s -p "$prompt: " val || { say ""; exit 130; }
    say ""
    printf -v "$var" '%s' "$val"
}

ask_yn() {
    # ask_yn VARNAME "Текст вопроса" [по умолчанию y|n]
    local var="$1" prompt="$2" default="${3:-n}" ans
    while :; do
        read -r -p "$prompt (y/n) [$default]: " ans || { say ""; exit 130; }
        [ -z "$ans" ] && ans="$default"
        case "$ans" in
            y|Y|yes) printf -v "$var" 'y'; return 0 ;;
            n|N|no)  printf -v "$var" 'n'; return 0 ;;
            *) say "   Ответьте y или n." ;;
        esac
    done
}

# q ЗНАЧЕНИЕ — безопасные одинарные кавычки для shell: '$', '`', '\', пробелы.
q() {
    local v="$1" out="" c
    while [ -n "$v" ]; do
        c="${v:0:1}"
        if [ "$c" = "'" ]; then
            out="${out}'\\''"
        else
            out="${out}${c}"
        fi
        v="${v:1}"
    done
    printf "'%s'" "$out"
}

# Каталог, где лежит lib/, и корень репозитория (на уровень выше).
COMMON_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_DIR="$(cd "$COMMON_DIR/.." && pwd)"
REPO_DIR="${REPO_DIR:-$PLUGIN_DIR}"
SECRETS_DIR="${SECRETS_DIR:-$(cd "$PLUGIN_DIR/.." && pwd)/.secrets}"

# Куда ставится бот и где живут инстансы (ttplugin@имя).
INSTANCES_ROOT="${TT_INSTANCES_ROOT:-/home/bot/ttplugin}"

# Проверка: установщик не должен работать от root — сервис пользовательский.
require_non_root() {
    if [ "$(id -u)" -eq 0 ]; then
        say "!! Вы запустили установщик от root. Он настраивает пользовательский сервис."
        say "   Выйдите из root и запустите как обычный пользователь:"
        say "   sudo bash install.sh"
        exit 1
    fi
}

# Есть ли рядом с установщиком готовый бот.
has_bot() { [ -f "$REPO_DIR/bot.py" ]; }

# Клонировать репозиторий, если рядом с установщиком бота нет.
ensure_repo() {
    has_bot && return 0
    local git_url="" target=""
    say "  Рядом с установщиком нет bot.py — клонирую репозиторий."
    ask git_url "Адрес репозитория" "$REPO_URL_DEFAULT"
    if [ -z "$git_url" ]; then
        say "  Адрес не указан. Разместите bot.py и install.sh в одном каталоге и повторите."
        return 1
    fi
    ask target "Куда клонировать?" "$HOME/teamtalk-universal-plugin"
    target="${target/#\~/$HOME}"
    if [ -e "$target" ]; then
        if [ -f "$target/bot.py" ]; then
            say "  В $target уже есть бот — использую как есть."
        else
            local replace="n"
            ask_yn replace "Папка $target существует, но бота в ней нет. Очистить и клонировать заново?" n
            if [ "$replace" != "y" ]; then
                say "  Отменено."
                return 1
            fi
            run rm -rf "$target"
            run git clone "$git_url" "$target"
        fi
    else
        run git clone "$git_url" "$target"
    fi
    REPO_DIR="$(cd "$target" && pwd)"
    SECRETS_DIR="$(cd "$REPO_DIR/.." && pwd)/.secrets"
}
