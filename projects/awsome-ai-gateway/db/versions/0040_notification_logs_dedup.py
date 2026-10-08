# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""알림 재배송 멱등: notification.notification_logs (event_id, event_type, recipient_email) 유니크

Revision ID: 0040
Revises: 0039
Create Date: 2026-10-08

## 무엇을 추가하나

``notification_logs`` 는 같은 이벤트가 재배송돼도 별도 행으로 쌓였다 —
``event_id`` 에 인덱스만 있고 중복 차단 제약이 없었다. 재배송 경로:

  * cost-recorder-worker 부분 replay 가 threshold 이벤트를 `notifications:budget`
    에 재발행
  * Redis PUB/SUB 는 구독자 전체에 브로드캐스트 → notification-worker 다중
    replica 가 모두 같은 이벤트를 수신

핸들러는 로그 INSERT 시점에 이 인덱스로 슬롯을 선점하고, IntegrityError 를
중복 배송으로 처리해 발송을 건너뛴다 (handlers/base.py 3d 주석 참조).

## 기존 데이터

유니크 인덱스 생성 전에 (event_id, event_type, recipient_email) 중복 행을
가장 오래된 것만 남기고 정리한다 — 기존 중복이 있으면 CREATE UNIQUE INDEX
자체가 실패해 배포가 막힌다.

## 되돌리기

인덱스를 내리면 재배송이 다시 별도 행으로 쌓인다(중복 메일 재발). 데이터는
보존된다.
"""

from alembic import op

revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 기존 중복 정리 — 같은 (event_id, event_type, recipient_email) 에서
    # created_at 가장 빠른 행만 남긴다.
    op.execute(
        """
        DELETE FROM notification.notification_logs a
        USING notification.notification_logs b
        WHERE a.event_id = b.event_id
          AND a.event_type = b.event_type
          AND a.recipient_email = b.recipient_email
          -- created_at 동률 대비 id tiebreak — 없으면 같은 타임스탬프 중복이 살아남는다
          AND (a.created_at > b.created_at
               OR (a.created_at = b.created_at AND a.id > b.id))
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_notification_logs_event_recipient
            ON notification.notification_logs (event_id, event_type, recipient_email)
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP INDEX IF EXISTS notification.ux_notification_logs_event_recipient"
    )
