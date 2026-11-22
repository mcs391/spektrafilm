from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import numpy as np
from PIL import Image
import pytest
from qtpy import QtCore, QtWidgets

from spektrafilm_gui import controller as controller_module
from spektrafilm_gui.catalog import Catalog
from spektrafilm_gui.controller import GuiController
from spektrafilm_gui.library_workspace import LibraryWorkspace
from spektrafilm_gui.state import PROJECT_DEFAULT_GUI_STATE, clone_gui_state
from spektrafilm_gui.state_bridge import apply_gui_state, collect_gui_state
from spektrafilm_gui.widgets import create_widget_bundle
from .helpers import FakeViewer


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    widgets = create_widget_bundle()
    apply_gui_state(clone_gui_state(PROJECT_DEFAULT_GUI_STATE), widgets=widgets)
    widgets.simulation.set_auto_preview_value(False)
    viewer = FakeViewer()
    controller = GuiController(viewer=viewer, widgets=widgets)
    monkeypatch.setattr(controller_module, 'set_status', lambda *a, **kw: None)
    monkeypatch.setattr(controller_module, 'load_image_oiio', lambda p: np.asarray(Image.open(p)).astype(float) / 255)
    def update(image, **_kwargs):
        controller._current_input_image = image
        controller._current_preview_image = image
    monkeypatch.setattr(controller, '_set_or_add_input_stack', update)
    monkeypatch.setattr(controller, '_update_preview_cache', update)
    previews = []
    monkeypatch.setattr(controller, '_run_preview', lambda **kw: previews.append(controller._current_input_path))
    errors = []
    for method in ('critical', 'warning', 'information'):
        monkeypatch.setattr(QtWidgets.QMessageBox, method, lambda *args: errors.append(args[-1]))
    widgets.filepicker.load_requested.connect(controller.load_input_image)
    widgets.load_raw.load_requested.connect(controller.load_raw_image)
    paths = [tmp_path / '照片一.png', tmp_path / '照片二.png']
    for index, path in enumerate(paths):
        Image.new('RGB', (30, 20), (50 + index * 100, 80, 100)).save(path)
    catalog = Catalog(tmp_path / 'catalog.sqlite3')
    catalog.import_files(paths)
    ids = [catalog.get_by_path(p).id for p in paths]
    window = QtWidgets.QMainWindow()
    window.setCentralWidget(QtWidgets.QWidget())
    work = LibraryWorkspace(window=window, controller=controller, widgets=widgets, catalog=catalog)
    work._autosave.stop()
    yield SimpleNamespace(app=app, work=work, controller=controller, widgets=widgets,
                          paths=paths, ids=ids, errors=errors, previews=previews)
    work.panel.shutdown()
    window.removeEventFilter(work)
    window.close()
    app.processEvents()


def exposure(widgets, value=None):
    state = collect_gui_state(widgets=widgets)
    if value is not None:
        state.camera.exposure_compensation_ev = value
        apply_gui_state(state, widgets=widgets)
    return state.camera.exposure_compensation_ev


def test_switch_persists_independent_edits_and_rotation(workspace):
    s = workspace
    before = [p.read_bytes() for p in s.paths]
    s.work.open_photo(s.ids[0])
    exposure(s.widgets, 1.5)
    s.controller._rotate_input_image(quarter_turns=1)
    s.work.open_photo(s.ids[1])
    assert exposure(s.widgets) == PROJECT_DEFAULT_GUI_STATE.camera.exposure_compensation_ev
    assert s.controller.input_rotation_quarters == 0
    exposure(s.widgets, -0.5)
    s.work.open_photo(s.ids[0])
    assert exposure(s.widgets) == 1.5
    assert s.controller.input_rotation_quarters == 1
    assert s.controller._current_input_image.shape[:2] == (30, 20)
    s.work.save_active()
    reopened = Catalog(s.work.catalog.path)
    assert reopened.get_edit(s.ids[0])['camera']['exposure_compensation_ev'] == 1.5
    assert reopened.get_edit(s.ids[1])['camera']['exposure_compensation_ev'] == -0.5
    assert 'display' not in reopened.get_edit(s.ids[0])
    assert [p.read_bytes() for p in s.paths] == before


def test_failed_load_restores_photo_and_suppresses_nested_preview(workspace, monkeypatch):
    s = workspace
    s.work.open_photo(s.ids[0])
    exposure(s.widgets, 1.0)
    s.widgets.simulation.set_auto_preview_value(True)
    s.app.processEvents()
    s.previews.clear()
    old_image = s.controller._current_input_image.copy()
    original = s.controller.load_input_image
    def fail(path):
        assert s.controller._preview_suspended
        s.controller.request_auto_preview()
        s.app.processEvents()  # A modal decode-error dialog also runs a nested event loop.
        assert s.previews == []
        return False
    monkeypatch.setattr(s.controller, 'load_input_image', fail)
    s.work.open_photo(s.ids[1])
    assert s.work.active_photo_id == s.ids[0]
    assert exposure(s.widgets) == 1.0
    np.testing.assert_array_equal(s.controller._current_input_image, old_image)
    assert s.work.catalog.get_edit(s.ids[1]) is None
    assert not s.controller._preview_suspended
    monkeypatch.setattr(s.controller, 'load_input_image', original)


def test_storage_failure_blocks_switch_and_close(workspace, monkeypatch):
    s = workspace
    s.work.open_photo(s.ids[0])
    exposure(s.widgets, 2.0)
    monkeypatch.setattr(s.work.catalog, 'save_edit', lambda *a: (_ for _ in ()).throw(OSError('disk full')))
    s.work.open_photo(s.ids[1])
    assert s.work.active_photo_id == s.ids[0]
    event = QtCore.QEvent(QtCore.QEvent.Close)
    assert s.work.eventFilter(s.work.window, event) is True
    assert not event.isAccepted()
    assert any('disk full' in e for e in s.errors)


def test_relocated_active_photo_reloads_and_busy_switch_is_refused(workspace):
    s = workspace
    s.work.open_photo(s.ids[0])
    moved = s.paths[0].with_name('新的位置.png')
    s.paths[0].rename(moved)
    s.work.catalog.relocate_photo(s.ids[0], moved)
    s.work.open_photo(s.ids[0])
    assert s.controller._current_input_path == str(moved)
    assert s.widgets.filepicker.file_path.text() == str(moved)
    s.controller._active_simulation_worker = object()
    s.work.open_photo(s.ids[1])
    assert s.work.active_photo_id == s.ids[0]
    s.controller._active_simulation_worker = None


@pytest.mark.parametrize('alias', ['original', 'hardlink', 'other'])
def test_export_never_overwrites_existing_photographs(workspace, monkeypatch, alias):
    s = workspace
    source = s.paths[0]
    destination = source
    if alias == 'hardlink':
        destination = source.with_name('ALIAS.PNG')
        destination.hardlink_to(source)
    elif alias == 'other':
        destination = s.paths[1]
    original_bytes = destination.read_bytes()
    s.controller._current_input_path = str(source)
    monkeypatch.setattr(s.controller, '_output_layer', lambda: object())
    monkeypatch.setattr(controller_module, '_DirMemoryDialog', lambda *a:
                        SimpleNamespace(get_save_file_name=lambda *a: (str(destination), '')))
    monkeypatch.setattr(controller_module, 'save_image_oiio', lambda *a, **kw: pytest.fail('must not write'))
    s.controller.save_output_layer()
    assert destination.read_bytes() == original_bytes
    assert any('导出只写入新文件' in e for e in s.errors)
