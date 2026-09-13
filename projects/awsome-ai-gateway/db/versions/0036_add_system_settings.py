# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""런타임에 바꿀 수 있는 전역 설정 저장소: public.system_settings

Revision ID: 0036
Revises: 0035
Create Date: 2026-09-13

## 왜 필요한가

이 레포의 모든 전역 on/off 는 **env 전용**이다(``core/config.py`` 의 lru_cache 된
Settings). 그래서 하나를 뒤집으려면 helm value 를 고치고 파드를 롤링해야 한다 —
장애 대응 중에 그 왕복은 비싸고, "지금 당장 끄기" 가 필요한 종류의 스위치에는 맞지
않는다.

이 테이블은 재시작을 넘어 살아남아야 하는 **운영자 조작 가능한** 설정을 담는다.
DB 가 진실의 원천이고 Redis 는 그 앞의 읽기 캐시다(쓰기 시 write-through).

첫 소비자는 body logging on/off(``key='body_logging_enabled'``)다.

## 행이 없는 것 = "미설정"

소비자가 기본값을 정한다. body logging 은 **OFF** 가 기본이다 — 프롬프트 본문을
수집하는 기능이 "설정을 못 읽었다" 는 이유로 켜지면 안 된다(fail-safe 방향).

## value 가 JSONB 인 이유

지금 필요한 것은 boolean 하나지만, 나중 플래그가 더 풍부한 설정(대상 목록, 샘플링
비율 등)을 담을 수 있어야 한다. 컬럼을 타입별로 늘리는 대신 한 컬럼으로 둔다.

⚠️ 그 대가: 값의 형상을 DB 가 검증하지 않는다. 읽는 쪽이 방어적으로 파싱해야 하고,
   실제로 그렇게 한다(``body_log_flag`` 는 파싱 실패를 OFF 로 떨어뜨린다).

## updated_by

``auth.users(id)`` 를 참조하는 nullable 컬럼. 누가 언제 바꿨는지 남긴다.

⚠️ 이 한 셀은 **감사 로그가 아니다** — 다음 변경이 덮어쓴다. 프라이버시에 영향을 주는
   스위치(본문 수집 등)는 ``audit.audit_logs`` 에 불변 행을 함께 남겨야 한다.
   그 요구는 이 마이그레이션 밖(라우터)의 책임이다.

## 왜 0036 인가 (번호 충돌 기록)

원본 레포에서 이 마이그레이션의 revision 은 ``0017`` 이었다. 이 트리의 ``0017`` 은
Codex 라우팅 데이터 마이그레이션이라 **완전히 다른 내용이고 같은
(revision, down_revision) 쌍**을 갖는다. 그대로 가져오면 alembic 이 다중 head 로
exit 255 이고 마이그레이션 Job 은 아무것도 적용하지 못한다(0035 의 같은 기록 참조).
"""

from alembic import op

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS public.system_settings (
            key         TEXT        PRIMARY KEY,
            value       JSONB       NOT NULL,
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_by  UUID        REFERENCES auth.users(id)
        )
        """
    )


def downgrade() -> None:
    # ⚠️ 테이블을 떨어뜨리면 설정이 함께 사라진다. 그래서 downgrade 후 다시 upgrade 하면
    #    모든 플래그가 "미설정" 으로 돌아간다 — body logging 은 OFF 가 기본이므로 그
    #    방향은 안전하다(켜져 있던 수집이 멈춘다). 반대 방향의 플래그를 추가할 때는
    #    이 성질을 다시 생각해야 한다.
    op.execute("DROP TABLE IF EXISTS public.system_settings")
