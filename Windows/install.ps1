# FM27Real one-step installer for Windows.
#   git clone https://github.com/David-bit-jpg/FM27Real.git  then double-click FM27Real\Windows\install.bat
$ErrorActionPreference = "Stop"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Repo = Split-Path -Parent $Here
function Ok($m)   { Write-Host "  [OK] $m" -ForegroundColor Green }
function Warn($m) { Write-Host "  [!]  $m" -ForegroundColor Yellow }
function Fail($m) { Write-Host "  [X]  $m" -ForegroundColor Red; exit 1 }

Write-Host "FM27Real - install (Windows)"

# 1. Python 3.9+
function Find-Python {
    foreach ($c in @("py -3", "python", "python3")) {
        try {
            $parts = @($c.Split(" "))
            $rest = @($parts | Select-Object -Skip 1) + @("-c", "import sys; print('%d.%d' % sys.version_info[:2])")
            $v = & $parts[0] @rest 2>$null
            if ($LASTEXITCODE -eq 0 -and $v -and [version]$v -ge [version]"3.9") { return $c }
        } catch {}
    }
    return $null
}
$py = Find-Python
if (-not $py) {
    Warn "Python 3.9+ not found."
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        $a = Read-Host "Install Python 3.12 now with winget? (y/n)"
        if ($a -eq "y") {
            winget install -e --id Python.Python.3.12 --accept-package-agreements
            $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
            $py = Find-Python
        }
    }
    if (-not $py) { Fail "Install Python 3.9+ from https://www.python.org/downloads/ (tick 'Add to PATH'), then run install.bat again." }
}
Ok "Python ($py)"

# 2. Football Manager 27 (Steam library folders)
function Find-FM {
    $roots = @()
    try { $roots += (Get-ItemProperty "HKCU:\Software\Valve\Steam" -ErrorAction Stop).SteamPath } catch {}
    $roots += "${env:ProgramFiles(x86)}\Steam", "$env:ProgramFiles\Steam"
    $libs = @()
    foreach ($r in $roots | Where-Object { $_ } | Select-Object -Unique) {
        $r = $r -replace "/", "\"
        $libs += $r
        $vdf = Join-Path $r "steamapps\libraryfolders.vdf"
        if (Test-Path $vdf) {
            foreach ($m in [regex]::Matches((Get-Content $vdf -Raw), '"path"\s+"([^"]+)"')) { $libs += ($m.Groups[1].Value -replace "\\\\", "\") }
        }
    }
    foreach ($l in $libs | Select-Object -Unique) {
        $dir = Join-Path $l "steamapps\common\Football Manager 27"
        if (Test-Path (Join-Path $dir "GameAssembly.dll")) {
            $exe = @("fm.exe", "Football Manager 27.exe") | ForEach-Object { Join-Path $dir $_ } | Where-Object { Test-Path $_ } | Select-Object -First 1
            if (-not $exe) {
                $exe = Get-ChildItem $dir -Filter *.exe | Where-Object { $_.Name -notmatch "Crash|Unity|Launcher|redist" } | Select-Object -First 1 -ExpandProperty FullName
            }
            if ($exe) { return $exe }
        }
    }
    return $null
}
$fm = Find-FM
if (-not $fm) {
    Warn "Football Manager 27 not found in your Steam libraries."
    $fm = Read-Host "Paste the full path of the game's .exe (in ...\steamapps\common\Football Manager 27\)"
    if (-not (Test-Path $fm)) { Fail "Not found: $fm" }
}
Ok "Football Manager 27: $fm"

# 3. Bridge DLL
$dll = Join-Path $Here "bin\fm27real.dll"
if (-not (Test-Path $dll)) { Fail "Missing $dll (download the repository again)." }
Get-ChildItem -Recurse $Here | Unblock-File -ErrorAction SilentlyContinue
Get-ChildItem -Recurse (Join-Path $Repo "engine") | Unblock-File -ErrorAction SilentlyContinue
Ok "Bridge bin\fm27real.dll"

# 4. AI model
if (Get-Command claude -ErrorAction SilentlyContinue) { Ok "Claude Code found (default model Claude Haiku 5.5, the cheapest)" }
else { Warn "Claude Code not found: in the game, open the phone panel > Settings and choose a model / API key (DeepSeek, Anthropic, local Ollama...)." }

# 5. Config + desktop shortcut
@{ python = $py; game = $fm } | ConvertTo-Json | Set-Content -Encoding UTF8 (Join-Path $Here "config.json")
$desk = [Environment]::GetFolderPath("Desktop")
$ws = New-Object -ComObject WScript.Shell
$lnk = $ws.CreateShortcut((Join-Path $desk "FM27Real.lnk"))
$lnk.TargetPath = Join-Path $Here "launch.bat"
$lnk.WorkingDirectory = $Here
$lnk.IconLocation = "$fm,0"
$lnk.Save()
Ok "Desktop shortcut: FM27Real"

Write-Host ""
Write-Host "Done! Start Steam, then double-click FM27Real on your desktop."
Write-Host "The phone panel opens in your browser once the game has loaded a save."
