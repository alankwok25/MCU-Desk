$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
Push-Location $projectRoot
try {
    if (-not (Test-Path '.venv/Scripts/python.exe')) {
        py -3.11 -m venv .venv
        if ($LASTEXITCODE -ne 0) { throw 'Failed to create Python 3.11 environment.' }
    }
    & ./.venv/Scripts/python.exe -m pip install -r packaging/requirements.txt
    if ($LASTEXITCODE -ne 0) { throw 'Failed to install build dependencies.' }
    & ./.venv/Scripts/python.exe -m PyInstaller --noconfirm --clean --onedir --windowed --contents-directory . --distpath dist --workpath build/MCU-Desk --name MCU-Desk --icon Image/serial.ico --runtime-hook packaging/runtime_hook.py --additional-hooks-dir packaging/hooks --add-data 'mcu_desk.ui;.' --add-data 'Image/serial.ico;Image' --add-data 'LICENSE;.' --add-data 'LICENSES;LICENSES' --add-data 'THIRD_PARTY_NOTICES.md;.' --add-data 'README.md;.' --add-data 'examples;examples' --add-binary 'libusb-1.0.24/MinGW64/dll/libusb-1.0.dll;libusb-1.0.24/MinGW64/dll' --hidden-import usb.backend.libusb1 main.py
    if ($LASTEXITCODE -ne 0) { throw 'Packaging failed.' }
    Write-Host 'Ready: dist/MCU-Desk/MCU-Desk.exe'
} finally {
    Pop-Location
}
