"""eks apply --build — 변경분 감지 이미지 빌드·푸시·핀 갱신.

update 경로(deploy apply)는 bootstrap 과 달리 이미지를 빌드하지 않는다 —
코드 패치 배포마다 rebuild-image.sh + 수동 핀 편집 + apply 3단계를 반복해야
했다. 이 모듈은 그 흐름을 apply 안으로 합친다:

  --build         핀 커밋→HEAD 간 빌드 컨텍스트 diff 가 있는 서비스만 리빌드
  --build --all   감지 없이 전체 서비스를 HEAD 태그로 리빌드

태그 규약은 rebuild-image.sh 와 동일(git HEAD short sha). IMMUTABLE ECR 에
이미 있는 태그는 빌드하지 않고 핀만 올린다(태그 동일 = 코드 동일).
갱신된 핀은 gateway.yaml 에 다시 쓴다 — 선언형 정본이 실제 배포와 어긋나지
않게 하려면 파일에 남겨야 한다.

안전장치는 rebuild-image.sh 계약을 이식한다:
  * dirty working tree 경고 — 빌드 내용이 태그(HEAD sha)와 어긋남
  * migration 게이트 — db/versions/ 에 신규 revision 이 있는데 migration
    이미지를 같이 올리지 않으면 중단(구 이미지의 alembic 이 no-op 이라
    새 코드가 구 스키마 위에 올라가는 사고 방지)
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from .bootstrap import EKS_SERVICES
from .schema import GatewayConfig


def _q(argv: list[str], *, cwd: Path | None = None,
       input: str | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, cwd=cwd, input=input,
                              capture_output=True, text=True, timeout=300)
    except FileNotFoundError:
        raise SystemExit(f"{argv[0]} 명령을 못 찾았습니다 — PATH 확인 필요")
    except subprocess.TimeoutExpired:
        raise SystemExit(f"{argv[0]} 타임아웃: {' '.join(argv[:4])}")


def _head_tag(repo_root: Path) -> str:
    r = _q(["git", "rev-parse", "--short=8", "HEAD"], cwd=repo_root)
    if r.returncode != 0 or not r.stdout.strip():
        raise SystemExit("git HEAD 조회 실패 — 이미지 태그를 만들 수 없습니다")
    return r.stdout.strip()


def _is_commit(repo_root: Path, ref: str) -> bool:
    return _q(["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
              cwd=repo_root).returncode == 0


def _ctx_changed(repo_root: Path, pin: str, ctx: str) -> bool | None:
    """pin 커밋→HEAD 간 ctx/ diff — 비교 불가(핀이 git 개체 아님)면 None."""
    if not _is_commit(repo_root, pin):
        return None
    return _q(["git", "diff", "--quiet", pin, "HEAD", "--", f"{ctx}/"],
              cwd=repo_root).returncode != 0


def detect_rebuilds(cfg: GatewayConfig, repo_root: Path,
                    build_all: bool) -> dict[str, str]:
    """리빌드할 helm_key → 사유 맵. 태그는 호출자가 HEAD sha 로 매긴다."""
    want: dict[str, str] = {}
    for helm_key, _repo, ctx in EKS_SERVICES:
        pin = (cfg.images.tags or {}).get(helm_key) or cfg.images.tag or ""
        if build_all:
            want[helm_key] = "--all"
            continue
        changed = _ctx_changed(repo_root, pin, ctx)
        if changed is True:
            want[helm_key] = f"{pin}→HEAD 변경"
        elif changed is None:
            want[helm_key] = f"핀 {pin or '(없음)'} 비교 불가"
    return want


def _dirty_ctxs(repo_root: Path, ctxs: list[str]) -> list[str]:
    dirty = []
    for ctx in ctxs:
        r = _q(["git", "status", "--porcelain", "--", f"{ctx}/"], cwd=repo_root)
        if r.returncode == 0 and r.stdout.strip():
            dirty.append(ctx)
    return dirty


def _migration_gate(cfg: GatewayConfig, repo_root: Path,
                    want: dict[str, str]) -> None:
    """db/versions/ 에 migration 핀 이후 신규 revision 이 있는데 migration 을
    같이 빌드하지 않으면 중단 — rebuild-image.sh R24-S5 게이트 이식."""
    if "migration" in want:
        return
    mig_pin = (cfg.images.tags or {}).get("migration") or ""
    if not mig_pin or not _is_commit(repo_root, mig_pin):
        return  # 비교 불가면 게이트 불가 — 경고 단계에서 이미 알림
    r = _q(["git", "diff", "--quiet", "--diff-filter=A", mig_pin, "HEAD",
            "--", "db/versions/"], cwd=repo_root)
    if r.returncode != 0:
        raise SystemExit(
            f"db/versions/ 에 migration 핀({mig_pin}) 이후 신규 revision 이 "
            "있습니다 — 스키마부터 배포해야 합니다.\n"
            "  `./deploy apply --build --all` (migration 포함 전체) 또는 "
            "deployment/scripts/rebuild-image.sh migration dev 후 재시도")


def _ecr_has_tag(repo: str, tag: str, region: str,
                 registry_id: str) -> bool | None:
    argv = ["aws", "ecr", "describe-images", "--repository-name",
            f"llm-gateway/{repo}", "--image-ids", f"imageTag={tag}",
            "--region", region, "--output", "json"]
    if registry_id:
        argv += ["--registry-id", registry_id]
    r = _q(argv)
    if r.returncode == 0:
        return True
    if "NotFoundException" in (r.stderr or ""):
        return False
    return None  # 권한/네트워크 — 유보


def _push_set(cfg: GatewayConfig, region: str, tag: str,
              want: dict[str, str], repo_root: Path) -> None:
    """want 의 서비스를 tag 로 build+push — 이미 ECR 에 있으면 빌드 생략."""
    from .eks_apply import _aws_account_id  # 늦은 import — 호출 시점 로드
    registry = (cfg.images.registry or "").rstrip("/")
    registry_id = ""
    if not registry:
        account = _aws_account_id(region)
        if not account:
            raise SystemExit("images.registry 미지정 + account 추론 실패 — "
                             "ECR 대상을 정할 수 없습니다")
        registry = f"{account}.dkr.ecr.{region}.amazonaws.com"
    elif ".dkr.ecr." in registry:
        registry_id = registry.split(".", 1)[0]

    pw = _q(["aws", "ecr", "get-login-password", "--region", region])
    if pw.returncode != 0:
        raise SystemExit(f"ECR 로그인 실패: {pw.stderr.strip()[:200]}")
    login = _q(["docker", "login", "--username", "AWS", "--password-stdin",
                registry], input=pw.stdout)
    if login.returncode != 0:
        raise SystemExit(f"docker login 실패: {login.stderr.strip()[:200]}")

    for helm_key, repo, ctx in EKS_SERVICES:
        if helm_key not in want:
            continue
        image = f"{registry}/llm-gateway/{repo}:{tag}"
        have = _ecr_has_tag(repo, tag, region, registry_id)
        if have is True:
            print(f"  ⏭ {image} — ECR 에 이미 있음(태그 동일 = 코드 동일) "
                  "— 빌드 생략, 핀만 갱신", file=sys.stderr)
            continue
        if have is None:
            raise SystemExit(f"ECR 조회 실패 — {repo}:{tag} 존재 여부를 알 수 "
                             "없어 멈춥니다(덮어쓰기 방지)")
        print(f"  🔨 {image} — {want[helm_key]}", file=sys.stderr)
        r = _q(["docker", "build", "--platform", "linux/amd64",
                "-t", image, str(repo_root / ctx)], cwd=repo_root)
        if r.returncode != 0:
            raise SystemExit(f"docker build 실패 ({image}): "
                             f"{(r.stderr or r.stdout or '')[-400:]}")
        r = _q(["docker", "push", image])
        if r.returncode != 0:
            # 동시 apply 레이스 — 상대가 먼저 같은 태그를 push 한 경우 성공.
            if "AlreadyExists" in (r.stderr or ""):
                print(f"  ⓘ {image} — push 레이스로 이미 존재 — 건너뜀",
                      file=sys.stderr)
                continue
            raise SystemExit(f"docker push 실패 ({image}): "
                             f"{(r.stderr or r.stdout or '')[-400:]}")


_TAG_LINE = re.compile(r"^(\s*)([A-Za-z_][\w]*)\s*:\s*([\"']?)[^#\s]*\3")


def rewrite_tags(text: str, updates: dict[str, str]) -> str:
    """gateway.yaml 텍스트의 images.tags 핀을 updates 로 갱신 — 순수 함수.

    항목은 `    key: "tag"   # comment` 형태 — 값 토큰만 바꾸고 인라인
    주석은 보존한다. 없는 키는 tags: 블록 첫 항목 자리에 같은 들여쓰기로
    삽입한다."""
    lines = text.splitlines(keepends=True)
    # images: 블록 범위 — 다음 최상위 키까지
    start = next((i for i, l in enumerate(lines)
                  if re.match(r"^images\s*:", l)), None)
    if start is None:
        raise SystemExit("gateway.yaml 에 images: 섹션이 없습니다")
    end = next((i for i in range(start + 1, len(lines))
                if lines[i] and not lines[i][0].isspace()
                and not lines[i].lstrip().startswith("#")), len(lines))
    # tags: 라인 — 없으면 images: 직후에 생성
    tags_i = next((i for i in range(start + 1, end)
                   if re.match(r"^\s+tags\s*:", lines[i])), None)
    if tags_i is None:
        lines.insert(start + 1, "  tags:\n")
        tags_i, end = start + 1, end + 1
    block_end = next((i for i in range(tags_i + 1, end)
                      if lines[i].rstrip() and not lines[i].lstrip().startswith("#")
                      and len(lines[i]) - len(lines[i].lstrip())
                      <= len(lines[tags_i]) - len(lines[tags_i].lstrip())),
                     end)

    pending = dict(updates)
    for i in range(tags_i + 1, block_end):
        m = _TAG_LINE.match(lines[i].rstrip("\n"))
        if not m or m.group(2) not in pending:
            continue
        key = m.group(2)
        comment = ("  " + lines[i][lines[i].index("#"):].rstrip("\n")
                   if "#" in lines[i] else "")
        lines[i] = f'{m.group(1)}{key}: "{pending.pop(key)}"{comment}\n'
    if pending:
        indent = "    "
        ins = tags_i + 1
        for key, tag in pending.items():
            lines.insert(ins, f'{indent}{key}: "{tag}"\n')
            ins += 1
    return "".join(lines)


def build_and_pin(cfg: GatewayConfig, repo_root: Path, config_path: Path,
                  *, build_all: bool = False) -> list[str]:
    """--build 진입점 — 감지→게이트→빌드/푸시→yaml 핀 갱신. 갱신 키 목록 반환.

    호출자는 반환 후 config 를 재로드해 render 가 새 핀을 보게 해야 한다."""
    tag = _head_tag(repo_root)
    want = detect_rebuilds(cfg, repo_root, build_all)
    # adminApi 를 올리면 이미지를 공유하는 scheduler 핀도 반드시 따라간다
    # (values 주석 "adminApi.image.tag 와 항상 같은 값" 계약).
    if "adminApi" in want and "scheduler" not in want:
        want["scheduler"] = "adminApi 이미지 공유"
    # scheduler 단독 핀(adminApi 와 다른 태그)은 계약 위반 — 같이 올린다.
    elif "scheduler" in want:
        want.setdefault("adminApi", "scheduler 이미지 공유")

    _migration_gate(cfg, repo_root, want)
    if not want:
        print("ⓘ 빌드할 변경분이 없습니다 — 핀은 그대로 둡니다", file=sys.stderr)
        return []

    dirty = _dirty_ctxs(repo_root, [ctx for k, _, ctx in EKS_SERVICES
                                  if k in want])
    if dirty:
        print(f"⚠ 미커밋 변경이 있는 빌드 컨텍스트: {', '.join(dirty)} — "
              f"빌드 내용과 태그({tag})가 어긋납니다. 커밋 후 재실행 권장",
              file=sys.stderr)

    _push_set(cfg, cfg.aws.region, tag, want, repo_root)

    updates = {k: tag for k in want}
    from .cli import _atomic_write
    text = config_path.read_text()
    _atomic_write(config_path, rewrite_tags(text, updates))
    print(f"  gateway.yaml 핀 갱신: "
          + ", ".join(f"{k}={t}" for k, t in updates.items()), file=sys.stderr)
    return sorted(updates)
