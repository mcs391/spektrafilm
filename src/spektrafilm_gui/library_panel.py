"""Local photo catalog UI; imported photos remain at their original locations."""
from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import threading
from typing import Callable

from qtpy import QtCore, QtGui, QtWidgets

from spektrafilm_gui.catalog import Catalog, Photo, SUPPORTED_EXTENSIONS
from spektrafilm_gui.library_thumbnails import make_thumbnail

COLORS = [('无颜色', ''), ('红色', 'red'), ('黄色', 'yellow'), ('绿色', 'green'),
          ('蓝色', 'blue'), ('紫色', 'purple')]
FLAGS = [('未标记', 0), ('已挑选', 1), ('已拒绝', -1)]
PAGE_SIZE = 200
DB_ERRORS = (OSError, sqlite3.Error, ValueError)


class _TaskSignals(QtCore.QObject):
    result = QtCore.Signal(object)
    progress = QtCore.Signal(str)
    finished = QtCore.Signal(object)


class _Task(QtCore.QRunnable):
    def __init__(self, key: object, fn: Callable, *, thumbnail: bool = False):
        super().__init__()
        self.key, self.fn, self.thumbnail = key, fn, thumbnail
        self.signals = _TaskSignals()

    def run(self):
        try:
            result = self.fn(self.signals.progress.emit)
            self.signals.result.emit((self.key, result, ''))
        except Exception as exc:
            self.signals.result.emit((self.key, None, str(exc)))
        finally:
            self.signals.finished.emit(self.key)


def import_photos(database_path: Path, roots: list[str], cancelled: threading.Event,
                  progress: Callable[[str], None]) -> dict:
    catalog = Catalog(database_path)
    added = existing = failed = 0
    errors: list[str] = []
    seen: set[str] = set()

    def failure(message):
        nonlocal failed
        failed += 1
        if len(errors) < 20:
            errors.append(message)

    def candidates():
        for root in roots:
            p = Path(root)
            if p.is_dir():
                for directory, folders, files in os.walk(p, followlinks=False,
                                                         onerror=lambda e: failure(str(e))):
                    if cancelled.is_set():
                        return
                    folders[:] = sorted(n for n in folders if not n.startswith('.')
                                        and not (Path(directory) / n).is_symlink())
                    for name in sorted(files):
                        yield Path(directory) / name
            else:
                yield p

    for candidate in candidates():
        if cancelled.is_set():
            break
        if candidate.suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue
        try:
            canonical = str(candidate.resolve(strict=True))
            if canonical in seen:
                continue
            seen.add(canonical)
            new, old = catalog.import_files([canonical])
            added += new
            existing += old
        except DB_ERRORS as exc:
            failure(f'{candidate.name}：{exc}')
        if (added + existing + failed) % 25 == 0:
            progress(f'正在建立索引… 新增 {added} · 已有 {existing} · 失败 {failed}')
    return dict(added=added, existing=existing, failed=failed, errors=errors,
                cancelled=cancelled.is_set())


class LibraryPanel(QtWidgets.QWidget):
    photoActivated = QtCore.Signal(int)
    photoRemoving = QtCore.Signal(int)

    def __init__(self, catalog: Catalog, parent=None):
        super().__init__(parent)
        self.catalog = catalog
        self.before_remove: Callable[[int], bool] | None = None
        self._thumb_cancelled = threading.Event()
        self._offset = 0
        self._generation = 0
        self._selected_id: int | None = None
        self._items: dict[int, QtWidgets.QListWidgetItem] = {}
        self._tasks: dict[object, _Task] = {}
        self._cancelled = threading.Event()
        self._closing = False
        self._importing = False
        # The application owns the pool, so removing a panel cannot destroy live workers.
        self._pool = QtCore.QThreadPool(QtWidgets.QApplication.instance())
        self._pool.setMaxThreadCount(2)
        cache = QtCore.QStandardPaths.writableLocation(QtCore.QStandardPaths.CacheLocation)
        self._cache_dir = Path(cache or Path.home() / '.cache' / 'spektrafilm') / 'library-thumbnails'
        self.destroyed.connect(lambda *_args, event=self._cancelled: event.set())
        self.setObjectName('photoLibrary')
        self.setStyleSheet('''
            QWidget#photoLibrary { background: #101010; color: #cecece; }
            QWidget#photoLibrary QWidget { color: #cecece; }
            QWidget#photoLibrary QWidget:disabled { color: #747474; }
            QLabel#libraryTitle { font-size: 24px; font-weight: 600; color: #f2f2f2; }
            QLabel#libraryHint { color: #8d8d8d; }
            QListWidget { background: #151515; border: 1px solid #2c2c2c; border-radius: 5px; }
            QListWidget::item { padding: 7px; border-radius: 4px; }
            QListWidget::item:selected { background: #3e3029; color: #f2f2f2; }
            QLineEdit, QComboBox { min-height: 27px; padding: 3px 7px; background: #202020; }
            QPushButton { min-height: 30px; padding: 3px 12px; background: #262626; border: 1px solid #404040; border-radius: 4px; }
            QPushButton:hover { background: #333333; }
            QPushButton:pressed { background: #1a1a1a; }
            QWidget#photoLibrary QPushButton#openPhoto { background: #ee9470; color: #17110e; font-weight: 600; }
            QLabel#photoName { font-weight: 600; font-size: 14px; }
        ''')
        palette = self.palette()
        palette.setColor(QtGui.QPalette.PlaceholderText, QtGui.QColor('#8d8d8d'))
        self.setPalette(palette)
        self._build_ui()
        self.refresh()

    def _build_ui(self):
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 14)
        layout.setSpacing(12)
        header = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel('照片图库')
        title.setObjectName('libraryTitle')
        header.addWidget(title)
        header.addStretch()
        self._import_files_button = QtWidgets.QPushButton('导入照片')
        self._import_folder_button = QtWidgets.QPushButton('导入文件夹')
        self._cancel_button = QtWidgets.QPushButton('停止导入')
        self._cancel_button.hide()
        header.addWidget(self._import_files_button)
        header.addWidget(self._import_folder_button)
        header.addWidget(self._cancel_button)
        layout.addLayout(header)
        hint = QtWidgets.QLabel('照片保留在原位置 · 双击进入调色 · 从图库移除不会删除原文件')
        hint.setObjectName('libraryHint')
        layout.addWidget(hint)
        filters = QtWidgets.QHBoxLayout()
        self._search = QtWidgets.QLineEdit()
        self._search.setPlaceholderText('搜索文件名、路径或关键词…')
        self._search.setAccessibleName('搜索照片')
        filters.addWidget(self._search, 1)
        self._rating_filter = QtWidgets.QComboBox()
        for n in range(6):
            self._rating_filter.addItem('全部评分' if n == 0 else f'{n} 星及以上', n)
        self._color_filter = QtWidgets.QComboBox()
        self._color_filter.addItem('全部颜色', '')
        for name, value in COLORS[1:]:
            self._color_filter.addItem(name, value)
        self._flag_filter = QtWidgets.QComboBox()
        self._flag_filter.addItem('全部标记', None)
        for name, value in FLAGS:
            self._flag_filter.addItem(name, value)
        for widget in (self._rating_filter, self._color_filter, self._flag_filter):
            filters.addWidget(widget)
            widget.currentIndexChanged.connect(self._filter_changed)
        layout.addLayout(filters)
        splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        self._folder_list = QtWidgets.QListWidget()
        self._folder_list.setMinimumWidth(140)
        self._folder_list.setMaximumWidth(300)
        self._folder_list.setAccessibleName('图库文件夹')
        splitter.addWidget(self._folder_list)
        grid_container = QtWidgets.QWidget()
        grid_layout = QtWidgets.QVBoxLayout(grid_container)
        grid_layout.setContentsMargins(0, 0, 0, 0)
        self._empty = QtWidgets.QLabel('导入一个照片文件夹，开始整理你的影像。')
        self._empty.setAlignment(QtCore.Qt.AlignCenter)
        self._empty.setWordWrap(True)
        grid_layout.addWidget(self._empty)
        self._grid = QtWidgets.QListWidget()
        self._grid.setViewMode(QtWidgets.QListView.IconMode)
        self._grid.setResizeMode(QtWidgets.QListView.Adjust)
        self._grid.setMovement(QtWidgets.QListView.Static)
        self._grid.setIconSize(QtCore.QSize(160, 108))
        self._grid.setGridSize(QtCore.QSize(182, 168))
        self._grid.setWordWrap(True)
        self._grid.setSpacing(4)
        self._grid.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self._grid.setAccessibleName('照片缩略图')
        grid_layout.addWidget(self._grid, 1)
        pages = QtWidgets.QHBoxLayout()
        self._previous = QtWidgets.QPushButton('上一页')
        self._next = QtWidgets.QPushButton('下一页')
        self._count = QtWidgets.QLabel()
        pages.addWidget(self._previous)
        pages.addWidget(self._count, 1, QtCore.Qt.AlignCenter)
        pages.addWidget(self._next)
        grid_layout.addLayout(pages)
        splitter.addWidget(grid_container)
        self._details = QtWidgets.QWidget()
        self._details.setMinimumWidth(225)
        self._details.setMaximumWidth(280)
        details = QtWidgets.QVBoxLayout(self._details)
        self._name = QtWidgets.QLabel('选择一张照片')
        self._name.setObjectName('photoName')
        self._name.setWordWrap(True)
        self._path = QtWidgets.QLabel()
        self._path.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        self._path.setWordWrap(True)
        self._file_status = QtWidgets.QLabel()
        self._file_status.setWordWrap(True)
        details.addWidget(self._name)
        details.addWidget(self._path)
        details.addWidget(self._file_status)
        form = QtWidgets.QFormLayout()
        self._rating = QtWidgets.QComboBox()
        for n in range(6):
            self._rating.addItem('未评分' if n == 0 else '★' * n, n)
        self._color = QtWidgets.QComboBox()
        for name, value in COLORS:
            self._color.addItem(name, value)
        self._flag = QtWidgets.QComboBox()
        for name, value in FLAGS:
            self._flag.addItem(name, value)
        self._tags = QtWidgets.QLineEdit()
        self._tags.setPlaceholderText('旅行, 人像, 上海')
        self._tags.setToolTip('用中文或英文逗号分隔；按回车保存')
        form.addRow('星级', self._rating)
        form.addRow('颜色', self._color)
        form.addRow('挑选', self._flag)
        form.addRow('关键词', self._tags)
        details.addLayout(form)
        save_tags = QtWidgets.QPushButton('保存关键词')
        self._open_button = QtWidgets.QPushButton('打开调色')
        self._open_button.setObjectName('openPhoto')
        self._relocate_button = QtWidgets.QPushButton('重新定位原文件…')
        self._remove_button = QtWidgets.QPushButton('从图库移除…')
        for button in (save_tags, self._open_button, self._relocate_button, self._remove_button):
            details.addWidget(button)
        details.addStretch()
        splitter.addWidget(self._details)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([175, 690, 245])
        layout.addWidget(splitter, 1)
        self._status = QtWidgets.QLabel('就绪')
        self._status.setWordWrap(True)
        layout.addWidget(self._status)
        self._search_timer = QtCore.QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(250)
        self._search_timer.timeout.connect(self._filter_changed)
        self._search.textChanged.connect(lambda: self._search_timer.start())
        self._folder_list.currentItemChanged.connect(self._filter_changed)
        self._grid.currentItemChanged.connect(self._selection_changed)
        self._grid.itemDoubleClicked.connect(self._open_selected)
        self._open_button.clicked.connect(self._open_selected)
        self._previous.clicked.connect(lambda: self._change_page(-1))
        self._next.clicked.connect(lambda: self._change_page(1))
        self._import_files_button.clicked.connect(self._choose_files)
        self._import_folder_button.clicked.connect(self._choose_folder)
        self._cancel_button.clicked.connect(self._cancelled.set)
        self._rating.currentIndexChanged.connect(lambda: self._save_metadata(rating=self._rating.currentData()))
        self._color.currentIndexChanged.connect(lambda: self._save_metadata(color=self._color.currentData()))
        self._flag.currentIndexChanged.connect(lambda: self._save_metadata(flag=self._flag.currentData()))
        self._tags.returnPressed.connect(self._save_tags)
        self._tags.editingFinished.connect(self._save_tags)
        save_tags.clicked.connect(self._save_tags)
        self._relocate_button.clicked.connect(self._relocate_selected)
        self._remove_button.clicked.connect(self._remove_selected)

    def _filters(self):
        current = self._folder_list.currentItem()
        return dict(search=self._search.text().strip(), min_rating=self._rating_filter.currentData(),
                    color=self._color_filter.currentData(), flag=self._flag_filter.currentData(),
                    folder=current.data(QtCore.Qt.UserRole) if current else '')

    def _filter_changed(self, *_args):
        self._offset = 0
        self.refresh()

    def _change_page(self, direction):
        self._offset = max(0, self._offset + direction * PAGE_SIZE)
        self.refresh()

    def refresh(self):
        if self._closing:
            return
        try:
            folder = self._filters()['folder']
            self._folder_list.blockSignals(True)
            self._folder_list.clear()
            for label, path in [('全部照片', '')] + [(Path(p).name or p, p) for p in self.catalog.folders()]:
                item = QtWidgets.QListWidgetItem(label)
                item.setData(QtCore.Qt.UserRole, path)
                item.setToolTip(path or '浏览全部已导入照片')
                self._folder_list.addItem(item)
                if path == folder:
                    self._folder_list.setCurrentItem(item)
            self._folder_list.blockSignals(False)
            filters = self._filters()
            total = self.catalog.count_photos(**filters)
            if self._offset >= total:
                self._offset = max(0, ((total - 1) // PAGE_SIZE) * PAGE_SIZE)
            photos = self.catalog.list_photos(**filters, limit=PAGE_SIZE, offset=self._offset)
        except DB_ERRORS as exc:
            self._folder_list.blockSignals(False)
            self._error('读取图库失败', exc)
            return
        self._thumb_cancelled.set()
        self._thumb_cancelled = threading.Event()
        self._generation += 1
        generation = self._generation
        selected = self._selected_id
        self._grid.blockSignals(True)
        self._grid.clear()
        self._items.clear()
        for photo in photos:
            missing = not Path(photo.path).is_file()
            status = ' · 原文件不可用' if missing else ''
            badges = ['★' * photo.rating or '未评分']
            if photo.color:
                badges.append(dict((value, name) for name, value in COLORS)[photo.color])
            if photo.flag:
                badges.append(dict((value, name) for name, value in FLAGS)[photo.flag])
            item = QtWidgets.QListWidgetItem(f'{photo.name}\n{" · ".join(badges)}{status}')
            item.setData(QtCore.Qt.UserRole, photo.id)
            item.setToolTip(photo.path + ('\n原文件不可用' if missing else ''))
            item.setIcon(self._placeholder('原文件不可用' if missing else '正在生成预览'))
            self._grid.addItem(item)
            self._items[photo.id] = item
            if photo.id == selected:
                self._grid.setCurrentItem(item)
            key = ('thumbnail', generation, photo.id)
            cancelled = self._thumb_cancelled
            self._start_task(key, lambda _progress, p=photo, cancel=cancelled:
                             (QtGui.QImage(), '') if cancel.is_set() else make_thumbnail(p, self._cache_dir),
                             thumbnail=True)
        self._grid.blockSignals(False)
        self._selection_changed()
        self._empty.setVisible(total == 0)
        self._empty.setText('没有符合筛选条件的照片。' if any(filters.values()) else
                            '导入一个照片文件夹，开始整理你的影像。')
        self._previous.setEnabled(self._offset > 0)
        self._next.setEnabled(self._offset + len(photos) < total)
        self._count.setText(f'{self._offset + 1 if photos else 0}–{self._offset + len(photos)} / {total} 张')

    def _placeholder(self, text):
        pixmap = QtGui.QPixmap(160, 108)
        pixmap.fill(QtGui.QColor('#242424'))
        painter = QtGui.QPainter(pixmap)
        painter.setPen(QtGui.QColor('#999999'))
        painter.drawText(pixmap.rect(), QtCore.Qt.AlignCenter, text)
        painter.end()
        return QtGui.QIcon(pixmap)

    def _start_task(self, key, fn, *, thumbnail=False):
        task = _Task(key, fn, thumbnail=thumbnail)
        self._tasks[key] = task
        task.signals.result.connect(self._task_result)
        task.signals.finished.connect(self._task_finished)
        if not thumbnail:
            task.signals.progress.connect(self._status.setText)
        self._pool.start(task, 0 if thumbnail else 1)

    @QtCore.Slot(object)
    def _task_finished(self, key):
        self._tasks.pop(key, None)

    @QtCore.Slot(object)
    def _task_result(self, payload):
        if self._closing:
            return
        key, result, error = payload
        if key[0] == 'thumbnail':
            if key[1] != self._generation or key[2] not in self._items:
                return
            item = self._items[key[2]]
            image, diagnostic = result if result is not None else (QtGui.QImage(), error)
            if not image.isNull():
                item.setIcon(QtGui.QIcon(QtGui.QPixmap.fromImage(image)))
            else:
                item.setIcon(self._placeholder('暂无预览'))
            if diagnostic:
                item.setToolTip(item.toolTip() + '\n' + diagnostic)
        else:
            self._importing = False
            self._import_files_button.setEnabled(True)
            self._import_folder_button.setEnabled(True)
            self._cancel_button.hide()
            self.refresh()
            if error:
                self._error('导入中断', error)
                return
            prefix = '已停止导入' if result['cancelled'] else '导入完成'
            self._status.setText(f"{prefix}：新增 {result['added']} · 已有 {result['existing']} · 失败 {result['failed']}")
            if result['errors']:
                QtWidgets.QMessageBox.warning(self, '部分文件未能导入', '\n'.join(result['errors']) +
                                               ('\n仅显示前 20 项。' if result['failed'] > 20 else ''))

    def _selection_changed(self, *_args):
        item = self._grid.currentItem()
        self._selected_id = item.data(QtCore.Qt.UserRole) if item else None
        try:
            photo = self.catalog.get_photo(self._selected_id) if self._selected_id is not None else None
        except DB_ERRORS as exc:
            self._error('读取照片信息失败', exc)
            photo = None
        self._details.setEnabled(photo is not None)
        if photo is None:
            self._name.setText('选择一张照片')
            self._path.clear()
            self._file_status.clear()
            return
        self._name.setText(photo.name)
        self._path.setText(photo.path)
        missing = not Path(photo.path).is_file()
        self._file_status.setText('原文件不可用；评分与关键词仍已保留。' if missing else
                                 f'{Path(photo.path).suffix.upper().lstrip(".")} · {photo.size / 1024 / 1024:.1f} MB')
        self._open_button.setEnabled(not missing)
        for widget, value in ((self._rating, photo.rating), (self._color, photo.color), (self._flag, photo.flag)):
            widget.blockSignals(True)
            widget.setCurrentIndex(widget.findData(value))
            widget.blockSignals(False)
        self._tags.setText(', '.join(photo.tags))

    def _save_metadata(self, **fields):
        if self._selected_id is None:
            return
        try:
            self.catalog.update_photo(self._selected_id, **fields)
            self.refresh()
            self._status.setText('已保存照片标记')
        except DB_ERRORS as exc:
            self._error('保存标记失败', exc)

    def _save_tags(self):
        tags = [part.strip() for part in self._tags.text().replace('，', ',').split(',') if part.strip()]
        if self._selected_id is None:
            return
        try:
            photo = self.catalog.get_photo(self._selected_id)
            if photo is not None and tuple(tags) != photo.tags:
                self._save_metadata(tags=tags)
        except DB_ERRORS as exc:
            self._error('保存关键词失败', exc)

    def _open_selected(self, *_args):
        if self._selected_id is None:
            return
        try:
            photo = self.catalog.get_photo(self._selected_id)
            if photo is None or not Path(photo.path).is_file():
                raise FileNotFoundError('原文件不可用，请重新连接磁盘或重新定位。')
            self.photoActivated.emit(photo.id)
        except DB_ERRORS as exc:
            self._error('无法打开照片', exc)

    def _choose_files(self):
        extensions = ' '.join('*' + suffix for suffix in sorted(SUPPORTED_EXTENSIONS))
        paths, _ = QtWidgets.QFileDialog.getOpenFileNames(self, '导入照片（保留原文件）', '', f'照片 ({extensions})')
        if paths:
            self.start_import(paths)

    def _choose_folder(self):
        folder = QtWidgets.QFileDialog.getExistingDirectory(self, '导入文件夹（含子文件夹）')
        if folder:
            self.start_import([folder])

    def start_import(self, paths: list[str]):
        if self._importing or self._closing:
            return
        self._cancelled.clear()
        self._importing = True
        self._import_files_button.setEnabled(False)
        self._import_folder_button.setEnabled(False)
        self._cancel_button.show()
        self._status.setText('正在扫描文件夹并建立照片索引…')
        self._start_task(('import',), lambda progress: import_photos(self.catalog.path, paths, self._cancelled, progress))

    def _relocate_selected(self):
        if self._selected_id is None:
            return
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, '选择原照片的新位置')
        if not path:
            return
        answer = QtWidgets.QMessageBox.question(self, '确认重新定位',
            '请确认这是同一张原照片。已有评分、关键词和调色参数将关联到你选择的文件。',
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No, QtWidgets.QMessageBox.No)
        if answer != QtWidgets.QMessageBox.Yes:
            return
        try:
            self.catalog.relocate_photo(self._selected_id, path)
            self.refresh()
            self._status.setText('已更新原文件位置')
        except DB_ERRORS as exc:
            self._error('重新定位失败', exc)

    def _remove_selected(self):
        if self._selected_id is None:
            return
        answer = QtWidgets.QMessageBox.question(self, '从图库移除',
            '仅移除图库记录及其评分、关键词和调色参数。原照片不会被删除。',
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No, QtWidgets.QMessageBox.No)
        if answer != QtWidgets.QMessageBox.Yes:
            return
        try:
            if self.before_remove is not None and not self.before_remove(self._selected_id):
                return
            self.catalog.remove_photo(self._selected_id)
            self.photoRemoving.emit(self._selected_id)
            self._selected_id = None
            self.refresh()
            self._status.setText('已从图库移除；原照片保持不变')
        except DB_ERRORS as exc:
            self._error('移除失败', exc)

    def _error(self, title, error):
        self._status.setText(f'{title}：{error}')
        QtWidgets.QMessageBox.warning(self, title, str(error))

    def shutdown(self):
        """Call before closing the host window; workers stop between imported files."""
        self._closing = True
        self._cancelled.set()
        self._thumb_cancelled.set()
        self._pool.clear()
        self._pool.waitForDone()
        self._tasks.clear()
