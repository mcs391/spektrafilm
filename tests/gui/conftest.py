"""GUI test configuration.

The editor follows the system language; upstream assertions use English,
so pin that language while testing. The new library is Chinese-only.
"""

import pytest


@pytest.fixture(autouse=True)
def _english_ui(monkeypatch):
    monkeypatch.setenv('SPEKTRAFILM_LANG', 'en')
