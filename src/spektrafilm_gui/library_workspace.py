"""Connect the local catalog to the existing editor without modifying originals."""
from __future__ import annotations

from copy import deepcopy
from importlib.metadata import version
from pathlib import Path
import os
import sqlite3

from qtpy import QtCore, QtWidgets

from spektrafilm_gui.catalog import Catalog, RAW_EXTENSIONS
from spektrafilm_gui.library_panel import LibraryPanel
from spektrafilm_gui.persistence import gui_state_from_dict, gui_state_to_dict
from spektrafilm_gui.state_bridge import apply_gui_state, collect_gui_state


def default_catalog_path() -> Path:
    override = os.environ.get('SPEKTRAFILM_LIBRARY')
    if override:
        return Path(override).expanduser()
    root = QtCore.QStandardPaths.writableLocation(QtCore.QStandardPaths.GenericDataLocation)
    return Path(root or Path.home() / '.local/share') / 'spektrafilm/library.sqlite3'


class LibraryWorkspace(QtCore.QObject):
    def __init__(self, *, window, controller, widgets, catalog: Catalog):
        super().__init__(window)
        self.window, self.controller, self.widgets, self.catalog = window, controller, widgets, catalog
        self.active_photo_id: int | None = None
        self._last_saved: dict | None = None
        self._save_error_shown = False
        self._switching = False
        self._loaded_signature = None
        self.engine_version = version('spektrafilm')
        self.defaults = gui_state_to_dict(collect_gui_state(widgets=widgets))
        self.editor = window.takeCentralWidget()
        self.tabs = QtWidgets.QTabWidget(window)
        self.tabs.setObjectName('libraryWorkspace')
        self.panel = LibraryPanel(catalog, self.tabs)
        self.tabs.addTab(self.panel, '图库')
        self.tabs.addTab(self.editor, '调色')
        window.setCentralWidget(self.tabs)
        window.setWindowTitle('spektrafilm · 图库与胶片调色')
        self.panel.photoActivated.connect(self.open_photo)
        self.panel.photoRemoving.connect(self._forget_photo)
        self.panel.before_remove = lambda photo_id: self.save_active() if photo_id == self.active_photo_id else True
        self.tabs.currentChanged.connect(self._tab_changed)
        # Route the original file pickers through the catalog too, so edits cannot
        # accidentally be saved to the previously selected photograph.
        widgets.filepicker.load_requested.disconnect(controller.load_input_image)
        widgets.load_raw.load_requested.disconnect(controller.load_raw_image)
        widgets.filepicker.load_requested.connect(self.open_external)
        widgets.load_raw.load_requested.connect(self.open_external)
        self._autosave = QtCore.QTimer(self)
        self._autosave.setInterval(2000)
        self._autosave.timeout.connect(self.save_active)
        self._autosave.start()
        window.installEventFilter(self)

    def _snapshot(self) -> dict:
        state = gui_state_to_dict(collect_gui_state(widgets=self.widgets))
        # Display appearance is global; photographic processing belongs to the photo.
        state.pop('display', None)
        state['library_rotation'] = self.controller.input_rotation_quarters
        return state

    def save_active(self) -> bool:
        if self.active_photo_id is None or self._switching:
            return True
        try:
            if self.catalog.get_photo(self.active_photo_id) is None:
                self.active_photo_id = None
                return True
            state = self._snapshot()
            if state != self._last_saved:
                self.catalog.save_edit(self.active_photo_id, state, self.engine_version)
                self._last_saved = deepcopy(state)
            self._save_error_shown = False
            return True
        except (OSError, sqlite3.Error, ValueError) as exc:
            if not self._save_error_shown:
                self._save_error_shown = True
                QtWidgets.QMessageBox.critical(self.window, '调色参数未保存', f'请解决存储问题后再切换照片或关闭窗口。\n\n{exc}')
            return False

    def open_external(self, path: str) -> None:
        try:
            self.catalog.import_files([path])
            photo = self.catalog.get_by_path(path)
            self.panel.refresh()
            if photo is not None:
                self.open_photo(photo.id, reload_current=True)
        except (OSError, sqlite3.Error, ValueError) as exc:
            QtWidgets.QMessageBox.critical(self.window, '导入失败', str(exc))

    def open_photo(self, photo_id: int, *, reload_current: bool = False) -> None:
        if self.controller._active_simulation_worker is not None:
            QtWidgets.QMessageBox.information(self.window, '正在处理', '请等待当前预览或扫描完成后再切换照片。')
            return
        if not self.save_active():
            return
        try:
            photo = self.catalog.get_photo(photo_id)
            if photo is None:
                return
            if not Path(photo.path).is_file():
                QtWidgets.QMessageBox.warning(self.window, '原文件不可用', '请连接外置硬盘，或在图库中使用“重新定位”。评分和调色参数仍保留。')
                return
            stat = Path(photo.path).stat()
            signature = (photo.path, stat.st_size, stat.st_mtime_ns)
            if photo_id == self.active_photo_id and not reload_current and signature == self._loaded_signature:
                self.tabs.setCurrentIndex(1)
                return
            saved = self.catalog.get_edit(photo_id)
            target = deepcopy(saved if saved is not None else self.defaults)
            if saved is None and Path(photo.path).suffix.lower() in {'.jpg', '.jpeg', '.png'}:
                # shortcut: assume sRGB for these rendered formats; use the input
                # color-space control for a differently encoded source.
                target['input_image']['input_color_space'] = 'sRGB'
                target['input_image']['input_cctf_decoding'] = True
            before = gui_state_to_dict(collect_gui_state(widgets=self.widgets))
            target['display'] = before['display']
            rotation = int(target.pop('library_rotation', 0)) % 4
            next_state = gui_state_from_dict(target)
        except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
            QtWidgets.QMessageBox.critical(self.window, '无法打开照片', str(exc))
            return

        self._switching = True
        self.controller._preview_suspended = True
        previous_input = self.controller._current_input_image
        previous_path = self.controller._current_input_path
        previous_rotation = self.controller.input_rotation_quarters
        previous_digest = self.controller._next_runtime_digest_applies_stock_specifics
        try:
            apply_gui_state(next_state, widgets=self.widgets)
            # Saved recipes already contain film-specific values. Do not reset
            # their fine adjustments when the simulator is first created.
            self.controller._next_runtime_digest_applies_stock_specifics = False
            loader = self.controller.load_raw_image if Path(photo.path).suffix.lower() in RAW_EXTENSIONS else self.controller.load_input_image
            if loader(photo.path) is False:
                raise ValueError('无法解码此照片，原来的调色参数已保留。')
            if rotation:
                self.controller._rotate_input_image(quarter_turns=rotation)
            old_output = self.controller._output_layer()
            if old_output is not None:
                self.controller._viewer.layers.remove(old_output)
            is_raw = Path(photo.path).suffix.lower() in RAW_EXTENSIONS
            self.widgets.load_raw.set_path(photo.path if is_raw else '')
            self.widgets.filepicker.set_path('' if is_raw else photo.path)
            self.active_photo_id = photo_id
            self._loaded_signature = signature
            self._last_saved = deepcopy(saved)
            self.tabs.setTabText(1, f'调色 · {photo.name}')
            self.tabs.setCurrentIndex(1)
        except Exception as exc:
            apply_gui_state(gui_state_from_dict(before), widgets=self.widgets)
            self.controller._next_runtime_digest_applies_stock_specifics = previous_digest
            self.controller._current_input_path = previous_path
            if previous_input is not None:
                self.controller._set_or_add_input_stack(previous_input)
            else:
                self.controller._current_input_image = None
                self.controller._current_preview_image = None
            self.controller.input_rotation_quarters = previous_rotation
            QtWidgets.QMessageBox.critical(self.window, '打开失败', str(exc))
        finally:
            self._switching = False
            self.controller._preview_suspended = False
            self.controller._request_auto_preview_if_enabled()
        self.save_active()

    def _forget_photo(self, photo_id: int) -> None:
        if photo_id == self.active_photo_id:
            self.active_photo_id = None
            self._last_saved = None
            self.tabs.setTabText(1, '调色 · 已从图库移除')

    def _tab_changed(self, index: int) -> None:
        if self._switching:
            return
        if not self.save_active():
            self.tabs.setCurrentIndex(1)
        elif index == 0:
            self.panel.refresh()

    def eventFilter(self, watched, event):  # noqa: N802 - Qt API
        if watched is self.window and event.type() == QtCore.QEvent.Close:
            if not self.save_active():
                event.ignore()
                return True
            if self.controller._active_simulation_worker is not None:
                QtWidgets.QMessageBox.information(self.window, '正在处理', '请等待当前计算完成后关闭窗口。')
                event.ignore()
                return True
            self._autosave.stop()
            self.panel.shutdown()
        return super().eventFilter(watched, event)


def install_library_workspace(window, controller, widgets) -> LibraryWorkspace:
    workspace = LibraryWorkspace(window=window, controller=controller, widgets=widgets, catalog=Catalog(default_catalog_path()))
    window.library_workspace = workspace
    return workspace
