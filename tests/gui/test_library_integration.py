"""A real Qt + image-I/O + simulation round trip, using generated test photos."""
import os

import numpy as np
from PIL import Image
import pytest
from qtpy import QtCore, QtGui, QtWidgets

from spektrafilm_gui import app as app_module
from spektrafilm_gui import controller as controller_module
from spektrafilm_gui.catalog import Catalog
from spektrafilm_gui.controller_layers import INPUT_PREVIEW_LAYER_NAME
from spektrafilm_gui.library_workspace import LibraryWorkspace


pytestmark = pytest.mark.skipif(
    os.environ.get('QT_QPA_PLATFORM') in {'offscreen', 'minimal'},
    reason='Napari requires a real OpenGL context; run this test with QT_QPA_PLATFORM=cocoa on macOS.',
)


def test_real_library_editor_export_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv('SPEKTRAFILM_LANG', 'zh')
    monkeypatch.setattr(app_module, '_schedule_background_warmup', lambda: None)
    app = app_module.create_app()
    assert app.widgets.simulation.preview_button.text() == '预览'
    app.widgets.simulation.set_auto_preview_value(False)
    source = tmp_path / '中文照片.png'
    second = tmp_path / '另一张照片.png'
    pixels = np.tile(np.linspace(20, 230, 48, dtype=np.uint8)[None, :, None], (32, 1, 3))
    Image.fromarray(pixels).save(source)
    Image.fromarray(pixels[:, ::-1]).save(second)
    original = source.read_bytes()
    catalog = Catalog(tmp_path / 'library.sqlite3')
    catalog.import_files([source, second])
    work = LibraryWorkspace(window=app.main_window, controller=app.controller,
                            widgets=app.widgets, catalog=catalog)
    work._autosave.stop()
    errors = []
    monkeypatch.setattr(QtWidgets.QMessageBox, 'critical', lambda *args: errors.append(args[-1]))
    try:
        photo_id = catalog.get_by_path(source).id
        work.open_photo(photo_id)
        assert work.active_photo_id == photo_id, errors
        assert app.controller._current_input_image.shape == (32, 48, 3)
        app.controller._run_simulation(source_layer_name=INPUT_PREVIEW_LAYER_NAME)
        result = app.controller._output_layer_float_data()
        assert result is not None and np.isfinite(result).all()
        target = tmp_path / '胶片导出.png'
        monkeypatch.setattr(controller_module.QFileDialog, 'getSaveFileName',
                            lambda *args: (str(target), 'PNG'))
        app.controller.save_output_layer()
        assert target.is_file(), errors
        with Image.open(target) as exported:
            assert exported.size == (48, 32)
        assert source.read_bytes() == original
        work.open_photo(catalog.get_by_path(second).id)
        assert app.controller._output_layer() is None  # Never export the previous photo's result.
        assert catalog.get_edit(photo_id) is not None
        assert not errors
        work.tabs.setCurrentIndex(0)
        work.panel._pool.waitForDone()
        QtWidgets.QApplication.processEvents()
        app.main_window.resize(1280, 800)
        app.main_window.ensurePolished()
        app.main_window.layout().activate()
        preview = QtGui.QImage(app.main_window.size(), QtGui.QImage.Format_ARGB32)
        preview.fill(QtCore.Qt.transparent)
        painter = QtGui.QPainter(preview)
        app.main_window.render(painter, QtCore.QPoint())
        painter.end()
        assert preview.save(str(tmp_path / 'library-preview.png'))
    finally:
        work._autosave.stop()
        work.panel.shutdown()
        app.main_window.removeEventFilter(work)
        app.main_window.close()
        app.viewer.close()
