# NDS Gateway 관리자 상세 가이드

**한국어** · [English HTML](admin-guide.en.html) · [NDS 문서](../README.md)

dev 관리자 콘솔에서 계정, 모델, 앱 정책, 예산, 자격 증명과 사용량을 관리하는 절차입니다. 화면은 관리자 UI `4fe6d927` 기준입니다. [관리자 화면 모음](admin-screens.md)에는 메뉴별 진입 경로와 원본 캡처가 있습니다.

| 목적 | 확인할 것 |
|---|---|
| 계정 등록 | 조직·팀·역할, 적용되는 모델과 앱 정책 |
| 모델 신규 제공 | 별칭의 상태와 단가, 앱 라우팅, 사용자 접근 |
| 비용 관리 | 앱·조회 기간, 팀과 사용자 한도, 예산 초과 정책 |
| 애플리케이션 연결 | 게이트웨이 주소, 모델 별칭, 자격 증명 종류 |
| 장애 조사 | 요청 ID, 시각, 앱과 모델, 관리자 변경 기록 |

저장 전에 콘솔 환경과 적용 범위를 확인합니다. 저장한 뒤 동일한 화면에서 적용된 값을, **Audit Log**에서 기록된 관리자 이벤트를 확인합니다.

## 로그인과 비밀번호 재설정

1. [dev 관리자 콘솔](https://admin-dev.ssir-dev.nds.remu.one/)에서 Cognito 이메일과 비밀번호를 입력하고 **Sign in**을 선택합니다.
2. 관리자 권한이 적용되면 **Dashboard**가 열립니다. 비밀번호를 잊었다면 **Forgot your password?**에서 이메일을 입력하고 재설정 안내를 따릅니다.
3. **User registration failed**에서는 그룹과 팀 매핑을 확인합니다. **403**에서는 필요한 페이지 역할이 있는지 확인합니다.

첫 웹 로그인에서 게이트웨이 사용자와 팀·역할이 등록됩니다. CLI용 Virtual Key는 `gateway-cli login`으로 발급받습니다. [로그인 화면과 오류 안내](login.md) · [ADFS 그룹 매핑](../ad-sync.md)

## Dashboard

**All**, **Claude Code**, **Cowork**, **Codex**, **Other** 중 앱을 선택합니다. **This month**, **Last month**, **Select period**에서 조회 기간을 맞추고 갱신 시각과 시간대를 함께 확인합니다. 비용·예산 사용률, 요청 수, 토큰을 비교합니다. 웹 검색 비용 범위는 **About web search pricing**에서, 세부 추이는 **Analytics**에서 확인합니다. [Dashboard 전체 화면](screenshots/admin/01-dashboard.png)

## Users/Teams

1. 조직 트리에서 부서와 팀을 펼치거나 **Search organization**으로 사용자를 찾습니다.
2. 팀 또는 사용자 상세의 **App access**, **Allowed models**, **Rate limit**에서 설정과 상속 여부를 확인합니다. 사용자에게 적용되는 조합은 **Show full model × app table**에 표시됩니다.
3. 팀 상세의 **Change leader**에서 리더를 변경할 수 있습니다. **Force re-authentication for team**은 팀의 활성 키에 영향을 주므로 확인 창에서 대상을 검토합니다.
4. **Sync Cognito**는 Cognito에 있는 사용자와 그룹을 일괄 반영합니다. 첫 웹 로그인에 따른 등록과 배치 동기화는 [운영 런북](../operations.md)을 참고합니다.

[Users/Teams 화면](screenshots/admin/02-users-teams.png)

## Model Management

**Add model**에서 공급자, 제공자 모델 ID, 게이트웨이 별칭과 지원 한도를 설정합니다. 단가는 화면의 **USD / 1M tokens**를 기준으로 확인합니다. 기존 모델은 행을 펼쳐 상세를 확인하고 **Edit**으로 수정합니다. **Sync Pricing**에서 단가 변경을 미리 보고 적용할 별칭을 선택합니다.

**Deactivate**는 새 요청을 중단합니다. 삭제는 가격 이력과 연결 설정을 제거하고 사용량 이력을 보존합니다. 별칭이 다른 모델의 다운그레이드 목적지면 관련 규칙을 먼저 정리합니다. [모델 관리 절차와 확인 화면](models.md)

## App Policy

목적에 맞는 **Claude Code**, **Cowork**, **Codex** 탭을 엽니다. 기본 모델, 라우팅, 웹 검색, 허용 모델과 적용 대상을 확인합니다. 저장 뒤 **Users/Teams**에서 대표 사용자의 유효 정책을 확인하고, 라우팅 변경은 의도한 앱의 요청으로 확인합니다. [App Policy 화면](screenshots/admin/04-app-policy.png)

## Budget Management

1. 팀 또는 확장된 팀의 사용자 행에서 **Set Budget**을 엽니다. 여러 사용자는 **Bulk edit member budgets**에서 대상 행을 확인합니다.
2. 적용 금액과 집행 정책을 선택합니다. **Hard Block**은 초과 요청을 차단하고, **Warning Only**는 경고를 보내며, **Throttle**은 요청 속도를 줄입니다. 개인 한도가 팀보다 커도 팀 한도는 적용됩니다.
3. **Web search quota**에서 조직·팀 풀을 확인합니다. 사용자 예외는 **User overrides**에서 찾습니다.
4. 저장 전에 자동 다운그레이드 여부를 확인합니다. 현재 사용량이 이미 임계값에 도달했다면 즉시 적용될 수 있습니다. **Switch to Team Budget**을 선택하면 개인 예산이 제거됩니다.

[Budget Management 화면](screenshots/admin/05-budget-management.png)

## API Keys와 Agents

**API Keys**에서 **Active**, **Expired**, **Revoked** 상태를 살핍니다. 소유자와 접두어가 일치하는지 확인한 뒤 필요한 키만 폐기합니다. **Revoke** 확정 즉시 키를 사용할 수 없게 됩니다.

서버 앱용 키는 **Agents → Issue long-lived key**에서 발급합니다. 대상 계정과 수명을 확인하고 승인된 비밀 저장소에 보관합니다. 키를 교체하면 기존 키가 즉시 만료되므로 앱의 비밀 갱신을 준비합니다. 에이전트 상세에서 비용과 토큰을 조회하고, 유효한 키의 인증 매핑이 유실된 경우 **Repair auth**를 확인합니다.

[API Keys 화면](screenshots/admin/06-api-keys.png) · [장기 키 발급 양식](screenshots/admin/69-agents-key-issue-form.png)

## Monitoring과 Analytics

**Monitoring → Usage log**의 요청 유형과 시간을 좁혀 요청 ID를 찾습니다. **Abnormal**은 오류, 타임아웃, 느린 요청을 모아 표시합니다. 본문 로그가 활성화된 환경에서는 **Sessions** 또는 **By request**로 요청을 특정한 뒤 필요한 범위만 확인합니다.

**Analytics**는 기간을 선택하고 **By Model**, **By Team**, **By User**로 집계합니다. 사용자별 모델·토큰 분해를 펼치거나 내보내기를 사용할 때는 앱·기간·집계 기준을 같이 기록합니다. [Monitoring 화면](screenshots/admin/08-monitoring.png) · [Analytics 화면](screenshots/admin/09-analytics.png)

## BI Insight와 Audit Log

**BI Insight**가 활성화된 환경에서는 질문에 지표와 기간, 앱 또는 팀을 명시합니다. 정책 변경에 활용하기 전 **Analytics**에서 결과를 대조합니다. **Audit Log**에서 시간, 분류, 동작 또는 수행자로 관리자 변경을 찾습니다. **Show details**에서 대상을 확인하고, 정확한 기록 필드가 필요한 경우 **Raw JSON**을 봅니다. [BI Insight 화면](screenshots/admin/10-bi-insight.png) · [Audit Log 화면](screenshots/admin/11-audit-log.png)

## Integration과 CLI

서버 앱은 **Integration**에서 Claude, OpenAI, LangChain 예제를 선택합니다. **Agents**에서 발급받은 장기 VK를 비밀 저장소에서 불러와 Bearer 자격 증명으로 사용합니다. 등록된 별칭과 API 계열에 맞는 엔드포인트로 연결을 확인합니다.

직원 PC는 **CLI**의 운영체제별 안내를 따릅니다. `gateway-cli login`에서 브라우저 인증을 마치고 Gateway URL을 설정한 뒤 `gateway-cli status`로 확인합니다. [Integration 화면](screenshots/admin/12-integration.png) · [CLI 화면](screenshots/admin/13-cli.png) · [직원 PC 연결](../client-setup.md)

## 문제 해결

| 증상 | 확인 순서 |
|---|---|
| 등록 또는 콘솔 접근 실패 | 그룹 클레임 → 팀 매핑 → 페이지 역할 |
| 서버 앱에서 401 | 게이트웨이 주소 → 현재 키 상태 → 교체 또는 매핑 유실 |
| 모델 사용 불가 | 등록 별칭 → 활성 상태 → API 계열 → 앱·모델 유효 정책 |
| 요청 제한 | 사용자·팀의 **Rate limit** → 예산 사용률 및 초과 정책 |
| 보고 화면마다 비용이 다름 | 조회 시간대와 기간 → 앱·팀 범위 → 웹 검색 비용 포함 여부 |

[관리자 화면 모음](admin-screens.md) · [운영·배포 인수인계](../op-guide/README.ko.md)
