$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$log = Join-Path $PSScriptRoot "server.log"
$pidFile = Join-Path $PSScriptRoot "server.pid"
"starting $(Get-Date -Format s)" | Out-File -FilePath $log -Encoding utf8 -Append

if (Test-Path $pidFile) {
  $oldPid = Get-Content $pidFile -ErrorAction SilentlyContinue
  $old = if ($oldPid) { Get-CimInstance Win32_Process -Filter "ProcessId=$oldPid" -ErrorAction SilentlyContinue } else { $null }
  if ($old -and $old.CommandLine -like "*server.py*") {
    "stopping stale server pid $oldPid" | Out-File -FilePath $log -Encoding utf8 -Append
    Stop-Process -Id ([int]$oldPid) -Force -ErrorAction SilentlyContinue
  }
  Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
}

function Ensure-Requirements($PythonCommand, [string[]]$PythonArgs = @()) {
  $missing = $false
  foreach ($line in Get-Content "requirements.txt") {
    $requirement = ($line -replace "\s+#.*$", "").Trim()
    if (-not $requirement) { continue }
    $package = ($requirement -split "[<>=!~; ]")[0]
    & $PythonCommand @PythonArgs -m pip show $package *> $null
    if ($LASTEXITCODE -ne 0) {
      $missing = $true
      break
    }
  }

  if ($missing) {
    Write-Host "Dependencies missing, installing..."
    & $PythonCommand @PythonArgs -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw "Dependency install failed" }
  } else {
    Write-Host "Dependencies already installed, skipping install."
  }
}

$python = Get-Command python -ErrorAction SilentlyContinue
if ($python) {
  "trying python: $($python.Source)" | Out-File -FilePath $log -Encoding utf8 -Append
  Ensure-Requirements $python.Source
  & $python.Source server.py
  if ($LASTEXITCODE -eq 0) { exit }
}

$py = Get-Command py -ErrorAction SilentlyContinue
if ($py) {
  "trying py: $($py.Source)" | Out-File -FilePath $log -Encoding utf8 -Append
  Ensure-Requirements $py.Source @("-3.11")
  & $py.Source -3.11 server.py
  if ($LASTEXITCODE -eq 0) { exit }
}

$bundled = "$env:USERPROFILE\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if (Test-Path $bundled) {
  "trying bundled: $bundled" | Out-File -FilePath $log -Encoding utf8 -Append
  Ensure-Requirements $bundled
  & $bundled server.py
  exit
}

Write-Error "Python 3.11/3.12 not found. Install Python, then run this script again."
