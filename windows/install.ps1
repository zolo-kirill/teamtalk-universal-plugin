# Установка TeamTalk Universal Plugin на Windows 10 и 11 в консоли.
#
#   install.bat -Console               установка с вопросами
#   install.bat -Console -Unattended   без вопросов: программы, окружение и SDK
#
# Обычный запуск install.bat открывает окно (windows\install-gui.ps1).
param([switch]$Console, [switch]$Unattended)

$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$Root = Split-Path -Parent $PSScriptRoot
$Child = Join-Path $PSScriptRoot "install-child.ps1"
$Setup = Join-Path $Root "windows\setup.py"

function Ask($text, $default = "") {
    $tail = if ($default) { " [$default]" } else { "" }
    $answer = Read-Host "  $text$tail"
    if ($answer) { return $answer.Trim() }
    return $default
}

function Ask-Secret($text) {
    $value = Read-Host "  $text" -AsSecureString
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($value)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
}

function Ask-Yes($text) {
    $answer = Read-Host "  $text [Y/n]"
    return ($answer -notmatch '^\s*[nNнН]')
}

if ($Unattended) {
    & powershell -NoProfile -ExecutionPolicy Bypass -File $Child -Root $Root -Setup $Setup -Deps 1 -Auto 0 -Start 0 -Deno 0
    exit $LASTEXITCODE
}

Write-Host "TeamTalk Universal Plugin: установка на Windows"
Write-Host ""
Write-Host "== Сервер TeamTalk =="

$hostName = ""
$login = ""
$password = ""
while (-not $hostName -or -not $login -or -not $password) {
    $hostName = Ask "Адрес сервера или IP"
    $login = Ask "Логин бота"
    $password = Ask-Secret "Пароль (ввод не отображается)"
    if (-not $hostName -or -not $login -or -not $password) {
        Write-Host "  Адрес, логин и пароль обязательны."
    }
}

$data = [ordered]@{
    TEAMTALK_HOST     = $hostName
    TEAMTALK_TCP_PORT = Ask "TCP-порт" "10333"
    TEAMTALK_UDP_PORT = Ask "UDP-порт" "10333"
    TEAMTALK_USERNAME = $login
    TEAMTALK_PASSWORD = $password
    TEAMTALK_NICKNAME = Ask "Имя бота (ник)" "!universal bot"
    TEAMTALK_CHANNEL  = Ask "Канал (пусто — корневой)"
}

Write-Host ""
Write-Host "== Telegram =="
$token = Ask "Токен бота для управления (пусто — пропустить)"
$data["TG_TOKEN"] = $token
if ($token) {
    $data["TG_OWNER_USER_ID"] = Ask "Ваш Telegram ID — владелец (пусто — пропустить)"
    $data["TG_NOTIFY_CHAT_ID"] = Ask "ID чата для уведомлений (пусто — пропустить)"
    $data["TG_PROXY"] = Ask "Прокси для Telegram, например http://127.0.0.1:8118 (пусто — напрямую)"
}

Write-Host ""
Write-Host "== Музыкальные сервисы =="
$data["YT_COOKIES"] = Ask "Путь к cookies.txt для YouTube (пусто — пропустить)"
$data["RT_COOKIES"] = Ask "Путь к rutube_cookies.txt (пусто — пропустить)"
$data["YM_TOKEN"] = Ask "Токен Яндекс Музыки (пусто — пропустить)"

$dir = Join-Path $Root ".secrets"
if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
$json = Join-Path $dir "install.json"
($data | ConvertTo-Json) | Set-Content -LiteralPath $json -Encoding UTF8

Write-Host ""
$auto = if (Ask-Yes "Запускать бота при входе в Windows?") { 1 } else { 0 }
$deno = if (Ask-Yes "Поставить Deno? С ним yt-dlp надёжнее работает с YouTube") { 1 } else { 0 }
$startNow = if (Ask-Yes "Запустить бота сейчас, в фоне?") { 1 } else { 0 }

& powershell -NoProfile -ExecutionPolicy Bypass -File $Child -Root $Root -Setup $Setup -Json $json `
    -Deps 1 -Auto $auto -Start $startNow -Deno $deno

Write-Host ""
Write-Host "Запуск в окне: run.bat. Журнал: logs\bot.log."
exit $LASTEXITCODE
