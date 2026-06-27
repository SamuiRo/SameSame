from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    try:
        from PySide6.QtCore import QCoreApplication
        from PySide6.QtWidgets import QApplication
    except ImportError:
        print("SameSame GUI requires PySide6. Install it with: pip install -e .[gui]", file=sys.stderr)
        return 2

    from .main_window import MainWindow

    QCoreApplication.setOrganizationName("SameSame")
    QCoreApplication.setApplicationName("SameSame")
    application = QApplication(argv if argv is not None else sys.argv)
    application.setApplicationDisplayName("SameSame")
    window = MainWindow()
    window.show()
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())
