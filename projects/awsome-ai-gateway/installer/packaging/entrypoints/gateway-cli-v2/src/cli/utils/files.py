# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Atomic file writes — write-temp-then-rename for settings files.

A ``Path.write_text`` truncates the target first, so a process kill or disk-full
mid-write leaves a truncated ``settings.json``/``managed-settings.json`` behind.
For settings files that is NOT self-healing: the next launch reads corrupt JSON
and either fails or silently loses the whole config (including keys we don't
own). Writing a sibling temp file and ``os.replace``-ing it over the target is
atomic on every supported filesystem (same-dir rename), so the file is either
the old version or the new one — never a torn write.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def atomic_write_text(path: Path, content: str, encoding: str = "utf-8") -> None:
    """Write ``content`` to ``path`` atomically (temp file + rename in same dir).

    The temp file is created in the target's own directory so the rename stays
    on one filesystem. The mode of a pre-existing target is preserved by the
    replace; a fresh file gets the tempfile default (0600 — fine for the files
    this suite writes, which either hold user config or get chmodded by the
    caller).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding=encoding) as fh:
            fh.write(content)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
