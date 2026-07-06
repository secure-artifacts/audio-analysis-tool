$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$log = Join-Path $PSScriptRoot "server.log"
$pidFile = Join-Path $PSScriptRoot "server.pid"
$vendorDir = Join-Path $PSScriptRoot "vendor"
"starting $(Get-Date -Format s)" | Out-File -FilePath $log -Encoding utf8 -Append

if (Test-Path $vendorDir) {
  $env:PYTHONPATH = $vendorDir + ";" + $env:PYTHONPATH
  Write-Host "Using bundled Python packages from vendor."
}

if (Test-Path $pidFile) {
  $oldPid = Get-Content $pidFile -ErrorAction SilentlyContinue
  $old = if ($oldPid) { Get-CimInstance Win32_Process -Filter "ProcessId=$oldPid" -ErrorAction SilentlyContinue } else { $null }
  if ($old -and $old.CommandLine -like "*server.py*") {
    "stopping stale server pid $oldPid" | Out-File -FilePath $log -Encoding utf8 -Append
    Stop-Process -Id ([int]$oldPid) -Force -ErrorAction SilentlyContinue
  }
  Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
}

function Ensure-Ffmpeg() {
  if (Get-Command ffmpeg -ErrorAction SilentlyContinue) {
    Write-Host "ffmpeg already installed."
    return
  }

  $localFfmpeg = Get-ChildItem -Path (Join-Path $PSScriptRoot ".tools\ffmpeg") -Filter ffmpeg.exe -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($localFfmpeg) {
    $env:Path = $localFfmpeg.DirectoryName + ";" + $env:Path
    Write-Host "Using downloaded ffmpeg."
    return
  }

  $winget = Get-Command winget -ErrorAction SilentlyContinue
  if ($winget) {
    Write-Host "ffmpeg missing, installing with winget..."
    & $winget.Source install --id Gyan.FFmpeg -e --accept-package-agreements --accept-source-agreements
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
    if (Get-Command ffmpeg -ErrorAction SilentlyContinue) {
      Write-Host "ffmpeg installed."
      return
    }
  }

  Write-Host "ffmpeg missing, downloading portable build..."
  [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
  $toolsDir = Join-Path $PSScriptRoot ".tools"
  $zipPath = Join-Path $toolsDir "ffmpeg.zip"
  $extractDir = Join-Path $toolsDir "ffmpeg"
  New-Item -ItemType Directory -Force -Path $toolsDir | Out-Null
  Invoke-WebRequest "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip" -OutFile $zipPath
  Expand-Archive -Force -Path $zipPath -DestinationPath $extractDir
  $localFfmpeg = Get-ChildItem -Path $extractDir -Filter ffmpeg.exe -Recurse | Select-Object -First 1
  if (-not $localFfmpeg) { throw "ffmpeg download completed, but ffmpeg.exe was not found." }
  $env:Path = $localFfmpeg.DirectoryName + ";" + $env:Path
}

function Ensure-Requirements($PythonCommand, [string[]]$PythonArgs = @()) {
  Write-Host "Using Python: $PythonCommand $($PythonArgs -join ' ')"
  & $PythonCommand @PythonArgs --version

  if (Test-Path (Join-Path $vendorDir "groq")) {
    Write-Host "Bundled dependencies found, skipping pip install."
    return
  }

  & $PythonCommand @PythonArgs -m pip --version *> $null
  if ($LASTEXITCODE -ne 0) {
    Write-Host "pip missing, enabling pip..."
    & $PythonCommand @PythonArgs -m ensurepip --upgrade
    if ($LASTEXITCODE -ne 0) { throw "pip is not available for this Python install." }
  }

  $missing = $false
  foreach ($line in Get-Content "requirements.txt") {
    $requirement = ($line -replace "\s+#.*$", "").Trim()
    if (-not $requirement) { continue }
    $package = ($requirement -split "[<>=!~; ]")[0]
    & $PythonCommand @PythonArgs -c "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('$package') else 1)"
    if ($LASTEXITCODE -ne 0) {
      $missing = $true
      break
    }
  }

  if ($missing) {
    Write-Host "Dependencies missing, installing..."
    & $PythonCommand @PythonArgs -m pip install --user -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw "Dependency install failed. Check the pip error above: usually internet/proxy, permissions, or a broken Python install." }
  } else {
    Write-Host "Dependencies already installed, skipping install."
  }
}

Ensure-Ffmpeg

$py = Get-Command py -ErrorAction SilentlyContinue
if ($py) {
  "trying py -3: $($py.Source)" | Out-File -FilePath $log -Encoding utf8 -Append
  Ensure-Requirements $py.Source @("-3")
  & $py.Source -3 server.py
  if ($LASTEXITCODE -eq 0) { exit }
}

$python = Get-Command python -ErrorAction SilentlyContinue
if ($python) {
  "trying python: $($python.Source)" | Out-File -FilePath $log -Encoding utf8 -Append
  Ensure-Requirements $python.Source
  & $python.Source server.py
  if ($LASTEXITCODE -eq 0) { exit }
}

$python3 = Get-Command python3 -ErrorAction SilentlyContinue
if ($python3) {
  "trying python3: $($python3.Source)" | Out-File -FilePath $log -Encoding utf8 -Append
  Ensure-Requirements $python3.Source
  & $python3.Source server.py
  if ($LASTEXITCODE -eq 0) { exit }
}

$bundled = "$env:USERPROFILE\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if (Test-Path $bundled) {
  "trying bundled: $bundled" | Out-File -FilePath $log -Encoding utf8 -Append
  Ensure-Requirements $bundled
  & $bundled server.py
  exit
}

Write-Error "Python not found. Install Python from https://www.python.org/downloads/windows/ and tick Add Python to PATH, then run start_windows.bat again."
