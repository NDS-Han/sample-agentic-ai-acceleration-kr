# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from functools import lru_cache

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Server
    # Dockerfile CMD `${WORKERS:-4}` 가 실제 워커 수를 정하므로 WORKERS 도 읽는다 —
    # 과거엔 차트가 WORKERS=2 를 주입했는데 이 설정은 4 에 머물러 Redis-down
    # fallback divisor 가 실제 워커 수의 2배로 계산돼 fallback 쿼타가 절반으로
    # 쪼그라들었다(R16). env 를 하나로 묶어 다시는 어긋나지 않게 한다.
    # WORKERS 우선 — Dockerfile/Helm 이 주입하는 이름이 WORKERS 라, 둘이 다르게
    # 잡히면 컨테이너가 실제로 띄운 프로세스 수가 설정값을 이긴다(R16 V-11).
    uvicorn_workers: int = Field(
        default=4, validation_alias=AliasChoices("WORKERS", "UVICORN_WORKERS")
    )

    @field_validator("uvicorn_workers", mode="before")
    @classmethod
    def _warn_worker_env_disagreement(cls, v):
        """두 env 가 다르게 잡히면 WORKERS 가 이기는데 조용하다 — 배포자가
        어느 쪽이 살았는지 모르면 같은 드리프트가 재발하므로 경고를 남긴다."""
        import os

        workers, uvicorn_w = os.environ.get("WORKERS"), os.environ.get(
            "UVICORN_WORKERS"
        )
        if workers and uvicorn_w and workers != uvicorn_w:
            import structlog

            structlog.get_logger(__name__).warning(
                "worker_env_disagreement",
                WORKERS=workers,
                UVICORN_WORKERS=uvicorn_w,
                effective=workers,
            )
        return v
    uvicorn_host: str = "0.0.0.0"
    uvicorn_port: int = 8000
    max_body_size: int = 20_971_520  # 20MB
    app_version: str = "0.1.0"

    # Redis
    redis_url: str = "redis://redis:6379/0"
    redis_pool_size: int = 150
    redis_cluster_mode: bool | None = None  # None = auto-detect
    redis_tls_enabled: bool = False

    # Redis 연결 복원력 (부하 강건성, deepdive Q50 Phase 2).
    # 과거 클라이언트는 max_connections 만 줘 socket_timeout 이 None(무한 블로킹)이라,
    # 느린/블랙홀 노드 하나가 모든 awaited 호출을 멈춰 풀(150)을 전 pod 에서 고갈시켰다.
    #  - socket_timeout: 명령(read) 상한. 초과 시 TimeoutError → 상위 fallback(DB) 로.
    #  - socket_connect_timeout: 연결 수립 상한(failover 직후 죽은 노드 빠른 포기).
    #  - retry: 단발 blip 을 백오프 재시도로 흡수(느린 DB 강등 전 1~2회).
    #  - health_check_interval: idle 연결 주기 ping → failover 후 stale 연결 회수.
    # 0/None 비활성(과거 동작). hot-path 라 값 조정 시 load A/B 권장.
    redis_socket_timeout: float = 2.0
    redis_connect_timeout: float = 1.0
    redis_retries: int = 1
    redis_health_check_interval: float = 30.0

    # 읽기를 replica 로 라우팅(deepdive Q50 Phase4-f). cluster 모드 + replica 있을 때만
    # 의미. read 스케일 확보(GET 류를 replica 가 분담)하나 replica lag 으로 약간의
    # stale read 가능 → TTL 캐시·rate-limit ZSET 처럼 lag 허용 워크로드에서만 ON.
    # 기본 False(primary-only, 강한 일관성 — 기존 동작). standalone 에선 무영향.
    redis_read_from_replicas: bool = False

    # rate-limit 회로 차단기(deepdive Q50 — per-request fast-fail). Redis 가 죽었을 때
    # 매 요청이 socket_timeout 을 다 기다리는 대신, 연속 실패가 임계를 넘으면 회로를
    # 열어 즉시 fallback 으로 보낸다. 기본 활성(소켓 타임아웃 대기 누적 방지). 끄려면
    # rl_breaker_enabled=false. 임계/복구는 아래 값으로.
    rl_breaker_enabled: bool = True
    # ⚠️ p1-hotpath-redis 부터 미사용(호환용으로만 남김). rate-limit eval 은 이제 프로세스 공용
    #    Redis 브레이커(hot_path.REDIS_BREAKER — 예산·모델 캐시·비용 이벤트와 같은 것)를 쓴다.
    #    임계는 "한 번의 인프라 실패", 복구는 hot_redis_breaker_cooldown_sec.
    rl_breaker_fail_threshold: int = 5
    rl_breaker_recovery_timeout: float = 5.0

    # ── Hot-path Redis (p1-hotpath-redis) ─────────────────────────────────────
    # 요청 경로의 rate-limit Redis 호출(한도 설정 GET, RPM/TPM/비용 Lua) 하나의 상한.
    # 클라이언트 socket_timeout(2 s) × (1 + redis_retries) 보다 훨씬 짧게 — Redis 행(hang)에서
    # 요청이 명령마다 ~4 s 를 기다리지 않게 한다. 넘으면 그 호출은 실패(정책대로 fail-open/closed)
    # 하고 공용 브레이커가 열린다. 0 이하면 상한 없음(socket_timeout 만).
    hot_redis_timeout_sec: float = 0.5
    # 공용 Redis 브레이커 — 요청 경로 Redis 호출이 타임아웃/연결 오류를 내면 이 프로세스는 이 시간
    # 동안 요청 경로의 Redis 를 건너뛴다(예산 → DB/마지막 값, rate-limit → 메모리 fallback 한도,
    # 비용 이벤트 → spool). 예산 열화 모드(wave 1c)가 쓰던 2 s 와 같은 값.
    hot_redis_breaker_cooldown_sec: float = 2.0
    # Redis 스크립트를 EVALSHA 로 보낸다(NOSCRIPT 면 EVAL 로 한 번 더). false 면 예전처럼 EVAL.
    redis_evalsha: bool = True

    # rate-limit eval 이 (Redis 장애 등으로) 실패할 때의 정책(deepdive Q50).
    #  - "open"(기본·기존 동작): 통과시킴(가용성 우선, NFR-2.4 graceful degradation).
    #    Redis 장애가 사용자 차단으로 번지지 않게. rl_fail_open_total 로 가시화.
    #  - "closed": 차단함(보안/쿼타 정확성 우선). 무제한 통과를 허용 못 하는 환경용.
    # **무단 전환 금지** — 이건 가용성↔정확성 트레이드오프라 명시 설정으로만 바꾼다.
    # budget/auth 는 이미 fail-closed(예산 미확인 시 차단) — 의도적 비대칭(예산 초과
    # 과금 방지 > 가용성). rate-limit 만 기본 open 인 건 RL 이 남용방지지 과금 게이트가
    # 아니기 때문. 운영 정책에 따라 closed 로 정렬 가능.
    rl_fail_mode: str = "open"

    # Database
    db_url: str = "postgresql+asyncpg://gateway:gateway@postgres:5432/gateway"
    db_pool_size: int = 20
    db_max_overflow: int = 10
    db_ssl_mode: str = "disable"
    # RDS Proxy 경유 시 0 으로 설정 (PostgreSQL pinning 회피). Aurora 직접 연결 시엔 기본값 유지.
    db_statement_cache_size: int = 100
    # 풀 고갈 시 커넥션 대기 상한. SQLAlchemy 기본은 30s 라 풀 포화 시 요청당 최대 30초
    # 무한대기성 지연("CPU 정상인데 느림" 시그니처)을 유발한다 → 짧은 값으로 fast-fail(TimeoutError).
    # hot-path(gateway-proxy)는 admin-api(core/db.py)와 정합되게 명시. (견고성 6축검증 축③)
    db_pool_timeout: int = 10
    # 커넥션 최대 수명(초). RDS Proxy/Aurora 가 유휴 커넥션을 끊으면 죽은 커넥션이 풀에 잔존.
    # pool_pre_ping 이 1차 방어하나, recycle 로 오래된 커넥션을 선제 폐기(기본 -1=무제한 회피).
    db_pool_recycle: int = 3600
    # asyncpg connect/command 상한 — 블랙홀 장애(거부 없이 무응답: SG 변경·AZ 파티션)에서
    # asyncpg 기본값(connect 60s, 명령 무제한)은 풀을 물고 요청을 붙잡는다.
    # connect 는 health probe 의 5s 와 정합, command 는 이 서비스 쿼리가 전부
    # 소규모라 30s 면 충분 — runaway 쿼리가 풀을 점유하는 것도 막는다.
    db_connect_timeout: float = 5.0
    db_command_timeout: float = 30.0

    # ── Hot-path isolation (wave 1) ────────────────────────────────────────────
    # Per-request DB lookups on the proxy path (VK is_active / user + entitlements on a
    # cache miss) use a small dedicated in-process pool with a hard deadline. A slow DB
    # costs at most this long, then the request is served from the last known good value.
    hot_db_timeout_sec: float = 0.3
    hot_db_pool_size: int = 4
    hot_db_max_overflow: int = 2
    # Below the RDS Proxy idle_client_timeout (1800 s) so pre_ping can be off on this pool.
    hot_db_pool_recycle: int = 1500
    # After a hot-path DB timeout/error, skip the DB for this long (per process).
    hot_db_breaker_cooldown_sec: float = 2.0
    # In-process auth cache: is_active per user + AuthContext per VK hash.
    auth_local_cache_ttl_sec: float = 45.0
    # How old a last-known-good entry may be when the DB/Redis is unavailable.
    auth_local_cache_max_stale_sec: float = 300.0
    auth_local_cache_max_entries: int = 20000
    # Redis lookups during auth give up after this long when a local fallback exists.
    auth_redis_fallback_timeout_sec: float = 0.5
    # admin-api publishes {"user_ids": [...], "key_hashes": [...]} here on changes.
    auth_invalidation_channel: str = "gateway:auth:invalidate"
    # Downgrade policy / web-search policy DB lookups (A5).
    degraded_lookup_timeout_sec: float = 0.5
    # Background web-search counter rebuild (off the request path).
    web_search_rebuild_timeout_sec: float = 20.0
    web_search_rebuild_lock_ttl_sec: int = 60

    # ── Budget check degraded mode (wave 1c, option B: decide from last known values) ──
    # Budget policy (limits/policy/thresholds per team/user/app) is cached per process and
    # re-read in the background after this long. Request-path DB reads use hot_db_timeout_sec.
    budget_policy_refresh_sec: float = 60.0
    # How old the last known policy / budget state may be when DB (and Redis) can't answer.
    budget_stale_max_seconds: float = 900.0
    # Neither policy nor state known (cold pod + DB down): allow for this long per process,
    # then fail closed (503). Reset when a budget DB read succeeds again.
    budget_failopen_grace_seconds: float = 300.0
    # The whole Redis part of the budget check gives up after this long (then DB fallback);
    # a timeout/error skips Redis for hot_db_breaker_cooldown_sec (per process).
    budget_redis_timeout_sec: float = 1.0
    # Redis config keys written from the last known policy (DB down) expire after this long,
    # so the real DB value replaces them soon after recovery.
    budget_degraded_config_ttl_sec: int = 60
    # Background policy refresh: DB deadline, and wait before retrying a failed key.
    budget_policy_refresh_timeout_sec: float = 2.0
    budget_policy_refresh_retry_sec: float = 10.0
    budget_local_cache_max_entries: int = 20000

    # ── Router model/catalog cache (wave 1d) ───────────────────────────────────
    # Model config / alias→provider / model list / routing profile lookups are cached per
    # process. false = legacy (Redis GET + DB on every request).
    model_cache_enabled: bool = True
    # Entries older than this are still served and re-read in the background (main pool).
    model_cache_refresh_sec: float = 60.0
    # Same, while the invalidation subscription is down (admin edits can't reach us).
    model_cache_refresh_unsubscribed_sec: float = 10.0
    # How old a last known entry may be when DB and Redis can't answer.
    model_cache_max_stale_sec: float = 3600.0
    # Negative results (unknown model / no routing profile), like the Redis model:nx 15 s.
    model_cache_negative_ttl_sec: float = 15.0
    model_cache_max_entries: int = 5000
    # Redis read on a local miss gives up after this long; a failure skips Redis for
    # hot_db_breaker_cooldown_sec. DB reads on a miss use hot_db_timeout_sec.
    model_cache_redis_timeout_sec: float = 0.3
    model_cache_refresh_timeout_sec: float = 2.0
    model_cache_refresh_retry_sec: float = 10.0
    # admin-api publishes {"model_refs": [...], "model_list": bool, "routing_clients": [...]}.
    model_invalidation_channel: str = "gateway:model:invalidate"

    # ── Startup warm-up (wave 1e) ──────────────────────────────────────────────
    # Before readiness reports ready: open Redis + DB pool connections, load the model/alias/
    # routing cache, the budget policy cache and the auth cache for recently used VKs. If that
    # does not finish within startup_warmup_timeout_sec (DB/Redis down or slow) the process
    # becomes ready anyway ("degraded" in /health/ready) and keeps warming in the background.
    startup_warmup_enabled: bool = True
    startup_warmup_timeout_sec: float = 20.0
    # Deadline of one warm-up step (one batch of queries); failed steps are retried.
    startup_warmup_step_timeout_sec: float = 10.0
    # Background retry back-off cap after the foreground window.
    startup_warmup_retry_max_sec: float = 30.0
    # Main-pool connections opened per worker; -1 = db_pool_size, 0 = none. The hot-path
    # (auth) pool is always opened in full (hot_db_pool_size).
    startup_warmup_db_conns: int = -1
    startup_warmup_redis_conns: int = 4
    # Recently used VKs pre-loaded into the auth cache (newest first, capped).
    startup_warmup_auth_window_sec: float = 86400.0
    startup_warmup_auth_max_keys: int = 5000
    startup_warmup_model_max_aliases: int = 500
    # Recently used VK hashes (sha256, never the raw key) — Redis sorted set written by the
    # gateway on successful VK auth, at most once per key per record interval per process.
    auth_recent_vk_enabled: bool = True
    auth_recent_vk_key: str = "gateway:auth:recent_vk"
    auth_recent_vk_record_interval_sec: float = 300.0
    auth_recent_vk_flush_interval_sec: float = 10.0
    auth_recent_vk_max_keys: int = 20000

    # Timeouts
    lua_timeout_ms: int = 1000
    stream_timeout: int = 300
    # SSE 청크 간 무응답 상한. 초과 시 `event: error`(timeout_error) 프레임을 보내고 종료.
    #
    # ⚠️ 두 개의 상한 사이에 끼워야 한다:
    #   ① upstream(Bedrock/Mantle) 첫 토큰 지연 — extended thinking 은 60s 를 넘길 수 있다.
    #      기본값 60 은 Opus extended thinking 요청을 정상 응답 중에 끊어버렸다.
    #   ② ALB idle_timeout — dev 300s / prod 600s
    #      (values-eks-fargate-{dev,prod}.yaml: load-balancer-attributes).
    #      이 값이 ALB 보다 **크거나 같으면** ALB 가 먼저 커넥션을 끊어 클라이언트는
    #      깔끔한 SSE 에러 대신 truncated stream 을 본다. 반드시 ALB 보다 작아야 한다.
    #
    # 기본 240 = dev ALB(300) 보다 60s 아래. 차트가 환경별로 override 한다
    # (STREAM_IDLE_TIMEOUT). ALB 값을 바꿀 때 이 값도 함께 조정할 것.
    stream_idle_timeout: int = 240
    # 클라이언트 끊김 후 백그라운드로 upstream 을 계속 소비하는 상한.
    # 이 시간 안에 받은 토큰까지 usage 로 기록된다(과금 정확성).
    stream_disconnect_drain_timeout: int = 30

    # 월예산 admission 예약 마커(budget:pending:*)의 수명. 요청이 이 시간보다
    # 오래 살아있으면 마커가 먼저 만료돼 settle 이 no-op 되고 예약치만 남는다
    # (보수적 방향 — 실비 미반영이 아니라 과대 계상). 파드 크래시로 정산 없이
    # 사라진 예약의 정리 시한이기도 하다 — 마커가 지워져도 카운터의 누수분은
    # 월 롤오버까지 남으므로, 이 값은 "누수 마커의 추적 가능 기간"이다.
    # ge=1 — 0/음수 TTL 은 예약 마커·settle 툼스톤의 SET ... EX 에서 Lua 가
    # 커밋 후 크래시해 예약 누수/이중정산 부분쓰기가 된다(11차 리뷰).
    # 상한 = Lua SET ... EX 가 int64 초를 받는 한계보다 훨씬 아래 — 무제한이면
    # 10**19 같은 값이 EX 파싱 실패로 커밋 후 크래시를 낸다(12차 리뷰).
    budget_reservation_ttl_seconds: int = Field(default=14400, ge=1, le=31_536_000)  # 4h, 상한 1y

    # 월예산 선예약의 worst-case 추정에 쓰는 출력 상한(토큰). 클라이언트의
    # max_tokens 를 그대로 쓰면 비싼 모델(Opus 출력 $75/MTok)에서 한 번의
    # 선예약이 소액 예산을 통째로 잠가 호출 자체가 막힌다 — 예약 추정만
    # 이 상한으로 클램프하고, 실비 정산은 실제 사용량을 쓴다(초과분은
    # 정산 시 카운터가 cap 위로 기록돼 다음 요청이 차단되는 방향으로
    # 수렴). 응답 자체는 잘리지 않는다 — 실제 max_tokens 로 나간다.
    budget_reservation_max_output_tokens: int = Field(default=16384, ge=0)

    # pendingidx 인덱스 롤아웃 웜업 — 인덱스 미포함 마커를 쓰는 구버전 파드가
    # 남아 있을 수 있는 창(배포 시각 + marker TTL) 동안 rebuild 가 인덱스 대신
    # 키스페이스 SCAN 을 쓴다. 전 파드가 인덱스 버전으로 올라간 뒤에는 False 로
    # 두면 된다 — 그러면 Redis 플러시 직후(rebuild 가 가장 몰리는 창)에도
    # 웜업이 재시작돼 SCAN 폭풍이 도는 일이 없다(R22 r4 N5).
    budget_pendingidx_warmup: bool = True

    # Reliability
    usage_buffer_max: int = 10_000
    background_task_max_retries: int = 3

    # ── 비용 이벤트 내구성(wave 3b) ──
    #: admission 'start' 이벤트(B2) — 워커 sweeper 가 완료 없이 죽은 요청에 추정 행을 쓴다.
    #: ⚠️ 워커(wave 3b 이상)를 먼저 배포한 뒤 켠다 — 구 워커는 start/close 를 dead-letter 로 보낸다.
    cost_start_events_enabled: bool = True
    #: 프로세스 생존 신호 ``cost:live:<instance>`` 갱신 주기/TTL(초). sweeper 는 신호가 끊긴
    #: 프로세스의
    #: 시작만 쓸어 담는다 — TTL 은 주기의 3배 이상(Redis 일시 지연에 흔들리지 않게).
    cost_heartbeat_interval_sec: float = Field(default=10.0, gt=0)
    cost_heartbeat_ttl_sec: int = Field(default=45, ge=5)
    #: XADD 실패 시 spool → ``usage.cost_pending_events`` DB 폴백(B3). 끄면 예전처럼 메모리 spool
    #: 만.
    cost_event_db_fallback_enabled: bool = True
    #: DB 폴백 한 배치 쓰기 상한(초) — 넘기면 항목은 spool 에 남고 다음 틱에 다시.
    cost_event_db_timeout_sec: float = Field(default=3.0, gt=0)
    #: 종료 시 스트림 드레인 태스크(B1 disconnect 드레인 → finalize)를 기다리는 추가 여유(초).
    #: 실제 대기 = stream_disconnect_drain_timeout + 이 값(lifespan 예산 안에서).
    shutdown_stream_drain_margin_sec: float = Field(default=5.0, ge=0)
    #: lifespan 종료 단계 전체 예산(초) — 차트 terminationGracePeriodSeconds 산식의 "40 lifespan".
    shutdown_lifespan_budget_sec: float = Field(default=40.0, gt=0)

    # Redis-down in-memory rate-limit fallback (middleware/rate_limit.py).
    # 이 fallback 의 USER RPM 카운터는 **프로세스(uvicorn worker)마다 독립**이라,
    # 함대 전체엔 `replicas × uvicorn_workers` 개의 독립 카운터가 존재한다. 사용자
    # 트래픽이 그만큼 분산되므로 각 카운터는 `limit // (replicas × uvicorn_workers)`
    # 를 허용해야 클러스터 총합이 limit 에 맞는다. 과거엔 divisor 가 4(=uvicorn_workers,
    # 1 pod 가정)로 **하드코딩**되어 HPA replica 수(3~30)를 무시 → fallback 진입 시
    # 쿼타가 현실과 불일치(부하테스트 429×6 의 배경, deepdive Q46/Q50).
    #
    # rl_fallback_replicas 를 **현재 환경의 대표 replica 수(예: HPA minReplicas)**로
    # 설정하면 divisor 가 현실을 추종한다. Helm chart 는 이미
    # `gatewayProxy.autoscaling.minReplicas`(없으면 replicaCount)를 주입한다
    # (templates/common/configmap.yaml). 기본 1 = 비-Helm 환경의 과거 동작
    # (divisor=uvicorn_workers) 보존.
    #
    # ⚠️ 잔여 한계(R3-10): HPA 가 minReplicas 위로 스케일하면 fallback 총합이
    #    limit × (실제/minReplicas) 만큼 열린다. Redis 장애 중 스케일아웃은
    #    이 창을 넓힌다 — 공유 백엔드 없이는 구조적 한계이며, 완화는
    #    (a) fallback 발동 알림으로 운영자 인지, (b) minReplicas 를 평상시
    #    replica 수에 가깝게 유지.
    rl_fallback_replicas: int = 1
    # fallback RPM 한도(클러스터 총합, 위 divisor 로 나뉜다). 스코프의 마지막으로 알던 한도
    # (rate_limit_config_loader 의 프로세스 캐시, budget_stale_max_seconds 까지)가 있으면 그 값을,
    # 없거나 무제한이면 이 값을 쓴다 — fallback 은 항상 유한하다. 예전 하드코딩 60/600/6000 과 같다.
    rl_fallback_user_rpm: int = Field(default=60, ge=1)
    rl_fallback_team_rpm: int = Field(default=600, ge=1)
    rl_fallback_global_rpm: int = Field(default=6000, ge=1)

    # Logging
    log_level: str = "INFO"
    log_format: str = "json"

    # OTel
    otel_exporter_otlp_endpoint: str = "http://otel-collector:4317"
    otel_traces_sampler_arg: float = 1.0
    otel_service_name: str = "gateway-proxy"

    # AWS
    aws_region: str = "ap-northeast-2"
    aws_profile: str | None = None

    # Mantle (Cowork cross-account) — region for the STS client used to assume the
    # 905 Mantle role; and the httpx timeout for Mantle streaming calls.
    mantle_assume_region: str = "ap-northeast-2"
    mantle_http_timeout: float = 120.0

    # OpenModel
    openmodel_base_url: str = "http://mock-vllm:8080"

    # --- Model availability fallback / circuit breaker (2026-06-22) ---
    # cb_* 는 Lua EXPIRE/PX 인자로 직결 — <=0 이면 breaker eval 이 매번
    # 크래시해 회로 차단기가 죽는다(12차 리뷰). jitter 는 0 허용(끔).
    # 상한은 Lua 클램프와 정합 — window>3600 은 합산 루프가 Redis 를 블록,
    # open>86400s 는 사실상 영구 개방(무소리 모델 장애)이라 기동 시 거부한다
    # (14차 리뷰).
    cb_window_sec: int = Field(default=30, ge=1, le=3600)
    cb_min_calls: int = Field(default=5, ge=1)
    cb_error_rate: float = Field(default=0.5, gt=0, le=1)
    cb_open_sec: int = Field(default=30, ge=1, le=86400)
    # 상한 필요 — 거대 PX 가 프로브 슬롯을 사실상 영구 점유하면 open 만료 뒤에도
    # 반개방 프로브를 선출할 수 없어 회로 검증 절차가 무소리 우회된다(16차 리뷰).
    cb_halfopen_probe_ttl_ms: int = Field(default=8000, ge=1, le=300000)
    # jitter>86s 면 open_ms 가 항상 Lua 상한을 넘어 결정적 1일 개방이 돼
    # herd 방지 목적이 무소리 소실된다 — 1h 상한(15차 리뷰).
    cb_open_jitter_sec: int = Field(default=5, ge=0, le=3600)
    bedrock_inference_read_timeout: int = 60
    bedrock_max_attempts: int = 1
    # 가용성 폴백 **총** 시간 예산 — 후보(원본+체인 최대 5)마다 상류 타임아웃이
    # 걸리면 누적 대기가 ALB idle 한도를 넘는다. 데드라인을 넘으면 새 후보 시도를
    # 멈추고 마지막 오류를 돌려준다(첫 후보는 항상 시도).
    fallback_budget_sec: int = 180

    # Features
    cors_enabled: bool = False

    # Request tracing (reasoning/tool_use observability — services/trace_extractor.py)
    # trace_enabled: 응답 본문에서 trace 추출 자체를 켤지(미배선 — 추후 hot-path 배선용).
    # trace_mask_pii: PII 마스킹 토글. **기본 True(fail-safe)** — tool input 값 +
    #   thinking/text PII 패턴을 마스킹. 신뢰 환경에서만 false 로 명시 해제(평문 저장).
    #   설정 누락 시 안전(마스킹)으로 동작하도록 기본값이 True.
    trace_enabled: bool = False
    trace_mask_pii: bool = True

    # ── Body logging (요청/응답 **본문** → Firehose → S3) ──
    #
    # ⚠️ 이 기능을 켜면 사용자가 프롬프트에 넣은 것이 그대로 durable 저장소로 나간다.
    #    현재 구현은 **마스킹하지 않는다** — 같은 코드베이스의 trace 경로는
    #    `trace_mask_pii` 기본 True 로 마스킹하는데, 본문 로거에는 그것이 적용되지
    #    않는다. 그건 알고 있는 격차이고 향후 개선 대상이다. 그래서 두 겹으로 잠근다:
    #      (1) `firehose_stream_name` 이 없으면 로거 자체가 no-op(로컬/compose 안전),
    #      (2) 런타임 토글(`bodylog:enabled`)이 **기본 OFF** — 배포만으로는 켜지지 않고
    #          관리자가 명시적으로 켜야 한다(admin-api PUT /admin/settings/body-logging,
    #          그 액션은 audit.audit_logs 에 불변 행을 남긴다).
    body_logging_enabled: bool = True
    firehose_stream_name: str | None = None
    body_log_s3_bucket: str | None = None
    body_log_max_queue: int = 10_000
    #: 인플라이트 body-log 버퍼의 바이트 상한 — 레코드 수 상한만으로는 대형
    #: 프롬프트가 쌓였을 때 메모리가 무한 증가한다(R16, 최대 ~9GB 시나리오).
    body_log_max_queue_bytes: int = 134_217_728  # 128 MiB
    body_log_batch_size: int = 100
    body_log_flush_interval: float = 5.0
    #: Firehose 레코드 상한(1 MB) 아래로. 초과분은 S3 에 직접 넣는다.
    body_log_max_record_bytes: int = 900_000
    #: 워커가 캐시된 플래그를 믿는 시간(초). 토글은 이 시간 안에 반영된다.
    body_log_flag_cache_ttl: float = 5.0
    #: 플래그를 읽지 못했을 때의 값. **False 여야 한다** — 설정을 못 읽었다는 이유로
    #: 본문 수집이 켜지면 안 된다(fail-safe 방향).
    body_log_flag_default: bool = False

    # --- AgentCore Gateway web search (server-side tool-use loop, 2026-07-01) ---
    # We inject a web_search tool, intercept the model's tool_use, call AgentCore
    # Gateway's managed WebSearch connector over MCP (SigV4/IRSA), feed results
    # back, and stream the final answer — emulating Anthropic 1P server-side search.
    # Bedrock does NOT expose native web_search (verified: ValidationException), so
    # this gateway loop is the only path. The managed connector is us-east-1-only,
    # so AGENTCORE_REGION differs from AWS_REGION (ap-northeast-2) — cross-region;
    # IRSA creds are global, only the SigV4 signing scope + endpoint use us-east-1.
    #   agentcore_gateway_url: full MCP endpoint (…gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp)
    #   agentcore_target_id:   Gateway target name → tool is "<target_id>___WebSearch"
    #   web_search_enabled:    global kill-switch (per-client toggle lives in routing_profiles)
    agentcore_gateway_url: str = ""
    agentcore_region: str = "us-east-1"
    agentcore_target_id: str = "web-search-tool"
    agentcore_http_timeout: float = 30.0
    #: Cap on the whole MCP handshake (three HTTP calls). agentcore_http_timeout is PER CALL,
    #: so on its own one handshake could take 3× that, and the handshake lock is
    #: process-global, so concurrent requests would queue behind it (see the field comments
    #: in services/agentcore_mcp_client.py).
    agentcore_handshake_timeout: float = 10.0
    #: How long to suppress handshake retries after a failure, so every request does not pay
    #: the full timeout against a dead gateway. The price is a recovery delayed by up to this.
    agentcore_handshake_negative_ttl: float = 30.0
    web_search_enabled: bool = False
    #: Defaults of the web-search settings below (2026-09-19): the values that were measured
    #: and verified on US dev are the DEFAULTS, so an install that sets nothing gets the cheap
    #: caps and the fixed behaviour instead of having to discover a script. Every one of them
    #: is still an env var — a client or model update that breaks something is rolled back
    #: by config, without a rebuild. Previous defaults: 5 rounds / 10 results / 60000 chars /
    #: 4 per turn / text trace / cowork only / mixed-turn and soft-final off.
    #:
    #: Search ROUNDS per request. 2→3 measured +50% searches and +47% cost with the same
    #: answer on narrow questions; only comparisons of 7+ items gained (2026-09-19).
    web_search_max_iterations: int = 2
    web_search_total_deadline_sec: float = 90.0
    #: Results kept per search (after URL/mirror de-duplication). The model's ``max_results``
    #: is honoured only up to this value — despite the name it is a MAXIMUM (2026-09-17: the
    #: model asked for 15 and the 12k-char cap cut the result JSON mid-record). 0 = unlimited.
    #: The connector is asked for up to 2× this number so de-duplication has candidates.
    web_search_max_results_default: int = 5
    #: Cap on ONE search's result text (chars). 0 = unlimited (pre-cap behaviour).
    #: max_iterations bounds turns and total_deadline_sec bounds time; the only bound on the
    #: bill-deciding axis — bytes injected into the next turn's input — is this one. One
    #: search once injected ~17.4K tokens on dev; 60000 chars ≈ 15K tokens, 12000 ≈ 3–4K.
    web_search_max_result_chars: int = 12000
    #: Searches run per TURN. 0 = unlimited. The model may issue many parallel web_search
    #: calls in one turn and the loop supports that; 20 of them would put 20× results into
    #: the next turn's input — an uncapped single-request cost, or a context overflow that
    #: 400s the continuation turn and loses everything billed so far. Observed: the model
    #: never asked for more than 3 in one turn.
    web_search_max_searches_per_turn: int = 3
    #: Searches per REQUEST when no budget.web_search_policies row sets ``per_request_cap``
    #: (migration 0059, resolved USER → TEAM → ORG → this). Replaces the old fixed
    #: rounds × per-turn bound: the round count is derived as ceil(cap / per_turn).
    web_search_max_searches_per_request: int = Field(default=5, ge=1, le=50)
    #: Cap on the ADMISSION-only input-token add for web-search requests (see
    #: ``conservative_admission_body``). The raw worst-case growth add is
    #: per_turn_results × max_result_chars × growth_rounds/4 — with defaults
    #: (3×12000×7/4) that alone is ~63k tokens, so any TPM budget below ~80k
    #: rejects web-search requests before they start (permanent 429, not
    #: degradation). Reserving is still conservative up to this cap; settle
    #: refunds the excess either way. Note the cap can also UNDER-reserve:
    #: a request whose real growth exceeds the cap is admitted while its
    #: minute bucket overshoots the TPM limit by the uncapped remainder —
    #: self-correcting at the next admission check in the same window
    #: (10th review). 0 = uncapped (old behaviour).
    web_search_admission_input_add_cap: int = Field(default=16384, ge=0)
    #: Put a cache_control marker on the search results (tool_result) the loop injects so later
    #: turns of the same request read them at the cached price (10% of list). 2026-09-16: most
    #: of a multi-search request's input cost was results re-sent every turn (3 searches: 6R
    #: result tokens, about 3.8R with the marker). _place_cache_breakpoint keeps the 4-marker
    #: limit. Set False to disable if it misbehaves.
    web_search_cache_results: bool = True
    #: Per-RESULT body cap (chars). Unlike max_result_chars (which cuts the whole JSON) this
    #: keeps every result's title/URL/lead and cuts only the body at a sentence boundary,
    #: dropping URL duplicates (2026-09-16: raw results were 13–24k chars; 1500 per item saves
    #: 40–50% of the tokens per search and no trailing result is lost). 0 = off.
    web_search_result_text_chars: int = 1500
    #: Language of the trace line left for the client ("en" | "ko"). The prefix
    #: `🔎 [gateway web_search]` is shared (the imitation filter and the tool description
    #: match on it).
    web_search_trace_lang: str = "en"
    #: "native" (default) | "text". Native leaves the trace as Anthropic native search blocks
    #: (server_tool_use + web_search_tool_result) in addition to the text line — a text line
    #: alone is denied under questioning ("I never searched") and imitated when it shares a
    #: message with a client tool call. Replayed blocks are converted back on the way in
    #: (2026-09-17). Only the client classes listed below get the blocks; every other client
    #: keeps the text line, so "native" is safe as a default. "text" turns the blocks off.
    web_search_trace_mode: str = "native"
    #: Client classes that get native traces (cowork | claude-code | codex, comma-separated;
    #: empty = all). Classification comes from ClientIdentificationMiddleware, not from a raw
    #: header value. Only clients that REPLAY the blocks verbatim belong here — otherwise the
    #: results never reach the model on the next request. Verified: cowork (2026-09-17),
    #: claude-code CLI 2.1.276 (2026-09-18: byte-identical replay, also with --continue, and
    #: its built-in WebSearch helper builds proper link results only from native blocks).
    web_search_trace_native_clients: str = "cowork,claude-code"
    #: Per-result excerpt length (chars) carried in the native blocks — what the replayed
    #: tool_result shows the model on later turns. 0 (default) = the same length as the trimmed
    #: result the model saw (web_search_result_text_chars). Shorter cuts evidence.
    web_search_digest_chars: int = 0
    #: Web search next to the CLIENT's own tools (2026-09-18). On by default since both were
    #: confirmed with real Cowork and real Claude Code (2026-09-19); set to false to get the
    #: previous behaviour back if a client or model update breaks them.
    #: - mixed_turn_run: a turn that calls a client tool AND web_search — run the searches and
    #:   send their native block pairs in the same message as the client's tool_use (native
    #:   trace mode only). Off: the searches are not run and the model re-issues them later.
    #: - final_turn_soft: when the search budget is used up, keep every tool callable; a
    #:   further web_search gets an error instead of running (like a server tool's max_uses),
    #:   and only then the hard final turn (tool_choice: none) follows. Off: hard final turn
    #:   at once, which also blocks the client's tools ("search and save a file" saved nothing).
    web_search_mixed_turn_run: bool = True
    web_search_final_turn_soft: bool = True

    #: Tool ``type`` prefixes (comma-separated) removed from /v1/messages requests before they
    #: go upstream: Anthropic-only SERVER tools that Bedrock rejects with a 400 for the whole
    #: request. 2026-09-18: Claude Code's advisor (``advisor_20260301``) made every request of
    #: an affected user fail, "hi" included. A new Anthropic-only tool type is a config change
    #: here, not a release. Blank = pass everything through. See services/upstream_compat.py.
    bedrock_unsupported_tool_type_prefixes: str = "advisor_"

    # --- AI Governance Hub (2026-11): the non-model part of GET /v1/policy ---
    # policy-helper lays the gateway's answer over its baseline. The MODELS slice is computed
    # here (policy_service, same tables the proxy enforces); plugins / MCP servers / lock keys
    # belong to the hub, asked per request with the VK's user_id + team_id (services/policy_hub.py).
    #: In-cluster URL, e.g. http://governance-hub.llm-gateway.svc:8080. Blank = models only
    #: (the PoC behaviour) — also what every hub failure degrades to.
    policy_hub_url: str = ""
    #: Static bearer the hub expects (a k8s Secret in the chart). Never defaulted here.
    policy_hub_token: str = ""
    #: Budget for the hub call. policy-helper gives the whole GET 3 s, so DB + hub must stay
    #: inside that: a slow hub costs the plugin/MCP slice for one refresh, never the helper.
    policy_hub_timeout: float = 2.0

    # ── Reporting timezone (§59) ──
    # 비용/사용량 집계의 "월/일 경계" 기준 타임존. admin-api·cost-recorder-worker·
    # notification-worker 와 같은 env(REPORTING_TIMEZONE)를 읽는다 — 네 서비스가
    # 같은 budget_usages.period 행과 budget:* Redis 키를 쓰므로 값이 갈리면 월 경계
    # 몇 시간 동안 서로 다른 행/키를 본다. **정규 IANA 이름만**(예: "Asia/Seoul",
    # "UTC", "America/Los_Angeles") — "KST"/"US/Pacific" 같은 약어·레거시 alias 는
    # 배포 이미지의 tzdata 에 없을 수 있어 거부.
    reporting_timezone: str = "Asia/Seoul"

    @field_validator("reporting_timezone")
    @classmethod
    def _validate_reporting_timezone(cls, v: str) -> str:
        """기동 시점에 잘못된 IANA 이름을 거부한다 — 쿼리 시점의 500 보다 부팅
        실패가 낫다(admin-api core/config.py 의 동명 검증과 같은 규약)."""
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as e:
            raise ValueError(
                f"Invalid REPORTING_TIMEZONE {v!r}: not a valid IANA timezone name. "
                "Use a canonical IANA name, e.g. 'Asia/Seoul', 'UTC', 'America/Los_Angeles'."
            ) from e
        return v
    #: Bedrock InvokeModel 로 넘길 anthropic-beta 값(본문 ``anthropic_beta``).
    #: 쉼표로 구분한 ``beta[:field]`` 형식이며, ``field`` 는 그 beta 가 여는
    #: 최상위 본문 필드로 beta 와 함께일 때만 넘어간다. 클라이언트 헤더의
    #: 나머지 beta 는 버린다 — Bedrock 은 모르는 beta 가 하나라도 있으면 요청
    #: 전체를 400 으로 거부한다(2026-10-05: prompt-caching-scope-2026-01-05).
    #: - dangerous-tool-use + safeguards: Claude Code 의 서버측 auto mode
    #:   분류기(판정은 ``safeguard_results`` 로 돌아온다).
    #: - per-turn-control: Claude Code 2.1.289 이상이 대화 중간 메시지에 붙이는
    #:   ``messages[].output_config``(턴별 effort)를 연다. 없으면 세션 첫
    #:   요청이 400 이다.
    #: - inline-tools (2026-10-09): system 메시지의 ``tool_addition`` /
    #:   ``tool_removal`` 블록(대화 중간 도구 추가·제거)을 연다. 지운 도구를
    #:   가리키는 블록은 upstream_compat 가 함께 지운다(advisor).
    #: - thinking-display-updates (2026-10-09): ``thinking.display: "updates"``
    #:   를 연다. 없으면 그 대화의 첫 요청이 400 이다(2026-10-08 US dev).
    #: 빈 값 = 아무것도 넘기지 않음(이전 동작) — 끄기 스위치다. beta 추가가
    #: 설정 변경만으로 되는 것은 그 필드와 응답이 웹 검색 루프·사용량 집계·
    #: 폴백과 얽히지 않을 때뿐이고, 그 밖에는 코드 변경이 필요하다.
    #: services/upstream_compat.py 참고.
    #:
    #: English: anthropic-beta values forwarded to Bedrock InvokeModel (body
    #: ``anthropic_beta``), as comma-separated ``beta[:field]``; ``field`` is the
    #: top-level body field the beta opens and that travels ONLY with it. Every
    #: other beta in the client's header is dropped: Bedrock 400s the whole
    #: request on a beta it does not know (2026-10-05:
    #: prompt-caching-scope-2026-01-05).
    #: - dangerous-tool-use + safeguards: Claude Code's server-side auto-mode
    #:   classifier (verdicts return as ``safeguard_results``).
    #: - per-turn-control: opens ``messages[].output_config`` (per-turn effort)
    #:   that Claude Code 2.1.289+ puts on mid-conversation messages; without it
    #:   a session's first request is a 400.
    #: - inline-tools (2026-10-09): opens ``tool_addition`` / ``tool_removal``
    #:   blocks in system messages (tools added or removed mid-conversation);
    #:   blocks that point at a removed tool (advisor) are removed by
    #:   upstream_compat.
    #: - thinking-display-updates (2026-10-09): opens ``thinking.display:
    #:   "updates"``; without it a conversation's first request is a 400.
    #: Blank = forward nothing (the previous behaviour) — this is a kill switch.
    #: Adding a beta is only a config change when its field and response do not
    #: touch the web-search loop, usage accounting or fallback; otherwise it
    #: needs a code change. See services/upstream_compat.py.
    bedrock_forward_betas: str = (
        "dangerous-tool-use-2026-09-03:safeguards,per-turn-control-2026-07-01,"
        "inline-tools-2026-09-15,thinking-display-updates-2026-08-18"
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
