# ATLAS launcher (Windows PowerShell). Usage: .\start.ps1 setup|test|status|probe|trade|loop|dashboard|stop|kill "reason"|resume
param([string]$cmd = "help", [Parameter(ValueFromRemainingArguments = $true)][string[]]$rest)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$py = ".venv\Scripts\python.exe"
function Need { if (-not (Test-Path $py)) { Write-Host "run .\start.ps1 setup first"; exit 1 } }
switch ($cmd) {
  "setup" {
    $base = if (Get-Command py -ErrorAction SilentlyContinue) { "py" } else { "python" }
    & $base -m venv .venv
    & $py -m pip install -q --upgrade pip
    & $py -m pip install -q -r requirements.txt
    if (-not (Test-Path .env)) { Copy-Item .env.example .env; Write-Host "created .env from .env.example (fill in your keys)" }
    & $py -m app.main init
  }
  "test"      { Need; & $py -m pytest -m "not live" -q }
  "status"    { Need; & $py -m app.main status }
  "probe"     { Need; & $py -m app.main probe }
  "trade"     { Need; & $py -m app.main trade }
  "loop"      { Need; & $py scripts\run_loop.py }
  "dashboard" { Need; & $py -m app.main dashboard }
  "stop"      { New-Item -ItemType Directory -Force var | Out-Null; New-Item -ItemType File -Force var\STOP | Out-Null; Write-Host "STOP requested" }
  "kill"      { Need; $r = if ($rest) { $rest -join " " } else { "manual kill" }; & $py -m app.main kill $r --flatten }
  "resume"    { Need; & $py -m app.main resume --by operator }
  default     { Write-Host 'usage: .\start.ps1 setup|test|status|probe|trade|loop|dashboard|stop|kill "reason"|resume' }
}
