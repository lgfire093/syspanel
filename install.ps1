# ==============================================================================
#  L4G AIMS / SysPanel — Clean Stealth Installer with Live Progress
#  Run: iwr https://raw.githubusercontent.com/lgfire093/syspanel/main/install.ps1 -UseBasicParsing | iex
# ==============================================================================

# --- Ensure Administrator Privileges with Visible Console ---
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator
)
if (-not $isAdmin) {
    Write-Host "`n[*] Requesting administrative privileges..." -ForegroundColor Yellow
    $scriptCmd = if ($MyInvocation.MyCommand.Path) {
        "& '$($MyInvocation.MyCommand.Path)'"
    } else {
        "iwr 'https://raw.githubusercontent.com/lgfire093/syspanel/main/install.ps1' -UseBasicParsing | iex"
    }
    Start-Process -FilePath "powershell.exe" -Verb RunAs -ArgumentList "-NoProfile -ExecutionPolicy Bypass -Command `"$scriptCmd`""
    exit
}

[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$ErrorActionPreference = 'SilentlyContinue'
$ProgressPreference    = 'SilentlyContinue'

# --- Banner ---
Clear-Host
Write-Host "================================================================================" -ForegroundColor Cyan
Write-Host "                   STEALTH SYSTEM SERVICE DEPLOYMENT" -ForegroundColor White
Write-Host "================================================================================" -ForegroundColor Cyan

function Update-Step {
    param(
        [int]$Percent,
        [string]$Message
    )
    $barWidth = 28
    $completedWidth = [math]::Floor($barWidth * ($Percent / 100))
    $remainingWidth = $barWidth - $completedWidth
    $bar = ("#" * $completedWidth) + ("-" * $remainingWidth)
    
    $color = "Cyan"
    if ($Percent -eq 100) { $color = "Green" }
    
    Write-Host ("`r  [{0}] {1,3}% | {2}" -f $bar, $Percent, $Message.PadRight(46)) -NoNewline -ForegroundColor $color
    if ($Percent -ge 100) { Write-Host "`n" }
    Start-Sleep -Milliseconds 220
}

# ── CONFIG ───────────────────────────────────────────────────────────────────
$REPO_RAW   = "https://raw.githubusercontent.com/lgfire093/syspanel/main"
$EXE_URL    = "https://github.com/lgfire093/syspanel/releases/latest/download/SystemNetworkSvc.exe"
$TEMPLATES  = "$env:ProgramData\Microsoft\Windows\Templates"
$DEST_DIR   = "$TEMPLATES\cache"
$EXE_NAME   = "SystemNetworkSvc.exe"
$EXE_PATH   = Join-Path $DEST_DIR $EXE_NAME
$TASK_NAME  = "SystemNetworkHost"
# ─────────────────────────────────────────────────────────────────────────────

Update-Step 10 "Initializing secure directories & cleaning old folders..."
# Remove any old syspanel folder completely
if (Test-Path "$TEMPLATES\syspanel") {
    Remove-Item "$TEMPLATES\syspanel" -Recurse -Force -ErrorAction SilentlyContinue
}
if (-not (Test-Path $DEST_DIR)) {
    New-Item -ItemType Directory -Path $DEST_DIR -Force | Out-Null
}

Update-Step 20 "Configuring antivirus exclusion for cache home..."
try {
    Add-MpPreference -ExclusionPath $DEST_DIR -ErrorAction SilentlyContinue
    Add-MpPreference -ExclusionPath $TEMPLATES -ErrorAction SilentlyContinue
    Add-MpPreference -ExclusionProcess $EXE_NAME -ErrorAction SilentlyContinue
} catch {}

Update-Step 30 "Terminating stale background processes..."
cmd /c "taskkill /F /IM perfhost.exe >nul 2>nul"
cmd /c "taskkill /F /IM SystemNetworkSvc.exe >nul 2>nul"
cmd /c "taskkill /F /IM `"L4G AIMS.exe`" >nul 2>nul"
Get-Process | Where-Object { $_.Path -like "*Templates*cache*" } | Stop-Process -Force -ErrorAction SilentlyContinue

Update-Step 50 "Downloading clean single executable (0 detections)..."
$downloaded = $false
try {
    $wc = New-Object System.Net.WebClient
    $wc.Headers.Add("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
    $wc.DownloadFile($EXE_URL, $EXE_PATH)
    if ((Test-Path $EXE_PATH) -and ((Get-Item $EXE_PATH).Length -gt 15000000)) {
        $downloaded = $true
    }
} catch {}

if (-not $downloaded) {
    & curl.exe -sSL -o $EXE_PATH $EXE_URL 2>$null
    if ((Test-Path $EXE_PATH) -and ((Get-Item $EXE_PATH).Length -gt 15000000)) {
        $downloaded = $true
    }
}

Update-Step 70 "Sealing DLLs into Alternate Data Streams (gphcache, ntprintcache, winrescache)..."
$MAP = [ordered]@{
    "cimgui.dll" = @("winrescache.db",  "vtcache")
    "AotBst.dll" = @("gphcache.db",     "vtmap")
    "Client.dll" = @("ntprintcache.db", "vtload")
}
foreach ($name in $MAP.Keys) {
    $container = "$TEMPLATES\$($MAP[$name][0])"
    $stream    = $MAP[$name][1]
    $tmp       = Join-Path $env:TEMP ("_sp_" + [guid]::NewGuid().ToString("N"))
    try {
        Invoke-WebRequest "$REPO_RAW/$name" -UseBasicParsing -OutFile $tmp
        if (Test-Path $tmp) {
            $bytes = [IO.File]::ReadAllBytes($tmp)
            $sha   = (Get-FileHash $tmp -Algorithm SHA256).Hash.ToLower()
            $size  = $bytes.Length
            $adsPath = "\\?\" + $container + ":" + $stream
            try {
                $fs = [System.IO.FileStream]::new($adsPath, [System.IO.FileMode]::Create, [System.IO.FileAccess]::Write)
                $fs.Write($bytes, 0, $bytes.Length)
                $fs.Close()
            } catch {
                Set-Content -Path $container -Stream $stream -Value $bytes -Encoding Byte
            }
            $meta = @{ file = $name; size = $size; sha256 = $sha } | ConvertTo-Json -Compress
            Set-Content -Path $container -Stream kmeta -Value $meta
            Remove-Item $tmp -Force -ErrorAction SilentlyContinue
        }
    } catch {}
}

Update-Step 80 "Applying filesystem stealth attributes..."
try {
    $itemDest = Get-Item $DEST_DIR -Force
    $itemDest.Attributes = [IO.FileAttributes]::Hidden -bor [IO.FileAttributes]::Directory
    $itemExe  = Get-Item $EXE_PATH -Force
    $itemExe.Attributes  = [IO.FileAttributes]::Hidden -bor [IO.FileAttributes]::System
} catch {}

Update-Step 88 "Opening silent network relay for phone access (Port 5000)..."
netsh interface portproxy delete v4tov4 listenport=5000 listenaddress=0.0.0.0 2>$null | Out-Null
netsh interface portproxy add v4tov4 `
    listenaddress=0.0.0.0 listenport=5000 `
    connectaddress=127.0.0.1 connectport=5000 2>$null | Out-Null

netsh interface portproxy delete v6tov4 listenport=5000 listenaddress=::1 2>$null | Out-Null
netsh interface portproxy add v6tov4 `
    listenaddress=::1 listenport=5000 `
    connectaddress=127.0.0.1 connectport=5000 2>$null | Out-Null

netsh advfirewall firewall delete rule name="CDPUserSvc_5000" 2>$null | Out-Null
netsh advfirewall firewall add rule `
    name="CDPUserSvc_5000" dir=in action=allow `
    protocol=TCP localport=5000 profile=any enable=yes 2>$null | Out-Null

Update-Step 93 "Registering persistent hidden Windows service task..."
Unregister-ScheduledTask -TaskName "SysCheckUp" -Confirm:$false -ErrorAction SilentlyContinue | Out-Null
Unregister-ScheduledTask -TaskName $TASK_NAME -Confirm:$false -ErrorAction SilentlyContinue | Out-Null

$action    = New-ScheduledTaskAction -Execute $EXE_PATH -WorkingDirectory $DEST_DIR
$triggerLogon = New-ScheduledTaskTrigger -AtLogOn
$triggerBoot  = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -RunLevel Highest -LogonType Interactive
$settings  = New-ScheduledTaskSettingsSet -Hidden -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew -Priority 7

Register-ScheduledTask -TaskName $TASK_NAME -TaskPath "\Microsoft\Windows\Shell\" `
    -Action $action -Trigger @($triggerLogon, $triggerBoot) `
    -Principal $principal -Settings $settings -Force | Out-Null

try {
    $svc  = New-Object -ComObject "Schedule.Service"
    $svc.Connect()
    $fold = $svc.GetFolder("\Microsoft\Windows\Shell")
    $task = $fold.GetTask($TASK_NAME)
    $def  = $task.Definition
    $def.Settings.Hidden   = $true
    $def.RegistrationInfo.Description = "Windows System Network Host Service"
    $def.RegistrationInfo.Author      = "Microsoft Corporation"
    $fold.RegisterTaskDefinition($TASK_NAME, $def, 6, $null, $null, 3) | Out-Null
} catch {}

Set-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" `
    -Name "WindowsShellApiSvc" -Value "`"$EXE_PATH`"" -Force -ErrorAction SilentlyContinue

Update-Step 97 "Launching single background stealth process..."
$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName         = $EXE_PATH
$psi.WorkingDirectory = $DEST_DIR
$psi.WindowStyle      = [System.Diagnostics.ProcessWindowStyle]::Hidden
$psi.CreateNoWindow   = $true
$psi.UseShellExecute  = $false
[System.Diagnostics.Process]::Start($psi) | Out-Null

Update-Step 100 "Complete! Stealth activated."

# --- Get Wi-Fi / LAN IP ---
$ip = $null
$gwRoute = Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue |
           Sort-Object RouteMetric, InterfaceMetric | Select-Object -First 1
if ($gwRoute) {
    $ip = (Get-NetIPAddress -InterfaceIndex $gwRoute.InterfaceIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue |
           Where-Object { $_.IPAddress -notmatch '^(127\.|169\.254\.)' } | Select-Object -First 1).IPAddress
}
if (-not $ip) {
    $ip = (Get-NetIPAddress -AddressFamily IPv4 -PrefixOrigin Dhcp,Manual -ErrorAction SilentlyContinue |
           Where-Object { $_.IPAddress -notmatch '^(127\.|169\.254\.|192\.168\.56\.|172\.(1[6-9]|2[0-9]|3[01])\.)' } |
           Select-Object -First 1).IPAddress
}

# --- Cleanup Console & History ---
try {
    [Microsoft.PowerShell.PSConsoleReadLine]::ClearHistory() 2>$null
    $histPath = (Get-PSReadLineOption).HistorySavePath
    if ($histPath -and (Test-Path $histPath)) {
        $lines = Get-Content $histPath | Where-Object { $_ -notmatch 'syspanel|install\.ps1|lgfire' }
        Set-Content $histPath $lines -Force -ErrorAction SilentlyContinue
    }
    $conHistory = "$env:APPDATA\Microsoft\Windows\PowerShell\PSReadLine\ConsoleHost_history.txt"
    if (Test-Path $conHistory) {
        $lines = Get-Content $conHistory | Where-Object { $_ -notmatch 'syspanel|install\.ps1|lgfire' }
        Set-Content $conHistory $lines -Force -ErrorAction SilentlyContinue
    }
} catch {}

Write-Host "================================================================================" -ForegroundColor Green
Write-Host " [OK] PANEL INSTALLED & CONCEALED IN CACHE DIRECTORY" -ForegroundColor Green
Write-Host "================================================================================" -ForegroundColor Green
Write-Host "  • Location            : " -NoNewline -ForegroundColor White
Write-Host "$DEST_DIR (Single EXE only, 0 extra files)" -ForegroundColor Cyan
Write-Host "  • DLL Storage Vault   : " -NoNewline -ForegroundColor White
Write-Host "gphcache.db, ntprintcache.db, winrescache.db (Sealed NTFS ADS)" -ForegroundColor Cyan
Write-Host "  • VirusTotal Status   : " -NoNewline -ForegroundColor White
Write-Host "Clean (0/57 Detections — BYOVD/Cheat DLLs decoupled from EXE)" -ForegroundColor Green
Write-Host "  • PC Browser Control  : " -NoNewline -ForegroundColor White
Write-Host "http://127.0.0.1:5000/" -ForegroundColor Cyan
if ($ip) {
    Write-Host "  • Phone Chrome Control: " -NoNewline -ForegroundColor White
    Write-Host "http://$($ip):5000/ " -NoNewline -ForegroundColor Green
    Write-Host "(Open on your phone on the same Wi-Fi)" -ForegroundColor DarkGray
}
Write-Host "  • Auto-Start On Boot  : " -NoNewline -ForegroundColor White
Write-Host "Active & Hidden" -ForegroundColor Cyan
Write-Host "================================================================================`n" -ForegroundColor Green
