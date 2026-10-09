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
        # render 가 읽는 값만 둬서 "정의된 것과 실제" 가 어긋나지 않게)
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
