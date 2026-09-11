# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""/cli/downloads 는 **실제로 내려줄 수 있는** 패키지만 광고해야 한다.

배경(엔드포인트 스윕):
  * CLI_DIST_DIR(/app/cli-dist) 는 docker-compose 에서만 채워진다. admin-api 이미지는
    패키지를 담지 않고 EKS 에도 마운트가 없다. 그런데 list_downloads 는 파일 존재와
    무관하게 3개 항목을 그대로 광고했다(file_size_bytes=0, checksum_sha256="").
    결과: /cli 화면에 '0.0 MB' + 체크섬 빈칸 카드 3장이 뜨고, 다운로드 버튼은 전부 404.
  * 부수적으로 checksum 을 매 요청 `read_bytes()` 로 계산했다 — 패키지 전체를 메모리에
    올리는 일을 화면 열 때마다 3번.
"""
from __future__ import annotations

import gzip
import hashlib
import re
from pathlib import Path
from typing import AsyncGenerator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.main import register_exception_handlers
from app.routers import cli as cli_router


@pytest.fixture
async def client() -> AsyncGenerator[AsyncClient, None]:
    """cli 라우터만 올린 최소 앱 — 다운로드 엔드포인트는 DB/Redis 를 쓰지 않는다.

    에러 봉투는 프로덕션과 같아야 하므로 핸들러는 main 의 것을 공유한다
    (손으로 복제하면 두 쪽이 조용히 갈라진다).
    """
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(cli_router.router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
def dist_dir(tmp_path, monkeypatch):
    """빈 CLI_DIST_DIR 로 갈아끼운다(테스트가 실제 빌드 산출물에 의존하지 않게)."""
    monkeypatch.setattr(cli_router, "_CLI_DIST_DIR", tmp_path)
    cli_router._CHECKSUM_CACHE.clear()
    # 모듈 수준 상태는 테스트 사이에 남는다 — 결손 경고 기록도 같이 비운다.
    # (안 비우면 tmp_path 가 매번 달라 지금은 우연히 통과하지만, dist_dir 이 고정되는
    #  테스트가 하나라도 생기면 실행 순서에 따라 조용히 깨진다.)
    cli_router._MISSING_WARNED.clear()
    return tmp_path


def _write_package(dist_dir, os_name: str, arch: str, ext: str, payload: bytes) -> str:
    filename = f"gateway-cli-{cli_router._CLI_VERSION}-{os_name}-{arch}.{ext}"
    (dist_dir / filename).write_bytes(payload)
    return filename


@pytest.mark.asyncio
async def test_missing_packages_are_not_advertised(client: AsyncClient, dist_dir):
    """디렉터리가 비었으면 빈 목록 — 없는 걸 광고하면 UI 에 죽은 버튼이 생긴다."""
    res = await client.get("/cli/downloads")
    assert res.status_code == 200
    assert res.json() == []


@pytest.mark.asyncio
async def test_only_present_packages_are_advertised(client: AsyncClient, dist_dir):
    payload = gzip.compress(b"pretend tarball")
    filename = _write_package(dist_dir, "linux", "amd64", "tar.gz", payload)

    res = await client.get("/cli/downloads")
    assert res.status_code == 200
    items = res.json()
    assert len(items) == 1, f"존재하지 않는 패키지까지 광고됨: {items}"

    item = items[0]
    assert (item["os"], item["arch"]) == ("linux", "amd64")
    assert item["filename"] == filename
    assert item["file_size_bytes"] == len(payload)
    # 체크섬은 실제 파일 내용이어야 한다(빈 문자열은 UI 에 빈칸으로 노출됐다).
    assert item["checksum_sha256"] == hashlib.sha256(payload).hexdigest()
    assert item["version"] == cli_router._CLI_VERSION


@pytest.mark.asyncio
async def test_checksum_is_cached_by_mtime(client: AsyncClient, dist_dir):
    """같은 파일을 두 번 조회하면 해시를 다시 계산하지 않는다."""
    _write_package(dist_dir, "linux", "amd64", "tar.gz", b"x" * 4096)

    await client.get("/cli/downloads")
    assert len(cli_router._CHECKSUM_CACHE) == 1
    key = next(iter(cli_router._CHECKSUM_CACHE))

    # 캐시에 없는 값을 심어 두고 재조회 — 캐시를 쓰면 심은 값이 그대로 나온다.
    cli_router._CHECKSUM_CACHE[key] = "sentinel"
    res = await client.get("/cli/downloads")
    assert res.json()[0]["checksum_sha256"] == "sentinel"


@pytest.mark.asyncio
async def test_checksum_cache_invalidates_when_the_file_changes(client: AsyncClient, dist_dir):
    """파일이 바뀌면(크기/mtime) 캐시 키가 달라져 새로 계산해야 한다."""
    filename = _write_package(dist_dir, "linux", "amd64", "tar.gz", b"v1")
    first = (await client.get("/cli/downloads")).json()[0]["checksum_sha256"]

    (dist_dir / filename).write_bytes(b"v2 - different length")
    second = (await client.get("/cli/downloads")).json()[0]["checksum_sha256"]

    assert second != first
    assert second == hashlib.sha256(b"v2 - different length").hexdigest()


@pytest.mark.asyncio
async def test_advertised_url_is_downloadable(client: AsyncClient, dist_dir):
    """광고한 download_url 은 반드시 200 이어야 한다(광고 ↔ 실제 일치)."""
    payload = gzip.compress(b"pretend tarball")
    _write_package(dist_dir, "darwin", "arm64", "tar.gz", payload)

    items = (await client.get("/cli/downloads")).json()
    assert items, "광고 목록이 비어 다운로드 검증이 공허해진다"

    for item in items:
        res = await client.get(item["download_url"])
        assert res.status_code == 200, f"{item['download_url']} → {res.status_code}"
        assert res.content == payload
        assert item["filename"] in res.headers.get("content-disposition", "")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "os_name,arch",
    [
        ("plan9", "amd64"),      # 존재하지 않는 OS
        ("linux", "riscv64"),    # 존재하지 않는 아키텍처
        ("..", "amd64"),         # 파일명에 섞여 들어가는 경로 토큰
    ],
)
async def test_unsupported_targets_are_404(client: AsyncClient, dist_dir, os_name, arch):
    """타깃 표에 없는 조합은 404 — URL 값이 파일명 조립에 그대로 들어가지 않는다.

    해당 이름의 파일을 **실제로 심어 두고** 확인한다. 그러지 않으면 '파일이 없어서 404' 와
    '조합이 허용되지 않아 404' 를 구분하지 못해 테스트가 공허해진다(음성대조군으로 확인).
    """
    _write_package(dist_dir, os_name, arch, "tar.gz", b"should never be served")
    res = await client.get(f"/cli/download/{os_name}/{arch}")
    assert res.status_code == 404, f"{os_name}/{arch} → {res.status_code}"


@pytest.mark.asyncio
async def test_all_targets_package_sh_builds_are_serveable(client: AsyncClient, dist_dir):
    """package.sh 가 만드는 5종은 전부 목록·다운로드가 가능해야 한다.

    예전 타깃 표는 3종뿐이라 linux/arm64·darwin/amd64 패키지는 디스크에 있어도
    화면에 나오지 않았다(빌드는 되는데 배포 경로가 없는 상태).
    """
    for os_name, arch, ext in cli_router._CLI_TARGETS:
        _write_package(dist_dir, os_name, arch, ext, f"{os_name}-{arch}".encode())

    items = (await client.get("/cli/downloads")).json()
    assert len(items) == len(cli_router._CLI_TARGETS) == 5

    for item in items:
        res = await client.get(item["download_url"])
        assert res.status_code == 200, f"{item['download_url']} → {res.status_code}"
        assert res.content == f"{item['os']}-{item['arch']}".encode()


def test_cli_version_matches_the_builder():
    """_CLI_VERSION 은 gateway-cli 빌더의 VERSION 과 같아야 한다.

    ⚠️ 이 파일의 다른 테스트는 모두 `_CLI_VERSION` 으로 파일을 만들고 `_CLI_VERSION` 으로
       찾으므로 **자기참조**다 — 버전이 드리프트해도 전부 통과한다. 실제로는 package.sh 가
       gateway-cli-<VERSION>-... 로 만들고 admin-api 가 다른 버전으로 찾으면 5종 전부
       '없는 파일' 이 되어 화면이 조용히 빈 상태가 된다. 그 드리프트만 이 테스트가 잡는다.
    """
    repo_root = Path(__file__).resolve().parents[3]

    package_sh = (repo_root / "gateway-cli" / "package.sh").read_text(encoding="utf-8")
    sh_version = re.search(r'^VERSION="([^"]+)"', package_sh, re.MULTILINE)
    assert sh_version, "package.sh 의 VERSION 정의를 찾지 못했다(형식이 바뀌면 이 가드가 공허해진다)"

    pyproject = (repo_root / "gateway-cli" / "pyproject.toml").read_text(encoding="utf-8")
    py_version = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.MULTILINE)
    assert py_version, "gateway-cli/pyproject.toml 의 version 정의를 찾지 못했다"

    assert cli_router._CLI_VERSION == sh_version.group(1), (
        f"admin-api 는 {cli_router._CLI_VERSION} 을 찾는데 package.sh 는 "
        f"{sh_version.group(1)} 로 만든다 — 모든 패키지가 목록에서 사라진다"
    )
    assert sh_version.group(1) == py_version.group(1), (
        "package.sh VERSION 과 gateway-cli/pyproject.toml version 이 어긋난다"
    )


@pytest.mark.asyncio
async def test_one_unreadable_package_does_not_lose_the_others(
    client: AsyncClient, dist_dir, monkeypatch
):
    """패키지 하나가 읽히지 않아도 나머지는 그대로 광고된다(목록 전체가 500 이 되지 않는다)."""
    for os_name, arch, ext in cli_router._CLI_TARGETS:
        _write_package(dist_dir, os_name, arch, ext, f"{os_name}-{arch}".encode())

    broken = cli_router._package_filename("linux", "arm64", "tar.gz")
    real_sha256 = cli_router._file_sha256

    def flaky(filepath: Path, size: int, mtime_ns: int) -> str:
        if filepath.name == broken:
            raise PermissionError(f"cannot read {filepath}")
        return real_sha256(filepath, size, mtime_ns)

    monkeypatch.setattr(cli_router, "_file_sha256", flaky)

    res = await client.get("/cli/downloads")
    assert res.status_code == 200, f"항목 하나의 I/O 실패로 목록 전체가 죽었다: {res.status_code}"
    items = res.json()
    assert len(items) == len(cli_router._CLI_TARGETS) - 1
    assert broken not in [i["filename"] for i in items]


@pytest.mark.asyncio
async def test_missing_warning_is_not_repeated_for_the_same_state(client: AsyncClient, dist_dir):
    """같은 결손 상태를 매 요청 경고하지 않는다(미인증 엔드포인트라 로그 증폭이 된다)."""
    cli_router._MISSING_WARNED.clear()

    await client.get("/cli/downloads")
    first = set(cli_router._MISSING_WARNED)
    assert len(first) == 1, "결손 경고가 기록되지 않았다 — 운영자에게 알릴 신호를 잃는다"

    await client.get("/cli/downloads")
    assert set(cli_router._MISSING_WARNED) == first, "같은 상태인데 경고 서명이 늘었다"

    # 상태가 바뀌면(패키지 하나가 생기면) 다시 알린다.
    _write_package(dist_dir, "linux", "amd64", "tar.gz", b"new")
    await client.get("/cli/downloads")
    assert len(cli_router._MISSING_WARNED) == 2


@pytest.mark.asyncio
async def test_windows_package_is_served_as_zip(client: AsyncClient, dist_dir):
    """확장자/미디어타입은 타깃 표에서 나온다 — windows 만 zip."""
    _write_package(dist_dir, "windows", "amd64", "zip", b"PK\x03\x04pretend")

    items = (await client.get("/cli/downloads")).json()
    assert items[0]["filename"].endswith(".zip")

    res = await client.get("/cli/download/windows/amd64")
    assert res.status_code == 200
    assert res.headers["content-type"] == "application/zip"
