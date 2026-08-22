# TeamTalk Music Bot

Бот для TeamTalk 5: проигрывает музыку и другое аудио в голосовом канале по ссылкам и поиску. Аудио передаётся как голос (`TT_InsertAudioBlock`), звуковые устройства не требуются.

## Возможности

- Поиск и проигрывание: `п <запрос>`, `пи <запрос>`, `play <запрос>` — по умолчанию YouTube (`ytsearch`), список до 10 результатов
- Автопереход по списку: после окончания трека бот сам играет следующий
- Переключение: `n` / `b` — следующий/предыдущий из списка
- Пауза/резюме: `п` (toggle), `пи`
- Стоп: `с` / `s` / `стоп`
- Скип: `скип` / `дальше`
- Громкость: `v <1-100>` (перекодировка через ffmpeg)
- Выбор сервиса: `sv yt` / `sv rt` / `sv ym` (YouTube / Rutube / Яндекс.Музыка)
- Локальные файлы: `lf <путь>` — проиграть файл с диска
- Смена ника: `cn <ник>` (сохраняется между рестартами)
- Радио (m3u): `радио` — список станций, `радио <номер>` — запустить станцию, `радио <текст>` — поиск по названию
- Телеграм-реле: бот-шлюз, в него можно кидать аудио/видео/голосовые — они транслируются в канал (токен бота через `TG_TOKEN`)

Сервисы:
- Rutube — прямые ссылки; поиск заблокирован их защитой от ботов
- Яндекс.Музыка — по OAuth-токену (`ym_token.txt`), играет 320kbps mp3
- YouTube — прямые ссылки и поиск; на датацентровых IP свежие видео могут требовать cookies (`--cookies`)

## Требования

- Ubuntu/Debian (x86_64), Python 3.8+, ffmpeg
- TeamTalk 5 SDK для Ubuntu 22.04 x86_64 (папка `sdk/tt5sdk*` с `libTeamTalk5.so`) — входит в репозиторий
- Доступ к TeamTalk-серверу и права бота в канале: `USERRIGHT_TRANSMIT_VOICE`

## Установка (от клонирования до запуска)

### 1. Клонируй репозиторий

```bash
git clone https://github.com/zolo-kirill/teamtalk-music-bot.git
cd teamtalk-music-bot
```

### 2. Проверь TeamTalk SDK

SDK уже входит в репозиторий (папка `sdk/`), отдельно качать ничего не нужно. Проверь, что библиотека на месте:

```bash
ls sdk/tt5sdk*/Library/TeamTalk_DLL/libTeamTalk5.so
```

### 3. Установи системные пакеты

```bash
sudo apt-get update
sudo apt-get install -y ffmpeg python3 python3-pip python3-venv
```

### 4. Автоустановщик (venv, зависимости, сервис)

```bash
bash install.sh
```

Что делает установщик: создаёт `.venv` с yt-dlp и yandex-music, проверяет SDK, создаёт шаблон `.secrets/.env`, ставит **пользовательский** systemd-сервис `teamtalk-music-bot` с автозапуском. Бот работает под твоим пользователем (не root) — sudo нужен только для установки системных пакетов. Установщик проверяет, что запущен не под root.

Или вручную:

```bash
# venv с зависимостями
python3 -m venv .venv
.venv/bin/pip install --upgrade yt-dlp yandex-music

# шаблон конфигурации
mkdir -p ../.secrets
# создай ../.secrets/.env — см. шаблон в install.sh
```

### 5. Настрой секреты — `../.secrets/.env`

Файл лежит на уровень выше проекта (вне репозитория, не попадает в git). Минимум для работы:

```bash
TEAMTALK_HOST=example.com
TEAMTALK_TCP_PORT=10333
TEAMTALK_UDP_PORT=10333
TEAMTALK_USERNAME=example
TEAMTALK_PASSWORD=example
TEAMTALK_NICKNAME=MusicBot
# TEAMTALK_CHANNEL=/root/music   # раскомментируй, чтобы бот сам входил в канал
```

Опционально:

- **Яндекс.Музыка** — положи OAuth-токен в `../.secrets/ym_token.txt`
- **Телеграм-реле** — токен бота от @BotFather как `TG_TOKEN=...` в `.env`
- **YouTube** — cookies залогиненного аккаунта в `../.secrets/cookies.txt` (для свежих видео с датацентровых IP)

### 6. Запуск

```bash
bash run.sh
```

Или, если ставил через `install.sh`, — сервис уже работает и поднимется сам после перезагрузки (сервис пользовательский, sudo не нужен):

```bash
systemctl --user status teamtalk-music-bot    # статус
journalctl --user -u teamtalk-music-bot -f    # логи
```

## Файлы

- `bot.py` — основной бот (ctypes-биндинги TeamTalk)
- `radio/` — плейлисты радиостанций (m3u, сгруппированы по категориям)
- `run.sh` — обёртка запуска (SDK env, секреты, venv)
- `install.sh` — автоустановщик
- `teamtalk-music-bot.service` — systemd-юнит
- `send_cmd.py`, `diag.py` — утилиты для тестирования/диагностики
