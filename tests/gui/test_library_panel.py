from __future__ import annotations

import hashlib
import os
import sqlite3
from pathlib import Path
import threading
import time

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from qtpy import QtCore, QtGui, QtWidgets

from spektrafilm_gui.catalog import Catalog
from spektrafilm_gui.library_panel import LibraryPanel, import_photos
from spektrafilm_gui.library_thumbnails import make_thumbnail


@pytest.fixture(scope='module')
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def image_file(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    image = QtGui.QImage(80, 60, QtGui.QImage.Format_RGB32)
    image.fill(QtGui.QColor('#ee9470'))
    assert image.save(str(path))
    return path


def test_folder_import_is_read_only_idempotent_and_does_not_follow_directory_links(tmp_path, app):
    folder = tmp_path / '旅行照片'
    photo = image_file(folder / '上海.png')
    before = hashlib.sha256(photo.read_bytes()).hexdigest()
    outside = tmp_path / 'outside'
    image_file(outside / '不应导入.png')
    (folder / '链接').symlink_to(outside, target_is_directory=True)
    (folder / '说明.txt').write_text('not a photo')
    catalog = Catalog(tmp_path / 'catalog.sqlite')
    report = import_photos(catalog.path, [str(folder)], threading.Event(), lambda _: None)
    assert (report['added'], report['failed']) == (1, 0)
    again = import_photos(catalog.path, [str(folder)], threading.Event(), lambda _: None)
    assert (again['added'], again['existing']) == (0, 1)
    assert catalog.count_photos() == 1
    assert hashlib.sha256(photo.read_bytes()).hexdigest() == before
    cancelled = threading.Event()
    cancelled.set()
    assert import_photos(catalog.path, [str(outside)], cancelled, lambda _: None)['added'] == 0


def test_panel_metadata_filter_activation_and_removal_veto(tmp_path, app, monkeypatch):
    monkeypatch.setattr(LibraryPanel, '_start_task', lambda *args, **kwargs: None)
    catalog = Catalog(tmp_path / 'catalog.sqlite')
    original = image_file(tmp_path / '人像.png')
    catalog.import_files([original])
    photo = catalog.list_photos()[0]
    panel = LibraryPanel(catalog)
    try:
        panel._grid.setCurrentRow(0)
        panel._rating.setCurrentIndex(4)
        panel._tags.setText('人像，旅行')
        panel._save_tags()
        saved = catalog.get_photo(photo.id)
        assert saved.rating == 4
        assert set(saved.tags) == {'人像', '旅行'}
        panel._search.setText('旅行')
        panel._filter_changed()
        assert panel._grid.count() == 1
        opened = []
        panel.photoActivated.connect(opened.append)
        panel._open_selected()
        assert opened == [photo.id]
        removed = []
        panel.photoRemoving.connect(removed.append)
        monkeypatch.setattr(QtWidgets.QMessageBox, 'question', lambda *args: QtWidgets.QMessageBox.Yes)
        panel.before_remove = lambda _: False
        panel._remove_selected()
        assert catalog.get_photo(photo.id) is not None
        assert removed == []
        panel.before_remove = lambda _: True
        panel._remove_selected()
        assert removed == [photo.id]
        assert catalog.get_photo(photo.id) is None
        assert original.is_file()
    finally:
        panel.shutdown()
        panel.deleteLater()
        app.processEvents()


def test_panel_pages_without_loading_whole_catalog_and_marks_missing_file(tmp_path, app, monkeypatch):
    monkeypatch.setattr(LibraryPanel, '_start_task', lambda *args, **kwargs: None)
    files = []
    for index in range(205):
        path = tmp_path / f'{index:03}.jpg'
        path.write_bytes(b'indexed without decoding')
        files.append(path)
    catalog = Catalog(tmp_path / 'catalog.sqlite')
    catalog.import_files(files)
    panel = LibraryPanel(catalog)
    try:
        assert panel._grid.count() == 200
        panel._change_page(1)
        assert panel._grid.count() == 5
        panel._grid.setCurrentRow(0)
        current = catalog.get_photo(panel._selected_id)
        Path(current.path).unlink()
        panel.refresh()
        assert not panel._open_button.isEnabled()
        assert '原文件不可用' in panel._file_status.text()
        assert catalog.count_photos() == 205
    finally:
        panel.shutdown()
        panel.deleteLater()
        app.processEvents()


def test_thumbnail_cache_retains_missing_previews_and_rejects_changed_sources(tmp_path, app):
    source = image_file(tmp_path / '原图.png')
    before = source.read_bytes()
    catalog = Catalog(tmp_path / 'catalog.sqlite')
    catalog.import_files([source])
    photo = catalog.list_photos()[0]
    image, error = make_thumbnail(photo, tmp_path / 'cache')
    assert not image.isNull() and not error
    assert source.read_bytes() == before
    source.unlink()
    image, error = make_thumbnail(photo, tmp_path / 'cache')
    assert not image.isNull() and not error
    source.write_bytes(b'changed')
    image, error = make_thumbnail(photo, tmp_path / 'cache')
    assert image.isNull() and '变化' in error


def test_background_import_updates_grid_and_shutdown_drains_workers(tmp_path, app, monkeypatch):
    monkeypatch.setattr(QtWidgets.QMessageBox, 'warning', lambda *args: None)
    photo = image_file(tmp_path / '照片.png')
    panel = LibraryPanel(Catalog(tmp_path / 'catalog.sqlite'))
    panel._cache_dir = tmp_path / 'cache'
    try:
        panel.start_import([str(photo)])
        deadline = time.monotonic() + 10
        while panel._importing and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        assert not panel._importing
        assert panel._grid.count() == 1
        assert '新增 1' in panel._status.text()
    finally:
        panel.shutdown()
        assert panel._pool.activeThreadCount() == 0
        panel.deleteLater()
        app.processEvents()


def test_failed_removal_does_not_detach_the_active_photo(tmp_path, app, monkeypatch):
    monkeypatch.setattr(LibraryPanel, '_start_task', lambda *args, **kwargs: None)
    monkeypatch.setattr(QtWidgets.QMessageBox, 'question', lambda *args: QtWidgets.QMessageBox.Yes)
    warnings = []
    monkeypatch.setattr(QtWidgets.QMessageBox, 'warning', lambda *args: warnings.append(args[1]))
    catalog = Catalog(tmp_path / 'catalog.sqlite')
    original = image_file(tmp_path / '照片.png')
    catalog.import_files([original])
    panel = LibraryPanel(catalog)
    removed = []
    panel.photoRemoving.connect(removed.append)
    try:
        panel._grid.setCurrentRow(0)
        photo_id = panel._selected_id
        def fail(_id):
            raise sqlite3.OperationalError('disk error')
        monkeypatch.setattr(catalog, 'remove_photo', fail)
        panel._remove_selected()
        assert removed == []
        assert panel._selected_id == photo_id
        assert catalog.get_photo(photo_id) is not None
        assert warnings == ['移除失败']
        assert original.is_file()
    finally:
        panel.shutdown()
        panel.deleteLater()
        app.processEvents()
