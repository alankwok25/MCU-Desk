"""Bundle the repository's x64 libusb, not a DLL from another installed tool."""
from pathlib import Path

root = Path(__file__).resolve().parents[2]
hiddenimports = ['glob', 'usb.backend.libusb1']
binaries = [(str(root / 'libusb-1.0.24/MinGW64/dll/libusb-1.0.dll'), '.')]
