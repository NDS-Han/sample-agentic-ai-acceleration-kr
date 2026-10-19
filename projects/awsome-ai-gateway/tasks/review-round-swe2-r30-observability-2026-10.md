# R30 — Observability 스택 리뷰 (config/ + deployment/observability/) — swe-2 직접 리뷰

날짜: 2026-10-19 | 범위: `config/`(compose 관측/캐시 설정) + `deployment/observability/`(EKS 관측 스택)

## 배경

R18–R29가 앱/배포 코드를 전부 다뤘지만 관측 파이프라인 자체는 미검토였다.
리뷰 중 dev 클러스터 실측으로 **라이브 버그 1건**을 찾아 수정·검증했다.

## 발견·수정

### M1 — OTel `cluster` 라벨 하드코딩 (라이브 오표기)

`deployment/observability/otel-collector/configmap.yaml`의 resource processor가
`cluster=llm-gateway-prod`를 하드코딩 — **dev Prometheus의 모든 gateway 메트릭이
`cluster="llm-gateway-prod"`로 기록**되고 있었다(실측: `count by (cluster)
(gateway_request_total)` → `{cluster: 'llm-gateway-prod'}`).

수정: `${env:CLUSTER_NAME}` env 확장 + Deployment env 추가 + `install.sh`가
`kubectl set env`로 현재 context 이름 주입(`CLUSTER_NAME` env 명시 가능).
적용 후 실측: 신규 시리즈 `cluster="llm-gateway-dev"` 정상 라벨링.

### M2 — Grafana CloudWatch `defaultRegion` 하드코딩

`kube-prometheus-stack/values.yaml`에 `ap-northeast-2` 고정 — dev(ap-south-1)에서
CW datasource가 틀린 리전을 조회. `install.sh`가 `GRAFANA_CW_REGION` →
`aws configure` → `describe-cluster`(context명=클러스터명 규약) 순으로 감지해
`--set` 주입.

### N1 — compose OTel `debug` exporter `verbosity: detailed`

메트릭 배치 전량(라벨 값 포함)을 collector 로그에 덤프 → `basic`으로.

### N2 — compose Redis `volatile-lru`

TTL 있는 키만 퇴거 — TTL 없는 키가 쌓이면 maxmemory 도달 시 쓰기 전부 OOM 거부.
캐시 전용 인스턴스(`save ""`, no AOF)이므로 `allkeys-lru`가 정답.

## 검토 통과 항목

- kps values: alertmanager/webhook 비활성(Fargate 안정성), remote-write receiver,
  resource limits, IRSA 분리 — 합리적
- prometheus-adapter: Fargate pod-level resource rules(container label 함정 문서화)
- otel-collector Deployment: 보안 컨텍스트(readOnlyRoot/drop ALL/nonRoot) 충실
- admin-secret.sh: openssl 랜덤 + idempotent
- uninstall.sh: HPA 영향 명시적 경고 + 확인 프롬프트
- install.sh idempotent / metrics-server 충돌 경고 / 검증 루프

## 메모

- `namespace.yaml`의 `purpose: load-test` 라벨은 낡음 — 현재 상시 운영 스택
  (README가 "상시 운영 관측성"이라 명시). 라벨만 정리하면 됨(기능 영향 없음)
- Prometheus emptyDir + 24h retention — 의도된 문서화 제한(AMP/PV는 별개 결정)
- OTel replicas=1 — 문서화된 제한(remote-write out-of-order). 상시 운영은
  loadbalancing exporter + statefulset 이 별개 작업
- dev 클러스터에 스택이 실제 배포돼 있어(60d) "load-test 전용" 가정은 이미 깨짐

## 검증

- yaml/구문/shellcheck 통과(선존 SC2034·SC2086 1건 함께 정리)
- **dev 라이브 적용·검증**: configmap+deployment apply → `set env CLUSTER_NAME` →
  rollout → PromQL로 신규 시리즈 라벨 교정 확인
