# web search — 게이트웨이가 서버에서 검색한다

Claude Desktop(Cowork · Chat)이나 Claude Code 에서 최신 정보를 물으면, PC 가 아니라 **게이트웨이가 서버에서 검색**해 답에 넣는다.
직원 PC 에는 아무것도 설치하지 않는다. 검색은 **Web Search on Amazon Bedrock AgentCore** 가 하고, 결과는 Anthropic 의 web search tool 과 같은 형식으로 앱에 돌아간다.

## 데모 영상

| 판 | 제목 | 링크 |
|---|---|---|
| 한국어 · Cowork | Claude Desktop Cowork + Web Search on AgentCore — AWSome AI Gateway | https://youtu.be/75OFe0q4YFs |
| English · Chat | Claude Desktop Chat + Web Search on AgentCore — AWSome AI Gateway | https://youtu.be/LC821HxkMYA |

## 알고 싶은 것 → 문서

| 알고 싶은 것 | 문서 |
|---|---|
| 어떻게 동작하나 (초보자용 그림) | [web-search-explained.md](web-search-explained.md) |
| 실제로 돌리면 — 질문 · 토큰 · 비용 | [web-search-demo.md](web-search-demo.md) |
| 설치할 때 켜기 | [install-guide.md](../install-guide.md) §5 |
| 검색 횟수·결과 크기 상한과 동작 설정 | [8-D](../ops/8-D-upstream-sync.md) ② 의 `17-set-websearch-caps.sh` |
| 코드·앱을 바꾼 뒤 회귀 테스트 | [update-scripts](../update-scripts/README.md) 의 `18-websearch-client-sim.py` |
| 무엇이 언제 바뀌었나 | [updates.md](../updates.md) US-10 |

## 한눈에

- **어느 앱에서** — Claude Code · Cowork · Chat. 앱별로 켜고 끄는 것은 관리 화면의 「앱별 웹서치 허용」.
- **검색 기록 유지** — 한 번 검색한 내용은 대화에 남아, 이어지는 질문은 다시 검색하지 않고 답한다.
- **비용 통제** — 한 질문에 검색은 턴당 최대 3건 · 2턴(요청당 최대 6건), 검색 결과는 건당 12,000자까지. 기본값이 코드에 들어 있어 따로 설정하지 않아도 된다.
- **앱 도구와 함께** — 검색한 내용을 그대로 파일로 저장하는 식으로, 검색과 앱의 도구가 한 대화에서 같이 동작한다.
- **어디서 검색하나** — Web Search on Amazon Bedrock AgentCore(이 배포는 us-east-1 을 호출한다).

## 관련 코드

**게이트웨이(검색이 실제로 도는 곳)** — `gateway-proxy/`
- [`services/web_search_loop.py`](../../../gateway-proxy/src/app/services/web_search_loop.py) — 검색 루프 본체: 모델이 검색을 요청하면 검색하고 결과를 넣어 다시 호출한다. 턴당 검색 수·반복 수 상한, 검색 기록 유지, 🔎 줄이 여기 있다.
- [`services/agentcore_mcp_client.py`](../../../gateway-proxy/src/app/services/agentcore_mcp_client.py) — Web Search on Amazon Bedrock AgentCore 를 호출하는 클라이언트.
- [`routers/messages.py`](../../../gateway-proxy/src/app/routers/messages.py) — 요청이 들어오는 곳. 그 앱에 web search 가 켜져 있으면 검색 루프로 보낸다.
- [`services/client_identifier.py`](../../../gateway-proxy/src/app/services/client_identifier.py) — 요청이 어느 앱(Cowork · Chat · Claude Code)에서 왔는지 가린다.
- [`config.py`](../../../gateway-proxy/src/app/config.py) — 상한과 동작 설정의 기본값(`web_search_*`).

**켜고 끄기(앱별)**
- [`admin-ui/…/WebSearchTogglePanel.tsx`](../../../admin-ui/src/components/models/WebSearchTogglePanel.tsx) — 관리 화면의 「앱별 웹서치 허용」.
- [`admin-api/…/routers/routing.py`](../../../admin-api/src/app/routers/routing.py) — 그 스위치를 저장하는 API.
- [`db/versions/0021_add_routing_web_search_enabled.py`](../../../db/versions/0021_add_routing_web_search_enabled.py) · [`0020_add_web_search_count.py`](../../../db/versions/0020_add_web_search_count.py) — 앱별 스위치와 검색 횟수 기록을 위한 DB 변경.

**설치**
- [`deployment/scripts/provision_agentcore_websearch.py`](../../../deployment/scripts/provision_agentcore_websearch.py) — AgentCore 쪽 검색 기능을 만든다.
- [`deployment/scripts/set-websearch-url.sh`](../../../deployment/scripts/set-websearch-url.sh) — 만든 주소를 게이트웨이 설정에 넣는다.
- [`deployment/terraform/modules/irsa/main.tf`](../../../deployment/terraform/modules/irsa/main.tf) — 게이트웨이가 AgentCore 를 호출할 수 있게 하는 권한.

**설정 · 확인 스크립트** — `docs/us-llm-gateway/update-scripts/`
- [`17-set-websearch-caps.sh`](../update-scripts/17-set-websearch-caps.sh) — 상한과 동작 설정을 바꾼다.
- [`18-websearch-client-sim.py`](../update-scripts/18-websearch-client-sim.py) — 앱을 흉내 내는 회귀 테스트.
- [`16-usage-recent.sh`](../update-scripts/16-usage-recent.sh) — 요청별 검색 횟수 · 토큰 · 비용을 본다.

## 참고 링크

- **Anthropic web search tool** — 앱이 기대하는 검색 도구의 형식(게이트웨이가 같은 형식으로 답한다)
  - 문서: [Web search tool — Claude Platform Docs](https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-search-tool)
  - 소개: [Introducing web search on the Anthropic API](https://claude.com/blog/web-search-api)
- **Web Search on Amazon Bedrock AgentCore** — 실제로 검색을 하는 AWS 관리형 기능
  - 문서: [Web Search Tool — Amazon Bedrock AgentCore Developer Guide](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-target-connector-web-search-tool.html)
  - 발표: [Announcing Web Search on Amazon Bedrock AgentCore](https://aws.amazon.com/about-aws/whats-new/2026/06/amazon-bedrock-agentcore-web-search/)
