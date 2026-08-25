"""tt_agent: persistent TeamTalk agent. Stays in channel, transcribes voice, plays replies.

Voice bridge (двухсторонние реплики):
- Captures each spoken utterance in the channel (energy VAD) and sends it to the
  owner's Telegram PM as a voice note — he hears the channel without logging in.
- Watches INBOX dir for .ogg/.wav files -> plays them into the channel (so the
  owner can drop a voice reply onto the server). The music bot already routes the
  owner's Telegram voice messages here via the same inbox.
"""
import os
import sys
import glob
import json
import time
import signal
import subprocess
import threading
import ctypes
import numpy as np

SDK = "/home/bot/teamtalk-music-bot/sdk/tt5sdk_v5.22a_ubuntu22_x86_64"
sys.path.insert(0, os.path.join(SDK, "Library", "TeamTalkPy"))
os.environ["LD_LIBRARY_PATH"] = os.path.join(SDK, "Library", "TeamTalk_DLL") + ":" + os.environ.get("LD_LIBRARY_PATH", "")

import TeamTalk5  # noqa: E402
from TeamTalk5 import StreamType, TextMsgType, buildTextMessage  # noqa: E402
import TeamTalk5 as TT  # noqa: E402

HOST = b"ranjerserver.ru"
TCP = 10989
UDP = 10989
USER = b"1"
PW = b"1"
CLIENT = b"py-agent-tt"
MODEL = "/home/bot/tt-models/vosk-model-small-ru-0.22"
MUSIC_NICKS = ("агригатор", "universal plugin")
CHANNEL = 1

OWNER_TG_CHAT = 1789080411  # owner's Telegram PM (audio goes here)

# VAD utterance capture
VAD_THRESH = 300.0        # RMS on 16k s16 speech vs silence
PREROLL_CHUNKS = 3        # 0.3 s pre-roll before speech onset
VAD_END_CHUNKS = 8        # 0.8 s of silence ends an utterance
VAD_MAX_BYTES = 30 * 16000 * 2  # hard cap ~30 s
MIN_UTT_SEC = 0.5         # drop blips shorter than this

# read TG_TOKEN from /home/bot/.secrets/.env
TG_TOKEN = ""
try:
    for _l in open("/home/bot/.secrets/.env", encoding="utf-8"):
        _l = _l.strip()
        if _l.startswith("TG_TOKEN="):
            TG_TOKEN = _l.split("=", 1)[1].strip().strip('"').strip("'")
            break
except Exception:
    pass

BASE = "/home/bot/tt-agent"
PHRASES = os.path.join(BASE, "phrases.txt")
INBOX = os.path.join(BASE, "inbox")
LOG = os.path.join(BASE, "agent.log")

VOICE_RATE = 48000
VOICE_CHUNK = 960  # 20 ms


def _b(s):
    return s.encode("utf-8") if isinstance(s, str) else s


def _f(user, name):
    v = getattr(user, name, "") or ""
    if isinstance(v, bytes):
        v = v.decode("utf-8", "ignore")
    return v.replace("\x00", "").strip()


def log(line):
    line = line.strip()
    try:
        print(line, flush=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(time.strftime("%H:%M:%S") + " " + line + "\n")
    except Exception:
        pass


class Agent(TeamTalk5.TeamTalk):
    def __init__(self, model):
        super().__init__()
        self.model = model
        self.my_id = 0
        self.joined = threading.Event()
        self.users = {}
        self.rec = {}      # uid -> KaldiRecognizer
        self.pcm16 = {}    # uid -> bytearray 16k s16
        self.utt = {}      # uid -> VAD state for utterance capture
        self.last_phrase = {}   # uid -> (text, ts)
        self.reconnect_req = False
        self.connected = False
        self.last_play = 0
        self.silent_since = time.time()
        self.astat = {}    # uid -> {"b": blocks, "s": samples, "t": last_log}

    # ---- connection ----
    def onConnectSuccess(self):
        self.connected = True
        self.doLogin(_b("агент Кирилла"), USER, PW, CLIENT)

    def onCmdMyselfLoggedIn(self, userid, useraccount):
        self.my_id = userid
        self.doJoinChannelByID(CHANNEL, b"")

    def onConnectionLost(self):
        log("[WARN] connection lost, scheduling reconnect")
        self.connected = False
        self.reconnect_req = True

    def onCmdServerKickUser(self, source, user):
        log("[KICK] got kicked, reconnect")
        self.reconnect_req = True

    # ---- users ----
    def onCmdUserLoggedIn(self, u):
        if u.nUserID != self.my_id:
            self.users[u.nUserID] = u

    def onCmdUserJoinedChannel(self, u):
        if u.nUserID == self.my_id:
            self.joined.set()
            for uid in list(self.users.keys()):
                self._sub(uid)
            return
        self.users[u.nUserID] = u
        self._sub(u.nUserID)

    def onCmdUserLeftChannel(self, source, u):
        self._drop(u.nUserID)

    def onCmdUserLoggedOut(self, u):
        self._drop(u.nUserID)

    def _drop(self, uid):
        self.rec.pop(uid, None)
        self.pcm16.pop(uid, None)
        self.utt.pop(uid, None)

    def onCmdUserTextMessage(self, tm):
        if not tm or tm.nFromUserID == self.my_id:
            return
        msg = tm.szMessage
        if isinstance(msg, bytes):
            msg = msg.decode("utf-8", "ignore")
        msg = msg.replace("\x00", "").strip()
        if not msg:
            return
        kind = "ЧАТ" if int(getattr(tm, "nMsgType", 0) or 0) == 2 else "ЛС"
        line = "[%s %s] %s: %s" % (kind, time.strftime("%H:%M:%S"),
                                   self._nick(tm.nFromUserID), msg)
        print(line, flush=True)
        try:
            with open(PHRASES, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

    def _nick(self, uid):
        return _f(self.users.get(uid), "szNickname") or _f(self.users.get(uid), "szUsername") or "id%d" % uid

    def _is_skip(self, uid):
        return uid == self.my_id or self._nick(uid) in MUSIC_NICKS

    def _sub(self, uid):
        if uid in self.rec or self._is_skip(uid):
            return
        try:
            from vosk import KaldiRecognizer
            self.rec[uid] = KaldiRecognizer(self.model, 16000)
            self.pcm16[uid] = bytearray()
            ok = self.enableAudioBlockEvent(uid, StreamType.STREAMTYPE_VOICE, True)
            log("[SUB] uid=%d %s ev=%s" % (uid, self._nick(uid), ok))
        except Exception as e:
            log("[SUB-ERR] uid=%d %r" % (uid, e))

    # ---- audio in ----
    def onUserAudioBlock(self, nUserID, nStreamType):
        if nUserID not in self.rec:
            return
        p = self.acquireUserAudioBlock(StreamType.STREAMTYPE_VOICE, nUserID)
        if not p:
            return
        try:
            ab = p.contents
            rate = ab.nSampleRate or 48000
            ch = ab.nChannels or 1
            nbytes = ab.nSamples * ch * 2
            data = ctypes.string_at(ab.lpRawAudio, nbytes)
        finally:
            self.releaseUserAudioBlock(p)
        try:
            self._feed(nUserID, data, rate, ch)
        except Exception:
            pass

    def _feed(self, uid, data, rate, ch):
        rec = self.rec[uid]
        buf = self.pcm16[uid]
        a = np.frombuffer(data, dtype=np.int16)
        if ch > 1:
            a = a.reshape(-1, ch).mean(axis=1).astype(np.int16)
        if rate == 48000:
            a = a[::3]
        elif rate == 44100:
            dst = np.linspace(0, len(a) - 1, int(len(a) * 16000 / 44100))
            a = np.interp(dst, np.arange(len(a)), a.astype(np.float64)).astype(np.int16)
        st = self.astat.setdefault(uid, {"b": 0, "s": 0, "t": 0})
        st["b"] += 1
        st["s"] += len(a)
        now = time.time()
        if now - st["t"] > 5:
            st["t"] = now
            log("[AUDIO] uid=%d %s blocks=%d samples16k=%d" % (uid, self._nick(uid), st["b"], st["s"]))
        # voice bridge: capture utterances -> audio to owner's Telegram
        try:
            self._vad(uid, a)
        except Exception as e:
            log("[VAD-ERR] uid=%d %r" % (uid, e))
        # vosk transcription (keeps phrases.txt alive)
        buf += a.tobytes()
        while len(buf) >= 1600 * 2:
            chunk = bytes(buf[:1600 * 2])
            del buf[:1600 * 2]
            if rec.AcceptWaveform(chunk):
                res = json.loads(rec.Result())
                text = res.get("text", "").strip()
                if text:
                    self._emit(uid, text)

    # ---- voice bridge: VAD utterance capture ----
    def _vad(self, uid, a):
        st = self.utt.setdefault(uid, {"buf": bytearray(), "speech": False,
                                       "sil": 0, "tail": [], "start": 0})
        st["tail"].append(a.tobytes())
        if len(st["tail"]) > PREROLL_CHUNKS:
            del st["tail"][:-PREROLL_CHUNKS]
        if len(a) == 0:
            return
        rms = float(np.sqrt(np.mean(np.square(a.astype(np.float32)))))
        if rms >= VAD_THRESH:
            if not st["speech"]:
                st["speech"] = True
                st["buf"] = bytearray(b"".join(st["tail"]))
                st["start"] = time.time()
            st["buf"] += a.tobytes()
            st["sil"] = 0
        elif st["speech"]:
            st["buf"] += a.tobytes()
            st["sil"] += 1
            if st["sil"] >= VAD_END_CHUNKS or len(st["buf"]) >= VAD_MAX_BYTES:
                self._finalize_utt(uid, st)
                st["speech"] = False
                st["buf"] = bytearray()
                st["sil"] = 0

    def _finalize_utt(self, uid, st):
        buf = st["buf"]
        dur = len(buf) / (16000.0 * 2)
        if dur < MIN_UTT_SEC:
            return
        nick = self._nick(uid)
        now = time.strftime("%H:%M:%S")
        caption = "%s %s" % (nick, now)
        lt, lts = self.last_phrase.get(uid, ("", 0))
        if lt and time.time() - lts < 8:
            caption += ": " + lt[:120]
        log("[VOICE->TG] uid=%d %s %.1fs -> %s" % (uid, nick, dur, OWNER_TG_CHAT))
        ogg = self._pcm_to_ogg(bytes(buf))
        if ogg:
            self._tg_send_voice(ogg, caption)

    def _pcm_to_ogg(self, pcm):
        try:
            p = subprocess.run(["ffmpeg", "-v", "error", "-f", "s16le", "-ar", "16000",
                                "-ac", "1", "-i", "-", "-c:a", "libopus", "-f", "ogg", "-"],
                               input=pcm, capture_output=True, timeout=30)
            return p.stdout or None
        except Exception as e:
            log("[OGG-ERR] %r" % e)
            return None

    def _tg_send_voice(self, ogg, caption):
        if not TG_TOKEN:
            return
        try:
            import requests
            r = requests.post("https://api.telegram.org/bot%s/sendVoice" % TG_TOKEN,
                              data={"chat_id": OWNER_TG_CHAT, "caption": caption[:200]},
                              files={"voice": ("utt.ogg", ogg, "audio/ogg")}, timeout=30)
            j = r.json()
            if not j.get("ok"):
                # fallback: voice notes may be forbidden; send as audio
                log("[VOICE-SEND-ERR] %s" % (j.get("description") or "?"))
                r2 = requests.post("https://api.telegram.org/bot%s/sendAudio" % TG_TOKEN,
                                   data={"chat_id": OWNER_TG_CHAT, "caption": caption[:200]},
                                   files={"audio": ("utt.ogg", ogg, "audio/ogg")}, timeout=30)
                j2 = r2.json()
                if not j2.get("ok"):
                    log("[AUDIO-SEND-ERR] %s" % (j2.get("description") or "?"))
        except Exception as e:
            log("[VOICE-SEND-ERR] %r" % e)

    def _emit(self, uid, text):
        now = time.time()
        last_t, last_s = self.last_phrase.get(uid, ("", 0))
        if text == last_t and now - last_s < 2.5:
            return  # duplicate fragment
        self.last_phrase[uid] = (text, now)
        line = "[%s] %s: %s" % (time.strftime("%H:%M:%S"), self._nick(uid), text)
        print(line, flush=True)
        try:
            with open(PHRASES, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

    # ---- audio out (play inbox files) ----
    def play_file(self, path):
        if not os.path.exists(path):
            return
        try:
            cmd = ["ffmpeg", "-v", "error", "-i", path, "-vn", "-f", "s16le",
                   "-ac", "1", "-ar", str(VOICE_RATE), "-"]
            p = subprocess.run(cmd, capture_output=True, timeout=120)
            data = p.stdout
            if not data:
                log("[PLAY-ERR] no audio from %s" % os.path.basename(path))
                return
            # scale to ~80%
            a = np.frombuffer(data, dtype=np.int16).astype(np.float32) * 0.8
            a = np.clip(a, -32768, 32767).astype(np.int16)
            data = a.tobytes()
            stream_id = int(time.time() * 1000) & 0xFFFF
            written = 0
            while written < len(data):
                chunk = data[written:written + VOICE_CHUNK * 2]
                if len(chunk) < VOICE_CHUNK * 2:
                    chunk += b"\x00" * (VOICE_CHUNK * 2 - len(chunk))
                raw = (ctypes.c_char * (VOICE_CHUNK * 2)).from_buffer_copy(chunk)
                ab = TeamTalk5.AudioBlock()
                ab.nStreamID = stream_id
                ab.nSampleRate = VOICE_RATE
                ab.nChannels = 1
                ab.lpRawAudio = ctypes.cast(raw, ctypes.c_void_p)
                ab.nSamples = VOICE_CHUNK
                ab.uStreamTypes = StreamType.STREAMTYPE_VOICE
                self.insertAudioBlock(ab)
                written += VOICE_CHUNK * 2
                time.sleep(0.02)
            TeamTalk5._InsertAudioBlock(self._tt, None)
            log("[PLAY] %s (%.1fs)" % (os.path.basename(path), len(data) / (VOICE_RATE * 2)))
        except Exception as e:
            log("[PLAY-ERR] %s: %r" % (os.path.basename(path), e))
        finally:
            try:
                os.remove(path)
            except Exception:
                pass

    def send_text_file(self, path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read().strip()
            if text:
                for m in buildTextMessage(text, TextMsgType.MSGTYPE_CHANNEL, 0, CHANNEL):
                    self.doTextMessage(m)
                log("[SEND] %r" % text)
        except Exception as e:
            log("[SEND-ERR] %r" % e)
        finally:
            try:
                os.remove(path)
            except Exception:
                pass

    def scan_inbox(self):
        for path in sorted(glob.glob(os.path.join(INBOX, "*.txt"))):
            self.send_text_file(path)
        for path in sorted(glob.glob(os.path.join(INBOX, "*.ogg")) +
                           sorted(glob.glob(os.path.join(INBOX, "*.wav")))):
            self.play_file(path)


def main():
    from vosk import Model
    os.makedirs(INBOX, exist_ok=True)
    os.makedirs(BASE, exist_ok=True)
    agent = Agent(Model(MODEL))
    stop = threading.Event()

    def _stop(*_):
        stop.set()
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    log("=== tt-agent start (voice bridge) ===")
    last_try = 0
    while not stop.is_set():
        try:
            if agent.reconnect_req:
                agent.reconnect_req = False
                agent.joined.clear()
                agent.connected = False
                try:
                    agent.disconnect()
                except Exception:
                    pass
                log("[RECONNECT]")
                last_try = time.time()
                time.sleep(2)
                continue
            if not agent.joined.is_set():
                if agent.connected:
                    agent.runEventLoop(100)
                    time.sleep(0.05)
                    continue
                if time.time() - last_try < 5:
                    agent.runEventLoop(50)
                    time.sleep(1)
                    continue
                last_try = time.time()
                if not agent.connect(HOST, TCP, UDP):
                    log("[ERR] connect failed, retry in 5s")
                    continue
                # init virtual sound device so incoming voice audio flows to us
                agent.initSoundOutputDevice(TT.TT_SOUNDDEVICE_ID_TEAMTALK_VIRTUAL)
                agent.runEventLoop(100)
                time.sleep(0.05)
                continue
            # joined: run loop + scan inbox periodically
            agent.runEventLoop(300)
            agent.scan_inbox()
            time.sleep(0.05)
        except Exception as e:
            log("[ERR] %r" % e)
            time.sleep(3)
    try:
        agent.doLogout()
        time.sleep(0.3)
        agent.runEventLoop(50)
    except Exception:
        pass
    agent.disconnect()
    log("=== tt-agent stop ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
