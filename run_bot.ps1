# Запуск планер-бота на компьютере Ольги.
# Ждёт VPN (v2RayTun, локальный прокси 10801), затем держит бота запущенным,
# перезапуская его, если он упал. Лог — bot_local.log рядом со скриптом.

$ErrorActionPreference = 'Continue'
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $dir
$py  = Join-Path $dir '.venv\Scripts\python.exe'
$log = Join-Path $dir 'bot_local.log'

function Say($m) {
  $line = "[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m
  Add-Content -Path $log -Value $line -Encoding UTF8
}

# лог не раздуваем: больше 5 МБ — начинаем заново
if ((Test-Path $log) -and ((Get-Item $log).Length -gt 5MB)) { Remove-Item $log -Force }

Say "=== запуск ==="

# бот ходит в Telegram и Groq только через VPN
$env:HTTP_PROXY  = 'http://127.0.0.1:10801'
$env:HTTPS_PROXY = 'http://127.0.0.1:10801'
$env:NO_PROXY    = 'localhost,127.0.0.1'

function Wait-Vpn {
  for ($i = 0; $i -lt 120; $i++) {   # до 20 минут
    try {
      $c = New-Object Net.Sockets.TcpClient
      $c.Connect('127.0.0.1', 10801); $c.Close()
      return $true
    } catch {
      if ($i -eq 0) { Say "жду VPN на порту 10801..." }
      Start-Sleep -Seconds 10
    }
  }
  return $false
}

while ($true) {
  if (-not (Wait-Vpn)) { Say "VPN не поднялся за 20 минут, пробую ещё раз"; continue }
  Say "VPN на месте, стартую бота"
  & $py 'run_local.py' *>> $log
  Say "бот остановился (код $LASTEXITCODE), перезапуск через 20 секунд"
  Start-Sleep -Seconds 20
}
