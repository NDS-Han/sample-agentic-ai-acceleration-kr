# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import Depends

from app.core.db import get_db_session
from app.schemas.cli import (
    SetupRequest,
    SetupResponse,
    VirtualKeyIssueRequest,
    VirtualKeyIssueResponse,
)

logger = structlog.get_logger()

router = APIRouter(prefix="/cli", tags=["CLI Integration"])

# CLI dist directory (relative to project root)
_CLI_DIST_DIR = Path(os.environ.get("CLI_DIST_DIR", "/app/cli-dist"))

# gateway-cli/package.sh 의 VERSION 과 같아야 한다 — 파일명이 여기서 만들어진다.
_CLI_VERSION = "0.1.0"

# 배포되는 (os, arch, 확장자) 조합 — gateway-cli/package.sh 가 실제로 만드는 5종과 1:1.
# ⚠️ 예전 목록은 3종(linux/amd64, darwin/arm64, windows/amd64)뿐이라, package.sh 가
#    만들어 둔 linux/arm64·darwin/amd64 패키지는 디스크에 있어도 목록에 안 나왔다.
#    없는 조합을 광고하지 않는 것과, 있는 조합을 숨기는 것은 다른 문제다.
_CLI_TARGETS: tuple[tuple[str, str, str], ...] = (
    ("linux", "amd64", "tar.gz"),
    ("linux", "arm64", "tar.gz"),
    ("darwin", "amd64", "tar.gz"),
    ("darwin", "arm64", "tar.gz"),
    ("windows", "amd64", "zip"),
)

# sha256 캐시 — 키는 (경로, mtime_ns, 크기). /cli/downloads 는 관리 UI 가 페이지를 열 때마다
# 호출하는데, 예전엔 매번 패키지 **전체**를 read_bytes() 로 메모리에 올려 해시했다
# (패키지 3개 × 수 MB~수십 MB 를 요청마다). 이제는 스트리밍 해시 + mtime 기반 캐시.
_CHECKSUM_CACHE: dict[tuple[str, int, int], str] = {}

# 이미 경고한 (dist_dir, 결손 파일 목록) 조합. 같은 상태를 매 요청 반복해서 찍지 않는다.
_MISSING_WARNED: set[tuple[str, tuple[str, ...]]] = set()


def _package_filename(os_name: str, arch: str, ext: str) -> str:
    return f"gateway-cli-{_CLI_VERSION}-{os_name}-{arch}.{ext}"


def _file_sha256(filepath: Path, size: int, mtime_ns: int) -> str:
    key = (str(filepath), mtime_ns, size)
    cached = _CHECKSUM_CACHE.get(key)
    if cached is not None:
        return cached
    digest = hashlib.sha256()
    with filepath.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    checksum = digest.hexdigest()
    _CHECKSUM_CACHE[key] = checksum
    return checksum


@router.post("/auth/virtual-key", response_model=VirtualKeyIssueResponse)
async def issue_virtual_key(
    request: Request,
    body: VirtualKeyIssueRequest,
    session: AsyncSession = Depends(get_db_session),
):
    """Issue a Virtual Key via STS Pre-signed GetCallerIdentity verification.

    No JWT auth required — CLI authenticates via AWS IAM credentials.
    """
    from app.services.cli_service import CLIService

    svc: CLIService = request.app.state.cli_service
    redis = request.app.state.redis
    return await svc.verify_sts_and_issue_key(
        session,
        redis=redis,
        data=body,
        ip_address=request.client.host if request.client else "0.0.0.0",
        request_id=request.headers.get("x-request-id", ""),
    )


@router.post("/setup", response_model=SetupResponse)
async def get_setup_config(
    request: Request,
    body: SetupRequest,
):
    """Return tool-specific configuration for CLI onboarding."""
    svc: CLIService = request.app.state.cli_service
    return svc.get_setup_config(body)


@router.get("/downloads")
async def list_downloads(request: Request):
    """실제로 내려줄 수 있는 CLI 패키지만 반환한다.

    ⚠️ 예전엔 파일이 없어도 3개 항목을 그대로 광고했다(file_size_bytes=0,
       checksum_sha256=""). CLI_DIST_DIR 은 docker-compose 에서만 채워지고
       EKS 이미지에는 아예 없어서, /cli 화면에 '0.0 MB' 카드 3장과 누르면 404 가
       나는 다운로드 버튼이 떴다. 없는 건 광고하지 않고, 대신 운영자가 볼 수 있게
       경고를 남긴다(패키지를 빌드해 마운트하라는 신호).
    """
    items = []
    missing = []
    for os_name, arch, ext in _CLI_TARGETS:
        filename = _package_filename(os_name, arch, ext)
        filepath = _CLI_DIST_DIR / filename
        # ⚠️ 패키지 하나의 I/O 실패가 목록 전체를 죽이지 않게 항목 단위로 격리한다.
        #    is_file() 통과 후 stat()/열기 사이에 파일이 교체·삭제될 수 있고(롤링 마운트,
        #    재빌드), 권한 문제도 있다. 예전엔 그 예외가 그대로 올라가 5종 전부를 잃고
        #    500 이 됐다 — 나머지 4개는 정상인데도 화면이 통째로 비었다.
        try:
            if not filepath.is_file():
                missing.append(filename)
                continue
            stat = filepath.stat()
            checksum = _file_sha256(filepath, stat.st_size, stat.st_mtime_ns)
        except OSError as exc:
            logger.warning(
                "cli.downloads.package_unreadable",
                filename=filename,
                dist_dir=str(_CLI_DIST_DIR),
                error=str(exc),
            )
            missing.append(filename)
            continue
        items.append({
            "os": os_name,
            "arch": arch,
            "filename": filename,
            "download_url": f"/cli/download/{os_name}/{arch}",
            "version": _CLI_VERSION,
            "file_size_bytes": stat.st_size,
            "checksum_sha256": checksum,
        })

    # 같은 결손 조합을 한 번만 알린다. /cli 는 미인증으로 열려 있고 패키지가 마운트되지
    # 않은 환경에서는 **모든** 요청이 결손이라, 매번 찍으면 운영자에게 신호가 아니라
    # 잡음이 되고(로그 비용도) 누가 열어보든 증폭시킬 수 있다. 조합이 바뀌면 다시 알린다.
    if missing:
        signature = (str(_CLI_DIST_DIR), tuple(missing))
        if signature not in _MISSING_WARNED:
            _MISSING_WARNED.add(signature)
            logger.warning(
                "cli.downloads.packages_missing",
                dist_dir=str(_CLI_DIST_DIR),
                missing=missing,
                hint="gateway-cli/package.sh 로 빌드해 CLI_DIST_DIR 에 마운트하세요.",
            )
    return items


@router.get("/download/{os_name}/{arch}")
async def download_binary(os_name: str, arch: str):
    """Download the gateway-cli package for the given OS/arch."""
    # 광고한 조합만 서빙한다(allowlist). os_name/arch 는 URL 에서 오는 값이고 파일명 조립에
    # 그대로 들어가므로, 조합을 고정해 두면 경로 조작 여지가 원천적으로 없다.
    target = next(
        (t for t in _CLI_TARGETS if t[0] == os_name and t[1] == arch),
        None,
    )
    if target is None:
        from fastapi import HTTPException
        raise HTTPException(
            status_code=404, detail=f"Unsupported target: {os_name}/{arch}"
        )
    ext = target[2]
    media = "application/zip" if ext == "zip" else "application/gzip"
    filename = _package_filename(os_name, arch, ext)
    filepath = _CLI_DIST_DIR / filename

    if not filepath.is_file():
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail=f"Package not found: {filename}")

    return FileResponse(
        path=str(filepath),
        filename=filename,
        media_type=media,
    )
