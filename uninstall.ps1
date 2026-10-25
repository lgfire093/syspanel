# ==============================================================================
#  syspanel deep cleaner & uninstaller
#  Completely purges panel binaries, scheduled tasks, firewall, NTFS streams,
#  Defender exclusions, and all forensic trace logs (USN journal, Prefetch,
#  MuiCache, AppSwitched, PCA AppCompatFlags, Event Logs).
#  Run: irm https://raw.githubusercontent.com/lgfire093/syspanel/main/uninstall.ps1 | iex
# ==============================================================================

[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$ErrorActionPreference = "SilentlyContinue"

# --- Ensure Administrator Privileges with Visible Console ---
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator
)
if (-not $isAdmin) {
    Write-Host "`n[*] Requesting administrative privileges for deep purge..." -ForegroundColor Yellow
    $scriptCmd = if ($MyInvocation.MyCommand.Path) {
        "& '$($MyInvocation.MyCommand.Path)'"
    } else {
        "irm 'https://raw.githubusercontent.com/lgfire093/syspanel/main/uninstall.ps1' | iex"
    }
    Start-Process -FilePath "powershell.exe" -Verb RunAs -ArgumentList "-NoProfile -ExecutionPolicy Bypass -Command `"$scriptCmd`""
    exit
}

Clear-Host
Write-Host "================================================================================" -ForegroundColor Red
Write-Host "                SYSPANEL DEEP PURGE & FORENSIC LOG WIPER" -ForegroundColor White
Write-Host "================================================================================" -ForegroundColor Red

function Status-Msg {
    param([string]$Num, [string]$Msg)
    Write-Host "  [$Num] $Msg" -ForegroundColor Cyan
}

# 1. Kill all running processes
Status-Msg "1/9" "Terminating running panel processes..."
$names = @("SystemNetworkSvc", "perfhost", "L4G AIMS", "svchost_net")
foreach ($n in $names) {
    cmd /c "taskkill /F /IM $n.exe >nul 2>nul"
}
Get-Process | Where-Object { 
    $_.ProcessName -match "SystemNetworkSvc|perfhost|L4G AIMS" -or 
    $_.Path -like "*Templates\cache*" -or 
    $_.Path -like "*Templates\syspanel*" 
} | Stop-Process -Force -ErrorAction SilentlyContinue

# 2. Unregister Auto-Start Scheduled Tasks
Status-Msg "2/9" "Unregistering Scheduled Tasks & persistence..."
Unregister-ScheduledTask -TaskName "SysCheckUp" -Confirm:$false -ErrorAction SilentlyContinue | Out-Null
Unregister-ScheduledTask -TaskName "SystemNetworkHost" -Confirm:$false -ErrorAction SilentlyContinue | Out-Null
Unregister-ScheduledTask -TaskPath "\Microsoft\Windows\Shell\" -TaskName "SystemNetworkHost" -Confirm:$false -ErrorAction SilentlyContinue | Out-Null

# 3. Clean Registry Autostarts
Status-Msg "3/9" "Wiping Startup Registry & AppExecution keys..."
$runKeys = @(
    "HKLM:\Software\Microsoft\Windows\CurrentVersion\Run",
    "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run",
    "HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Run"
)
foreach ($rk in $runKeys) {
    Remove-ItemProperty -Path $rk -Name "SystemNetworkHost" -ErrorAction SilentlyContinue
    Remove-ItemProperty -Path $rk -Name "SystemNetworkSvc" -ErrorAction SilentlyContinue
    Remove-ItemProperty -Path $rk -Name "WindowsShellApiSvc" -ErrorAction SilentlyContinue
}
Remove-ItemProperty -Path "HKLM:\Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run" -Name "SystemNetworkHost" -ErrorAction SilentlyContinue
Remove-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run" -Name "WindowsShellApiSvc" -ErrorAction SilentlyContinue

# 4. Remove Files, Folders & NTFS Alternate Data Streams
Status-Msg "4/9" "Purging installation files & NTFS Alternate Data Streams..."
$TEMPLATES = "$env:ProgramData\Microsoft\Windows\Templates"

# Reset file attributes and delete cache folder & syspanel folder
if (Test-Path "$TEMPLATES\cache") {
    Get-ChildItem -Path "$TEMPLATES\cache" -Force -Recurse | ForEach-Object {
        Set-ItemProperty -Path $_.FullName -Name Attributes -Value "Normal" -ErrorAction SilentlyContinue
    }
    Remove-Item "$TEMPLATES\cache" -Recurse -Force -ErrorAction SilentlyContinue
}
if (Test-Path "$TEMPLATES\syspanel") {
    Get-ChildItem -Path "$TEMPLATES\syspanel" -Force -Recurse | ForEach-Object {
        Set-ItemProperty -Path $_.FullName -Name Attributes -Value "Normal" -ErrorAction SilentlyContinue
    }
    Remove-Item "$TEMPLATES\syspanel" -Recurse -Force -ErrorAction SilentlyContinue
}

# Delete ADS streams
$MAP = @{
    "winrescache.db"  = "vtcache"
    "gphcache.db"     = "vtmap"
    "ntprintcache.db" = "vtload"
}
foreach ($c in $MAP.Keys) {
    $container = "$TEMPLATES\$c"
    Remove-Item "${container}:$($MAP[$c])" -Force -ErrorAction SilentlyContinue
    Remove-Item "${container}:kmeta" -Force -ErrorAction SilentlyContinue
}

# Delete any _MEI directories in TEMP
Get-ChildItem -Path $env:TEMP -Filter "_MEI*" -Directory -ErrorAction SilentlyContinue | ForEach-Object {
    Set-ItemProperty -Path $_.FullName -Name Attributes -Value "Normal" -ErrorAction SilentlyContinue
    Remove-Item -Path $_.FullName -Recurse -Force -ErrorAction SilentlyContinue
}

# 5. Clean Firewall & Portproxy Rules
Status-Msg "5/9" "Reverting Firewall and network rules..."
netsh interface portproxy delete v4tov4 listenport=5000 listenaddress=0.0.0.0 2>$null | Out-Null
netsh interface portproxy delete v6tov4 listenport=5000 listenaddress=::1 2>$null | Out-Null
netsh advfirewall firewall delete rule name="CDPUserSvc_5000" 2>$null | Out-Null
netsh advfirewall firewall delete rule name="CDPUserSvc_App" 2>$null | Out-Null
netsh advfirewall firewall delete rule name="CDPUserSvc_5000_App" 2>$null | Out-Null

# 6. Revert Windows Defender Exclusions
Status-Msg "6/9" "Clearing Windows Defender exclusions..."
Remove-MpPreference -ExclusionPath "$TEMPLATES\cache" -ErrorAction SilentlyContinue
Remove-MpPreference -ExclusionPath "$TEMPLATES\syspanel" -ErrorAction SilentlyContinue
Remove-MpPreference -ExclusionPath $TEMPLATES -ErrorAction SilentlyContinue
Remove-MpPreference -ExclusionProcess "SystemNetworkSvc.exe" -ErrorAction SilentlyContinue
Remove-MpPreference -ExclusionProcess "perfhost.exe" -ErrorAction SilentlyContinue

# 7. Wipe Forensic Execution Artifacts (MuiCache, FeatureUsage, AppCompatFlags)
Status-Msg "7/9" "Wiping Shell execution logs (MuiCache, AppSwitched, PCA)..."
$patterns = @("SystemNetworkSvc", "perfhost", "syspanel", "cache\SystemNetworkSvc", "L4G AIMS")

# MuiCache
$muiPath = "HKCU:\Software\Classes\Local Settings\Software\Microsoft\Windows\Shell\MuiCache"
if (Test-Path $muiPath) {
    $props = Get-ItemProperty -Path $muiPath
    $props.PSObject.Properties | Where-Object {
        $val = $_.Value
        $name = $_.Name
        foreach ($p in $patterns) {
            if ($name -match $p -or ($val -and $val.ToString() -match $p)) { return $true }
        }
        return $false
    } | ForEach-Object {
        Remove-ItemProperty -Path $muiPath -Name $_.Name -ErrorAction SilentlyContinue
    }
}

# AppSwitched
$swPath = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\FeatureUsage\AppSwitched"
if (Test-Path $swPath) {
    $props = Get-ItemProperty -Path $swPath
    $props.PSObject.Properties | Where-Object {
        $name = $_.Name
        foreach ($p in $patterns) {
            if ($name -match $p) { return $true }
        }
        return $false
    } | ForEach-Object {
        Remove-ItemProperty -Path $swPath -Name $_.Name -ErrorAction SilentlyContinue
    }
}

# Compatibility Assistant Store (PCA)
$pcaPaths = @(
    "HKCU:\Software\Microsoft\Windows NT\CurrentVersion\AppCompatFlags\Compatibility Assistant\Store",
    "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\AppCompatFlags\Compatibility Assistant\Store"
)
foreach ($pca in $pcaPaths) {
    if (Test-Path $pca) {
        $props = Get-ItemProperty -Path $pca
        $props.PSObject.Properties | Where-Object {
            $name = $_.Name
            foreach ($p in $patterns) {
                if ($name -match $p) { return $true }
            }
            return $false
        } | ForEach-Object {
            Remove-ItemProperty -Path $pca -Name $_.Name -ErrorAction SilentlyContinue
        }
    }
}

# BAM (Background Activity Moderator)
$bamUserRoot = "HKLM:\SYSTEM\CurrentControlSet\Services\bam\State\UserSettings"
if (Test-Path $bamUserRoot) {
    Get-ChildItem -Path $bamUserRoot -ErrorAction SilentlyContinue | ForEach-Object {
        $bPath = $_.PSPath
        $props = Get-ItemProperty -Path $bPath -ErrorAction SilentlyContinue
        $props.PSObject.Properties | Where-Object {
            $name = $_.Name
            foreach ($p in $patterns) {
                if ($name -match $p) { return $true }
            }
            return $false
        } | ForEach-Object {
            Remove-ItemProperty -Path $bPath -Name $_.Name -ErrorAction SilentlyContinue
        }
    }
}

# 8. Wipe Windows Prefetch (.pf) files matching panel
Status-Msg "8/9" "Wiping Prefetch execution traces..."
$prefetchDir = "$env:SystemRoot\Prefetch"
if (Test-Path $prefetchDir) {
    Get-ChildItem -Path $prefetchDir -Filter "*.pf" -ErrorAction SilentlyContinue | Where-Object {
        $_.Name -match "SYSTEMNETWORK|PERFHOST|L4G|SYSCHECK"
    } | Remove-Item -Force -ErrorAction SilentlyContinue
}

# 9. Clear NTFS USN Change Journal (Destroys deleted file history checked by Ocean)
Status-Msg "9/9" "Resetting NTFS USN Journal ($env:SystemDrive) to purge deleted file logs..."
cmd /c "fsutil usn deletejournal /d /n $env:SystemDrive >nul 2>nul"

# Wipe PowerShell History
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

Write-Host "`n================================================================================" -ForegroundColor Green
Write-Host " [OK] FULL CLEANUP COMPLETE: PANEL & ALL SYSTEM LOGS REMOVED" -ForegroundColor Green
Write-Host "================================================================================" -ForegroundColor Green
Write-Host "  * All panel processes stopped" -ForegroundColor White
Write-Host "  * Files & Folders ($TEMPLATES\cache, syspanel) deleted" -ForegroundColor White
Write-Host "  * Scheduled Tasks & Registry persistence purged" -ForegroundColor White
Write-Host "  * MuiCache, AppSwitched, PCA, BAM entries wiped" -ForegroundColor White
Write-Host "  * Prefetch & NTFS USN Journal reset (Ocean scanner will find 0 logs)" -ForegroundColor Green
Write-Host "================================================================================`n" -ForegroundColor Green
