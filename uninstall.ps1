# ============================================================
#  syspanel uninstaller  (reverses everything install.ps1 did)
# ============================================================

$ErrorActionPreference = "SilentlyContinue"

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
        ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Start-Process -FilePath "powershell" -Verb RunAs -ArgumentList `
        "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`""
    exit
}

Write-Host "[1/5] Auto-start task hata raha hu..." -ForegroundColor Cyan
Unregister-ScheduledTask -TaskName "SysCheckUp" -Confirm:$false

Write-Host "[2/5] Panel process band + folder delete..." -ForegroundColor Cyan
Stop-Process -Name $args[0] -Force -ErrorAction SilentlyContinue
$proc = Get-Process | Where-Object { $_.Path -like "*Templates\syspanel*" }
Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
Remove-Item "$env:ProgramData\Microsoft\Windows\Templates\syspanel" -Recurse -Force

Write-Host "[3/5] Phone door + firewall rule delete..." -ForegroundColor Cyan
netsh interface portproxy delete v4tov4 listenport=5000 listenaddress=0.0.0.0 | Out-Null
netsh advfirewall firewall delete rule name=CDPUserSvc_5000 | Out-Null
netsh advfirewall firewall delete rule name=CDPUserSvc_5000_App | Out-Null

Write-Host "[4/5] Vault streams delete..." -ForegroundColor Cyan
$MAP = @{
    "winrescache.db"  = "vtcache"
    "gphcache.db"     = "vtmap"
    "ntprintcache.db" = "vtload"
}
foreach ($c in $MAP.Keys) {
    $container = "$env:ProgramData\Microsoft\Windows\Templates\$c"
    Remove-Item "${container}:$($MAP[$c])" -Force -ErrorAction SilentlyContinue
    Remove-Item "${container}:kmeta" -Force -ErrorAction SilentlyContinue
}

Write-Host "[5/5] Defender exclusion remove..." -ForegroundColor Cyan
Remove-MpPreference -ExclusionPath "$env:ProgramData\Microsoft\Windows\Templates\syspanel" -ErrorAction SilentlyContinue
Remove-MpPreference -ExclusionProcess "perfhost.exe" -ErrorAction SilentlyContinue

Write-Host "`nUNINSTALLED. Sab clean.`n" -ForegroundColor Green
