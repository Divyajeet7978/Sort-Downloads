"""Tests for sort_downloads.

Month folders come from each file's creation time, which the unit tests replace
with a fixed month (``MONTH``) so they don't depend on when they run. The parity
test sets real creation times and checks the Python port files everything
exactly like the original PowerShell script.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest
from rich.console import Console

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import sort_downloads as sd  # noqa: E402

MONTH = "2026-09"
REPO = Path(__file__).resolve().parents[1]


def make(root: Path, relative: str, content: bytes = b"x") -> Path:
    """Create a file (and its folders) under ``root``."""
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def tree(root: Path) -> set[str]:
    """All files under ``root`` as forward-slash relative paths."""
    return {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}


@pytest.fixture(autouse=True)
def fixed_month(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sd, "arrival_month", lambda path: MONTH)


def run(root: Path) -> tuple[sd.Result, list[str]]:
    """Build and apply a plan; return the result and the folders sent to the 'Recycle Bin'."""
    trashed: list[str] = []
    result = sd.apply_plan(sd.build_plan(root), trash=trashed.append)
    return result, trashed


def test_one_off_stays_loose_in_its_category(tmp_path: Path) -> None:
    make(tmp_path, "notes.txt")
    run(tmp_path)
    assert tree(tmp_path) == {"Documents/notes.txt"}


def test_second_file_of_an_extension_gets_extension_and_month_folders(tmp_path: Path) -> None:
    make(tmp_path, "a.pdf", b"a")
    make(tmp_path, "b.PDF", b"b")
    run(tmp_path)
    assert tree(tmp_path) == {f"Documents/PDF/{MONTH}/a.pdf", f"Documents/PDF/{MONTH}/b.PDF"}


def test_existing_extension_folder_is_always_used(tmp_path: Path) -> None:
    make(tmp_path, "Compressed/ZIP/2025-01/old.zip", b"old")
    make(tmp_path, "new.zip", b"new")
    run(tmp_path)
    assert tree(tmp_path) == {"Compressed/ZIP/2025-01/old.zip", f"Compressed/ZIP/{MONTH}/new.zip"}


def test_one_off_moves_once_its_extension_has_company(tmp_path: Path) -> None:
    make(tmp_path, "Images/first.png", b"1")
    make(tmp_path, "second.png", b"2")
    run(tmp_path)
    assert tree(tmp_path) == {f"Images/PNG/{MONTH}/first.png", f"Images/PNG/{MONTH}/second.png"}


def test_loose_file_in_extension_folder_gets_a_month_folder(tmp_path: Path) -> None:
    make(tmp_path, "Data/CSV/loose.csv")
    run(tmp_path)
    assert tree(tmp_path) == {f"Data/CSV/{MONTH}/loose.csv"}


def test_filed_files_and_other_subfolders_are_never_moved(tmp_path: Path) -> None:
    make(tmp_path, "Documents/PDF/2025-03/filed.pdf")
    make(tmp_path, "Some Game/setup.exe")
    plan = sd.build_plan(tmp_path)
    assert plan.moves == []


def test_unknown_and_missing_extensions_go_to_other(tmp_path: Path) -> None:
    make(tmp_path, "model.blend")
    make(tmp_path, "README", b"1")
    make(tmp_path, "LICENSE", b"2")
    run(tmp_path)
    assert tree(tmp_path) == {"Other/model.blend", f"Other/NoExtension/{MONTH}/README",
                              f"Other/NoExtension/{MONTH}/LICENSE"}


def test_partial_downloads_and_the_tools_own_files_are_skipped(tmp_path: Path) -> None:
    for name in ("movie.mp4.crdownload", "setup.part", "sort_downloads.py", "Sort-Downloads.ps1",
                 "Sort-Downloads.cmd"):
        make(tmp_path, name)
    assert sd.build_plan(tmp_path).moves == []


@pytest.mark.skipif(os.name != "nt", reason="hidden attribute is Windows-only")
def test_hidden_files_are_skipped(tmp_path: Path) -> None:
    hidden = make(tmp_path, "secret.txt")
    subprocess.run(["attrib", "+h", str(hidden)], check=True)
    assert sd.build_plan(tmp_path).moves == []


def test_duplicates_keep_the_shortest_name_and_are_recycled_together(tmp_path: Path) -> None:
    make(tmp_path, "report.pdf", b"same")
    make(tmp_path, "report (1).pdf", b"same")
    make(tmp_path, "other.pdf", b"different")
    result, trashed = run(tmp_path)

    assert [duplicate.path.name for duplicate in result.recycled] == ["report (1).pdf"]
    assert result.recycled[0].kept.name == "report.pdf"
    assert len(trashed) == 1 and Path(trashed[0]).name.startswith("Duplicates ")
    # The fake trash leaves the folder in place, so the recycled copy is still inside it.
    assert (Path(trashed[0]) / "report (1).pdf").read_bytes() == b"same"
    assert {f"Documents/PDF/{MONTH}/report.pdf", f"Documents/PDF/{MONTH}/other.pdf"} <= tree(tmp_path)


def test_a_copy_already_filed_is_kept(tmp_path: Path) -> None:
    make(tmp_path, "Documents/PDF/2025-01/a.pdf", b"same")
    make(tmp_path, "a.pdf", b"same")
    result, _ = run(tmp_path)
    assert result.recycled[0].path == tmp_path / "a.pdf"
    assert result.recycled[0].kept == tmp_path / "Documents/PDF/2025-01/a.pdf"


def test_duplicates_are_only_matched_within_the_same_scope(tmp_path: Path) -> None:
    make(tmp_path, "a.pdf", b"same")
    make(tmp_path, "a.png", b"same")  # a different category, so not a duplicate
    result, trashed = run(tmp_path)
    assert result.recycled == [] and trashed == []


def test_one_offs_loose_in_the_same_category_are_compared(tmp_path: Path) -> None:
    make(tmp_path, "a.pdf", b"same")
    make(tmp_path, "copy.txt", b"same")  # both stay loose in Documents: same scope
    result, _ = run(tmp_path)
    assert [duplicate.path.name for duplicate in result.recycled] == ["copy.txt"]


def test_empty_files_are_never_duplicates(tmp_path: Path) -> None:
    make(tmp_path, "one.log", b"")
    make(tmp_path, "two.log", b"")
    result, _ = run(tmp_path)
    assert result.recycled == [] and len(result.moved) == 2


def test_name_clash_gets_a_numbered_name(tmp_path: Path) -> None:
    make(tmp_path, f"Data/CSV/{MONTH}/data.csv", b"filed")
    make(tmp_path, "data.csv", b"new")
    run(tmp_path)
    assert tree(tmp_path) == {f"Data/CSV/{MONTH}/data.csv", f"Data/CSV/{MONTH}/data (2).csv"}


def test_split_extension_matches_dotnet() -> None:
    assert sd.split_extension("archive.tar.gz") == ("archive.tar", ".gz")
    assert sd.split_extension(".env") == ("", ".env")
    assert sd.split_extension("file.") == ("file.", "")
    assert sd.split_extension("README") == ("README", "")


def test_dry_run_moves_nothing(tmp_path: Path) -> None:
    make(tmp_path, "a.pdf", b"a")
    make(tmp_path, "b.pdf", b"b")
    console = Console(record=True, width=100)
    assert sd.main([str(tmp_path), "--dry-run"], console=console) == 0
    assert tree(tmp_path) == {"a.pdf", "b.pdf"}
    output = console.export_text()
    assert "Documents" in output and "2 files to sort" in output and "Dry run" in output


def test_yes_sorts_without_asking(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    make(tmp_path, "a.pdf", b"same")
    make(tmp_path, "b.pdf", b"same")
    trashed: list[str] = []
    monkeypatch.setattr(sd, "send2trash", trashed.append)
    console = Console(record=True, width=100)
    assert sd.main([str(tmp_path), "--yes"], console=console) == 0
    assert len(trashed) == 1
    assert "Files sorted: 1" in console.export_text()


def test_tidy_folder_says_so(tmp_path: Path) -> None:
    make(tmp_path, "Documents/PDF/2025-01/a.pdf")
    console = Console(record=True, width=100)
    assert sd.main([str(tmp_path)], console=console) == 0
    assert "Nothing to sort" in console.export_text()


# --- Parity with the original PowerShell script -------------------------------------------

def _set_created(path: Path, when: datetime) -> None:
    """Set a file's creation time (Windows)."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                     wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel32.SetFileTime.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME), wintypes.LPVOID,
                                     wintypes.LPVOID]
    handle = kernel32.CreateFileW(str(path), 0x100, 0x7, None, 3, 0x80, None)  # FILE_WRITE_ATTRIBUTES
    ticks = int((when.timestamp() + 11644473600) * 10_000_000)
    created = wintypes.FILETIME(ticks & 0xFFFFFFFF, ticks >> 32)
    try:
        assert kernel32.SetFileTime(handle, ctypes.byref(created), None, None), ctypes.get_last_error()
    finally:
        kernel32.CloseHandle(handle)


def _build_messy_downloads(root: Path) -> None:
    """A Downloads folder exercising every rule, with creation times across several months."""
    files = {
        "Invoice.pdf": (b"invoice", datetime(2026, 9, 3)),
        "Invoice (1).pdf": (b"invoice", datetime(2026, 9, 4)),       # duplicate, longer name
        "notes.txt": (b"notes", datetime(2026, 9, 5)),               # one-off
        "photo.JPG": (b"photo", datetime(2026, 8, 15)),              # upper-case extension
        "holiday.jpg": (b"holiday", datetime(2026, 9, 1)),
        "setup.exe": (b"setup", datetime(2026, 7, 2)),               # one-off
        "archive.zip": (b"archive", datetime(2026, 9, 6)),
        "copy of filed.zip": (b"filed zip", datetime(2026, 9, 7)),   # duplicate of a filed file
        "one.log": (b"", datetime(2026, 9, 8)),                      # empty: never a duplicate
        "two.log": (b"", datetime(2026, 9, 8)),
        "README": (b"readme", datetime(2026, 6, 1)),                 # no extension
        ".env": (b"env", datetime(2026, 9, 9)),                      # all extension
        "data.csv": (b"new data", datetime(2026, 9, 10)),            # clashes with a filed name
        "movie.mp4.crdownload": (b"partial", datetime(2026, 9, 11)),
        "Sort-Downloads.ps1": (b"the tool itself", datetime(2026, 9, 11)),
        "Some Game/game.exe": (b"game", datetime(2026, 5, 5)),       # subfolder: untouched
        "Documents/old.pdf": (b"old pdf", datetime(2026, 5, 10)),    # one-off from an earlier run
        "Compressed/ZIP/2026-01/filed.zip": (b"filed zip", datetime(2026, 1, 20)),
        "Images/PNG/loose.png": (b"loose png", datetime(2026, 7, 1)),  # not yet dated
        "Data/CSV/2026-09/data.csv": (b"filed data", datetime(2026, 9, 2)),
    }
    for relative, (content, created) in files.items():
        _set_created(make(root, relative, content), created)
    hidden = make(root, "secret.dat", b"hidden")
    subprocess.run(["attrib", "+h", str(hidden)], check=True)


def _snapshot(root: Path) -> set[tuple[str, str]]:
    """Every file with its content hash; the timestamped Duplicates folder name is normalized."""
    snapshot = set()
    for path in root.rglob("*"):
        if path.is_file():
            parts = path.relative_to(root).parts
            if parts[0].startswith("Duplicates "):
                parts = ("Duplicates *", *parts[1:])
            snapshot.add(("/".join(parts), hashlib.md5(path.read_bytes()).hexdigest()))
    return snapshot


@pytest.mark.skipif(os.name != "nt" or shutil.which("powershell") is None, reason="needs Windows PowerShell")
def test_matches_the_original_powershell_script(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.undo()  # use real creation times
    ps_root, py_root, tool = tmp_path / "ps", tmp_path / "py", tmp_path / "tool"
    for root in (ps_root, py_root):
        _build_messy_downloads(root)

    # Run a copy of the original that leaves the Duplicates folder in place of recycling it.
    script = (REPO / "powershell" / "Sort-Downloads.ps1").read_text(encoding="utf-8")
    recycle = "[Microsoft.VisualBasic.FileIO.FileSystem]::DeleteDirectory($duplicatesFolder, 'OnlyErrorDialogs', 'SendToRecycleBin')"
    assert script.count(recycle) == 1
    tool.mkdir()
    (tool / "Sort-Downloads.ps1").write_text(script.replace(recycle, "$null = $duplicatesFolder"), encoding="utf-8")
    subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                    str(tool / "Sort-Downloads.ps1"), "-Path", str(ps_root)], check=True, capture_output=True)

    result = sd.apply_plan(sd.build_plan(py_root), trash=lambda folder: None)

    assert result.skipped == []
    assert _snapshot(py_root) == _snapshot(ps_root)
    # Spot-check the rules the snapshot covers.
    filed = {name for name, _ in _snapshot(py_root)}
    assert {"Documents/PDF/2026-09/Invoice.pdf", "Documents/PDF/2026-05/old.pdf", "Duplicates */Invoice (1).pdf",
            "Duplicates */copy of filed.zip", "Images/JPG/2026-08/photo.JPG", "Images/PNG/2026-07/loose.png",
            "Data/CSV/2026-09/data (2).csv", "Documents/notes.txt", "Programs/setup.exe", "Other/README",
            "Other/.env", "movie.mp4.crdownload", "Sort-Downloads.ps1", "secret.dat",
            "Some Game/game.exe"} <= filed
