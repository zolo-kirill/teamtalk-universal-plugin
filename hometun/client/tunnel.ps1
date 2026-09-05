# Домашний туннель для universal plugin
# Программа связывает домашний компьютер с сервером бота и даёт боту
# выход в интернет через домашний интернет, где YouTube не блокирован.
#
# Настройки читаются из файла config.txt, лежащего рядом с программой.
# При первом запуске программа спросит данные и сама создаст этот файл.
# Если нужно сменить сервер - удали config.txt и запусти программу снова,
# либо отредактируй config.txt обычным блокнотом.

try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
$Host.UI.RawUI.WindowTitle = 'Туннель для YouTube'

function Beep($freq, $dur, $times) {
  for ($i = 0; $i -lt $times; $i++) {
    try { [console]::beep($freq, $dur) } catch {}
    Start-Sleep -Milliseconds 160
  }
}

$dir = $PSScriptRoot
$cfgPath = Join-Path $dir 'config.txt'

# Значения по умолчанию. Пустые server и ssh_login означают,
# что настройка ещё не выполнена.
$cfg = @{
  server      = ''
  server_port = '22'
  ssh_login   = ''
  key_file    = 'hometun.ppk'
  host_key    = ''
  remote_port = '1080'
  local_port  = '1088'
}

# ---- Чтение config.txt ----
function Read-Cfg {
  $cfgOk = $false
  if (Test-Path $cfgPath) {
    Get-Content $cfgPath | ForEach-Object {
      $line = $_.Trim()
      if ($line -eq '' -or $line.StartsWith('#')) { return }
      $i = $line.IndexOf('=')
      if ($i -gt 0) {
        $k = $line.Substring(0, $i).Trim()
        $v = $line.Substring($i + 1).Trim()
        if ($k -and $v) { $script:cfg[$k] = $v }
      }
    }
    if ($script:cfg.server -ne '' -and $script:cfg.ssh_login -ne '') { $cfgOk = $true }
  }
  return $cfgOk
}

# ---- Интерактивная настройка при первом запуске ----
function Ask-Cfg {
  Write-Host ''
  Write-Host 'Первый запуск. Сейчас нужно указать данные твоего сервера бота.'
  Write-Host 'Отвечай на вопросы и после каждого ответа нажимай Enter.'
  Write-Host 'Если в подсказке есть значение в скобках и оно подходит - просто нажми Enter.'
  Write-Host ''
  $script:cfg.server = Read-Host 'Адрес сервера, например myserver.ru'
  $p = Read-Host ('Порт для подключения (Enter = ' + $script:cfg.server_port + ')')
  if ($p) { $script:cfg.server_port = $p }
  $script:cfg.ssh_login = Read-Host 'Логин на сервере (его создаёт серверный скрипт установки)'
  $kf = Read-Host ('Имя файла ключа в этой папке (Enter = ' + $script:cfg.key_file + ')')
  if ($kf) { $script:cfg.key_file = $kf }
  $hk = Read-Host 'Код безопасности сервера, строка SHA256:... (его тоже выдаёт скрипт установки)'
  if ($hk) { $script:cfg.host_key = $hk }
  $rp = Read-Host ('Порт на сервере для выхода в интернет (Enter = ' + $script:cfg.remote_port + ')')
  if ($rp) { $script:cfg.remote_port = $rp }
  $lp = Read-Host ('Локальный порт помощника (Enter = ' + $script:cfg.local_port + ')')
  if ($lp) { $script:cfg.local_port = $lp }

  if (-not $script:cfg.server -or -not $script:cfg.ssh_login) {
    Write-Host ''
    Write-Host 'Не заполнены адрес сервера или логин. Запусти программу ещё раз и заполни их.'
    Beep 300 400 3
    Read-Host 'Нажми Enter, чтобы закрыть'
    exit 1
  }

  $lines = @(
    '# Настройки домашнего туннеля для universal plugin.',
    '# Файл можно править обычным блокнотом. Строки, начинающиеся с #, - пояснения.',
    '',
    '# Адрес сервера бота',
    ('server = ' + $script:cfg.server),
    '# Порт для подключения по SSH',
    ('server_port = ' + $script:cfg.server_port),
    '# Логин туннельного пользователя на сервере',
    ('ssh_login = ' + $script:cfg.ssh_login),
    '# Имя файла ключа (лежит в этой же папке)',
    ('key_file = ' + $script:cfg.key_file),
    '# Код безопасности сервера - строка вида SHA256:...',
    '# Его печатает серверный скрипт установки. Пустым оставлять не стоит:',
    '# тогда при первом подключении нужно будет подтвердить ключ вручную.',
    ('host_key = ' + $script:cfg.host_key),
    '# Порт на сервере, через который бот выходит в интернет',
    ('remote_port = ' + $script:cfg.remote_port),
    '# Локальный порт помощника (менять не нужно)',
    ('local_port = ' + $script:cfg.local_port)
  )
  $lines | Set-Content -Path $cfgPath -Encoding UTF8
  Write-Host ''
  Write-Host 'Настройки сохранены в файл config.txt рядом с программой.'
}

$haveConfig = Read-Cfg
if (-not $haveConfig) {
  Ask-Cfg
  $haveConfig = Read-Cfg
  if (-not $haveConfig) {
    Write-Host ''
    Write-Host 'Не удалось прочитать настройки. Запусти программу ещё раз.'
    Beep 300 400 3
    Read-Host 'Нажми Enter, чтобы закрыть'
    exit 1
  }
}

# ---- Пути и проверки ----
$plink = Join-Path $dir 'plink.exe'
$gost = Join-Path $dir 'gost.exe'
$ppk = Join-Path $dir $cfg.key_file

Write-Host ''
Write-Host 'Туннель для YouTube.'
Write-Host 'Эта программа связывает твой компьютер с сервером бота,'
Write-Host 'чтобы бот мог выходить в интернет через твой домашний интернет,'
Write-Host ('где YouTube работает свободно.')
Write-Host ''
Write-Host ('Сервер: ' + $cfg.server)
Write-Host ''

if (-not (Test-Path $plink)) {
  Write-Host 'Не найден файл plink.exe. Проверь, что папка программы не повреждена.'
  Beep 300 400 3
  Read-Host 'Нажми Enter, чтобы закрыть'
  exit 1
}
if (-not (Test-Path $gost)) {
  Write-Host 'Не найден файл gost.exe. Проверь, что папка программы не повреждена.'
  Beep 300 400 3
  Read-Host 'Нажми Enter, чтобы закрыть'
  exit 1
}
if (-not (Test-Path $ppk)) {
  Write-Host ''
  Write-Host ('Не найден файл ключа: ' + $cfg.key_file)
  Write-Host 'Положи файл ключа в эту же папку и запусти программу снова.'
  Beep 300 400 3
  Read-Host 'Нажми Enter, чтобы закрыть'
  exit 1
}

# Снимаем пометку "скачано из интернета", чтобы Windows не блокировала программы
Get-ChildItem $dir -Filter *.exe -ErrorAction SilentlyContinue | Unblock-File -ErrorAction SilentlyContinue

# ---- Шаг 1: локальный помощник-прокси ----
if (-not (Get-Process -Name 'gost' -ErrorAction SilentlyContinue)) {
  Write-Host 'Шаг 1 из 2. Запускаю локального помощника...'
  Start-Process -FilePath $gost -ArgumentList ('-L=socks5://127.0.0.1:{0}' -f $cfg.local_port) -WindowStyle Hidden
  Start-Sleep -Seconds 2
} else {
  Write-Host 'Шаг 1 из 2. Локальный помощник уже работает.'
}

$proxyOk = $false
for ($i = 0; $i -lt 5; $i++) {
  Start-Sleep -Seconds 1
  try {
    if (Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort ([int]$cfg.local_port) -State Listen -ErrorAction Stop) {
      $proxyOk = $true
      break
    }
  } catch {}
}
if (-not $proxyOk) {
  Write-Host ''
  Write-Host 'Не удалось запустить локального помощника.'
  Write-Host 'Возможно, Windows блокирует программу. Сделай так:'
  Write-Host 'найди в этой папке файл gost.exe, нажми на него правой кнопкой,'
  Write-Host 'выбери "Свойства", поставь галочку "Разблокировать", нажми ОК,'
  Write-Host 'и запусти start-tunnel.bat снова.'
  Beep 300 400 3
  Read-Host 'Нажми Enter, чтобы закрыть'
  exit 1
}

# ---- Шаг 2: соединение с сервером ----
Write-Host 'Шаг 2 из 2. Подключаюсь к серверу...'
Write-Host ''

$sshArgs = @(
  '-ssh', '-N',
  '-R', ('127.0.0.1:{0}:127.0.0.1:{1}' -f $cfg.remote_port, $cfg.local_port),
  ($cfg.ssh_login + '@' + $cfg.server),
  '-i', ('"{0}"' -f $ppk)
)
if ($cfg.host_key) {
  $sshArgs += @('-hostkey', $cfg.host_key)
}
$sshArgs += @('-batch')

$attempt = 0
while ($true) {
  $attempt++
  if ($attempt -le 2) {
    Write-Host 'Проверяю связь с сервером, это может занять несколько секунд...'
  }
  $proc = Start-Process -FilePath $plink -ArgumentList $sshArgs -NoNewWindow -PassThru
  Start-Sleep -Seconds 6
  if ($proc.HasExited) {
    Write-Host ''
    Write-Host 'Не получилось подключиться. Проверь, что компьютер в интернете'
    Write-Host 'и что в файле config.txt верно указан адрес сервера.'
    Write-Host 'Повторяю попытку через 10 секунд...'
    Beep 300 300 2
    Start-Sleep -Seconds 10
    continue
  }

  Write-Host ''
  Write-Host 'Подключено! Теперь YouTube в боте работает.'
  Write-Host 'ВАЖНО: не закрывай это окно, пока нужен YouTube.'
  Write-Host 'Если связь оборвётся - программа восстановит её сама.'
  Beep 900 250 2

  $proc.WaitForExit()
  Write-Host ''
  Write-Host 'Связь с сервером прервалась. Переподключаюсь через 5 секунд...'
  Start-Sleep -Seconds 5
}
