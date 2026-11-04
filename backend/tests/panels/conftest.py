"""Fixtures of the panels tests: the shared auth fixtures and the v4 application on fakeuni."""

from tests.auth.support import *  # noqa: F403
from tests.sources.v4.conftest import (  # noqa: F401
    account,
    artifact_store,
    fakeuni_cookies,
    fakeuni_transport,
    second_account,
    v4_app,
    v4_client,
    v4_settings,
)
