$ErrorActionPreference = 'Stop'

python -m pip install -r "$PSScriptRoot\requirements.txt"
python -m PyInstaller --noconfirm --clean --windowed --onefile `
    --name "PMDG Livery Drop Installer" `
    --collect-all tkinterdnd2 `
    "$PSScriptRoot\pmdg_livery_installer.py"

Write-Host "Built: $PSScriptRoot\dist\PMDG Livery Drop Installer.exe"
