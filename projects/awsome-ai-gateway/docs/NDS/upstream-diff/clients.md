# 클라이언트와 직원 배포

**한국어** · [English](clients.en.md) · [비교 목차](README.md)

| 구성 | 기준 이후 변경 | 배포 시 확인 |
|---|---|---|
| `installer/` | Windows Inno Setup, 선택 오프라인 휠, Linux deb, 사이트 설정과 관리 drop-in | 배포 URL, 설정 생성, 설치·제거 후 기존 설정 |
| `cowork-installer/` | Cowork/Claude Desktop 설치 경로 및 관리 프로필 | HTTPS 접속, 실제 프로필과 로그인 |
| `gateway-cli/` | 기존 CLI 로그인·만료 처리·쓰기·복원 동작 강화, 환경값/파일·STS 로그인 지원과 OTEL 배선 | CLI 프로필, 만료된 키 재로그인, 저장 권한 |
| `gateway-clients/` | 배포 환경과 앱별 기본값 연계 | 해당 앱에서 실제 전송하는 alias와 유효 정책 |
| `policy-helper/` | Claude Code 기본 정책에 VK로 받은 `/v1/policy` 조각을 병합하는 선택 도구, 설치·점검·원복 스크립트 | 헬퍼를 설치한 환경과 반환 정책을 확인 |

`./deploy client-values`에서 나온 환경 정보를 직원에게 줍니다. 사용자 자격과 OTEL bearer 헤더는 사용자 범위에만 저장합니다. 관리 설정은 일반적으로 여러 사용자가 읽을 수 있고, CLI 비밀 파일은 0600·원자적 쓰기를 사용합니다. 배포 검증은 관리자 설정 및 직원 로그인, 키 발급 후 호출, 앱별 오류와 사용량 반영, 제거 후 원복으로 구성합니다. 설치 파일이나 콘텐츠의 현재 운영 배포 여부는 배포 소유자와 확인합니다.

[직원 설정](../client-setup.md) · [설치 소스](../../../installer) · [Cowork 설치기](../../../cowork-installer) · [정책 헬퍼](../../../policy-helper)
