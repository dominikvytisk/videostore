"""Path sanitization for archive members.

The decoder treats every video it opens as untrusted input (see docs/security.md).
A malicious or corrupted archive manifest must never be able to write outside the
extraction directory, so every stored path is normalized and re-validated both when
an archive is built and again when it is extracted.
"""
from __future__ import annotations

import os
import posixpath

MAX_PATH_LENGTH = 4096


class UnsafePathError(ValueError):
    pass


def to_archive_path(host_path: str, root: str) -> str:
    """Convert a filesystem path (relative to `root`) into a normalized posix
    archive member path, e.g. "sub/dir/file.bin". Rejects escapes from root."""
    rel = os.path.relpath(host_path, root)
    rel = rel.replace(os.sep, "/")
    if os.altsep:
        rel = rel.replace(os.altsep, "/")
    return sanitize_archive_path(rel)


def sanitize_archive_path(archive_path: str) -> str:
    """Validate/normalize a path as it will be stored in (or read from) the archive
    manifest. Raises UnsafePathError on anything that could escape the extraction
    directory or otherwise misbehave."""
    if not archive_path or len(archive_path) > MAX_PATH_LENGTH:
        raise UnsafePathError(f"invalid path length: {archive_path!r}")
    if "\x00" in archive_path:
        raise UnsafePathError("NUL byte in path")
    # Reject absolute paths and drive letters outright.
    if archive_path.startswith("/") or archive_path.startswith("\\"):
        raise UnsafePathError(f"absolute path not allowed: {archive_path!r}")
    if len(archive_path) >= 2 and archive_path[1] == ":":
        raise UnsafePathError(f"drive-letter path not allowed: {archive_path!r}")

    normalized = posixpath.normpath(archive_path)
    if normalized in (".", ""):
        raise UnsafePathError(f"empty path after normalization: {archive_path!r}")
    parts = normalized.split("/")
    if any(p == ".." for p in parts):
        raise UnsafePathError(f"path escapes root: {archive_path!r}")
    if any(p == "" for p in parts):
        raise UnsafePathError(f"empty path segment: {archive_path!r}")
    return normalized


def safe_extract_path(output_dir: str, archive_path: str) -> str:
    """Resolve the on-disk extraction path for an archive member, guaranteeing the
    result is contained within output_dir even if archive_path is adversarial."""
    clean = sanitize_archive_path(archive_path)
    output_dir_abs = os.path.abspath(output_dir)
    candidate = os.path.abspath(os.path.join(output_dir_abs, clean))
    if candidate != output_dir_abs and not candidate.startswith(output_dir_abs + os.sep):
        raise UnsafePathError(f"resolved path escapes output dir: {archive_path!r}")
    return candidate
