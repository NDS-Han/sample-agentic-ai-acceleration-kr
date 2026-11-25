# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 흔히 쓰는 비-IANA 약어/레거시 alias → 정규 IANA 이름 힌트 (검증은 그대로 엄격하게
# 유지하되, 실수했을 때 바로 고칠 수 있도록 에러 메시지에 제안을 붙인다).
_TZ_ALIAS_HINTS: dict[str, str] = {
    "KST": "Asia/Seoul",
    "JST": "Asia/Tokyo",
    "IST": "Asia/Kolkata",
    "PST": "America/Los_Angeles",
    "PDT": "America/Los_Angeles",
    "EST": "America/New_York",
    "EDT": "America/New_York",
    "CST": "America/Chicago",
    "CDT": "America/Chicago",
    "MST": "America/Denver",
    "MDT": "America/Denver",
    "GMT": "UTC",
    "US/PACIFIC": "America/Los_Angeles",
    "US/EASTERN": "America/New_York",
    "US/CENTRAL": "America/Chicago",
    "US/MOUNTAIN": "America/Denver",
}


def _reporting_timezone_error(v: str) -> str:
    """reporting_timezone 검증 실패 메시지 — 흔한 실수(약어/레거시 alias/대소문자)에
    대해 정규 IANA 이름을 제안한다."""
    hint = _TZ_ALIAS_HINTS.get(v.strip().upper())
    if hint is None and v.upper() == "UTC" and v != "UTC":
        hint = "UTC"  # 대소문자 오타 (예: "utc")
    msg = f"Invalid reporting_timezone {v!r}: not a valid IANA timezone name"
    if hint:
        msg += f" (did you mean {hint!r}?)"
    msg += ". Use a canonical IANA name, e.g. 'Asia/Seoul', 'UTC', 'America/Los_Angeles'."
    return msg


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Database — gateway-proxy와 동일 schema, 쓰기 가능한 사용자 필요
    db_url: str = "postgresql+asyncpg://gateway:gateway_dev_password@postgres:5432/gateway"
    db_pool_size: int = 10
    db_pool_overflow: int = 5
    # 풀 고갈 대기 상한/커넥션 재생성 주기. 차트의 공통/서비스별 DB pool env 를 읽는다.
    db_pool_timeout: int = 10
    db_pool_recycle: int = 1500
    db_ssl_mode: str = "disable"
    # RDS Proxy 경유 시 0 으로 설정 (PostgreSQL pinning 회피). Aurora 직접 연결 시엔 기본값 유지.
    db_statement_cache_size: int = 100
    # asyncpg connect/command 상한 — 블랙홀 장애(무응답)에서 asyncpg 기본값
    # (connect 60s·명령 무제한) 대기를 막는다. 배치/집계 쿼리라 command 는 60s.
    db_connect_timeout: float = 5.0
    db_command_timeout: float = 60.0

    # Redis
    redis_url: str = "redis://redis:6379/0"
    redis_tls_enabled: bool = False

    # Cost stream configuration — gateway-proxy `services/cost_recorder.py` 와 일치해야 함
    cost_stream_key: str = "cost:stream"
    cost_stream_group: str = "cost-recorder-workers"
    cost_stream_consumer: str = "worker-1"  # replica 구분 (prod에서 hostname 기반 권장)

    # Batch flush: 두 조건 중 먼저 도달한 것으로 flush
    batch_max_size: int = 100  # entries
    batch_max_interval_sec: float = 5.0
    xread_block_ms: int = 5_000  # XREADGROUP BLOCK 대기
    #: 다른 consumer 이름의 PEL 에 남은 메시지를 회수(XAUTOCLAIM)하는 주기(초).
    #: ⚠️ 왜 필요한가: consumer 이름이 파드 이름이고 파드 이름은 롤아웃·축출·OOM 재시작마다
    #:    바뀐다. XREADGROUP id='0' 은 Redis 의미상 **자기 이름의 PEL 만** 돌려주므로,
    #:    죽은 파드가 남긴 unacked 메시지는 새 파드가 영원히 보지 못한다. cost:stream 은
    #:    MAXLEN~500_000 으로 트림되므로 결국 데이터 자체가 사라진다 — usage_logs 행도,
    #:    budget_usages 차감도, 일별 카운터도 없다.
    xautoclaim_interval_sec: float = 60.0
    #: 회수 대상의 최소 idle 시간(ms). 살아 있는 형제 replica 의 진행 중 배치를 빼앗지
    #: 않도록 넉넉히 둔다. 겹쳐 회수되더라도 배치 flusher 의 재처리 필터가 이중청구를 막는다.
    xautoclaim_min_idle_ms: int = 300_000
    #: 같은 배치 머리가 이 시간(초) 넘게 계속 flush 실패하면 결정적 오류로 보고
    #: dead-letter(행 단위 소화)로 전환한다. 그냥 재시도하면 스트림 전체 봉쇄 →
    #: MAXLEN 트림 전량 유실. 일시 DB 장애는 이 창 안에서 자연 복구된다.
    stream_dead_letter_grace_sec: float = 120.0

    # Daily aggregator cron. 기본 매일 00:10 (reporting_timezone 기준).
    daily_usage_agg_cron: str = "10 0 * * *"
    # 매 실행마다 무조건 재집계하는 최근 완료 일수(어제 + 그제).
    # ⚠️ 예전엔 8일이었다 — 늦게 도착한 행을 놓치지 않으려는 안전망이었는데, 0061 부터는
    #    늦은 행이 같은 트랜잭션에서 usage.rollup_dirty_marks 를 남기고 시간 잡이 나이와 무관하게
    #    그 날짜를 다시 집계한다. 8일 무조건 재집계는 cold 기준 8×47~88s(최대 ~12분)를 매일
    #    쓰면서 같은 결과를 다시 만들 뿐이라 2일로 줄인다(env 로 되돌릴 수 있다).
    daily_usage_agg_recent_days: int = 2
    # 통째로 누락된 과거 날짜(크론 실패·워커 다운)를 탐지·백필하는 lookback 일수.
    # 기동 점검도 이 창 안에서만 본다(전체 이력 스캔 없음 — 그 밖은 백필 CLI).
    daily_usage_agg_backfill_lookback_days: int = 30
    # 집계 전용 엔진(워커 flush 엔진과 분리) — cold 하루 집계 47~88s(1.36M 행/일 실측)가
    # 워커 command_timeout(60s)에 걸려 실패하던 문제. 하루/한 시간 집계 문장의 상한이다
    # (asyncpg command_timeout + 트랜잭션 안 SET LOCAL statement_timeout).
    daily_usage_agg_command_timeout: float = 900.0
    # 집계 전용 풀 — advisory lock 커넥션 1 + 작업 세션 1 + 동시에 뜬 다른 잡의 lock 시도 1.
    daily_usage_agg_pool_size: int = 3
    # 야간 잡이 시간 잡·기동 점검의 advisory lock 을 기다리는 시간(초). 시간 잡은 기다리지 않는다.
    daily_usage_agg_nightly_lock_wait_sec: float = 900.0
    # 실패한 날짜(failed_at)를 시간 잡이 다시 시도하기까지의 대기(초). 야간 잡은 항상 시도한다.
    daily_usage_agg_failed_retry_sec: int = 10_800
    # 시간 롤업 잡 — 오늘(과 일 집계가 아직 없는 최근 날짜)의 완료된 시간 + dirty 날짜 재집계.
    hourly_rollup_enabled: bool = True
    hourly_rollup_cron: str = "5 * * * *"
    # 일 집계가 신뢰 불가일 때 시간 롤업으로 메울 최근 날짜 수(어제 야간 잡 전 등).
    daily_usage_agg_hourly_bridge_days: int = 2
    # 늦은 행 판정 여유(초) — 행의 시간 버킷 끝이 now + 이 값 이전이면 표시한다. flush
    # 트랜잭션이 느리게 커밋돼 시간 잡 스냅샷 뒤에 보이는 경우까지 덮는다(시간 잡은 :05).
    rollup_dirty_margin_sec: int = 120

    # daily_aggregates 의 일별 캘린더 경계 + cron 실행 타임존(§59). IANA TZ 이름.
    # admin-api 의 REPORTING_TIMEZONE 과 동일 값으로 맞춰야 한다 — 안 그러면
    # daily_aggregates(이 값 기준)와 usage_logs 실시간 집계(admin-api 기준)가
    # 서로 다른 날짜 경계를 갖게 된다.
    # ── wave 3: 재가격(B7) / 비용 대조(B8) ──
    #: pricing_missing 행 재가격 주기(초). 0 이면 끈다.
    reprice_interval_sec: int = 600
    reprice_batch_size: int = 500
    #: 야간 비용 대조 — 리포팅 tz 01:00(일 롤업 00:10 이후). 자기 advisory lock 727_002.
    reconciliation_enabled: bool = True
    reconciliation_cron: str = "0 1 * * *"
    #: 추정 행 비율 경고 임계(%) — 넘으면 checks.estimated_share.warn=true(불일치로 세지 않는다).
    reconciliation_estimated_warn_pct: float = 0.5
    #: dirty 날짜가 이 시간 넘게 남으면 불일치로 센다(R1).
    reconciliation_dirty_alert_hours: int = 48

    # ── wave 3b: 요청 시작 원장 sweeper(B2) / 게이트웨이 DB 폴백 소화(B3) ──
    #: sweeper 주기(초). 0 이면 끈다.
    request_sweep_interval_sec: float = 30.0
    #: 완료 없는 시작을 추정 행으로 쓰기까지의 최소 나이(초) — 그리고 그 게이트웨이 프로세스의 생존
    #: 신호
    #: (cost:live:<instance>)가 끊겨 있어야 한다.
    request_sweep_min_age_sec: float = 300.0
    #: 생존 신호와 무관하게 쓸어 담는 나이(초) — 신호를 읽을 수 없거나(Redis 장애) 살아 있는
    #: 프로세스가
    #: 시작을 흘린 경우의 상한. 게이트웨이 예약 마커 TTL(4 h)과 같다.
    request_sweep_max_age_sec: float = 14_400.0
    #: 스트림 lag 이 이보다 크면 쉰다 — 아직 소비하지 않은 완료가 있을 수 있다(추정 행 → 교체 왕복
    #: 방지).
    request_sweep_max_lag: int = 1000
    request_sweep_batch_size: int = 200
    #: 닫힌 표지(close)·추정 행을 쓴 시작의 보존 시간 — 늦게 도착한 중복 start/완료를 판정하는 창.
    request_starts_retention_hours: int = 48
    #: usage.cost_pending_events(게이트웨이 XADD 실패 폴백) 소화 주기(초). 0 이면 끈다.
    pending_events_interval_sec: float = 2.0
    pending_events_batch_size: int = 500

    reporting_timezone: str = "Asia/Seoul"

    @field_validator("reporting_timezone")
    @classmethod
    def _validate_reporting_timezone(cls, v: str) -> str:
        """Fail fast at boot on an invalid IANA TZ name instead of a CrashLoop
        surfacing only once the aggregator cron first fires."""
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as e:
            raise ValueError(_reporting_timezone_error(v)) from e
        return v

    # Graceful shutdown
    shutdown_grace_period_sec: float = 30.0

    # Logging
    log_level: str = "INFO"
    log_format: str = "json"  # json | console

    # OTel
    otel_exporter_otlp_endpoint: str = "http://otel-collector:4317"
    otel_service_name: str = "cost-recorder-worker"
    otel_traces_sampler_arg: float = 1.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
