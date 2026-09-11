# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Config file backup utilities (BR-BACKUP-01~04)."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from platformdirs import user_config_dir


@dataclass
class BackupEntry:
    """Record of a config file backup."""

    original_path: str
    backup_path: str
    created_at: datetime
    tool_name: str


def _get_backup_dir() -> Path:
    """Return the backup directory path (BR-BACKUP-01). Does not touch the filesystem."""
    return Path(user_config_dir("gateway-cli")) / "backups"


def _ensure_backup_dir(backup_dir: Path) -> None:
    """Create the backup directory with mode 700 if needed (BR-BACKUP-01).

    디렉터리 생성을 경로 조회(`_get_backup_dir`)에서 분리한 이유:
    조회 함수가 부작용까지 갖고 있으면 그 함수를 대체(patch)하는 순간 mkdir 도
    함께 사라져 `shutil.copy2` 가 FileNotFoundError 로 죽는다. 실제로 백업
    테스트 5개가 그 이유로 깨져 있었다. 조회는 순수하게, 생성은 쓰기 직전에
    한 번 — 이렇게 두면 호출마다 멱등하게 확인하므로 프로세스가 시작된 뒤
    누군가 백업 디렉터리를 지운 경우에도 다음 백업이 살아난다.
    """
    if backup_dir.exists():
        return
    backup_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(str(backup_dir), 0o700)
    except OSError:
        pass  # Windows may not fully support chmod


def backup_config(tool_name: str, original_path: str | Path) -> BackupEntry | None:
    """Back up a config file before modification (BR-BACKUP-03).

    Returns None if original file doesn't exist (skip backup).
    Naming: {tool_name}.{original_filename}.{YYYYMMDDTHHMMSS}.bak (BR-BACKUP-02)
    """
    original_path = Path(original_path)
    if not original_path.exists():
        return None

    backup_dir = _get_backup_dir()
    _ensure_backup_dir(backup_dir)
    now = datetime.now(timezone.utc)
    timestamp = now.strftime("%Y%m%dT%H%M%S")
    backup_name = f"{tool_name}.{original_path.name}.{timestamp}.bak"
    backup_path = backup_dir / backup_name

    # Copy with permission preservation (BR-BACKUP-04)
    shutil.copy2(str(original_path), str(backup_path))

    return BackupEntry(
        original_path=str(original_path),
        backup_path=str(backup_path),
        created_at=now,
        tool_name=tool_name,
    )
