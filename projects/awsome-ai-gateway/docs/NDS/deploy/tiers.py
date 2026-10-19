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

    # deploy.sizing 으로 서비스별 override 가능 (예: {"gateway-proxy": {"cpu": 2048}})
    for name, ov in (cfg.deploy.sizing or {}).items():
        if name in sizing and isinstance(ov, dict):
            sizing[name].update(ov)
            # desired 만 override 되면 상한도 같은 배수로 재계산 —
            # 프리셋 desired 로 계산된 stale max_count 가 남으면 예산 검증과
            # 실제 autoscaling 상한이 어긋난다 (max_count 명시 시에는 존중)
            if name in AUTOSCALE_MAX_FACTOR and "max_count" not in ov:
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
