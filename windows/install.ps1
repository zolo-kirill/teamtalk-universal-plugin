# Установка TeamTalk Universal Plugin на Windows 10 и 11.
#
#   install.bat               полная установка с вопросами
#   install.bat -Unattended   без вопросов: только программы, окружение и SDK
#
# Ставит через winget Python, ffmpeg и Deno, если их нет, создаёт .venv,
# качает TeamTalk SDK и запускает настройку (windows\setup.py).
param([switch]$Unattended)

$env:PYTHONUTF8 = "1"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$Root = Split-Path -Parent $PSScriptRoot
$Venv = Join-Path $Root ".venv"
$VenvPy = Join-Path $Venv "Scripts\python.exe"
$Setup = Join-Path $Root "windows\setup.py"

function Say($text) { Write-Host $text }

function Test-Tool($name) {
    return [bool](Get-Command $name -ErrorAction SilentlyContinue)
}

# После winget новые пути видны только новым окнам: дочитываем их из реестра.
function Update-Path {
    $parts = @($env:Path -split ';')
    foreach ($scope in @("Machine", "User")) {
        $parts += @([Environment]::GetEnvironmentVariable("Path", $scope) -split ';')
    }
    $env:Path = (($parts | Where-Object { $_ } | Select-Object -Unique) -join ';')
}

function Install-WithWinget($id, $title) {
    if (-not (Test-Tool "winget")) {
        Say "  winget не найден, поставить $title автоматически не получится."
        return
    }
    Say "  Ставлю $title через winget..."
    winget install -e --id $id --accept-source-agreements --accept-package-agreements --disable-interactivity | Out-Null
    Update-Path
}

# Путь к python.exe версии 3.10 или новее, иначе пустая строка.
function Find-Python {
    $probe = "import sys; sys.version_info >= (3, 10) and print(sys.executable)"
    foreach ($cmd in @("py", "python", "python3")) {
        if (-not (Test-Tool $cmd)) { continue }
        if ($cmd -eq "py") {
            $found = & $cmd -3 -c $probe 2>$null | Select-Object -First 1
        } else {
            $found = & $cmd -c $probe 2>$null | Select-Object -First 1
        }
        if ($found -and (Test-Path $found)) { return $found }
    }
    return ""
}

function Ask-Yes($question) {
    $answer = Read-Host "  $question [Y/n]"
    return ($answer -notmatch '^\s*[nNнН]')
}

Say "TeamTalk Universal Plugin: установка на Windows"

Say ""
Say "== Python =="
$Py = Find-Python
if (-not $Py) {
    Install-WithWinget "Python.Python.3.12" "Python 3.12"
    $Py = Find-Python
}
if (-not $Py) {
    Say "  !! Python 3.10 или новее не найден."
    Say "     Скачайте его с https://www.python.org/downloads/, при установке отметьте"
    Say "     Add python.exe to PATH и запустите install.bat ещё раз."
    exit 1
}
Say "  Python: $Py"

Say ""
Say "== ffmpeg =="
$LocalFfmpeg = Join-Path $Root "bin\ffmpeg.exe"
if (-not (Test-Tool "ffmpeg") -and -not (Test-Path $LocalFfmpeg)) {
    Install-WithWinget "Gyan.FFmpeg" "ffmpeg"
}
if (Test-Tool "ffmpeg") {
    Say "  ffmpeg: $((Get-Command ffmpeg).Source)"
} elseif (Test-Path $LocalFfmpeg) {
    Say "  ffmpeg: $LocalFfmpeg"
} else {
    Say "  !! ffmpeg не найден, без него бот не играет звук."
    Say "     Положите ffmpeg.exe в папку bin рядом с bot.py и запустите install.bat ещё раз."
}

Say ""
Say "== Deno =="
if (-not (Test-Tool "deno") -and -not $Unattended) {
    if (Ask-Yes "Поставить Deno? С ним yt-dlp надёжнее работает с YouTube") {
        Install-WithWinget "DenoLand.Deno" "Deno"
    }
}
if (Test-Tool "deno") {
    Say "  Deno: $((Get-Command deno).Source)"
} else {
    Say "  Deno не установлен. Для YouTube он полезен, но необязателен."
}

Say ""
Say "== Окружение Python (.venv) =="
if (-not (Test-Path $VenvPy)) {
    & $Py -m venv $Venv
    if (-not (Test-Path $VenvPy)) {
        Say "  !! Не удалось создать .venv."
        exit 1
    }
}
& $VenvPy -m pip install --quiet --upgrade pip wheel
& $VenvPy -m pip install --quiet --upgrade requests yt-dlp yandex-music curl_cffi py7zr tzdata
if ($LASTEXITCODE -ne 0) {
    Say "  !! pip завершился с ошибкой, зависимости не установлены."
    exit 1
}
Say "  Зависимости установлены."

Say ""
Say "== TeamTalk SDK =="
& $VenvPy $Setup sdk
if ($LASTEXITCODE -ne 0) { exit 1 }

if ($Unattended) {
    Say ""
    Say "Программы и SDK готовы. Настройка: .venv\Scripts\python windows\setup.py config"
    exit 0
}

& $VenvPy $Setup config
if ($LASTEXITCODE -ne 0) { exit 1 }

Say ""
Say "== Запуск =="
if (Ask-Yes "Запускать бота при входе в Windows?") {
    & $VenvPy $Setup autostart on
}
if (Ask-Yes "Запустить бота сейчас, в фоне?") {
    & $VenvPy $Setup start
}

Say ""
Say "Готово. Запуск в окне: run.bat. Журнал фонового запуска: logs\bot.log."
Say "Остановить: .venv\Scripts\python windows\setup.py stop"
exit 0
