#!/usr/bin/env bash
# Компонент: интеграция с системой.
# Ставит systemd-сервисы, включает автозапуск, поднимает бота.
#
#   bash components/service.sh              user-сервис для текущей папки
#   bash components/service.sh --template   ещё и шаблон ttplugin@.service
#                                           (нужен sudo: /etc/systemd/system)
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/common.sh
. "$HERE/../lib/common.sh"

TEMPLATE=0
[ "${1:-}" = "--template" ] && TEMPLATE=1

require_non_root
ensure_repo || exit 1

UNIT_SRC="$REPO_DIR/tt-plugin.service"
TTS_SRC="$REPO_DIR/tt-plugin-tts.service"
TPL_SRC="$REPO_DIR/ttplugin@.service.in"

say ""
say "== Интеграция с системой =="
say "  Папка: $REPO_DIR"

if [ -d "/run/systemd/system" ] && [ -f "$UNIT_SRC" ]; then
    mkdir -p "$UNIT_DIR"
    if [ "$DRY_RUN" = "1" ]; then
        say "    [проверка] $UNIT_SRC -> $UNIT_DIR/$UNIT_NAME"
    else
        sed -e "s|__DIR__|$REPO_DIR|g" "$UNIT_SRC" > "$UNIT_DIR/$UNIT_NAME"
        say "  Сервис: $UNIT_DIR/$UNIT_NAME"
    fi
    run systemctl --user daemon-reload
    run systemctl --user enable "$UNIT_NAME"
    say "  Запускаю бота..."
    run systemctl --user start "$UNIT_NAME"

    # Воркер синтеза речи: отдельный процесс, потому что torch в одном
    # процессе с TeamTalk SDK падает в segfault (проверено на практике).
    if [ -f "$TTS_SRC" ] && [ -x "$REPO_DIR/.venv/bin/python" ] \
       && "$REPO_DIR/.venv/bin/python" -c "import torch" >/dev/null 2>&1; then
        if [ "$DRY_RUN" = "1" ]; then
            say "    [проверка] $TTS_SRC -> $UNIT_DIR/tt-plugin-tts.service"
        else
            sed -e "s|__DIR__|$REPO_DIR|g" "$TTS_SRC" > "$UNIT_DIR/tt-plugin-tts.service"
        fi
        run systemctl --user daemon-reload
        run systemctl --user enable --now tt-plugin-tts.service
        say "  Голосовое приветствие: воркер синтеза (tt-plugin-tts) запущен"
    fi

    if command -v loginctl >/dev/null 2>&1; then
        run sudo loginctl enable-linger "$(id -un)" \
            || say "  (linger не включился — после перезагрузки сервис поднимется только после входа)"
    fi

    say ""
    say "  Статус: systemctl --user status $UNIT_NAME"
    say "  Логи:   journalctl --user -u $UNIT_NAME -f"
else
    say "  systemd не найден — автозапуск пропущен."
    local_start="n"
    ask_yn local_start "  Запустить бота сейчас в фоне?" y
    if [ "$local_start" = "y" ]; then
        run bash -c "cd \"$REPO_DIR\" && nohup bash run.sh > bot.log 2>&1 &"
        say "  Бот запущен в фоне. Лог: $REPO_DIR/bot.log"
    else
        say "  Ручной запуск: bash $REPO_DIR/run.sh"
    fi
fi

# ---- шаблон ttplugin@имя (для нескольких серверов на одной машине) ----
if [ "$TEMPLATE" = 1 ]; then
    say ""
    say "== Шаблон ttplugin@имя =="
    if [ ! -f "$TPL_SRC" ]; then
        say "  !! нет $TPL_SRC — шаблон пропущен."
    else
        TPL_DST="/etc/systemd/system/ttplugin@.service"
        say "  Куда: $TPL_DST (нужен sudo)"
        TPL_USER="$(id -un)"
        TPL_HOME="$HOME"
        TPL_RUNTIME="/run/user/$(id -u)"
        sed -e "s|__DIR__|$REPO_DIR|g" \
            -e "s|__INSTANCES__|$INSTANCES_ROOT|g" \
            -e "s|__USER__|$TPL_USER|g" \
            -e "s|__HOME__|$TPL_HOME|g" \
            -e "s|__RUNTIME__|$TPL_RUNTIME|g" "$TPL_SRC" \
            | run sudo tee "$TPL_DST" >/dev/null
        run sudo mkdir -p "/etc/systemd/system/ttplugin@.service.d"
        run sudo systemctl daemon-reload
        say "  Шаблон установлен."
        say "  Запуск инстанса: sudo systemctl start ttplugin@имя"
        say "  Дефис в имени — это вложенный путь: ttplugin@kirill-root = $INSTANCES_ROOT/kirill/root/app"
    fi
fi

say ""
say "== Система настроена =="
say "  $CREATOR_LINE"
