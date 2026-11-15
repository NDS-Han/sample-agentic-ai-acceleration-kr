# 리뷰: 용량 티어·사이징·DB/Redis 예산

**범위**: `deploy/tiers.py`(preset/ecs_db_budget/eks_capacity_sizing/
ecs_service_sizing), `deploy/schema.py` 의 `size_tier`/`capacity_tier`/
`deploy.sizing`, `deploy/capture.py` 의 밴드 매칭.

**판정: 수렴**

## 계약

- `size_tier`(t0/t1/t2/t3)는 target 별 허용 대역 강제 — compose t0,
  ecs t1/t2, eks t2/t3.
- `capacity_tier`("500"~"6000+")는 인터랙티브 Claude Code 워크로드 밴드 —
  eks HPA/pool 프리셋·ecs gateway task 수로 확장.
- `deploy.sizing.<svc>` 명시 override 가 프리셋을 이긴다 — ecs 는
  cpu/memory/desired/min/max_count 키만 허용.
- DB 커넥션 예산: `pool_size×procs×worst`(HPA 상한)가 Aurora ACU 상한의
  80% 를 넘으면 스키마 오류 — 상한은 경고가 아니라 거부.

## 발견·수정된 결함

### HIGH

- **capacity_tier configure 소실** (19차): wizard 가 미질문 키를 버려
  다음 apply 가 프리셋으로 조용히 축소 → deploy.* 전체 보존.
- **migration DB 예산 제외** (18차): 주석은 고려한다고 쓰여 있으나 코드는
  desired=0 으로 worst 에 0 기여 → worst-case 계산에 migration +1 task
  반영(예산 초과 오탐 방향이 아니라 방어 방향).

### MEDIUM

- **validate() 2차 예외** (18차): 잘못된 `size_tier` 로 `preset()` 호출,
  비정수 sizing 으로 예산 계산 → 유효성 가드 우선으로 선언 오류만 보고.
- **capacity_tier→ecs 밴드 매핑**: 선언 `max_count` 를 프리셋이 덮지 않게
  보호 (`tiers.py` 매핑 가드).
- **캡처 밴드 매칭**: 라이브 HPA/pool 이 밴드와 정확히 일치하면
  `capacity_tier` 키로, 불일치면 `deploy.sizing` 실측으로 기록 —
  채택 yaml 이 의도와 실측을 구분한다.
- **scalable-targets min/max 캡처** (18차): 수동 상한이 render 의
  desired×factor 재계산으로 유실되던 경로 복원.

## 회귀 테스트

`test_render_ecs.py`(예산·max_count 거부·migration 반영),
`test_capture.py`(밴드 매칭), `test_schema.py`(invalid tier 가드).

## 잔여 리스크

- Redis `maxclients` 예산 검증은 스키마 경고 — 실제 노드 사양과의 정합은
  운영 확인 영역.
- capacity_tier 는 인터랙티브 워크로드 전제 — batch/백그라운드 비중이
  높으면 보수적 상한으로 동작하므로 `deploy.sizing` 명시를 권장.
