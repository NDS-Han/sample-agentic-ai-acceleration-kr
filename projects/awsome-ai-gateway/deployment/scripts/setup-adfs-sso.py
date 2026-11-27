#!/usr/bin/env python3
# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""ADFS SAML SSO 설치자 — gateway.yaml 을 정합성 있게 갱신하고 adopt 절차를 안내한다.

명령적(검증·조회·import) 작업은 여기서, 결과는 선언적(gateway.yaml)으로 —
이후 수렴은 표준 경로 `./deploy apply` 가 담당한다 (NDS 배포 방식).

하는 일 (setup):
  1. 메타데이터 fetch/파싱 검증 — 유효한 SAML federation metadata 인지 확인
  2. Cognito 풀 조회(가능하면) — 동명 IdP 가 이미 있으면 adopt(import) 절차 안내/실행
  3. gateway.yaml 정합 갱신 — infra.cognito_saml + oidc.groups_claim=custom:groups
     + oidc.group_prefix + oidc.admin_groups (SAML attribute-mode 필수 연계 세트)
  4. 고객사 ADFS 관리자용 relying party trust 설정 안내 출력
  5. 다음 단계(render → import → apply) 명령을 정확히 출력

teardown 은 infra.cognito_saml 항목을 지우고 apply 안내를 출력한다
(IdP 자체 삭제는 apply 의 terraform destroy — 켜기만 소유하는 overlay 계약상
yaml 에서 지우면 managed overlay 가 키를 빼므로 env tfvars 수동 절차를 안내한다).

사용:
  python3 deployment/scripts/setup-adfs-sso.py setup \
      --provider-name ssir-adfs \
      --metadata-url https://adfs.example.com/federationmetadata/2007-06/federationmetadata.xml \
      --group-prefix ssir_ --admin-group ssir_claude_admin
  python3 deployment/scripts/setup-adfs-sso.py teardown --provider-name ssir-adfs
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_CONFIG = REPO_ROOT / "deployment" / "gateway.yaml"

# Microsoft ADFS 의 표준 claim URI — 고객사가 커스텀 rule 을 쓰면 인자로 바꾼다.
ADFS_EMAIL_CLAIM = "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress"
ADFS_NAME_CLAIM = "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/name"
# ADFS 의 그룹 클레임 표준 pairing — LDAP attribute "Token-Groups - Unqualified Names"
# → outgoing claim type "Group" (이 URI). microsoft.com 2008/06 groups URI 도
# 쓰이는 환경이 있으나 ADFS UI 기본 드롭다운 조합은 이쪽이다 — 다르면 --groups-claim 으로.
ADFS_GROUPS_CLAIM = "http://schemas.xmlsoap.org/claims/Group"


def log(msg: str) -> None:
    print(f"[adfs-setup] {msg}", file=sys.stderr)


def fail(msg: str) -> int:
    print(f"[adfs-setup] ERROR: {msg}", file=sys.stderr)
    return 1


def _run(argv: list[str], cwd: Path | None = None, timeout: int = 60) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return subprocess.CompletedProcess(argv, 127, "", f"{argv[0]} 실행 파일 없음")
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(argv, 124, "", "timeout")


# ------------------------------------------------------------------------------
# 1. 메타데이터 검증
# ------------------------------------------------------------------------------
def fetch_and_validate_metadata(args) -> tuple[str, str]:
    """(xml 내용, entityID) 반환 — url 또는 file 에서 읽고 SAML 메타데이터인지 검증."""
    if args.metadata_url:
        try:
            with urllib.request.urlopen(args.metadata_url, timeout=15) as resp:
                xml_text = resp.read().decode("utf-8")
        except (urllib.error.URLError, OSError) as exc:
            raise SystemExit(fail(
                f"메타데이터 URL fetch 실패: {exc}\n"
                "  ADFS 메타데이터 엔드포인트가 이 환경에서 reachable 한지 확인하세요.\n"
                "  내부망 전용이면 XML 을 다운로드해 --metadata-file 로 지정할 수 있습니다."))
    else:
        xml_text = Path(args.metadata_file).read_text()
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise SystemExit(fail(f"메타데이터 XML 파싱 실패: {exc}"))
    if not root.tag.endswith("EntityDescriptor"):
        raise SystemExit(fail(
            f"SAML federation metadata 가 아닙니다 (root={root.tag!r}) — "
            "EntityDescriptor 여야 합니다. ADFS 라면 보통 "
            "https://<adfs-host>/federationmetadata/2007-06/federationmetadata.xml 입니다."))
    entity_id = root.attrib.get("entityID", "")
    return xml_text, entity_id


# ------------------------------------------------------------------------------
# 2. Cognito 조회 — terraform output + 기존 IdP 감지
# ------------------------------------------------------------------------------
def tf_outputs(env_dir: Path) -> dict:
    r = _run(["terraform", "output", "-json"], cwd=env_dir, timeout=120)
    if r.returncode != 0:
        return {}
    try:
        return {k: v.get("value") for k, v in json.loads(r.stdout).items()}
    except (json.JSONDecodeError, AttributeError):
        return {}


def find_env_dir(doc: dict, env: str) -> Path | None:
    rel = ((doc.get("deploy") or {}).get("tf_env_dir")
           or f"deployment/terraform/environments/llm-gateway-{env}")
    p = REPO_ROOT / rel
    return p if p.exists() else None


def list_pool_idps(pool_id: str, region: str) -> list[str]:
    r = _run(["aws", "cognito-idp", "list-identity-providers",
              "--user-pool-id", pool_id, "--max-results", "60",
              "--region", region, "--output", "json"])
    if r.returncode != 0:
        return []
    try:
        return [p.get("ProviderName", "")
                for p in json.loads(r.stdout).get("Providers", [])]
    except json.JSONDecodeError:
        return []


# ------------------------------------------------------------------------------
# 3. gateway.yaml 갱신
# ------------------------------------------------------------------------------
def update_yaml(config_path: Path, provider: str, entry: dict, *,
                group_prefix: str, admin_group: str) -> None:
    doc = yaml.safe_load(config_path.read_text()) or {}
    if not isinstance(doc, dict):
        raise SystemExit(fail("gateway.yaml 최상위가 mapping 이 아닙니다"))
    infra = doc.get("infra")
    if not isinstance(infra, dict):
        infra = doc["infra"] = {}
    saml = infra.get("cognito_saml")
    if not isinstance(saml, dict):
        saml = infra["cognito_saml"] = {}
    if provider in saml:
        log(f"기존 infra.cognito_saml.{provider} 항목을 갱신합니다 (idempotent)")
    saml[provider] = entry

    oidc = doc.get("oidc")
    if not isinstance(oidc, dict):
        oidc = doc["oidc"] = {}
    # 정합 세트 — SAML attribute-mode 에서 groups_claim 은 반드시 custom:groups.
    # cognito:groups(기본)로 두면 토큰에서 그룹을 못 읽어 전원 403 이 된다.
    if oidc.get("groups_claim") not in (None, "", "custom:groups"):
        log(f"⚠ 기존 oidc.groups_claim={oidc['groups_claim']!r} → custom:groups 로 변경합니다")
    oidc["groups_claim"] = "custom:groups"
    if group_prefix:
        oidc["group_prefix"] = group_prefix
    if admin_group:
        existing = oidc.get("admin_groups") or []
        if isinstance(existing, str):
            existing = [existing]
        if admin_group not in existing:
            oidc["admin_groups"] = [*existing, admin_group]

    # 쓰기 전 스키마 검증 — 깨진 yaml 저장으로 모든 deploy 명령이 멈추는 사고 방지.
    # 원본이 이미 무효였다면 이번 갱신 탓이 아니므로 저장한다 (bootstrap 과 같은 계약).
    sys.path.insert(0, str(REPO_ROOT / "docs" / "NDS"))
    try:
        from deploy import schema as _schema  # type: ignore
        _schema.from_dict(doc)
    except Exception as exc:
        try:
            _schema.from_dict(yaml.safe_load(config_path.read_text()) or {})
        except Exception:
            pass  # 원본도 이미 무효 — 갱신 탓이 아니니 저장 진행
        else:
            raise SystemExit(fail(
                f"갱신 결과가 gateway.yaml 스키마를 통과하지 못합니다 — 저장 중단:\n{exc}"))
    config_path.write_text(
        yaml.dump(doc, allow_unicode=True, sort_keys=False, width=120))
    log(f"gateway.yaml 갱신 완료: infra.cognito_saml.{provider} + oidc 정합 세트")


# ------------------------------------------------------------------------------
# setup / teardown
# ------------------------------------------------------------------------------
def cmd_setup(args) -> int:
    config_path = Path(args.config)
    if not config_path.exists():
        return fail(f"gateway.yaml 없음: {config_path}")
    if not re.fullmatch(r"[\w][\w.\-]{0,127}", args.provider_name):
        return fail(f"provider 이름은 영숫자/밑줄/하이픈/마침표 1~128자: {args.provider_name!r}")

    xml_text, entity_id = fetch_and_validate_metadata(args)
    log(f"메타데이터 검증 통과 — entityID: {entity_id or '(없음)'}")

    entry = {"attribute_mapping": {
        "email": args.email_claim,
        "name": args.name_claim,
        "custom:groups": args.groups_claim,
    }}
    if args.metadata_url:
        entry["metadata_url"] = args.metadata_url
    else:
        entry["metadata_xml"] = xml_text
    if args.idp_identifier:
        entry["idp_identifiers"] = args.idp_identifier

    doc = yaml.safe_load(config_path.read_text()) or {}
    env = doc.get("env", "dev")
    region = (doc.get("aws") or {}).get("region", "ap-northeast-2")

    # 기존 수동 IdP 감지 — import 안내 판단
    env_dir = find_env_dir(doc, env)
    pool_id, client_id, domain_host = "", "", ""
    existing_idps: list[str] = []
    detection_failed = False
    if env_dir:
        outs = tf_outputs(env_dir)
        pool_id = outs.get("cognito_user_pool_id") or ""
        client_id = outs.get("cognito_client_id") or ""
        domain_host = outs.get("cognito_hosted_ui_domain") or ""
        if pool_id:
            existing_idps = list_pool_idps(pool_id, region)
            if not existing_idps:
                # aws cli 실패(자격/권한)와 '진짜 IdP 없음'을 구별할 수 없다 — 경고한다
                detection_failed = True
        else:
            detection_failed = True
    else:
        detection_failed = True

    update_yaml(config_path, args.provider_name, entry,
                group_prefix=args.group_prefix, admin_group=args.admin_group)

    print()
    print("=" * 72)
    print("다음 단계 — 순서대로 실행하세요")
    print("=" * 72)
    print()
    print("1. overlay 렌더:")
    print("   ./deploy render")
    print()
    already = args.provider_name in existing_idps
    if already or args.import_existing:
        gen_tfvars = REPO_ROOT / "deployment" / "gen" / env / "eks" / "terraform.tfvars"
        imp = (f"terraform -chdir={env_dir or '<env-dir>'} import "
               f"-var-file={gen_tfvars} "
               f"'module.cognito.aws_cognito_identity_provider.saml[\"{args.provider_name}\"]' "
               f"{pool_id or '<pool-id>'}/{args.provider_name}")
        print("2. 기존 수동 IdP adopt — 최초 한 번만:")
        if already:
            print(f"   (풀에 '{args.provider_name}' 이 이미 있습니다 — import 없으면 apply 실패)")
        print(f"   {imp}")
        if args.execute_import and pool_id and env_dir:
            r = _run(["terraform", f"-chdir={env_dir}", "import",
                      "-var-file", str(gen_tfvars),
                      f'module.cognito.aws_cognito_identity_provider.saml["{args.provider_name}"]',
                      f"{pool_id}/{args.provider_name}"], timeout=300)
            if r.returncode == 0:
                print("   ✓ import 완료")
            else:
                print(f"   ⚠ import 실패 — 수동 실행 필요: {(r.stderr or '').strip()[:200]}")
        print()
    else:
        print("2. (신규 IdP — import 불필요)")
        if detection_failed:
            print("   ⚠ 풀 조회를 못 했습니다(env terraform output/AWS 자격 없음).")
            print("     콘솔에 수동 등록된 동명 IdP 가 있으면 apply 가 실패합니다 —")
            print("     있다면 --import-existing 을 붙여 import 절차를 출력받으세요.")
        print()
    print("3. 적용:")
    print("   ./deploy apply")
    print()
    print("4. 수렴 확인:")
    print("   ./deploy doctor   (saml-idp 점검 OK 확인)")
    print()
    print("=" * 72)
    print("고객사 ADFS 관리자에게 전달할 relying party trust 설정")
    print("=" * 72)
    print()
    print(f"  Relying party identifier : urn:amazon:cognito:sp:{pool_id or '<pool-id>'}")
    if domain_host:
        host = domain_host  # output 은 이미 전체 호스트명
    else:
        host = "<cognito-domain>.auth." + region + ".amazoncognito.com"
    print(f"  ACS (SAML consumer) URL  : https://{host}/saml2/idpresponse")
    print(f"  Sign-out URL             : https://{host}/saml2/logout")
    print()
    print("  필요한 claim issuance rules (template: Send LDAP Attributes as Claims):")
    print(f"    E-Mail-Addresses                 → E-Mail Address")
    print(f"    Display-Name                     → Name")
    print(f"    Token-Groups - Unqualified Names → Group")
    print()
    print("  ⚠ LDAP attribute 는 반드시 'Token-Groups - Unqualified Names' 여야 합니다.")
    print("    'Token-Groups' 또는 'as SIDs' 는 그룹명이 아니라 SID(S-1-5-21-...) 를")
    print("    보내 그룹 파싱이 전부 실패합니다. outgoing 'Group' 의 URI")
    print(f"    ({args.groups_claim}) 가 --groups-claim 기본값과 다르면 인자로 맞추세요.")
    print("  ⚠ 사용자의 AD 그룹이 많으면 custom 속성 2048자 제한에 잘릴 수 있습니다 —")
    print("    게이트웨이용 그룹만 보내도록 regex 필터 규칙(예: ^ssir_)을 권장합니다.")
    print()
    print("  ⚠ groups claim 은 AD 그룹명이 그대로 옵니다 — 대소문자가")
    print(f"    oidc.group_prefix({args.group_prefix!r})·admin_groups 와 정확히 일치해야 합니다.")
    print()
    print("검증: SAML 사용자 1명으로 hosted UI 로그인 후 팀 배정·역할을 확인하세요.")
    return 0


def cmd_teardown(args) -> int:
    config_path = Path(args.config)
    doc = yaml.safe_load(config_path.read_text()) or {}
    saml = ((doc.get("infra") or {}).get("cognito_saml") or {})
    if args.provider_name not in saml:
        return fail(f"infra.cognito_saml.{args.provider_name} 없음 — 제거할 것이 없습니다")
    del saml[args.provider_name]
    if not saml:
        doc["infra"].pop("cognito_saml", None)
    config_path.write_text(
        yaml.dump(doc, allow_unicode=True, sort_keys=False, width=120))
    log(f"gateway.yaml 에서 infra.cognito_saml.{args.provider_name} 제거")
    print()
    print("⚠ managed overlay 는 '켜기'만 소유합니다 — yaml 에서 지우면 overlay 가")
    print("  키를 빼므로 IdP 는 terraform 에 남습니다. 실제 삭제는 env terraform 절차:")
    print()
    env = doc.get("env", "dev")
    env_dir = find_env_dir(doc, env) or Path(f"deployment/terraform/environments/llm-gateway-{env}")
    print(f"  terraform -chdir={env_dir} destroy \\")
    print(f"    -target='module.cognito.aws_cognito_identity_provider.saml[\"{args.provider_name}\"]'")
    print()
    print("  ⚠ 수동 destroy 전까지 ./deploy apply 는 이 IdP 의 파괴 변경을 감지해")
    print("    범위 가드가 중단시킵니다 — 절차를 같은 창에 완료하세요.")
    print()
    print("  oidc.groups_claim/group_prefix/admin_groups 는 SAML 전용이 아니라")
    print("  자동으로 되돌리지 않았습니다 — 필요하면 gateway.yaml 을 직접 편집하세요.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("setup", help="ADFS SAML 연동 설정 — gateway.yaml 정합 갱신 + 절차 안내")
    sp.add_argument("--config", default=str(DEFAULT_CONFIG))
    sp.add_argument("--provider-name", required=True, help="Cognito IdP 이름 (예: ssir-adfs)")
    src = sp.add_mutually_exclusive_group(required=True)
    src.add_argument("--metadata-url", help="ADFS federation metadata URL")
    src.add_argument("--metadata-file", help="메타데이터 XML 파일 경로 (내부망 전용일 때)")
    sp.add_argument("--group-prefix", default="", help="그룹 파싱 prefix (예: ssir_)")
    sp.add_argument("--admin-group", default="", help="admin 부트스트랩 그룹 (예: ssir_claude_admin)")
    sp.add_argument("--email-claim", default=ADFS_EMAIL_CLAIM)
    sp.add_argument("--name-claim", default=ADFS_NAME_CLAIM)
    sp.add_argument("--groups-claim", default=ADFS_GROUPS_CLAIM)
    sp.add_argument("--idp-identifier", action="append", default=[],
                    help="IdP identifier (이메일 도메인 라우팅용, 반복 가능)")
    sp.add_argument("--import-existing", action="store_true",
                    help="기존 수동 IdP 가 있음을 알고 import 절차를 출력")
    sp.add_argument("--execute-import", action="store_true",
                    help="import 를 안내 대신 직접 실행 (terraform state 접근 필요)")
    tp = sub.add_parser("teardown", help="SAML 연동 제거 — yaml 정리 + 삭제 절차 안내")
    tp.add_argument("--config", default=str(DEFAULT_CONFIG))
    tp.add_argument("--provider-name", required=True)
    args = ap.parse_args()
    return {"setup": cmd_setup, "teardown": cmd_teardown}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
