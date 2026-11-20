# Графический установщик TeamTalk Universal Plugin для Windows 10 и 11.
#
#   install.bat            открывает это окно
#   install.bat -Console   установка вопросами в консоли (windows\install.ps1)
#
# Окно собирает данные, пишет их в .secrets\install.json и запускает
# windows\install-child.ps1 отдельным процессом, показывая его журнал.

$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()

$Root = Split-Path -Parent $PSScriptRoot
$Setup = Join-Path $Root "windows\setup.py"
$Child = Join-Path $PSScriptRoot "install-child.ps1"
$EnvFile = Join-Path $Root ".secrets\.env"
$JsonFile = Join-Path $Root ".secrets\install.json"

$Stamp = [guid]::NewGuid().ToString("N")
$LogFile = Join-Path $env:TEMP "ttinstall-$Stamp.log"
$ErrFile = Join-Path $env:TEMP "ttinstall-$Stamp.err"

$script:fields = @{}
$script:pos = @{}
$script:proc = $null
$script:done = $true


# Прошлые настройки, чтобы не вводить всё заново при повторной установке.

function Read-EnvFile {
    $map = @{}
    if (-not (Test-Path -LiteralPath $EnvFile)) { return $map }
    foreach ($line in (Get-Content -LiteralPath $EnvFile -Encoding UTF8)) {
        if ($line -match '^\s*([A-Z_]+)\s*=\s*(.*?)\s*$') {
            $map[$Matches[1]] = $Matches[2]
        }
    }
    return $map
}

$saved = Read-EnvFile

function Saved($key, $default) {
    if ($saved.ContainsKey($key) -and $saved[$key]) { return $saved[$key] }
    return $default
}


# Элементы окна

function Add-Header {
    param($Panel, $Y, $Text)
    $label = New-Object Windows.Forms.Label
    $label.Text = $Text
    $label.Font = New-Object Drawing.Font("Segoe UI", 10, [Drawing.FontStyle]::Bold)
    $label.SetBounds(16, $Y, 560, 22)
    $Panel.Controls.Add($label)
}

function Add-Field {
    param($Panel, $Y, $Caption, $Name, $Value = "", [switch]$Secret)
    $label = New-Object Windows.Forms.Label
    $label.Text = $Caption
    $label.SetBounds(18, $Y + 4, 216, 22)
    $Panel.Controls.Add($label)

    $box = New-Object Windows.Forms.TextBox
    $box.SetBounds(240, $Y, 380, 24)
    $box.Text = $Value
    if ($Secret) { $box.UseSystemPasswordChar = $true }
    $Panel.Controls.Add($box)
    $script:fields[$Name] = $box
}

function Add-FileField {
    param($Panel, $Y, $Caption, $Name)
    Add-Field $Panel $Y $Caption $Name

    $button = New-Object Windows.Forms.Button
    $button.Text = "Обзор…"
    $button.SetBounds(626, $Y - 1, 74, 26)
    $button.Add_Click({
        $dialog = New-Object Windows.Forms.OpenFileDialog
        $dialog.Filter = "Текстовые файлы (*.txt)|*.txt|Все файлы (*.*)|*.*"
        if ($dialog.ShowDialog() -eq "OK") {
            $script:fields[$Name].Text = $dialog.FileName
        }
    })
    $Panel.Controls.Add($button)
}


# Само окно

$form = New-Object Windows.Forms.Form
$form.Text = "Universal Plugin: установка для Windows"
$form.ClientSize = New-Object Drawing.Size(780, 660)
$form.StartPosition = "CenterScreen"
$form.FormBorderStyle = "FixedSingle"
$form.MaximizeBox = $false
$form.Font = New-Object Drawing.Font("Segoe UI", 9.5)
$form.Icon = [Drawing.SystemIcons]::Application

$tabs = New-Object Windows.Forms.TabControl
$tabs.SetBounds(12, 12, 756, 584)
$form.Controls.Add($tabs)

$tabSetup = New-Object Windows.Forms.TabPage
$tabSetup.Text = "Настройки"
$tabs.TabPages.Add($tabSetup)

$panel = New-Object Windows.Forms.Panel
$panel.Dock = "Fill"
$panel.AutoScroll = $true
$tabSetup.Controls.Add($panel)

Add-Header $panel 10 "Сервер TeamTalk"
Add-Field $panel 40 "Адрес сервера или IP" "host" (Saved "TEAMTALK_HOST" "")
Add-Field $panel 70 "TCP-порт" "tcp" (Saved "TEAMTALK_TCP_PORT" "10333")
Add-Field $panel 100 "UDP-порт" "udp" (Saved "TEAMTALK_UDP_PORT" "10333")
Add-Field $panel 130 "Логин бота" "user" (Saved "TEAMTALK_USERNAME" "")
Add-Field $panel 160 "Пароль" "pass" (Saved "TEAMTALK_PASSWORD" "") -Secret
Add-Field $panel 190 "Имя бота (ник)" "nick" (Saved "TEAMTALK_NICKNAME" "!universal bot")
Add-Field $panel 220 "Канал (пусто — корневой)" "chan" (Saved "TEAMTALK_CHANNEL" "")

Add-Header $panel 258 "Telegram"
Add-Field $panel 288 "Токен бота для управления" "token" (Saved "TG_TOKEN" "") -Secret
Add-Field $panel 318 "Ваш Telegram ID (владелец)" "owner" (Saved "TG_OWNER_USER_ID" "")
Add-Field $panel 348 "ID чата для уведомлений" "notify" (Saved "TG_NOTIFY_CHAT_ID" "")
Add-Field $panel 378 "Прокси для Telegram" "proxy" (Saved "TG_PROXY" "")

Add-Header $panel 416 "Музыкальные сервисы"
Add-FileField $panel 446 "cookies.txt для YouTube" "ytc"
Add-FileField $panel 476 "rutube_cookies.txt" "rtc"
Add-Field $panel 506 "Токен Яндекс Музыки" "ym" "" -Secret

Add-Header $panel 544 "Что сделать"

$chkDeps = New-Object Windows.Forms.CheckBox
$chkDeps.Text = "Поставить Python, ffmpeg и зависимости в .venv"
$chkDeps.SetBounds(20, 572, 580, 24)
$chkDeps.Checked = $true
$panel.Controls.Add($chkDeps)

$chkDeno = New-Object Windows.Forms.CheckBox
$chkDeno.Text = "Поставить Deno (помогает yt-dlp с YouTube)"
$chkDeno.SetBounds(20, 598, 580, 24)
$chkDeno.Checked = $true
$panel.Controls.Add($chkDeno)

$chkAuto = New-Object Windows.Forms.CheckBox
$chkAuto.Text = "Запускать бота при входе в Windows"
$chkAuto.SetBounds(20, 624, 580, 24)
$chkAuto.Checked = $true
$panel.Controls.Add($chkAuto)

$chkStart = New-Object Windows.Forms.CheckBox
$chkStart.Text = "Запустить бота сразу после установки"
$chkStart.SetBounds(20, 650, 580, 24)
$chkStart.Checked = $true
$panel.Controls.Add($chkStart)

$tabRun = New-Object Windows.Forms.TabPage
$tabRun.Text = "Установка"
$tabs.TabPages.Add($tabRun)

$logBox = New-Object Windows.Forms.TextBox
$logBox.Multiline = $true
$logBox.ReadOnly = $true
$logBox.ScrollBars = "Vertical"
$logBox.WordWrap = $false
$logBox.Dock = "Fill"
$logBox.Font = New-Object Drawing.Font("Consolas", 9)
$logBox.BackColor = [Drawing.Color]::White
$tabRun.Controls.Add($logBox)

$progress = New-Object Windows.Forms.ProgressBar
$progress.Dock = "Bottom"
$progress.Height = 14
$progress.Style = "Marquee"
$progress.MarqueeAnimationSpeed = 0
$tabRun.Controls.Add($progress)

$status = New-Object Windows.Forms.Label
$status.SetBounds(16, 608, 470, 24)
$status.Text = "Заполните данные и нажмите «Установить»."
$form.Controls.Add($status)

$btnInstall = New-Object Windows.Forms.Button
$btnInstall.Text = "Установить"
$btnInstall.SetBounds(500, 602, 160, 32)
$form.Controls.Add($btnInstall)

$btnClose = New-Object Windows.Forms.Button
$btnClose.Text = "Закрыть"
$btnClose.SetBounds(672, 602, 96, 32)
$form.Controls.Add($btnClose)


# Журнал установки читается по мере появления

function Read-New {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return "" }
    if (-not $script:pos.ContainsKey($Path)) { $script:pos[$Path] = [long]0 }
    $text = ""
    try {
        $stream = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
        try {
            if ($stream.Length -gt $script:pos[$Path]) {
                [void]$stream.Seek($script:pos[$Path], [IO.SeekOrigin]::Begin)
                $reader = New-Object IO.StreamReader($stream, [Text.Encoding]::UTF8)
                $text = $reader.ReadToEnd()
                $reader.Dispose()
                $script:pos[$Path] = $stream.Length
            }
        } finally {
            $stream.Dispose()
        }
    } catch { }
    return $text
}

$timer = New-Object Windows.Forms.Timer
$timer.Interval = 400
$timer.Add_Tick({
    $logBox.AppendText((Read-New $LogFile))
    $logBox.AppendText((Read-New $ErrFile))

    if ($script:proc -and $script:proc.HasExited -and -not $script:done) {
        $logBox.AppendText((Read-New $LogFile))
        $logBox.AppendText((Read-New $ErrFile))
        $script:done = $true
        $timer.Stop()
        $progress.MarqueeAnimationSpeed = 0
        $btnInstall.Enabled = $true
        $tabs.TabPages[0].Enabled = $true
        if ($script:proc.ExitCode -eq 0) {
            $status.Text = "Готово."
            [Windows.Forms.MessageBox]::Show(
                "Установка закончена.`n`nЗапуск в окне: run.bat`nЖурнал фонового запуска: logs\bot.log",
                "Готово", "OK", "Information") | Out-Null
        } else {
            $status.Text = "Установка прервалась. Подробности — в журнале на этой вкладке."
            [Windows.Forms.MessageBox]::Show(
                "Установка не закончена. Что именно пошло не так — в журнале на вкладке «Установка».",
                "Ошибка", "OK", "Error") | Out-Null
        }
    }
})


# Установка

$btnInstall.Add_Click({
    if ($script:proc -and -not $script:proc.HasExited) { return }

    $hostName = $script:fields["host"].Text.Trim()
    $login = $script:fields["user"].Text.Trim()
    $password = $script:fields["pass"].Text

    if (-not $hostName -or -not $login -or -not $password) {
        [Windows.Forms.MessageBox]::Show("Заполните адрес сервера, логин и пароль.",
            "Не хватает данных", "OK", "Warning") | Out-Null
        $tabs.SelectedIndex = 0
        return
    }

    foreach ($name in @("tcp", "udp")) {
        $number = 0
        if (-not [int]::TryParse($script:fields[$name].Text.Trim(), [ref]$number) -or
            $number -lt 1 -or $number -gt 65535) {
            [Windows.Forms.MessageBox]::Show("Порт должен быть числом от 1 до 65535.",
                "Неверный порт", "OK", "Warning") | Out-Null
            $tabs.SelectedIndex = 0
            return
        }
    }

    $data = [ordered]@{
        TEAMTALK_HOST     = $hostName
        TEAMTALK_TCP_PORT = $script:fields["tcp"].Text.Trim()
        TEAMTALK_UDP_PORT = $script:fields["udp"].Text.Trim()
        TEAMTALK_USERNAME = $login
        TEAMTALK_PASSWORD = $password
        TEAMTALK_NICKNAME = $script:fields["nick"].Text.Trim()
        TEAMTALK_CHANNEL  = $script:fields["chan"].Text.Trim()
        TG_TOKEN          = $script:fields["token"].Text.Trim()
        TG_OWNER_USER_ID  = $script:fields["owner"].Text.Trim()
        TG_NOTIFY_CHAT_ID = $script:fields["notify"].Text.Trim()
        TG_PROXY          = $script:fields["proxy"].Text.Trim()
        YT_COOKIES        = $script:fields["ytc"].Text.Trim()
        RT_COOKIES        = $script:fields["rtc"].Text.Trim()
        YM_TOKEN          = $script:fields["ym"].Text.Trim()
    }

    $dir = Split-Path -Parent $JsonFile
    if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    ($data | ConvertTo-Json) | Set-Content -LiteralPath $JsonFile -Encoding UTF8

    $deps = if ($chkDeps.Checked) { 1 } else { 0 }
    $deno = if ($chkDeno.Checked) { 1 } else { 0 }
    $auto = if ($chkAuto.Checked) { 1 } else { 0 }
    $startNow = if ($chkStart.Checked) { 1 } else { 0 }

    $arguments = '-NoProfile -ExecutionPolicy Bypass -File "{0}" -Root "{1}" -Setup "{2}" -Json "{3}" -Deps {4} -Auto {5} -Start {6} -Deno {7}' -f `
        $Child, $Root, $Setup, $JsonFile, $deps, $auto, $startNow, $deno

    $logBox.Clear()
    $script:pos = @{}
    $script:done = $false
    $tabs.SelectedIndex = 1
    $progress.MarqueeAnimationSpeed = 30
    $btnInstall.Enabled = $false
    $status.Text = "Установка идёт, окно можно не закрывать."

    $script:proc = Start-Process -FilePath "powershell" -ArgumentList $arguments `
        -RedirectStandardOutput $LogFile -RedirectStandardError $ErrFile `
        -WindowStyle Hidden -PassThru
    $timer.Start()
})

$btnClose.Add_Click({ $form.Close() })

$form.Add_FormClosing({
    if ($script:proc -and -not $script:proc.HasExited) {
        $answer = [Windows.Forms.MessageBox]::Show("Установка ещё идёт. Закрыть окно и прервать её?",
            "Прервать установку?", "YesNo", "Question")
        if ($answer -ne [Windows.Forms.DialogResult]::Yes) {
            $_.Cancel = $true
            return
        }
        & taskkill /F /T /PID $script:proc.Id 2>$null | Out-Null
    }
})

[void]$form.ShowDialog()
