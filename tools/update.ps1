# Updates Tatanka Charts on this PC: the newest version (git pull), the server rebuilt and restarted (docker compose),
# and the NinjaTrader add-on copied into NinjaTrader, keeping the Host, Port and Token set in your copy.
# Run it with update.cmd (double-click) in the Tatanka Charts folder. Open pages reload by themselves.
$ErrorActionPreference = 'Continue'
Set-Location (Split-Path $PSScriptRoot -Parent)

function Fail($why) { Write-Host ''; Write-Host "Stopped: $why" -ForegroundColor Red; exit 1 }
# A command that isn't there only prints an error and leaves $LASTEXITCODE as it was, so the checks below wouldn't stop
if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Fail 'git was not found. Install Git for Windows (git-scm.com), then run this again.' }
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { Fail 'docker was not found. Is Docker Desktop installed? (A server run without Docker: git pull, then start it again.)' }

Write-Host '1/3  Getting the newest version (git pull)...' -ForegroundColor Cyan
git pull --ff-only --autostash   # your own edits (config/holidays.json, docs/DATA.md) go back on top of the new version
if ($LASTEXITCODE) { Fail 'git pull did not work (see above). A file changed in this folder? "git status" shows which.' }
$clash = @(git diff --name-only --diff-filter=U)
foreach ($f in $clash) {   # yours and the update's on the same lines: the update's is used, yours kept in git's stash
    git cat-file -e "HEAD:$f" 2>$null
    if ($LASTEXITCODE) {   # the update removed a file you changed
        git rm -q -- $f
        Write-Host "     The update removed $f, which you had changed: yours is kept ('git stash show -p')." -ForegroundColor Yellow
    } else {
        git checkout HEAD -- $f
        Write-Host "     Your change to $f clashed with the update's: the update's is in place, yours is kept ('git stash show -p')." -ForegroundColor Yellow
    }
}

Write-Host '2/3  Rebuilding and restarting the server (docker compose)...' -ForegroundColor Cyan
$env:CHART_COMMIT = (git rev-parse HEAD).Trim()   # the build knows its version: Connections > Updates
docker compose up -d --build
if ($LASTEXITCODE) { Fail 'docker compose did not work (see above). Is Docker Desktop running?' }

Write-Host '3/3  The NinjaTrader add-on...' -ForegroundColor Cyan
$addons = Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'NinjaTrader 8\bin\Custom\AddOns'
if (-not (Test-Path $addons)) {
    Write-Host '     NinjaTrader is not on this PC: skipped. (On the NinjaTrader PC: Connections > NinjaTrader > Download the add-on.)'
} else {
    $dst = Join-Path $addons 'TatankaChartFeed.cs'
    $new = [IO.File]::ReadAllText((Resolve-Path 'ninjatrader\TatankaChartFeed.cs').Path)
    $old = if (Test-Path $dst) { [IO.File]::ReadAllText($dst) } else { '' }
    foreach ($name in 'Host', 'Port', 'Token') {   # your own settings in the copy NinjaTrader has stay as they are
        $re = "private const (string|int)\s+$name\s*=\s*(""(?:[^""\\]|\\.)*""|-?\d+)\s*;"   # the whole value, a ';' in it too
        $m = [regex]::Match($old, $re)
        if ($m.Success) { $new = [regex]::Replace($new, $re, $m.Value.Replace('$', '$$')) }
    }
    if ($new -ceq $old) {
        Write-Host '     Already up to date.'
    } else {
        [IO.File]::WriteAllText($dst, $new, (New-Object Text.UTF8Encoding $false))
        Write-Host '     Updated. NinjaTrader compiles it by itself if its NinjaScript Editor is open;'
        Write-Host '     otherwise: in NinjaTrader, New > NinjaScript Editor, press F5.'
    }
}
Write-Host ''
Write-Host 'Done. Open pages and the phone app reload by themselves in a few seconds.' -ForegroundColor Green
