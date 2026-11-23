# Cognito Managed Login 로그인 화면

`gateway.yaml` 의 `branding.managed_login: true` 로 켜는 로그인 화면입니다.
ecs/eks 전용(Cognito 를 terraform `module.cognito` 가 만드는 배포).

## 무엇이 바뀌나

| 항목 | 값 | 변경 방식 |
|---|---|---|
| user pool 요금제 | `ESSENTIALS` | in-place (이미 Essentials 면 변화 없음) |
| Hosted UI 도메인 | `managed_login_version = 2` | in-place (도메인 재생성 없음) |
| 브랜딩 스타일 | `awscc_cognito_managed_login_branding.this` | 생성 — 앱 클라이언트(`cli`) 1개에 1개. 내용 변경은 교체(destroy+create, ~1분) |
| classic `ui_customization` | 그대로 둠 | Managed Login 에서는 안 쓰임. 되돌리면 다시 쓰임 |

admin-ui 와 gateway-cli 가 같은 앱 클라이언트를 쓰므로 두 로그인 화면이 같아집니다.

## 디자인 자산 — `deployment/terraform/modules/cognito/managed-login/`

| 파일 | 내용 |
|---|---|
| `settings.json` | 브랜딩 설정(색·폼 위치·헤더/푸터 끔 등). 색은 `#` 없는 RGBA hex |
| `background.svg` | 페이지 배경 — 왼쪽 그래프 패널(노드·엣지 + CSS @keyframes 움직임 + 영문 문구), 오른쪽 폼 영역 |
| `generate_background.py` | `background.svg` 생성기 — 문구·노드를 바꾸면 `python3 generate_background.py` 후 커밋 |
| `favicon.svg` | 탭 아이콘(그래프 노드 마크 — admin-ui `src/app/icon.svg` 와 같은 도형, `prefers-color-scheme` 로 라이트/다크 색 전환) |

- 폼 정렬 — Cognito 폼은 END 정렬(402px 카드 + 81px 여백)이라, 흰 영역을
  `calc(100% - 564px)`로 두고 나머지를 패널이 채웁니다. 1024px 미만은 패널이
  전체를 덮고 문구를 숨깁니다(폼 카드가 경계에 걸치지 않게).
- 에셋 검증 — `<animate>`/`<animateMotion>`(SMIL)은 Cognito 가 **거부**합니다.
  `<style>` 안의 CSS `@keyframes`는 허용됩니다 — 움직임은 CSS 로만 둡니다.
- 에셋/설정을 바꾸면 `terraform_data.managed_login_branding_rev` 해시 트리거가
  브랜딩을 **교체**합니다 — `client_id` 가 createOnly 라 in-place update 가
  AWS 에서 거부되기 때문입니다(awscc/CloudControl 제약). `./deploy apply` 는
  이 교체를 허용 목록으로 통과시킵니다.

- 왼쪽 문구는 이미지라 **언어 전환을 따르지 않습니다**(영어 고정). `branding.name`
  과도 연동되지 않습니다 — 바꾸려면 생성기의 `HEADING`/`SUBTITLE`/`FOOTER`.
- 폼 문구(레이블·버튼)는 Cognito 가 그립니다 — admin-ui 가 authorize 요청에
  `lang=<ko|en>`(admin-ui 의 KO/EN 선택)을 실어 보냅니다.
- OS 의 "동작 줄이기"가 켜진 사용자에게는 움직이는 점을 숨기고 정지 그래프만 보입니다.

## 켜기 — 표준 경로

```bash
# deployment/gateway.yaml
branding:
  managed_login: true

./deploy apply --plan   # module.cognito: tier/domain update + branding create 확인
./deploy apply
```

- 처음 한 번은 terraform 이 `hashicorp/awscc` provider 를 내려받고
  `.terraform.lock.hcl` 에 기록합니다 — 바뀐 lock 파일은 커밋하세요.
- 같은 앱 클라이언트에 콘솔에서 만든 스타일이 이미 있으면 생성이
  `already exists` 로 실패합니다 — 콘솔에서 그 스타일을 지우거나
  `terraform import` 로 가져온 뒤 다시 apply.

## 끄기 — env terraform 수동 절차

브랜딩 스타일 삭제는 파괴 변경이라 `./deploy apply` 가 거부합니다.

```bash
# 1) gateway.yaml 에서 branding.managed_login 을 지우거나 false
# 2) env 디렉토리에서 (eks: deployment/terraform/environments/<env>, ecs: gateway-ecs)
terraform apply -target=module.cognito -var cognito_managed_login=false
```

도메인은 classic(1)으로 돌아가고 기존 `ui_customization` 이 다시 쓰입니다.
요금제는 자동으로 내리지 않습니다(콘솔에서 직접).
