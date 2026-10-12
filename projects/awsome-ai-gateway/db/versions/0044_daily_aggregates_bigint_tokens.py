# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""daily_aggregates 토큰/카운트 컬럼 INTEGER → BIGINT: usage.daily_aggregates

Revision ID: 0044
Revises: 0043
Create Date: 2026-10-12

## 왜 필요한가

집계 SQL 은 ``SUM(integer)`` 를 낸다 — PG 에서 이 결과 타입은 bigint 이다.
그 값을 INTEGER 컬럼에 넣다가 범위(2,147,483,647)를 넘으면 ``numeric value out
of range`` 로 **INSERT…SELECT 전체**가 실패한다. 하루치를 단일 구문으로 쓰므로
한 사용자·모델 조합이 하루 cache_read 토큰 21억을 넘기면(자동화 에이전트의
캐시 재사용 워크로드에서 실현 가능) **그 날짜의 집계가 전 사용자에 대해 실패**하고,
백필도 같은 SQL 이라 매일 같은 지점에서 실패해 영구 공백이 된다.

usage_logs 는 행 단위라 21억 토큰/요청은 비현실적이라 INTEGER 유지 —
daily_aggregates 만 넓힌다.

## 배포 순서

⚠️ INTEGER→BIGINT 는 저장 폭(4→8B)이 달라 **테이블 전체 재작성 + 해당 컬럼
인덱스 재생성**이 일어나고, 그동안 ACCESS EXCLUSIVE 락이 걸린다. 그래서
컬럼 6 개를 **한 ALTER TABLE 문**으로 묶어 재작성을 1 회로 줄인다(컬럼별
문장이면 재작성이 6 번 반복). daily_aggregates 가 큰 배포에선 마이그레이션
Job 실행 중 이 테이블의 조회/집계가 잠시 멈춘다 — 배포 창을 저부하 시간으로.
읽기/쓰기 스키마가 느슨한 숫자라 코드 배포 순서 제약은 없음.
"""

from alembic import op

revision = "0044"
down_revision = "0043"
branch_labels = None
depends_on = None

_TABLE = "usage.daily_aggregates"
_COLUMNS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_tokens",
    "cache_read_tokens",
    "total_tokens",
    "request_count",
)


def upgrade() -> None:
    # 한 문장 — 재작성 1 회 + 락 점유 최소화.
    alters = ", ".join(f"ALTER COLUMN {c} TYPE BIGINT" for c in _COLUMNS)
    op.execute(f"ALTER TABLE {_TABLE} {alters}")


def downgrade() -> None:
    # overflow 행이 있으면 downgrade 는 실패한다 — 그 경우가 바로 이
    # 마이그레이션의 존재 이유이므로 별도 변환을 두지 않는다.
    alters = ", ".join(f"ALTER COLUMN {c} TYPE INTEGER" for c in _COLUMNS)
    op.execute(f"ALTER TABLE {_TABLE} {alters}")
