"""Sort a Downloads folder into category, extension and month folders.

Loose files in the target folder are filed as ``Category/EXT/yyyy-MM/name.ext``
(for example ``Compressed/ZIP/2026-09/archive.zip``) and exact duplicates are
sent to the Recycle Bin. Existing subfolders are left untouched.

The rules match the original ``powershell/Sort-Downloads.ps1``:

* The category comes from ``CATEGORIES``; unlisted extensions go to ``Other``.
* An extension with a single file stays loose in its category folder. Once a
  second file of that extension shows up, both move to their own extension
  folder. An extension that already has a folder always uses it.
* Inside an extension folder, files are grouped by the month they arrived
  (creation time). Files already in a month folder are never moved again.
* Duplicates are files with identical content (same size and MD5) in the same
  extension folder, across all its month folders, or loose in the same category
  folder. A copy already filed is kept, otherwise the shortest name, then the
  oldest. The rest go to the Recycle Bin together, in one folder named
  ``Duplicates <date time>``, so they can be restored in one go. Empty files are
  never treated as duplicates.
* Files are never overwritten: a name clash becomes ``name (2).ext``.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import stat
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from rich import box
from rich.console import Console, Group
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn
from rich.prompt import Confirm
from rich.table import Table
from rich.text import Text
from rich.tree import Tree
from send2trash import send2trash

__version__ = "1.0.0"

# Browser/download-manager files that are still being written.
PARTIAL_EXTENSIONS = frozenset({".crdownload", ".part", ".partial", ".download", ".opdownload", ".tmp"})

CATEGORIES: dict[str, tuple[str, ...]] = {
    "Documents": ("pdf", "doc", "docx", "odt", "rtf", "txt", "md", "xls", "xlsx", "xlsm", "ods", "ppt",
                  "pptx", "odp", "dwg", "dxf"),
    "Images": ("png", "jpg", "jpeg", "gif", "bmp", "svg", "webp", "avif", "heic", "ico", "tif", "tiff"),
    "Videos": ("mp4", "mkv", "avi", "mov", "wmv", "webm", "m4v"),
    "Audio": ("mp3", "wav", "flac", "aac", "m4a", "ogg", "wma"),
    "Compressed": ("zip", "rar", "7z", "tar", "gz", "tgz", "xz", "bz2", "iso"),
    "Programs": ("exe", "msi", "dll", "sys", "inf", "apk", "msix", "appx"),
    "Code": ("js", "ts", "css", "html", "htm", "py", "java", "c", "cpp", "cs", "sh", "ps1", "bat", "cmd",
             "ipynb"),
    "Data": ("json", "csv", "xml", "yaml", "yml", "sql", "cypher", "log", "dat", "bak", "evtx", "bag", "prp",
             "ini"),
}
OTHER = "Other"
CATEGORY_OF = {extension: category for category, extensions in CATEGORIES.items() for extension in extensions}
CATEGORY_STYLES = {
    "Documents": "bright_blue", "Images": "magenta", "Videos": "red", "Audio": "green",
    "Compressed": "yellow", "Programs": "cyan", "Code": "bright_green", "Data": "orange3", OTHER: "grey70",
}

# The tool's own files (this script, its launcher, the PowerShell original) are never moved.
SELF_STEMS = frozenset({"sort_downloads", "sort-downloads"})

_HIDDEN_OR_SYSTEM = stat.FILE_ATTRIBUTE_HIDDEN | stat.FILE_ATTRIBUTE_SYSTEM
_FILES_SHOWN_PER_FOLDER = 4


@dataclass(frozen=True)
class Move:
    """A file to move.

    Attributes
    ----------
    source : Path
        The file as it is now.
    folder : Path
        Destination folder (a month folder, or the category folder for a one-off).
    scope : Path
        Extension folder, or the category folder for a one-off. Duplicates are
        looked for within the same scope.
    """

    source: Path
    folder: Path
    scope: Path


@dataclass(frozen=True)
class Duplicate:
    """A file with the same content as another file in its scope.

    Attributes
    ----------
    path : Path
        The copy that goes to the Recycle Bin.
    kept : Path
        The identical copy that stays.
    size : int
        File size in bytes.
    """

    path: Path
    kept: Path
    size: int


@dataclass
class Plan:
    """Everything a run would do, computed without touching any file.

    Attributes
    ----------
    root : Path
        The folder being sorted.
    moves : list of Move
        Files that are not where the rules put them (including duplicates).
    duplicates : list of Duplicate
        Files to send to the Recycle Bin instead of moving.
    """

    root: Path
    moves: list[Move]
    duplicates: list[Duplicate]

    @property
    def to_move(self) -> list[Move]:
        """Moves that are not duplicates."""
        recycled = {_key(duplicate.path) for duplicate in self.duplicates}
        return [move for move in self.moves if _key(move.source) not in recycled]


@dataclass
class Result:
    """What a run did.

    Attributes
    ----------
    moved : list of Move
        Files moved to their folder.
    recycled : list of Duplicate
        Duplicates sent to the Recycle Bin.
    skipped : list of tuple of (Path, str)
        Files that could not be moved, with the reason.
    """

    moved: list[Move] = field(default_factory=list)
    recycled: list[Duplicate] = field(default_factory=list)
    skipped: list[tuple[Path, str]] = field(default_factory=list)


def _key(path: Path) -> str:
    """Comparison key for a path (case-insensitive on Windows, like PowerShell)."""
    return os.path.normcase(os.path.normpath(str(path)))


def split_extension(name: str) -> tuple[str, str]:
    """Split a file name into base name and extension, the way .NET does.

    Parameters
    ----------
    name : str
        File name without folders.

    Returns
    -------
    tuple of (str, str)
        Base name and extension including the dot. ``.env`` is all extension;
        ``file.`` has none.
    """
    dot = name.rfind(".")
    if dot == -1 or dot == len(name) - 1:
        return name, ""
    return name[:dot], name[dot:]


def category_folder(root: Path, name: str) -> Path:
    """Category folder for a file name, e.g. ``root/Documents`` for ``a.PDF``."""
    extension = split_extension(name)[1].lstrip(".").lower()
    return root / CATEGORY_OF.get(extension, OTHER)


def extension_folder(root: Path, name: str) -> Path:
    """Extension folder for a file name, e.g. ``root/Documents/PDF`` for ``a.pdf``."""
    extension = split_extension(name)[1].lstrip(".")
    return category_folder(root, name) / (extension.upper() if extension else "NoExtension")


def is_hidden(path: Path) -> bool:
    """Whether the file is hidden or a system file (skipped in the target folder)."""
    attributes = getattr(path.stat(), "st_file_attributes", None)
    if attributes is None:  # not Windows: dotfiles are hidden
        return path.name.startswith(".")
    return bool(attributes & _HIDDEN_OR_SYSTEM)


def arrival_month(path: Path) -> str:
    """Month the file arrived (its creation time), as ``yyyy-MM``."""
    info = path.stat()
    created = getattr(info, "st_birthtime", info.st_ctime)
    return datetime.fromtimestamp(created).strftime("%Y-%m")


def file_md5(path: Path) -> str:
    """MD5 of a file's content, read in chunks."""
    digest = hashlib.md5(usedforsecurity=False)
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def unique_path(folder: Path, name: str) -> Path:
    """First free path for ``name`` in ``folder``: ``name.ext``, ``name (2).ext``, ..."""
    base, extension = split_extension(name)
    target = folder / name
    n = 2
    while target.exists():
        target = folder / f"{base} ({n}){extension}"
        n += 1
    return target


def _files_in(folder: Path) -> list[Path]:
    return sorted((path for path in folder.iterdir() if path.is_file()), key=lambda path: path.name.lower())


def collect_files(root: Path) -> list[Path]:
    """Files to (re)consider for filing.

    Parameters
    ----------
    root : Path
        The folder being sorted.

    Returns
    -------
    list of Path
        Visible loose files in ``root`` (minus partial downloads and the tool's own
        files), plus files loose in a category folder (one-offs) or in an
        extension folder (not yet dated), so they can move once their extension
        has company or get a month folder.
    """
    files = []
    for path in _files_in(root):
        base, extension = split_extension(path.name)
        if extension.lower() in PARTIAL_EXTENSIONS or base.lower() in SELF_STEMS or is_hidden(path):
            continue
        files.append(path)
    for category in [*CATEGORIES, OTHER]:
        folder = root / category
        if folder.is_dir():
            subfolders = sorted((path for path in folder.iterdir() if path.is_dir()), key=lambda p: p.name.lower())
            for directory in [folder, *subfolders]:
                files += [path for path in _files_in(directory) if path.name.lower() != "desktop.ini"]
    return files


def find_duplicates(root: Path, moves: list[Move]) -> list[Duplicate]:
    """Find incoming files whose content already exists in their scope, or among each other.

    Parameters
    ----------
    root : Path
        The folder being sorted.
    moves : list of Move
        Planned moves.

    Returns
    -------
    list of Duplicate
        Copies to recycle. In each set of identical files, a copy already filed is
        kept, otherwise the shortest name, then the oldest.
    """
    moving = {_key(move.source) for move in moves}
    candidates = [(move.source, _key(move.scope), True) for move in moves]
    scopes = {_key(move.scope): move.scope for move in moves}
    for scope_key, scope in sorted(scopes.items()):
        if not scope.is_dir():
            continue
        # An extension folder is searched through all its month folders; a category
        # folder only for its loose files.
        found = scope.rglob("*") if _key(scope.parent) != _key(root) else scope.iterdir()
        candidates += [(path, scope_key, False) for path in sorted(found)
                       if path.is_file() and _key(path) not in moving]

    by_size: dict[tuple[str, int], list[tuple[Path, bool]]] = defaultdict(list)
    for path, scope_key, is_moving in candidates:
        size = path.stat().st_size
        if size > 0:
            by_size[(scope_key, size)].append((path, is_moving))

    duplicates = []
    for (_, size), group in by_size.items():
        # Only groups with an incoming file are hashed, so filed files aren't re-read every run.
        if len(group) < 2 or not any(is_moving for _, is_moving in group):
            continue
        by_hash: dict[str, list[tuple[Path, bool]]] = defaultdict(list)
        for path, is_moving in group:
            by_hash[file_md5(path)].append((path, is_moving))
        for same in by_hash.values():
            if len(same) < 2:
                continue
            same.sort(key=lambda entry: (entry[1], len(entry[0].name), entry[0].stat().st_mtime))
            kept = same[0][0]
            duplicates += [Duplicate(path, kept, size) for path, _ in same[1:]]
    return duplicates


def build_plan(root: Path) -> Plan:
    """Work out where every file goes, without moving anything.

    Parameters
    ----------
    root : Path
        The folder to sort.

    Returns
    -------
    Plan
        The moves and duplicates a run would make.
    """
    files = collect_files(root)
    counts = Counter(_key(extension_folder(root, path.name)) for path in files)
    moves = []
    for path in files:
        scope = extension_folder(root, path.name)
        if scope.is_dir() or counts[_key(scope)] >= 2:
            folder = scope / arrival_month(path)
        else:
            scope = folder = category_folder(root, path.name)
        if _key(path.parent) != _key(folder):
            moves.append(Move(path, folder, scope))
    return Plan(root, moves, find_duplicates(root, moves))


def apply_plan(plan: Plan, trash: Callable[[str], None] | None = None,
               advance: Callable[[], None] = lambda: None) -> Result:
    """Move the files and recycle the duplicates.

    Parameters
    ----------
    plan : Plan
        The plan from ``build_plan``.
    trash : callable, optional
        Sends a folder to the Recycle Bin. Defaults to ``send2trash``.
    advance : callable, optional
        Called after each file, for progress display.

    Returns
    -------
    Result
        What was moved, recycled and skipped.
    """
    trash = trash or send2trash
    result = Result()
    bin_folder = None
    if plan.duplicates:
        # Collected in one folder and recycled at the end: a single Recycle Bin
        # operation, and everything can be restored in one go.
        bin_folder = plan.root / f"Duplicates {datetime.now():%Y-%m-%d %H%M%S}"
        bin_folder.mkdir()
        for duplicate in plan.duplicates:
            try:
                shutil.move(str(duplicate.path), str(unique_path(bin_folder, duplicate.path.name)))
                result.recycled.append(duplicate)
            except OSError as error:
                result.skipped.append((duplicate.path, error.strerror or str(error)))
            advance()

    for move in plan.to_move:
        try:
            move.folder.mkdir(parents=True, exist_ok=True)
            shutil.move(str(move.source), str(unique_path(move.folder, move.source.name)))
            result.moved.append(move)
        except OSError as error:
            result.skipped.append((move.source, error.strerror or str(error)))
        advance()

    if bin_folder is not None:
        trash(str(bin_folder))
    return result


def human_size(size: float) -> str:
    """File size for display, e.g. ``4.2 MB``."""
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return ""


def _plural(count: int, word: str) -> str:
    return word if count == 1 else f"{word}s"


def _relative(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def render_plan(plan: Plan) -> Group:
    """Preview of a plan: a tree of destination folders and a table of duplicates."""
    root = plan.root
    by_folder: dict[Path, list[Move]] = defaultdict(list)
    for move in plan.to_move:
        by_folder[move.folder].append(move)

    tree = Tree(Text(str(root), style="bold"), guide_style="grey42")
    categories: dict[str, Tree] = {}
    for folder in sorted(by_folder, key=lambda path: _relative(path, root).lower()):
        category, *rest = Path(_relative(folder, root)).parts
        if category not in categories:
            count = sum(len(moves) for path, moves in by_folder.items()
                        if Path(_relative(path, root)).parts[0] == category)
            label = Text.assemble((category, f"bold {CATEGORY_STYLES.get(category, 'white')}"),
                                  (f"  {count} {_plural(count, 'file')}", "grey62"))
            categories[category] = tree.add(label)
        moves = by_folder[folder]
        node = categories[category]
        if rest:
            node = node.add(Text.assemble((os.sep.join(rest), "bold"), (f"  {len(moves)}", "grey62")))
        for move in moves[:_FILES_SHOWN_PER_FOLDER]:
            line = Text(move.source.name)
            if _key(move.source.parent) != _key(root):
                line.append(f"  from {_relative(move.source.parent, root)}", style="grey50")
            node.add(line)
        if len(moves) > _FILES_SHOWN_PER_FOLDER:
            node.add(Text(f"... and {len(moves) - _FILES_SHOWN_PER_FOLDER} more", style="grey50 italic"))

    parts: list = [tree]
    if plan.duplicates:
        table = Table(box=box.SIMPLE_HEAD, header_style="bold", title_justify="left",
                      title=Text("Duplicates to the Recycle Bin", style="bold red"))
        table.add_column("Duplicate", overflow="fold")
        table.add_column("Size", justify="right", style="grey62")
        table.add_column("Identical to (kept)", overflow="fold", style="green")
        for duplicate in plan.duplicates:
            table.add_row(_relative(duplicate.path, root), human_size(duplicate.size),
                          _relative(duplicate.kept, root))
        parts += [Text(), table]

    freed = sum(duplicate.size for duplicate in plan.duplicates)
    summary = Text.assemble(("\n", ""), (str(len(plan.to_move)), "bold"), f" {_plural(len(plan.to_move), 'file')} to sort")
    if plan.duplicates:
        count = len(plan.duplicates)
        summary.append_text(Text.assemble("  ·  ", (str(count), "bold red"),
                                          f" {_plural(count, 'duplicate')} ({human_size(freed)}) to the Recycle Bin"))
    parts.append(summary)
    return Group(*parts)


def render_result(result: Result, root: Path) -> Group:
    """Summary of a finished run: files per folder, totals and anything skipped."""
    counts = Counter(_relative(move.scope, root) for move in result.moved)
    table = Table(box=box.SIMPLE_HEAD, header_style="bold")
    table.add_column("Folder")
    table.add_column("Files", justify="right")
    for folder, count in sorted(counts.items(), key=lambda item: item[0].lower()):
        table.add_row(folder, str(count))

    parts: list = [table] if counts else []
    parts.append(Text.assemble(("Files sorted: ", ""), (str(len(result.moved)), "bold green"),
                               ("   Duplicates sent to Recycle Bin: ", ""), (str(len(result.recycled)), "bold red")))
    for path, reason in result.skipped:
        parts.append(Text.assemble(("Skipped ", "yellow"), _relative(path, root), (f": {reason}", "grey62")))
    return Group(*parts)


def main(argv: list[str] | None = None, console: Console | None = None) -> int:
    """Command-line entry point.

    Parameters
    ----------
    argv : list of str, optional
        Arguments (defaults to ``sys.argv[1:]``).
    console : Console, optional
        Where to print (defaults to the terminal).

    Returns
    -------
    int
        Exit code: 0 on success, 1 if cancelled or a file was skipped, 2 on bad input.
    """
    parser = argparse.ArgumentParser(
        prog="sort-downloads",
        description="Sort loose files into Category/EXT/yyyy-MM folders and recycle exact duplicates.")
    parser.add_argument("path", nargs="?", type=Path, default=Path.home() / "Downloads",
                        help="folder to sort (default: your Downloads folder)")
    parser.add_argument("-n", "--dry-run", action="store_true", help="show the plan without moving anything")
    parser.add_argument("-y", "--yes", action="store_true", help="sort without asking for confirmation")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)
    console = console or Console()

    root = args.path.expanduser().resolve()
    if not root.is_dir():
        console.print(f"[red]Not a folder:[/] {root}")
        return 2

    with console.status("Scanning..."):
        plan = build_plan(root)
    if not plan.moves:
        console.print(f"[green]Nothing to sort.[/] {root} is already tidy.")
        return 0

    console.print(render_plan(plan))
    if args.dry_run:
        console.print("[grey62]Dry run: nothing was moved.[/]")
        return 0
    try:
        if not args.yes and not Confirm.ask("\nSort now?", default=True, console=console):
            console.print("Cancelled. Nothing was moved.")
            return 1
    except EOFError:
        console.print("Cancelled (no input). Run with --yes to sort without asking.")
        return 1

    total = len(plan.to_move) + len(plan.duplicates)
    with Progress(TextColumn("Sorting"), BarColumn(), MofNCompleteColumn(), console=console,
                  transient=True) as progress:
        task = progress.add_task("sort", total=total)
        result = apply_plan(plan, advance=lambda: progress.advance(task))
    console.print(render_result(result, root))
    return 1 if result.skipped else 0


if __name__ == "__main__":
    sys.exit(main())
