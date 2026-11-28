# 관리자 UI 및 기능

**한국어** · [English](admin-guide.en.html)

관리자로 로그인하면 **Dashboard**가 열립니다. 왼쪽 메뉴에서 목적에 맞는 화면을 선택합니다. 화면 이름은 dev 배포 버전 `4fe6d927`의 영문 메뉴 기준입니다.

[관리자 상세 가이드](admin-guide.ko.md)에서 단계별 절차를, [관리자 화면 모음](admin-screens.md)에서 메뉴별 기본 화면과 상세 화면, 탭, 필터, 변경 양식을 여는 경로를 볼 수 있습니다.

| 목적 | 메뉴 | 확인하거나 관리할 내용 |
|---|---|---|
| 전체 상태 파악 | **Dashboard** | 전체 요청, 사용량, 비용과 예산 요약 |
| 조직과 권한 관리 | **Users/Teams** | 조직·팀·사용자 목록과 상세 정책 |
| 모델 카탈로그 관리 | **Model Management** | 모델 상태, 단가, 등록과 편집. [모델 관리 절차](models.md) |
| 클라이언트 정책 관리 | **App Policy** | 앱별 접근 정책과 적용 범위 |
| 사용 한도 관리 | **Budget Management** | 팀·사용자 예산, 소진 현황, 웹 검색 한도 |
| 키 상태 확인 | **API Keys** | 활성·만료·폐기 키 목록 |
| 에이전트 관리 | **Agents** | 에이전트별 키 상태, 비용·토큰 추이 |
| 사용량과 비용 분석 | **Analytics** | 기간별 요청·토큰·비용 추이 |
| 장애 확인 | **Monitoring** | 요청 로그와 오류 상태. 본문 로그는 기능이 활성화된 환경에서 확인합니다. |
| 자연어로 분석 | **BI Insight** | 기능이 활성화된 환경에서 사용량과 비용을 질의 |
| 변경 추적 | **Audit Log** | 관리자 작업의 시간, 대상, 결과 |
| 애플리케이션 연결 | **Integration** | Claude, OpenAI, LangChain 연동 예제 |
| CLI 설치와 사용 | **CLI** | 운영체제별 gateway-cli 설치, 로그인, 연결 명령 |

팀 리더는 **Budget Management**, **Analytics**, **My Usage** 등을 사용할 수 있습니다. 개발자는 **My Usage**, **Integration**, **CLI**에서 자기 사용량과 연결 방법을 확인합니다. 사용을 마치면 상단의 로그아웃 버튼을 선택합니다.

[가이드 목차](README.md) · [로그인](login.md) · [운영 가이드](../op-guide/README.ko.md)
