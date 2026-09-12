# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""admin-api 와 admin-ui 가 **같은 IdP 그룹 설정**을 받는지 렌더로 확인한다.

왜 렌더 테스트인가 — 이 계약은 코드에서 보이지 않는다. admin-api 와 admin-ui 는 각각
자기 env 를 읽을 뿐이고, 두 값이 같은지는 **차트가 어떻게 주입하는가**에만 달려 있다.
그래서 파이썬/타입스크립트 단위 테스트로는 원리적으로 잡을 수 없다.

어긋나면 무슨 일이 나나: admin-api 는 그룹을 보고 ADMIN 을 인가하는데 admin-ui 는
역할을 정하지 못해 ``checkPagePermission`` 이 모든 페이지를 ``/403`` 으로 보낸다 —
"API 는 되는데 화면은 전부 막힌" 관리자 잠김이다. 예전엔 admin-ui 가 그룹 이름을 소스에
하드코딩하고 있어서, admin-api 쪽 값만 바꾸면 그대로 재현됐다.

⚠️ 이 파일은 ``helm`` 바이너리를 쓴다. 없으면 skip 하되, CI 에서는 **실패**시킨다 —
   조용히 건너뛰면 이 계약이 아무 데서도 검증되지 않는다.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml", reason="pyyaml 이 필요하다(렌더 결과 파싱)")

CHART = Path(__file__).resolve().parents[1]
PARITY_KEYS = ("OIDC_GROUPS_CLAIM", "ADMIN_GROUPS")


def _helm() -> str:
    exe = shutil.which("helm")
    if exe is None:
        if os.environ.get("CI"):
            raise AssertionError(
                "CI 인데 helm 이 없다 — 렌더 테스트가 조용히 skip 되면 "
                "admin-api↔admin-ui 그룹 설정 정합성이 아무 데서도 검증되지 않는다."
            )
        pytest.skip("helm 바이너리가 없다")
    return exe


def _render(*set_args: str) -> list[dict]:
    cmd = [_helm(), "template", "parity-test", str(CHART)]
    for a in set_args:
        cmd += ["--set", a]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    assert proc.returncode == 0, f"helm template 실패:\n{proc.stderr[-2000:]}"
    docs = [d for d in yaml.safe_load_all(proc.stdout) if d]
    # 대조군 — 렌더가 실제로 뭔가 만들었는가. 빈 결과를 "정합" 으로 오판하지 않는다.
    assert len(docs) > 5, f"렌더 결과가 {len(docs)}개뿐이다 — 차트 경로/값을 확인"
    return docs


def _env_of(docs: list[dict], name_fragment: str) -> dict[str, str | None]:
    for d in docs:
        if d.get("kind") != "Deployment":
            continue
        if name_fragment not in d["metadata"]["name"]:
            continue
        containers = d["spec"]["template"]["spec"]["containers"]
        assert len(containers) >= 1
        # ⚠️ 같은 이름의 env 가 여러 번 오면 쿠버네티스는 **마지막**을 쓴다. 그래서 dict
        #    빌드 순서도 그대로 두어야 실제 동작과 같은 값을 본다(운영자 override 가 이김).
        return {e["name"]: e.get("value") for e in containers[0].get("env", [])}
    raise AssertionError(f"{name_fragment} Deployment 를 렌더 결과에서 찾지 못했다")


OIDC_ON = (
    "adminApi.oidc.enabled=true",
    "adminApi.oidc.groupsClaim=cognito:groups",
    "adminApi.adminBootstrap.groups={GatewayAdmin,Ops}",
)


def test_both_containers_get_the_same_group_settings():
    docs = _render(*OIDC_ON)
    api = _env_of(docs, "admin-api")
    ui = _env_of(docs, "admin-ui")

    for key in PARITY_KEYS:
        assert key in api, f"admin-api 에 {key} 가 없다"
        assert key in ui, (
            f"admin-ui 에 {key} 가 주입되지 않는다 — admin-ui 는 그룹으로 역할을 정할 수 "
            "없고 모든 페이지가 /403 이 된다(관리자 잠김)"
        )
        assert api[key] == ui[key], (
            f"{key} 가 두 컨테이너에서 다르다: api={api[key]!r} ui={ui[key]!r} — "
            "이 어긋남이 정확히 관리자 잠김의 원인이다"
        )


def test_values_actually_flow_through():
    """대조군 — 두 컨테이너가 **빈 값으로 나란히 같은** 게 아님을 보인다.

    이 단정이 없으면 위 테스트는 둘 다 `""` 여도 통과한다.
    """
    docs = _render(*OIDC_ON)
    ui = _env_of(docs, "admin-ui")
    assert ui["OIDC_GROUPS_CLAIM"] == "cognito:groups"
    assert ui["ADMIN_GROUPS"] == "GatewayAdmin,Ops"


def test_admin_groups_is_comma_joined_like_admin_api_expects():
    """admin-api ``_split_csv`` 와 admin-ui ``envList`` 가 모두 이해하는 형식."""
    docs = _render(*OIDC_ON)
    for frag in ("admin-api", "admin-ui"):
        value = _env_of(docs, frag)["ADMIN_GROUPS"]
        assert value is not None and "," in value
        assert json.loads(f'["{value.replace(",", chr(34) + chr(44) + chr(34))}"]') == [
            "GatewayAdmin",
            "Ops",
        ]


def test_not_injected_when_oidc_disabled():
    """OIDC 를 안 쓰면 IdP 토큰이 없다 — 주입할 이유가 없고 오늘 동작 그대로여야 한다."""
    docs = _render("adminApi.oidc.enabled=false")
    ui = _env_of(docs, "admin-ui")
    for key in PARITY_KEYS:
        assert key not in ui, f"oidc 비활성인데 {key} 가 주입됐다"


def test_operator_override_in_adminui_env_wins():
    """운영자가 ``adminUi.env`` 로 명시하면 그 값이 이겨야 한다.

    쿠버네티스는 중복 env 중 마지막을 쓰므로, 헬퍼 블록이 자유형 map **앞에** 있어야
    한다. 순서가 뒤바뀌면 운영자 override 가 조용히 무시된다.
    """
    docs = _render(*OIDC_ON, "adminUi.env.ADMIN_GROUPS=OperatorPinned")
    containers = None
    for d in docs:
        if d.get("kind") == "Deployment" and "admin-ui" in d["metadata"]["name"]:
            containers = d["spec"]["template"]["spec"]["containers"]
    assert containers is not None

    env_list = containers[0]["env"]
    occurrences = [e.get("value") for e in env_list if e["name"] == "ADMIN_GROUPS"]
    assert len(occurrences) == 2, (
        f"ADMIN_GROUPS 가 {len(occurrences)}번 나온다 — override 시나리오가 재현되지 않았다"
    )
    assert occurrences[-1] == "OperatorPinned", (
        "자유형 adminUi.env 가 헬퍼보다 **앞에** 렌더됐다 — 운영자 override 가 무시된다"
    )
