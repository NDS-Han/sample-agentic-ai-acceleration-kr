# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import hashlib
import time
from abc import ABC
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Protocol

import structlog
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from worker.models.notification import NotificationLog
from worker.observability.metrics import WorkerMetrics
from worker.schemas.events import EventType, NotificationEvent, ServiceSource
from worker.schemas.recipients import Recipient, RecipientRole, RenderedEmail
from worker.senders.base import EmailSendError, EmailSender
from worker.services.config_cache import ConfigCache
from worker.services.email_mask import mask_email
from worker.services.recipient_resolver import RecipientResolver
from worker.services.retry_executor import RetryExecutor
from worker.services.template_engine import TemplateEngine

if TYPE_CHECKING:
    from redis.asyncio import Redis

    from worker.models.auth import User as _UserT  # noqa: F401  (타입 힌트용)

logger = structlog.get_logger(__name__)


class NotificationInfraError(Exception):
    """인프라(DB) 장애로 이벤트 처리가 불가능하다는 표시.

    ChannelListener 가 이 예외를 받으면 이벤트를 NotificationBufferQueue 에 넣어
    DB 복구 후 재처리한다. Pub/Sub 은 재전송이 없으므로 삼키면 영구 유실이다.
    """


class EventHandler(Protocol):
    """이벤트 핸들러 인터페이스."""

    async def handle(self, event: NotificationEvent) -> None: ...


def _is_infra_error(exc: BaseException) -> bool:
    """DB/인프라 계열 오류인지 — 처리 로직 버그와 구분한다."""
    return isinstance(exc, (SQLAlchemyError, NotificationInfraError))


# dedup 키로 쓸 페이로드의 "정체성" 필드 — 같은 알림의 재발행에서 불변인 값만 쓴다.
# failure_count·timestamp 같이 매번 바뀌는 값을 넣으면 dedup 이 무력화된다.
_DEDUP_PAYLOAD_FIELDS = (
    "source_ip",
    "user_id",
    "team_id",
    "key_id",
    "threshold_pct",
    "threshold",
    "scope",
    "service",
    "provider",
    "model",
    "resource",
)


def _cooldown_key(event: NotificationEvent) -> str | None:
    """이벤트의 쿨다운 dedup 키 — 정체성 필드가 하나도 없으면 dedup 불가(None)."""
    parts = [
        f"{f}={event.payload[f]}"
        for f in _DEDUP_PAYLOAD_FIELDS
        if event.payload.get(f) is not None
    ]
    if not parts:
        return None
    digest = hashlib.sha1("|".join(sorted(parts)).encode()).hexdigest()
    return f"notif:cooldown:{event.type.value}:{digest}"


class BaseHandler(ABC):
    """이메일 알림 이벤트 처리 오케스트레이터 (Wave 4).

    config 조회 → 수신자 결정 → 템플릿 렌더링 → DB 로그 생성 →
    이메일 전송(재시도 포함) → 결과 기록 순서로 처리한다.

    DB 장애 시 이메일 전송은 계속 진행하며, 전송 결과는 최선(best-effort)으로 기록한다.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        config_cache: ConfigCache,
        recipient_resolver: RecipientResolver,
        template_engine: TemplateEngine,
        email_sender: EmailSender,
        retry_executor: RetryExecutor,
        metrics: WorkerMetrics | None = None,
        redis_client: Redis | None = None,
        cooldown_seconds: int = 0,
    ) -> None:
        self._session_factory = session_factory
        self._config_cache = config_cache
        self._recipient_resolver = recipient_resolver
        self._template_engine = template_engine
        self._email_sender = email_sender
        self._retry_executor = retry_executor
        self._metrics = metrics
        self._redis = redis_client
        self._cooldown_seconds = cooldown_seconds

    async def handle(self, event: NotificationEvent) -> None:
        """NotificationEvent 하나를 end-to-end로 처리한다."""
        start = time.monotonic()

        # 1. 설정 조회
        config = self._config_cache.get(event.type.value)
        if config is None:
            logger.warning("event_type_not_configured", event_type=event.type.value)
            return
        if not config.enabled:
            logger.info("notification_disabled", event_type=event.type.value)
            return

        # 1b. 쿨다운 dedup — auth_failure_spike 같은 카운터형 이벤트는 발행자가
        #     임계 도달마다 새 event_id 로 재발행하고 워커/pod × uvicorn 워커 수만큼
        #     뻥튀기된다. Redis SETNX 로 같은 정체성 알림을 cooldown 창 안에서 1회만
        #     발송한다. Pub/Sub 특성상 실제 재전송은 없으므로 이 dedup 은 "폭주 억제"
        #     용도다 (notification_logs 의 (event_id, recipient) 유니크 dedup 과 별개).
        cooldown_key = await self._claim_cooldown(event)
        if cooldown_key is False:
            return  # 쿨다운 창 안 — 이미 발송된(또는 발송 중인) 알림

        # 아래 블록이 어떤 이유로든(취소·버그·인프라) 예외로 빠져나가면 쿨다운
        # 키를 해제한다 — 슬롯 INSERT 전에 멈춘 경우 행도 키만 남아 replica
        # fan-out 재처리가 억제돼 이벤트가 유실된다. 정상 완료·스킵 경로는
        # 키를 유지해 폭주 억제가 동작한다.
        try:
            # 2. 수신자 결정 — DB 장애는 NotificationInfraError 로 전환해
            #    리스너가 이벤트를 버퍼에 넣게 한다 (Pub/Sub 재전송 없음 →
            #    삼키면 영구 유실).
            try:
                recipients = await self._recipient_resolver.resolve(
                    roles=config.recipient_roles,
                    payload=event.payload,
                )
            except SQLAlchemyError as exc:
                raise NotificationInfraError("recipient resolution failed") from exc
            if not recipients:
                logger.warning("no_recipients_resolved", event_type=event.type.value)
                return

            # 3. 수신자별 처리 — 한 수신자의 렌더/DB/기타 실패가 나머지 수신자를
            #    데려가지 않도록 수신자 단위로 격리한다(예외 전파 시 잔여 수신자 전부
            #    미발송이 되던 결함).
            infra_errors = 0
            for recipient in recipients:
                try:
                    await self._deliver_to_recipient(event, recipient)
                except Exception as exc:
                    if _is_infra_error(exc):
                        infra_errors += 1
                    logger.exception(
                        "recipient_processing_failed",
                        event_type=event.type.value,
                        recipient=mask_email(getattr(recipient, "email", "?")),
                    )
                    if self._metrics:
                        self._metrics.errors_total.add(1, {"error_type": "recipient"})

            # 3-end. 전 수신자가 인프라 오류로 실패 = DB 다운 — 전체 이벤트를
            #        버퍼로 돌려 복구 후 재처리한다. 일부만 실패면 성공분은 보내졌고
            #        실패분의 pending 행은 sweeper 가 회수한다.
            if infra_errors and infra_errors == len(recipients):
                raise NotificationInfraError(
                    f"all {infra_errors} recipients failed on infra errors"
                )
        except BaseException:
            await self._release_cooldown(cooldown_key)
            raise

        # 4. 처리 완료 메트릭
        if self._metrics:
            self._metrics.events_processed_total.add(
                1, {"event_type": event.type.value, "status": "success"}
            )
            duration = time.monotonic() - start
            self._metrics.event_processing_duration.record(
                duration, {"event_type": event.type.value}
            )

    async def _claim_cooldown(self, event: NotificationEvent) -> str | bool | None:
        """쿨다운 키를 SETNX 한다.

        Returns:
            str: 선점 성공 — 이어서 처리하고, 실패로 되돌릴 땐 이 키를 지운다.
            False: 쿨다운 창 안 — 이벤트를 스킵한다.
            None: dedup 불가(키 없음/미구성/Redis 오류) — 그냥 처리한다.
        """
        if self._redis is None or self._cooldown_seconds <= 0:
            return None
        key = _cooldown_key(event)
        if key is None:
            return None
        try:
            claimed = await self._redis.set(key, "1", nx=True, ex=self._cooldown_seconds)
        except Exception:
            # dedup 못 해도 발송 자체는 진행 — 중복 알림이 유실보다 낫다.
            logger.warning("cooldown_claim_failed_fail_open", event_type=event.type.value)
            return None
        if not claimed:
            logger.info(
                "notification_cooldown_suppressed",
                event_type=event.type.value,
                event_id=event.event_id,
            )
            if self._metrics:
                self._metrics.errors_total.add(1, {"error_type": "cooldown_suppressed"})
            return False
        return key

    async def _release_cooldown(self, key: str | bool | None) -> None:
        """인프라 실패로 이벤트를 되돌릴 때 쿨다운 키를 해제 — 안 지우면
        버퍼 드레인/스위퍼의 재처리가 창 안에서 억제돼 이벤트가 유실된다."""
        if not isinstance(key, str) or self._redis is None:
            return
        try:
            await self._redis.delete(key)
        except Exception:
            logger.warning("cooldown_release_failed", key=key)

    async def _deliver_to_recipient(self, event: NotificationEvent, recipient) -> None:
        """단일 수신자 처리 — 호출자가 예외를 수신자 단위로 격리한다."""
        async with self._session_factory() as session:
                # 3a. 템플릿 컨텍스트 구성
                context = TemplateEngine.build_context(
                    event, recipient.name, recipient.email
                )

                # 3b. 렌더링
                subject, html_body = self._template_engine.render(
                    event.type.value, context
                )

                # 3c. NotificationLog 생성
                log = NotificationLog(
                    event_id=event.event_id,
                    event_type=event.type.value,
                    channel="email",
                    recipient_email=recipient.email,
                    recipient_user_id=recipient.user_id,
                    subject=subject,
                    status="pending",
                    attempt_count=0,
                    event_payload=event.payload,
                )

                # 3d. DB 저장 — 유니크 슬롯 선점으로 중복 발송 방지.
                # ⚠️ ux_notification_logs_event_recipient 유니크 인덱스가
                #    (event_id, event_type, recipient_email) 슬롯을 여기서 선점한다.
                #    같은 이벤트의 재배송(cost-recorder replay 재발행, pub/sub
                #    다중 구독 등)은 IntegrityError → **중복 메일 발송 없이 스킵**.
                #    단 IntegrityError 가 'pending' dead-claim 이면(아래 takeover
                #    분기) 인수해 발송한다 — 예전엔 스킵해 그 수신자가 영구
                #    미발송이었다.
                # ⚠️ IntegrityError **외** 실패(일시 DB 끊김/타임아웃)는 1회 재시도해
                #    슬롯을 다시 잡는다 — 예전엔 슬롯 없이 발송해 재배송 시 중복 메일이
                #    나갔다(R2-6). 그래도 실패하면 **발송을 우선**한다 — 알림 채널이
                #    lossy pub/sub 이라 스킵하면 영구 유실이 되므로, 지속적 DB 장애
                #    하에서는 "중복 가능한 발송"이 "유실"보다 낫다(운영 결정).
                duplicate = False
                slot_claimed = False
                for _claim_attempt in (1, 2):
                    try:
                        session.add(log)
                        await session.commit()
                        slot_claimed = True
                        break
                    except IntegrityError:
                        await session.rollback()
                        duplicate = True
                        break
                    except Exception as exc:
                        await session.rollback()
                        logger.warning(
                            "dedup_slot_claim_failed",
                            attempt=_claim_attempt,
                            event_type=event.type.value,
                            recipient=mask_email(recipient.email),
                            error=str(exc),
                        )
                if duplicate:
                    # IntegrityError 는 두 경우다.
                    #   (a) 진짜 재배송 — 상태가 확정된(sent/failed) 행이 이미 있으면 스킵.
                    #   (b) dead-claim — 이전 시도가 INSERT 후 send 전 crash 했거나,
                    #       commit 자체는 성공했는데 응답이 유실돼 재시도가 슬롯을 다시
                    #       잡으려다 충돌한 경우. 행은 'pending' 으로 남는다.
                    # (b) 를 그대로 스킵하면 이 수신자는 영구 미발송이다 —
                    # 발송 우선 원칙(위 주석)에 따라 pending 행을 인수해 발송하고
                    # 그 행을 갱신한다.
                    # ⚠️ 'pending' 이라고 다 dead-claim이 아니다 — **지금 진행 중인**
                    #    정상 발송도 INSERT commit~최종 갱신 사이에는 'pending'이다.
                    #    그 행을 인수하면 두 워커가 같은 메일을 동시 발송한다.
                    #    발송 창의 상한 = 재시도 3회×(connect10+read30)+backoff7s ≈
                    #    130s — 그 두 배(300s)보다 오래된 pending 만 dead 로 인수한다.
                    #    ⚠️ 나이는 created_at 이 아니라 **최근 활동**(last_attempt_at)
                    #    기준이다 — 인수한 워커가 아래에서 하트비트를 찍으므로,
                    #    created_at 만 보면 다른 워커가 지금 인수해 처리 중인
                    #    오래된 행을 재인수해 중복 발송한다.
                    # 인수는 조건부 UPDATE 단일문으로 한다 — read→write 두 단계면
                    # 두 replica 가 같은 stale 행을 동시에 읽고 둘 다 발송한다.
                    _DEAD_CLAIM_AGE_SEC = 300.0
                    now = datetime.now(timezone.utc)
                    claimed_id = (
                        await session.execute(
                            update(NotificationLog)
                            .where(
                                NotificationLog.event_id == event.event_id,
                                NotificationLog.event_type == event.type.value,
                                NotificationLog.recipient_email == recipient.email,
                                NotificationLog.status == "pending",
                                func.coalesce(
                                    NotificationLog.last_attempt_at,
                                    NotificationLog.created_at,
                                )
                                < now - timedelta(seconds=_DEAD_CLAIM_AGE_SEC),
                            )
                            .values(
                                last_attempt_at=now,
                                attempt_count=NotificationLog.attempt_count + 1,
                            )
                            .returning(NotificationLog.id)
                        )
                    ).scalar_one_or_none()
                    if claimed_id is not None:
                        logger.warning(
                            "dedup_dead_claim_takeover",
                            event_id=event.event_id,
                            event_type=event.type.value,
                            recipient=mask_email(recipient.email),
                        )
                        # 인수 하트비트(UPDATE 값)를 즉시 커밋해 다른 워커가
                        # 이 행을 '살아있는 인수'로 보고 재인수하지 않게 한다.
                        try:
                            await session.commit()
                        except Exception:
                            # 하트비트 실패해도 발송은 진행 — 최악은 중복 인수
                            # 가능성으로 회귀하는 것뿐이고 발송 유실보다 낫다.
                            logger.warning(
                                "dedup_takeover_heartbeat_failed",
                                event_id=event.event_id,
                                recipient=mask_email(recipient.email),
                            )
                        log = (
                            await session.execute(
                                select(NotificationLog).where(
                                    NotificationLog.id == claimed_id
                                )
                            )
                        ).scalar_one()
                        slot_claimed = True
                    else:
                        logger.info(
                            "duplicate_event_skipped",
                            event_id=event.event_id,
                            event_type=event.type.value,
                            recipient=mask_email(recipient.email),
                        )
                        return
                if not slot_claimed:
                    if self._metrics:
                        self._metrics.errors_total.add(1, {"error_type": "db"})
                    logger.warning(
                        "dedup_slot_unclaimed_sending_anyway",
                        event_type=event.type.value,
                        recipient=mask_email(recipient.email),
                    )

                await self._send_and_finalize(
                    session, event, recipient, log, subject, html_body
                )

    async def resend_log_entry(
        self, log_id: str, claimed_at: datetime | None = None
    ) -> None:
        """sweeper 가 회수한 stuck 행을 재발송한다.

        sweeper 가 조건부 UPDATE 로 행을 'pending' + attempt_count+1 상태로
        클레임해 둔 뒤 호출한다 — 여기서는 새 슬롯을 잡지 않고 기존 행을 갱신한다.

        ``claimed_at`` 은 스위퍼 클레임이 찍은 last_attempt_at 펜싱 토큰이다.
        클레임~재발송 사이 다른 replica 의 스위퍼/리플레이가 같은 행을 인수하면
        토큰이 달라지므로, 발송 전 조건부 UPDATE 로 재검증해 이중 발송을 막는다.
        """
        async with self._session_factory() as session:
            if claimed_at is not None:
                fenced = (
                    await session.execute(
                        update(NotificationLog)
                        .where(
                            NotificationLog.id == log_id,
                            NotificationLog.status == "pending",
                            NotificationLog.last_attempt_at == claimed_at,
                        )
                        .values(last_attempt_at=datetime.now(timezone.utc))
                        .returning(NotificationLog.id)
                    )
                ).scalar_one_or_none()
                if fenced is None:
                    logger.info(
                        "resend_claim_lost",
                        log_id=log_id,
                    )
                    return
                await session.commit()  # 재하트비트 — 발송 창 동안 stale 방지

            log = (
                await session.execute(
                    select(NotificationLog).where(NotificationLog.id == log_id)
                )
            ).scalar_one_or_none()
            if log is None or log.status != "pending":
                return  # 다른 워커가 이미 처리했거나 상태가 바뀜

            config = self._config_cache.get(log.event_type)
            if config is None or not config.enabled:
                # 'skipped' 는 sweeper 클레임 조건(pending/failed)에 안 걸리는
                # 터미널 상태 — 매 스윕마다 재회수되는 것을 막는다.
                log.status = "skipped"
                log.error_message = "resend_skipped_config_disabled"
                log.resolved_at = datetime.now(timezone.utc)
                await session.commit()
                return

            # 수신자 표시명 — 발송 시점 표시명 조회가 안 되면 이메일로 대체.
            name = log.recipient_email
            if log.recipient_user_id:
                from worker.models.auth import User

                display = (
                    await session.execute(
                        select(User.display_name).where(
                            User.id == str(log.recipient_user_id)
                        )
                    )
                ).scalar_one_or_none()
                if display:
                    name = display

            # 원본 발행자 source 는 로그에 보존되지 않는다 — 템플릿은
            # event.type/payload 만 쓰므로 복원용 고정값으로 충분하다.
            try:
                event_type = EventType(log.event_type)
            except ValueError:
                log.status = "skipped"
                log.error_message = "resend_skipped_unknown_event_type"
                log.resolved_at = datetime.now(timezone.utc)
                await session.commit()
                return
            event = NotificationEvent(
                event_id=log.event_id,
                type=event_type,
                timestamp=datetime.now(timezone.utc),
                source=ServiceSource.ADMIN_API,
                payload=log.event_payload or {},
            )
            recipient = Recipient(
                email=log.recipient_email,
                name=name,
                user_id=log.recipient_user_id,
                role=RecipientRole.AFFECTED_USER,
            )

            context = TemplateEngine.build_context(event, name, log.recipient_email)
            subject, html_body = self._template_engine.render(
                log.event_type, context
            )
            await self._send_and_finalize(
                session, event, recipient, log, subject, html_body
            )

    async def _send_and_finalize(
        self,
        session: AsyncSession,
        event: NotificationEvent,
        recipient,
        log: NotificationLog,
        subject: str,
        html_body: str,
    ) -> None:
        """렌더된 메일을 재시도 포함 발송하고 결과를 행에 반영한다."""
        rendered = RenderedEmail(
            subject=subject,
            html_body=html_body,
            recipient=recipient,
        )

        try:
            await self._retry_executor.execute(
                fn=lambda r=rendered: self._email_sender.send(r),
                event_type=event.type.value,
            )
            log.status = "sent"
            log.resolved_at = datetime.now(timezone.utc)
            if self._metrics:
                self._metrics.emails_sent_total.add(
                    1, {"event_type": event.type.value, "status": "sent"}
                )
        except Exception as exc:
            # 재시도 불가 오류(잘못된 주소 등)는 'rejected' — sweeper 클레임
            # 조건에 안 걸리는 터미널 상태라 영구 실패를 6번 재시도하지 않는다.
            log.status = (
                "failed"
                if not isinstance(exc, EmailSendError) or exc.retryable
                else "rejected"
            )
            log.error_message = str(exc)
            log.resolved_at = datetime.now(timezone.utc)
            if self._metrics:
                self._metrics.emails_sent_total.add(
                    1, {"event_type": event.type.value, "status": "failed"}
                )
            logger.error(
                "email_send_failed",
                event_type=event.type.value,
                recipient=mask_email(recipient.email),
                error=str(exc),
            )

        log.last_attempt_at = datetime.now(timezone.utc)
        try:
            await session.commit()
        except Exception as exc:
            logger.error(
                "db_write_failed",
                event_type=event.type.value,
                recipient=mask_email(recipient.email),
                error=str(exc),
            )
            if self._metrics:
                self._metrics.errors_total.add(1, {"error_type": "db"})
