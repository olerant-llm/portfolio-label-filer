$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$pythonVersion = '3.14.3'
$pythonDir = Join-Path $root 'python_portable'
$pythonExe = Join-Path $pythonDir 'python.exe'
$pythonZip = Join-Path $root 'python_portable.zip'
$pythonUrl = "https://www.python.org/ftp/python/$pythonVersion/python-$pythonVersion-embed-amd64.zip"
$setupMarker = Join-Path $pythonDir '.setup_complete'

if ((Test-Path -LiteralPath $pythonExe) -and (Test-Path -LiteralPath $setupMarker)) {
    Write-Host 'Portable Python is already installed.'
    exit 0
}

if (-not (Test-Path -LiteralPath $pythonExe)) {
    Write-Host "Downloading portable Python $pythonVersion..."
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -Uri $pythonUrl -OutFile $pythonZip

    if (-not (Test-Path -LiteralPath $pythonZip)) {
        throw 'Portable Python download failed.'
    }

    Write-Host 'Extracting portable Python...'
    Expand-Archive -LiteralPath $pythonZip -DestinationPath $pythonDir -Force
    Remove-Item -LiteralPath $pythonZip
} else {
    Write-Host 'Resuming portable Python setup...'
}

$pth = Join-Path $pythonDir 'python314._pth'
$lines = Get-Content -LiteralPath $pth
$lines = $lines -replace '^#import site$', 'import site'
Set-Content -LiteralPath $pth -Value $lines -Encoding ascii

Write-Host 'Installing pip...'
$getPip = Join-Path $pythonDir 'get-pip.py'
Invoke-WebRequest -Uri 'https://bootstrap.pypa.io/get-pip.py' -OutFile $getPip
& $pythonExe $getPip --no-warn-script-location
if ($LASTEXITCODE -ne 0) {
    throw 'pip setup failed.'
}
Remove-Item -LiteralPath $getPip

Write-Host 'Installing Label Filer dependencies...'
& $pythonExe -m pip install --upgrade pip --no-warn-script-location
if ($LASTEXITCODE -ne 0) {
    throw 'pip upgrade failed.'
}
& $pythonExe -m pip install -r (Join-Path $root 'requirements.txt') --no-warn-script-location
if ($LASTEXITCODE -ne 0) {
    throw 'Dependency installation failed.'
}

Set-Content -LiteralPath $setupMarker -Value $pythonVersion -Encoding ascii
Write-Host 'Portable setup complete.'
