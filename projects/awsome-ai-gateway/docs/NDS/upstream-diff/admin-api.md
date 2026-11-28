# 관리 API와 데이터 계약

**한국어** · [English](admin-api.en.md) · [비교 목차](README.md)

## 라우터와 관리 기능

기준 트리에 없던 라우터 모듈은 8개입니다. `apps.py`는 앱 기본 모델과 모델 접근, `audit_reconcile.py`는 감사 대조, `body_logs.py`는 제한된 본문 로그, `service_accounts.py`는 장기 서비스 키, `settings.py`는 운영 설정을 담당합니다. `web_search_quota.py`는 검색 정책·사용량, `aicm_admin.py`는 AICM 미러, `managed_fields.py`는 필드 소유권을 다룹니다. 실제 HTTP 마운트는 앱의 [router 등록](../../../admin-api/src/app/main.py)에 있습니다.

기존 API에서는 Cognito 전체·개별 동기화, 조직·팀·사용자 정책의 유효값, 모델 장문 가격·계정 라우팅·공식 단가 미리보기, 임의 기간 및 앱별 Analytics, 사용 로그 및 감사 조회를 확장했습니다. 설정 변경 후 Redis 무효화와 요청 ID 전파를 보강했습니다. 일반 서비스 토큰 계약과 통합 토큰 계약을 분리한 수정은 `8040b1d5`에 포함됩니다.

## AICM 관리 경계

- `AICM_API_ENABLED`로 `/aicm/v1/admin/*` 미러를 제어합니다. allowlist에 정책 엔트리 26개와 고유 HTTP method/path 25개가 있습니다. budget summary의 두 엔트리는 같은 주소에 서로 다른 조회 범위를 적용합니다.
- 인증된 관리자가 `POST /admin/service-tokens`로 정해진 scope의 `svc-` 토큰을 발급합니다. 일반 서비스 토큰과 사용자 JWT는 해당 미러에서 거부되며, scoped 토큰은 일반 관리자 경로에 사용할 수 없습니다. scope, CIDR, 레이트리밋, 만료, 회전을 확인합니다.
- 이전 `itg-` 자격 및 레거시 발급·provision·deactivate 경로를 제거했습니다. 기존 통합은 최신 계약과 호출 범위를 검토하고 토큰을 교체해야 합니다.
- 보호 대상인 관리자·시스템·서비스 계정에 대한 AICM 변경을 제한합니다. 사용자의 누적 spend 일괄 입력은 원자적이고 감사 대상으로 기록됩니다.
- `AICM_MANAGED_LOCK_ENABLED`를 켜면 AICM 소유 필드의 사람 변경은 충돌로 응답할 수 있습니다. 필요한 경우 소유권과 `RELEASE_AICM_MANAGED_FIELD` 감사 이력을 확인하고 해제합니다.

[미러 allowlist](../../../admin-api/src/app/routers/aicm_admin.py) · [필드 소유권 라우터](../../../admin-api/src/app/routers/managed_fields.py) · [연동 고객 계약](../aicm/README.ko.md)

## 예산 및 스키마

| 구간 | 주요 변경 |
|---|---|
| 0024–0057 | 유일성·조회 인덱스, 모델 별칭·라우팅·가격·장문 요금, 앱 허용 계층, 팀 기본 사용자 한도, 알림, 팀 배정 출처, 서비스 계정·키 해시, 사용 session ID, BI 보고서 |
| 0058–0061 | 웹 검색 비용·정책과 v2 일·월·시간 집계, 재계산 대상 표시 |
| 0062–0063 | 비용 정밀성과 내구성 |
| 0064–0065 | 범위가 제한된 통합 토큰과 idempotency 스키마. 최종 미러는 이 idempotency 테이블을 사용하지 않음 |
| 0069–0071 | 비용 알림 설정, 앱 그룹 예산, AICM 필드 소유권 |

마이그레이션 파일은 기준 27개에서 종료점 72개로 45개 증가했습니다. 새 번호는 0024–0071이며 0066–0068은 종료점에 없습니다. Claude Code/Cowork별 앱 그룹 예산은 기존 사용자 앱 예산과 함께 적용하며, `0`과 그룹 제한 삭제가 서로 다른 의미를 가집니다. 변경 계획에는 현재 DB 리비전과 직전 이미지의 스키마 호환성을 포함합니다. [마이그레이션](../../../db/versions) · [운영 인수인계](../op-guide/README.ko.md)
