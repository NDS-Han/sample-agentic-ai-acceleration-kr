# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""size_tier 프리셋 — deploy backend 와 독립적인 용량/HA 축.

티어는 "토폴로지"를 정의한다. 세부 수치는 deploy.sizing 으로 override 가능."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class TierPreset:
    name: str
    label: str
    users: str
    backend: str                       # 이 티어의 기본 deploy target
    db_mode: str                       # container | rds-serverless | rds-provisioned
    cache_mode: str                    # container | elasticache-single | elasticache-ha
    db_spec: dict = field(default_factory=dict)
    cache_spec: dict = field(default_factory=dict)
    compute_spec: dict = field(default_factory=dict)
    ha: str = "none"                   # none | backup-only | multi-az
    monthly_cost_usd: str = ""         # 추정치(±50%) — 안내용


TIERS: dict[str, TierPreset] = {
    "t0": TierPreset(
        name="t0",
        label="평가 — 단일 EC2, 컨테이너 DB/캐시",
        users="~50명 / 평가용",
        backend="compose",
        db_mode="container",
        cache_mode="container",
        compute_spec={"instance": "t3.xlarge", "vcpu": 4, "mem_gb": 16},
        ha="backup-only",
        monthly_cost_usd="~$120",
    ),
    "t1": TierPreset(
        name="t1",
        label="소규모 — ECS Fargate, Serverless DB",
        users="100~300명",
        backend="ecs",
        db_mode="rds-serverless",
        db_spec={"engine": "aurora-postgresql", "min_acu": 1.0, "max_acu": 4.0, "multi_az": False},
        cache_mode="elasticache-single",
        cache_spec={"node_type": "cache.t4g.small", "replicas": 0},
        compute_spec={"tasks_per_service": 1, "gateway_tasks": 2},
        ha="backup-only",
        monthly_cost_usd="~$200",
    ),
    "t2": TierPreset(
        name="t2",
        # Multi-AZ 는 NAT·캐시·task 에만 적용 — Aurora serverless 모듈은
        # instance_count=1 이라 DB 는 단일 writer 이다 (multi_az 키를 두지 않는다:
        # render 가 읽는 값만 둬서 "정의된 것과 실제" 가 어긋나지 않게).
        # cost-recorder/notification 워커도 desired_count=1 고정이다 (스트림
        # consumer 중복 방지) — 이 둘은 t2 에서도 단일 장애점이다.
        label="표준 — NAT/캐시/task Multi-AZ (DB는 serverless 단일 writer)",
        users="300~1000명",
        backend="ecs",
        db_mode="rds-serverless",
        db_spec={"engine": "aurora-postgresql", "min_acu": 1.0, "max_acu": 16.0},
        cache_mode="elasticache-ha",
        cache_spec={"node_type": "cache.t4g.medium", "replicas": 1},
        compute_spec={"tasks_per_service": 2, "gateway_tasks": 4},
        ha="multi-az",
        monthly_cost_usd="~$500",
    ),
    "t3": TierPreset(
        # ⚠️ eks 경로는 기존 인프라를 이어받는 모델이라 tier spec 을 소비하지
        # 않는다 — 아래 db_spec/cache_spec/compute_spec 은 "현재 prod 수준"
        # 문서용 수치이며 어떤 렌더러도 읽지 않는다. eks 의 실제 크기는
        # 차트 values/HPA/requests 가 결정한다.
        name="t3",
        label="대규모 — EKS + cluster mode (현재 prod)",
        users="1000명+",
        backend="eks",
        db_mode="rds-provisioned",
        db_spec={"instance_class": "db.r6g.large", "multi_az": True},
        cache_mode="elasticache-ha",
        cache_spec={"node_type": "cache.t4g.medium", "cluster_mode": True, "replicas": 1},
        compute_spec={"hpa": True},
        ha="multi-az",
        monthly_cost_usd="현재 prod 수준",
    ),
}


def preset(name: str) -> TierPreset:
    return TIERS[name]


def tier_choices_for_target(target: str) -> list[str]:
    """backend 가 받을 수 있는 티어 목록."""
    return [t.name for t in TIERS.values() if t.backend == target]


# ──────────────────────────────────────────────────────────────────────────
# ECS 서비스 사이징 + DB 커넥션 예산
#
# service_sizing 은 terraform 모듈의 service_sizing map 으로 내려가는 정본.
# schema.validate 와 render.ecs 가 같이 써야 하므로 여기 둔다(순환 import 방지 —
# 이 모듈은 schema 를 import 하지 않고 duck typing 만 한다).
# ──────────────────────────────────────────────────────────────────────────

# taskdef env 계약 (modules/ecs-gateway): 모든 DB 서비스가
# DB_POOL_SIZE=10 + DB_MAX_OVERFLOW=20 → 프로세스당 typical 10 / worst 30 conns.
# gateway-proxy 는 WORKERS=2, 나머지는 1 프로세스, admin-ui 는 DB 없음.
# migration 은 run-task 라 상주하지 않지만 배포 창에 겹치므로 worst 에 포함.
DB_POOL_PER_TASK: dict[str, int] = {     # typical — pool_size 만 (steady 비교용)
    "gateway-proxy":        20,   # 2 procs × 10
    "admin-api":            10,   # 1 worker
    "admin-ui":              0,   # DB 없음
    "scheduler":            10,
    "cost-recorder-worker": 10,
    "notification-worker":  10,
    "migration":            10,
}
DB_CONNS_PER_TASK: dict[str, int] = {    # worst — pool+overflow (ceiling 비교용)
    "gateway-proxy":        60,   # 2 procs × 30
    "admin-api":            30,
    "admin-ui":              0,
    "scheduler":            30,
    "cost-recorder-worker": 30,
    "notification-worker":  30,
    "migration":            30,
}

# Aurora Serverless v2: max_connections ≈ ACU × (2GiB / 9.53MiB) ≈ 225/ACU.
ACU_CONNECTIONS = 225
# provisioned db.r6g/r7g.large (16GiB) 기준 보수치.
PROVISIONED_CONNECTIONS = 1700

# 오토스케일링 상한 배수 — 트래픽을 받는 서비스만. 워커·스케줄러는 싱글톤
# 계약(스트림 consumer 이름, cron 중복 방지)이라 상한을 두지 않는다.
AUTOSCALE_MAX_FACTOR: dict[str, int] = {
    "gateway-proxy": 4,
    "admin-api":     2,
}


def effective_db_spec(cfg) -> dict:
    """tier 프리셋 + infra.db override → 실효 DB spec.

    반환 키: mode(serverless|provisioned), min_acu, max_acu, instance_class.
    schema.validate·render.ecs·ecs_db_budget 이 같은 값을 봐야 한다."""
    t = preset(cfg.deploy.size_tier)
    spec = {
        "mode": "serverless" if t.db_mode == "rds-serverless" else "provisioned",
        "min_acu": t.db_spec.get("min_acu", 0.5),
        "max_acu": t.db_spec.get("max_acu", 4.0),
        "instance_class": t.db_spec.get("instance_class", "db.r7g.large"),
    }
    db = getattr(cfg.infra, "db", None)
    if db is None:
        return spec
    if db.mode:
        spec["mode"] = db.mode
    if db.min_acu is not None:
        spec["min_acu"] = db.min_acu
    if db.max_acu is not None:
        spec["max_acu"] = db.max_acu
    if db.instance_class:
        spec["instance_class"] = db.instance_class
    return spec


def effective_cache_spec(cfg) -> dict:
    """tier 프리셋 + infra.cache override → 실효 cache spec.

    반환 키: mode(single|replicated|cluster), node_type, num_clusters,
    num_node_groups, replicas_per_node_group. 토폴로지 기본값은 effective mode 가
    결정한다 — mode 를 바꾸면 그 mode 의 기본 토폴로지로 리셋된 뒤 명시 override
    만 얹는다(이전 mode 의 값이 새 mode 에 새어 들어가지 않게)."""
    t = preset(cfg.deploy.size_tier)
    if t.cache_mode == "elasticache-single":
        mode = "single"
    else:
        mode = "cluster" if t.cache_spec.get("cluster_mode") else "replicated"
    node_type = t.cache_spec.get("node_type", "cache.t4g.small")
    c = getattr(cfg.infra, "cache", None)
    if c is not None:
        if c.mode:
            mode = c.mode
        if c.node_type:
            node_type = c.node_type
    if mode == "cluster":
        spec = {"mode": mode, "node_type": node_type,
                "num_clusters": 0,
                "num_node_groups": int(t.cache_spec.get("num_node_groups", 3)),
                "replicas_per_node_group":
                    int(t.cache_spec.get("replicas_per_node_group", 1))}
    else:
        # tier cache_spec.replicas = replica 수 → 총 노드 = 1 + replicas
        spec = {"mode": mode, "node_type": node_type,
                "num_clusters": int(t.cache_spec.get(
                    "num_clusters", 1 + t.cache_spec.get("replicas", 0))),
                "num_node_groups": 0,
                "replicas_per_node_group": 0}
    if c is not None:
        if c.num_clusters is not None:
            spec["num_clusters"] = c.num_clusters
        if c.num_node_groups is not None:
            spec["num_node_groups"] = c.num_node_groups
        if c.replicas_per_node_group is not None:
            spec["replicas_per_node_group"] = c.replicas_per_node_group
    return spec


def ecs_service_sizing(cfg) -> dict:
    """tier preset + deploy.sizing override → module service_sizing map.

    cpu/memory 는 서비스 성격으로 고정(I/O bound — SSE 스트림 수용량은
    메모리·커넥션 풀이 결정), 개수와 오토스케일 상한만 티어가 바꾼다.
    """
    spec = preset(cfg.deploy.size_tier).compute_spec
    n = int(spec.get("tasks_per_service", 1))
    gw = int(spec.get("gateway_tasks", n))
    sizing = {
        "gateway-proxy":        {"cpu": 1024, "memory": 2048, "desired_count": gw},
        "admin-api":            {"cpu": 512,  "memory": 1024, "desired_count": n},
        "admin-ui":             {"cpu": 512,  "memory": 1024, "desired_count": n},
        "scheduler":            {"cpu": 256,  "memory": 512,  "desired_count": 1},
        # 스트림 소비자는 1개만 (COST_STREAM_CONSUMER 고정 — 늘리면 consumer name 충돌)
        "cost-recorder-worker": {"cpu": 256,  "memory": 512,  "desired_count": 1},
        "notification-worker":  {"cpu": 256,  "memory": 512,  "desired_count": 1},
        # run-task 로만 실행 — desired_count 는 쓰이지 않지만 스키마상 필요
        "migration":            {"cpu": 256,  "memory": 512,  "desired_count": 0},
    }
    for name, factor in AUTOSCALE_MAX_FACTOR.items():
        sizing[name]["max_count"] = sizing[name]["desired_count"] * factor

    # deploy.capacity_tier (사용자 수 밴드) — gateway-proxy 태스크 수를 밴드로.
    # task 도 pod 와 같은 슬롯 단위(WORKERS=2 × 128 스레드 = 256/task — ecs
    # taskdefs.tf 가 같은 이미지/env 를 쓴다)라 밴드가 그대로 전사된다.
    # size_tier 의 gateway_tasks 보다 정확한 선언 — sizing 명시 키는 아래서 최종 승자.
    cap = getattr(cfg.deploy, "capacity_tier", "") or ""
    if cap and cfg.deploy.target == "ecs":
        band = EKS_CAPACITY_TIERS[cap]["gateway-proxy"]
        sizing["gateway-proxy"]["desired_count"] = band["min_replicas"]
        sizing["gateway-proxy"]["max_count"] = band["max_replicas"]

    # deploy.sizing 으로 서비스별 override 가능 (예: {"gateway-proxy": {"cpu": 2048}})
    for name, ov in (cfg.deploy.sizing or {}).items():
        if name in sizing and isinstance(ov, dict):
            sizing[name].update(ov)
            # desired 만 override 되면 상한도 같은 배수로 재계산 —
            # 프리셋 desired 로 계산된 stale max_count 가 남으면 예산 검증과
            # 실제 autoscaling 상한이 어긋난다 (max_count 명시 시에는 존중).
            # 단 capacity_tier 가 둔 max 는 의도 선언이라 덮지 않는다 —
            # 밴드 max 를 desired×factor 로 깎으면 사용자가 모르게 상한이 내려간다.
            band_max = cap and cfg.deploy.target == "ecs" \
                and name == "gateway-proxy" and "max_count" not in ov
            if (name in AUTOSCALE_MAX_FACTOR and "max_count" not in ov
                    and not band_max):
                sizing[name]["max_count"] = (sizing[name]["desired_count"]
                                            * AUTOSCALE_MAX_FACTOR[name])
    return sizing


def ecs_db_budget(cfg) -> dict:
    """ecs 경로의 worst-case DB 커넥션 수요 vs Aurora 한도.

    반환: {steady, worst, floor_capacity, ceiling_capacity}.
    - steady: pool_size(10/proc) × desired_count — 정상 수요, min ACU 바닥과 비교
    - worst: (pool+overflow 30/proc) × max(desired,max_count) — 최악 수요,
      max ACU 천장과 비교. 천장 초과 = 스케일아웃이 DB 를 고갈 → validation 에러.
    """
    if cfg.deploy.target != "ecs":
        return {}
    db = effective_db_spec(cfg)
    sizing = ecs_service_sizing(cfg)
    steady = worst = 0
    for svc, s in sizing.items():
        typical = DB_POOL_PER_TASK.get(svc, 10)
        per = DB_CONNS_PER_TASK.get(svc, 30)
        desired = int(s.get("desired_count", 0))
        cap = max(desired, int(s.get("max_count", desired)))
        steady += typical * desired
        # migration 은 run-task(상주 아님)라 desired=0 이지만 배포 창에는
        # 1 task 가 겹쳐 돈다 — worst 계산에 1 task 분을 포함시켜야
        # 배포 중 커넥션 피크가 예산 밖으로 빠지지 않는다.
        if svc == "migration":
            cap = max(cap, 1)
        worst += per * cap
    if db["mode"] == "serverless":
        floor_cap = int(db["min_acu"] * ACU_CONNECTIONS)
        ceil_cap = int(db["max_acu"] * ACU_CONNECTIONS)
    else:
        floor_cap = ceil_cap = PROVISIONED_CONNECTIONS
    return {
        "steady": steady, "worst": worst,
        "floor_capacity": floor_cap, "ceiling_capacity": ceil_cap,
    }


# ── eks 용량 밴드 (Claude Code형 인터랙티브 프로파일 기준) ─────────────────────
# size_tier 는 eks 에서 문서용이라 실제 pods/pool 은 여기서 정한다.
# 산식: 동시 스트림 ≈ 사용자 × 활성률 0.25 × 밀도 0.3 ≈ N×0.075
#       pods = 동시 스트림 ÷ 180 (256슬롯/pod × 안전율 0.7)
# 자동화/배치 비중이 크면 밀도가 ~3배 — 한 단계 위 밴드로 잡는다.
# pool_size 는 워커 프로세스당 — 노드 최대 = pool × WORKERS(2) × max_replicas.
EKS_CAPACITY_TIERS: dict[str, dict] = {
    "500": {
        "users": "~500명",
        "gateway-proxy": {"min_replicas": 1, "max_replicas": 3},
        "redis": {"pool_size": 150},
        "note": "슬롯 점유 ~15% — replicas 는 용량이 아니라 HA 용도",
    },
    "1000": {
        "users": "~1,000명",
        "gateway-proxy": {"min_replicas": 2, "max_replicas": 5},
        "redis": {"pool_size": 150},
        "note": "1pod 슬롯 ~30% — min2 는 HA+롤아웃 버스트 여유",
    },
    "3000": {
        "users": "~3,000명",
        "gateway-proxy": {"min_replicas": 3, "max_replicas": 6},
        "redis": {"pool_size": 150},
        "note": "정상 2pod 가동(슬롯 44%) — min3 는 배포 중 1pod 결손 수용",
    },
    "6000": {
        "users": "~6,000명",
        "gateway-proxy": {"min_replicas": 4, "max_replicas": 8},
        "redis": {"pool_size": 150},
        "note": "정상 3pod(슬롯 59%) — Redis 커넥션 150×2×8=2,400",
    },
    "6000+": {
        "users": "~10,000명 이상",
        "gateway-proxy": {"min_replicas": 6, "max_replicas": 15},
        "redis": {"pool_size": 150},
        "note": "정상 5pod — 이 밴드부터 ElastiCache 노드는 t4g.medium+ 검토",
    },
}


def eks_capacity_sizing(cfg) -> dict:
    """deploy.capacity_tier 프리셋 + deploy.sizing 개별 오버라이드 병합.

    반환 shape 은 render/eks.py 의 sizing 소비와 동일 (키는 schema.EKS_SIZING):
    {"gateway-proxy": {min_replicas, max_replicas, bedrock_thread_pool_size?,
                       db_*?},
     "admin-api" | "scheduler" | "notification-worker" | "cost-recorder-worker":
         {db_pool_size?, db_max_overflow?, db_pool_timeout?, db_pool_recycle?},
     "redis": {pool_size}}.
    프리셋(capacity_tier)은 gateway-proxy/redis 만 채운다. 프리셋이 바닥,
    sizing 의 명시 키가 이긴다."""
    merged: dict[str, dict] = {}
    tier = getattr(cfg.deploy, "capacity_tier", "") or ""
    if tier:
        for svc, kv in EKS_CAPACITY_TIERS[tier].items():
            if svc in ("users", "note"):
                continue
            merged[svc] = dict(kv)
    for svc, kv in (cfg.deploy.sizing or {}).items():
        merged.setdefault(svc, {}).update(kv)
    return merged


# ── eks DB 커넥션 예산 (rv-swe P1-1 — ecs_db_budget 의 eks 판) ─────────────────
# eks 는 인스턴스 크기를 스키마가 모르므로(기존 인프라 승계) 차단이 아니라 경고다.
# 절대 상한은 schema.EKS_DB_POOL_BOUNDS 가 validate 에서 건다. 수요 합산은 env
# overlay 의 replica 값이 필요해 render(eks) 단계에서 노트로 낸다.
#
# 기준 천장: dev Aurora Serverless v2 max 4 ACU. max_connections 공식
# LEAST({DBInstanceClassMemory/9531392},5000) 의 AWS 표값은 4 ACU 에서 823–844
# (플랫폼 버전별) → 하한 823 × RDS Proxy MaxConnectionsPercent 90% ≈ 741 사용 가능
# → 그 70% ≈ 518. 더 큰 클러스터(prod)라면 이 경고는 보수적이다.
EKS_DB_MAX_CONNECTIONS = 823
EKS_DB_PROXY_PERCENT = 0.9
EKS_DB_TARGET_RATIO = 0.7
EKS_DB_CONN_BUDGET = int(EKS_DB_MAX_CONNECTIONS * EKS_DB_PROXY_PERCENT
                         * EKS_DB_TARGET_RATIO)          # 518

# deploy.sizing 서비스명 → (차트 키, 서비스별 overflow env 이름, 롤아웃 surge 규칙)
# surge: "25%" = RollingUpdate maxSurge 25%(올림), "1" = maxSurge 1,
#        "0" = Recreate(scheduler singleton). 차트 templates/*/deployment.yaml 과 동일.
# 워커는 공통 DB_MAX_OVERFLOW 를 읽지 않는다 — 서비스별 override 가 없으면 코드 기본.
EKS_WORKER_OVERFLOW_DEFAULT = {"notification-worker": 3, "cost-recorder-worker": 5}
EKS_DB_SERVICES: dict[str, tuple[str, str, str]] = {
    "gateway-proxy":        ("gatewayProxy",       "DB_MAX_OVERFLOW",  "25%"),
    "admin-api":            ("adminApi",           "DB_MAX_OVERFLOW",  "25%"),
    "scheduler":            ("scheduler",          "DB_MAX_OVERFLOW",  "0"),
    "notification-worker":  ("notificationWorker", "DB_POOL_OVERFLOW", "1"),
    "cost-recorder-worker": ("costRecorderWorker", "DB_POOL_OVERFLOW", "1"),
}
# gateway-proxy hot-path 전용 풀 (app/config.py hot_db_pool_size/max_overflow 기본).
# db:/deploy.sizing 으로는 못 바꾸고 gatewayProxy.env.HOT_DB_* 로만 바뀐다.
EKS_HOT_POOL_DEFAULT = (4, 2)
# migration Job — psql init → alembic 순차 실행이라 동시 1 커넥션.
EKS_MIGRATION_CONNS = 1


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _as_int(v, default: int) -> int:
    if isinstance(v, bool):
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _first(*vals):
    for v in vals:
        if v is not None:
            return v
    return None


def eks_db_budget(cfg, values: dict) -> dict:
    """eks worst-case DB 커넥션 수요 (롤아웃 surge 포함).

    values: 차트 values.yaml → env overlay → gen 을 helm 과 같은 순서로 병합한
    values. env overlay 의 replica/풀 값까지 알아야 정확하므로 schema 가 아니라
    render(eks) 가 호출한다 — 차트 기본 replica(HPA max 20/8/6/4)로 근사하면
    dev(1 replica) 에서 거짓 경고가 난다.

    서비스당 = (HPA max 또는 replicaCount + surge) × 프로세스 × (pool + overflow).
    프로세스: gateway-proxy 는 env WORKERS(Dockerfile 기본 4), 나머지는 1.
    pool/overflow 우선순위: <svc>.db → <svc>.env → 공통 database.external.
    워커 overflow 는 서비스별 DB_POOL_OVERFLOW → 코드 기본(cost 5 / notification 3)
    이다 — 워커는 공통 DB_MAX_OVERFLOW 를 읽지 않는다.
    gateway-proxy 는 hot-path 풀(HOT_DB_*, 기본 4+2/프로세스)을 더한다.

    반환: {services: {svc: {pods, procs, pool, overflow, conns}}, hot, migration,
    total, budget}. cfg.deploy.target 이 eks 가 아니면 {}.
    """
    if cfg.deploy.target != "eks":
        return {}
    vals = values if isinstance(values, dict) else {}
    common = ((vals.get("database") or {}).get("external") or {})
    c_pool = _as_int(common.get("poolSize"), 10)
    c_ovf = _as_int(common.get("maxOverflow"), 20)
    services: dict[str, dict] = {}
    hot = 0
    for svc, (hk, ovf_env, surge) in EKS_DB_SERVICES.items():
        sv = vals.get(hk) if isinstance(vals.get(hk), dict) else {}
        if sv.get("enabled") is False:
            continue
        asg = sv.get("autoscaling") if isinstance(sv.get("autoscaling"), dict) else {}
        if svc == "scheduler":
            pods = 1                       # 템플릿이 replicas: 1 고정
        elif asg.get("enabled"):
            pods = _as_int(asg.get("maxReplicas"), 1)
        else:
            pods = _as_int(sv.get("replicaCount"), 1)
        extra = {"25%": -(-pods // 4), "1": 1, "0": 0}[surge]
        env = sv.get("env") if isinstance(sv.get("env"), dict) else {}
        db = sv.get("db") if isinstance(sv.get("db"), dict) else {}
        procs = _as_int(env.get("WORKERS"), 4) if svc == "gateway-proxy" else 1
        pool = _as_int(_first(db.get("poolSize"), env.get("DB_POOL_SIZE")), c_pool)
        ovf = _as_int(_first(db.get("maxOverflow"), env.get(ovf_env)),
                      EKS_WORKER_OVERFLOW_DEFAULT.get(svc, c_ovf))
        n = (pods + extra) * procs
        services[svc] = {"pods": pods + extra, "procs": procs, "pool": pool,
                         "overflow": ovf, "conns": n * (pool + ovf)}
        if svc == "gateway-proxy":
            hot = n * (_as_int(env.get("HOT_DB_POOL_SIZE"), EKS_HOT_POOL_DEFAULT[0])
                       + _as_int(env.get("HOT_DB_MAX_OVERFLOW"),
                                 EKS_HOT_POOL_DEFAULT[1]))
    total = sum(s["conns"] for s in services.values()) + hot + EKS_MIGRATION_CONNS
    return {"services": services, "hot": hot, "migration": EKS_MIGRATION_CONNS,
            "total": total, "budget": EKS_DB_CONN_BUDGET}


def eks_db_budget_warning(b: dict, basis: str) -> str | None:
    """예산 초과 시 경고 문구 (없으면 None)."""
    if not b or b["total"] <= b["budget"]:
        return None
    top = sorted(b["services"].items(), key=lambda kv: -kv[1]["conns"])
    parts = ", ".join(
        f"{svc} {s['pods']}pod×{s['procs']}×({s['pool']}+{s['overflow']})={s['conns']}"
        for svc, s in top)
    return (f"DB 커넥션 worst-case {b['total']}개 ({basis}; {parts}, "
            f"gateway hot pool {b['hot']}, migration {b['migration']}) 가 기준 "
            f"{b['budget']}개를 넘습니다 — 기준은 Aurora Serverless v2 4 ACU "
            f"max_connections {EKS_DB_MAX_CONNECTIONS} × RDS Proxy "
            f"{int(EKS_DB_PROXY_PERCENT * 100)}% × {int(EKS_DB_TARGET_RATIO * 100)}% "
            f"입니다. 더 큰 클러스터라면 그 max_connections 로 다시 계산하고, "
            f"아니면 deploy.sizing 의 db_pool_size/db_max_overflow/max_replicas 를 "
            f"낮추세요.")
