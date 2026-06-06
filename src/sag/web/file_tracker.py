"""Workspace file change snapshots for SAG Workbench."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from sag.web.models import (
    FileChangeCounts,
    FileChangeDigest,
    FileChangeItem,
    FileSnapshotRef,
)

DEFAULT_IGNORE_DIRS = {
    ".git",
    ".venv",
    "__pycache__",
    "node_modules",
    "target",
    "build",
    "dist",
}


@dataclass(frozen=True)
class FileMeta:
    path: str
    type: Literal["file", "dir", "other"]
    size: int
    mtime_ns: int


@dataclass(frozen=True)
class FileSnapshot:
    id: str
    root: Path
    mode: str
    files: dict[str, FileMeta]


class FileChangeTracker:
    def __init__(self, root: Path, ignore_dirs: set[str] | None = None):
        self.root = root
        self.ignore_dirs = DEFAULT_IGNORE_DIRS if ignore_dirs is None else ignore_dirs

    def snapshot(self, snapshot_id: str) -> FileSnapshot:
        files: dict[str, FileMeta] = {}
        for dirpath, dirnames, filenames in os.walk(self.root):
            dir_path = Path(dirpath)
            kept_dirnames: list[str] = []

            for dirname in sorted(dirnames):
                path = dir_path / dirname
                if self._ignored(path):
                    continue
                kept_dirnames.append(dirname)
                self._add_path(files, path)

            dirnames[:] = kept_dirnames

            for filename in sorted(filenames):
                path = dir_path / filename
                if self._ignored(path):
                    continue
                self._add_path(files, path)

        return FileSnapshot(id=snapshot_id, root=self.root, mode="metadata", files=files)

    def diff(self, base: FileSnapshot, head: FileSnapshot) -> FileChangeDigest:
        items: list[FileChangeItem] = []
        base_paths = set(base.files)
        head_paths = set(head.files)

        for rel in sorted(head_paths - base_paths):
            items.append(self._item(head.files[rel], "added"))
        for rel in sorted(base_paths - head_paths):
            items.append(self._item(base.files[rel], "deleted"))
        for rel in sorted(base_paths & head_paths):
            before = base.files[rel]
            after = head.files[rel]
            if (
                before.size != after.size
                or before.mtime_ns != after.mtime_ns
                or before.type != after.type
            ):
                items.append(self._item(after, "modified"))

        counts = FileChangeCounts(
            added=sum(1 for item in items if item.change == "added"),
            modified=sum(1 for item in items if item.change == "modified"),
            deleted=sum(1 for item in items if item.change == "deleted"),
            renamed=0,
        )

        return FileChangeDigest(
            snapshot=FileSnapshotRef(base=base.id, head=head.id, mode=head.mode),
            counts=counts,
            items=items,
        )

    def _ignored(self, path: Path) -> bool:
        rel_parts = path.relative_to(self.root).parts
        return any(part in self.ignore_dirs for part in rel_parts)

    def _add_path(self, files: dict[str, FileMeta], path: Path) -> None:
        rel = path.relative_to(self.root).as_posix()
        try:
            path_stat = path.lstat()
        except OSError:
            return

        mode = path_stat.st_mode
        kind: Literal["file", "dir", "other"]
        if stat.S_ISDIR(mode):
            kind = "dir"
        elif stat.S_ISREG(mode):
            kind = "file"
        else:
            kind = "other"

        files[rel] = FileMeta(
            path=rel,
            type=kind,
            size=path_stat.st_size,
            mtime_ns=path_stat.st_mtime_ns,
        )

    def _item(
        self,
        meta: FileMeta,
        change: Literal["added", "modified", "deleted", "renamed"],
    ) -> FileChangeItem:
        return FileChangeItem(
            path=meta.path,
            change=change,
            type=meta.type,
            size=_format_size(meta.size),
            mtime=str(meta.mtime_ns),
        )


def _format_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"
