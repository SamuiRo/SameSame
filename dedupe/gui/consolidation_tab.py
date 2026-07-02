from __future__ import annotations

from pathlib import Path
from typing import Callable

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtWidgets import (
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..consolidation import (
    BatchStatus,
    ConsolidationBatch,
    ConsolidationPlan,
    ConsolidationPlanner,
    ConsolidationResult,
    ConsolidationService,
    FolderMapping,
    PlannedMove,
    PlanStatus,
)
from .worker import ConsolidationWorker


class ConsolidationTab(QWidget):
    busy_changed = Signal(bool)
    log_message = Signal(str)
    journal_changed = Signal()

    def __init__(self, journal_path: Callable[[], Path], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._journal_path = journal_path
        self._planner = ConsolidationPlanner()
        self._title_root: Path | None = None
        self._plan: ConsolidationPlan | None = None
        self._thread: QThread | None = None
        self._worker: ConsolidationWorker | None = None
        self._external_actions_enabled = True
        self._preview_dirty = True
        self._build_ui()
        self._refresh_undo_button()
        self._update_controls()

    @property
    def is_busy(self) -> bool:
        return self._thread is not None

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        intro = QLabel(
            "Choose one title folder after duplicate cleanup. SameSame proposes a clear folder-to-folder merge, "
            "then shows every exact file move before anything changes. Existing files are never overwritten."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        setup_group = QGroupBox("1. Title folder and final name")
        setup_layout = QVBoxLayout(setup_group)
        source_row = QHBoxLayout()
        self.title_path = QLineEdit()
        self.title_path.setPlaceholderText("Folder containing folder1, folder2, or other source folders")
        self.browse_button = QPushButton("Choose title folder…")
        self.browse_button.clicked.connect(self._choose_title_folder)
        self.analyze_button = QPushButton("Analyze folders")
        self.analyze_button.clicked.connect(self._analyze)
        source_row.addWidget(QLabel("Title folder"))
        source_row.addWidget(self.title_path, 1)
        source_row.addWidget(self.browse_button)
        source_row.addWidget(self.analyze_button)
        setup_layout.addLayout(source_row)

        name_row = QHBoxLayout()
        self.final_name = QLineEdit()
        self.final_name.setPlaceholderText("Program suggestion or your preferred folder name")
        self.final_name.textChanged.connect(self._mark_preview_dirty)
        self.final_path_label = QLabel("Final folder: —")
        self.final_path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        name_row.addWidget(QLabel("Final folder name"))
        name_row.addWidget(self.final_name, 1)
        name_row.addWidget(self.final_path_label, 2)
        setup_layout.addLayout(name_row)
        layout.addWidget(setup_group)

        mapping_group = QGroupBox("2. Folder mapping — edit the target subfolder when the suggestion is not right")
        mapping_layout = QVBoxLayout(mapping_group)
        self.mapping_table = QTableWidget(0, 4)
        self.mapping_table.setHorizontalHeaderLabels(["Use", "Source folder", "Target subfolder", "Files"])
        self.mapping_table.verticalHeader().setVisible(False)
        self.mapping_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.mapping_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.mapping_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.mapping_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.mapping_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.mapping_table.itemChanged.connect(self._mark_preview_dirty)
        mapping_layout.addWidget(self.mapping_table)
        mapping_buttons = QHBoxLayout()
        self.preview_button = QPushButton("Generate exact move preview")
        self.preview_button.clicked.connect(self._generate_preview)
        mapping_buttons.addStretch(1)
        mapping_buttons.addWidget(self.preview_button)
        mapping_layout.addLayout(mapping_buttons)

        preview_group = QGroupBox("3. Exact file plan — uncheck any file that should stay where it is")
        preview_layout = QVBoxLayout(preview_group)
        self.preview_table = QTableWidget(0, 4)
        self.preview_table.setHorizontalHeaderLabels(["Move", "Source file", "Destination file", "Status"])
        self.preview_table.verticalHeader().setVisible(False)
        self.preview_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.preview_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.preview_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.preview_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.preview_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.preview_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.preview_table.itemChanged.connect(self._update_controls)
        preview_layout.addWidget(self.preview_table)
        self.preview_summary = QLabel("No preview generated")
        self.preview_summary.setWordWrap(True)
        preview_layout.addWidget(self.preview_summary)

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(mapping_group)
        splitter.addWidget(preview_group)
        splitter.setSizes([270, 360])
        layout.addWidget(splitter, 1)

        controls = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setMinimumWidth(260)
        self.status = QLabel("Ready")
        self.execute_button = QPushButton("Consolidate selected files…")
        self.execute_button.clicked.connect(self._confirm_execute)
        self.undo_button = QPushButton("Undo last consolidation…")
        self.undo_button.clicked.connect(self._confirm_undo)
        controls.addWidget(self.status, 1)
        controls.addWidget(self.progress)
        controls.addWidget(self.undo_button)
        controls.addWidget(self.execute_button)
        layout.addLayout(controls)

    def set_external_actions_enabled(self, enabled: bool) -> None:
        self._external_actions_enabled = enabled
        self._update_controls()

    def _choose_title_folder(self) -> None:
        start = self.title_path.text().strip() or str(Path.home())
        selected = QFileDialog.getExistingDirectory(self, "Choose title folder to consolidate", start)
        if selected:
            self.title_path.setText(selected)
            self._analyze()

    def _analyze(self) -> None:
        if self.is_busy:
            return
        try:
            root = Path(self.title_path.text().strip()).expanduser().resolve()
            suggested_name = self._planner.suggest_final_name(root)
            mappings = self._planner.suggested_mappings(root, suggested_name)
        except Exception as exc:  # noqa: BLE001 - show path/discovery errors in the tab.
            QMessageBox.warning(self, "Cannot analyze folder", str(exc))
            return
        self._title_root = root
        self.final_name.blockSignals(True)
        self.final_name.setText(suggested_name)
        self.final_name.blockSignals(False)
        self.mapping_table.blockSignals(True)
        self.mapping_table.setRowCount(len(mappings))
        for row, mapping in enumerate(mappings):
            use_item = QTableWidgetItem()
            use_item.setFlags(use_item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            use_item.setCheckState(Qt.CheckState.Checked)
            use_item.setData(Qt.ItemDataRole.UserRole, mapping.source)
            source_item = QTableWidgetItem(str(mapping.source.relative_to(root) or Path(".")))
            source_item.setToolTip(str(mapping.source))
            source_item.setFlags(source_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            destination_text = str(mapping.relative_destination) if mapping.relative_destination.parts else "."
            destination_item = QTableWidgetItem(destination_text)
            destination_item.setToolTip("Use . to move files directly into the final title folder")
            count_item = QTableWidgetItem(str(mapping.file_count))
            count_item.setFlags(count_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.mapping_table.setItem(row, 0, use_item)
            self.mapping_table.setItem(row, 1, source_item)
            self.mapping_table.setItem(row, 2, destination_item)
            self.mapping_table.setItem(row, 3, count_item)
        self.mapping_table.blockSignals(False)
        self._preview_dirty = True
        self._plan = None
        self.preview_table.setRowCount(0)
        self.preview_summary.setText(
            f"Found {len(mappings)} source folder(s). Review the arrows, then generate preview."
        )
        self._update_final_path_label()
        self._generate_preview()

    def _selected_mappings(self) -> list[FolderMapping]:
        mappings: list[FolderMapping] = []
        for row in range(self.mapping_table.rowCount()):
            use_item = self.mapping_table.item(row, 0)
            destination_item = self.mapping_table.item(row, 2)
            if use_item is None or use_item.checkState() != Qt.CheckState.Checked or destination_item is None:
                continue
            source = use_item.data(Qt.ItemDataRole.UserRole)
            if not isinstance(source, Path):
                continue
            mappings.append(FolderMapping(source, Path(destination_item.text().strip() or "."), 0))
        return mappings

    def _generate_preview(self) -> None:
        if self._title_root is None:
            return
        try:
            plan = self._planner.build_plan(self._title_root, self.final_name.text(), self._selected_mappings())
        except Exception as exc:  # noqa: BLE001 - show validation errors before any move is possible.
            QMessageBox.warning(self, "Invalid consolidation plan", str(exc))
            return
        self._plan = plan
        self._preview_dirty = False
        self.preview_table.blockSignals(True)
        self.preview_table.setRowCount(len(plan.moves))
        for row, move in enumerate(plan.moves):
            use_item = QTableWidgetItem()
            use_item.setData(Qt.ItemDataRole.UserRole, move)
            if move.status == PlanStatus.READY:
                use_item.setFlags(use_item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                use_item.setCheckState(Qt.CheckState.Checked)
            else:
                use_item.setFlags(use_item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
                use_item.setCheckState(Qt.CheckState.Unchecked)
            source_item = QTableWidgetItem(str(move.source))
            destination_item = QTableWidgetItem(str(move.destination))
            status_item = QTableWidgetItem(move.status.value)
            status_item.setToolTip(move.message)
            if move.status == PlanStatus.CONFLICT:
                status_item.setForeground(Qt.GlobalColor.red)
            self.preview_table.setItem(row, 0, use_item)
            self.preview_table.setItem(row, 1, source_item)
            self.preview_table.setItem(row, 2, destination_item)
            self.preview_table.setItem(row, 3, status_item)
        self.preview_table.blockSignals(False)
        ready = len(plan.ready_moves)
        conflicts = len(plan.conflicts)
        in_place = sum(move.status == PlanStatus.ALREADY_IN_PLACE for move in plan.moves)
        self.preview_summary.setText(
            f"Final folder: {plan.final_root} · {ready} ready · {conflicts} conflicts · {in_place} already in place. "
            "Conflicts are never selected and never overwritten."
        )
        self._update_final_path_label()
        self._update_controls()

    def _selected_moves(self) -> list[PlannedMove]:
        moves: list[PlannedMove] = []
        for row in range(self.preview_table.rowCount()):
            item = self.preview_table.item(row, 0)
            if item is None or item.checkState() != Qt.CheckState.Checked:
                continue
            move = item.data(Qt.ItemDataRole.UserRole)
            if isinstance(move, PlannedMove) and move.status == PlanStatus.READY:
                moves.append(move)
        return moves

    def _confirm_execute(self) -> None:
        if self._plan is None or self._preview_dirty:
            QMessageBox.information(self, "Preview required", "Generate a fresh exact preview before consolidating.")
            return
        selected = self._selected_moves()
        if not selected:
            return
        mapping_lines = []
        for mapping in self._plan.mappings[:10]:
            relative_source = mapping.source.relative_to(self._plan.title_root)
            target = mapping.relative_destination if mapping.relative_destination.parts else Path(".")
            mapping_lines.append(f"• {relative_source}  →  {target}")
        if len(self._plan.mappings) > 10:
            mapping_lines.append(f"… and {len(self._plan.mappings) - 10} more")
        answer = QMessageBox.question(
            self,
            "Confirm folder consolidation",
            f"Move and SHA-256 verify {len(selected)} selected file(s)?\n\n"
            f"Final folder:\n{self._plan.final_root}\n\nFolder mapping:\n"
            + "\n".join(mapping_lines)
            + "\n\nExisting files will never be overwritten. Empty source folders are removed only after success.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._start_worker(plan=self._plan.select_moves(selected))

    def _confirm_undo(self) -> None:
        batch = self._latest_undoable_batch()
        if batch is None:
            self._refresh_undo_button()
            return
        answer = QMessageBox.question(
            self,
            "Undo last consolidation",
            f"Restore {batch.moved_count} file(s) to their original paths?\n\n"
            f"Batch: {batch.batch_id}\nFinal folder: {batch.final_root}\n\n"
            "Every moved file will be SHA-256 verified first. Undo stops if an original path is occupied.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._start_worker(undo_batch_id=batch.batch_id)

    def _start_worker(
        self,
        *,
        plan: ConsolidationPlan | None = None,
        undo_batch_id: str | None = None,
    ) -> None:
        if self.is_busy:
            return
        thread = QThread(self)
        worker = ConsolidationWorker(self._journal_path(), plan=plan, undo_batch_id=undo_batch_id)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._worker_progress)
        worker.completed.connect(self._worker_completed)
        worker.failed.connect(self._worker_failed)
        worker.finished.connect(worker.deleteLater)
        worker.finished.connect(thread.quit)
        thread.finished.connect(self._worker_finished)
        thread.finished.connect(thread.deleteLater)
        self._thread = thread
        self._worker = worker
        self.progress.setRange(0, 0)
        self.status.setText("Preparing journaled consolidation…")
        self.busy_changed.emit(True)
        self._update_controls()
        thread.start()

    def _worker_progress(self, current: int, total: int, message: str) -> None:
        self.progress.setRange(0, max(1, total))
        self.progress.setValue(current)
        self.status.setText(message)

    def _worker_completed(self, value: object) -> None:
        if not isinstance(value, ConsolidationResult):
            return
        self.status.setText(value.message)
        self.log_message.emit(f"CONSOLIDATION {value.status.value.upper()}: {value.message} · {value.final_root}")
        self.journal_changed.emit()
        if value.status == BatchStatus.COMPLETED and value.final_root.exists():
            self.title_path.setText(str(value.final_root))
        elif value.status == BatchStatus.UNDONE and value.title_root.exists():
            self.title_path.setText(str(value.title_root))
        if value.status in {BatchStatus.FAILED, BatchStatus.ROLLED_BACK}:
            QMessageBox.warning(self, "Consolidation did not complete", value.message)

    def _worker_failed(self, message: str) -> None:
        self.status.setText("Consolidation failed")
        self.log_message.emit(f"CONSOLIDATION ERROR: {message}")
        QMessageBox.critical(self, "Consolidation failed", message)

    def _worker_finished(self) -> None:
        self._thread = None
        self._worker = None
        self.progress.setRange(0, 100)
        self.progress.setValue(100)
        self._refresh_undo_button()
        self.busy_changed.emit(False)
        self._update_controls()
        path = Path(self.title_path.text().strip()).expanduser()
        if path.is_dir():
            self._analyze()

    def _mark_preview_dirty(self, *_args: object) -> None:
        self._preview_dirty = True
        self._update_final_path_label()
        self._update_controls()

    def _update_final_path_label(self) -> None:
        if self._title_root is None:
            self.final_path_label.setText("Final folder: —")
            return
        name = self.final_name.text().strip() or "?"
        self.final_path_label.setText(f"Final folder: {self._title_root.parent / name}")

    def _refresh_undo_button(self) -> None:
        batch = self._latest_undoable_batch()
        self.undo_button.setToolTip(
            f"Restore batch {batch.batch_id} ({batch.moved_count} files)" if batch else "No completed batch to undo"
        )

    def _update_controls(self, *_args: object) -> None:
        enabled = self._external_actions_enabled and not self.is_busy
        self.title_path.setEnabled(enabled)
        self.browse_button.setEnabled(enabled)
        self.analyze_button.setEnabled(enabled)
        self.final_name.setEnabled(enabled and self._title_root is not None)
        self.mapping_table.setEnabled(enabled and self._title_root is not None)
        self.preview_button.setEnabled(enabled and self._title_root is not None)
        self.preview_table.setEnabled(enabled and self._plan is not None and not self._preview_dirty)
        self.execute_button.setEnabled(
            enabled and self._plan is not None and not self._preview_dirty and bool(self._selected_moves())
        )
        undoable = self._latest_undoable_batch() is not None
        self.undo_button.setEnabled(enabled and undoable)

    def _latest_undoable_batch(self) -> ConsolidationBatch | None:
        journal_path = self._journal_path()
        if not journal_path.exists():
            return None
        try:
            return ConsolidationService(journal_path).latest_undoable_batch()
        except Exception:  # noqa: BLE001 - journal errors are reported if the user requests the action.
            return None
