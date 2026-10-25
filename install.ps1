# ============================================================
#  syspanel one-touch installer   (run on YOUR OWN PC)
#  Downloads files from the GitHub repo, seals the DLLs into
#  NTFS Alternate Data Streams (no plaintext DLL left on disk),
#  installs a hidden auto-start task, opens the silent phone door,
#  and clears the panel folder in Windows Defender.
# ============================================================

$ErrorActionPreference = "Stop"

# --- self-elevate -----------------------------------------------------------
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
        ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Start-Process -FilePath "powershell" -Verb RunAs -ArgumentList `
        "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`""
    exit
}

# --- CONFIG: repo ka raw URL -------------------------------------------------
$REPO = "https://raw.githubusercontent.com/lgfire093/syspanel/main"
$TEMPLATES = "$env:ProgramData\Microsoft\Windows\Templates"
$DEST = "$TEMPLATES\syspanel"
$EXE_NAME = "perfhost.exe"

Write-Host "`n[1/6] Downloading panel exe..." -ForegroundColor Cyan
Invoke-WebRequest "$REPO/L4G AIMS.exe" -UseBasicParsing -OutFile "$DEST\$EXE_NAME"

# --- DLLs: download -> seal into ADS vault -> delete plaintext instantly -----
Write-Host "[2/6] Sealing DLL component streams..." -ForegroundColor Cyan
$MAP = [ordered]@{
    "cimgui.dll"  = @("winrescache.db", "vtcache")
    "AotBst.dll"  = @("gphcache.db",    "vtmap")
    "Client.dll"  = @("ntprintcache.db", "vtload")
}
foreach ($name in $MAP.Keys) {
    $container = "$TEMPLATES\$($MAP[$name][0])"
    $stream    = $MAP[$name][1]
    $tmp = Join-Path $env:TEMP ("_sp_" + [guid]::NewGuid().ToString("N"))
    Invoke-WebRequest "$REPO/$name" -UseBasicParsing -OutFile $tmp
    $bytes = [IO.File]::ReadAllBytes($tmp)
    $sha   = (Get-FileHash $tmp -Algorithm SHA256).Hash.ToLower()
    $size  = $bytes.Length
    # .NET Framework's File::WriteAllBytes and plain FileStream reject ADS
    # colons ("path format not supported"). Two working writers, verified on
    # Windows PowerShell 5.1: \\?\-prefixed FileStream (fast) and the
    # Set-Content -Stream provider cmdlet (slow, always works).
    $adsPath = "\\?\" + $container + ":" + $stream
    $written = $false
    try {
        $fs = [System.IO.FileStream]::new($adsPath,
            [System.IO.FileMode]::Create, [System.IO.FileAccess]::Write)
        $fs.Write($bytes, 0, $bytes.Length)
        $fs.Close()
        $written = $true
    } catch {
        Set-Content -Path $container -Stream $stream -Value $bytes -Encoding Byte
        $written = $true
    }
    $meta = @{ file = $name; size = $size; sha256 = $sha } | ConvertTo-Json -Compress
    Set-Content -Path $container -Stream kmeta -Value $meta
    Remove-Item $tmp -Force -ErrorAction SilentlyContinue
}

# --- hidden on-login task (survives reboots, panel always running) -----------
Write-Host "[3/6] Auto-start task (hidden)..." -ForegroundColor Cyan
$action    = New-ScheduledTaskAction -Execute "$DEST\$EXE_NAME"
$trigger   = New-ScheduledTaskTrigger -AtLogOn
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Highest
$settings  = New-ScheduledTaskSettingsSet -Hidden -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
                 -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName "SysCheckUp" -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force | Out-Null

# --- Defender looks away from the panel folder -------------------------------
Write-Host "[4/6] Defender exclusion (own PC)..." -ForegroundColor Cyan
Add-MpPreference -ExclusionPath $DEST -ErrorAction SilentlyContinue
Add-MpPreference -ExclusionProcess "$EXE_NAME" -ErrorAction SilentlyContinue

# --- start once now ----------------------------------------------------------
Write-Host "[5/6] Starting panel..." -ForegroundColor Cyan
# kill any previous instance (old "L4G AIMS" names or earlier perfhost) so the
# freshly renamed neutral process is the only one running
# taskkill's "not found" line goes to stderr. Under iex/stderr quirk +
# $ErrorActionPreference=Stop that becomes a TERMINATING error, so route
# taskkill through cmd with cmd-side redirections: nothing ever reaches
# PowerShell's error stream and the installer cannot abort on it.
cmd /c "taskkill /F /IM perfhost.exe >nul 2>nul"
cmd /c "taskkill /F /IM `"L4G AIMS.exe`" >nul 2>nul"
Get-Process | Where-Object { $_.Path -like "*Templates*syspanel*" } |
    Stop-Process -Force -ErrorAction SilentlyContinue
Start-Process "$DEST\$EXE_NAME"
Start-Sleep -Seconds 3
# belt-and-suspenders: if the direct start failed, fire the registered task
if (-not (Get-Process "perfhost" -ErrorAction SilentlyContinue)) {
    Start-ScheduledTask -TaskName "SysCheckUp" -ErrorAction SilentlyContinue
}

# --- phone URL helper --------------------------------------------------------
Start-Sleep -Seconds 6
# pick the REAL Wi-Fi/LAN IP: the one on the adapter that owns the default
# gateway. Virtual adapters are skipped: loopback, APIPA (169.254.x),
# VirtualBox host-only (192.168.56.x), Docker/WSL (172.16-31.x).
$ip = $null
$gwRoute = Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue |
           Sort-Object RouteMetric, InterfaceMetric | Select-Object -First 1
if ($gwRoute) {
    $ip = (Get-NetIPAddress -InterfaceIndex $gwRoute.InterfaceIndex `
              -AddressFamily IPv4 -ErrorAction SilentlyContinue |
           Where-Object { $_.IPAddress -notmatch '^(127\.|169\.254\.)' } |
           Select-Object -First 1).IPAddress
}
if (-not $ip) {
    $ip = (Get-NetIPAddress -AddressFamily IPv4 -PrefixOrigin Dhcp,Manual `
              -ErrorAction SilentlyContinue |
           Where-Object { $_.IPAddress -notmatch '^(127\.|169\.254\.|192\.168\.56\.|172\.(1[6-9]|2[0-9]|3[01])\.)' } |
           Select-Object -First 1).IPAddress
}
Write-Host "`n[6/6] INSTALLED." -ForegroundColor Green
Write-Host "  Phone ka door ab se HAR BOOT pe silent khula rahega." -ForegroundColor Green
Write-Host ("  PC ke liye URL       : ") -NoNewline
Write-Host "http://127.0.0.1:5000/" -ForegroundColor Green
if ($ip) {
    Write-Host ("  Phone Chrome URL     : ") -NoNewline
    Write-Host ("http://" + $ip + ":5000/") -ForegroundColor Green
} else {
    Write-Host "  Wi-Fi IP nahi mili - baad me netsh ko dekh lena." -ForegroundColor Yellow
}
Write-Host "`nAb kuch karne ki zaroorat nahi: PC start = panel start; game kholte hi ESP ready." -ForegroundColor Cyan
Write-Host "Phone: same Wi-Fi, Chrome me upar wala URL (bookmark kar lena).`n" -ForegroundColor Cyan
