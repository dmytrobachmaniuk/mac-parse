# Зупиняє фонового бота (усі процеси «mac-parser bot» разом із дочірніми).
param([switch]$Quiet)
$procs = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'mac-parser(\.exe)?"?\s+bot' }
foreach ($p in $procs) {
    taskkill /PID $p.ProcessId /T /F 2>$null | Out-Null
}
if (-not $Quiet) {
    if ($procs) { Write-Host "Бота зупинено ($(@($procs).Count) процес.)" } else { Write-Host "Бот не був запущений" }
}
