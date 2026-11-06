#!/usr/bin/env python3
"""Silero TTS worker (двуязычный).

Живёт отдельным процессом: torch рядом с TeamTalk SDK в одном процессе
роняет бота в segfault (проверено). Бот общается с ним по unix-сокету:

    TEXT \t <speaker> \t <base64 текста> \n   -> OK <путь к raw> | ERR <текст>
    PING \n                                    -> PONG

Две модели: v4_ru для кириллицы и v3_en для латиницы. Текст режется на
куски по азбуке, каждый кусок озвучивается своей моделью и склеивается
в один PCM (s16le, 48 кГц, стерео) с короткой паузой между кусками.
Результат кэшируется в cache/greet_<hash>.raw.
"""
import base64
import hashlib
import os
import re
import socket
import sys
import threading

BASE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(BASE, "cache")
os.makedirs(CACHE, exist_ok=True)
SOCK = os.environ.get("TTS_SOCK") or os.path.join(CACHE, "tts.sock")
RATE = 48000
CHANNELS = 2
GAP_MS = 80                      # пауза между кусками на разных языках

# Английский голос: можно переопределить через окружение или запросом бота
EN_SPEAKER_DEFAULT = os.environ.get("TTS_EN_SPEAKER", "en_21").strip() or "en_21"
RU_MODEL = ("ru", "v4_ru")
EN_MODEL = ("en", "v3_en")


_models = {"ru": None, "en": None}
_en_speaker = None
_lock = threading.Lock()


def _key(speaker, en_speaker, text):
    return hashlib.sha1(("n2|%s|%s|%s" % (speaker, en_speaker or "", text)).encode("utf-8")).hexdigest()[:16]


def _load(lang):
    """Загрузить модель языка один раз. Возвращает модель или None."""
    global _en_speaker
    name, speaker = RU_MODEL if lang == "ru" else EN_MODEL
    with _lock:
        if _models[lang] is not None:
            return _models[lang]
        try:
            import torch  # noqa: F401  (тяжёлый импорт — только здесь)
        except Exception as e:  # noqa: BLE001
            print("tts worker: нет torch: %s" % str(e)[:200], flush=True)
            return None
        try:
            model, _ = torch.hub.load(
                repo_or_dir="snakers4/silero-models",
                model="silero_tts",
                language=name,
                speaker=speaker,
                trust_repo=True,
            )
            model.to(torch.device("cpu"))
            _models[lang] = model
        except Exception as e:  # noqa: BLE001
            print("tts worker: модель %s не загрузилась: %s" % (lang, str(e)[:300]), flush=True)
            return None
    if lang == "en":
        try:
            speakers = list(getattr(model, "speakers", []) or [])
        except Exception:  # noqa: BLE001
            speakers = []
        if speakers:
            _en_speaker = EN_SPEAKER_DEFAULT if EN_SPEAKER_DEFAULT in speakers else speakers[0]
            print("tts worker: en голос %s (из %d)" % (_en_speaker, len(speakers)), flush=True)
    print("tts worker: модель %s загружена" % lang, flush=True)
    return model


def load_models():
    """Фоновая загрузка обеих моделей."""
    _load("ru")
    _load("en")


def split_segments(text):
    """Разбить фразу на куски по азбуке: [(lang, кусок), …].

    Слово с латиницей -> en, с кириллицей -> ru. Числа и пунктуация
    остаются за предыдущим языком, чтобы куски не дробились по запятым.
    """
    text = text or ""
    words = list(re.finditer(r"\S+", text))
    if not words:
        return []
    out = []
    cur = None
    start = end = 0
    for m in words:
        w = m.group(0)
        if re.search(r"[A-Za-z]", w):
            lang = "en"
        elif re.search(r"[А-Яа-яЁё]", w):
            lang = "ru"
        else:
            lang = cur or "ru"
        if cur is None or lang != cur:
            if cur is not None:
                out.append((cur, text[start:end]))
            cur = lang
            start = m.start()
        end = m.end()
    out.append((cur, text[start:end]))
    return [(lang, chunk) for lang, chunk in out if chunk.strip()]



# ---------------------------------------------------------------------------
# Числительные словами. Silero v4_ru и v3_en не читают цифры: "Сейчас 12
# часов 30 минут" звучало как "Сейчас часов минут" (проверено по длительности:
# 4.4 с против 5.2 с у варианта словами). Поэтому перед синтезом числа
# заменяются словами на языке того куска, в котором стоят.
# ---------------------------------------------------------------------------
RU_NUM_WORDS = [
    'ноль', 'один', 'два', 'три', 'четыре', 'пять', 'шесть', 'семь', 'восемь', 'девять',
    'десять', 'одиннадцать', 'двенадцать', 'тринадцать', 'четырнадцать', 'пятнадцать',
    'шестнадцать', 'семнадцать', 'восемнадцать', 'девятнадцать',
]
RU_TENS = ['', '', 'двадцать', 'тридцать', 'сорок', 'пятьдесят', 'шестьдесят',
           'семьдесят', 'восемьдесят', 'девяносто']
RU_HUNDREDS = ['', 'сто', 'двести', 'триста', 'четыреста', 'пятьсот', 'шестьсот',
               'семьсот', 'восемьсот', 'девятьсот']
RU_FEM = {'один': 'одна', 'два': 'две'}

EN_NUM_WORDS = [
    'zero', 'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine',
    'ten', 'eleven', 'twelve', 'thirteen', 'fourteen', 'fifteen', 'sixteen',
    'seventeen', 'eighteen', 'nineteen',
]
EN_TENS = ['', '', 'twenty', 'thirty', 'forty', 'fifty', 'sixty', 'seventy',
           'eighty', 'ninety']


def _ru_plural(n, one, few, many):
    n = abs(int(n)) % 100
    if 11 <= n <= 19:
        return many
    n %= 10
    if n == 1:
        return one
    if 2 <= n <= 4:
        return few
    return many


def _ru_triple(n, fem=False):
    words = []
    h, rem = divmod(n, 100)
    if h:
        words.append(RU_HUNDREDS[h])
    if rem < 20:
        if rem:
            w = RU_NUM_WORDS[rem]
            words.append(RU_FEM.get(w, w) if fem else w)
    else:
        words.append(RU_TENS[rem // 10])
        u = rem % 10
        if u:
            w = RU_NUM_WORDS[u]
            words.append(RU_FEM.get(w, w) if fem else w)
    return words


def ru_number(n, fem_units=False):
    n = int(n)
    if n == 0:
        return ['ноль']
    out = []
    millions, rest = divmod(n, 1000000)
    thousands, units = divmod(rest, 1000)
    if millions:
        out += _ru_triple(millions)
        out.append(_ru_plural(millions, 'миллион', 'миллиона', 'миллионов'))
    if thousands:
        out += _ru_triple(thousands, fem=True)
        out.append(_ru_plural(thousands, 'тысяча', 'тысячи', 'тысяч'))
    if units:
        out += _ru_triple(units, fem=fem_units)
    return out


def _en_triple(n):
    words = []
    h, rem = divmod(n, 100)
    if h:
        words += [EN_NUM_WORDS[h], 'hundred']
    if rem < 20:
        if rem:
            words.append(EN_NUM_WORDS[rem])
    else:
        words.append(EN_TENS[rem // 10])
        if rem % 10:
            words.append(EN_NUM_WORDS[rem % 10])
    return words


def en_number(n):
    n = int(n)
    if n == 0:
        return ['zero']
    out = []
    millions, rest = divmod(n, 1000000)
    thousands, units = divmod(rest, 1000)
    if millions:
        out += _en_triple(millions) + ['million']
    if thousands:
        out += _en_triple(thousands) + ['thousand']
    if units:
        out += _en_triple(units)
    return out


def spell_numbers(text, lang):
    """Заменить группы цифр словами на языке куска."""
    def rep(m):
        try:
            value = int(m.group(0))
        except Exception:  # noqa: BLE001
            return m.group(0)
        # «1 минута» -> «одна минута»: женский род, если дальше женское слово
        fem = bool(re.match(r'\s+(минут|секунд|тысяч)', text[m.end():] or ''))
        words = ru_number(value, fem_units=fem) if lang == 'ru' else en_number(value)
        return ' '.join(words)

    return re.sub(r'\d+', rep, text)


def _tts_pcm(model, lang, text, speaker, en_speaker=None):
    """Один кусок -> сырой PCM s16le."""
    import numpy as np
    spk = speaker
    if lang == "en":
        want = (en_speaker or "").strip() or _en_speaker or EN_SPEAKER_DEFAULT
        speakers = list(getattr(model, "speakers", []) or [])
        if speakers and want not in speakers:
            want = _en_speaker if _en_speaker in speakers else speakers[0]
        spk = want
    audio = model.apply_tts(text=text, speaker=spk, sample_rate=RATE)
    arr = audio.detach().cpu().numpy().astype("float32").reshape(-1)
    pcm = (arr * 32767.0).clip(-32768, 32767).astype("<i2")
    if CHANNELS == 2:
        pcm = np.repeat(pcm, 2)
    return pcm.tobytes()


def synth(text, speaker, en_speaker=None):
    """Синтез текста -> путь к raw-файлу. Кэш по паре (голос, текст)."""
    raw = os.path.join(CACHE, "greet_%s.raw" % _key(speaker, en_speaker, text))
    if os.path.exists(raw) and os.path.getsize(raw) > 1000:
        return raw
    if speaker.startswith("ya:") or speaker.startswith("yandex:"):
        # голоса Яндекса больше не поддерживаются
        speaker = "baya"
    segs = [(lang, spell_numbers(chunk, lang)) for lang, chunk in split_segments(text)]
    if not segs:
        raise RuntimeError("пустой текст")
    needs = set(lang for lang, _ in segs)
    models = {}
    for lang in needs:
        models[lang] = _load(lang)
        if models[lang] is None:
            raise RuntimeError("модель %s не загрузилась" % lang)
    gap = b"\x00" * int(RATE * CHANNELS * 2 * (GAP_MS / 1000.0))
    buf = []
    for i, (lang, chunk) in enumerate(segs):
        if i:
            buf.append(gap)
        buf.append(_tts_pcm(models[lang], lang, chunk.strip(), speaker, en_speaker))
    data = b"".join(buf)
    tmp = "%s.%d.%d.tmp" % (raw, os.getpid(), threading.get_ident())
    try:
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, raw)
    except Exception:
        # Тот же текст мог синтезироваться параллельно: файл уже на месте.
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except Exception:
            pass
        if not (os.path.exists(raw) and os.path.getsize(raw) > 1000):
            raise
    dur = len(data) / float(RATE * CHANNELS * 2)
    print("tts worker: синтез %d симв. -> %.1f с (%s)" % (
        len(text), dur, "+".join("%s:%s" % (l, c.strip()[:20]) for l, c in segs)), flush=True)
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
                if not parts or parts[0] != b"TEXT" or len(parts) not in (3, 4):
                    f.write(b"ERR bad request\n")
                    f.flush()
                    continue
                speaker = parts[1].decode("ascii", "replace").strip() or "baya"
                if len(parts) == 4:
                    en_speaker = parts[2].decode("ascii", "replace").strip()
                    text = base64.b64decode(parts[3]).decode("utf-8")
                else:
                    en_speaker = ""
                    text = base64.b64decode(parts[2]).decode("utf-8")
                path = synth(text, speaker, en_speaker)
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
    threading.Thread(target=load_models, daemon=True).start()
    while True:
        try:
            conn, _ = srv.accept()
        except KeyboardInterrupt:
            break
        threading.Thread(target=handle, args=(conn,), daemon=True).start()


if __name__ == "__main__":
    sys.exit(main())
