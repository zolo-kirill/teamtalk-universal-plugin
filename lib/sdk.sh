#!/usr/bin/env bash
# Подбор TeamTalk SDK: свежий с bearware.dk, но только тот, что грузится.
# Подключается после lib/common.sh.
# Скрипт старается взять самый свежий SDK с bearware.dk, но только тот, который
# реально грузится на этой системе: свежие сборки собираются под новый glibc и
# на старых Ubuntu падают. Если скачать нечего или сборка не грузится —
# остаётся SDK из репозитория, он рабочий.

# sdk_host_glibc — версия glibc хоста, например 2.39
sdk_host_glibc() {
    ldd --version 2>/dev/null | head -1 | grep -oE '[0-9]+\.[0-9]+' | head -1
}

# версия a не больше b
sdk_ver_le() {
    [ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | head -1)" = "$1" ]
}

# sdk_ubuntu_glibc 26 → 2.43 — минимум glibc для сборки под эту Ubuntu
sdk_ubuntu_glibc() {
    case "$1" in
        18) printf '2.27' ;;
        20) printf '2.31' ;;
        22) printf '2.35' ;;
        24) printf '2.39' ;;
        25) printf '2.41' ;;
        26) printf '2.43' ;;
        *)  printf '0' ;;
    esac
}

# sdk_candidates — печатает "ubuntu url" подходящих сборок: свежие версии первыми
sdk_candidates() {
    curl -fsSL -m 25 "https://bearware.dk/?page_id=419" 2>/dev/null \
      | grep -oE 'https?://[^"[:space:]]*tt5sdk_v[0-9][^"[:space:]]*_ubuntu[0-9]+_x86_64\.7z' \
      | sed -e 's#^http://#https://#' -e 's#^https://www\.#https://#' \
      | sed -E 's#^(https://[^ ]*/tt5sdk_v([0-9]+(\.[0-9]+)*[a-z]?)_ubuntu([0-9]+)_x86_64\.7z)$#\4 \1#' \
      | sort -u \
      | sort -k1,1nr
}

# sdk_test_load DIR PYTHON — грузится ли TeamTalk5 из этого каталога
sdk_log() { printf '%s\n' "$*" >&2; }

sdk_test_load() {
    local dir="$1" py="$2" bin=""
    local lib="$dir/Library/TeamTalk_DLL/libTeamTalk5.so"
    [ -f "$lib" ] || return 1
    case "$py" in
        "")  return 0 ;;
        */*) [ -x "$py" ] && bin="$py" ;;
        *)   bin="$(command -v "$py" 2>/dev/null || true)" ;;
    esac
    # если интерпретатор из .venv не создан (или его снесли) — проверяем системным
    [ -n "$bin" ] || bin="$(command -v python3 2>/dev/null || true)"
    [ -n "$bin" ] || return 1
    # 1) библиотека должна реально загружаться: ловит несовместимый glibc
    ( LD_LIBRARY_PATH="$dir/Library/TeamTalk_DLL" \
      "$bin" -c 'import ctypes,sys; ctypes.CDLL(sys.argv[1])' "$lib" ) >/dev/null 2>&1 || return 1
    # 2) и питоновский модуль поверх неё должен импортироваться
    ( LD_LIBRARY_PATH="$dir/Library/TeamTalk_DLL" \
      PYTHONPATH="$dir/Library/TeamTalkPy" \
      "$bin" -c 'import TeamTalk5' ) >/dev/null 2>&1 || return 1
    return 0
}

sdk_install_best() {
    local repo="$1" py="$2" host_glibc tmp url ub name need dir ok=""
    host_glibc="$(sdk_host_glibc)"
    [ -n "$host_glibc" ] || host_glibc="0"

    if [ "$DRY_RUN" = "1" ]; then
        sdk_log "    [проверка] поиск свежего SDK на bearware.dk и загрузка подходящей сборки"
        return 0
    fi

    sdk_log "  glibc системы: $host_glibc"
    tmp="$(mktemp -d)"
    while read -r ub url; do
        [ -n "${url:-}" ] || continue
        need="$(sdk_ubuntu_glibc "$ub")"
        if ! sdk_ver_le "$need" "$host_glibc"; then
            sdk_log "  сборка для Ubuntu $ub (glibc $need) не подойдёт — пропускаю"
            continue
        fi
        sdk_log "  Качаю $(basename "$url")..."
        if ! curl -fsSL -m 600 -o "$tmp/sdk.7z" "$url"; then
            sdk_log "  ! не скачалось"
            continue
        fi
        rm -rf "${tmp:?}/x"; mkdir -p "$tmp/x"
        if ! 7z x -y -o"$tmp/x" "$tmp/sdk.7z" >/dev/null 2>&1; then
            sdk_log "  ! не распаковалось (нужен p7zip-full)"
            continue
        fi
        dir="$(find "$tmp/x" -maxdepth 1 -mindepth 1 -type d | head -1)"
        [ -n "$dir" ] || continue
        name="$(basename "$dir")"
        if sdk_test_load "$dir" "$py"; then
            sdk_log "  Проверка импорта: OK"
            mkdir -p "$repo/sdk"
            rm -rf "${repo:?}/sdk/$name"
            mkdir -p "$repo/sdk/$name"
            cp -a "$dir/." "$repo/sdk/$name/"
            ok="$repo/sdk/$name"
            break
        fi
        sdk_log "  ! сборка не грузится (нужен более новый glibc) — пробую следующую"
    done <<EOF
$(sdk_candidates)
EOF
    rm -rf "$tmp"
    printf '%s' "$ok"
}
