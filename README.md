# MCU Desk: A Desktop Toolkit for Embedded Development

A desktop debugging toolkit for embedded development, featuring RTT logging,
variable monitoring, real-time plotting, and firmware flashing.

**[Download Windows x64 preview](https://github.com/alankwok25/MCU-Desk/releases/download/v0.1.0/MCU-Desk-v0.1.0-windows-x64.zip)**
[Quick start](#quick-start)

Based on [RTTView](https://github.com/XIVN1987/RTTView).

## Download

The `v0.1.0` Windows x64 preview is available on the
[Releases page](https://github.com/alankwok25/MCU-Desk/releases/tag/v0.1.0).
The interface is currently in Chinese. Source changes made after that release,
including the OpenOCD configuration picker and improved halt diagnostics,
are not included in that download.

Download `MCU-Desk-v0.1.0-windows-x64.zip`, extract the entire archive to a writable
folder, and run `MCU-Desk.exe`. Python and Qt are included; probe drivers and
external tools must be installed as listed below.

## Screenshots

![MCU Desk showing variable monitoring, real-time plots, and colored RTT logs](docs/images/overview.png)

*Application preview with simulated data; no hardware is connected. The current interface is in Chinese.*

<details>
<summary>Project settings</summary>

![Project settings for selecting a debug probe, chip model, and optional ELF file](docs/images/project-settings.png)

</details>

## Features

- Compatible wireless DAPLink support; RTT logging and firmware flashing tested by the maintainer.
- RTT log streaming, color display, and recording
- Variable monitoring from ELF files, including structure members
- Real-time plots with zoom controls and CSV export
- Firmware flashing from HEX, ELF, and BIN files
- Saved project settings and window layouts

Projects use the `.mcudesk.json` extension. Existing `.rttview.json` projects
remain supported.

## Prerequisites

| Use case | Requirements |
|---|---|
| J-Link connection and standard flashing | Install the official SEGGER J-Link software and drivers |
| DAPLink connection | Install the appropriate USB driver |
| DAPLink flashing | Install OpenOCD and configure its executable path, interface, and target configuration files |
| RTT logging | The target firmware must include RTT and be running with RTT output enabled |
| Variable monitoring and plotting | Provide an ELF / AXF / OUT file with debug information that matches the firmware on the target |

Device support depends on the debug probe and backend tools.

## Quick Start

1. Connect the debug probe and power the target board.
2. Select the probe and chip under **Project → Project Settings** (`工程 → 工程设置`).
3. For variable monitoring, open **Variable File / Chip Info** (`变量文件 / 芯片信息`), select the ELF file, and add variables.
4. Click **Connect** (`连接`) to view logs, variables, and plots.

An ELF file is not required for RTT logging alone.

To program firmware, click **Flash Firmware** (`烧录固件`), choose the probe,
chip, and HEX / ELF / BIN file in that window, then verify the write addresses.
You do not need to save a project or select a variable ELF first. J-Link is
detected automatically; if unavailable, select `JLink.exe` in the same window.
OpenOCD paths, write ranges, and protected regions are under **Advanced Settings**
(`高级设置`). BIN files require an explicit write address.

In the current source version, selecting OpenOCD searches common installation
locations for `scripts/target/*.cfg` and populates a searchable configuration
list. Existing selections and custom paths are preserved. If discovery fails,
choose the scripts directory manually. Hardware reset and connect-under-reset
require an NRST connection. A failed halt stops flashing before erase/write and
reports the failing stage with OpenOCD diagnostics.

## Run from Source

Use Python 3.11 and run from the repository root:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
```

`main.py` is the application entry point.

## Build a Windows Portable Package

Requires Windows x64 and Python 3.11. Run from the repository root:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File packaging/build.ps1
```

Output: `dist/MCU-Desk/MCU-Desk.exe`. Distribute the entire output directory
and provide corresponding source code and build instructions as required by the licenses.

## Reporting Issues

Include the application version, chip model, probe type, reproduction steps,
and relevant logs. Remove sensitive information before submitting an issue.

## Acknowledgments and License

Thanks to RTTView, pyOCD, PyQt / Qt, SEGGER RTT, and OpenOCD.

The project as a whole is licensed under the GNU General Public License v3.0.
Third-party code and components retain their respective copyrights and licenses.

See [LICENSE](LICENSE), [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md),
and [LICENSES/](LICENSES/).

The J-Link DLL integration still requires a license compatibility review before
release; see the third-party notices above.
