# Шаги установки TeamTalk Universal Plugin на Windows.
#
# Запускается отдельным процессом из install-gui.ps1 (окно) или из
# install.ps1 (консоль), чтобы окно оставалось живым и показывало журнал.
#
#   powershell -File windows\install-child.ps1 -Root <папка> -Setup <setup.py>
#   ключи: -Json <файл настроек> -Deps 1 -Deno 1 -Auto 1 -Start 1
param(
    [Parameter(Mandatory = $true)][string]$Root,
    [Parameter(Mandatory = $true)][string]$Setup,
    [string]$Json = "",
    [int]$Deps = 1,
    [int]$Auto = 1,
    [int]$Start = 1,
    [int]$Deno = 0
)

$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$Venv = Join-Path $Root ".venv"
$VenvPy = Join-Path $Venv "Scripts\python.exe"

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

try {
    if ($Deps -eq 1) {
        Say "== Python =="
        $python = Find-Python
        if (-not $python) {
            Install-WithWinget "Python.Python.3.12" "Python 3.12"
            $python = Find-Python
        }
        if (-not $python) {
            throw "Python 3.10 или новее не найден. Поставьте его с python.org, отметив «Add python.exe to PATH», и запустите установку ещё раз."
        }
        Say "  Python: $python"

        Say ""
        Say "== ffmpeg =="
        $local = Join-Path $Root "bin\ffmpeg.exe"
        if (-not (Test-Tool "ffmpeg") -and -not (Test-Path $local)) {
            Install-WithWinget "Gyan.FFmpeg" "ffmpeg"
        }
        if (Test-Tool "ffmpeg") {
            Say "  ffmpeg: $((Get-Command ffmpeg).Source)"
        } elseif (Test-Path $local) {
            Say "  ffmpeg: $local"
        } else {
            Say "  ffmpeg не найден: бот не сможет играть звук."
        }

        if ($Deno -eq 1 -and -not (Test-Tool "deno")) {
            Say ""
            Say "== Deno =="
            Install-WithWinget "DenoLand.Deno" "Deno"
        }
        if (Test-Tool "deno") { Say "  Deno: $((Get-Command deno).Source)" }

        Say ""
        Say "== Окружение Python (.venv) =="
        if (-not (Test-Path $VenvPy)) {
            & $python -m venv $Venv
        }
        if (-not (Test-Path $VenvPy)) { throw "Не удалось создать .venv." }
        & $VenvPy -m pip install --quiet --upgrade pip wheel
        & $VenvPy -m pip install --quiet --upgrade requests yt-dlp yandex-music curl_cffi py7zr tzdata
        if ($LASTEXITCODE -ne 0) { throw "pip завершился с ошибкой, зависимости не установлены." }
        Say "  Зависимости установлены."
    }

    if (-not (Test-Path $VenvPy)) {
        throw "Нет .venv. Включите «Поставить Python, ffmpeg и зависимости»."
    }

    Say ""
    Say "== TeamTalk SDK =="
    & $VenvPy $Setup sdk
    if ($LASTEXITCODE -ne 0) { throw "TeamTalk SDK не готов." }

    if ($Json) {
        Say ""
        Say "== Настройки =="
        & $VenvPy $Setup config --json $Json
        if ($LASTEXITCODE -ne 0) { throw "Не удалось записать настройки." }
        Remove-Item -LiteralPath $Json -ErrorAction SilentlyContinue
    }

    Say ""
    Say "== Автозапуск =="
    if ($Auto -eq 1) {
        & $VenvPy $Setup autostart on
    } else {
        & $VenvPy $Setup autostart off
    }

    if ($Start -eq 1) {
        Say ""
        Say "== Запуск =="
        & $VenvPy $Setup start
    }

    Say ""
    Say "ГОТОВО"
    exit 0
} catch {
    Say ""
    Say ("ОШИБКА: " + $_.Exception.Message)
    exit 1
}
