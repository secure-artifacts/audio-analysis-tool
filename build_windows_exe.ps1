$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Find-Python() {
  $py = Get-Command py -ErrorAction SilentlyContinue
  if ($py) { return @($py.Source, "-3") }

  $python = Get-Command python -ErrorAction SilentlyContinue
  if ($python) { return @($python.Source) }

  throw "Python not found. Install Python, tick Add Python to PATH, then run this script again."
}

function Run-Python([string[]]$Args) {
  & $script:PythonCommand @script:PythonArgs @Args
  if ($LASTEXITCODE -ne 0) { throw "Python command failed: $($Args -join ' ')" }
}

function Ensure-Ffmpeg() {
  $localFfmpeg = Get-ChildItem -Path (Join-Path $PSScriptRoot ".tools\ffmpeg") -Filter ffmpeg.exe -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($localFfmpeg) { return $localFfmpeg.FullName }

  $pathFfmpeg = Get-Command ffmpeg -ErrorAction SilentlyContinue
  if ($pathFfmpeg) { return $pathFfmpeg.Source }

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
  return $localFfmpeg.FullName
}

$python = Find-Python
$script:PythonCommand = $python[0]
$script:PythonArgs = if ($python.Count -gt 1) { $python[1..($python.Count - 1)] } else { @() }

Write-Host "Using Python: $script:PythonCommand $($script:PythonArgs -join ' ')"
Run-Python @("--version")
Run-Python @("-m", "pip", "install", "--user", "-r", "requirements.txt")
Run-Python @("-m", "pip", "install", "--user", "pyinstaller")

$ffmpeg = Ensure-Ffmpeg
Run-Python @(
  "-m", "PyInstaller",
  "--noconfirm",
  "--clean",
  "--onedir",
  "--name", "TemoignageTranscriber",
  "--add-data", "index.html;.",
  "server.py"
)

$appDir = Join-Path $PSScriptRoot "dist\TemoignageTranscriber"
Copy-Item -Force $ffmpeg (Join-Path $appDir "ffmpeg.exe")

Write-Host ""
Write-Host "Done. Send this whole folder to another Windows computer:"
Write-Host $appDir
Write-Host "They only need to double-click TemoignageTranscriber.exe."
