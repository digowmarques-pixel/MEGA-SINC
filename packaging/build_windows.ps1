# Gera dist\MegaSinc\MegaSinc.exe, um .zip portátil e (se o Inno Setup estiver
# instalado) o instalador dist\MegaSinc-Setup.exe
# Uso (PowerShell):  powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

py -3 -m venv .venv-build
.\.venv-build\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt pyinstaller pillow

python packaging\fetch_ffmpeg.py
pyinstaller packaging\megasinc.spec --noconfirm --clean

$version = python -c "import megasinc; print(megasinc.__version__)"
Compress-Archive -Path dist\MegaSinc\* -DestinationPath "dist\MegaSinc-Windows-$version.zip" -Force

$iscc = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
if (Test-Path $iscc) {
    & $iscc "/DAppVersion=$version" packaging\installer.iss
} else {
    Write-Host "Inno Setup não encontrado: instalador não gerado (o .zip portátil está em dist\)."
}
Write-Host "Pronto."
