from __future__ import annotations

import logging
import sys
import traceback

LOGGER = logging.getLogger(__name__)


def _install_excepthook() -> None:
    from PySide6.QtWidgets import QApplication, QMessageBox

    def handle(exc_type: type[BaseException], exc_value: BaseException, exc_tb) -> None:
        LOGGER.error("Unhandled exception", exc_info=(exc_type, exc_value, exc_tb))
        detail = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        instance = QApplication.instance()
        if instance is None:
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        box = QMessageBox(QMessageBox.Icon.Critical, "Unexpected error", str(exc_value) or exc_type.__name__)
        box.setDetailedText(detail)
        box.exec()

    sys.excepthook = handle


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
    # Tooltips carry real guidance (preset help, destructive-action warnings),
    # so give them one readable look on every platform and theme.
    application.setStyleSheet(
        "QToolTip { color: #1a1a1a; background-color: #ffffff; border: 1px solid #a0a0a0; padding: 6px; }"
    )
    _install_excepthook()
    window = MainWindow()
    window.show()
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())
