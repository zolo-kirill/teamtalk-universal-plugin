#!/usr/bin/env python3
"""Silero TTS worker.

Живёт отдельным процессом: torch рядом с TeamTalk SDK в одном процессе
роняет бота в segfault (проверено). Бот общается с ним по unix-сокету:

    TEXT \t <speaker> \t <base64 текста> \n   -> OK <путь к raw> | ERR <текст>
    PING \n                                    -> PONG

Результат кэшируется в cache/greet_<hash>.raw (s16le, 48 кГц, стерео).
"""
import base64
import hashlib
import os
import socket
import sys
import threading

BASE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(BASE, "cache")
os.makedirs(CACHE, exist_ok=True)
SOCK = os.environ.get("TTS_SOCK") or os.path.join(CACHE, "tts.sock")
RATE = 48000
CHANNELS = 2

_model = None
_lock = threading.Lock()


def _key(speaker, text):
    return hashlib.sha1(("%s|%s" % (speaker, text)).encode("utf-8")).hexdigest()[:16]


def load_model():
    """Загрузить модель один раз (в фоне, чтобы сокет уже принимал запросы)."""
    global _model
    try:
        import torch  # noqa: F401  (тяжёлый импорт — только здесь)
        with _lock:
            if _model is not None:
                return
            model, _ = torch.hub.load(
                repo_or_dir="snakers4/silero-models",
                model="silero_tts",
                language="ru",
                speaker="v4_ru",
                trust_repo=True,
            )
            model.to(torch.device("cpu"))
            _model = model
        print("tts worker: модель загружена", flush=True)
    except Exception as e:  # noqa: BLE001
        print("tts worker: ошибка загрузки: %s" % str(e)[:300], flush=True)


def synth(text, speaker):
    """Синтез текста -> путь к raw-файлу. Кэш по паре (голос, текст)."""
    raw = os.path.join(CACHE, "greet_%s.raw" % _key(speaker, text))
    if os.path.exists(raw) and os.path.getsize(raw) > 1000:
        return raw
    import numpy as np
    with _lock:
        model = _model
    if model is None:
        load_model()
        with _lock:
            model = _model
    if model is None:
        raise RuntimeError("модель не загрузилась")
    audio = model.apply_tts(text=text, speaker=speaker, sample_rate=RATE)
    arr = audio.detach().cpu().numpy().astype("float32").reshape(-1)
    pcm = (arr * 32767.0).clip(-32768, 32767).astype("<i2")
    if CHANNELS == 2:
        pcm = np.repeat(pcm, 2)
    tmp = raw + ".tmp"
    with open(tmp, "wb") as f:
        f.write(pcm.tobytes())
    os.replace(tmp, raw)
    dur = len(pcm) / float(RATE * CHANNELS)
    print("tts worker: синтез %d симв. -> %.1f с" % (len(text), dur), flush=True)
    return raw


def handle(conn):
    try:
        f = conn.makefile("rwb")
        for line in f:
            line = line.strip()
            if not line:
                continue
            if line == b"PING":
                f.write(b"PONG\n")
                f.flush()
                continue
            try:
                parts = line.split(b"\t")
                if len(parts) != 3 or parts[0] != b"TEXT":
                    f.write(b"ERR bad request\n")
                    f.flush()
                    continue
                speaker = parts[1].decode("ascii", "replace").strip() or "baya"
                text = base64.b64decode(parts[2]).decode("utf-8")
                path = synth(text, speaker)
                f.write(("OK %s\n" % path).encode("utf-8"))
                f.flush()
            except Exception as e:  # noqa: BLE001
                msg = str(e)[:200].replace("\n", " ")
                f.write(("ERR %s\n" % msg).encode("utf-8"))
                f.flush()
    except Exception as e:  # noqa: BLE001
        print("tts worker: соединение: %s" % str(e)[:150], flush=True)
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def main():
    try:
        os.unlink(SOCK)
    except FileNotFoundError:
        pass
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCK)
    os.chmod(SOCK, 0o600)
    srv.listen(8)
    print("tts worker: сокет %s" % SOCK, flush=True)
    threading.Thread(target=load_model, daemon=True).start()
    while True:
        try:
            conn, _ = srv.accept()
        except KeyboardInterrupt:
            break
        threading.Thread(target=handle, args=(conn,), daemon=True).start()


if __name__ == "__main__":
    sys.exit(main())
