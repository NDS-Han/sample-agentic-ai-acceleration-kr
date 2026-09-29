# NDS-02. BI Insight (admin-chat-agent) 배포

> **NDS 전용 절차** (upstream 대상 아님). BI Insight = admin-ui 의 `/chat`
> (심층 분석) + 퀵챗 드로어입니다. 백엔드는 `admin-chat-agent`를 담은
> **AgentCore Runtime**으로, EKS 릴리스와 독립적으로 배포됩니다.

## 왜

- BI Insight는 별도 인프라(ECR·S3 staging·IAM·BI tool Lambdas·AgentCore
  runtime)가 필요한 선택 기능입니다. 배포되지 않은 환경에서 메뉴가 보이면
  클릭해도 503만 나는 죽은 화면이 됩니다.
- 그래서 **배포 여부는 `adminUi.env.CHAT_ENABLED`로 표시**하고, admin-ui는
  이 값이 `"true"`가 아니면 사이드바 BI Insight 항목·퀵챗 FAB·`/chat`
  페이지를 전부 숨깁니다(`layout.tsx` → `Sidebar`, `ChatShell`, `chat/page.tsx`).

## 구성 요소

| 요소 | 내용 |
|---|---|
| terraform 모듈 | `module.agentcore_runtime` (`enable_chat_agent`, `enable_chat_db_tools`) — ECR·S3 staging·실행 role·query_db/get_schema Lambda |
| 이미지 | `admin-chat-agent` — **arm64 전용**(AgentCore = Graviton microVM) |
| runtime | `bedrock-agentcore-control` create/update-agent-runtime, networkMode PUBLIC, SigV4 인증 |
| values 배선 | `adminApi.env.AGENTCORE_RUNTIME_ARN`·`AGENTCORE_REGION`·`CHAT_STAGING_BUCKET`, `adminUi.env.CHAT_ENABLED` |

미배포 시 admin-api의 chat 엔드포인트는 503 "AgentCore runtime not configured"를
반환하고(`chat_agent.py`), admin-ui는 관련 UI를 렌더하지 않습니다.

## 배포

```bash
cd docs/us-llm-gateway/update-scripts
./NDS-02-deploy-bi-insight.sh           # 상태 확인 (읽기 전용)
./NDS-02-deploy-bi-insight.sh --apply   # infra → 이미지 → runtime → values
./deployment/scripts/install-eks.sh dev # values 반영 (롤아웃)
```

`--apply` 순서:

1. `admin-chat-agent/lambdas/build-lambdas.sh` — query_db/get_schema zip 산출물
   (manylinux 휠, 모듈이 zip으로 묶는 `build/` 트리 생성)
2. `terraform.tfvars`에 `enable_chat_agent=true`, `enable_chat_db_tools=true`
   → `terraform apply -target=module.agentcore_runtime`
3. `admin-chat-agent` 이미지 arm64 빌드 → ECR push
   (`<pyproject version>-arm64` 태그; immutable ECR이므로 코드 변경 시 version 상승 필요)
4. AgentCore runtime 생성/갱신 → READY 대기(최대 5분)
5. admin-api IRSA role에 `bedrock-agentcore:InvokeAgentRuntime` 인라인 정책 부여
   (terraform 모듈이 부여하지 않으므로 스크립트가 실제 runtime ARN으로 좁혀 부여)
6. values 주입: `AGENTCORE_RUNTIME_ARN`, `AGENTCORE_REGION=$AWS_REGION`,
   `CHAT_STAGING_BUCKET`, `CHAT_ENABLED=true`

## 제거

```bash
./NDS-02-deploy-bi-insight.sh --delete
./deployment/scripts/install-eks.sh dev
```

invoke 정책 제거 → runtime 삭제 → tfvars 두 플래그 `false` → `terraform apply
-target`로 모듈 파괴(ECR 리포·S3 버킷·Lambda·role 포함) → values 정리
(`CHAT_ENABLED=false`).

## 검증

- status: `./NDS-02-deploy-bi-insight.sh` — tfvars/tf 출력/런타임 상태/values 배선
- admin-api: `GET /admin/chat/sessions`가 503이 아닌 정상 응답
- admin-ui: 로그인 후 사이드바에 **BI Insight** 메뉴 + 우하단 퀵챗 버튼 표시
- 직접 invoke: README의 SigV4 `invoke_agent_runtime` 스니펫

## 주의

- arm64 빌드는 x86_64 호스트에서 docker buildx qemu(binfmt)가 필요합니다.
- ECR 태그 immutable — 재배포는 `admin-chat-agent/pyproject.toml` version 상승.
- runtime 이름은 하이픈 불가 → 모듈의 agent_name을 `tr '-' '_'`로 변환합니다.
- 인증은 SigV4 — admin-api가 IAM으로 `InvokeAgentRuntime`을 호출합니다.
  권한은 `--apply`가 admin-api IRSA role에 인라인 정책(`BIInsightInvoke`)으로 부여합니다.
