"""MCU Desk application entry point."""
import multiprocessing


def main():
    import os
    import sys
    from pathlib import Path

    project_file = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else None
    app_dir = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent
    os.chdir(app_dir)

    from PyQt5.QtWidgets import QApplication
    from workbench import Workbench

    app = QApplication(sys.argv)
    app.setApplicationName('MCU Desk')
    window = Workbench(project_file=project_file)
    window.show()
    return app.exec()


if __name__ == '__main__':
    # Dispatch packaged worker processes before importing Qt.
    multiprocessing.freeze_support()
    raise SystemExit(main())
