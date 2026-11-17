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

import yaml

from .bootstrap import EKS_SERVICES
from .schema import GatewayConfig


def _q(argv: list[str], *, cwd: Path | None = None,
       input: str | None = None,
       timeout: int = 300) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, cwd=cwd, input=input,
                              capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise SystemExit(f"{argv[0]} 명령을 못 찾았습니다 — PATH 확인 필요")
    except subprocess.TimeoutExpired:
        raise SystemExit(f"{argv[0]} 타임아웃({timeout}s): {' '.join(argv[:4])}")


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
    # render 와 같은 폴백 — 무핀이면 images.tag 가 migration 태그다.
    mig_pin = (cfg.images.tags or {}).get("migration") or cfg.images.tag or ""
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


# bootstrap.push_images_eks 와 같은 판별 — ECR 호스트만 aws ecr API 대상.
_ECR_RE = re.compile(
    r"^(\d{12})\.dkr\.ecr\.([a-z0-9-]+)\.amazonaws\.com(\.cn)?$")


def _registry(cfg: GatewayConfig, region: str) -> tuple[str, str, str | None]:
    """(registry 호스트, registry_id, ecr_region) — ecr_region 이 None 이면
    비-ECR 레지스트리(harbor 등)라 aws ecr API/로그인 대상이 아니다.
    cross-region ECR 은 호스트에 박힌 리전이 API 리전이다(deploy 리전 X)."""
    registry = (cfg.images.registry or "").rstrip("/")
    if not registry:
        from .eks_apply import _aws_account_id  # 늦은 import — 호출 시점 로드
        account = _aws_account_id(region)
        if not account:
            raise SystemExit("images.registry 미지정 + account 추론 실패 — "
                             "ECR 대상을 정할 수 없습니다")
        registry = f"{account}.dkr.ecr.{region}.amazonaws.com"
    m = _ECR_RE.match(registry)
    if m:
        return registry, m.group(1), m.group(2)
    return registry, "", None


def _push_set(cfg: GatewayConfig, region: str, tag: str,
              want: dict[str, str], repo_root: Path) -> None:
    """want 의 서비스를 tag 로 build+push — ECR 이면 기존 태그는 빌드 생략."""
    registry, registry_id, ecr_region = _registry(cfg, region)
    if ecr_region:
        pw = _q(["aws", "ecr", "get-login-password",
                 "--region", ecr_region])
        if pw.returncode != 0:
            raise SystemExit(f"ECR 로그인 실패: {pw.stderr.strip()[:200]}")
        login = _q(["docker", "login", "--username", "AWS",
                    "--password-stdin", registry], input=pw.stdout)
        if login.returncode != 0:
            raise SystemExit(
                f"docker login 실패: {login.stderr.strip()[:200]}")
    else:
        print(f"ⓘ images.registry({registry}) 가 ECR 이 아닙니다 — ECR "
              "로그인/태그 확인을 건너뜁니다. 대상 레지스트리에 docker "
              "login 돼 있어야 push 가 성공합니다", file=sys.stderr)

    for helm_key, repo, ctx in EKS_SERVICES:
        if helm_key not in want:
            continue
        image = f"{registry}/llm-gateway/{repo}:{tag}"
        have = (_ecr_has_tag(repo, tag, ecr_region, registry_id)
                if ecr_region else False)  # 비-ECR 은 존재 확인 불가 → 빌드
        if have is True:
            print(f"  ⏭ {image} — ECR 에 이미 있음(태그 동일 = 코드 동일) "
                  "— 빌드 생략, 핀만 갱신", file=sys.stderr)
            continue
        if have is None:
            raise SystemExit(f"ECR 조회 실패 — {repo}:{tag} 존재 여부를 알 수 "
                             "없어 멈춥니다(덮어쓰기 방지)")
        print(f"  🔨 {image} — {want[helm_key]}", file=sys.stderr)
        # cold 빌드(npm install 없음 상태)는 5분을 넘길 수 있다 — admin-ui
        # 캐시 적중도 ~2분이라 git 조회와 같은 타임아웃을 쓰면 스푸리어스 실패.
        r = _q(["docker", "build", "--platform", "linux/amd64",
                "-t", image, str(repo_root / ctx)], cwd=repo_root,
               timeout=1800)
        if r.returncode != 0:
            raise SystemExit(f"docker build 실패 ({image}): "
                             f"{(r.stderr or r.stdout or '')[-400:]}")
        r = _q(["docker", "push", image], timeout=900)
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
        # 기존 항목의 들여쓰기를 따른다 — 없으면 tags: 보다 한 단계 안쪽.
        # 하드코딩된 깊이는 파일의 실제 관례와 어긋나 malformed YAML 이 된다.
        indent = next(
            (m.group(1) for i in range(tags_i + 1, block_end)
             if (m := _TAG_LINE.match(lines[i].rstrip("\n")))),
            " " * (len(lines[tags_i]) - len(lines[tags_i].lstrip()) + 2))
        ins = tags_i + 1
        for key, tag in pending.items():
            lines.insert(ins, f'{indent}{key}: "{tag}"\n')
            ins += 1
    return "".join(lines)


def propagate_shared(want: dict[str, str]) -> dict[str, str]:
    """이미지 공유 계약 전파 — adminApi 를 올리면 같은 이미지를 당기는
    scheduler 핀도 반드시 따라간다(values 주석 "adminApi.image.tag 와
    항상 같은 값"). --build/--plan 양쪽에서 쓴다."""
    if "adminApi" in want and "scheduler" not in want:
        want = {**want, "scheduler": "adminApi 이미지 공유"}
    elif "scheduler" in want and "adminApi" not in want:
        want = {**want, "adminApi": "scheduler 이미지 공유"}
    return want


def build_and_pin(cfg: GatewayConfig, repo_root: Path, config_path: Path,
                  *, build_all: bool = False) -> list[str]:
    """--build 진입점 — 감지→게이트→빌드/푸시→yaml 핀 갱신. 갱신 키 목록 반환.

    호출자는 반환 후 config 를 재로드해 render 가 새 핀을 보게 해야 한다."""
    config_path = config_path.resolve()  # symlink 이면 실체에 쓴다 —
    # read_text 는 링크를 따라가는데 _atomic_write(rename) 는 링크 자체를
    # regular 파일로 바꿔 타겟이 어긋나는 걸 막는다.
    tag = _head_tag(repo_root)
    want = propagate_shared(detect_rebuilds(cfg, repo_root, build_all))

    _migration_gate(cfg, repo_root, want)

    # 핀=HEAD 인데 레지스트리에 이미지가 없는 서비스는 감지에서 빠져 apply 가
    # ImagePullBackOff 로 간다 — 핀만 쓴 수동 경로의 잔재를 여기서 회수.
    # scheduler 는 admin-api 이미지를 당기므로 같은 repo 를 확인한다.
    # ECR 이면 describe 로 확인하고, 비-ECR 은 확인 자체가 불가라 전부 빌드.
    _reg, registry_id, ecr_region = _registry(cfg, cfg.aws.region)
    tags = cfg.images.tags or {}
    ensure = [(k, repo) for k, repo, _ctx in EKS_SERVICES]
    ensure.append(("scheduler", "admin-api"))
    for helm_key, repo in ensure:
        if helm_key in want:
            continue
        pin = tags.get(helm_key) or cfg.images.tag or ""
        if pin != tag:
            continue
        if not ecr_region:
            want[helm_key] = "핀=HEAD — 비-ECR 이라 존재 확인 불가"
            continue
        have = _ecr_has_tag(repo, tag, ecr_region, registry_id)
        if have is False:
            want[helm_key] = "핀=HEAD인데 ECR 에 이미지 없음"
        elif have is None:
            print(f"⚠ {repo}:{tag} ECR 조회 실패 — 존재 여부를 확인할 수 "
                  "없어 그대로 진행합니다(없으면 ImagePullBackOff)",
                  file=sys.stderr)
    want = propagate_shared(want)  # ensure 가 adminApi 를 늦게 넣은 경우 추종

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

    # config 가 git 추적 파일이면(커밋된 템플릿 등) 핀 쓰기가 working tree 를
    # 오염시킨다 — gateway.yaml 처럼 무시된 정본이 관례라 미리 알린다.
    if _q(["git", "ls-files", "--error-unmatch", config_path.name],
          cwd=config_path.parent).returncode == 0:
        print(f"⚠ {config_path} 는 git 추적 파일입니다 — 핀 갱신이 "
              "커밋된 파일을 변경합니다", file=sys.stderr)

    updates = {k: tag for k in want}
    from .cli import _atomic_write
    text = config_path.read_text()
    new_text = rewrite_tags(text, updates)
    try:
        yaml.safe_load(new_text)
    except yaml.YAMLError as exc:
        raise SystemExit(
            f"핀 갱신 결과가 YAML 로 파싱되지 않습니다 — "
            f"{config_path} 는 건드리지 않았습니다: {exc}")
    _atomic_write(config_path, new_text)
    print(f"  gateway.yaml 핀 갱신: "
          + ", ".join(f"{k}={t}" for k, t in updates.items()), file=sys.stderr)
    return sorted(updates)
