#!/usr/bin/env bash
# TeamTalk Universal Plugin — установщик, собранный из отдельных компонентов.
#
#   bash install.sh              меню
#   bash install.sh deps         только зависимости (apt, venv, Deno, SDK)
#   bash install.sh config       только конфигуратор (.secrets/.env)
#   bash install.sh service      только интеграция с systemd и запуск
#   bash install.sh service --template   ещё и шаблон ttplugin@имя
#   bash install.sh all          всё подряд
#   bash install.sh uninstall    удаление
#
# Автор: Kirill (@zolo-kirill) в Telegram.
# Тестовый режим (без реальных действий): SETUP_DRY_RUN=1 bash install.sh
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
. "$HERE/lib/common.sh"

usage() {
    cat <<'EOF'
Установка TeamTalk Universal Plugin по компонентам.

  all                    всё подряд: зависимости, конфигуратор, сервис
  deps                   зависимости: apt, .venv, Deno, Silero TTS, TeamTalk SDK
  deps --check           только проверить, что уже стоит
  config                 конфигуратор: вопросы и .secrets/.env
  service                systemd-сервис, автозапуск, запуск бота
  service --template     плюс шаблон ttplugin@имя (нужен sudo)
  uninstall              остановить и удалить бота
  menu                   интерактивное меню (по умолчанию)

Переменные: SETUP_DRY_RUN=1 — проверка без действий,
            TT_INSTANCES_ROOT — где живут инстансы (по умолчанию /home/bot/ttplugin).
EOF
}

welcome() {
    say "===================================================="
    say "  TeamTalk Universal Plugin — установка"
    say "===================================================="
    say ""
    say "  Добро пожаловать в установку TeamTalk Universal Plugin!"
    say ""
    say "  Это музыкальный бот для голосовых каналов TeamTalk 5."
    say "  Что он умеет:"
    say "    • ищет и играет музыку по запросу — YouTube и Яндекс.Музыка;"
    say "    • ставит в очередь плейлисты, радио и избранное;"
    say "    • переключает треки, перематывает, меняет громкость;"
    say "    • проигрывает в канале аудио и команды из Telegram-реле;"
    say "    • сообщает в Telegram о входе и выходе пользователей."
    say ""
    say "  $CREATOR_LINE"
    say ""
    if [ "$DRY_RUN" = "1" ]; then
        say "  (режим проверки SETUP_DRY_RUN=1 — реальных действий не выполняется)"
        say ""
    fi
}

component() {
    local name="$1"; shift
    local path="$HERE/components/$name.sh"
    [ -f "$path" ] || { say "!! нет компонента: $path"; return 1; }
    bash "$path" "$@"
}

cmd_all() {
    local tpl="${1:-}"
    component deps   || return 1
    component config || return 1
    component service "$tpl" || return 1
    say ""
    say "== Готово =="
    say "  Запуск вручную:  bash $REPO_DIR/run.sh --config /путь/к/config.json"
    say "  Проверить пути:  bash $REPO_DIR/run.sh --show"
}

menu() {
    while :; do
        say ""
        say "Что нужно сделать?"
        say "  1) Установить всё (зависимости, конфигурация, сервис)"
        say "  2) Только зависимости"
        say "  3) Только конфигуратор"
        say "  4) Только сервис и запуск"
        say "  5) Шаблон ttplugin@имя для нескольких серверов"
        say "  6) Удалить бота"
        say "  7) Выход"
        local choice=""
        read -r -p "Выберите (1-7): " choice || { say ""; exit 0; }
        case "$choice" in
            1) cmd_all ;;
            2) component deps ;;
            3) component config ;;
            4) component service ;;
            5) component service --template ;;
            6) component uninstall ;;
            7) say "До свидания!"; exit 0 ;;
            *) say "  Такого пункта нет — выберите 1-7." ;;
        esac
    done
}

CMD="${1:-menu}"
shift || true

case "$CMD" in
    menu)      welcome; menu ;;
    all|install) welcome; cmd_all "$@" ;;
    deps)      welcome; component deps "$@" ;;
    config)    welcome; component config "$@" ;;
    service)   welcome; component service "$@" ;;
    uninstall) welcome; component uninstall "$@" ;;
    -h|--help|help) usage ;;
    *)
        say "Неизвестная команда: $CMD"
        say ""
        usage
        exit 2
        ;;
esac
