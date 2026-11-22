# Starts Football Manager 27 with the FM27Real bridge loaded, plus the world engine,
# and opens the phone panel in the browser. Steam must be running.
$ErrorActionPreference = "Stop"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Repo = Split-Path -Parent $Here
$AppId = "4530370"
$cfgFile = Join-Path $Here "config.json"
if (-not (Test-Path $cfgFile)) { Write-Host "Run install.bat first." -ForegroundColor Red; Read-Host; exit 1 }
$cfg = Get-Content $cfgFile -Raw | ConvertFrom-Json
$dll = Join-Path $Here "bin\fm27real.dll"

$docs = [Environment]::GetFolderPath("MyDocuments")
$data = Join-Path $docs "Sports Interactive\Football Manager 27\FM27Real"
New-Item -ItemType Directory -Force -Path $data | Out-Null

if (-not (Get-Process steam -ErrorAction SilentlyContinue)) { Write-Host "Start Steam first." -ForegroundColor Red; Read-Host; exit 1 }

# World engine (unless one is already running)
$engine = $null
try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 1 "http://127.0.0.1:27271/" | Out-Null; $running = $true } catch { $running = $_.Exception.Response -ne $null }
if (-not $running) {
    $env:PYTHONUTF8 = "1"
    $parts = @($cfg.python.Split(" "))
    $pyArgs = @($parts | Select-Object -Skip 1) + @("-m", "engine")
    $engine = Start-Process -FilePath $parts[0] -ArgumentList $pyArgs -WorkingDirectory $Repo -WindowStyle Hidden -PassThru `
        -RedirectStandardError (Join-Path $data "engine.log") -RedirectStandardOutput (Join-Path $data "engine.out.log")
    Write-Host "engine started (log: $data\engine.log)"
}

# The game, started for Steam (SteamAppId), then the bridge loaded into it
$env:SteamAppId = $AppId
$env:SteamGameId = $AppId
$game = Start-Process -FilePath $cfg.game -WorkingDirectory (Split-Path -Parent $cfg.game) -PassThru

Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
using System.Text;
public static class Fm27Inject {
    [DllImport("kernel32", SetLastError = true)] static extern IntPtr OpenProcess(uint access, bool inherit, int pid);
    [DllImport("kernel32", SetLastError = true)] static extern IntPtr VirtualAllocEx(IntPtr p, IntPtr a, UIntPtr size, uint type, uint protect);
    [DllImport("kernel32", SetLastError = true)] static extern bool WriteProcessMemory(IntPtr p, IntPtr a, byte[] b, UIntPtr n, out UIntPtr w);
    [DllImport("kernel32", SetLastError = true)] static extern IntPtr GetModuleHandle(string name);
    [DllImport("kernel32", SetLastError = true, CharSet = CharSet.Ansi)] static extern IntPtr GetProcAddress(IntPtr m, string name);
    [DllImport("kernel32", SetLastError = true)] static extern IntPtr CreateRemoteThread(IntPtr p, IntPtr a, UIntPtr s, IntPtr start, IntPtr arg, uint f, IntPtr id);
    [DllImport("kernel32", SetLastError = true)] static extern uint WaitForSingleObject(IntPtr h, uint ms);
    [DllImport("kernel32", SetLastError = true)] static extern bool CloseHandle(IntPtr h);
    public static string Load(int pid, string dll) {
        IntPtr p = OpenProcess(0x043A, false, pid);  // create thread, query, VM operation/write/read
        if (p == IntPtr.Zero) return "OpenProcess failed: " + Marshal.GetLastWin32Error();
        byte[] path = Encoding.Unicode.GetBytes(dll + "\0");
        IntPtr mem = VirtualAllocEx(p, IntPtr.Zero, (UIntPtr)path.Length, 0x3000, 0x04);
        UIntPtr w;
        if (mem == IntPtr.Zero || !WriteProcessMemory(p, mem, path, (UIntPtr)path.Length, out w)) { CloseHandle(p); return "write failed: " + Marshal.GetLastWin32Error(); }
        IntPtr load = GetProcAddress(GetModuleHandle("kernel32.dll"), "LoadLibraryW");
        IntPtr t = CreateRemoteThread(p, IntPtr.Zero, UIntPtr.Zero, load, mem, 0, IntPtr.Zero);
        if (t == IntPtr.Zero) { CloseHandle(p); return "CreateRemoteThread failed: " + Marshal.GetLastWin32Error(); }
        WaitForSingleObject(t, 15000);
        CloseHandle(t); CloseHandle(p);
        return "ok";
    }
}
"@

# Wait until Unity has started in the game process (the bridge then waits for IL2CPP itself)
$target = $null
for ($i = 0; $i -lt 120 -and -not $target; $i++) {
    Start-Sleep -Milliseconds 500
    $procs = Get-Process | Where-Object { $_.Path -eq $cfg.game }
    foreach ($p in $procs) {
        try { if ($p.Modules | Where-Object { $_.ModuleName -eq "UnityPlayer.dll" }) { $target = $p } } catch {}
    }
}
if (-not $target) { Write-Host "The game did not start (or Steam restarted it). Start Steam first, then try again." -ForegroundColor Red; Read-Host; exit 1 }
$r = [Fm27Inject]::Load($target.Id, $dll)
if ($r -ne "ok") { Write-Host "Could not load the bridge: $r" -ForegroundColor Red; Write-Host "Antivirus software may block it; see README." }
else { Write-Host "bridge loaded into the game (log: $data\bridge.log)" }

# Open the phone panel once the engine is up
$engineFile = Join-Path $data "engine.json"
for ($i = 0; $i -lt 60 -and -not (Test-Path $engineFile); $i++) { Start-Sleep 1 }
if (Test-Path $engineFile) {
    $e = Get-Content $engineFile -Raw | ConvertFrom-Json
    Start-Process "$($e.url)/ui?token=$($e.token)"
}

Write-Host "Playing. Close this window after you quit the game."
$target.WaitForExit()
if ($engine) { Stop-Process -Id $engine.Id -ErrorAction SilentlyContinue }
