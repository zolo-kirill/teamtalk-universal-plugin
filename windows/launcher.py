#!/usr/bin/env python3
"""Запуск бота под Windows.

Делает то же, что run.sh и systemd на Linux: читает .secrets\\.env, находит
TeamTalk SDK и держит bot.py запущенным. Бот сам завершается по командам
rs и cookies и раз в сутки ночью — после выхода он поднимается снова.

    python windows\\launcher.py          запустить
    python windows\\launcher.py --show   показать, с чем запустится, и выйти

Без консоли (pythonw.exe, автозапуск) вывод бота идёт в logs\\bot.log.
"""
import atexit
import ctypes
import glob
import hashlib
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(ROOT, "logs")
LOG_FILE = os.path.join(LOG_DIR, "bot.log")
LOG_MAX = 5 * 1024 * 1024
PID_FILE = os.path.join(ROOT, "launcher.pid")


def read_env(path):
    """Строки вида КЛЮЧ=значение; пустые и начинающиеся с # пропускаются."""
    out = {}
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            if key:
                out[key] = value
    return out


def find_env_file():
    """Секреты: сначала внутри каталога бота, потом уровнем выше."""
    for base in (ROOT, os.path.dirname(ROOT)):
        path = os.path.join(base, ".secrets", ".env")
        if os.path.isfile(path):
            return path
    return ""


def find_sdk():
    found = sorted(glob.glob(os.path.join(ROOT, "sdk", "tt5sdk_*_win64")), reverse=True)
    for path in [os.environ.get("TT_SDK_DIR", "")] + found:
        if path and os.path.isfile(os.path.join(path, "Library", "TeamTalk_DLL", "TeamTalk5.dll")):
            return path
    return ""


def console_python():
    """python.exe рядом с текущим интерпретатором: под pythonw.exe у бота не
    было бы ни вывода, ни консоли, которую наследуют ffmpeg и yt-dlp."""
    exe = os.path.join(os.path.dirname(sys.executable), "python.exe")
    return exe if os.path.isfile(exe) else sys.executable


def child_env(env_file, sdk):
    env = dict(os.environ)
    if env_file:
        env.update(read_env(env_file))
    env.setdefault("TEAMTALK_PASSWORD", env.get("TEAMTALK_BOT_PASSWORD", ""))
    # куки и токен Яндекс Музыки лежат рядом с .env
    secrets = os.path.dirname(env_file) if env_file else os.path.join(ROOT, ".secrets")
    env.setdefault("TEAMTALK_COOKIES", os.path.join(secrets, "cookies.txt"))
    env.setdefault("TEAMTALK_RUTUBE_COOKIES", os.path.join(secrets, "rutube_cookies.txt"))
    env.setdefault("TEAMTALK_YM_TOKEN", os.path.join(secrets, "ym_token.txt"))
    # без режима UTF-8 Python на Windows читает и пишет файлы в cp1251
    env["PYTHONUTF8"] = "1"
    paths = [os.path.join(ROOT, "bin")]
    if sdk:
        paths.append(os.path.join(sdk, "Library", "TeamTalk_DLL"))
        pythonpath = [os.path.join(sdk, "Library", "TeamTalkPy")]
        if env.get("PYTHONPATH"):
            pythonpath.append(env["PYTHONPATH"])
        env["PYTHONPATH"] = os.pathsep.join(pythonpath)
    env["PATH"] = os.pathsep.join(paths + [env.get("PATH", "")])
    return env


def already_running():
    """Второй бот с тем же токеном получает от Telegram 409, поэтому на
    каталог разрешён один запуск. Мьютекс живёт, пока жив процесс."""
    if os.name != "nt":
        return False
    name = "tt-universal-plugin-" + hashlib.md5(ROOT.lower().encode("utf-8")).hexdigest()
    kernel32 = ctypes.windll.kernel32
    already_running.handle = kernel32.CreateMutexW(None, False, name)
    return kernel32.GetLastError() == 183  # ERROR_ALREADY_EXISTS


def open_log():
    os.makedirs(LOG_DIR, exist_ok=True)
    try:
        if os.path.getsize(LOG_FILE) > LOG_MAX:
            os.replace(LOG_FILE, LOG_FILE + ".1")
    except OSError:
        pass
    return open(LOG_FILE, "a", encoding="utf-8", buffering=1)


def remove_pid_file():
    try:
        os.remove(PID_FILE)
    except OSError:
        pass


def main():
    env_file = find_env_file()
    sdk = find_sdk()
    py = console_python()

    if "--show" in sys.argv[1:]:
        print("каталог бота: %s" % ROOT)
        print("секреты:      %s" % (env_file or "(не найдены)"))
        print("SDK:          %s" % (sdk or "(не найден)"))
        print("python:       %s" % py)
        return 0

    headless = sys.stdout is None  # pythonw.exe: консоли нет
    log = open_log() if headless else None

    def say(text):
        line = "%s launcher: %s" % (time.strftime("%H:%M:%S"), text)
        if log:
            log.write(line + "\n")
        else:
            print(line, flush=True)

    if not sdk:
        say("TeamTalk SDK не найден в папке sdk — запустите install.bat")
        return 2
    if not env_file and not os.path.isfile(os.path.join(ROOT, "config.json")):
        say("нет ни .secrets\\.env, ни config.json — запустите install.bat")
        return 2
    if already_running():
        say("бот из этой папки уже запущен")
        return 3

    with open(PID_FILE, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))
    atexit.register(remove_pid_file)

    env = child_env(env_file, sdk)
    flags = subprocess.CREATE_NO_WINDOW if headless and os.name == "nt" else 0
    quick_exits = 0
    while True:
        if log:
            log.close()
            log = open_log()
        started = time.monotonic()
        proc = subprocess.Popen(
            [py, "-u", "bot.py"], cwd=ROOT, env=env,
            stdin=subprocess.DEVNULL if headless else None,
            stdout=log, stderr=subprocess.STDOUT if log else None,
            creationflags=flags,
        )
        try:
            code = proc.wait()
        except KeyboardInterrupt:
            # Ctrl+C получает и бот: ждём, пока он выйдет сам
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            return 0
        # бот падает сразу после старта — не крутим его без пауз
        quick_exits = quick_exits + 1 if time.monotonic() - started < 15 else 0
        delay = min(60, 2 + quick_exits * 5)
        say("бот завершился с кодом %s, перезапуск через %d с" % (code, delay))
        try:
            time.sleep(delay)
        except KeyboardInterrupt:
            return 0


if __name__ == "__main__":
    sys.exit(main())
