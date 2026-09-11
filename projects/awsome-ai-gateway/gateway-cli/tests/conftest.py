# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""테스트 전역 격리 — 실행하는 머신의 실제 홈 디렉터리를 절대 읽지 않게 한다.

이 CLI 는 하는 일이 대부분 "사용자 홈의 설정 파일을 찾아서 고치는 것"이라
격리가 없으면 테스트 결과가 개발자 머신 상태에 따라 달라진다. 실제로 그랬다:
`cli.tools.detector._PATH_RESOLVERS` 가 import 시점에 함수 객체를 붙잡고 있어서
`patch("cli.tools.detector._claude_code_paths", ...)` 가 no-op 이 되고,
detect_tools 가 개발자의 진짜 `~/.claude/settings.json` 을 읽었다. 그 결과
4개 테스트가 로컬에서만 실패하고 1개는 그 파일이 존재한다는 이유로 '우연히'
통과했다 — CI 의 깨끗한 박스에서는 정반대로 보인다.

`_PATH_RESOLVERS` 쪽 원인은 고쳤지만, 그 seam 이 다시 깨지더라도 테스트가
실 사용자 파일을 건드리지 못하도록(그리고 읽어서 통과/실패가 갈리지 않도록)
여기서 홈 계열 환경변수를 tmp 로 돌린다. autouse 이므로 전 테스트에 적용된다.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

# `~` 확장과 설정 디렉터리 탐색에 쓰이는 변수들. platformdirs 도 이들을 본다.
_HOME_VARS = (
    "HOME",  # POSIX
    "USERPROFILE",  # Windows
    "APPDATA",  # Windows roaming
    "LOCALAPPDATA",  # Windows local
    "XDG_CONFIG_HOME",  # freedesktop
    "XDG_DATA_HOME",
    "XDG_CACHE_HOME",
)


@pytest.fixture(autouse=True)
def isolated_home(tmp_path_factory, monkeypatch) -> Path:
    """모든 테스트를 가짜 홈 디렉터리 안에서 실행한다."""
    home = tmp_path_factory.mktemp("home")
    for var in _HOME_VARS:
        monkeypatch.setenv(var, str(home))
    # os.path.expanduser 는 POSIX 에서 HOME 을, Windows 에서 USERPROFILE 을 본다.
    # 둘 다 세팅했으므로 아래 단정은 두 플랫폼에서 모두 성립한다.
    assert os.path.expanduser("~") == str(home)
    return home
