# Legacy Windows host notes

These notes describe the earlier Windows desktop setup. The current plan is
to run ATLAS on a server; read [server readiness](docs/server-readiness.md)
before choosing or configuring that host. No services have been installed
by the current development work.

Compatibility update: the supervisor now requires an explicit mode. Runtime
data paths can live outside the checkout, backups require an explicit
destination, and watchdog recovery/dashboard startup require opt-in flags.
The examples below are historical setup instructions, not a verified server
deployment recipe.

## Prerequisites (one time)

1. **Install Python 3.12** — https://www.python.org/downloads/ — tick
   "Add python.exe to PATH" during install.
2. **Install Git** — https://git-scm.com/download/win
3. **Install uv** (fast venv/dependency manager) in PowerShell:
   ```powershell
   irm https://astral.sh/uv/install.ps1 | iex
   ```

## Install ATLAS

Open **PowerShell** and run:

```powershell
cd $HOME
git clone https://github.com/Noctilucenty/ATLAS.git ATLAS
cd ATLAS

# the reverse-engineered broker library (gitignored, cloned separately)
git clone https://github.com/iqoptionapi/iqoptionapi vendor/iqoptionapi

# environment + dependencies (Python 3.12)
uv venv --python 3.12 .venv
uv pip install --python .venv\Scripts\python.exe `
    mcp duckdb pandas pandera pyarrow ta scikit-learn lightgbm optuna pytest `
    .\vendor\iqoptionapi

# credentials
Copy-Item .env.example .env
notepad .env      # fill IQ_EMAIL / IQ_PASSWORD ; keep IQ_ALLOW_REAL=0

# sanity check (should say "133 passed")
.venv\Scripts\python.exe -m pytest -q
```

## Bring the data across (optional but faster)

The collector rebuilds `market.duckdb` from scratch (broker serves ~60 days),
so you can skip this. To start with the full history immediately, copy
`market.duckdb`, the `models\` folder, and `logs\live_h2.jsonl` from the Mac
(e.g. via a USB drive or a private file transfer). The frozen model pickles
in `models\` are portable **only if the LightGBM version matches** — the
`uv pip install` above pulls the current release; if a pickle fails to load,
rebuild it with `.venv\Scripts\python.exe live_model_build.py`.

## Run it always-on

**First, verify one live cycle works:**
```powershell
.venv\Scripts\python.exe supervisor.py --paper --collect-min 999
```
Watch `logs\supervisor.log` and `logs\live_h2_heartbeat.jsonl` update, then
Ctrl-C. `--paper` logs signals without placing demo trades; drop it to trade.

**Then register it to launch at logon and restart on failure** (run
PowerShell **as Administrator**, edit the path if you cloned elsewhere):
```powershell
$py  = "$HOME\ATLAS\.venv\Scripts\python.exe"
$dir = "$HOME\ATLAS"
$action  = New-ScheduledTaskAction -Execute $py -Argument "supervisor.py --mode paper" -WorkingDirectory $dir
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 2) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName "ATLAS-supervisor" -Action $action -Trigger $trigger `
    -Settings $settings -Description "ATLAS always-on collector + trader"
Start-ScheduledTask -TaskName "ATLAS-supervisor"
```
The example explicitly chooses paper mode. PRACTICE orders require the
separate `--mode practice` choice after the host and artifacts have been
verified. An invocation without a mode now exits before starting workers.

Check it: **Task Scheduler → Task Scheduler Library → ATLAS-supervisor**, or
`Get-ScheduledTask ATLAS-supervisor`. Stop with
`Stop-ScheduledTask -TaskName "ATLAS-supervisor"`; remove with
`Unregister-ScheduledTask -TaskName "ATLAS-supervisor"`.

## CRITICAL: one machine per account

**Do not run the trader on the Mac and Windows at the same time.** Two
`--trade` runners on the same demo account from two IPs will double-place
orders and can trip broker fraud heuristics. The socket lock only protects
against duplicates on the *same* machine.

Migration order:
1. Set up and verify Windows (above).
2. Confirm Windows is placing/logging trades (`logs\live_h2.jsonl` grows).
3. **Only then** decommission the Mac agents:
   ```bash
   launchctl bootout gui/$(id -u)/com.atlas.h2-paper
   launchctl bootout gui/$(id -u)/com.atlas.iqoption-collector
   ```
   (Leave the Mac's MCP server registered if you still want to query the
   account interactively from Claude Code on the Mac — that's read-only and
   doesn't place trades.)

## Windows notes

- **Sleep**: a desktop set to "never sleep" (Settings → Power) runs 24/7.
  If it does sleep, candles self-heal on wake (broker serves ~60 days) but
  demo-trade minutes during sleep are lost, same as the Mac.
- **The forward test / evaluation** (`forward_eval.py`) and all research
  tooling run identically on Windows — nothing is Mac-only anymore.
- **Claude Code on Windows**: if you install it there, point it at the ATLAS
  folder and it auto-loads `CLAUDE.md`; you can then run the forward
  evaluation and everything else from Windows directly.
