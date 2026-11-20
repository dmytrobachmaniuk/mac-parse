# Запускає бота у фоні, незалежно від вікон і терміналів (через WMI).
# Лог: data\bot.log. Повторний запуск спершу зупиняє стару копію — двох ботів на одному токені бути не може.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
& "$PSScriptRoot\stop-bot.ps1" -Quiet

$uv = (Get-Command uv -ErrorAction Stop).Source
New-Item -ItemType Directory -Force -Path "$root\data" | Out-Null
$log = "$root\data\bot.log"
$cmd = "cmd.exe /c cd /d `"$root`" && `"$uv`" run mac-parser bot >> `"$log`" 2>&1"

$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $cmd; CurrentDirectory = $root }
if ($r.ReturnValue -ne 0) { throw "Не вдалося запустити бота (код $($r.ReturnValue))" }
Write-Host "Бот запущено у фоні (PID $($r.ProcessId)). Лог: $log"
