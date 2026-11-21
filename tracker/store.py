"""Content-addressed blob store plus a 'current' view of the portal tree.

objects/<sha[:2]>/<sha>  every version ever seen, deduplicated, read-only
current/<path>           hardlink (or copy) of the current version of each path
tmp/                     downloads in progress, same filesystem so renames are atomic
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
from pathlib import Path, PurePosixPath

SHA_RE = re.compile(r"^[0-9a-f]{64}$")


class UnsafePath(ValueError):
    pass


def check_path(path: str) -> str:
    """Reject remote paths that could escape the data directory or are not plain relative paths."""
    if not path or "\x00" in path or "\\" in path or path.startswith("/"):
        raise UnsafePath(f"unsafe path: {path!r}")
    parts = PurePosixPath(path).parts
    if any(p in ("", ".", "..") for p in parts) or path != "/".join(parts):
        raise UnsafePath(f"unsafe path: {path!r}")
    return path


def _fsync_dir(directory: Path) -> None:
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


class BlobStore:
    def __init__(self, data_dir: Path):
        self.objects = data_dir / "objects"
        self.current = data_dir / "current"
        self.tmp = data_dir / "tmp"
        for d in (self.objects, self.current, self.tmp):
            d.mkdir(parents=True, exist_ok=True)
        self._current_root = self.current.resolve()

    def object_path(self, sha: str) -> Path:
        if not SHA_RE.match(sha):
            raise ValueError(f"not a sha256: {sha!r}")
        return self.objects / sha[:2] / sha

    def has(self, sha: str, size: int | None = None) -> bool:
        """True if the blob exists (and, when given, has the expected size, so a blob truncated by a
        crash is never trusted)."""
        try:
            st = self.object_path(sha).stat()
        except FileNotFoundError:
            return False
        return size is None or st.st_size == size

    def read(self, sha: str) -> bytes:
        return self.object_path(sha).read_bytes()

    def put_file(self, tmp_path: Path, sha: str, size: int | None = None) -> Path:
        """Move a verified temp file into the store (or drop it if that blob already exists intact)."""
        target = self.object_path(sha)
        if self.has(sha, size):
            tmp_path.unlink(missing_ok=True)
            return target
        target.parent.mkdir(exist_ok=True)
        os.chmod(tmp_path, 0o444)
        os.replace(tmp_path, target)
        _fsync_dir(target.parent)
        return target

    def import_file(self, source: Path) -> tuple[str, int]:
        """Hash and copy a local file into the store. Used for baseline and history imports."""
        digest = hashlib.sha256()
        size = 0
        with tempfile.NamedTemporaryFile(dir=self.tmp, delete=False) as out, source.open("rb") as src:
            while chunk := src.read(1 << 16):
                digest.update(chunk)
                out.write(chunk)
                size += len(chunk)
            out.flush()
            os.fsync(out.fileno())
        sha = digest.hexdigest()
        self.put_file(Path(out.name), sha, size)
        return sha, size

    def _current_path(self, path: str) -> Path:
        check_path(path)
        target = (self.current / path).resolve()
        if not target.is_relative_to(self._current_root):
            raise UnsafePath(f"path escapes the data directory: {path!r}")
        return target

    def is_current(self, path: str, sha: str) -> bool:
        """Cheap check (stat only) that current/<path> points at the given blob."""
        try:
            cur = self._current_path(path).stat()
            obj = self.object_path(sha).stat()
        except (OSError, ValueError):
            return False
        if cur.st_ino == obj.st_ino and cur.st_dev == obj.st_dev:
            return True
        return cur.st_size == obj.st_size and cur.st_nlink == 1

    def set_current(self, path: str, sha: str) -> None:
        target = self._current_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = target.parent / f".{target.name}.{os.getpid()}.tmp"
        staging.unlink(missing_ok=True)
        try:
            os.link(self.object_path(sha), staging)
        except OSError:
            shutil.copyfile(self.object_path(sha), staging)
        os.replace(staging, target)

    def remove_current(self, path: str) -> None:
        target = self._current_path(path)
        target.unlink(missing_ok=True)
        parent = target.parent
        while parent != self._current_root and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent

    def current_files(self) -> list[str]:
        """Relative paths of every file in the current view (ignores in-progress staging files)."""
        return [
            p.relative_to(self.current).as_posix()
            for p in self.current.rglob("*")
            if p.is_file() and not (p.name.startswith(".") and p.name.endswith(".tmp"))
        ]
