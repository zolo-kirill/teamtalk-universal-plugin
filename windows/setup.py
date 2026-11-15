#!/usr/bin/env python3
"""Настройка бота под Windows: TeamTalk SDK, конфигурация, автозапуск.

    python windows\\setup.py sdk             скачать TeamTalk SDK
    python windows\\setup.py config          спросить данные и записать .secrets\\.env
    python windows\\setup.py autostart on    запускать бота при входе в Windows
    python windows\\setup.py autostart off
    python windows\\setup.py start           запустить в фоне, без окна
    python windows\\setup.py stop
    python windows\\setup.py status

Запускать интерпретатором из .venv; install.bat делает это сам.
"""
import getpass
import glob
import hashlib
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SDK_DIR = os.path.join(ROOT, "sdk")
SECRETS = os.path.join(ROOT, ".secrets")
ENV_FILE = os.path.join(SECRETS, ".env")
PID_FILE = os.path.join(ROOT, "launcher.pid")
LOG_FILE = os.path.join(ROOT, "logs", "bot.log")
LAUNCHER = os.path.join(ROOT, "windows", "launcher.py")

SDK_PAGE = "https://bearware.dk/?page_id=419"
SDK_FALLBACK = "https://www.bearware.dk/teamtalksdk/v5.23a/tt5sdk_v5.23a_win64.7z"
HTTP_HEADERS = {"User-Agent": "Mozilla/5.0"}

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
# у каждой папки с ботом своя запись, чтобы на одном компьютере жили несколько
RUN_NAME = "TeamTalkUniversalPlugin-" + hashlib.md5(ROOT.lower().encode("utf-8")).hexdigest()[:6]


# TeamTalk SDK

def sdk_path():
    for path in sorted(glob.glob(os.path.join(SDK_DIR, "tt5sdk_*_win64")), reverse=True):
        if os.path.isfile(os.path.join(path, "Library", "TeamTalk_DLL", "TeamTalk5.dll")):
            return path
    return ""


def sdk_load_error(path):
    """Пустая строка, если модуль TeamTalk5 импортируется, иначе текст ошибки."""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.join(path, "Library", "TeamTalkPy")
    env["PATH"] = os.path.join(path, "Library", "TeamTalk_DLL") + os.pathsep + env.get("PATH", "")
    res = subprocess.run([sys.executable, "-c", "import TeamTalk5"],
                         env=env, capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    if res.returncode == 0:
        return ""
    lines = (res.stderr or "").strip().splitlines()
    return lines[-1] if lines else "код %d" % res.returncode


def _version_key(url):
    m = re.search(r"tt5sdk_v(\d+)\.(\d+)([a-z]?)", url)
    return (int(m.group(1)), int(m.group(2)), m.group(3)) if m else (0, 0, "")


def sdk_url():
    """Самая свежая сборка для Windows со страницы BearWare."""
    try:
        req = urllib.request.Request(SDK_PAGE, headers=HTTP_HEADERS)
        with urllib.request.urlopen(req, timeout=25) as r:
            html = r.read().decode("utf-8", "ignore")
        urls = re.findall(r'https?://[^"\s]*tt5sdk_v\d[^"\s]*_win64\.7z', html)
        if urls:
            return max(urls, key=_version_key).replace("http://", "https://")
    except Exception:
        pass
    return SDK_FALLBACK


def sdk_unpack(archive):
    """Из архива нужны только библиотека, питоновские биндинги и лицензия."""
    import py7zr
    with py7zr.SevenZipFile(archive) as z:
        want = [n for n in z.getnames()
                if n.endswith("/Library/TeamTalk_DLL/TeamTalk5.dll")
                or n.endswith("/License.txt")
                or ("/Library/TeamTalkPy/" in n and n.endswith(".py") and "/test/" not in n)]
        z.extract(path=SDK_DIR, targets=want)


def cmd_sdk():
    have = sdk_path()
    if have and not sdk_load_error(have):
        print("  TeamTalk SDK уже на месте: %s" % have)
        return 0
    if not importlib.util.find_spec("py7zr"):
        print("  !! Нет пакета py7zr. Поставьте: .venv\\Scripts\\pip install py7zr")
        return 1
    url = sdk_url()
    print("  Качаю %s, около 30 МБ..." % os.path.basename(url))
    os.makedirs(SDK_DIR, exist_ok=True)
    archive = os.path.join(SDK_DIR, "download.7z")
    try:
        req = urllib.request.Request(url, headers=HTTP_HEADERS)
        with urllib.request.urlopen(req, timeout=60) as r, open(archive, "wb") as f:
            shutil.copyfileobj(r, f)
        sdk_unpack(archive)
    except Exception as e:
        print("  !! Не удалось скачать или распаковать SDK: %s" % e)
        return 1
    finally:
        if os.path.isfile(archive):
            os.remove(archive)
    have = sdk_path()
    if not have:
        print("  !! В архиве нет TeamTalk5.dll.")
        return 1
    err = sdk_load_error(have)
    if err:
        print("  !! SDK распакован, но не загружается: %s" % err)
        print("     Обычно не хватает Microsoft Visual C++ Redistributable (x64).")
        return 1
    print("  TeamTalk SDK готов: %s" % have)
    return 0


# Конфигурация

def ask(prompt, default=""):
    tail = " [%s]" % default if default else ""
    return input("  %s%s: " % (prompt, tail)).strip() or default


def ask_yes(prompt):
    return not input("  %s [Y/n]: " % prompt).strip().lower().startswith(("n", "н"))


def copy_secret(src, name):
    src = src.strip().strip('"')
    if not src:
        return
    if os.path.isfile(src):
        shutil.copyfile(src, os.path.join(SECRETS, name))
        print("  Скопировано: %s" % name)
    else:
        print("  !! Файл не найден: %s — пропущено." % src)


def protect(path):
    """Оставить доступ к файлу только текущему пользователю."""
    if os.name != "nt":
        os.chmod(path, 0o600)
        return
    user = os.environ.get("USERNAME", "")
    if os.environ.get("USERDOMAIN"):
        user = "%s\\%s" % (os.environ["USERDOMAIN"], user)
    quiet = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # сначала свои права, и только потом отключаем унаследованные
    if user and subprocess.call(["icacls", path, "/grant:r", "%s:F" % user], **quiet) == 0:
        subprocess.call(["icacls", path, "/inheritance:r"], **quiet)


def cmd_config():
    if os.path.isfile(ENV_FILE) and not ask_yes("Настройки уже есть. Записать заново?"):
        return 0
    print("")
    print("== Сервер TeamTalk (адрес, логин и пароль обязательны) ==")
    while True:
        host = ask("Адрес сервера TeamTalk")
        tcp_port = ask("TCP-порт", "10333")
        udp_port = ask("UDP-порт", "10333")
        username = ask("Логин")
        password = getpass.getpass("  Пароль (при вводе не виден): ").strip()
        if host and username and password:
            break
        print("  !! Адрес сервера, логин и пароль обязательны. Попробуйте ещё раз.")
    values = [
        ("TEAMTALK_HOST", host),
        ("TEAMTALK_TCP_PORT", tcp_port),
        ("TEAMTALK_UDP_PORT", udp_port),
        ("TEAMTALK_USERNAME", username),
        ("TEAMTALK_PASSWORD", password),
        ("TEAMTALK_NICKNAME", ask("Имя бота (ник)", "UniversalBot")),
        ("TEAMTALK_CHANNEL", ask("Канал (пусто = корневой)")),
    ]
    print("")
    print("== Telegram ==")
    token = ask("Токен бота для управления и уведомлений (пусто = пропустить)")
    values.append(("TG_TOKEN", token))
    if token:
        values += [
            ("TG_OWNER_USER_ID", ask("Ваш Telegram ID — владелец (пусто = пропустить)")),
            ("TG_NOTIFY_CHAT_ID", ask("ID чата для уведомлений о входе и выходе (пусто = пропустить)")),
            ("TG_MUSIC_TOKEN", ask("Токен бота для музыки (пусто = пропустить)")),
            ("TG_PROXY", ask("Прокси для Telegram, например http://127.0.0.1:8118 (пусто = напрямую)")),
        ]
    print("")
    print("== Музыкальные сервисы ==")
    yt_cookies = ask("Путь к cookies.txt для YouTube (пусто = пропустить)")
    rt_cookies = ask("Путь к rutube_cookies.txt (пусто = пропустить)")
    ym_token = ask("Токен Яндекс Музыки (пусто = пропустить)")

    os.makedirs(SECRETS, exist_ok=True)
    if os.path.isfile(ENV_FILE):
        shutil.copyfile(ENV_FILE, ENV_FILE + ".bak")
    with open(ENV_FILE, "w", encoding="utf-8", newline="\n") as f:
        for key, value in values:
            if value:
                f.write("%s=%s\n" % (key, value))
    protect(ENV_FILE)
    copy_secret(yt_cookies, "cookies.txt")
    copy_secret(rt_cookies, "rutube_cookies.txt")
    if ym_token:
        with open(os.path.join(SECRETS, "ym_token.txt"), "w", encoding="utf-8") as f:
            f.write(ym_token + "\n")
        protect(os.path.join(SECRETS, "ym_token.txt"))
    print("")
    print("  Настройки записаны в %s" % ENV_FILE)
    return 0


# Автозапуск и управление

def pythonw():
    exe = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return exe if os.path.isfile(exe) else sys.executable


def autostart_enabled():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, RUN_NAME)
        return True
    except OSError:
        return False


def cmd_autostart(mode):
    import winreg
    if mode == "on":
        command = '"%s" "%s"' % (pythonw(), LAUNCHER)
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.SetValueEx(key, RUN_NAME, 0, winreg.REG_SZ, command)
        print("  Автозапуск включён: бот стартует при входе в Windows.")
        return 0
    if mode == "off":
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, RUN_NAME)
        except OSError:
            pass
        print("  Автозапуск выключен.")
        return 0
    print("  Использование: setup.py autostart on|off")
    return 2


def launcher_pid():
    """PID работающего лаунчера или 0."""
    try:
        with open(PID_FILE, encoding="utf-8") as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return 0
    res = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid, "/FO", "CSV", "/NH"],
                         capture_output=True, text=True)
    return pid if "python" in (res.stdout or "").lower() else 0


def cmd_start():
    if launcher_pid():
        print("  Бот уже запущен.")
        return 0
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([pythonw(), LAUNCHER], cwd=ROOT, creationflags=flags, close_fds=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("  Бот запущен в фоне. Журнал: %s" % LOG_FILE)
    return 0


def cmd_stop():
    pid = launcher_pid()
    if not pid:
        print("  Бот не запущен.")
        return 0
    subprocess.call(["taskkill", "/F", "/T", "/PID", str(pid)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        os.remove(PID_FILE)
    except OSError:
        pass
    print("  Бот остановлен.")
    return 0


def cmd_status():
    print("  Бот:        %s" % ("запущен" if launcher_pid() else "не запущен"))
    print("  Автозапуск: %s" % ("включён" if autostart_enabled() else "выключен"))
    print("  SDK:        %s" % (sdk_path() or "не найден"))
    if os.path.isfile(LOG_FILE):
        with open(LOG_FILE, encoding="utf-8", errors="replace") as f:
            tail = f.readlines()[-5:]
        print("  Последние строки журнала:")
        for line in tail:
            print("    " + line.rstrip())
    return 0


def main():
    args = sys.argv[1:]
    cmd = args[0] if args else ""
    if cmd == "sdk":
        return cmd_sdk()
    if cmd == "config":
        return cmd_config()
    if cmd in ("autostart", "start", "stop", "status"):
        if os.name != "nt":
            print("  Эта команда работает только в Windows.")
            return 2
        if cmd == "autostart":
            return cmd_autostart(args[1] if len(args) > 1 else "")
        return {"start": cmd_start, "stop": cmd_stop, "status": cmd_status}[cmd]()
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
