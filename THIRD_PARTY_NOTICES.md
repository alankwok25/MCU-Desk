# Third-party notices

MCU Desk is derived from [XIVN1987/RTTView](https://github.com/XIVN1987/RTTView).
The original code and icon retain their MIT license and the copyright notice
`Copyright (c) 2018 XIVN1987`; see `LICENSES/RTTView-MIT.txt`.
The combined application is offered under GPLv3, subject to the retained
third-party terms. This does not replace the original component licenses.

## Included components

- `pyocd/`: vendored pyOCD subset, with original Arm and contributor notices
  retained in the files; Apache-2.0, see `LICENSES/Apache-2.0.txt`.
  MCU Desk changes to CMSIS-DAP transport files cover transfer errors,
  USB backend handling and recovery. Modified files are marked in their headers.
  Upstream: https://github.com/pyocd/pyOCD
- `libusb-1.0.24/MinGW64/dll/libusb-1.0.dll`: Windows x64 runtime inherited
  from RTTView; LGPL-2.1-or-later, see `LICENSES/libusb-LGPL-2.1.txt`.
  Source for version 1.0.24: https://github.com/libusb/libusb/tree/v1.0.24
  Release/source archives: https://github.com/libusb/libusb/releases/tag/v1.0.24
  The DLL is separate and replaceable with a compatible x64 build.

## Python dependencies

Exact runtime versions are pinned in `requirements.txt`. License files supplied
by installed distributions are retained under `LICENSES/` where available.

| Component | License |
| --- | --- |
| PyQt5, PyQtChart | GPLv3 / separately available commercial licenses |
| PyQt5-Qt5 | LGPLv3 and applicable Qt component terms |
| PyQtChart-Qt5 | GPLv3 |
| PyQt5_sip | See `LICENSES/PyQt5_sip/LICENSE` |
| hidapi | See the original and alternative licenses under `LICENSES/hidapi/` |
| pyusb | BSD-3-Clause |
| pyelftools | Public domain, with exceptions described in its LICENSE |
| six | MIT |

PyQt licensing: https://www.riverbankcomputing.com/software/pyqt
Qt licensing: https://www.qt.io/licensing/
PyInstaller is a build tool with a bootloader exception; its use does not
replace or remove the licenses of bundled dependencies.

## External tools and release status

SEGGER J-Link and OpenOCD are not included and must be installed separately
when needed. Supporting RTT does not grant redistribution rights to SEGGER's
other software. No SEGGER RTT firmware source is included here.

The application loads J-Link's library through ctypes. Compatibility of that
integration with the applicable GPL and SEGGER terms remains to be reviewed;
having users install the library separately is not by itself a resolution.
This inventory is not a completed legal review or permission to redistribute
proprietary components. Binary releases must also satisfy corresponding-source
and notice requirements for bundled dependencies, including Qt and libusb.
