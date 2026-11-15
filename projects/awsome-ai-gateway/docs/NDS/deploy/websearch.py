# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""AgentCore WebSearch Gateway 자동 프로비저닝 — install-guide §5-1/§5-2 자동화.

기존 수동 절차:
  1. provision_agentcore_websearch.py deploy  → AgentCore Gateway + WebSearch target
  2. set-websearch-url.sh                     → URL 을 배포 산출물에 주입
  3. 재배포

`deploy apply` 경로에서는 features.web_search=true 만으로 1~2 를 자동 수행한다.
프로비저너는 멱등(기존 role/gateway/target 재사용)이라 apply 를 다시 돌려도 안전하다.
실패해도 배포를 중단하지 않는다 — WEB_SEARCH_ENABLED 플래그만 켜진 채 라우터가
건너뛰는 graceful-off 상태로 두고 경고를 낸다 (install-guide 와 같은 의미).

⚠️ GW_NAME 규칙은 set-websearch-url.sh 와 동일해야 한다 — 이름이 다르면
find_gateway() 가 못 찾아 중복 게이트웨이가 생기고 values 의 URL 은 옛 것을
가리킨 채로 남는다 (프로비저너 docstring 주석 참조).
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

PROVISIONER = Path("deployment/scripts/provision_agentcore_websearch.py")
AGENTCORE_REGION = "us-east-1"   # 관리형 WebSearch 커넥터는 us-east-1 전용
TARGET_ID = "web-search-tool"


def _env(env_name: str, caller_role_arn: str = "") -> dict[str, str]:
    # 부모 쉘의 stale export 가 핀 값을 이기는 걸 막는다 — TARGET_NAME 을
    # 재지정한 쉘에서 deploy 하면 target 이 다른 이름으로 만들어지는데
    # 주입값은 web-search-tool 고정이라 도구를 못 찾는다 (chatagent._env 와
    # 같은 스크럽).
    from .chatagent import _PROVISIONER_INPUTS
    e = {k: v for k, v in os.environ.items()
         if k not in _PROVISIONER_INPUTS}
    e["REGION"] = AGENTCORE_REGION
    # set-websearch-url.sh 와 같은 이름 규칙 — 수동 배포가 만든 gateway 재사용
    e["GW_NAME"] = f"llm-gateway-websearch-{env_name}"
    # exec role 도 env 스코프 — 고정값이면 prod 가 dev- 이름 role 을 만들고,
    # 한 env 의 teardown 이 다른 env gateway 가 참조하는 role 을 지운다.
    # 이 패턴은 env=dev 에서 기존 기본값(llm-gateway-dev-agentcore-websearch-gw)과
    # 정확히 같아 마이그레이션 없이 수렴한다.
    e["ROLE_NAME"] = f"llm-gateway-{env_name}-agentcore-websearch-gw"
    if caller_role_arn:
        e["CALLER_ROLE_ARN"] = caller_role_arn
    return e


def _run(env_name: str, repo_root: Path, sub: str,
         caller_role_arn: str = "") -> subprocess.CompletedProcess:
    script = repo_root / PROVISIONER
    if not script.exists():
        return subprocess.CompletedProcess([], 1, "", f"{script} 없음")
    try:
        return subprocess.run(
            [sys.executable, str(script), sub],
            env=_env(env_name, caller_role_arn),
            # 최악 내부 대기 ≈ 840s(죽은 gw 폴링+gw 생성+target 생성) — 600s
            # 타임아웃은 create 와 READY 사이에서 프로비저너를 죽여 부분 상태+
            # '실패' 오보를 낸다. 여유 포함 1200s.
            capture_output=True, text=True, timeout=1200)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return subprocess.CompletedProcess([], 1, "", str(exc))


def _norm_mcp(url: str) -> str:
    url = url.strip().rstrip("/")
    return url if url.endswith("/mcp") else f"{url}/mcp"


def status_url(env_name: str, repo_root: Path, caller_role_arn: str = "") -> str:
    """정상 gateway 의 /mcp URL — 없거나 조회 실패 시 "".

    읽기 전용 — plan/preview 경로에서도 안전하다.
    ⚠️ URL 만으로 판정하지 않는다 — target 없이 gateway 만 남은 부분 삭제나
    FAILED 상태도 URL 은 출력한다. 그런 상태를 "존재"로 보면 ensure 가
    deploy(자가치유 경로)를 영영 안 돌려 검색이 죽은 채로 수렴한다 —
    gateway READY + target 존재 둘 다 만족할 때만 URL 을 인정한다."""
    r = _run(env_name, repo_root, "status", caller_role_arn)
    if r.returncode != 0:
        return ""
    if not re.search(r"^gateway: \S+ status=READY", r.stdout, re.M):
        return ""
    if not re.search(r"^  target: \S+ status=(READY|CREATING|UPDATING)",
                     r.stdout, re.M):
        return ""
    m = re.search(r"^  url: (\S+)", r.stdout, re.M)
    return _norm_mcp(m.group(1)) if m else ""


def ensure_gateway_url(env_name: str, repo_root: Path,
                       caller_role_arn: str = "") -> str:
    """status 로 조회 → 없으면 deploy(멱등)로 생성 → /mcp URL 반환. 실패 시 ""."""
    url = status_url(env_name, repo_root, caller_role_arn)
    if url:
        return url
    print("web_search: AgentCore Gateway 가 없어 프로비저닝합니다 "
          f"(region={AGENTCORE_REGION}, name=llm-gateway-websearch-{env_name})",
          file=sys.stderr)
    r = _run(env_name, repo_root, "deploy", caller_role_arn)
    if r.returncode != 0:
        tail = (r.stderr or r.stdout or "").strip().splitlines()
        print(f"⚠ web_search 프로비저닝 실패 — {tail[-1] if tail else '오류 없음'} "
              "(boto3 필요 · deploy EC2 자격증명 확인). 플래그만 켜진 채로 "
              "진행하면 라우터가 검색을 건너뜁니다", file=sys.stderr)
        return ""
    m = re.search(r"^AGENTCORE_GATEWAY_URL=(\S+)", r.stdout, re.M)
    url = _norm_mcp(m.group(1)) if m else ""
    if not url:
        print("⚠ web_search 프로비저닝 출력에서 URL 을 못 읽었습니다 — "
              "플래그만 켜진 채로 진행합니다", file=sys.stderr)
    return url


def teardown(env_name: str, repo_root: Path, caller_role_arn: str = "") -> bool:
    """env 규칙명의 AgentCore Gateway + target + exec role 삭제(멱등).

    과금(~$7/1,000 queries)·노출 표면을 끝낸다. 프로비저너는 없으면 skip 한다."""
    r = _run(env_name, repo_root, "teardown", caller_role_arn)
    if r.returncode != 0:
        tail = (r.stderr or r.stdout or "").strip().splitlines()
        print(f"⚠ web_search teardown 실패 — {tail[-1] if tail else '오류 없음'} "
              f"(수동: REGION={AGENTCORE_REGION} GW_NAME=llm-gateway-websearch-{env_name} "
              f"ROLE_NAME=llm-gateway-{env_name}-agentcore-websearch-gw "
              "python3 deployment/scripts/provision_agentcore_websearch.py teardown)",
              file=sys.stderr)
        return False
    return True


def upsert_env_file(env_path: Path, keys: dict[str, str]) -> None:
    """.env 의 k=v 를 upsert — compose backend 의 URL 주입용."""
    existing = env_path.read_text().splitlines() if env_path.exists() else []
    seen: set[str] = set()
    out = []
    for line in existing:
        k = line.split("=", 1)[0].strip()
        if k in keys:
            out.append(f"{k}={keys[k]}")
            seen.add(k)
        else:
            out.append(line)
    for k, v in keys.items():
        if k not in seen:
            out.append(f"{k}={v}")
    # .env 에는 생성 시크릿이 있다 — 직접 truncate 는 쓰기 중 크래시 시
    # 반쪽 파일을 남긴다. 임시파일+rename 으로 원자화하고 권한을 보존한다.
    # umask(0644) tmp → replace 창 동안 시크릿이 노출된다 — 처음부터 0600
    # 으로 열고 무조건 0600 유지(write_env_file 계약: 시크릿 파일은
    # 강화 방향으로 수렴). mkstemp 로 경쟁 upsert 간섭도 피한다.
    import tempfile
    fd, _tname = tempfile.mkstemp(dir=env_path.parent,
                                  prefix=env_path.name + ".", suffix=".tmp")
    tmp = Path(_tname)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write("\n".join(out) + "\n")
        tmp.replace(env_path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
