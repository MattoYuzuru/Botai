from __future__ import annotations

from collections.abc import Iterator

import pytest

from tests.fakeuni import api as fakeuni_api
from tests.fakeuni import login as fakeuni_login


@pytest.fixture(autouse=True)
def _reset_fakeuni() -> Iterator[None]:
    fakeuni_api.reset()
    fakeuni_login.SESSIONS.clear()
    yield
    fakeuni_api.reset()
    fakeuni_login.SESSIONS.clear()
