# 8-G. policy-helper — 직원 PC 의 Claude Code 설정을 게이트웨이가 정함

> ← [operations.md](../operations.md) §8 목차로 · 이 절 = **§8-G** · 업데이트 ID **US-21** · 등급 **선택**

Claude Code 가 시작할 때와 5분마다 `policy-helper` 가 직원의 VK 로 게이트웨이에 정책을 묻고,
받은 조각을 PC 의 기본값(`baseline.json`) 위에 얹어 그 세션의 관리형 설정으로 씁니다.
개념과 설계는 [policy-helper/README.md](../policy-helper/README.md) 에 있습니다. 여기는 적용 절차입니다.

```
Claude Code --(start, every 5 min)--> policy-helper --(VK)--> gateway GET /v1/policy
            <-- baseline + fragment --------------------       models: gateway DB
                                                               plugins, MCP, locks: hub (21-B only)
   gateway unreachable -> last cached policy -> else baseline (Claude Code always starts)
```

## 1. 두 단계 — 어디까지 할지 먼저 고릅니다

- **21-A 헬퍼만** — 관리 화면의 허용 모델이 PC 의 `/model` 목록에 그대로 반영됩니다(사용자·팀별).
  플러그인·MCP 서버·잠금은 `baseline.json` 에 적은 값이 전 직원에게 같게 적용됩니다. 여기서 멈춰도 됩니다.
- **21-B 허브 연결** — 플러그인·MCP 서버·잠금을 팀별로 화면에서 정하고, 승인 흐름과 활동 기록을
  씁니다. AI Governance Hub(별도 저장소)를 설치해야 합니다.
- **아무것도 하지 않으면** — 새 이미지에 코드는 들어 있지만 달라지는 것이 없습니다. PC 에 헬퍼가
  없으면 `/v1/policy` 를 부르는 것이 없고, 추론·예산·요청 제한 경로는 바뀌지 않습니다.

## 2. 21-A 헬퍼만

### (1) 게이트웨이 — 배포 EC2

[8-D](8-D-upstream-sync.md#us-21-로-따라-할-때-달라지는-것) 를 「US-21」 차이대로 합니다.
바뀌는 것은 gateway-proxy 이미지 하나이고 DB·terraform·단가 변경은 없습니다(약 30분).

▶ 확인 — 배포 EC2
```bash
cd ~/awsome-ai-gateway/docs/us-llm-gateway/update-scripts && bash status.sh
```
📋 `US-21` 줄이 `policy-helper 경로(/v1/policy) — 있음 · 허브 연결 안 함`.

### (2) 직원 PC — 기준값 만들기

`baseline.json` 은 지금 PC 의 관리형 설정 전부입니다. 헬퍼가 값을 내면 Claude Code 는 관리형
설정 파일의 다른 키를 읽지 않으므로, **여기에 빠진 키는 적용되지 않습니다**(apiKeyHelper·env 포함).

- Mac: `python3 make-baseline-mac.py` 가 지금 적용 중인 설정을 그대로 옮겨 `~/ph-build/baseline.json` 을 만듭니다.
  먼저 `bash check-mac.sh` 로 상태를 봅니다(읽기만 함).
- Windows: `baseline.example.json` 을 복사해 지금 `C:\Program Files\ClaudeCode\managed-settings.json` 의 키를 옮깁니다.
- 주소 같은 배포 값이 들어가므로 저장소에 올리지 않습니다.

### (3) 직원 PC — 빌드·설치·연결

저장소의 `projects/awsome-ai-gateway/policy-helper/` 에서 합니다(Python 3.11 이상).

▶ Mac
```bash
bash build.sh ~/ph-build/baseline.json
sudo bash install-mac.sh dist/policy-helper-mac
```
📋 `install-mac.sh` 는 백업(`~/ph-backup-<시각>/`) → 설치 → 세 번 실행해 시간 확인 → 연결 순서로
진행하고, 실행이 실패하거나 5초를 넘으면 연결 전에 멈춥니다.

▶ Windows(관리자 PowerShell)
```powershell
powershell -ExecutionPolicy Bypass -File .\build.ps1 -Baseline C:\build\baseline.json
```
그 뒤 `dist\policy-helper\` 를 `C:\Program Files\PolicyHelper\` 로 복사하고 `managed-settings.json` 에
`policyHelper` 블록을 넣습니다. 명령은 [poc-step1-windows.md](../policy-helper/poc-step1-windows.md) 5~6절 그대로입니다.

### (4) 확인 — 직원 PC

- Claude Code 를 새로 시작 → `/status` 에 `Enterprise managed settings (helper)`.
- 관리 화면에서 그 직원 팀의 허용 모델을 줄이고 Claude Code 를 다시 시작 → `/model` 에 그 모델만.
- 헬퍼는 유효한 VK 가 있어야 묻습니다. VK 가 만료됐으면 Claude Code 에서 한 번 대화한 뒤 다시 시작합니다.

## 3. 21-B 허브 연결 (선택)

허브 설치와 값 넣기는 허브 저장소가 맡습니다: https://github.com/gonsoomoon-ml/ai-governance-hub

게이트웨이 쪽에서 바뀌는 것은 values 의 `gatewayProxy.env` 두 줄(`POLICY_HUB_URL` · `POLICY_HUB_TOKEN`)뿐이고,
허브 저장소의 `deploy/hub/50-gateway-values.sh` 가 넣습니다. 그 뒤 `install-eks.sh` 로 배포합니다.

📋 `status.sh` 의 `US-21` 줄이 `… · 허브 연결함`.

## 4. 되돌리기

- **PC** — Mac: `sudo bash rollback-mac.sh ~/ph-backup-<시각>` · Windows: `managed-settings.json` 에서
  `policyHelper` 블록 삭제([poc-step1-windows.md](../policy-helper/poc-step1-windows.md) 「되돌리기」). 다음 실행부터 적용됩니다.
- **허브 연결** — values 에서 두 줄을 빼고 `install-eks.sh`. 21-A 상태로 돌아갑니다.
- **게이트웨이** — `helm rollback`(DB 변경 없음). PC 에 헬퍼가 남아 있어도 캐시·기본값으로 시작합니다.

## 5. 알아 둘 것

- **허브를 연결한 뒤 허브가 멈추면** `/v1/policy` 는 503 이고 PC 는 마지막으로 받은 정책을 그대로 씁니다.
  모델만 담은 답으로 바꾸지 않는 이유는, 그러면 모든 PC 에서 플러그인·MCP·잠금이 한꺼번에 풀리기 때문입니다.
- **모델은 서버에서도 막습니다.** PC 의 목록은 보여 주는 것이고, 허용 밖 모델 요청은 게이트웨이가 거부합니다.
- **허브가 정할 수 있는 키는 8개뿐**입니다(플러그인 출처·설치·MCP 서버·잠금·`permissions.deny`). 그 밖의 키는 게이트웨이가 버립니다.
- **Mac 에 Cowork 가 있으면** `install-mac.sh` 가 파일에도 `disableSideloadFlags: true` 를 둡니다. 빼면 Cowork 가 시작하지 못합니다.
- **시제품 한계** — PC 의 정책 캐시에 서명이 없습니다. 목록형 키는 조각이 기본값을 통째로 바꿉니다(합치지 않음).
