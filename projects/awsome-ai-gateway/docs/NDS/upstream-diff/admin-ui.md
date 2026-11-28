# 관리자 콘솔

**한국어** · [English](admin-ui.en.md) · [비교 목차](README.md)

## 메뉴별 변화

| 메뉴 | NDS에서 확장된 운영 동선 |
|---|---|
| Dashboard | 앱·기간 선택, 모델 비용과 검색 비용, 사용량과 예산, 대시보드 집계 시간 |
| Users/Teams | 사용자·팀 딥링크, 유효 정책과 상속, Cognito 동기화, 팀 재인증, 레이트리밋, 다운그레이드와 검색 쿼터 |
| Model Management | 별칭·ID·context spec, 계정 라우팅, 장문 가격, 공식 단가 선택 미리보기·적용, 비활성화·삭제 영향 확인 |
| App Policy | `/apps`에서 앱별 기본 모델 및 접근 허용을 설정 |
| Budget Management | 사용자·팀 cap, 분배·약정률, 앱별 조회와 일괄 작업 |
| API Keys | 직원 VK 상태·만료·폐기 확인 |
| Agents | 서비스 계정 장기키 발급, 회전과 인증 복구. `/agents/[userId]`의 세부 동선 |
| Monitoring | 필터와 요청 추적, 지원되는 환경에서 `/monitoring/body-logs` |
| Analytics | 사용자·모델, 앱, 임의 기간과 시간대, CSV/JSON 등 집계 |
| BI Insight | `/chat`에서 대화형 분석. 인프라와 기능 플래그에 따라 노출 |
| Audit Log | `/audit`에서 관리 이벤트와 변경 대상 조회 |
| Integration | `/integration`에서 승인된 클라이언트·외부 앱 연동 안내 |
| Organization Policy | 조직 수준 기본값·유효 정책 확인. 사용자·팀 설정과 대조 |

`/service-accounts`와 `/rate-limits`에는 기존 사용자를 연결하는 리다이렉트가 있습니다. `/analytics/models`에도 호환 이동 경로가 있습니다. 콘솔 메뉴와 라우트는 역할·플래그·이미지에 따라 달라집니다. [관리자 상세 가이드](../guides/admin-guide.ko.md) · [UI 소스](../../../admin-ui/src)

## 변경 확인 방법

한·영 로케일을 지원하고 보고 시간대를 기준으로 동일 날짜 범위를 조회합니다. 관리자가 변경한 뒤 **Apply** 결과와 **Audit Log**를 확인하고, 사용자나 팀 리더의 실제 권한으로 같은 정책을 확인합니다. `/v1/auth/provision` 실패 시 로그인을 완료하지 않으며 직원은 역할에 맞는 페이지를 사용합니다. 비용과 단가 조회는 설치된 백엔드·가격 소스·배포 버전에 영향을 받습니다. 캡처와 소스 비교에 표시된 서비스 버전을 확인합니다. [비교 범위](README.md) · [운영 기준점](../op-guide/README.ko.md)
