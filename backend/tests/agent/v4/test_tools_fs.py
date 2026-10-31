"""`fs_read` / `fs_write` / `fs_list`: the workload stays inside the run's workspace (L0)."""

from __future__ import annotations

from pathlib import Path

import pytest

from botai.agent.v4.protocol import ToolContext
from botai.agent.v4.tools.fs import FsListTool, FsReadTool, FsWriteTool, resolve_in_workspace


def context(workspace: Path) -> ToolContext:
    return ToolContext(run_id="run-1", workspace=workspace)


# --- write and read -----------------------------------------------------------------------------


async def test_write_then_read_roundtrip(tmp_path: Path) -> None:
    writer = FsWriteTool()
    reader = FsReadTool()
    written = await writer.run(
        {"path": "notes/today.md", "content": "привет, мир"}, context(tmp_path)
    )
    assert written.ok
    assert (tmp_path / "notes" / "today.md").read_text("utf-8") == "привет, мир"
    assert written.data["bytes"] == len("привет, мир".encode())
    assert written.data["mode"] == "overwrite"

    read = await reader.run({"path": "notes/today.md"}, context(tmp_path))
    assert read.ok
    assert read.data["content"] == "привет, мир"
    assert read.data["total_chars"] == len("привет, мир")
    assert read.data["truncated"] is False


async def test_append_mode_keeps_the_previous_content(tmp_path: Path) -> None:
    writer = FsWriteTool()
    await writer.run({"path": "log.txt", "content": "a"}, context(tmp_path))
    appended = await writer.run(
        {"path": "log.txt", "content": "b", "mode": "append"}, context(tmp_path)
    )
    assert appended.ok
    assert (tmp_path / "log.txt").read_text() == "ab"


async def test_reading_with_offset_and_limit_reports_truncation(tmp_path: Path) -> None:
    (tmp_path / "data.txt").write_text("abcdef", encoding="utf-8")
    result = await FsReadTool().run(
        {"path": "data.txt", "offset": 2, "max_chars": 3}, context(tmp_path)
    )
    assert result.ok
    assert result.data["content"] == "cde"
    assert result.data["truncated"] is True


async def test_binary_file_is_not_decoded_as_text(tmp_path: Path) -> None:
    (tmp_path / "image.bin").write_bytes(b"\x00\x01\x02")
    result = await FsReadTool().run({"path": "image.bin"}, context(tmp_path))
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "binary_file"


async def test_missing_file_and_directory_are_structured_errors(tmp_path: Path) -> None:
    missing = await FsReadTool().run({"path": "nope.txt"}, context(tmp_path))
    assert missing.error is not None
    assert missing.error.code == "not_found"
    directory = await FsReadTool().run({"path": "."}, context(tmp_path))
    assert directory.error is not None
    assert directory.error.code == "is_directory"


async def test_write_can_refuse_to_create_parents(tmp_path: Path) -> None:
    result = await FsWriteTool().run(
        {"path": "a/b/c.txt", "content": "x", "create_parents": False}, context(tmp_path)
    )
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "parent_missing"


# --- workspace guard ----------------------------------------------------------------------------


async def test_relative_traversal_is_refused(tmp_path: Path) -> None:
    result = await FsWriteTool().run(
        {"path": "../outside.txt", "content": "escape"}, context(tmp_path)
    )
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "workspace_escape"
    assert not (tmp_path.parent / "outside.txt").exists()

    read = await FsReadTool().run({"path": "../outside.txt"}, context(tmp_path))
    assert read.error is not None
    assert read.error.code == "workspace_escape"


async def test_absolute_path_outside_is_refused(tmp_path: Path) -> None:
    outside = tmp_path.parent / "absolute.txt"
    outside.write_text("outside", encoding="utf-8")
    result = await FsReadTool().run({"path": str(outside)}, context(tmp_path))
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "workspace_escape"

    inside = tmp_path / "inside.txt"
    inside.write_text("inside", encoding="utf-8")
    allowed = await FsReadTool().run({"path": str(inside)}, context(tmp_path))
    assert allowed.ok


async def test_symlink_cannot_walk_out_of_the_workspace(tmp_path: Path) -> None:
    outside = tmp_path.parent / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    link = tmp_path / "link.txt"
    link.symlink_to(outside)
    result = await FsReadTool().run({"path": "link.txt"}, context(tmp_path))
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "workspace_escape"


def test_resolve_accepts_the_root_and_rejects_escapes(tmp_path: Path) -> None:
    assert resolve_in_workspace(tmp_path, ".") == tmp_path.resolve()
    resolved = resolve_in_workspace(tmp_path, "sub/file.txt")
    assert resolved == (tmp_path / "sub" / "file.txt").resolve()
    with pytest.raises(ValueError, match="workspace"):
        resolve_in_workspace(tmp_path, "../x")


# --- list ---------------------------------------------------------------------------------------


async def test_list_shows_files_and_directories(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.txt").write_text("b", encoding="utf-8")
    result = await FsListTool().run({"path": "."}, context(tmp_path))
    assert result.ok
    entries = {entry["path"]: entry["kind"] for entry in result.data["entries"]}
    assert entries == {"a.txt": "file", "sub": "dir"}


async def test_recursive_list_and_entry_cap(tmp_path: Path) -> None:
    for index in range(5):
        (tmp_path / f"f{index}.txt").write_text("x", encoding="utf-8")
    result = await FsListTool().run(
        {"path": ".", "recursive": True, "max_entries": 3}, context(tmp_path)
    )
    assert result.ok
    assert len(result.data["entries"]) == 3
    assert result.data["truncated"] is True
    assert result.truncated is True


async def test_list_of_a_missing_path_and_of_a_file(tmp_path: Path) -> None:
    missing = await FsListTool().run({"path": "nope"}, context(tmp_path))
    assert missing.error is not None
    assert missing.error.code == "not_found"
    (tmp_path / "one.txt").write_text("1", encoding="utf-8")
    single = await FsListTool().run({"path": "one.txt"}, context(tmp_path))
    assert single.ok
    assert single.data["entries"][0]["kind"] == "file"
