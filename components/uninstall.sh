#!/usr/bin/env bash
# Компонент: удаление.
# Останавливает и снимает systemd-сервисы, по желанию удаляет папку бота
# и каталог с секретами.
#
#   bash components/uninstall.sh
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/common.sh
. "$HERE/../lib/common.sh"

require_non_root

say ""
say "== Удаление TeamTalk Universal Plugin =="

REPO=""
if has_bot; then
    REPO="$REPO_DIR"
fi
UNIT="$UNIT_DIR/$UNIT_NAME"

if [ -z "$REPO" ] && [ ! -f "$UNIT" ]; then
    say "  Не удалось найти установленного бота: нет ни сервиса $UNIT, ни bot.py рядом."
    say "  Удалять нечего."
    exit 0
fi

# ---- сервисы ----
if [ -f "$UNIT" ]; then
    if [ -d "/run/systemd/system" ]; then
        say "  Останавливаю сервис..."
        run systemctl --user stop "$UNIT_NAME" || true
        run systemctl --user disable "$UNIT_NAME" || true
        if [ -f "$UNIT_DIR/tt-plugin-tts.service" ]; then
            run systemctl --user disable --now tt-plugin-tts.service || true
            run rm -f "$UNIT_DIR/tt-plugin-tts.service"
            say "  Воркер синтеза речи остановлен."
        fi
    fi
    run rm -f "$UNIT"
    if [ -d "/run/systemd/system" ]; then
        run systemctl --user daemon-reload
    fi
    say "  Сервис остановлен и удалён."
else
    say "  systemd-сервис не установлен (пропускаю)."
fi

# ---- папка бота ----
if [ -n "$REPO" ]; then
    say ""
    say "  Папка бота: $REPO"
    DEL_REPO="n"
    ask_yn DEL_REPO "  Удалить её целиком, вместе с .venv и SDK?" n
    if [ "$DEL_REPO" = "y" ]; then
        run rm -rf "$REPO"
        say "  Папка удалена."
    else
        say "  Папка сохранена."
    fi
fi

# ---- секреты ----
if [ -d "$SECRETS_DIR" ]; then
    say ""
    say "  Секреты: $SECRETS_DIR"
    DEL_SECRETS="n"
    ask_yn DEL_SECRETS "  Удалить папку с секретами (пароль, токены)?" n
    if [ "$DEL_SECRETS" = "y" ]; then
        run rm -rf "$SECRETS_DIR"
        say "  Секреты удалены."
    else
        say "  Секреты сохранены."
    fi
fi

say ""
say "== Бот удалён. До свидания! =="
say "  $CREATOR_LINE"
