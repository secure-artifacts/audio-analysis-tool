$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$log = Join-Path $PSScriptRoot "server.log"
"starting $(Get-Date -Format s)" | Out-File -FilePath $log -Encoding utf8

$python = Get-Command python -ErrorAction SilentlyContinue
if ($python) {
  "trying python: $($python.Source)" | Out-File -FilePath $log -Encoding utf8 -Append
  & $python.Source server.py
  if ($LASTEXITCODE -eq 0) { exit }
}

$py = Get-Command py -ErrorAction SilentlyContinue
if ($py) {
  "trying py: $($py.Source)" | Out-File -FilePath $log -Encoding utf8 -Append
  & $py.Source -3.11 server.py
  if ($LASTEXITCODE -eq 0) { exit }
}

$bundled = "$env:USERPROFILE\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if (Test-Path $bundled) {
  "trying bundled: $bundled" | Out-File -FilePath $log -Encoding utf8 -Append
  & $bundled server.py
  exit
}

Write-Error "Python 3.11/3.12 not found. Install Python, then run this script again."
