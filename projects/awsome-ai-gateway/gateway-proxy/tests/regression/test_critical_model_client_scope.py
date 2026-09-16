# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""모델 × 앱 인가 축(``model_aliases.allowed_clients``, migration 0035).

두 개의 서로 다른 인가 축이 있고, **의미가 다르다**:

    auth.user_allowed_clients        사용자 U 가 앱 codex 를 쓸 수 있나   (사용자 × 앱)
    model_aliases.allowed_clients    앱 codex 가 모델 opus-5 를 쓸 수 있나 (모델 × 앱)

전자는 ``None``/``[]`` 를 **둘 다 전체 허용**으로 본다(그 필드는 "이 키가 쓸 수 있는 앱"
이라 빈 값이 "제한 없음" 을 뜻하도록 설계됐다). 후자는 ``[]`` 가 **전면 거부**다.

⚠️ 이 파일의 존재 이유가 그 차이다. 두 함수를 하나로 합치거나, 후자를 falsiness
   (``if not allowed``)로 판정하면 ``[]`` 가 "제한 없음" 으로 뒤집힌다. 그리고 ``[]`` 는
   운영자가 콘솔에서 **마지막 앱의 체크를 해제**했을 때 만들어지는 값이다 — 화면은
   "허용된 앱 없음" 으로 보여주는데 게이트가 모든 앱을 통과시키는, 방금 내린 제한이
   아무 효과도 없는 상태가 된다. 원본 구현이 실제로 그 상태였다.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from app.services.router_service import check_client_model_scope, check_client_scope

_SRC = Path(__file__).resolve().parents[2] / "src" / "app"


class _Cfg:
    """``allowed_clients`` 만 갖는 모델 설정 대역."""

    def __init__(self, allowed):
        self.alias = "test-model"
        self.allowed_clients = allowed


# ─────────────────────────────────────────────────────────────────────────────
# 1. 3-상태 의미론
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("client", ["codex", "claude-code", "cowork", "other", None])
def test_none_means_unrestricted(client):
    """미설정 = 제한 없음. 지금 앱과 **나중에 추가될 앱** 전부."""
    check_client_model_scope(_Cfg(None), client)  # raise 하지 않아야 한다


@pytest.mark.parametrize("client", ["codex", "claude-code", "cowork", "other", None])
def test_empty_list_denies_every_client(client):
    """⚠️ 이게 핵심 단정이다 — ``[]`` 는 전면 거부다(fail-closed).

    falsiness 로 판정하면 이 테스트 5건이 모두 통과해 버린다(= 전부 허용).
    """
    with pytest.raises(PermissionError):
        check_client_model_scope(_Cfg([]), client)


def test_allowlist_permits_listed_and_denies_others():
    cfg = _Cfg(["codex"])
    check_client_model_scope(cfg, "codex")
    for denied in ("claude-code", "cowork", "other", None, "CODEX", "codex "):
        with pytest.raises(PermissionError):
            check_client_model_scope(cfg, denied)


def test_matching_is_exact_not_substring():
    """``codex`` 허용목록에 ``code`` 가 통과하면 안 된다.

    ``client in allowed`` 에서 allowed 가 **문자열**이면 부분 문자열 매칭이 된다.
    리스트여야 원소 동등 비교다.
    """
    with pytest.raises(PermissionError):
        check_client_model_scope(_Cfg(["codex"]), "code")


def test_denial_message_names_the_model_and_the_client():
    """운영자가 로그만 보고 어느 모델·어느 앱인지 알아야 한다."""
    with pytest.raises(PermissionError) as exc:
        check_client_model_scope(_Cfg(["codex"]), "cowork")
    msg = str(exc.value)
    assert "test-model" in msg and "cowork" in msg


def test_missing_attribute_is_treated_as_unrestricted():
    """0035 를 적용하지 않은 DB / 옛 캐시 엔트리에서도 오늘 동작이어야 한다.

    ⚠️ 여기서 거부로 떨어뜨리면 롤링 배포 중 **모든 요청이 400** 이 된다.
    """

    class _NoField:
        alias = "legacy"

    check_client_model_scope(_NoField(), "codex")


# ─────────────────────────────────────────────────────────────────────────────
# 2. 두 축이 섞이지 않았는지
# ─────────────────────────────────────────────────────────────────────────────


def test_the_user_axis_still_treats_empty_as_unrestricted():
    """사용자 × 앱 축은 예전 의미를 유지해야 한다 — 여기까지 fail-closed 로 바꾸면
    ``allowed_clients`` 를 설정하지 않은 **모든 기존 키**가 즉시 막힌다."""
    check_client_scope(None, "codex")
    check_client_scope([], "codex")
    check_client_scope([], None)


def test_the_two_gates_disagree_on_the_empty_case_on_purpose():
    """한 줄로 요약한 계약. 이 차이가 사라지면 둘 중 하나가 잘못된 것이다."""
    # 사용자 축: [] → 허용
    check_client_scope([], "codex")
    # 모델 축: [] → 거부
    with pytest.raises(PermissionError):
        check_client_model_scope(_Cfg([]), "codex")


def test_gate_uses_is_none_not_falsiness():
    """구현이 ``is None`` 인지 AST 로 확인한다.

    행동 테스트(위 ``test_empty_list_denies_every_client``)가 이미 잡지만, 이 단정은
    **왜 틀렸는지**를 실패 메시지로 말해 준다 — 다음 사람이 "빈 리스트도 제한 없음
    아닌가" 로 되돌리는 것을 막는다.
    """
    src = (_SRC / "services" / "router_service.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    # ⚠️ **모듈 레벨** 함수를 골라야 한다. 같은 이름의 클래스 메서드(위임 래퍼)가 있어서
    #    ast.walk 로 훑으면 마지막에 그 래퍼가 잡히고, 래퍼 본문에는 비교가 없으므로
    #    이 단정이 거짓 실패한다(실제로 그랬다).
    target = next(
        (n for n in tree.body if isinstance(n, ast.FunctionDef)
         and n.name == "check_client_model_scope"),
        None,
    )
    assert target is not None, "모듈 레벨 check_client_model_scope 를 찾지 못했다"

    body = list(target.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]
    code = ast.dump(ast.Module(body=body, type_ignores=[]))

    assert "Is()" in code, (
        "`is None` 비교가 없다 — falsiness 로 판정하면 [] 가 '제한 없음' 으로 뒤집혀 "
        "운영자가 방금 만든 전면 거부가 무효가 된다"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 3. 게이트가 실제로 요청 경로에 걸려 있는지
# ─────────────────────────────────────────────────────────────────────────────

_CALL_SITES = [
    "routers/openai_compat.py",
    "routers/messages.py",
    "routers/bedrock.py",
    "services/fallback_loop.py",
]


@pytest.mark.parametrize("rel", _CALL_SITES)
def test_gate_is_called_wherever_key_scope_is(rel):
    """``check_key_scope`` 가 걸린 곳마다 이 게이트도 걸려 있어야 한다.

    ⚠️ 한 지점만 빠지면 그 경로로만 제한이 우회된다 — 그리고 그 경로가 어느 것인지
       사용자는 알 수 있다(요청 형태를 바꿔 보면 된다). 부분 집행은 집행이 아니다.
    """
    src = (_SRC / rel).read_text(encoding="utf-8")
    tree = ast.parse(src)
    calls = [
        n.func.attr
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    ]
    key_n = calls.count("check_key_scope")
    model_n = calls.count("check_client_model_scope")
    assert key_n > 0, f"{rel}: check_key_scope 호출이 없다 — 이 테스트의 전제가 깨졌다"
    assert model_n >= key_n, (
        f"{rel}: check_key_scope {key_n}곳인데 check_client_model_scope 는 {model_n}곳 — "
        "빠진 경로로 모델×앱 제한이 우회된다"
    )


def test_main_inference_path_is_covered():
    """``/v1/messages`` 주 경로는 fallback_loop 를 타므로 거기 게이트가 있어야 한다.

    messages.py 자체의 호출은 ``count_tokens`` 뿐이다 — 그것만 보고 "배선됐다" 고
    판단하면 주 경로가 비어 있는 채로 통과한다.
    """
    fl = (_SRC / "services" / "fallback_loop.py").read_text(encoding="utf-8")
    assert "check_client_model_scope" in fl, (
        "fallback_loop 에 게이트가 없다 — /v1/messages 주 추론 경로가 통째로 우회된다"
    )
    msgs = (_SRC / "routers" / "messages.py").read_text(encoding="utf-8")
    assert "run_fallback_loop" in msgs, "주 경로가 fallback_loop 를 타지 않는다 — 전제 확인"


def test_schema_default_is_none_not_empty_list():
    """``ModelConfigSchema`` 는 Redis 캐시에서 **검증 없이** 복원된다.

    ⚠️ 기본값이 ``[]`` 면, 필드가 없는 옛 캐시 엔트리가 "전면 거부" 로 복원되어 배포
       직후 캐시가 갈리기 전까지 **모든 모델이 400** 이 된다.
    """
    from app.schemas.domain import ModelConfigSchema

    field = ModelConfigSchema.model_fields["allowed_clients"]
    assert field.default is None, (
        f"기본값이 {field.default!r} — 옛 캐시 엔트리가 전면 거부로 복원된다"
    )


def test_the_orm_actually_declares_the_column():
    """⚠️ 이 단정이 없어서 게이트 전체가 무력화된 채로 배포됐다.

    ``_orm_to_schema`` 는 ``getattr(alias_row, "allowed_clients", None)`` 로 읽는다.
    ORM 클래스가 컬럼을 선언하지 않으면 그 getattr 은 **언제나** None 을 돌려주고
    ``check_client_model_scope`` 는 즉시 return 한다. 그러면:

      - 관리자 화면은 "이 앱 차단" 으로 표시하고
      - admin-api 는 값을 실제로 DB 에 저장하고(그쪽 ORM 에는 컬럼이 있다)
      - 게이트웨이는 모든 앱을 계속 통과시킨다.

    오류도 경고도 없다. 아래 ``test_orm_conversion_reads_the_orm_attribute`` 는 이전에
    ``_orm_to_schema`` 의 **kwarg 이름만** AST 로 확인했기 때문에, 값이 영구히 None 인
    상태에서 통과했다 — 통과하는 테스트가 미집행을 보증서로 덮은 것이다.

    그래서 여기서는 파싱이 아니라 **매핑된 컬럼을 실행으로** 확인한다.
    """
    from sqlalchemy import ARRAY

    from app.models.model import ModelAlias

    columns = {c.name: c for c in ModelAlias.__table__.columns}
    assert "allowed_clients" in columns, (
        f"ModelAlias 에 allowed_clients 컬럼이 없다(현재: {sorted(columns)}) — "
        "모델 × 앱 게이트가 데이터 경로에서 조용히 무력화된다"
    )
    col = columns["allowed_clients"]
    assert isinstance(col.type, ARRAY), f"타입이 ARRAY 가 아니다: {col.type!r}"
    assert col.nullable, (
        "nullable=False 면 NULL(제한 없음)을 표현할 수 없어 3-state 가 2-state 로 붕괴한다"
    )


def test_admin_api_and_gateway_agree_on_the_column():
    """두 서비스의 ORM 이 같은 컬럼을 선언해야 한다.

    admin-api 만 선언하면 "쓰기는 되는데 집행은 안 되는" 정확히 그 상태가 된다.
    게이트웨이 쪽 소스를 문자열로 대조한다(두 서비스는 별개 venv 라 한 프로세스에서
    양쪽 모델을 import 하면 모듈명이 충돌한다 — ``app.models.model`` 이 양쪽에 있다).
    """
    admin_model = (
        _SRC.parents[2] / "admin-api" / "src" / "app" / "models" / "model.py"
    )
    if not admin_model.exists():  # 단독 체크아웃
        pytest.skip("admin-api 소스가 이 체크아웃에 없다")
    text = admin_model.read_text(encoding="utf-8")
    assert "allowed_clients" in text, (
        "admin-api ORM 에 allowed_clients 가 없다 — 관리자 화면의 저장이 컬럼에 닿지 않는다"
    )


def test_init_sql_adds_the_column_idempotently():
    """⚠️ ORM 컬럼만 추가하면 pre-0035 DB 의 **모든 모델 조회가** 500 이 된다.

    컬럼은 model_aliases 의 모든 SELECT 에 들어간다. init SQL 로만 만든 DB(compose,
    로컬, alembic 이 0035 에 닿지 않은 환경)에는 컬럼이 없으므로 UndefinedColumn 이
    나고, 그것은 allow-list 기능만이 아니라 **추론 경로 전체**를 죽인다.
    """
    init_sql = _SRC.parents[2] / "db" / "init" / "02_create_tables.sql"
    if not init_sql.exists():
        pytest.skip("db/init 이 이 체크아웃에 없다")
    text = init_sql.read_text(encoding="utf-8")
    stmt = re.search(
        r"ALTER TABLE model\.model_aliases\s+ADD COLUMN IF NOT EXISTS\s+allowed_clients\s+TEXT\[\]",
        text,
        re.I,
    )
    assert stmt is not None, (
        "db/init 에 allowed_clients 의 idempotent ALTER 가 없다 — "
        "0035 미적용 DB 에서 모든 모델 조회가 UndefinedColumn 으로 죽는다"
    )
    # DEFAULT '{}' 이면 모든 기존 alias 가 "어떤 앱도 불가" 로 바뀐다.
    assert "allowed_clients TEXT[] DEFAULT" not in text.replace("  ", " "), (
        "DEFAULT 가 붙어 있다 — '{}' 기본값은 모든 모델을 전면 거부로 만든다"
    )


def test_orm_conversion_reads_the_orm_attribute():
    """변환 함수가 필드를 빠뜨리면 게이트는 항상 None(제한 없음)을 본다.

    ⚠️ 이 검사만으로는 부족하다 — 위 ``test_the_orm_actually_declares_the_column`` 이
       함께 있어야 "kwarg 은 있는데 값이 영구 None" 인 상태를 잡는다. 둘 다 남겨 둔다:
       이건 배선을, 위 것은 데이터를 본다.
    """
    src = (_SRC / "services" / "router_service.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    target = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_orm_to_schema":
            target = node
    assert target is not None
    kwargs = {
        kw.arg
        for n in ast.walk(target)
        if isinstance(n, ast.Call)
        for kw in n.keywords
        if kw.arg
    }
    assert "allowed_clients" in kwargs, (
        "_orm_to_schema 가 allowed_clients 를 전달하지 않는다 — 컬럼이 죽는다"
    )
