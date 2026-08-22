#!/usr/bin/env python3
"""Music bot for TeamTalk: plays audio from URLs into a voice channel."""
import ctypes
import json
import os
import queue
import re
import select
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
import uuid

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

HOST = os.environ.get("TEAMTALK_HOST", "example.com")
TCP_PORT = int(os.environ.get("TEAMTALK_TCP_PORT", "10333"))
UDP_PORT = int(os.environ.get("TEAMTALK_UDP_PORT", "10333"))
NICKNAME = os.environ.get("TEAMTALK_NICKNAME", "MusicBot")
USERNAME = os.environ.get("TEAMTALK_USERNAME", "example")
PASSWORD = os.environ.get("TEAMTALK_PASSWORD", "")
CLIENTNAME = "teamtalk-music-bot"
CHANNEL = os.environ.get("TEAMTALK_CHANNEL", "")  # empty = root channel

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(BASE_DIR, "cache")
os.makedirs(CACHE_DIR, exist_ok=True)
INBOX_DIR = os.path.join(BASE_DIR, "inbox")  # files relayed from Telegram
os.makedirs(INBOX_DIR, exist_ok=True)

NICKNAME_FILE = os.path.join(BASE_DIR, ".nickname")

TG_TOKEN = os.environ.get("TG_TOKEN", "").strip()  # optional: own Telegram bot that relays files

# Optional YouTube cookies to bypass bot-check on restricted videos.
COOKIES = os.environ.get("TEAMTALK_COOKIES") or os.path.join(
    BASE_DIR, "..", ".secrets", "cookies.txt"
)
if not os.path.isfile(COOKIES):
    COOKIES = None

# Optional Rutube cookies for auth-gated video downloads (search stays blocked by their bot-protection).
RUTUBE_COOKIES = os.environ.get("TEAMTALK_RUTUBE_COOKIES") or os.path.join(
    BASE_DIR, "..", ".secrets", "rutube_cookies.txt"
)
if not os.path.isfile(RUTUBE_COOKIES):
    RUTUBE_COOKIES = None

# Optional Yandex Music OAuth token (from .secrets/ym_token.txt or TEAMTALK_YM_TOKEN).
YM_TOKEN = None
_ym_path = os.environ.get("TEAMTALK_YM_TOKEN") or os.path.join(
    BASE_DIR, "..", ".secrets", "ym_token.txt"
)
if os.path.isfile(_ym_path):
    YM_TOKEN = open(_ym_path, encoding="utf-8").read().strip()

URL_RE = re.compile(r"https?://\S+", re.I)

# FFMpeg/yt-dlp resolve via PATH
YTDLP = sys.executable and [sys.executable, "-m", "yt_dlp"]

# Voice transmission: raw PCM fed to TT_InsertAudioBlock as STREAMTYPE_VOICE.
VOICE_RATE = 48000  # Hz
VOICE_CHUNK = 960   # samples per block (20 ms at 48 kHz)
VOICE_CHUNK_BYTES = VOICE_CHUNK * 2  # s16 mono


def log(msg):
    print("%s %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def _fmt_ms(ms):
    s = int(ms) // 1000
    return "%d:%02d" % (s // 60, s % 60)


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
        self.playing = False
        self.downloading = set()
        self.reconnect = True
        self.connecting = False
        self._last_err_sent = 0
        self.paused = False
        self.volume = 10
        self.service = "yt"  # "yt" = YouTube, "ym" = Yandex.Music
        self.cur_offset_ms = 0
        self.segment_started_at = 0
        self.current_orig = None
        # voice transmission (TT_InsertAudioBlock)
        self.voice_stop = threading.Event()
        self.voice_thread = None
        self.voice_proc = None
        self.voice_offset_base = 0
        self.voice_started_at = 0
        # last search result list for n/b switching and auto-advance
        self.search_results = []
        self.search_index = 0
        self.auto_list = False  # True when playing through the search-result list
        # radio stations from radio/ folder (m3u playlists grouped by category)
        self.radio = self._load_radio()
        if self.radio:
            log("radio loaded: %d stations" % len(self.radio))
        self.nickname = NICKNAME
        try:
            n = open(NICKNAME_FILE).read().strip()
            if n:
                self.nickname = n
        except Exception:
            pass
        # optional Telegram relay: an own bot that forwards files into the channel
        self._tg_offset = 0
        if TG_TOKEN:
            threading.Thread(target=self._tg_poll, daemon=True, name="tg-poll").start()
            log("telegram relay enabled")

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
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode())

    def _tg_poll(self):
        while True:
            try:
                res = self._tg_api("getUpdates", timeout=25, offset=self._tg_offset)
                for upd in res.get("result", []):
                    self._tg_offset = upd.get("update_id", 0) + 1
                    self._tg_handle_update(upd)
            except Exception as e:
                log("tg poll err: %s" % str(e)[:150])
                time.sleep(5)

    def _tg_handle_update(self, upd):
        msg = upd.get("message") or upd.get("channel_post")
        if not msg:
            return
        media = msg.get("voice") or msg.get("audio") or msg.get("video") or msg.get("document")
        if not media:
            return
        file_id = media.get("file_id")
        if not file_id:
            return
        title = (
            msg.get("caption")
            or media.get("file_name")
            or media.get("title")
            or "audio"
        ).strip() or "audio"
        path = self._tg_download(file_id)
        if path:
            self.api_q.put(("local_file", path, title[:80]))

    def _tg_download(self, file_id):
        try:
            info = self._tg_api("getFile", file_id=file_id).get("result")
            if not info or not info.get("file_path"):
                return None
            fpath = info["file_path"]
            url = "https://api.telegram.org/file/bot%s/%s" % (TG_TOKEN, fpath)
            with urllib.request.urlopen(url, timeout=180) as r:
                data = r.read()
            name = os.path.basename(fpath) or ("audio_%s.ogg" % file_id[:8])
            local = os.path.join(INBOX_DIR, name)
            with open(local, "wb") as f:
                f.write(data)
            log("tg downloaded %s (%d bytes)" % (local, len(data)))
            return local
        except Exception as e:
            log("tg download err: %s" % str(e)[:150])
            return None

    def _send(self, text):
        if not (self.logged_in and self.my_channel_id):
            log("_send skip: logged_in=%s chan=%s" % (self.logged_in, self.my_channel_id))
            return
        try:
            msgs = buildTextMessage(
                text, TextMsgType.MSGTYPE_CHANNEL, nChannelID=self.my_channel_id
            )
            for m in msgs:
                r = self.doTextMessage(m)
                log("_send(%r) -> %s" % (text, r))
        except Exception as e:
            log("send error: %s" % e)

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
            "--extractor-args", "youtube:po_token_provider=bgutil:http",
        ]
        if COOKIES:
            cmd += ["--cookies", COOKIES]
        cmd += ["--", "ytsearch10:" + query]
        try:
            rc, out, err = self._run_ydl(cmd, timeout=90)
        except subprocess.TimeoutExpired:
            return []
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

    def _download_worker(self, url, title):
        real_url = url
        if url.startswith("ytsearch1:"):
            q = url.split(":", 1)[1]
            self.api_q.put(("status", "🔎 Ищу на YouTube: %s…" % q))
            items = self._yt_search_list(q)
            if not items:
                self.api_q.put(("download_fail", url, title, "Поиск не нашёл видео"))
                return
            real_url = items[0]["key"]
        ym_title = None
        if url.startswith("ymtrack:"):
            tid = url.split(":", 1)[1]
            self.api_q.put(("status", "🎵 Ищу трек на Яндекс.Музыке…"))
            real_url, ym_title = self._ym_resolve(tid)
            if not real_url:
                self.api_q.put(("download_fail", url, title, ym_title or "не нашёл"))
                return
            title = ym_title or title
        elif url.startswith("ymsearch1:"):
            q = url.split(":", 1)[1]
            self.api_q.put(("status", "🔎 Ищу на Яндекс.Музыке: %s…" % q))
            items = self._ym_search_list(q)
            if not items:
                self.api_q.put(("download_fail", url, title, "Ничего не нашёл на Яндекс.Музыке."))
                return
            real_url, ym_title = self._ym_resolve(items[0]["key"].split(":", 1)[1])
            if not real_url:
                self.api_q.put(("download_fail", url, title, ym_title or "не нашёл"))
                return
            title = ym_title or title
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
                    "--no-playlist",
                    "--no-simulate",
                    "-x",
                    "--audio-format", "mp3",
                    "--audio-quality", "5",
                    "--js-runtimes", "deno:/home/superlisa/.local/bin/deno",
                    "--remote-components", "ejs:github",
                    "--extractor-args", "youtube:po_token_provider=bgutil:http",
                    "-o", out + ".%(ext)s",
                    "--print", "%(title)s",
                ]
                ck = RUTUBE_COOKIES if "rutube.ru" in real_url else COOKIES
                if ck:
                    cmd += ["--cookies", ck]
                cmd += ["--", real_url]
                rc, stdout, stderr = self._run_ydl(cmd, timeout=600)
                mp3 = out + ".mp3"
                if rc != 0 or not os.path.exists(mp3):
                    err_text = (stderr or stdout or "yt-dlp failed").strip()
                    if "Sign in to confirm" in err_text or "LOGIN_REQUIRED" in err_text:
                        self.api_q.put(("download_fail", url, title, "YouTube заблокировал это видео на нашем сервере (Sign in to confirm you're not a bot). Попробуй другой запрос или прямую ссылку — популярные видео обычно играют."))
                        return
                    err_msg = (err_text.splitlines()[-1] if err_text else "yt-dlp failed")[:300]
                    if attempt < 3:
                        self.api_q.put(("status", "⚠ %s. Повтор %d/3 через 15с…" % (err_msg, attempt)))
                        time.sleep(15)
                        continue
                    self.api_q.put(("download_fail", url, title, err_msg))
                    return
                got_title = (stdout or "").strip().splitlines()
                real_title = got_title[0] if got_title else title
                self.api_q.put(("download_ok", url, real_title, mp3))
                return
            except subprocess.TimeoutExpired:
                if attempt < 3:
                    self.api_q.put(("status", "⚠ Таймаут скачивания. Повтор %d/3 через 15с…" % attempt))
                    time.sleep(15)
                    continue
                self.api_q.put(("download_fail", url, title, "timeout"))
                return
            except Exception as e:
                if attempt < 3:
                    self.api_q.put(("status", "⚠ Ошибка: %s. Повтор %d/3 через 15с…" % (str(e)[:120], attempt)))
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
        self.voice_stop.clear()
        self.voice_offset_base = int(offset_ms)
        self.voice_started_at = time.time()
        t = threading.Thread(
            target=self._voice_worker, args=(path, int(offset_ms)), daemon=True
        )
        self.voice_thread = t
        t.start()

    def _voice_worker(self, path, offset_ms):
        """Decode the audio file to raw PCM and feed it to TeamTalk as voice."""
        cmd = ["ffmpeg", "-y"]
        if offset_ms > 0:
            cmd += ["-ss", "%.3f" % (offset_ms / 1000.0)]
        cmd += ["-i", path, "-vn", "-f", "s16le", "-ac", "1", "-ar", str(VOICE_RATE)]
        if self.volume < 100:
            cmd += ["-af", "volume=%.3f" % (self.volume / 100.0)]
        cmd += ["-"]
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception as e:
            self.api_q.put(("voice_error", "ffmpeg: %s" % str(e)[:120]))
            return
        self.voice_proc = proc
        stream_id = int(time.time() * 1000) & 0xFFFF
        finished = False
        buf = b""
        block_dur = VOICE_CHUNK / float(VOICE_RATE)  # 0.02 s per 20 ms block
        next_slot = time.monotonic()
        try:
            while not self.voice_stop.is_set():
                # Feed blocks on a strict 20 ms schedule. A fixed sleep after
                # each insert drifts (read + insert take time) and stutters;
                # sleeping to the exact next slot keeps the stream smooth.
                if len(buf) < VOICE_CHUNK_BYTES:
                    r, _, _ = select.select([proc.stdout], [], [], 0.1)
                    if not r:
                        continue
                    data = proc.stdout.read(VOICE_CHUNK_BYTES)
                    if not data:
                        finished = True
                        break
                    buf += data
                    if len(buf) < VOICE_CHUNK_BYTES:
                        continue  # partial read: wait for a full block
                next_slot += block_dur
                delay = next_slot - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                else:
                    next_slot = time.monotonic()  # behind: don't compound backlog
                chunk = buf[:VOICE_CHUNK_BYTES]
                buf = buf[VOICE_CHUNK_BYTES:]
                raw = (ctypes.c_char * VOICE_CHUNK_BYTES).from_buffer_copy(chunk)
                ab = TeamTalk5.AudioBlock()
                ab.nStreamID = stream_id
                ab.nSampleRate = VOICE_RATE
                ab.nChannels = 1
                ab.lpRawAudio = ctypes.cast(raw, ctypes.c_void_p)
                ab.nSamples = VOICE_CHUNK
                ab.uStreamTypes = StreamType.STREAMTYPE_VOICE
                try:
                    self.insertAudioBlock(ab)
                except Exception as e:
                    log("insertAudioBlock err: %s" % e)
        except Exception as e:
            log("voice worker err: %s" % e)
        finally:
            if buf:
                pad = VOICE_CHUNK_BYTES - (len(buf) % VOICE_CHUNK_BYTES)
                if pad and pad != VOICE_CHUNK_BYTES:
                    buf += b"\x00" * pad
                for i in range(0, len(buf), VOICE_CHUNK_BYTES):
                    chunk = buf[i:i + VOICE_CHUNK_BYTES]
                    if len(chunk) < VOICE_CHUNK_BYTES:
                        chunk += b"\x00" * (VOICE_CHUNK_BYTES - len(chunk))
                    raw = (ctypes.c_char * VOICE_CHUNK_BYTES).from_buffer_copy(chunk)
                    ab = TeamTalk5.AudioBlock()
                    ab.nStreamID = stream_id
                    ab.nSampleRate = VOICE_RATE
                    ab.nChannels = 1
                    ab.lpRawAudio = ctypes.cast(raw, ctypes.c_void_p)
                    ab.nSamples = VOICE_CHUNK
                    ab.uStreamTypes = StreamType.STREAMTYPE_VOICE
                    try:
                        self.insertAudioBlock(ab)
                    except Exception as e:
                        log("insertAudioBlock err: %s" % e)
            try:
                self.insertAudioBlockEnd()
            except Exception:
                pass
            try:
                if proc.poll() is None:
                    proc.kill()
            except Exception:
                pass
            self.voice_proc = None
            if finished:
                self.api_q.put(("voice_finished", path))

    def _stop_voice(self):
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
        self.playing = True
        self.paused = False
        self.cur_offset_ms = 0
        self._send("▶ Сейчас играет: %s" % title)
        self._set_status("Playing: %s" % title)
        self._start_voice(path, 0)

    def _play_local(self, path, title):
        """Play a local file (no download) — used for files sent via Telegram."""
        self.auto_list = False
        self.queue.clear()
        self.downloading.clear()
        self._stop_voice()
        self.playing = False
        self.paused = False
        self.current = None
        self.current_orig = None
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
        self._send("⏸ Пауза (%s)" % _fmt_ms(self.cur_offset_ms))

    def _resume(self):
        if not self.playing or not self.paused or not self.current_orig:
            return
        self.paused = False
        self._set_status("Playing: %s" % (self.current[1] if self.current else ""))
        self._start_voice(self.current_orig, self.cur_offset_ms)
        self._send("▶ Продолжаю (%s)" % _fmt_ms(self.cur_offset_ms))

    def _pause_or_resume(self):
        if self.playing and self.paused:
            self._resume()
        elif self.playing:
            self._pause()
        else:
            self._send("Сейчас ничего не играет.")

    def _set_volume(self, v):
        v = max(1, min(100, v))
        self.volume = v
        self._send("🔊 Громкость: %d%%" % v)
        if self.playing and self.current_orig:
            self._restart_for_volume()

    def _restart_for_volume(self):
        orig = self.current_orig
        offset = self._elapsed_ms()
        self.api_q.put(("restart", orig, offset))

    def _switch_to(self, key, label):
        """Stop whatever plays and immediately play `key` (used by n/b and direct links)."""
        self.auto_list = False
        self.queue.clear()
        self._stop_voice()
        self.playing = False
        self.paused = False
        self.current = None
        self.current_orig = None
        self.cur_offset_ms = 0
        self._set_status("")
        self._enqueue_url(key, label)

    def _play_search_index(self, idx):
        if not self.search_results:
            self._send("Список результатов пуст. Сначала поищи: п <запрос>")
            return
        if idx < 0 or idx >= len(self.search_results):
            self._send("Нет результата под номером %d (всего %d)." % (idx + 1, len(self.search_results)))
            return
        self.search_index = idx
        item = self.search_results[idx]
        self._switch_to(item["key"], "🎵 %d. %s" % (idx + 1, item["title"]))
        self.auto_list = True

    def _do_search(self, query):
        self._send("🔎 Ищу: %s…" % query)
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

    def _advance(self):
        self._stop_voice()
        self.playing = False
        self.paused = False
        self.current = None
        self.current_orig = None
        self.cur_offset_ms = 0
        self.voice_offset_base = 0
        self.voice_started_at = 0
        self._set_status("")
        if self.queue:
            self.queue.pop(0)
        if self.queue:
            self._enqueue_next()
        elif self.auto_list and self.search_results and self.search_index + 1 < len(self.search_results):
            # auto-advance: keep playing the rest of the search-result list
            self.search_index += 1
            self._play_search_index(self.search_index)
        else:
            ended = self.auto_list
            self.auto_list = False
            self._send("⏹ Конец списка." if ended else "⏹ Очередь пуста.")

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
            self._send("Нет папки radio/ с m3u. Положи станции и перезапусти.")
            return
        if not arg:
            lines = ["📻 Радио (%d станций):" % len(self.radio)]
            for i, (t, _u) in enumerate(self.radio, 1):
                lines.append("%d. %s" % (i, t))
                if i >= 15:
                    lines.append("… и ещё %d" % (len(self.radio) - 15))
                    break
            lines.append("Радио <номер> — запуск")
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
            self._send("Не нашёл станцию «%s»." % arg)
            return
        lines = ["Нашёл:"]
        for idx, t in found[:10]:
            lines.append("%d. %s" % (idx, t))
        self._send("\n".join(lines) + "\nРадио <номер> — запуск")

    def _play_radio(self, url, title):
        """Play an internet radio stream directly (no download)."""
        self.auto_list = False
        self.queue.clear()
        self.downloading.clear()
        self._stop_voice()
        self.playing = False
        self.paused = False
        self.current = None
        self.current_orig = None
        self.cur_offset_ms = 0
        self._send("📻 ▶ %s" % title)
        self._set_status("Radio: %s" % title)
        self._start_voice(url, 0)

    def _handle_cmd(self, text, from_user):
        text = text.strip()
        low = text.lower()
        cmd = low[1:] if low.startswith("/") else low

        # --- громкость: v 100 / v 50 / громкость 30 / volume 80 ---
        m = re.match(r"^(?:v|громкость|громко|volume)\s+(\d{1,3})$", cmd)
        if m:
            self._set_volume(int(m.group(1)))
            return

        # --- выбор сервиса: sv yt / sv ym / sv ---
        first = cmd.split(None, 1)[0]
        if first in ("sv", "svc", "сервис"):
            parts = text.split(None, 1)
            if len(parts) < 2:
                cur = {"yt": "YouTube", "ym": "Яндекс.Музыка"}.get(self.service, "?")
                self._send("Сейчас: %s. Сменить: sv yt или sv ym." % cur)
                return
            svc = parts[1].strip().lower()
            if svc in ("yt", "youtube", "ютуб", "ютюб"):
                self.service = "yt"
                self._send("🎬 Сервис: YouTube.")
            elif svc in ("ym", "ya", "yandex", "яндекс", "яндекс музыка", "ямузыка"):
                self.service = "ym"
                self._send("🎵 Сервис: Яндекс.Музыка.")
            else:
                self._send("Не знаю сервис «%s». Доступно: yt (YouTube), ym (Яндекс.Музыка)." % svc)
            return

        # --- смена ника: cn <ник> ---
        if cmd == "cn" or cmd.startswith("cn "):
            parts = text.split(None, 1)
            if len(parts) < 2 or not parts[1].strip():
                self._send("Использование: cn <ник>, напр. cn музыкант")
                return
            nick = parts[1].strip()
            if len(nick) > 255:
                self._send("Ник слишком длинный (максимум 255 символов).")
                return
            self.nickname = nick
            try:
                with open(NICKNAME_FILE, "w") as f:
                    f.write(nick)
            except Exception as e:
                log("nickname save err: %s" % e)
            self.doChangeNickname(nick)
            self._send("✅ Ник: %s" % nick)
            return

        # --- стоп / скип / очередь / статус / помощь ---
        if cmd in ("с", "s", "стоп", "останови", "stop"):
            self.queue.clear()
            self.downloading.clear()
            self.auto_list = False
            self._stop_voice()
            self.playing = False
            self.paused = False
            self.current = None
            self.current_orig = None
            self.cur_offset_ms = 0
            self._set_status("")
            self._send("⏹ Стоп.")
            return

        if cmd in ("скип", "дальше", "след", "следующий", "skip"):
            self._advance()
            return

        if cmd in ("очередь", "queue", "q"):
            self._queue_cmd()
            return

        if cmd in ("статус", "status", "now"):
            self._status_cmd()
            return

        if cmd in ("помощь", "help", "h", "команды", "commands"):
            self._help_cmd()
            return

        # --- локальный файл: lf <путь> (из Telegram-моста) ---
        if cmd.startswith("lf ") or cmd.startswith("файл ") or cmd.startswith("локальный "):
            path = text.split(None, 1)[1].strip()
            if not os.path.isfile(path):
                self._send("Файл не найден: %s" % path)
                return
            self._play_local(path, os.path.basename(path))
            return

        # --- n/b: следующий/предыдущий по списку результатов поиска ---
        if cmd in ("n", "н", "next"):
            self._play_search_index(self.search_index + 1)
            return

        if cmd in ("b", "back", "назад"):
            self._play_search_index(self.search_index - 1)
            return

        # --- радио: радио / радио <N> / радио <текст> ---
        if cmd.startswith("радио") or cmd.startswith("radio") or cmd == "r":
            arg = text.split(None, 1)[1].strip() if " " in text else ""
            self._radio_cmd(arg)
            return

        # --- play: bare «пи»/«play» = продолжить, если пауза ---
        if cmd in ("пи", "pi", "play", "плей", "играй"):
            if self.paused:
                self._resume()
            elif self.playing:
                self._send("Уже играет. с — стоп, скип — дальше.")
            else:
                self._send("Что играем? п <запрос> или ссылка.")
            return

        if cmd in ("п", "p"):
            self._pause_or_resume()
            return

        # --- смена канала ---
        if cmd.startswith("channel"):
            parts = text.split(None, 1)
            if len(parts) < 2:
                self._send("Использование: /channel <путь>, напр. /channel /root/music")
                return
            path = parts[1].strip()
            cid = self.getChannelIDFromPath(path)
            if not cid:
                self._send("Канал не найден: %s" % path)
                return
            self.doJoinChannelByID(cid, "")
            self.play_channel_id = cid
            self._send("Перехожу в канал: %s" % path)
            return

        # --- играть: п <запрос>, пи <запрос>, play <запрос>, /play <запрос|ссылка> ---
        m = re.match(r"^(?:п|p|пи|pi|play|плей|играй|найди)\s+(\S.*)$", low)
        if m or cmd.startswith("play"):
            query = m.group(1) if m else (text.split(None, 1)[1] if " " in text else None)
            if not query:
                self._send("Дай ссылку или запрос: п <запрос>, /play <ссылка>.")
                return
            u = URL_RE.search(query)
            if u:
                self._switch_to(u.group(0), u.group(0))
            else:
                self._do_search(query)
            return

        # --- играть по прямой ссылке независимо от сервиса: u <url> / ссылка <url> / link <url> ---
        m = re.match(r"^(?:u|ссылка|link|url)\s+(\S.*)$", low)
        if m:
            u = URL_RE.search(m.group(1))
            if not u:
                self._send("Дай ссылку: u https://…")
                return
            self._switch_to(u.group(0), u.group(0))
            return

        # --- bare ссылка ---
        m = URL_RE.search(text)
        if m and not low.startswith("/"):
            self._switch_to(m.group(0), m.group(0))
            return

    def _enqueue_url(self, url, label):
        if url in [u for u, _ in self.queue]:
            self._send("Уже в очереди: %s" % label)
            return
        self.queue.append((url, label))
        self._send("➕ %s" % label)
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
            lines = ["▶ Играет: %s" % self.current[1]]
            lines.append("⏱ %s" % _fmt_ms(self._elapsed_ms()))
            if self.paused:
                lines.append("⏸ Пауза")
            if self.volume < 100:
                lines.append("🔊 %d%%" % self.volume)
        else:
            lines = ["Ничего не играет."]
        self._send("\n".join(lines))

    def _help_cmd(self):
        self._send(
            "п <запрос> — поиск (покажет список), играет №1\n"
            "n — следующий по списку, b — предыдущий\n"
            "пи — play, п — пауза\n"
            "с — стоп, скип — дальше (очередь)\n"
            "u <url> / ссылка <url> — играть по ссылке (независимо от сервиса)\n"
            "радио — список станций (радио <номер> — запуск)\n"
            "v <1-100> — громкость\n"
            "sv yt / sv ym — сервис\n"
            "cn <ник> — сменить ник бота\n"
            "lf <путь> — играть локальный файл\n"
            "очередь, статус, помощь\n"
            "/channel <путь> — сменить канал"
        )

    # ----- events ----------------------------------------------------
    def onConnectSuccess(self):
        log("connected, logging in")
        self._login()

    def onConnectFailed(self):
        log("connect failed")
        self._schedule_reconnect()

    def onConnectionLost(self):
        log("connection lost")
        self.playing = False
        self.logged_in = False
        self.joined = False
        self._schedule_reconnect()

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
        log("joining channel id %d (path %s)" % (cid, target))
        self.doJoinChannelByID(cid, "")

    def onCmdUserJoinedChannel(self, user):
        try:
            if user.nUserID == self.my_user_id:
                self.my_channel_id = user.nChannelID
                log("joined channel id %d" % self.my_channel_id)
        except Exception:
            pass

    def onCmdSuccess(self, cmdId):
        pass

    def onCmdError(self, cmdId, errmsg):
        msg = errmsg.szErrorMsg if errmsg else ""
        if isinstance(msg, bytes):
            msg = msg.decode("utf-8", "ignore")
        log("cmd error (cmd %d): %s" % (cmdId, msg))
        now = time.time()
        if now - self._last_err_sent > 5:
            self._last_err_sent = now
            self._send("⚠ Ошибка: %s" % msg)

    def onCmdUserTextMessage(self, textmessage):
        if not textmessage:
            return
        if textmessage.nFromUserID == self.my_user_id:
            return
        try:
            msg = textmessage.szMessage
            if isinstance(msg, bytes):
                msg = msg.decode("utf-8", "ignore")
            msg = msg.replace("\x00", "").strip()
            if not msg:
                return
            log("msg from %d: %s" % (textmessage.nFromUserID, msg))
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

    def _schedule_reconnect(self):
        if not self.reconnect:
            return
        def do_reconnect():
            time.sleep(10)
            self.api_q.put(("reconnect",))
        threading.Thread(target=do_reconnect, daemon=True).start()

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
                        else:
                            self._send("⬇ Скачано (в очереди): %s" % title)
                    # else: stale download (superseded by n/b switch) — drop
                elif kind == "download_fail":
                    _, url, title, err = item
                    if url in self.downloading:
                        self.downloading.discard(url)
                    # remove from queue if first
                    if self.queue and self.queue[0][0] == url:
                        self.queue.pop(0)
                        self._send("⚠ Не удалось скачать %s: %s" % (url, err))
                        self._enqueue_next()
                    else:
                        self._send("⚠ Ошибка скачивания: %s" % err)
                elif kind == "search_done":
                    _, query, items = item
                    if not items:
                        self._send("Ничего не нашёл по запросу «%s»." % query)
                        return
                    self.search_results = items
                    self.search_index = 0
                    lines = ["Результаты (%d):" % len(items)]
                    for i, it in enumerate(items[:10], 1):
                        lines.append("%d. %s" % (i, it["title"][:60]))
                    self._send("\n".join(lines))
                    self._play_search_index(0)
                elif kind == "advance":
                    self._advance()
                elif kind == "voice_finished":
                    self._advance()
                elif kind == "voice_error":
                    _, msg = item
                    self._send("⚠ Голос: %s" % msg)
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
                elif kind == "reconnect":
                    if self.reconnect and not self.connecting:
                        self.connecting = True
                        log("reconnecting")
                        self.connect(HOST, TCP_PORT, UDP_PORT)
                        self.connecting = False
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
    bot.run()


if __name__ == "__main__":
    main()
