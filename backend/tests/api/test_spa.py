from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from botai.main import create_app
from botai.settings import get_settings


@pytest.fixture
def static_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<!doctype html><title>BotAI</title>")
    (tmp_path / "assets" / "app.js").write_text("console.log(1)")
    (tmp_path / "manifest.webmanifest").write_text("{}")
    monkeypatch.setenv("STATIC_DIR", str(tmp_path))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


async def test_spa_fallback_and_api_isolation(static_dir: Path) -> None:
    app = create_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        assert "BotAI" in (await c.get("/schedule")).text
        assert (await c.get("/assets/app.js")).status_code == 200
        assert (await c.get("/manifest.webmanifest")).text == "{}"
        assert (await c.get("/api/nope")).status_code == 404
        assert (await c.get("/../../etc/passwd")).status_code in (200, 404)
        assert "root:" not in (await c.get("/..%2f..%2fetc/passwd")).text
        assert (await c.get("/api/health")).json() == {"status": "ok"}
