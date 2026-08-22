#!/usr/bin/env python3
"""Универсальный плагин для TeamTalk-сервера: музыка в голосовой канал,
реле сообщений и файлов в Telegram, защита от ботнетов, регистрация учёток."""
import array
import base64
import hashlib
import ctypes
import json
import os
import queue
import re
import secrets
import select
import socket
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request

import requests
import uuid
import bisect
import glob
import ipaddress
from collections import deque
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from ctypes import byref

import TeamTalk5
from TeamTalk5 import (
    TextMsgType,
    buildTextMessage,
    MediaFileStatus,
    MediaFilePlayback,
    StreamType,
    TT_MEDIAPLAYBACK_OFFSET_IGNORE,
)


def _b(s):
    """TeamTalk ctypes bindings expect UTF-8 bytes (c_char_p) on Linux."""
    return s.encode("utf-8") if isinstance(s, str) else s


def _media_env():
    """Окружение для внешних медиа-инструментов (ffmpeg/parec).

    run.sh кладёт $DIR/lib в LD_LIBRARY_PATH ради библиотек TeamTalk5, но
    там же лежат чужие копии libav*/libasound, из-за которых системный
    ffmpeg не стартует (например, libxml2.so.2 не найден). Дочерним
    процессам отдаём окружение без этого пути."""
    env = dict(os.environ)
    env.pop("LD_LIBRARY_PATH", None)
    return env

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


# ---- прокси для запросов к Telegram API и YouTube ----
# Дата-центр сервера DPI-блокирует исходящие TCP к подсетям Telegram, поэтому
# запросы к api.telegram.org идут через локальный мост (TG_PROXY). Тот же дата-центр
# режет и домены YouTube (www.youtube.com, googlevideo.com), поэтому yt-dlp для
# YouTube тоже ходит через мост (YT_PROXY). yandex-music, rutube и пр. — напрямую.
TG_PROXY = os.environ.get("TG_PROXY", "").strip()
YT_PROXY = os.environ.get("YT_PROXY", "").strip()
# Домашний туннель (см. hometun/README.md): когда он поднят, YouTube качается
# через него (домашний интернет), иначе — обычным каналом. Формат как у yt-dlp,
# например socks5://127.0.0.1:1080
YT_TUNNEL = os.environ.get("YT_TUNNEL", "").strip()
_TG_OPENER = None


def _tunnel_proxy():
    """Домашний/RU-туннель, если он сейчас реально поднят.

    Возвращает строку YT_TUNNEL только когда порт отвечает, иначе пустую строку.
    Нужен там, где прямой выход заведомо заблокирован по гео (Rutube): там
    нельзя откатываться на зарубежный мост, но и подниматься на мёртвый туннель
    тоже бессмысленно — yt-dlp просто упадёт по таймауту.
    """
    if not YT_TUNNEL:
        return ""
    m = re.match(r"^socks5h?://([^:/]+):(\d+)", YT_TUNNEL)
    if not m:
        return ""
    host, port = m.group(1), int(m.group(2))
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return YT_TUNNEL
    except OSError:
        return ""


def _yt_proxy():
    """Прокси для очередного скачивания YouTube.

    Если настроен домашний туннель (YT_TUNNEL) и он сейчас поднят — YouTube идёт
    через него. Если туннель закрыт или не настроен — обычный YT_PROXY, а когда
    и он пуст — напрямую. Режим без YT_TUNNEL не меняет прежнего поведения:
    YT_PROXY, иначе мост TG_PROXY.
    """
    if YT_TUNNEL:
        m = re.match(r"^socks5h?://([^:/]+):(\d+)", YT_TUNNEL)
        if m:
            host, port = m.group(1), int(m.group(2))
            try:
                with socket.create_connection((host, port), timeout=0.5):
                    return YT_TUNNEL
            except OSError:
                pass
        # Туннель настроен, но лежит: раньше здесь возвращался только YT_PROXY,
        # и при пустом YT_PROXY бот уходил напрямую — а дата-центр режет YouTube,
        # так что видео не качалось вовсе, пока не поднимут домашний туннель.
        # Откат должен вести на общий мост, как и в ветке без туннеля.
        return YT_PROXY or TG_PROXY
    return YT_PROXY or TG_PROXY


_TG_TL = threading.local()


def _tg_session():
    """Пул соединений на поток: urlopen платит TLS-хендшейк на каждый запрос
    (~0.3-0.5 с через мост, а если маршрут залип — до 25 с), keep-alive
    переиспользует уже открытый туннель."""
    s = getattr(_TG_TL, "s", None)
    if s is None:
        s = requests.Session()
        if TG_PROXY:
            s.proxies = {"http": TG_PROXY, "https": TG_PROXY}
        adapter = requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=8)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        _TG_TL.s = s
    return s


def _tg_reset_session():
    s = getattr(_TG_TL, "s", None)
    if s is not None:
        try:
            s.close()
        except Exception:
            pass
        _TG_TL.s = None


class _TgResp(object):
    """Ответ requests в том виде, в каком его ждёт код: read() и with."""

    __slots__ = ("_r",)

    def __init__(self, r):
        self._r = r

    def read(self):
        return self._r.content

    def getcode(self):
        return self._r.status_code

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._r.close()
        return False


def _tg_urlopen(req, timeout):
    """Запрос к Telegram API через keep-alive пул (и мост, если задан)."""
    if isinstance(req, str):
        # скачивание файла: сюда приходит обычный URL, а не Request
        r = _tg_session().get(req, timeout=timeout)
        r.raise_for_status()
        return _TgResp(r)
    data = req.data
    headers = dict(req.header_items())
    headers.setdefault("Connection", "keep-alive")
    if data is not None:
        # requests не ставит Content-Type для тела-байтов, и Telegram
        # тело не разбирает: «Bad Request: file_id not specified».
        headers.setdefault("Content-type", "application/x-www-form-urlencoded")
    for attempt in (1, 2):
        try:
            r = _tg_session().request(req.get_method(), req.full_url, data=data,
                                      headers=headers, timeout=timeout)
            r.raise_for_status()
            return _TgResp(r)
        except requests.RequestException:
            _tg_reset_session()
            if attempt == 2:
                raise


# ---- признаки ботнет-клиентов (массовые входы «мёртвых» ботов) ----
# Флуд-ники вида «🤖 Shadow_Pilot_46»: эмодзи + латинское слово_слово_число.
# Кириллические ники и одиночные подчёркивания («kirill_mobile») не трогаем.
_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U00002B00-\U00002BFF\U00002190-\U000021FF\U0000FE0F\U00002300-\U000023FF]"
)
_BOTNET_WORD_RE = re.compile(r"[A-Za-z]{2,}_[A-Za-z0-9]{1,}(?:_[0-9]{1,})?")


def _deep_merge(base, over):
    out = dict(base or {})
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


CONFIG_PATH = os.environ.get("CONFIG") or os.path.join(BASE_DIR, "config.json")
CFG = _deep_merge(
    _load_json(os.path.join(BASE_DIR, "config_default.json")),
    _load_json(CONFIG_PATH),
)


def _cfg(path, env=None, default=None):
    """Value from CFG by dotted path; env var and default as fallback."""
    node = CFG
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            node = None
            break
        node = node[part]
    if isinstance(node, (dict, list)):
        if node:
            return node
    elif node is not None and node != "":
        return node
    if env and env in os.environ and os.environ.get(env) != "":
        return os.environ[env]
    return default


_CYR_DIGRAPHS = (
    ("sch", "щ"), ("tch", "ч"), ("dzh", "дж"), ("sh", "ш"), ("ch", "ч"),
    ("th", "т"), ("ph", "ф"), ("kh", "х"), ("zh", "ж"), ("ck", "к"),
    ("qu", "кв"), ("kn", "н"), ("wh", "в"), ("wr", "р"),
    ("ight", "айт"), ("igh", "ай"), ("eigh", "эй"),
    ("oo", "у"), ("ee", "и"), ("ea", "и"), ("ay", "эй"), ("ey", "эй"),
    ("oy", "ой"), ("ai", "эй"), ("ei", "эй"), ("ou", "ау"), ("ow", "оу"),
    ("ya", "я"), ("yu", "ю"), ("yo", "ё"), ("ye", "е"),
    ("ce", "се"), ("ci", "си"), ("cy", "си"),
)
_CYR_LETTERS = {
    "a": "а", "b": "б", "c": "к", "d": "д", "e": "е", "f": "ф", "g": "г",
    "h": "х", "i": "и", "j": "дж", "k": "к", "l": "л", "m": "м", "n": "н",
    "o": "о", "p": "п", "q": "к", "r": "р", "s": "с", "t": "т", "u": "у",
    "v": "в", "w": "в", "x": "кс", "y": "й", "z": "з",
}


def _save_config(patch):
    """Deep-merge `patch` into the on-disk config (CONFIG_PATH) and rewrite the
    file atomically, so runtime changes survive a restart."""
    data = _load_json(CONFIG_PATH)
    stack = [(data, patch)]
    while stack:
        dst, src = stack.pop()
        for k, v in src.items():
            if isinstance(v, dict) and isinstance(dst.get(k), dict):
                stack.append((dst[k], v))
            else:
                dst[k] = v
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, CONFIG_PATH)


HOST = str(_cfg("server.host", "TEAMTALK_HOST", "example.com"))
TCP_PORT = int(_cfg("server.tcp_port", "TEAMTALK_TCP_PORT", 10333))
UDP_PORT = int(_cfg("server.udp_port", "TEAMTALK_UDP_PORT", 10333))
NICKNAME = str(_cfg("server.nickname", "TEAMTALK_NICKNAME", "UniversalBot"))
USERNAME = str(_cfg("server.username", "TEAMTALK_USERNAME", "example"))
PASSWORD = str(_cfg("server.password", "TEAMTALK_PASSWORD", ""))
CHANNEL = str(_cfg("server.channel", "TEAMTALK_CHANNEL", ""))  # empty = root channel
CHANNEL_PASSWORD = str(_cfg("server.channel_password", "TEAMTALK_CHANNEL_PASSWORD", ""))
DEFAULT_VOLUME = int(_cfg("playback.default_volume", None, 10))
# Стартовое воспроизведение: что играть при запуске бота и с какой громкостью.
# resume_last=False — больше не возвращаем последнюю ссылку после перезапуска.
STARTUP_RESUME_LAST = bool(_cfg("startup.resume_last", None, False))
STARTUP_URL = str(_cfg("startup.url", None, "") or "").strip()
try:
    STARTUP_VOLUME = int(_cfg("startup.volume", None, 0) or 0)
except (TypeError, ValueError):
    STARTUP_VOLUME = 0
MAX_VOLUME = int(_cfg("playback.max_volume", None, 100))
DEFAULT_SERVICE = str(_cfg("runtime.main_service", None, "yt"))
DEFAULT_CHANNEL_MSG = bool(_cfg("runtime.send_to_channel", None, True))
START_COMMANDS = list(_cfg("runtime.startup_commands", None, []))
CLIENTNAME = "teamtalk-universal-plugin"
CONNECT_TIMEOUT = int(_cfg("server.connect_timeout_sec", None, 20))

CACHE_DIR = os.path.join(BASE_DIR, "cache")
os.makedirs(CACHE_DIR, exist_ok=True)
INBOX_DIR = os.path.join(BASE_DIR, "inbox")  # files relayed from Telegram
os.makedirs(INBOX_DIR, exist_ok=True)

STATUS_MSG_FILE = os.path.join(BASE_DIR, ".statusmsg")
CHANNEL_MSG_FILE = os.path.join(BASE_DIR, ".channel_msg")
VOICE_ANNOUNCE_FILE = os.path.join(BASE_DIR, ".voice_announce")
NET_MSG_FILE = os.path.join(BASE_DIR, ".net_msg")  # /as: сетевые приветствия
FAVORITES_FILE = os.path.join(BASE_DIR, "favorites.json")
SUBS_FILE = os.path.join(BASE_DIR, "subs.json")
SUB_TTL_SEC = 86400  # сколько живёт ссылка-подписка (24 ч)
ADMINS_FILE = os.path.join(BASE_DIR, "users.db")  # user id администраторов Telegram
BANS_FILE = os.path.join(BASE_DIR, "bans.json")  # кого бот забанил через Telegram (для /unban)
REPLIES_FILE = os.path.join(BASE_DIR, "replies.json")  # пересланные сообщения TeamTalk: tg_message_id → tt_user_id
REPLY_TTL_SEC = 3600  # сколько можно ответить на пересланное сообщение (1 ч, как в sender-rs)
MUSIC_SUBS_FILE = os.path.join(BASE_DIR, "music_subs.json")  # подписчики на музыку в Telegram
LAST_PLAYED_FILE = os.path.join(BASE_DIR, "last_played.json")  # последний трек: переживает перезапуск
MUSIC_SUB_TTL_SEC = 86400  # сколько живёт ссылка подписки на музыку (24 ч)

TG_TOKEN = str(_cfg("telegram.token", "TG_TOKEN", "")).strip()  # optional: own Telegram bot that relays files
TG_OWNER_USER_ID = int(_cfg("telegram.owner_user_id", "TG_OWNER_USER_ID", 0) or 0)  # only this user can send commands
TG_NOTIFY_CHAT_ID = int(_cfg("telegram.notify_chat_id", "TG_NOTIFY_CHAT_ID", 0) or 0)
TG_SEND_ONLY = str(_cfg("telegram.send_only", "TG_SEND_ONLY", "")).strip().lower() in ("1", "true", "yes", "on")  # только отправка, без опроса
TG_JOIN_TO_SUBS = str(_cfg("telegram.join_leave_to_subs", "TG_JOIN_TO_SUBS", "")).strip().lower() in ("1", "true", "yes", "on")  # входы/выходы подписчикам
  # сюда слать вход/выход пользователей (0 = выкл)
TG_NOTIFY_SERVER = str(_cfg("telegram.server_display_name", "TG_NOTIFY_SERVER_NAME", "")).strip()  # пусто = брать имя сервера из TeamTalk
TG_NOTIFY_IGNORE = {u.strip().lower() for u in (_cfg("telegram.ignore_usernames", None, []) or []) if u.strip()}
TG_NOTIFY_IGNORE.add("bot_admin")  # все боты на одной админ-учётке — их не анонсируем
# Исключения по НИКУ (а не логину): таких не приветствуем сетевым сообщением
# и не анонсируем вход/выход. Нужно, когда учётка частая или служебная и логин
# общий (гости), а замолчать надо конкретного по имени в чате (напр. «Stats»).
TG_NOTIFY_IGNORE_NICKS = {n.strip().lower() for n in (_cfg("telegram.ignore_nicks", None, []) or []) if n.strip()}

# Служебные учётки проверяющих сервер (списки серверов TeamTalk): сборщик
# статистики заходит под ником/логином «Stats» — его не приветствуем и не
# анонсируем вход/выход. Продублировано в коде, чтобы работало независимо
# от того, что записано в config.json конкретного сервера.
TG_NOTIFY_IGNORE.add("stats")
TG_NOTIFY_IGNORE_NICKS.add("stats")

# Слежение за правами учёток (account_watch): если у учётки изменились права —
# добавили/убрали разрешение или сменился тип — всех, кто сейчас залогинен под
# ней, выкидываем, чтобы перезашли и получили новые права. Сервер применяет
# права учётки при логине; живая сессия держит старые до переподключения.
ACC_WATCH_ENABLED  = bool(_cfg("account_watch.enabled", None, True))
ACC_WATCH_INTERVAL = int(_cfg("account_watch.interval_sec", None, 10) or 10)
ACC_WATCH_PAGE     = int(_cfg("account_watch.page_size", None, 50) or 50)
ACC_WATCH_NOTIFY   = bool(_cfg("account_watch.notify_owner", None, True))
# Пауза между личным предупреждением и киком: человек должен успеть прочитать,
# зачем его выбрасывает. 0 — кикать сразу (account_watch.notice_delay_sec).
ACC_WATCH_NOTICE_DELAY = float(_cfg("account_watch.notice_delay_sec", None, 5) or 5)
ACC_WATCH_NOTICE_TEXT  = ("Права учётной записи изменились. "
                          "Войдите на сервер заново, чтобы они вступили в силу.")

# Приветствие при входе пользователя на сервер: просьба ознакомиться с правилами
# (welcome.rules_text в config.json; пусто — стандартная строка).
WELCOME_RULES = str(_cfg("welcome.rules_text", None, "") or "").strip()
WELCOME_TEXT_ENABLED = bool(_cfg("welcome.text_enabled", None, True))

# ---- Голосовое приветствие (Silero TTS) ------------------------------
WELCOME_VOICE_ENABLED = bool(_cfg("welcome.voice_enabled", None, True))
WELCOME_VOICE_TEXT = str(_cfg("welcome.voice_text", None,
    "Привет, {nick}! Добро пожаловать на сервер {server}.") or "").strip()
WELCOME_VOICE_SPEAKER = str(_cfg("welcome.voice_speaker", None, "baya") or "baya").strip()
# Неизвестный голос переводим на Silero
if WELCOME_VOICE_SPEAKER not in ("baya", "kseniya", "xenia", "eugene", "aidar"):
    WELCOME_VOICE_SPEAKER = "baya"

# Двуязычная озвучка: латиницу читает английская модель (v3_en),
# кириллицу — русская (v4_ru). welcome.voice_bilingual=false возвращает
# старую транслитерацию в кириллицу.
WELCOME_VOICE_BILINGUAL = bool(_cfg("welcome.voice_bilingual", None, True))
WELCOME_EN_SPEAKER = str(_cfg("welcome.voice_en_speaker", None, "en_21") or "en_21").strip()

# Голоса Silero v4_ru, доступные для выбора из Telegram
WELCOME_VOICES = ("baya", "kseniya", "xenia", "eugene", "aidar")
WELCOME_VOICE_LABELS = {
    "baya": "Бая (жен)",
    "kseniya": "Ксения (жен)",
    "xenia": "Ксения-2 (жен)",
    "eugene": "Евгений (муж)",
    "aidar": "Айдар (муж)",
}
WELCOME_VOICE_DUCK = float(_cfg("welcome.voice_duck", None, 0.15) or 0.15)
# Приглушение эфира на время голосового из Телеграма (music.voice_duck)
VOICE_FILE_DUCK = float(_cfg("music.voice_duck", None, 0.2) or 0.2)
# Прямые трансляции. Twitch отдаёт HLS только по внутреннему GQL-запросу
# (публичный Client-ID веб-плеера, без входа в аккаунт), Kick — через свой
# открытый API канала. yt-dlp для Kick не годится, для Twitch требует входа.
_TWITCH_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"
_TWITCH_GQL = "https://gql.twitch.tv/gql"
_TWITCH_GQL_HASH = "0828119ded1c13477966434e15800ff57ddacf13ba1911c129dc2200705b0712"
_STREAM_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

try:
    # 0 — приветствовать каждый вход; «or 0» нужен, чтобы 0 не превращался в 900
    WELCOME_VOICE_COOLDOWN = max(0, int(_cfg("welcome.voice_cooldown", None, 900) or 0))
except (TypeError, ValueError):
    WELCOME_VOICE_COOLDOWN = 900
SILERO_MODEL_ID = str(_cfg("welcome.silero_model", None, "v4_ru") or "v4_ru")

# ---- Имя сервера для озвучки -----------------------------------------
_TT_JUNK_RE = re.compile(r"#[A-Za-z][A-Za-z0-9_-]*#")


def _fresh_cookies(src=None):
    """yt-dlp rewrites the --cookies file in place on every run, stripping the
    signed-in auth cookies (SID/PSID/etc). Hand it a per-run writable COPY so
    the master file keeps the full session and YouTube bot-check stays solved."""
    src = src or COOKIES
    if not src or not os.path.isfile(src):
        return None
    dst = os.path.join(CACHE_DIR, "ck_%s.txt" % uuid.uuid4().hex[:10])
    try:
        shutil.copyfile(src, dst)
        return dst
    except Exception:
        return None


def _drop_cookies(path):
    if path and os.path.exists(path):
        try:
            os.unlink(path)
        except Exception:
            pass


def _find_downloaded(out):
    """Файл, который yt-dlp реально создал для префикса out.*. Скачивание идёт
    без перекодирования в mp3, так что расширение заранее неизвестно
    (webm/m4a/mp3/…) — ищем по префиксу. Возвращает путь или None."""
    try:
        for p in sorted(glob.glob(out + ".*")):
            if os.path.isfile(p) and os.path.getsize(p) > 0:
                return p
    except Exception:
        pass
    return None


def _looks_like_cookie_export(text):
    """Владелец прислал экспорт кук (Netscape): строка с шапкой + youtube.com."""
    t = (text or "").lstrip()
    return t.startswith("# Netscape HTTP Cookie File") or (
        t.startswith(".youtube.com") and "youtube.com" in t
    )


def _normalize_cookie_text(text):
    """Экспорт кук → tab-разделяемый Netscape-файл только с youtube-куками."""
    out = []
    for ln in (text or "").splitlines():
        if ln.startswith("#") or not ln.strip():
            continue
        f = ln.split()
        dom = f[0].lstrip(".")
        if dom != "youtube.com" and not dom.endswith(".youtube.com"):
            continue
        if len(f) == 6:
            f.append("")  # пустое значение
        if len(f) != 7:
            raise ValueError("строка не из 7 полей: %s" % ln[:80])
        out.append("\t".join(f))
    if not out:
        raise ValueError("нет youtube-кук в экспорте")
    return "# Netscape HTTP Cookie File\n" + "\n".join(out) + "\n"


# Optional Yandex Music OAuth token (config services.ym.token, else .secrets/ym_token.txt).
YM_TOKEN = str(_cfg("services.ym.token", None, "")).strip()
if not YM_TOKEN:
    _ym_path = os.environ.get("TEAMTALK_YM_TOKEN") or os.path.join(
        BASE_DIR, "..", ".secrets", "ym_token.txt"
    )
    if os.path.isfile(_ym_path):
        YM_TOKEN = open(_ym_path, encoding="utf-8").read().strip()

URL_RE = re.compile(r"https?://\S+", re.I)

# FFMpeg/yt-dlp resolve via PATH
YTDLP = sys.executable and [sys.executable, "-m", "yt_dlp"]

# JS runtime for yt-dlp challenge solving (deno binary; override via config/env).
YT_JS_RUNTIME = os.environ.get("YT_JS_RUNTIME")
if YT_JS_RUNTIME is None:
    YT_JS_RUNTIME = _cfg("runtime.yt_js_runtime", None, None)
if not YT_JS_RUNTIME:
    YT_JS_RUNTIME = shutil.which("deno") or "/home/superlisa/.local/bin/deno"

# YouTube po_token provider extractor-arg; empty disables it (e.g. no bgutil server).
YT_PO_TOKEN = os.environ.get("YT_PO_TOKEN_EXTRACTOR")
if YT_PO_TOKEN is None:
    YT_PO_TOKEN = _cfg("runtime.yt_po_token_extractor", None, None)
    if YT_PO_TOKEN is None:
        YT_PO_TOKEN = "youtube:po_token_provider=bgutil:http"

# Max tracks loaded from a playlist (YouTube / Yandex Music). High default so big
# playlists («Мне нравится» ≈ тысячи треков) load fully; override via config.
PLAYLIST_LIMIT = int(_cfg("runtime.playlist_limit", None, 5000))

# Voice transmission: raw PCM fed to TT_InsertAudioBlock as STREAMTYPE_VOICE.
# Стерео: серверные каналы стоят на Opus 48000/2ch, так что шлём 2 канала.
VOICE_RATE = 48000  # Hz
VOICE_CHANNELS = 2
VOICE_CHUNK = 960   # samples per channel per block (20 ms at 48 kHz)
VOICE_CHUNK_BYTES = VOICE_CHUNK * 2 * VOICE_CHANNELS  # s16 interleaved
# Запас PCM, который держим вычитанным впереди эфирного темпа. Без него
# ffmpeg упирается в 64-килобайтный pipe (0,34 с звука), и на стыке
# HLS-сегментов канал получает разрыв.
VOICE_READ_AHEAD = int(VOICE_RATE * VOICE_CHANNELS * 2 * 1.5)  # 1,5 секунды
VOICE_READ_CHUNK = 65536  # читаем пачками, а не по 20 мс

# Playback through PulseAudio: when set, the track is played by ffmpeg into
# this sink and captured back from its monitor, so anything audible on the
# machine can be routed into the channel. Empty = decode straight to PCM.
PULSE_SINK = _cfg("playback.pulse_sink", None, "") or ""
# Тихий режим: автоматические уведомления о воспроизведении не уходят ни в канал, ни в Telegram.
QUIET_PLAYBACK = str(_cfg("runtime.quiet_playback", None, "1")).strip().lower() not in ("0", "false", "no", "off")


def log(msg):
    print("%s %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def _fmt_ms(ms):
    s = int(ms) // 1000
    return "%d:%02d" % (s // 60, s % 60)


def _restart_bot_soon():
    """Exit the process shortly; the service supervisor (restart=always) relaunches."""
    time.sleep(1.5)
    log("exiting for restart")
    os._exit(0)


def _nightly_restart_delay(tzname="Europe/Moscow", hour=3, minute=0):
    """Секунды до ближайшего ночного перезапуска (по московскому времени)."""
    now = datetime.now(ZoneInfo(tzname))
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def _start_nightly_restart():
    """Daemon-поток: перезапуск бота каждую ночь в 03:00 МСК."""
    def loop():
        while True:
            delay = _nightly_restart_delay()
            log("nightly restart in %.0fs" % delay)
            time.sleep(delay)
            log("nightly restart")
            _restart_bot_soon()
    threading.Thread(target=loop, daemon=True).start()


_YTDLP_UPDATE_STAMP = os.path.join(BASE_DIR, ".ytdlp_update")


# ---- константы, восстановленные после резки ----
TTS_SOCK = str(_cfg("welcome.tts_socket", None, "") or os.path.join(CACHE_DIR, "tts.sock"))
GEO_CACHE = {}
GEO_CACHE_TTL = 6 * 3600
GEO_CACHE_MAX = 500
TG_MUSIC_TOKEN = str(_cfg("telegram.music_token", "TG_MUSIC_TOKEN", "")).strip()
REG_ENABLED = bool(_cfg("registration.enabled", None, False))
REG_TOKEN = str(_cfg("registration.token", "TG_REG_TOKEN", "")).strip()
REG_ADMIN_USER_IDS = [int(x) for x in (_cfg("registration.notify_user_ids", None, []) or []) if x]
REG_BROADCAST_TEXT = str(_cfg("registration.broadcast_text", None, "")).strip()
REG_ADMIN_TT_USER = str(_cfg("registration.admin_username", "TEAMTALK_ADMIN_USER", "bot_admin")).strip()
REG_ADMIN_TT_PASS = str(_cfg("registration.admin_password", "TEAMTALK_ADMIN_PASSWORD", "")).strip()
REG_ADMIN_TT_NICK = str(_cfg("registration.admin_nickname", None, "регистратор")).strip()
REG_USER_RIGHTS = int(_cfg("registration.user_rights", None, 0) or 0)
COOKIES = _cfg("services.yt.cookiefile_path", "TEAMTALK_COOKIES", None) or os.path.join(
    BASE_DIR, "..", ".secrets", "cookies.txt"
)
RUTUBE_COOKIES = _cfg("services.rt.cookiefile_path", "TEAMTALK_RUTUBE_COOKIES", None) or os.path.join(
    BASE_DIR, "..", ".secrets", "rutube_cookies.txt"
)

class MusicBot(TeamTalk5.TeamTalk):
    def __init__(self):
        super().__init__()
        self.api_q = queue.Queue()  # main-thread work items
        self.lock = threading.Lock()
        self.my_user_id = 0
        self.my_channel_id = 0
        self.play_channel_id = 0
        self.logged_in = False
        self.joined = False
        self.queue = []  # list of (url, title)
        self.current = None  # (url, title)
        self.current_file = None
        self.cur_source = None  # (key, label, is_radio) — what's playing now, for favorites
        self.favorites = self._load_favorites()  # list of {"key","label","radio"}
        self.playing = False
        self.downloading = set()
        self.reconnect = True
        self.connected = False
        self._last_err_sent = 0
        self.paused = False
        self.volume = min(DEFAULT_VOLUME, MAX_VOLUME)
        self.cur_vol = self.volume / 100.0  # actual gain, ramps toward self.volume
        self.duck = 1.0  # множитель приглушения (голосовое приветствие)
        self.service = DEFAULT_SERVICE  # "yt" = YouTube, "ym" = Yandex.Music
        self.cur_offset_ms = 0
        self.segment_started_at = 0
        self.current_orig = None
        # voice transmission (TT_InsertAudioBlock)
        self.voice_stop = threading.Event()
        self.voice_thread = None
        self.voice_proc = None
        self.voice_offset_base = 0
        self.voice_started_at = 0
        self._voice_run = 0  # поколение голоса: растёт при каждом старте/останове
        # last search result list for n/b switching and auto-advance
        self.search_results = []
        self.search_index = 0
        self.auto_list = False  # True when playing through the search-result list
        self.silent = False  # True when switching tracks on auto-advance (no status spam)
        # playlist navigation (YouTube / Yandex Music playlist links)
        self.playlist = []  # list of (url, title)
        self.playlist_index = -1
        self.auto_playlist = False  # True when playing through a playlist
        # radio stations from radio/ folder (m3u playlists grouped by category)
        self.radio = self._load_radio()
        if self.radio:
            log("radio loaded: %d stations" % len(self.radio))
        self.nickname = NICKNAME
        self.status_msg = ""
        try:
            s = open(STATUS_MSG_FILE).read().strip()
            if s:
                self.status_msg = s
        except Exception:
            pass
        # reply targeting: PM to the command author, optionally mirrored to channel
        self.reply_user_id = 0  # who sent the last command → PM replies
        self.channel_msg = self._load_channel_msg()  # mirror replies to channel (cm)
        self._announce_pending = None  # (path, offset) of real track waiting behind a title announcement
        # dl: upload current track to the channel as a server file (TT_DoSendFile)
        self._dl_cmd_id = None  # command id of the active upload (from TT_DoSendFile)
        self._dl_remote = None  # remote file name
        self._dl_local = None  # temp local copy to remove after upload
        # optional Telegram relay: an own bot that forwards files and commands into the channel
        self._tg_offset = 0
        self._tg_reply_chat = None  # set while handling a Telegram command → mirror replies
        self._pending_msg = None  # ожидание текста для отправки ЛС в TeamTalk: {cid, uid, nick}
        self._await_cookies = 0.0  # deadline режима приёма кук после /cookies (time.time); 0 = выключен
        self._ready_time = None  # when the bot finished joining — for join/leave notify grace
        # подписки на уведомления: /sub в TeamTalk → ссылка → активация в Telegram
        self.users = {}  # nUserID -> User (кто сейчас на сервере)
        self._tg_username = None  # bot username from getMe, для /sub-ссылок
        self.sub_pending = {}  # token -> {nick, username, nUserID, created}
        self.sub_active = {}  # chat_id(str) -> {nick, username, nUserID, subscribed_at}
        self._load_subs()
        # подписка на музыку: отдельный Telegram-бот, присылает играющие треки (sub mus)
        self.mus_pending = {}  # token -> {nick, username, nUserID, created}
        self.music_subs = {}  # chat_id(str) -> {nick, username, nUserID, subscribed_at}
        self._load_music_subs()
        self._music_username = None  # username музыкального бота (getMe, кэш)
        self._music_offset = 0
        self.admins = self._load_admins()  # user id администраторов Telegram (users.db); владелец — всегда
        self.pending_replies = {}  # пересылка реплик вырезана вместе с Telegram-управлением
        if TG_TOKEN and not TG_SEND_ONLY:
            log("telegram relay enabled (только исходящие уведомления)")
        if TG_MUSIC_TOKEN:
            threading.Thread(target=self._music_poll, daemon=True, name="music-poll").start()
            log("music telegram bot enabled")

    def _load_channel_msg(self):
        try:
            return open(CHANNEL_MSG_FILE).read().strip() == "1"
        except Exception:
            return DEFAULT_CHANNEL_MSG

    def _save_channel_msg(self):
        try:
            with open(CHANNEL_MSG_FILE, "w") as f:
                f.write("1" if self.channel_msg else "0")
        except Exception as e:
            log("channel_msg save err: %s" % e)

    def _scale_pcm(self, chunk):
        """Apply volume with a smooth ramp toward the target (no ffmpeg restart).

        Each call is one 20 ms block; self.cur_vol steps ~2% toward self.volume,
        so a full-scale change eases in over ~1 second instead of snapping.
        """
        target = (self.volume / 100.0) * self.duck
        cur = self.cur_vol
        if cur < target:
            cur = min(target, cur + 0.02)
        elif cur > target:
            cur = max(target, cur - 0.02)
        self.cur_vol = cur
        if cur >= 0.999:
            return chunk
        a = array.array("h")
        a.frombytes(chunk)
        for i in range(len(a)):
            a[i] = int(a[i] * cur)
        return a.tobytes()

    # ----- helpers ---------------------------------------------------
    # ----- SDK wrappers (bindings need explicit byte strings) --------
    def connect(self, host, tcp, udp, ltcp=0, ludp=0, enc=False):
        return TeamTalk5._Connect(self._tt, _b(host), tcp, udp, ltcp, ludp, enc)

    def doLogin(self, nick, user, pwd, client):
        return TeamTalk5._DoLoginEx(self._tt, _b(nick), _b(user), _b(pwd), _b(client))

    def doJoinChannelByID(self, cid, pwd):
        return TeamTalk5._DoJoinChannelByID(self._tt, cid, _b(pwd))

    def doTextMessage(self, msg):
        return TeamTalk5._DoTextMessage(self._tt, byref(msg))

    def startStreamingMediaFileToChannel(self, path, video_codec=None):
        return TeamTalk5._StartStreamingMediaFileToChannel(self._tt, _b(path), video_codec)

    def startStreamingMediaFileToChannelEx(self, path, playback, video_codec=None):
        return TeamTalk5._StartStreamingMediaFileToChannelEx(
            self._tt, _b(path), byref(playback), video_codec
        )

    def updateStreamingMediaFileToChannel(self, playback):
        return TeamTalk5._UpdateStreamingMediaFileToChannel(self._tt, byref(playback), None)

    def insertAudioBlock(self, block):
        return TeamTalk5._InsertAudioBlock(self._tt, block)

    def insertAudioBlockEnd(self):
        """End the raw-audio voice input session (lpAudioBlock=NULL)."""
        return TeamTalk5._InsertAudioBlock(self._tt, None)

    def doChangeStatus(self, status_mode, text):
        return TeamTalk5._DoChangeStatus(self._tt, status_mode, _b(text))

    def doChangeNickname(self, nick):
        return TeamTalk5._DoChangeNickname(self._tt, _b(nick))

    def getChannelIDFromPath(self, path):
        return TeamTalk5._GetChannelIDFromPath(self._tt, _b(path))

    # ----- telegram relay (own bot, forwards files into the channel) -----
    def _tg_api(self, method, **params):
        url = "https://api.telegram.org/bot%s/%s" % (TG_TOKEN, method)
        data = urllib.parse.urlencode(params).encode()
        req = urllib.request.Request(url, data=data, method="POST")
        with _tg_urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode())

    # ---- музыкальный Telegram-бот: подписка sub mus, раздача треков ----

    def _music_api(self, method, **params):
        url = "https://api.telegram.org/bot%s/%s" % (TG_MUSIC_TOKEN, method)
        data = urllib.parse.urlencode(params).encode()
        req = urllib.request.Request(url, data=data, method="POST")
        with _tg_urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode())

    def _music_bot_username(self):
        if self._music_username:
            return self._music_username
        try:
            res = self._music_api("getMe")
            u = ((res or {}).get("result") or {}).get("username")
            if u:
                self._music_username = u
        except Exception as e:
            log("music getMe err: %s" % str(e)[:100])
        return self._music_username

    def _music_send_text(self, chat_id, text):
        try:
            self._music_api("sendMessage", chat_id=chat_id, text=text[:4000])
        except Exception as e:
            log("music send err: %s" % str(e)[:120])

    def _music_send_document(self, chat_id, path, caption="", fname=None):
        """Отправить файл подписчику музыки. multipart собирается вручную — в venv нет requests."""
        try:
            boundary = "----BotBoundary" + uuid.uuid4().hex
            fname = fname or os.path.basename(path)
            with open(path, "rb") as f:
                fdata = f.read()
            def _part(name, value):
                return ("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                        % (boundary, name, value)).encode("utf-8")
            body = (
                _part("chat_id", str(chat_id))
                + _part("caption", caption)
                + ("--%s\r\nContent-Disposition: form-data; name=\"document\"; filename=\"%s\"\r\n"
                   "Content-Type: application/octet-stream\r\n\r\n" % (boundary, fname)).encode("utf-8")
                + fdata
                + ("\r\n--%s--\r\n" % boundary).encode("utf-8")
            )
            url = "https://api.telegram.org/bot%s/sendDocument" % TG_MUSIC_TOKEN
            req = urllib.request.Request(url, data=body, method="POST")
            req.add_header("Content-Type", "multipart/form-data; boundary=%s" % boundary)
            with _tg_urlopen(req, timeout=180) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            log("music send doc err: %s" % str(e)[:150])
            return None

    def _music_poll(self):
        while True:
            try:
                res = self._music_api("getUpdates", timeout=25, offset=self._music_offset)
                for upd in res.get("result", []):
                    self._music_offset = upd.get("update_id", 0) + 1
                    self._music_handle_update(upd)
            except Exception as e:
                log("music poll err: %s" % str(e)[:150])
                time.sleep(5)

    def _music_handle_update(self, upd):
        msg = upd.get("message")
        if not msg:
            return
        cid = str((msg.get("chat") or {}).get("id") or "")
        if not cid:
            return
        text = (msg.get("text") or "").strip()
        low = text.lower()
        if low in ("/unsub_mus", "/unsub", "/stop"):
            if cid in self.music_subs:
                del self.music_subs[cid]
                self._save_music_subs()
                self._music_send_text(int(cid), "Вы отписались от уведомлений о музыке.")
            else:
                self._music_send_text(int(cid), "Уведомления о музыке и так отключены.")
            return
        # /start sub_mus_<token> — активация подписки по ссылке
        token = text[len("/start "):].strip() if low.startswith("/start ") else ""
        if token.startswith("sub_mus_"):
            rec = self.mus_pending.pop(token, None)
            self._save_music_subs()
            if not rec:
                self._music_send_text(int(cid), "Ссылка недействительна или истекла. Повторите запрос sub mus в TeamTalk.")
                return
            rec["subscribed_at"] = time.time()
            self.music_subs[cid] = rec
            self._save_music_subs()
            who = rec.get("nick") or rec.get("username") or "аккаунт"
            self._music_send_text(int(cid), "Подписка на музыку оформлена (%s). Каждый сыгранный трек будет приходить в этот чат. Отписка: /unsub_mus." % who)
            return
        if cid in self.music_subs:
            self._music_send_text(int(cid), "Подписка на музыку активна. Треки приходят в этот чат. Отписка — /unsub_mus.")
        else:
            self._music_send_text(int(cid), "Это бот подписки на музыку. Отправьте в TeamTalk боту личное сообщение «sub mus» — придёт ссылка на подписку.")

    def _music_broadcast(self, path, title):
        """Разослать играющий трек всем подписчикам музыки (в фоне, не блокируя музыку)."""
        if not TG_MUSIC_TOKEN or not self.music_subs:
            return
        if not path or not os.path.isfile(path):
            return  # радио и треки без локального файла не шлём
        title = title or "трек"
        ext = os.path.splitext(path)[1] or ".mp3"
        safe = re.sub(r'[\\/:*?"<>|\s]+', "_", title).strip("_")[:80] or "track"
        fname = safe + ext
        subs = list(self.music_subs.keys())

        def _worker():
            for cid in subs:
                try:
                    self._music_send_document(int(cid), path, title[:100], fname)
                except Exception as e:
                    log("music broadcast err: %s" % str(e)[:120])
        threading.Thread(target=_worker, daemon=True, name="music-broadcast").start()

    def _load_music_subs(self):
        try:
            with open(MUSIC_SUBS_FILE, encoding="utf-8") as f:
                d = json.load(f)
            now = time.time()
            self.mus_pending = {
                t: p for t, p in (d.get("pending") or {}).items()
                if p.get("created", 0) > now - MUSIC_SUB_TTL_SEC
            }
            self.music_subs = {str(c): s for c, s in (d.get("active") or {}).items()}
        except Exception:
            self.mus_pending = {}
            self.music_subs = {}

    def _save_music_subs(self):
        try:
            now = time.time()
            self.mus_pending = {
                t: p for t, p in self.mus_pending.items()
                if p.get("created", 0) > now - MUSIC_SUB_TTL_SEC
            }
            with open(MUSIC_SUBS_FILE, "w", encoding="utf-8") as f:
                json.dump({"pending": self.mus_pending, "active": self.music_subs},
                          f, ensure_ascii=False, indent=2)
        except Exception as e:
            log("music subs save err: %s" % e)

    def _tg_send_text(self, chat_id, text):
        try:
            self._tg_api("sendMessage", chat_id=chat_id, text=text[:4000])
        except Exception as e:
            log("tg send err: %s" % str(e)[:120])

    # ---- inline-клавиатуры: интерактивное управление подписчиками ----

    def _notify(self, text):
        """Автоуведомление о воспроизведении: в тихом режиме не отправляется никуда."""
        if QUIET_PLAYBACK:
            log("_notify quiet-skip(%r)" % (text,))
            return
        self._send(text)

    def _send(self, text):
        if not self.logged_in:
            log("_send skip: logged_in=%s" % self.logged_in)
            return
        try:
            sent_pm = False
            if self.reply_user_id:
                msgs = buildTextMessage(
                    text, TextMsgType.MSGTYPE_USER, nToUserID=self.reply_user_id,
                    nChannelID=0, nFromUserID=self.my_user_id,
                )
                for m in msgs:
                    self.doTextMessage(m)
                sent_pm = True
            to_channel = bool(self.channel_msg) or not self.reply_user_id
            if to_channel and self.my_channel_id:
                msgs = buildTextMessage(
                    text, TextMsgType.MSGTYPE_CHANNEL, nChannelID=self.my_channel_id
                )
                for m in msgs:
                    self.doTextMessage(m)
            log("_send(%r) pm=%s chan=%s" % (text, sent_pm, to_channel and self.my_channel_id > 0))
        except Exception as e:
            log("send error: %s" % e)
        if self._tg_reply_chat:
            try:
                self._tg_api("sendMessage", chat_id=self._tg_reply_chat, text=text[:4000])
            except Exception as e:
                log("tg send err: %s" % str(e)[:120])

    def _enqueue_next(self):
        """Start next queue item from main thread."""
        if self.playing or not self.queue:
            return
        url, title = self.queue[0]
        self._start_download(url, title)

    def _start_download(self, url, title):
        if url in self.downloading:
            return
        self.downloading.add(url)
        t = threading.Thread(target=self._download_worker, args=(url, title), daemon=True)
        t.start()

    def _run_ydl(self, cmd, timeout=600):
        """Run yt-dlp, kill the whole process group on timeout (children may be node/js)."""
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True,
        )
        try:
            out, err = proc.communicate(timeout=timeout)
            return proc.returncode, out, err
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), 9)
            except Exception:
                proc.kill()
            proc.wait()
            raise

    _YT_ALT_CLIENTS = ("android", "ios", "tv_embedded", "web_embedded", "mweb", "web_safari")

    def _yt_alt_download(self, out, real_url):
        """yt-dlp не взял видео основным клиентом — перебираем запасные клиенты
        YouTube (player_client), с куками и анонимно. Возвращает (rc, stdout,
        stderr) первого успеха либо (None, "", последняя ошибка)."""
        last_err = ""
        for p in glob.glob(out + ".*"):  # подчистить хвосты прошлых попыток
            try:
                os.unlink(p)
            except Exception:
                pass
        for client in self._YT_ALT_CLIENTS:
            for use_ck in (True, False):
                cmd = list(YTDLP) + [
                    "--no-playlist", "--extractor-retries", "3", "--user-agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36", "--impersonate", "chrome", "--no-simulate",
                    # Так же без mp3-транскода, как и основной клиент
                    "-f", "ba/b",
                    "--remote-components", "ejs:github",
                    "--extractor-args", "youtube:player_client=%s" % client,
                    "--throttled-rate", "250K",
                    "-o", out + ".%(ext)s",
                    "--print", "%(title)s",
                ]
                if YT_JS_RUNTIME:
                    cmd += ["--js-runtimes", "deno:%s" % YT_JS_RUNTIME]
                _proxy = _yt_proxy()
                if _proxy:
                    cmd += ["--proxy", _proxy]
                ck = None
                if use_ck:
                    ck = _fresh_cookies(COOKIES)
                    if ck:
                        cmd += ["--cookies", ck]
                cmd += ["--", real_url]
                try:
                    rc, stdout, stderr = self._run_ydl(cmd, timeout=300)
                except subprocess.TimeoutExpired:
                    rc, stdout, stderr = 1, "", "timeout"
                finally:
                    if ck:
                        _drop_cookies(ck)
                if rc == 0 and _find_downloaded(out):
                    log("yt alt client %s ck=%d ok" % (client, use_ck))
                    return rc, stdout, stderr
                tail = ""
                blob = (stderr or stdout or "").strip()
                if blob:
                    tail = blob.splitlines()[-1][:200]
                log("yt alt client %s ck=%d fail: %s" % (client, use_ck, tail))
                if tail:
                    last_err = tail
        return None, "", last_err

    def _ym_search_list(self, query):
        """Search Yandex Music; return list of {"key","title"} (up to 10)."""
        if not YM_TOKEN:
            return []
        try:
            from yandex_music import Client
        except Exception as e:
            log("ym import err: %s" % e)
            return []
        try:
            client = Client(YM_TOKEN).init()
            res = client.search(query, type_="track", page=0)
            if not res or not res.tracks or not res.tracks.results:
                return []
            items = []
            for tr in res.tracks.results:
                title = " - ".join(tr.artists_name()) + " - " + tr.title
                items.append({"key": "ymtrack:%s" % tr.track_id, "title": title})
                if len(items) >= 10:
                    break
            return items
        except Exception as e:
            log("ym search err: %s" % e)
            return []

    def _ym_resolve(self, track_id):
        """Resolve a Yandex track id to (direct mp3 url, display title) or (None, err)."""
        try:
            from yandex_music import Client
            client = Client(YM_TOKEN).init()
            tr = client.tracks(track_id)[0]
            title = " - ".join(tr.artists_name()) + " - " + tr.title
            info = tr.get_download_info(get_direct_links=True)
            mp3 = [i for i in info if i.codec == "mp3"]
            if not mp3:
                return None, "У трека нет mp3-версии."
            best = sorted(mp3, key=lambda x: x.bitrate_in_kbps, reverse=True)[0]
            if not best.direct_link:
                return None, "Нет прямой ссылки на файл."
            return best.direct_link, title
        except Exception as e:
            return None, "Яндекс.Музыка: %s" % str(e)[:150]

    def _yt_search_list(self, query):
        """Return list of {"key","title"} for the first *video* results, skipping
        channels/playlists that a bare ytsearch would otherwise dive into."""
        cmd = list(YTDLP) + [
            "--flat-playlist",
            "--playlist-items", "1-10",
            "--no-warnings",
            "--print", "%(title)s\t%(url)s",
        ]
        _proxy = _yt_proxy()
        if _proxy:
            cmd += ["--proxy", _proxy]
        if YT_PO_TOKEN:
            cmd += ["--extractor-args", YT_PO_TOKEN]
        ck = _fresh_cookies()
        if ck:
            cmd += ["--cookies", ck]
        cmd += ["--", "ytsearch10:" + query]
        try:
            rc, out, err = self._run_ydl(cmd, timeout=90)
        except subprocess.TimeoutExpired:
            return []
        finally:
            _drop_cookies(ck)
        if rc != 0:
            log("yt search err: %s" % (err or out or "")[-200:])
            return []
        items = []
        for ln in (out or "").splitlines():
            parts = ln.split("\t", 1)
            if len(parts) != 2:
                continue
            t, u = parts[0].strip(), parts[1].strip()
            if re.search(r"youtube\.com/watch\?v=|youtu\.be/", u):
                items.append({"key": u, "title": t or u})
            if len(items) >= 10:
                break
        return items

    def _canonical_yt_url(self, url):
        """YouTube IDs are case-sensitive: a lowercased pasted link fails with
        'Video unavailable'. Recover the canonical spelling by searching the id
        text and picking the result whose id matches case-insensitively."""
        m = re.search(r"(?:watch\?v=|youtu\.be/)([A-Za-z0-9_-]{11})", url)
        if not m:
            return url
        vid = m.group(1)
        if not re.search(r"[A-Za-z]", vid):
            return url
        try:
            items = self._yt_search_list(vid)
        except Exception:
            return url
        for it in items:
            f = re.search(r"(?:watch\?v=|youtu\.be/)([A-Za-z0-9_-]{11})", it["key"])
            if f and f.group(1).lower() == vid.lower():
                if f.group(1) != vid:
                    return url.replace(vid, f.group(1))
                break
        return url

    def _is_yt_playlist_url(self, url):
        low = url.lower()
        if "youtube.com" not in low and "youtu.be" not in low:
            return False
        # watch/shorts/embed links (even with &list= autoplay param) are single videos
        if "watch?" in low or "/shorts/" in low or "/embed/" in low:
            return False
        return bool(re.search(r"(^|[/?&])list=", url)) or "/playlist" in low

    def _yt_playlist_items(self, url, limit=PLAYLIST_LIMIT):
        """Return list of (video_url, title) for a YouTube playlist (no download)."""
        cmd = list(YTDLP) + [
            "--flat-playlist",
            "--playlist-items", "1-%d" % limit,
            "--no-warnings",
            "--print", "%(title)s\t%(url)s",
        ]
        _proxy = _yt_proxy()
        if _proxy:
            cmd += ["--proxy", _proxy]
        if YT_PO_TOKEN:
            cmd += ["--extractor-args", YT_PO_TOKEN]
        ck = _fresh_cookies()
        if ck:
            cmd += ["--cookies", ck]
        cmd += ["--", url]
        try:
            rc, out, err = self._run_ydl(cmd, timeout=120)
        except subprocess.TimeoutExpired:
            return []
        finally:
            _drop_cookies(ck)
        if rc != 0:
            log("yt playlist err: %s" % (err or out or "")[-200:])
            return []
        items = []
        for ln in (out or "").splitlines():
            parts = ln.split("\t", 1)
            if len(parts) != 2:
                continue
            t, u = parts[0].strip(), parts[1].strip()
            if re.search(r"youtube\.com/watch\?v=|youtu\.be/", u):
                items.append((u, t or u))
            if len(items) >= limit:
                break
        return items

    def _is_ym_playlist_url(self, url):
        return bool(re.search(r"music\.yandex\.[^/]+/(?:users/[^/]+/playlists/\d+|playlists/[^/]+)", url))

    def _ym_playlist_items(self, url, limit=PLAYLIST_LIMIT):
        """Return list of (ymtrack:<id>, label) for a Yandex Music playlist.

        Supports both link shapes:
          music.yandex.ru/users/<login>/playlists/<kind>
          music.yandex.ru/playlists/lk.<uuid>   (personal/shared playlist links)
        """
        if not YM_TOKEN:
            return []
        m = re.search(r"music\.yandex\.[^/]+/users/([^/]+)/playlists/(\d+)", url)
        m2 = re.search(r"music\.yandex\.[^/]+/playlists/([^/]+)", url)
        try:
            from yandex_music import Client
            client = Client(YM_TOKEN).init()
            if m:
                pl = client.users_playlists(kind=int(m.group(2)), user_id=m.group(1))
            elif m2:
                pl = client.playlist(m2.group(1))
            else:
                return []
            if not pl:
                return []
            items = []
            for ts in (pl.fetch_tracks() or []):
                tr = ts.track if ts else None
                if not tr or not tr.id:
                    continue
                label = " - ".join(a.name for a in (tr.artists or [])) + " - " + tr.title
                items.append(("ymtrack:%s" % tr.id, label))
                if len(items) >= limit:
                    break
            return items
        except Exception as e:
            log("ym playlist err: %s" % e)
            return []

    def _playlist_worker(self, url):
        try:
            if self._is_ym_playlist_url(url):
                items = self._ym_playlist_items(url)
            else:
                items = self._yt_playlist_items(url)
            self.api_q.put(("playlist_done", url, items))
        except Exception as e:
            log("playlist worker err: %s" % e)
            self.api_q.put(("playlist_done", url, []))

    def _normalize_ym_url(self, url):
        """Map a direct Yandex.Music track URL (album/<a>/track/<id> or
        track/<id>) to its ymtrack:<id> key. yt-dlp cannot handle such URLs
        and crashes with a TypeError, so they must go through the YM API."""
        m = re.search(r"music\.yandex\.[^/\s]+/(?:album/[^/?#]+/)?track/(\d+)", url)
        if m:
            return "ymtrack:%s" % m.group(1)
        return url

    # Хосты, которые отдают обычные файлы: их потоком не считаем.
    _FILE_HOSTS = ("youtube.com", "youtu.be", "music.yandex", "rutube.ru",
                   "vk.com", "soundcloud.com", "yandex.ru", "sberzvuk", "zvuk.com")

    def _is_live_stream(self, url):
        """True, если ссылка — живой поток (радио, ICY), а не файл.

        Такой поток бесконечен: скачивать его нельзя, download_worker висит
        до таймаута (10 минут на попытку), поэтому его играем напрямую через
        ffmpeg — тем же путём, что и станции из radio/.
        """
        low = (url or "").lower()
        if not low.startswith(("http://", "https://")):
            return False
        host = urllib.parse.urlsplit(low).netloc
        if any(h in host for h in self._FILE_HOSTS):
            return False
        hdrs = {"User-Agent": "Mozilla/5.0", "Icy-MetaData": "1",
                "Accept": "*/*"}
        for method in ("HEAD", "GET"):
            try:
                req = urllib.request.Request(url, headers=hdrs, method=method)
                with urllib.request.urlopen(req, timeout=6) as r:
                    h = r.headers
                    if any(k.lower().startswith("icy-") for k in h.keys()):
                        return True
                    ctype = (h.get("Content-Type") or "").lower()
                    clen = h.get("Content-Length")
                    if low.endswith((".m3u8", ".pls")):
                        return True
                    if ctype.startswith(("audio/", "application/ogg",
                                         "application/vnd.apple.mpegurl")) and not clen:
                        return True
                    return False
            except Exception as e:
                log("stream probe %s fail %s: %s" % (method, url, e))
        return False

    # Страницы трансляций: разбираем и играем HLS, а не качаем файл.
    _KICK_RESERVED = ("video", "videos", "category", "categories", "following",
                      "browse", "search", "dashboard", "settings", "login",
                      "signup", "about", "help", "terms", "privacy", "clips",
                      "clip", "stream", "streams")

    def _platform_of(self, url):
        """'twitch', 'kick' или None по адресу страницы."""
        try:
            host = (urllib.parse.urlsplit(url or "").netloc or "").lower()
        except Exception:
            return None
        if "twitch.tv" in host or host.endswith("ttvnw.net"):
            return "twitch"
        if "kick.com" in host:
            return "kick"
        return None

    def _twitch_login(self, url):
        """twitch.tv/<канал>, m.twitch.tv/<канал>, player.twitch.tv/?channel=<канал>."""
        try:
            sp = urllib.parse.urlsplit(url)
        except Exception:
            return None
        qs = urllib.parse.parse_qs(sp.query or "")
        ch = (qs.get("channel") or qs.get("login") or [""])[0].strip()
        if ch:
            return ch.strip("/").lower()
        parts = [p for p in (sp.path or "").split("/") if p.strip()]
        if not parts:
            return None
        if parts[0].lower() in ("directory", "videos", "video", "search",
                                "downloads", "settings", "subscriptions"):
            return None
        return parts[0].lower()

    def _kick_slug(self, url):
        """kick.com/<канал> -> slug канала."""
        try:
            sp = urllib.parse.urlsplit(url)
        except Exception:
            return None
        parts = [p for p in (sp.path or "").split("/") if p.strip()]
        if not parts:
            return None
        slug = parts[0].lower()
        if slug in self._KICK_RESERVED:
            return None
        return slug

    def _http_json(self, url, headers=None, method="GET", body=None, timeout=10):
        """JSON по HTTP: сначала напрямую, при сбое — через прокси окружения."""
        hdrs = {"User-Agent": _STREAM_UA}
        hdrs.update(headers or {})
        tries = [None]
        for p in (os.environ.get("YT_PROXY"), os.environ.get("HTTPS_PROXY"),
                  os.environ.get("TG_PROXY")):
            if p and p not in tries:
                tries.append(p)
        last = None
        for proxy in tries:
            sess = None
            try:
                # trust_env=False обязателен: иначе requests сам подставит
                # HTTPS_PROXY из окружения, и «прямая» попытка уйдёт в xray.
                sess = requests.Session()
                sess.trust_env = False
                kw = {"timeout": timeout, "headers": hdrs}
                if proxy:
                    kw["proxies"] = {"http": proxy, "https": proxy}
                if method == "POST":
                    r = sess.post(url, json=body, **kw)
                else:
                    r = sess.get(url, **kw)
                if r.status_code >= 400:
                    last = Exception("HTTP %s" % r.status_code)
                    sess.close()
                    continue
                data = r.json()
                sess.close()
                return data
            except Exception as e:
                last = e
                if sess is not None:
                    try:
                        sess.close()
                    except Exception:
                        pass
        raise last if last else Exception("нет ответа")

    def _twitch_hls(self, login):
        """m3u8 живого канала Twitch через публичный GQL-запрос."""
        body = {"operationName": "PlaybackAccessToken",
                "variables": {"isLive": True, "login": login, "isVod": False,
                              "vodID": "", "playerType": "site"},
                "extensions": {"persistedQuery": {"version": 1,
                                                  "sha256Hash": _TWITCH_GQL_HASH}}}
        data = self._http_json(_TWITCH_GQL, method="POST", body=body,
                               headers={"Client-ID": _TWITCH_CLIENT_ID})
        tok = ((data or {}).get("data") or {}).get("streamPlaybackAccessToken")
        if not tok:
            return None
        q = urllib.parse.urlencode({"allow_source": "true",
                                    "allow_audio_only": "true",
                                    "player": "twitchweb", "p": "1234567",
                                    "type": "any", "sig": tok.get("signature"),
                                    "token": tok.get("value")})
        master = "https://usher.ttvnw.net/api/channel/hls/%s.m3u8?%s" % (login, q)
        # MEGADANCE-HLS-AUDIO-ONLY: звук вместо Full HD
        return self._hls_audio_only(master) or master

    def _kick_hls(self, slug):
        """(m3u8, заметка) живого канала Kick."""
        data = self._http_json("https://kick.com/api/v2/channels/%s" % slug,
                               headers={"Accept": "application/json"})
        if not data:
            return None, "канал не найден"
        if not data.get("livestream"):
            return None, "канал офлайн"
        url = (data.get("playback_url") or "").strip()
        if not url:
            return None, "нет ссылки на поток"
        # MEGADANCE-HLS-AUDIO-ONLY: звук вместо Full HD
        return (self._hls_audio_only(url) or url), "ok"

    # --- MEGADANCE-HLS-AUDIO-ONLY: играем звук, а не Full HD -------------
    def _http_text(self, url, headers=None, timeout=10):
        """Текст по HTTP: сначала напрямую, при сбое — через прокси окружения."""
        hdrs = {"User-Agent": _STREAM_UA}
        hdrs.update(headers or {})
        tries = [None]
        for p in (os.environ.get("YT_PROXY"), os.environ.get("HTTPS_PROXY"),
                  os.environ.get("TG_PROXY")):
            if p and p not in tries:
                tries.append(p)
        last = None
        for proxy in tries:
            sess = None
            try:
                sess = requests.Session()
                sess.trust_env = False
                kw = {"timeout": timeout, "headers": hdrs}
                if proxy:
                    kw["proxies"] = {"http": proxy, "https": proxy}
                r = sess.get(url, **kw)
                if r.status_code >= 400:
                    last = Exception("HTTP %s" % r.status_code)
                    sess.close()
                    continue
                text = r.text
                sess.close()
                return text
            except Exception as e:
                last = e
                if sess is not None:
                    try:
                        sess.close()
                    except Exception:
                        pass
        raise last if last else Exception("нет ответа")

    @staticmethod
    def _parse_master_playlist(text):
        """Мастер-плейлист -> (список вариантов, ссылка на аудио-дорожку)."""
        variants = []
        audio_uri = ""
        pending = None
        for raw in (text or "").splitlines():
            line = raw.strip()
            if not line:
                continue
            if line.startswith("#EXT-X-STREAM-INF:"):
                attrs = line.split(":", 1)[1]
                bw = 0
                m = re.search(r"BANDWIDTH=(\d+)", attrs)
                if m:
                    bw = int(m.group(1))
                pending = {"bandwidth": bw,
                           "audio_only": "audio_only" in attrs.lower()}
                continue
            if line.startswith("#EXT-X-MEDIA:"):
                attrs = line.split(":", 1)[1]
                if "TYPE=AUDIO" in attrs.replace(" ", "").upper():
                    m = re.search(r'URI="([^"]+)"', attrs)
                    if m and not audio_uri:
                        audio_uri = m.group(1)
                continue
            if line.startswith("#"):
                continue
            if pending is not None:
                pending["uri"] = line
                variants.append(pending)
                pending = None
        return variants, audio_uri

    def _hls_audio_only(self, master_url):
        """Мастер-плейлист -> адрес звуковой дорожки.

        Twitch и Kick кладут в мастер и Full HD 60 к/с, и отдельный
        audio_only. Если отдать мастер целиком, ffmpeg берёт первую
        программу и тащит 8-12 Мбит/с видео ради звука — на слабом канале
        это даёт затыки на стыках сегментов. Не разобрали — None, тогда
        играем как раньше.
        """
        try:
            text = self._http_text(master_url, timeout=8)
        except Exception as e:
            log("hls звук: плейлист не прочитан (%s)" % str(e)[:80])
            return None
        if "#EXT-X-STREAM-INF" not in (text or ""):
            return None  # это уже не мастер, а обычный плейлист
        variants, audio_uri = self._parse_master_playlist(text)
        if not variants and not audio_uri:
            log("hls звук: в мастере нет вариантов")
            return None
        target = audio_uri
        if not target:
            pure = [v for v in variants if v.get("audio_only")]
            pool = pure or variants
            target = min(pool, key=lambda v: v.get("bandwidth") or 0)["uri"]
        if not target:
            return None
        url = urllib.parse.urljoin(master_url, target)
        log("hls звук: выбрана дорожка %s" % url.split("?")[0][-70:])
        return url

    def _resolve_live_platform(self, url):
        """Ссылка на трансляцию -> (m3u8, подпись) либо (None, причина)."""
        kind = self._platform_of(url)
        if kind == "twitch":
            login = self._twitch_login(url)
            if not login:
                return None, "не понял, какой канал включить"
            try:
                hls = self._twitch_hls(login)
            except Exception as e:
                return None, "Twitch не ответил: %s" % str(e)[:80]
            if not hls:
                return None, "канал %s сейчас не в эфире" % login
            return hls, "Twitch · %s" % login
        if kind == "kick":
            slug = self._kick_slug(url)
            if not slug:
                return None, "не понял, какой канал включить"
            try:
                hls, note = self._kick_hls(slug)
            except Exception as e:
                return None, "Kick не ответил: %s" % str(e)[:80]
            if not hls:
                return None, "%s — %s" % (slug, note)
            return hls, "Kick · %s" % slug
        return None, "не трансляция"

    def _handle_url(self, url, label):
        """Play a direct link; a YouTube/Yandex playlist queues all its tracks."""
        url = self._normalize_ym_url(url)
        if "rutube.ru" in url:
            # RuTube: не качаем файл, играем поток сразу
            threading.Thread(target=self._play_rutube_stream,
                             args=(url, label), daemon=True).start()
            return
        if self._platform_of(url):
            kind = "Twitch" if self._platform_of(url) == "twitch" else "Kick"
            self._send("Смотрю трансляцию %s…" % kind)
            hls, note = self._resolve_live_platform(url)
            if not hls:
                log("live %s: отказ — %s" % (kind, note))
                self._send("Не получилось: %s" % note)
                return
            log("live %s: %s" % (kind, note))
            self._play_radio(hls, note)
            # помним страницу, а не ссылку с токеном: после перезапуска
            # её надо получать заново
            self._remember_last(url, note, True)
            return
        if self._is_yt_live_url(url):
            self._send("Открываю трансляцию YouTube…")
            threading.Thread(target=self._play_yt_stream,
                             args=(url, label), daemon=True).start()
            return
        if self._is_live_stream(url):
            self._play_radio(url, label)
            return
        if self._is_yt_playlist_url(url) or self._is_ym_playlist_url(url):
            self._send("Плейлист: сбор треков…")
            threading.Thread(target=self._playlist_worker, args=(url,), daemon=True).start()
            return
        self._switch_to(url, label)

    def _is_yt_live_url(self, url):
        """YouTube-трансляция: её нельзя скачивать, только играть потоком."""
        low = (url or "").lower()
        return ("youtube.com/live/" in low) or ("youtube.com/watch" in low and "live=1" in low)

    def _play_yt_stream(self, url, label):
        """YouTube live без скачивания: прямой адрес потока -> эфир."""
        ck = None
        try:
            log("yt live: получаю прямой адрес для %s" % label)
            cmd = list(YTDLP) + [
                "--no-playlist", "-g",
                "-f", "ba/b",
                "--impersonate", "chrome",
                "--socket-timeout", "20", "--extractor-retries", "8",
                "--remote-components", "ejs:github",
            ]
            if YT_JS_RUNTIME:
                cmd += ["--js-runtimes", "deno:%s" % YT_JS_RUNTIME]
            if YT_PO_TOKEN:
                cmd += ["--extractor-args", YT_PO_TOKEN]
            _proxy = _yt_proxy()
            if _proxy:
                cmd += ["--proxy", _proxy]
            ck = _fresh_cookies(COOKIES)
            if ck:
                cmd += ["--cookies", ck]
            cmd += ["--", url]
            rc, out, err = self._run_ydl(cmd, timeout=150)
            lines = [l.strip() for l in (out or "").splitlines() if l.strip().startswith("http")]
            if rc != 0 or not lines:
                log("yt live: адрес не получен (%s), качаю как раньше" % str(err or "")[:150])
                self._download_worker(url, label)
                return
            stream = lines[-1]
            log("yt live: адрес получен (%d симв.), играю напрямую" % len(stream))
            self.stream_http_proxy = os.environ.get("TT_STREAM_HTTP_PROXY") or "http://127.0.0.1:8118"
            self._play_radio(stream, label)
        except Exception as e:
            log("yt live err: %s" % str(e)[:150])
        finally:
            if ck:
                _drop_cookies(ck)

    def _play_rutube_stream(self, url, label):
        """RuTube без скачивания: прямой адрес потока -> эфир."""
        try:
            cmd = list(YTDLP) + [
                "--no-playlist", "-g",
                "-f", "ba/b[height<=144]/b[height<=144]/ba/b",
                "--impersonate", "chrome",
                "--socket-timeout", "20", "--extractor-retries", "8",
                "--", url,
            ]
            rc, out, err = self._run_ydl(cmd, timeout=180)
            lines = [l.strip() for l in (out or "").splitlines() if l.strip().startswith("http")]
            if rc != 0 or not lines:
                log("rutube stream: не получил адрес, качаю как раньше")
                self._download_worker(url, label)
                return
            stream = lines[-1]
            log("rutube stream: адрес получен (%d симв.), играю напрямую" % len(stream))
            if ".m3u8" in stream or "m3u8" in stream:
                self._play_radio(stream, label)
            else:
                self._play_file(stream, label, stream)
        except Exception as e:
            log("rutube stream err: %s" % str(e)[:150])
            try:
                self._download_worker(url, label)
            except Exception:
                pass

    def _download_worker(self, url, title):
        real_url = url
        if url.startswith("ytsearch1:"):
            q = url.split(":", 1)[1]
            if not self.silent:
                self.api_q.put(("status", "Поиск на YouTube: %s…" % q))
            items = self._yt_search_list(q)
            if not items:
                self.api_q.put(("download_fail", url, title, "Видео по запросу не найдено"))
                return
            real_url = items[0]["key"]
        ym_title = None
        if url.startswith("ymtrack:"):
            tid = url.split(":", 1)[1]
            if not self.silent:
                self.api_q.put(("status", "Поиск трека на Яндекс.Музыке…"))
            real_url, ym_title = self._ym_resolve(tid)
            if not real_url:
                self.api_q.put(("download_fail", url, title, ym_title or "трек не найден"))
                return
            title = ym_title or title
        elif url.startswith("ymsearch1:"):
            q = url.split(":", 1)[1]
            if not self.silent:
                self.api_q.put(("status", "Поиск на Яндекс.Музыке: %s…" % q))
            items = self._ym_search_list(q)
            if not items:
                self.api_q.put(("download_fail", url, title, "На Яндекс.Музыке ничего не найдено."))
                return
            real_url, ym_title = self._ym_resolve(items[0]["key"].split(":", 1)[1])
            if not real_url:
                self.api_q.put(("download_fail", url, title, ym_title or "трек не найден"))
                return
            title = ym_title or title
        canon_done = False
        alt_tried = False  # запасные клиенты YouTube пробуем один раз на цикл
        for attempt in range(1, 4):
            try:
                out = os.path.join(CACHE_DIR, uuid.uuid4().hex)
                if ym_title:
                    try:
                        mp3 = out + ".mp3"
                        req = urllib.request.Request(real_url, headers={"User-Agent": "yandex-music/3.0.0"})
                        with urllib.request.urlopen(req, timeout=180) as r, open(mp3, "wb") as f:
                            shutil.copyfileobj(r, f)
                        if not os.path.exists(mp3) or os.path.getsize(mp3) < 1024:
                            raise Exception("пустой файл")
                        self.api_q.put(("download_ok", url, ym_title, mp3))
                        return
                    except Exception as e:
                        self.api_q.put(("download_fail", url, title, "Не удалось скачать с Яндекс.Музыки: %s" % str(e)[:150]))
                        return
                cmd = list(YTDLP) + [
                    "--no-playlist", "--extractor-retries", "3", "--user-agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
                    "--impersonate", "chrome", "--no-simulate",
                    # Без mp3-транскода: он на этом сервере жрёт ~16с на трек.
                    # Качаем лучший аудио-поток как есть (обычно opus/webm или m4a)
                    # и играем его через ffmpeg — в голосовом канале контейнер не важен.
                    # Rutube — исключение: audio-only потоков у него нет вовсе, все
                    # 24 формата содержат видео, поэтому -f ba/b уходил в "best" и
                    # тянул полное видео (5.7 ГБ на двухчасовом ролике, из них бот
                    # играет только звук — ffmpeg с -vn). Поэтому для Rutube берём
                    # самое низкое разрешение: аудио там то же, файл в десятки раз
                    # меньше. Проверено 2026-10-02: 144p = 175 МБ за 261 с.
                    "-f", ("ba/b[height<=144]/b[height<=144]/ba/b"
                           if "rutube.ru" in real_url else "ba/b"),
                    "--remote-components", "ejs:github",
                    # Узлы балансера очень разные по скорости: через один ролик идёт
                    # на мегабайтах в секунду, через другой ползёт на десятках килобайт
                    # (проверено 2026-09-12: 5.5 МБ/с против ~40 КБ/с на одном и том же
                    # видео). Если поток просел ниже порога — yt-dlp переизвлекает ссылку,
                    # а новое соединение балансер ведёт уже через другой узел.
                    "--throttled-rate", "250K",
                    "-o", out + ".%(ext)s",
                    "--print", "%(title)s",
                ]
                if YT_JS_RUNTIME:
                    cmd += ["--js-runtimes", "deno:%s" % YT_JS_RUNTIME]
                if YT_PO_TOKEN:
                    cmd += ["--extractor-args", YT_PO_TOKEN]
                if "rutube.ru" in real_url:
                    # RuTube работает только напрямую: через туннель 1080 и мост
                    # 8118 Qrator рвёт TLS (Recv failure, code 35). Повторы и
                    # паузы оставляем, прокси не ставим вовсе.
                    cmd += ["--retries", "10", "--fragment-retries", "10",
                            "--retry-sleep", "linear=1:10"]
                else:
                    _proxy = _yt_proxy()
                    if _proxy:
                        cmd += ["--proxy", _proxy]
                ck = _fresh_cookies(RUTUBE_COOKIES if "rutube.ru" in real_url else COOKIES)
                if ck:
                    cmd += ["--cookies", ck]
                cmd += ["--", real_url]
                try:
                    rc, stdout, stderr = self._run_ydl(
                        cmd, timeout=1500 if "rutube.ru" in real_url else 600)
                finally:
                    _drop_cookies(ck)
                audio = _find_downloaded(out)
                if rc != 0 or not audio:
                    err_text = (stderr or stdout or "yt-dlp failed").strip()
                    is_yt = "youtube.com" in real_url or "youtu.be" in real_url
                    if ("Video unavailable" in err_text and not canon_done
                            and ("youtube.com/watch?v=" in real_url or "youtu.be/" in real_url)):
                        new_url = self._canonical_yt_url(real_url)
                        if new_url != real_url:
                            log("yt id case fix: %s -> %s" % (real_url, new_url))
                            real_url = new_url
                            canon_done = True
                            continue
                    # основной клиент не взял — пробуем запасные клиенты YouTube
                    if is_yt and not alt_tried:
                        alt_tried = True
                        arc, aout, aerr = self._yt_alt_download(out, real_url)
                        if arc == 0:
                            rc, stdout, stderr = 0, aout, aerr
                            audio = _find_downloaded(out)
                        elif aerr:
                            err_text = aerr
                    # rc=0 без файла (молчаливый отказ YouTube) — это тоже ошибка
                    if rc != 0 or not audio:
                        if "Sign in to confirm" in err_text or "LOGIN_REQUIRED" in err_text:
                            # Проверено 2026-09-12: тот же ролик проходит со второй-третьей
                            # попытки — балансер каждый раз выходит через другой узел, и часть
                            # узлов YouTube отдаёт без входа. Поэтому не сдаёмся с первого раза.
                            if attempt < 3:
                                self.api_q.put(("status", "YouTube просит вход. Пробую другой узел — повтор %d из 3." % attempt))
                                time.sleep(5)
                                continue
                            self.api_q.put(("download_fail", url, title, "YouTube это видео с сервера не отдаёт — просит войти в аккаунт. Три узла подряд отказали, скорее всего ролик возрастной или закрытый. Попробуй другой."))
                            return
                        err_msg = (err_text.splitlines()[-1] if err_text else "yt-dlp failed")[:300]
                        if attempt < 3:
                            self.api_q.put(("status", "%s. Повтор %d из 3 через 15 с." % (err_msg, attempt)))
                            time.sleep(15)
                            continue
                        self.api_q.put(("download_fail", url, title, err_msg))
                        return
                got_title = (stdout or "").strip().splitlines()
                real_title = got_title[0] if got_title else title
                self.api_q.put(("download_ok", url, real_title, audio))
                return
            except subprocess.TimeoutExpired:
                if attempt < 3:
                    self.api_q.put(("status", "Таймаут скачивания. Повтор %d из 3 через 15 с." % attempt))
                    time.sleep(15)
                    continue
                self.api_q.put(("download_fail", url, title, "timeout"))
                return
            except Exception as e:
                if attempt < 3:
                    self.api_q.put(("status", "Ошибка: %s. Повтор %d из 3 через 15 с." % (str(e)[:120], attempt)))
                    time.sleep(15)
                    continue
                self.api_q.put(("download_fail", url, title, str(e)[:300]))
                return

    def _set_status(self, text):
        """Set the bot's TeamTalk status line (visible in the client)."""
        try:
            self.doChangeStatus(0, text)
        except Exception as e:
            log("status err: %s" % e)

    def _start_voice(self, path, offset_ms=0):
        self._stop_voice()
        run = self._voice_run  # поколение уже увеличено в _stop_voice
        self.voice_stop.clear()
        self.voice_offset_base = int(offset_ms)
        self.voice_started_at = time.time()
        t = threading.Thread(
            target=self._voice_worker, args=(path, int(offset_ms), run), daemon=True
        )
        self.voice_thread = t
        t.start()

    def _voice_worker(self, path, offset_ms, run):
        """Play audio to TeamTalk as voice.

        With PULSE_SINK set, ffmpeg plays the track into that PulseAudio sink
        (real-time via -re) and parec captures the sink's monitor back, so the
        bot relays whatever is audible on the machine. Otherwise ffmpeg decodes
        the file straight to raw PCM — no sound devices involved.
        """
        pulse = PULSE_SINK
        cap = None  # parec process (pulse mode only)
        try:
            spp = getattr(self, "stream_http_proxy", None) if str(path).startswith(("http://", "https://")) else None
            if pulse:
                cmd = ["ffmpeg", "-y"]
                if offset_ms > 0:
                    cmd += ["-ss", "%.3f" % (offset_ms / 1000.0)]
                if spp:
                    cmd += ["-http_proxy", spp, "-rw_timeout", "15000000"]
                else:
                    cmd += ["-re"]
                cmd += ["-i", path, "-vn", "-f", "pulse", pulse]
                proc = subprocess.Popen(
                    cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True, env=_media_env(),
                )
                cap = subprocess.Popen(
                    ["parec", "--device=%s.monitor" % pulse, "--format=s16le",
                     "--rate", str(VOICE_RATE), "--channels=%d" % VOICE_CHANNELS],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    start_new_session=True, env=_media_env(),
                )
                src = cap.stdout
            else:
                cmd = ["ffmpeg", "-y"]
                if offset_ms > 0:
                    cmd += ["-ss", "%.3f" % (offset_ms / 1000.0)]
                if spp:
                    cmd += ["-http_proxy", spp, "-rw_timeout", "15000000"]
                cmd += ["-i", path, "-vn", "-f", "s16le", "-ac", str(VOICE_CHANNELS),
                        "-ar", str(VOICE_RATE), "-"]
                proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    start_new_session=True, env=_media_env(),
                )
                src = proc.stdout
        except Exception as e:
            self.api_q.put(("voice_error", "ffmpeg: %s" % str(e)[:120]))
            return
        self.voice_proc = proc
        stream_id = int(time.time() * 1000) & 0xFFFF
        finished = False
        buf = b""
        sent_blocks = 0
        block_dur = VOICE_CHUNK / float(VOICE_RATE)  # 0.02 s per 20 ms block
        # В режиме PulseAudio звук и так идёт в темпе устройства — запас
        # нужен минимальный, иначе после трека копится хвост.
        read_ahead = VOICE_CHUNK_BYTES * 4 if pulse else VOICE_READ_AHEAD
        next_slot = time.monotonic()
        drain_until = None  # pulse mode: tail-drain deadline after player exits
        try:
            while not self.voice_stop.is_set():
                if pulse and proc.poll() is not None and drain_until is None:
                    drain_until = time.monotonic() + 0.4
                # Держим запас впереди собственного темпа: ffmpeg пишет в
                # pipe (64 КБ = 0,34 с), и на стыке HLS-сегментов этот запас
                # кончается — звук рвётся. Вычитываем всё готовое до
                # read_ahead, чтобы переждать сетевую заминку.
                while len(buf) < read_ahead and not self.voice_stop.is_set():
                    wait = 0.1 if len(buf) < VOICE_CHUNK_BYTES else 0
                    r, _, _ = select.select([src], [], [], wait)
                    if not r:
                        break
                    try:
                        data = os.read(src.fileno(), VOICE_READ_CHUNK)
                    except OSError:
                        data = b""
                    if not data:
                        finished = True
                        break
                    buf += data
                if finished:
                    break
                if pulse and drain_until is not None and time.monotonic() >= drain_until:
                    finished = True
                    break
                if len(buf) < VOICE_CHUNK_BYTES:
                    continue  # partial/empty read: wait for a full block
                # Feed blocks on a strict 20 ms schedule. A fixed sleep after
                # each insert drifts (read + insert take time) and stutters;
                # sleeping to the exact next slot keeps the stream smooth.
                next_slot += block_dur
                delay = next_slot - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                else:
                    next_slot = time.monotonic()  # behind: don't compound backlog
                chunk = buf[:VOICE_CHUNK_BYTES]
                buf = buf[VOICE_CHUNK_BYTES:]
                chunk = self._scale_pcm(chunk)
                raw = (ctypes.c_char * VOICE_CHUNK_BYTES).from_buffer_copy(chunk)
                ab = TeamTalk5.AudioBlock()
                ab.nStreamID = stream_id
                ab.nSampleRate = VOICE_RATE
                ab.nChannels = VOICE_CHANNELS
                ab.lpRawAudio = ctypes.cast(raw, ctypes.c_void_p)
                ab.nSamples = VOICE_CHUNK
                ab.uStreamTypes = StreamType.STREAMTYPE_VOICE
                try:
                    ok = self.insertAudioBlock(ab)
                    sent_blocks += 1
                    if sent_blocks % 100 == 0:
                        log("voice blocks=%d ret=%s" % (sent_blocks, ok))
                except Exception as e:
                    log("insertAudioBlock err: %s" % e)
        except Exception as e:
            log("voice worker err: %s" % e)
        finally:
            if buf and not self.voice_stop.is_set():
                pad = VOICE_CHUNK_BYTES - (len(buf) % VOICE_CHUNK_BYTES)
                if pad and pad != VOICE_CHUNK_BYTES:
                    buf += b"\x00" * pad
                # Хвост доигрываем в темпе эфира, а не залпом: иначе
                # накопленный запас вылетел бы в канал ускоренным.
                tail_slot = time.monotonic()
                for i in range(0, len(buf), VOICE_CHUNK_BYTES):
                    chunk = buf[i:i + VOICE_CHUNK_BYTES]
                    if len(chunk) < VOICE_CHUNK_BYTES:
                        chunk += b"\x00" * (VOICE_CHUNK_BYTES - len(chunk))
                    chunk = self._scale_pcm(chunk)
                    raw = (ctypes.c_char * VOICE_CHUNK_BYTES).from_buffer_copy(chunk)
                    ab = TeamTalk5.AudioBlock()
                    ab.nStreamID = stream_id
                    ab.nSampleRate = VOICE_RATE
                    ab.nChannels = VOICE_CHANNELS
                    ab.lpRawAudio = ctypes.cast(raw, ctypes.c_void_p)
                    ab.nSamples = VOICE_CHUNK
                    ab.uStreamTypes = StreamType.STREAMTYPE_VOICE
                    try:
                        self.insertAudioBlock(ab)
                    except Exception as e:
                        log("insertAudioBlock err: %s" % e)
                    tail_slot += block_dur
                    tail_delay = tail_slot - time.monotonic()
                    if tail_delay > 0:
                        time.sleep(tail_delay)
            try:
                self.insertAudioBlockEnd()
            except Exception:
                pass
            for p in (proc, cap):
                try:
                    if p is not None and p.poll() is None:
                        p.kill()
                except Exception:
                    pass
            log("voice done: blocks=%d finished=%s" % (sent_blocks, finished))
            self.voice_proc = None
            if finished:
                self.api_q.put(("voice_finished", run, path))

    def _stop_voice(self):
        self._voice_run += 1  # инвалидировать поколение: устаревшие voice_finished игнорируются
        self.voice_stop.set()
        t = self.voice_thread
        self.voice_thread = None
        if t and t.is_alive():
            t.join(timeout=1.5)
        try:
            self.insertAudioBlockEnd()
        except Exception:
            pass

    def _play_file(self, url, title, path):
        self.current_orig = path
        self.current_file = path
        self.current = (url, title)
        self.cur_source = (url, title, False)
        self._remember_last(url, title, False)
        self.playing = True
        self.paused = False
        self.cur_offset_ms = 0
        if not self.silent:
            self._notify("Сейчас играет: %s" % title)
        self._set_status("Playing: %s" % title)
        self._start_voice(path, 0)
        self._music_broadcast(path, title)  # раздать трек подписчикам музыки (в фоне)

    def _file_to_pcm(self, path):
        """Файл (mp3/ogg/m4a) -> s16le стерео 48 кГц для подмешивания в эфир."""
        try:
            cmd = ["ffmpeg", "-v", "error", "-i", str(path), "-vn",
                   "-f", "s16le", "-acodec", "pcm_s16le",
                   "-ar", str(VOICE_RATE), "-ac", str(VOICE_CHANNELS), "-"]
            p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               timeout=300)
            data = p.stdout or b""
            frame = VOICE_CHANNELS * 2
            if len(data) % frame:
                data = data[:len(data) - (len(data) % frame)]
            if not data:
                err = (p.stderr or b"")[:200].decode("utf-8", "replace")
                log("voice_file: пустое аудио (%s)" % err)
                return None
            return data
        except Exception as e:
            log("voice_file: decode err: %s" % str(e)[:150])
            return None

    def _play_local(self, path, title):
        """Play a local file (no download) — used for files sent via Telegram."""
        self.auto_list = False
        self.auto_playlist = False
        self.queue.clear()
        self.downloading.clear()
        self._stop_voice()
        self.playing = False
        self.paused = False
        self.current = None
        self.current_orig = None
        self.cur_source = None
        self.cur_offset_ms = 0
        self._play_file(path, title, path)

    def _elapsed_ms(self):
        if self.playing and not self.paused and self.voice_started_at:
            return self.voice_offset_base + int(
                (time.time() - self.voice_started_at) * 1000
            )
        return self.cur_offset_ms

    def _pause(self):
        if not self.playing or self.paused:
            return
        self.cur_offset_ms = self._elapsed_ms()
        self.paused = True
        self._stop_voice()
        self._set_status("Paused: %s" % (self.current[1] if self.current else ""))
        self._send("Пауза (%s)" % _fmt_ms(self.cur_offset_ms))

    def _resume(self):
        if not self.playing or not self.paused or not self.current_orig:
            return
        self.paused = False
        self._set_status("Playing: %s" % (self.current[1] if self.current else ""))
        self._start_voice(self.current_orig, self.cur_offset_ms)
        self._send("Воспроизведение продолжено (%s)" % _fmt_ms(self.cur_offset_ms))

    def _save_all(self):
        """Полное сохранение всех настроек: config.json плюс файлы состояния.

        Пишет всё, что бот держит в памяти: ник, сервис, громкость, озвучку
        приветствия и голос, сообщения в канал, озвучку названий, статус.
        После перезапуска бот поднимается с этим же состоянием.
        """
        _save_config({
            "server": {"nickname": self.nickname},
            "runtime": {"main_service": self.service},
            "playback": {"default_volume": int(self.volume)},
            "welcome": {
                "voice_enabled": bool(WELCOME_VOICE_ENABLED),
                "voice_speaker": WELCOME_VOICE_SPEAKER,
            },
        })
        try:
            with open(STATUS_MSG_FILE, "w") as f:
                f.write(self.status_msg or "")
        except Exception as e:
            log("status save err: %s" % e)
        self._save_channel_msg()
        return {
            "nick": self.nickname,
            "service": self.service,
            "volume": int(self.volume),
            "tts": bool(WELCOME_VOICE_ENABLED),
            "speaker": WELCOME_VOICE_SPEAKER,
            "channel_msg": bool(self.channel_msg),
            "voice_announce": False,  # озвучка названий вырезана в lite
            "status": self.status_msg or "",
        }

    def _save_runtime(self):
        """Автосохранение настроек команд: источник и громкость.

        Вызывается сразу после смены значения, чтобы после перезапуска
        бот поднялся с теми же настройками, без ручного /sc.
        """
        try:
            _save_config({
                "runtime": {"main_service": self.service},
                "playback": {"default_volume": int(self.volume)},
            })
        except Exception as e:
            log("runtime save err: %s" % str(e)[:120])

    def _set_volume(self, v):
        v = max(1, min(MAX_VOLUME, v))
        self.volume = v
        self._save_runtime()
        # No ffmpeg restart needed: _scale_pcm applies the new gain to the next
        # audio block, so volume changes take effect instantly for any source.
        self._send("Громкость: %d%%" % v)

    def _seek(self, delta_s):
        """Seek the current track by delta_s seconds (negative = backwards)."""
        if not self.playing or not self.current_orig:
            self._send("Воспроизведение неактивно.")
            return
        cur = self.cur_offset_ms if self.paused else self._elapsed_ms()
        new_ms = max(0, cur + delta_s * 1000)
        self.cur_offset_ms = new_ms
        direction = "назад" if delta_s < 0 else "вперёд"
        msg = "Перемотка %s на %d с. Позиция: %s" % (direction, abs(delta_s), _fmt_ms(new_ms))
        if self.paused:
            self._set_status("Paused: %s" % (self.current[1] if self.current else ""))
            self._send(msg)
        else:
            self._set_status("Playing: %s" % (self.current[1] if self.current else ""))
            self._start_voice(self.current_orig, new_ms)
            self._send(msg)

    def _switch_to(self, key, label):
        """Stop whatever plays and immediately play `key` (used by n/b and direct links)."""
        key = self._normalize_ym_url(key)
        self.auto_list = False
        self.auto_playlist = False
        self.queue.clear()
        self._stop_voice()
        self.playing = False
        self.paused = False
        self.current = None
        self.current_orig = None
        self.cur_source = None
        self.cur_offset_ms = 0
        self._set_status(self.status_msg)
        self._enqueue_url(key, label)

    def _play_search_index(self, idx, silent=False):
        self.silent = silent
        if not self.search_results:
            self._send("Список результатов пуст. Сначала выполните поиск: /play <запрос>")
            return
        if idx < 0 or idx >= len(self.search_results):
            self._send("Нет результата под номером %d (всего %d)." % (idx + 1, len(self.search_results)))
            return
        self.search_index = idx
        item = self.search_results[idx]
        self._switch_to(item["key"], "%d. %s" % (idx + 1, item["title"]))
        self.auto_list = True

    def _play_playlist_index(self, idx, silent=False):
        """Play track `idx` of the current playlist (YouTube / Yandex Music)."""
        self.silent = silent
        if not self.playlist:
            self._send("Список плейлиста пуст. Вставьте ссылку на плейлист.")
            return
        if idx < 0 or idx >= len(self.playlist):
            self._send("Нет трека под номером %d (всего %d)." % (idx + 1, len(self.playlist)))
            return
        self.playlist_index = idx
        u, t = self.playlist[idx]
        self._switch_to(u, "%d. %s" % (idx + 1, t))
        self.auto_playlist = True

    def _playlist_page_lines(self, page=1, per_page=20):
        """Numbered page of the loaded playlist (whole list fits via pages)."""
        if not self.playlist:
            return ["Список плейлиста пуст. Вставьте ссылку на плейлист."]
        total = len(self.playlist)
        pages = max(1, (total + per_page - 1) // per_page)
        page = max(1, min(pages, page))
        start = (page - 1) * per_page
        end = min(total, start + per_page)
        lines = ["Плейлист: %d треков (стр. %d из %d)" % (total, page, pages)]
        for i in range(start, end):
            _u, t = self.playlist[i]
            lines.append("%d. %s" % (i + 1, t[:45]))
        lines.append("playlist <страница> — листать, n/b — следующий/предыдущий трек")
        return lines

    def _do_search(self, query):
        self._send("Поиск: %s…" % query)
        threading.Thread(target=self._search_worker, args=(query,), daemon=True).start()

    def _search_worker(self, query):
        try:
            if self.service == "ym":
                items = self._ym_search_list(query)
            else:
                items = self._yt_search_list(query)
            self.api_q.put(("search_done", query, items))
        except Exception as e:
            log("search err: %s" % e)
            self.api_q.put(("search_done", query, []))

    def _advance(self, silent=False):
        self.silent = silent
        self._stop_voice()
        self.playing = False
        self.paused = False
        self.current = None
        self.current_orig = None
        self.cur_source = None
        self.cur_offset_ms = 0
        self.voice_offset_base = 0
        self.voice_started_at = 0
        self._set_status(self.status_msg)
        if self.auto_playlist and self.playlist and self.playlist_index + 1 < len(self.playlist):
            # auto-advance through the playlist
            self._play_playlist_index(self.playlist_index + 1, silent=True)
            return
        if self.queue:
            self.queue.pop(0)
        if self.queue:
            self._enqueue_next()
        elif self.auto_list and self.search_results and self.search_index + 1 < len(self.search_results):
            # auto-advance: keep playing the rest of the search-result list
            self.search_index += 1
            self._play_search_index(self.search_index, silent=True)
        else:
            ended = self.auto_list or self.auto_playlist
            self.auto_list = False
            self.auto_playlist = False
            self._notify("Конец списка." if ended else "Очередь пуста.")

    # ----- favorites (избранное) -----
    def _load_favorites(self):
        """Load favorites from favorites.json; survives restarts."""
        try:
            with open(FAVORITES_FILE, encoding="utf-8") as f:
                data = json.load(f)
            return [i for i in data.get("items", []) if i.get("key")]
        except Exception:
            return []

    def _save_favorites(self):
        try:
            with open(FAVORITES_FILE, "w", encoding="utf-8") as f:
                json.dump({"items": self.favorites}, f, ensure_ascii=False, indent=2)
        except Exception as e:
            log("fav save err: %s" % e)

    def _fav_cmd(self, arg):
        """Favorites: f — list, f + — add current track, f + <url> — add a link, f <n> — play."""
        if not arg:
            if not self.favorites:
                self._send("Избранное пусто. Добавить: f + (текущий трек) или f + <ссылка>.")
                return
            lines = ["Избранное:"]
            for i, it in enumerate(self.favorites, 1):
                lines.append("%d. %s" % (i, it["label"]))
            lines.append("f <номер> — играть, f + <ссылка> — добавить, f - <номер> — удалить")
            self._send("\n".join(lines))
            return
        if arg == "+":
            if not self.cur_source:
                self._send("Сейчас ничего не играет. Добавить ссылку: f + <ссылка>.")
                return
            key, label, is_radio = self.cur_source
            if any(it["key"] == key for it in self.favorites):
                self._send("Уже в избранном: %s" % label)
                return
            self.favorites.append({"key": key, "label": label, "radio": is_radio})
            self._save_favorites()
            self._send("Добавлено в избранное: %s" % label)
            return
        if arg.startswith("+ "):
            u = URL_RE.search(arg[2:])
            if not u:
                self._send("Формат: f + https://…")
                return
            key = self._normalize_ym_url(u.group(0))
            if any(it["key"] == key for it in self.favorites):
                self._send("Уже в избранном: %s" % key)
                return
            self.favorites.append({"key": key, "label": key, "radio": False})
            self._save_favorites()
            self._send("Добавлено в избранное: %s" % key)
            return
        if arg == "-" or arg.startswith("- "):
            num = arg[1:].strip()
            if not num.isdigit():
                self._send("Формат удаления: f - <номер>, например f - 1.")
                return
            idx = int(num) - 1
            if idx < 0 or idx >= len(self.favorites):
                self._send("Нет записи под номером %d (всего %d)." % (idx + 1, len(self.favorites)))
                return
            it = self.favorites.pop(idx)
            self._save_favorites()
            self._send("Удалено из избранного: %s" % it["label"])
            return
        if arg.isdigit():
            idx = int(arg) - 1
            if idx < 0 or idx >= len(self.favorites):
                self._send("Нет записи под номером %d (всего %d)." % (idx + 1, len(self.favorites)))
                return
            it = self.favorites[idx]
            if it.get("radio"):
                self._play_radio(it["key"], it["label"])
            else:
                self._switch_to(it["key"], it["label"])
            return
        self._send("Команды: f — список, f + — добавить текущий, f + <ссылка> — добавить, f <номер> — играть, f - <номер> — удалить")

    # ----- radio stations (m3u playlists) -----
    def _load_radio(self):
        """Scan BASE_DIR/radio for .m3u files, flat list of stations."""
        stations = []
        base = os.path.join(BASE_DIR, "radio")
        if not os.path.isdir(base):
            return stations
        for fn in sorted(os.listdir(base)):
            if not fn.lower().endswith(".m3u"):
                continue
            full = os.path.join(base, fn)
            title, url = self._parse_m3u(full, fn)
            if url:
                stations.append((title, url))
        return stations

    def _parse_m3u(self, path, fallback_name):
        """Return (title, stream_url) from an m3u file, or (fallback, None)."""
        title = os.path.splitext(fallback_name)[0]
        url = None
        try:
            with open(path, encoding="utf-8", errors="ignore") as f:
                for ln in f:
                    ln = ln.strip()
                    if not ln:
                        continue
                    if ln.upper().startswith("#EXTINF"):
                        m = re.search(r",\s*(.+)\s*$", ln)
                        if m and m.group(1).strip():
                            title = m.group(1).strip()
                    elif ln.startswith("http://") or ln.startswith("https://"):
                        url = ln
                        break
        except Exception as e:
            log("m3u parse err %s: %s" % (path, e))
        return title, url

    def _radio_cmd(self, arg):
        """Radio station browser: плоский список → запуск по номеру."""
        if not self.radio:
            self._send("Нет папки radio/ с m3u. Добавьте станции и перезапустите бота.")
            return
        if not arg:
            lines = ["Радио (%d станций):" % len(self.radio)]
            for i, (t, _u) in enumerate(self.radio, 1):
                lines.append("%d. %s" % (i, t))
                if i >= 15:
                    lines.append("… и ещё %d" % (len(self.radio) - 15))
                    break
            lines.append("radio <номер> — запуск")
            self._send("\n".join(lines))
            return
        if arg.isdigit():
            idx = int(arg) - 1
            if idx < 0 or idx >= len(self.radio):
                self._send("Нет станции под номером %d (всего %d)." % (idx + 1, len(self.radio)))
                return
            title, url = self.radio[idx]
            self._play_radio(url, title)
            return
        # текстовый поиск по названию станции
        q = arg.lower()
        found = [(i + 1, t) for i, (t, _u) in enumerate(self.radio) if q in t.lower()]
        if not found:
            self._send("Станция «%s» не найдена." % arg)
            return
        lines = ["Нашёл:"]
        for idx, t in found[:10]:
            lines.append("%d. %s" % (idx, t))
        self._send("\n".join(lines) + "\nradio <номер> — запуск")

    def _play_radio(self, url, title):
        """Play an internet radio stream directly (no download)."""
        self.auto_list = False
        self.auto_playlist = False
        self.queue.clear()
        self.downloading.clear()
        self._stop_voice()
        self.playing = False
        self.paused = False
        self.current = None
        self.current_orig = None
        self.cur_source = (url, title, True)
        self.cur_offset_ms = 0
        self._remember_last(url, title, True)
        self._send("Играет радио: %s" % title)
        self._set_status("Radio: %s" % title)
        self._start_voice(url, 0)

    def _remember_last(self, key, label, is_radio):
        """Запомнить последнее воспроизведение (переживает перезапуск)."""
        try:
            with open(LAST_PLAYED_FILE, "w", encoding="utf-8") as f:
                json.dump({"key": key, "label": label, "radio": bool(is_radio)}, f, ensure_ascii=False)
        except Exception as e:
            log("last played save err: %s" % e)

    def _forget_last(self):
        """Ручной стоп отменяет автопродолжение после перезапуска."""
        try:
            os.remove(LAST_PLAYED_FILE)
        except FileNotFoundError:
            pass
        except Exception as e:
            log("last played clear err: %s" % e)

    def _resume_last(self):
        """Продолжить последнее воспроизведение после перезапуска."""
        try:
            with open(LAST_PLAYED_FILE, encoding="utf-8") as f:
                it = json.load(f)
        except Exception:
            return
        key = it.get("key")
        label = it.get("label") or key
        if not key:
            return
        log("resume last played: %s" % label)
        try:
            if self._platform_of(key):
                # токен в ссылке на трансляцию живёт недолго — берём заново
                self._handle_url(key, label)
            elif it.get("radio"):
                self._play_radio(key, label)
            elif str(key).startswith(("http://", "https://")):
                self._play_file(key, label, key)
            elif str(key).startswith("ymtrack:"):
                self._play_file(key, label, key)
            else:
                log("resume last: skip non-url source")
        except Exception as e:
            log("resume last err: %s" % e)

    def _run_resume_last(self):
        time.sleep(5)
        try:
            self._resume_last()
        except Exception as e:
            log("resume thread err: %s" % e)

    def _run_startup_playback(self):
        """Что играть при старте бота.

        Настройки в config.json:
          startup.volume       1..100 — громкость сразу после входа в канал
          startup.url          ссылка, которая заиграет при запуске
          startup.resume_last  true — вернуть прежнее поведение: продолжать
                               то, что играло до перезапуска (по умолчанию выкл.)
        """
        time.sleep(3)
        try:
            if STARTUP_VOLUME > 0:
                v = max(1, min(MAX_VOLUME, STARTUP_VOLUME))
                self.volume = v
                self.cur_vol = v / 100.0
                log("startup volume: %d%%" % v)
            if STARTUP_RESUME_LAST:
                self._resume_last()
                return
            if not STARTUP_URL:
                log("startup url: не задана — тишина")
                return
            url = STARTUP_URL
            log("startup url: %s" % url)
            if self._platform_of(url):
                self._handle_url(url, url)
                return
            if self._is_live_stream(url):
                self._play_radio(url, url)
            else:
                self._play_file(url, url, url)
        except Exception as e:
            log("startup playback err: %s" % str(e)[:150])

    def _mow_cmd(self):
        """Моя волна — персональный поток Яндекс.Музыки."""
        if not YM_TOKEN:
            self._send("Токен Яндекс.Музыки не настроен. Добавьте services.ym.token в config.json.")
            return
        try:
            from yandex_music import Client
        except Exception:
            self._send("Библиотека yandex-music не установлена. Выполните: pip install yandex-music")
            return
        try:
            client = Client(YM_TOKEN).init()
            station = client.rotor_station_tracks("user:onyourwave")
            if not station or not station.sequence:
                self._send("Моя волна недоступна. Проверьте токен Яндекс.Музыки.")
                return
            tracks = station.sequence
            self._send("🌊 Моя волна — загружаю %d треков..." % len(tracks))
            self.queue.clear()
            for item in tracks:
                tr = item.track
                if not tr:
                    continue
                title = " - ".join(tr.artists_name()) + " - " + tr.title
                self.queue.append(("ymtrack:%s" % tr.track_id, title))
            if self.queue:
                self._enqueue_next()
            else:
                self._send("Моя волна: треки не найдены.")
        except Exception as e:
            self._send("Моя волна: ошибка — %s" % str(e)[:150])
            log("mow err: %s" % str(e))

    # Команды-«запросы», которые при приходе из Telegram объявляются в канале TeamTalk.
    _TG_KNOWN_CMDS = frozenset([
        "v", "volume", "sf", "sb", "sub", "cm", "vo", "rs", "restart", "sv", "svc",
        "cn", "cs", "s", "stop", "skip", "queue", "q", "status", "now",
        "dl", "download", "lf", "n", "next", "b", "back", "prev", "playlist",
        "radio", "r", "f", "fav", "favorites", "play", "p", "pause", "resume",
        "channel", "u", "link", "url", "mow", "волна",
    ])

    def _is_known_cmd(self, text):
        """Первый токен (со слешем или без) — известная команда бота."""
        t = (text or "").strip()
        if not t:
            return False
        if t.startswith("/"):
            t = t[1:].strip()
        parts = t.split(None, 1)
        return bool(parts and parts[0].lower() in self._TG_KNOWN_CMDS)

    def _handle_cmd(self, text, from_user):
        text = (text or "").strip()
        if not text:
            return
        low = text.lower()
        cmd = low[1:].strip() if low.startswith("/") else low
        if not cmd:
            return
        parts = cmd.split(None, 1)
        if not parts:
            return
        first = parts[0]
        if from_user:
            self.reply_user_id = from_user
        self.silent = False  # explicit command → report status again

        # --- гейт: служебные команды — только администраторам ---
        # (в TeamTalk админ = USERTYPE_ADMIN; из Telegram сюда попадают уже после _tg_allowed)
        admin_only = first in (
            "rs", "restart", "cn", "cs", "sv", "svc", "cm", "channel",
            "sc", "save", "cookies"
        )
        if not admin_only:
            admin_only = first == "lf" or cmd.startswith("lf ")
        if admin_only and not self._is_admin(from_user):
            self._send("Эта команда только для администраторов.")
            return

        # --- громкость: v 100 / v 50 / громкость 30 / volume 80 ---
        m = re.match(r"^(?:v|volume)\s+(\d{1,3})$", cmd)
        if m:
            self._set_volume(int(m.group(1)))
            return

        # --- перемотка: sf 5 (вперёд на 5с), sf -5 (назад на 5с) ---
        m = re.match(r"^sf\s+(-?\d{1,6})$", cmd)
        if m:
            self._seek(int(m.group(1)))
            return
        # --- перемотка назад: sb 66 (на 66с назад) ---
        m = re.match(r"^sb\s+(\d{1,6})$", cmd)
        if m:
            self._seek(-int(m.group(1)))
            return

        # --- сообщения в канал: cm — вкл/выкл (по умолчанию ответы в личку) ---
        if cmd == "cm":
            self.channel_msg = not self.channel_msg
            self._save_channel_msg()
            self._send("Сообщения в канал: %s." % ("включены" if self.channel_msg else "выключены"))
            return

        # --- приём кук YouTube: cookies — включить режим ожидания файла кук ---
        if cmd == "cookies":
            self._await_cookies = time.time() + 900  # 15 минут на присылку файла
            self._send(
                "Режим приёма кук включён на 15 минут.\n"
                "Пришлите в Telegram файл с экспортом кук (формат Netscape, "
                "расширение Get cookies.txt). Бот проверит его, сохранит и перезапустится."
            )
            return

        # --- перезапуск бота: rs ---
        if cmd in ("rs", "restart"):
            self._send("Выполняется перезапуск.")
            threading.Thread(target=_restart_bot_soon, daemon=True).start()
            return

        # --- выбор сервиса: sv yt / sv ym / sv ---
        first = cmd.split(None, 1)[0]
        if first in ("sv", "svc"):
            parts = text.split(None, 1)
            if len(parts) < 2:
                cur = {"yt": "YouTube", "ym": "Яндекс.Музыка"}.get(self.service, "?")
                self._send("Текущий сервис: %s. Смена: sv yt или sv ym." % cur)
                return
            svc = parts[1].strip().lower()
            if svc in ("yt", "youtube"):
                self.service = "yt"
                self._send("Сервис: YouTube.")
            elif svc in ("ym", "ya", "yandex"):
                self.service = "ym"
                self._send("Сервис: Яндекс.Музыка.")
            else:
                self._send("Сервис «%s» не распознан. Доступно: yt (YouTube), ym (Яндекс.Музыка)." % svc)
            self._save_runtime()
            return

        # --- сохранить настройки сессии в config.json: sc ---
        if cmd in ("sc", "save"):
            svc = {"yt": "YouTube", "ym": "Яндекс.Музыка"}.get(self.service, self.service)
            try:
                st = self._save_all()
                self._send("Сохранено полностью: ник «%s», сервис %s, громкость %d, "
                           "озвучка приветствия %s (%s), сообщения в канал %s, "
                           "озвучка названий %s, статус %s."
                           % (st["nick"], svc, st["volume"],
                              "вкл" if st["tts"] else "выкл", st["speaker"],
                              "вкл" if st["channel_msg"] else "выкл",
                              "вкл" if st["voice_announce"] else "выкл",
                              ("«%s»" % st["status"]) if st["status"] else "пуст"))
            except Exception as e:
                log("sc save err: %s" % e)
                self._send("Не удалось сохранить настройки: %s" % e)
            return

        # --- смена ника: cn <ник> (в текущей сессии; навсегда — /sc) ---
        if cmd == "cn" or cmd.startswith("cn "):
            parts = text.split(None, 1)
            if len(parts) < 2 or not parts[1].strip():
                self._send("Использование: cn <ник>, например: cn музыкант")
                return
            nick = parts[1].strip()
            if len(nick) > 255:
                self._send("Ник слишком длинный (максимум 255 символов).")
                return
            self.nickname = nick
            self.doChangeNickname(nick)
            self._send("Ник изменён: %s. Постоянное сохранение — /sc." % nick)
            return

        # --- смена статусного сообщения: cs <текст> (пусто = очистить) ---
        if cmd == "cs" or cmd.startswith("cs "):
            parts = text.split(None, 1)
            status = parts[1].strip() if len(parts) > 1 else ""
            self.status_msg = status
            try:
                with open(STATUS_MSG_FILE, "w") as f:
                    f.write(status)
            except Exception as e:
                log("status msg save err: %s" % e)
            self._set_status(status)
            self._send("Статус установлен: %s" % (status or "(пусто — очищен)"))
            return

        # --- стоп / скип / очередь / статус / помощь ---
        if cmd in ("s", "stop"):
            self._forget_last()
            self.queue.clear()
            self.downloading.clear()
            self.auto_list = False
            self.auto_playlist = False
            self.silent = False
            self._stop_voice()
            self.playing = False
            self.paused = False
            self.current = None
            self.current_orig = None
            self.cur_offset_ms = 0
            self._announce_pending = None
            self._set_status(self.status_msg)
            self._send("Воспроизведение остановлено.")
            return

        if cmd in ("skip",):
            self._advance()
            return

        if cmd in ("queue", "q"):
            self._queue_cmd()
            return

        if cmd in ("status", "now"):
            self._status_cmd()
            return

        if cmd in ("mow", "волна", "моя волна"):
            self._mow_cmd()
            return

        # --- загрузить играющий трек файлом в канал (сервер TeamTalk): dl / скачать / download ---
        if cmd in ("dl", "download"):
            if not self.playing or not self.current_file:
                self._send("Воспроизведение неактивно — выгружать нечего.")
                return
            if self._dl_cmd_id is not None:
                self._send("Файл уже загружается. Ожидайте завершения.")
                return
            path = self.current_file
            if not os.path.isfile(path):
                self._send("Файл трека недоступен (вероятно, это радио).")
                return
            title = (self.current or ("", "трек"))[1] or "трек"
            ext = os.path.splitext(path)[1] or ".mp3"
            safe = re.sub(r'[\\/:*?"<>|\s]+', "_", title).strip("_")[:80] or "track"
            # TT_DoSendFile кладёт файл на сервер под именем basename локального файла —
            # делаем копию с нормальным именем, чтобы в канале был читаемый файл
            tmp = os.path.join(CACHE_DIR, "upload_%d_%s%s" % (int(time.time() * 1000), safe, ext))
            try:
                shutil.copy2(path, tmp)
            except Exception as e:
                log("dl copy err: %s" % str(e)[:150])
                self._send("Не удалось подготовить файл: %s" % str(e)[:100])
                return
            # Треки теперь хранятся как есть (opus/webm/m4a) без перекодирования;
            # в канал удобнее mp3 — конвертируем копию один раз, только по /dl.
            if ext.lower() != ".mp3":
                self._send("Подготовка mp3…")
                mp3tmp = os.path.join(CACHE_DIR, "upload_%d_%s.mp3" % (int(time.time() * 1000), safe))
                try:
                    # nice, чтобы транскод не отъедал CPU у играющего трека
                    rcc = subprocess.call(
                        ["nice", "-n", "10", "ffmpeg", "-y", "-i", tmp, "-vn",
                         "-codec:a", "libmp3lame", "-q:a", "5", mp3tmp],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        env=_media_env(),
                    )
                except Exception:
                    rcc = 1
                if rcc == 0 and os.path.isfile(mp3tmp) and os.path.getsize(mp3tmp) > 1024:
                    try:
                        os.remove(tmp)
                    except Exception:
                        pass
                    tmp = mp3tmp
                else:
                    try:
                        os.remove(mp3tmp)
                    except Exception:
                        pass
            cid = self.my_channel_id or 1
            cmd_id = TeamTalk5._DoSendFile(self._tt, cid, _b(tmp))
            if cmd_id < 0:
                try:
                    os.remove(tmp)
                except Exception:
                    pass
                self._send("Не удалось начать загрузку файла в канал.")
                return
            self._dl_cmd_id = cmd_id
            self._dl_remote = os.path.basename(tmp)
            self._dl_local = tmp
            self._send("Загрузка файла «%s» в канал…" % self._dl_remote)
            return

        if cmd in ("help", "h"):
            self._help_cmd()
            return

        # --- локальный файл: lf <путь> (только аудиофайлы) ---
        if first == "lf":
            parts = text.split(None, 1)
            if len(parts) < 2 or not parts[1].strip():
                self._send("Использование: /lf <путь к аудиофайлу>")
                return
            path = os.path.realpath(parts[1].strip())
            if not os.path.isfile(path):
                self._send("Файл не найден: %s" % parts[1].strip())
                return
            ext = os.path.splitext(path)[1].lower()
            if ext not in (".mp3", ".wav", ".ogg", ".flac", ".m4a", ".opus", ".aac", ".wma", ".m3u", ".m3u8"):
                self._send("Разрешены только аудиофайлы (" + ", ".join((".mp3", ".wav", ".ogg", ".flac", ".m4a", ".opus", ".aac", ".wma", ".m3u", ".m3u8")) + ").")
                return
            self._play_local(path, os.path.basename(path))
            return

        # --- n/b: следующий/предыдущий (по активному плейлисту или списку поиска) ---
        if cmd in ("n", "next"):
            if self.auto_playlist:
                self._play_playlist_index(self.playlist_index + 1)
            elif self.auto_list:
                self._play_search_index(self.search_index + 1)
            elif self.playlist:
                self._play_playlist_index(self.playlist_index + 1)
            else:
                self._play_search_index(self.search_index + 1)
            return

        if cmd in ("b", "back", "prev"):
            if self.auto_playlist:
                self._play_playlist_index(self.playlist_index - 1)
            elif self.auto_list:
                self._play_search_index(self.search_index - 1)
            elif self.playlist:
                self._play_playlist_index(self.playlist_index - 1)
            else:
                self._play_search_index(self.search_index - 1)
            return

        # --- плейлист: пл / список / плейлист [<страница>] — полный список ---
        cmd_first = first
        if cmd_first in ("playlist",):
            parts = text.split(None, 1)
            page = 1
            if len(parts) > 1 and parts[1].strip().isdigit():
                page = int(parts[1].strip())
            self._send("\n".join(self._playlist_page_lines(page)))
            return

        # --- радио: радио / радио <N> / радио <текст> ---
        if cmd.startswith("radio") or cmd == "r":
            arg = text.split(None, 1)[1].strip() if " " in text else ""
            self._radio_cmd(arg)
            return

        # --- избранное: f / f + / f + <ссылка> / f <номер> ---
        if cmd in ("f", "fav", "favorites") or cmd.startswith(("f ", "fav ", "favorites ")):
            arg = text.split(None, 1)[1].strip() if " " in text else ""
            self._fav_cmd(arg)
            return

        # --- play/p: с текстом — поиск и игра; голый — пауза/продолжить.
        # Ссылки сюда не принимаем — для них /u или просто вставь ссылку.
        if cmd == "play" or cmd.startswith("play ") or cmd == "p" or cmd.startswith("p "):
            parts = text.split(None, 1)
            arg = parts[1].strip() if len(parts) > 1 else ""
            if arg:
                if URL_RE.search(arg):
                    self._send("Это ссылка. Для ссылок: /u <ссылка> или отправьте ссылку напрямую.")
                else:
                    self._do_search(arg)
                return
            if self.paused:
                self._resume()
            elif self.playing:
                self._pause()
            else:
                self._send("Запрос не указан. Формат: /play <запрос> или отправьте ссылку напрямую.")
            return

        # --- pause / resume: явные команды ---
        if cmd in ("pause", "resume"):
            if cmd == "pause" and self.playing and not self.paused:
                self._pause()
            elif cmd == "resume" and self.paused:
                self._resume()
            return

        # --- смена канала ---
        if cmd.startswith("channel"):
            parts = text.split(None, 1)
            if len(parts) < 2:
                self._send("Использование: /channel <путь>, например: /channel /root/music")
                return
            path = parts[1].strip()
            cid = self.getChannelIDFromPath(path)
            if not cid:
                self._send("Канал не найден: %s" % path)
                return
            self.doJoinChannelByID(cid, CHANNEL_PASSWORD)
            self.play_channel_id = cid
            self._send("Переход в канал: %s" % path)
            return

        # --- прямая ссылка: u <url> / link <url> / url <url> ---
        # (cmd уже без слэша: "/u https://…" → cmd = "u https://…")
        first = cmd.split(None, 1)[0]
        if first in ("u", "link", "url"):
            parts = text.split(None, 1)
            if len(parts) < 2:
                self._send("Ссылка не указана. Формат: /u <ссылка> или отправьте ссылку напрямую.")
                return
            u = URL_RE.search(parts[1])
            if not u:
                self._send("Это не ссылка: «%s». Формат: /u https://…" % parts[1][:80])
                return
            self._handle_url(u.group(0), u.group(0))
            return

        # --- bare ссылка ---
        m = URL_RE.search(text)
        if m and not low.startswith("/"):
            # Через _handle_url, а не _switch_to: так живой поток (радио)
            # играется напрямую, а плейлист раскрывается в очередь.
            self._handle_url(m.group(0), m.group(0))
            return

    def _enqueue_url(self, url, label):
        if url in [u for u, _ in self.queue]:
            if not self.silent:
                self._send("Уже в очереди: %s" % label)
            return
        self.queue.append((url, label))
        if not self.silent:
            self._notify("Добавлено в очередь: %s" % label)
        self._enqueue_next()

    def _queue_cmd(self):
        if not self.queue:
            self._send("Очередь пуста.")
        else:
            lines = ["Очередь:"]
            for i, (u, t) in enumerate(self.queue[:10], 1):
                lines.append("%d. %s (%s)" % (i, t, u))
            self._send("\n".join(lines))

    def _status_cmd(self):
        if self.playing and self.current:
            lines = ["Сейчас играет: %s" % self.current[1]]
            lines.append("Время: %s" % _fmt_ms(self._elapsed_ms()))
            if self.paused:
                lines.append("Пауза")
            if self.volume < 100:
                lines.append("Громкость: %d%%" % self.volume)
        else:
            lines = ["Воспроизведение неактивно."]
        lines.append("Справка: /help.")
        self._send("\n".join(lines))

    def _help_cmd(self):
        """Полная справка. Длинный текст TeamTalk рвёт на части, поэтому
        отправляем несколькими короткими сообщениями подряд."""
        parts = [
            "Справка 1/4 · воспроизведение\n"
            "play, p <запрос> — поиск и игра; без запроса — пауза/продолжение\n"
            "pause, resume — пауза и продолжить\n"
            "stop, s — стоп и очистка очереди; skip — пропустить трек\n"
            "n, next — следующий; b, back, prev — предыдущий\n"
            "queue, q — очередь воспроизведения\n"
            "playlist <стр> — плейлист постранично\n"
            "u, link, url <ссылка> — играть по ссылке\n"
            "radio, r — станции; radio <номер> — запуск\n"
            "f, fav — избранное: f +, f + <ссылка>, f <номер>, f - <номер>",

            "Справка 2/4 · звук и источники\n"
            "v, volume <1-100> — громкость\n"
            "sf <сек> — перемотка вперёд, sb <сек> — назад\n"
            "mow, волна — моя волна\n"
            "sv yt — YouTube; sv ym — Яндекс.Музыка\n"
            "status, now — что играет сейчас\n"
            "lf <путь> — файл с диска\n"
            "dl — выгрузить текущий трек файлом в канал\n"
            "sub — уведомления о входах; sub mus — о треках\n"
            "login — вход в YouTube; cookies — обновить куки",

            "Справка 3/4 · оформление и приветствия\n"
            "as on | as off — сетевые приветствия при входе\n"
            "tts on | tts off — голосовое приветствие\n"
            "tts <голос> — выбрать голос; tts — показать текущий\n"
            "vo — озвучка названий треков\n"
            "cm — ответы в канал или в личку\n"
            "cn <ник> — сменить ник\n"
            "cs <текст> — статус\n"
            "channel <путь> — перейти в канал\n"
            "sc — сохранить настройки",

            "Справка 4/4 · служебное\n"
            "help, h — эта справка\n"
            "rs — перезапуск бота\n"
            "\nВ Telegram дополнительно:\n"
            "admins — список админов\n"
            "subs — админы и подписки (кнопки)\n"
            "kick <ник> — исключить\n"
            "ban <ник> — заблокировать\n"
            "unban — разблокировать\n"
            "net <текст> — сетевое сообщение всем\n"
            "panel, пульт — пульт управления",
        ]
        for i, part in enumerate(parts):
            self._send(part)
            if i < len(parts) - 1:
                time.sleep(0.5)

    # ----- events ----------------------------------------------------
    def onConnectSuccess(self):
        log("connected, logging in")
        self.connected = True
        self._login()

    def onConnectFailed(self):
        log("connect failed; exiting for restart")
        threading.Thread(target=_restart_bot_soon, daemon=True).start()

    def onConnectionLost(self):
        log("connection lost; exiting for restart")
        self.connected = False
        self.playing = False
        self.logged_in = False
        self.joined = False
        # In-process reconnect (self.connect) hangs without firing a result
        # event, so exit and let systemd Restart=always launch a fresh process;
        # run.sh waits for the tt5 TCP port before starting bot.py.
        threading.Thread(target=_restart_bot_soon, daemon=True).start()

    def onCmdMyselfLoggedOut(self):
        log("logged out")

    def onCmdMyselfKickedFromChannel(self, channelid, user):
        log("kicked from channel %s by %s" % (channelid, user.szNickname))

    def onCmdMyselfLoggedIn(self, userid, useraccount):
        self.my_user_id = userid
        self.logged_in = True
        log("logged in as %s (user id %d)" % (self.nickname, userid))
        target = CHANNEL if CHANNEL else "/"
        cid = self.getChannelIDFromPath(target)
        if not cid:
            cid = self.getRootChannelID()
        self.play_channel_id = cid
        self.my_channel_path = target
        log("joining channel id %d (path %s)" % (cid, target))
        self.doJoinChannelByID(cid, CHANNEL_PASSWORD)
        if self.status_msg:
            self._set_status(self.status_msg)
        if START_COMMANDS:
            threading.Thread(target=self._run_startup_commands, daemon=True).start()

    def _run_startup_commands(self):
        time.sleep(3)
        for c in START_COMMANDS:
            try:
                log("startup cmd: %s" % c)
                self._handle_cmd(c, 0)
            except Exception as e:
                log("startup cmd error: %s" % e)
            time.sleep(1)

    def onCmdUserJoinedChannel(self, user):
        try:
            if user.nUserID == self.my_user_id:
                self.my_channel_id = user.nChannelID
                self._ready_time = time.time()
                log("joined channel id %d" % self.my_channel_id)
                if not getattr(self, "_resume_started", False):
                    self._resume_started = True
                    threading.Thread(target=self._run_startup_playback, daemon=True).start()
        except Exception:
            pass

    def _tt_field(self, user, name):
        """Read a User struct field, decode bytes → str, strip NUL."""
        v = getattr(user, name, "") or ""
        if isinstance(v, bytes):
            v = v.decode("utf-8", "ignore")
        return v.replace("\x00", "").strip()

    def _tg_send_notify(self, text, chat_id):
        """Send a Telegram notification in a background thread (don't block the audio loop)."""
        def _do():
            try:
                self._tg_api("sendMessage", chat_id=chat_id, text=text)
                log("tg notify -> %s: %s" % (chat_id, text[:80]))
            except Exception as e:
                log("tg notify err: %s" % str(e)[:120])
        threading.Thread(target=_do, daemon=True).start()

    def _load_subs(self):
        try:
            with open(SUBS_FILE, encoding="utf-8") as f:
                d = json.load(f)
            now = time.time()
            self.sub_pending = {
                t: p for t, p in (d.get("pending") or {}).items()
                if p.get("created", 0) > now - SUB_TTL_SEC
            }
            self.sub_active = {str(c): s for c, s in (d.get("active") or {}).items()}
        except Exception:
            self.sub_pending = {}
            self.sub_active = {}

    def _save_subs(self):
        try:
            now = time.time()
            self.sub_pending = {
                t: p for t, p in self.sub_pending.items()
                if p.get("created", 0) > now - SUB_TTL_SEC
            }
            with open(SUBS_FILE, "w", encoding="utf-8") as f:
                json.dump({"pending": self.sub_pending, "active": self.sub_active},
                          f, ensure_ascii=False, indent=2)
        except Exception as e:
            log("subs save err: %s" % e)

    def _load_admins(self):
        """Админы Telegram из users.db (владелец из конфига — админ всегда)."""
        try:
            with open(ADMINS_FILE, encoding="utf-8") as f:
                d = json.load(f)
            return [int(a) for a in (d.get("admins") or []) if str(a).strip()]
        except Exception:
            return []

    def _save_admins(self):
        try:
            with open(ADMINS_FILE, "w", encoding="utf-8") as f:
                json.dump({"admins": self.admins}, f, ensure_ascii=False, indent=2)
        except Exception as e:
            log("admins save err: %s" % e)

    def _is_tt_command(self, text):
        """Команда боту только со слэшем: /play, /sub, ... Всё остальное — сообщение
        (пересылается в Telegram)."""
        return bool(text and text.startswith("/"))

    def _is_typing_indicator(self, msg):
        """Клиенты TeamTalk, пока пользователь набирает текст, шлют в канал текст
        «typing\nN» — это индикатор набора, не настоящее сообщение. Его не
        пересылаем и не обрабатываем, иначе засоряет чат в Telegram."""
        low = msg.strip().lower()
        return low == "typing" or low.startswith("typing\n")

    def _bare_link(self, text):
        """Вернуть URL, если сообщение — ровно одна ссылка и ничего больше."""
        t = (text or "").strip()
        m = URL_RE.match(t)
        if m and m.group(0) == t:
            return t
        return None

    def _tg_admin_ids(self):
        ids = set(self.admins)
        if TG_OWNER_USER_ID:
            ids.add(TG_OWNER_USER_ID)
        return ids

    def _is_admin(self, from_user):
        """TeamTalk: админ = USERTYPE_ADMIN (2) на сервере. Telegram-путь (from_user=0)
        уже отфильтрован в _tg_allowed, поэтому проходит."""
        if not from_user:
            return True
        u = self.users.get(from_user)
        return bool(u and (int(getattr(u, "uUserType", 0) or 0) & 2))  # USERTYPE_ADMIN — бит

    def _server_name(self):
        if TG_NOTIFY_SERVER:
            return TG_NOTIFY_SERVER
        try:
            sp = self.getServerProperties()
            if sp:
                name = self._tt_field(sp, "szServerName")
                if name:
                    return name
        except Exception:
            pass
        return "TeamTalk"

    # Пол в статус-режиме (nStatusMode): бит 0x100 — женский, 0x1000 — нейтральный (TeamTalk).
    _STATUSMODE_GENDER_MASK = 0x1100
    _STATUSMODE_FEMALE = 0x0100

    def _user_female(self, user):
        """True, если у пользователя в статус-режиме стоит женский пол."""
        try:
            return (user.nStatusMode & self._STATUSMODE_GENDER_MASK) == self._STATUSMODE_FEMALE
        except Exception:
            return False

    def _join_quiet(self, user):
        """Учётка из исключений: такую не приветствуем и не анонсируем вход/выход.
        Совпадает по логину (ignore_usernames) или по нику (ignore_nicks)."""
        try:
            uname = (self._tt_field(user, "szUsername") or "").strip().lower()
            if uname in TG_NOTIFY_IGNORE:
                return True
            nick = (self._tt_field(user, "szNickname") or "").strip().lower()
            if nick and nick in TG_NOTIFY_IGNORE_NICKS:
                return True
        except Exception:
            pass
        return False

    def _notify_join_leave(self, sign, user):
        """Announce a user logging in (+)/out (-): владельцу и всем подписчикам."""
        try:
            if not TG_TOKEN or not user:
                return
            if user.nUserID == self.my_user_id:
                return
            # grace period: don't broadcast the initial roster replay right after connect
            if not self._ready_time or time.time() - self._ready_time < 3:
                return
            nick = self._tt_field(user, "szNickname") or self._tt_field(user, "szUsername")
            if not nick:
                return
            if self._join_quiet(user):
                return
            female = self._user_female(user)
            if sign == "+":
                text = "%s %s к серверу %s" % (nick, "присоединилась" if female else "присоединился", self._server_name())
            else:
                text = "%s %s сервер %s" % (nick, "покинула" if female else "покинул", self._server_name())
            if TG_NOTIFY_CHAT_ID:
                self._tg_send_notify(text, TG_NOTIFY_CHAT_ID)
            if TG_JOIN_TO_SUBS:
                for cid in list(self.sub_active):
                    self._tg_send_notify(text, int(cid))
        except Exception as e:
            log("notify join/leave err: %s" % str(e)[:150])

    def _audio_active(self):
        """Играет ли сейчас звук в канал: трек ИЛИ живой поток (радио).

        У радио self.playing остаётся False: поток бесконечный, прогресс по
        нему не считается. Одного флага мало — иначе приветствие решает, что
        музыки нет, глушит поток и после фразы тишина. Смотрим ещё на
        cur_source с признаком radio и на живой поток отправки.
        """
        try:
            if self.playing and not self.paused:
                return True
            cs = self.cur_source
            t = self.voice_thread
            if cs and len(cs) >= 3 and cs[2] and t is not None and t.is_alive():
                return True
        except Exception:
            pass
        return False

    def _trim_bans(self):
        """Авто-баны не копим бесконечно: свыше 1200 записей режем старые авто до 800."""
        try:
            if len(self.bans) <= 1200:
                return
            auto = sorted(
                ((v.get("banned_at") or 0, k) for k, v in self.bans.items()
                 if isinstance(v, dict) and v.get("auto")),
                key=lambda kv: kv[0],
            )
            for _, k in auto[:max(0, len(auto) - 800)]:
                self.bans.pop(k, None)
        except Exception:
            pass

    def onCmdUserLoggedIn(self, user):
        try:
            self.users[user.nUserID] = user
        except Exception:
            pass
        self._notify_join_leave("+", user)

    def onCmdUserLoggedOut(self, user):
        try:
            self.users.pop(user.nUserID, None)
        except Exception:
            pass
        self._notify_join_leave("-", user)

    def _dl_finish(self, ok, detail=""):
        if self._dl_local:
            try:
                os.remove(self._dl_local)
            except Exception:
                pass
            self._dl_local = None
        name = self._dl_remote or "трек"
        self._dl_cmd_id = None
        self._dl_remote = None
        if ok:
            self._send("Файл «%s» загружен в канал — доступен для скачивания в TeamTalk." % name)
        else:
            self._send("Не удалось загрузить файл в канал: %s" % (detail or "неизвестная ошибка"))

    def onCmdSuccess(self, cmdId):
        if self._dl_cmd_id is not None and cmdId == self._dl_cmd_id:
            self._dl_finish(True)
            return

    def onCmdError(self, cmdId, errmsg):
        msg = errmsg.szErrorMsg if errmsg else ""
        if isinstance(msg, bytes):
            msg = msg.decode("utf-8", "ignore")
        log("cmd error (cmd %d): %s" % (cmdId, msg))
        # Серверный login-delay (flood) режет повторный вход, когда второй
        # клиент с того же IP (регистратор) успел залогиниться первым.
        # Не застреваем в connected-but-not-logged-in: ждём и повторяем логин.
        low = msg.lower() if isinstance(msg, str) else ""
        if cmdId == 1 and not self.logged_in and ("too fast" in low or "flood" in low):
            n = getattr(self, "_login_flood_retries", 0) + 1
            self._login_flood_retries = n
            if n >= 5:
                log("login flood: 5 попыток не хватило, полный рестарт")
                threading.Thread(target=_restart_bot_soon, daemon=True).start()
                return
            delay = 3.0
            log("login flood (попытка %d/5): повтор логина через %.1fs" % (n, delay))
            def _retry_login():
                time.sleep(delay)
                if not self.logged_in:
                    self._login()
            threading.Thread(target=_retry_login, daemon=True).start()
            return
        if self._dl_cmd_id is not None and cmdId == self._dl_cmd_id:
            self._dl_finish(False, msg)
            return
        now = time.time()
        if now - self._last_err_sent > 5:
            self._last_err_sent = now
            self._send("Ошибка: %s" % msg)

    def onCmdUserTextMessage(self, textmessage):
        if not textmessage:
            return
        if textmessage.nFromUserID == self.my_user_id:
            return
        try:
            # Реагируем только на личные сообщения (MSGTYPE_USER). Всё, что
            # приходит в канал (ссылки, команды, текст), бот полностью игнорирует —
            # не видит и не отвечает.
            if int(getattr(textmessage, "nMsgType", 0) or 0) != TextMsgType.MSGTYPE_USER:
                return
            msg = textmessage.szMessage
            if isinstance(msg, bytes):
                msg = msg.decode("utf-8", "ignore")
            msg = msg.replace("\x00", "").strip()
            if not msg:
                return
            log("msg from %d: %s" % (textmessage.nFromUserID, msg))
            if self._is_typing_indicator(msg):
                log("typing indicator from %d, skip" % textmessage.nFromUserID)
                return
            # команды — только со слэшем (/sub, /play, ...). Исключение:
            # сообщение-ссылка (ровно один URL и больше ничего) в личку боту —
            # играем напрямую (минуя play-ветку: /play ссылки не принимает,
            # для них /u или вставка).
            # Прочие не-команды: пересылаем админам (можно ответить).
            if not self._is_tt_command(msg):
                bare = self._bare_link(msg)
                if bare:
                    self._handle_url(bare, bare)
                    return
                return  # пересылка сообщений админам вырезана в lite-сборке
            self._handle_cmd(msg, textmessage.nFromUserID)
        except Exception as e:
            log("handle msg error: %s" % e)

    def onStreamMediaFile(self, mediafileinfo):
        try:
            status = mediafileinfo.nStatus
            if status in (
                MediaFileStatus.MFS_FINISHED,
                MediaFileStatus.MFS_CLOSED,
                MediaFileStatus.MFS_ABORTED,
                MediaFileStatus.MFS_ERROR,
            ):
                if self.playing:
                    self.api_q.put(("advance",))
        except Exception as e:
            log("stream event error: %s" % e)

    # ----- lifecycle --------------------------------------------------
    def _login(self):
        self.doLogin(self.nickname, USERNAME, PASSWORD, CLIENTNAME)

    def _run_once(self):
        self.runEventLoop(50)
        while True:
            try:
                item = self.api_q.get_nowait()
            except queue.Empty:
                break
            kind = item[0]
            try:
                if kind == "download_ok":
                    _, url, title, path = item
                    if url in self.downloading:
                        self.downloading.discard(url)
                    if self.queue and self.queue[0][0] == url:
                        self.queue[0] = (url, title)
                        if not self.playing:
                            self._play_file(url, title, path)
                        elif not self.silent:
                            self._send("Скачано (в очереди): %s" % title)
                    # else: stale download (superseded by n/b switch) — drop
                elif kind == "download_fail":
                    _, url, title, err = item
                    if url in self.downloading:
                        self.downloading.discard(url)
                    # remove from queue if first
                    if self.queue and self.queue[0][0] == url:
                        self.queue.pop(0)
                        if self.auto_playlist and self.playlist:
                            self._send("Не удалось: %s" % err)
                            self._advance(silent=True)
                        else:
                            self._send("Не удалось скачать %s: %s" % (url, err))
                            self._enqueue_next()
                    else:
                        self._send("Ошибка скачивания: %s" % err)
                elif kind == "search_done":
                    _, query, items = item
                    if not items:
                        self._send("По запросу «%s» ничего не найдено." % query)
                        return
                    self.search_results = items
                    self.search_index = 0
                    lines = ["Результаты (%d):" % len(items)]
                    for i, it in enumerate(items[:10], 1):
                        lines.append("%d. %s" % (i, it["title"][:60]))
                    self._send("\n".join(lines))
                    self._play_search_index(0)
                elif kind == "playlist_done":
                    _, url, items = item
                    if not items:
                        self._send("Не удалось получить плейлист: %s" % url)
                        return
                    self.auto_list = False
                    self.queue = []
                    self.downloading.clear()
                    self._stop_voice()
                    self.playing = False
                    self.paused = False
                    self.current = None
                    self.current_orig = None
                    self.cur_offset_ms = 0
                    self.voice_offset_base = 0
                    self.voice_started_at = 0
                    self._set_status(self.status_msg)
                    self.playlist = items
                    self._send("\n".join(self._playlist_page_lines(1)))
                    self._play_playlist_index(0)
                elif kind == "advance":
                    self._advance(silent=True)
                elif kind == "voice_finished":
                    _, run, path = item
                    if run != self._voice_run:
                        continue  # устаревшее событие остановленного/заменённого голоса
                    if self._announce_pending:
                        tpath, offset = self._announce_pending
                        self._announce_pending = None
                        # закончилась озвучка названия — стартуем сам трек
                        self._start_voice(tpath, offset)
                        if self.current:
                            self._music_broadcast(tpath, self.current[1])
                    else:
                        self._advance(silent=True)
                elif kind == "announce_ready":
                    _, path, ann = item
                    pending = self._announce_pending
                    if not pending or pending[0] != path:
                        # трек остановлен/заменён, пока шёл синтез — озвучка не нужна
                        try:
                            os.remove(ann)
                        except Exception:
                            pass
                        continue
                    self._start_voice(ann, 0)  # озвучка; трек стартует по её завершении
                elif kind == "announce_failed":
                    _, path, title = item
                    pending = self._announce_pending
                    if not pending or pending[0] != path:
                        continue  # трек уже неактуален
                    self._announce_pending = None
                    self._start_voice(path, 0)  # озвучка не вышла — играем трек сразу
                    if self.current and self.current[1] == title:
                        self._music_broadcast(path, title)
                elif kind == "voice_error":
                    _, msg = item
                    self._send("Ошибка озвучки: %s" % msg)
                    self._advance()
                elif kind == "status":
                    _, msg = item
                    self._send(msg)
                elif kind == "local_file":
                    _, path, title = item
                    self._play_local(path, title)
                elif kind == "restart":
                    _, path, offset = item
                    self._start_voice(path, int(offset))
            except Exception as e:
                log("api_q handler error: %s\n%s" % (e, traceback.format_exc()))

    def run(self):
        while self.reconnect:
            try:
                self.connect(HOST, TCP_PORT, UDP_PORT)
                break
            except Exception as e:
                log("connect exception: %s\n%s" % (e, traceback.format_exc()))
                time.sleep(10)
        # The SDK's connect is async and sometimes stalls with no result
        # event (TCP established, handshake never completes). Watchdog:
        # if not connected within 20s, exit and let systemd relaunch.
        deadline = time.monotonic() + CONNECT_TIMEOUT
        while self.reconnect and not self.connected:
            self.runEventLoop(100)
            if time.monotonic() > deadline:
                log("connect watchdog timeout; exiting for restart")
                threading.Thread(target=_restart_bot_soon, daemon=True).start()
                return
        while self.reconnect:
            try:
                self._run_once()
            except KeyboardInterrupt:
                break
            except Exception as e:
                log("loop error: %s" % e)
                time.sleep(1)


def main():
    bot = MusicBot()
    log("starting music bot for %s:%d (user %s)" % (HOST, TCP_PORT, USERNAME))
    _start_nightly_restart()
    if REG_ENABLED and REG_TOKEN and REG_ADMIN_USER_IDS \
            and os.environ.get("TT_REGISTRAR", "") != "service":
        try:
            import tt_register
            bot._registrar = tt_register.start({
                "token": REG_TOKEN,
                "admin_user_ids": REG_ADMIN_USER_IDS,
                "broadcast_text": REG_BROADCAST_TEXT,
                "hostname": HOST,
                "tcp_port": TCP_PORT,
                "udp_port": UDP_PORT,
                "tt_username": REG_ADMIN_TT_USER,
                "tt_password": REG_ADMIN_TT_PASS,
                "tt_nickname": REG_ADMIN_TT_NICK,
                "user_rights": REG_USER_RIGHTS,
                "state_file": os.path.join(BASE_DIR, "register_requests.json"),
                "log_fn": log,
            })
            if bot._registrar:
                log("registrar: модуль регистрации запущен")
            else:
                log("registrar: пустой токен/админ — регистратор выключен")
        except Exception as e:
            log("registrar start error: %s" % e)
    bot.run()


if __name__ == "__main__":
    main()
