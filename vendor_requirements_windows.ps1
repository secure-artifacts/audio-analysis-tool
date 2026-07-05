$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$vendorDir = Join-Path $PSScriptRoot "vendor"
Remove-Item -Recurse -Force $vendorDir -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $vendorDir | Out-Null

$py = Get-Command py -ErrorAction SilentlyContinue
if ($py) {
  & $py.Source -3 -m pip install -r requirements.txt -t $vendorDir
  if ($LASTEXITCODE -eq 0) { exit }
}

$python = Get-Command python -ErrorAction SilentlyContinue
if ($python) {
  & $python.Source -m pip install -r requirements.txt -t $vendorDir
  if ($LASTEXITCODE -eq 0) { exit }
}

throw "Python not found or pip install failed."
