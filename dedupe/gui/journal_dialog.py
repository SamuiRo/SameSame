from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..actions import FileAction, FileActionService, OperationRecord, OperationStatus


class JournalDialog(QDialog):
    restore_requested = Signal(str)
    journal_purged = Signal()

    def __init__(
        self,
        journal_path: Path,
        quarantine_root: Path,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.journal_path = journal_path
        self.quarantine_root = quarantine_root
        self.setWindowTitle("SameSame operation journal")
        self.resize(1100, 520)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["Time", "Action", "Status", "Source", "Destination", "Message"])
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.table.horizontalHeader().setStretchLastSection(True)

        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(self.refresh)
        self.restore_button = QPushButton("Restore quarantine")
        self.restore_button.clicked.connect(self._restore)
        self.open_quarantine_button = QPushButton("Open quarantine folder")
        self.open_quarantine_button.clicked.connect(self._open_quarantine)
        self.empty_quarantine_button = QPushButton("Empty quarantine…")
        self.empty_quarantine_button.setStyleSheet("color: #c62828; font-weight: 600")
        self.empty_quarantine_button.setToolTip(
            "Permanently delete every restorable file in the quarantine folder. "
            "This is what actually frees the space a quarantine move only reserved."
        )
        self.empty_quarantine_button.clicked.connect(self._empty_quarantine)
        close_button = QPushButton("Close")
        close_button.clicked.connect(self.close)
        controls = QHBoxLayout()
        controls.addWidget(self.refresh_button)
        controls.addWidget(self.restore_button)
        controls.addWidget(self.open_quarantine_button)
        controls.addWidget(self.empty_quarantine_button)
        controls.addStretch(1)
        controls.addWidget(close_button)

        layout = QVBoxLayout(self)
        layout.addWidget(self.table, 1)
        layout.addLayout(controls)
        self.refresh()

    def refresh(self) -> None:
        service = FileActionService(self.journal_path, self.quarantine_root)
        records = service.recent_operations()
        self.table.setRowCount(len(records))
        for row, record in enumerate(records):
            values = [
                datetime.fromtimestamp(record.requested_at).isoformat(sep=" ", timespec="seconds"),
                record.action.value,
                record.status.value,
                str(record.source),
                str(record.destination or ""),
                record.message,
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, record)
                self.table.setItem(row, column, item)
        self.table.resizeColumnsToContents()
        self._selection_changed()

    def _selected_record(self) -> OperationRecord | None:
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, 0)
        record = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        return record if isinstance(record, OperationRecord) else None

    def _selection_changed(self) -> None:
        record = self._selected_record()
        can_restore = bool(
            record
            and record.action == FileAction.QUARANTINE
            and record.status == OperationStatus.COMPLETED
            and record.reversible
            and record.destination
            and record.destination.exists()
            and not record.source.exists()
        )
        self.restore_button.setEnabled(can_restore)
        count, _total = FileActionService(self.journal_path, self.quarantine_root).quarantine_contents()
        self.empty_quarantine_button.setEnabled(count > 0)

    @staticmethod
    def _format_size(size: int) -> str:
        value = float(size)
        for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
            if value < 1024 or unit == "TiB":
                return f"{value:.1f} {unit}"
            value /= 1024
        return f"{value:.1f} TiB"

    def _empty_quarantine(self) -> None:
        service = FileActionService(self.journal_path, self.quarantine_root)
        count, total = service.quarantine_contents()
        if not count:
            QMessageBox.information(self, "Quarantine is empty", "There is nothing left to delete.")
            self.refresh()
            return
        answer = QMessageBox.warning(
            self,
            "Permanently delete everything in quarantine?",
            f"{count} file(s), {self._format_size(total)}, will be ERASED from\n{self.quarantine_root}\n\n"
            "Until now these files were only moved, so on a single drive they still occupy their full "
            "size. Deleting them is what releases that space, and it cannot be undone — every one of them "
            "stops being restorable.\n\nContinue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        purge = service.purge_quarantine()
        self.refresh()
        summary = f"Deleted {purge.removed} file(s) and freed {self._format_size(purge.freed_bytes)}."
        if purge.failures:
            QMessageBox.warning(
                self,
                "Quarantine partly emptied",
                summary + "\n\nThese were left behind:\n" + "\n".join(purge.failures[:20]),
            )
        else:
            QMessageBox.information(self, "Quarantine emptied", summary)
        self.journal_purged.emit()

    def _restore(self) -> None:
        record = self._selected_record()
        if record is not None:
            self.restore_requested.emit(record.operation_id)

    def _open_quarantine(self) -> None:
        self.quarantine_root.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.quarantine_root)))
