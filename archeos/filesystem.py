"""Minimal fail-closed filesystem primitives shared by local adapters."""

from __future__ import annotations

import os
import sys
from ctypes import CDLL, c_char_p, c_int, get_errno
from errno import EEXIST, ENOTEMPTY
from pathlib import Path


AT_FDCWD_DARWIN = -2
AT_FDCWD_LINUX = -100
RENAME_EXCL_DARWIN = 0x00000004
RENAME_NOREPLACE_LINUX = 1


def publish_directory_no_replace(staging_path: Path, final_path: Path) -> None:
    """Atomically publish a directory only when no target entry exists."""

    libc = CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        rename_no_replace = libc.renameatx_np
        directory_fd = AT_FDCWD_DARWIN
        flag = RENAME_EXCL_DARWIN
    elif sys.platform == "linux" and hasattr(libc, "renameat2"):
        rename_no_replace = libc.renameat2
        directory_fd = AT_FDCWD_LINUX
        flag = RENAME_NOREPLACE_LINUX
    else:
        raise OSError("atomic no-replace directory publication is unavailable")
    rename_no_replace.argtypes = (c_int, c_char_p, c_int, c_char_p, c_int)
    rename_no_replace.restype = c_int
    result = rename_no_replace(
        directory_fd,
        os.fsencode(staging_path),
        directory_fd,
        os.fsencode(final_path),
        flag,
    )
    if result == 0:
        return
    error_number = get_errno()
    if error_number in {EEXIST, ENOTEMPTY}:
        raise FileExistsError(error_number, "target entry already exists", final_path)
    raise OSError(error_number, "atomic no-replace directory publication failed", final_path)


def publish_file_no_replace(staging_path: Path, final_path: Path) -> None:
    """Atomically publish a regular file without ever replacing ``final_path``."""

    os.link(staging_path, final_path, follow_symlinks=False)
    staging_path.unlink()
