# 서버측 Web Search 는 어떻게 동작하나 (초보자용)

> 이 문서는 [install-guide.md §5](../install-guide.md#5-서버측-web-search-us-east-1) 를 **개념부터** 이해하려는 사람을 위한 것이다. 설치 명령은 §5 에 있고, 여기서는 *"직원이 질문하면 무슨 일이 일어나나"* 를 그림으로 설명한다.

---

## 한 문장

**직원의 앱(Claude Code · Cowork)은 아무 설정도 안 한다.** 게이트웨이가 대신 웹 검색을 붙여주고, 검색이 필요한지는 **모델이 스스로 판단**한다.

---

## 가장 헷갈리는 것 먼저 — "어떤 단어가 검색을 켜나?"

**아무 단어도 아니다.** "지금"·"최신"·"now" 같은 키워드로 켜지는 게 **아니다.**

```
❌ 흔한 오해:   프롬프트에 "지금" 이 있으면 → 검색
✅ 실제:        게이트웨이는 web_search 툴을 "항상 테이블 위에 올려두고",
               쓸지 말지는 모델이 질문을 보고 스스로 정한다 (1P Claude 와 동일)
```

게이트웨이는 **프롬프트 내용을 읽지 않는다.** 그냥 툴을 제공만 하고, 판단은 모델 몫이다. 그래서 "지금" 이 없어도 검색하고("삼성전자 2분기 실적"), "지금" 이 있어도 안 할 수 있다("지금 몇 시야" → 모델이 자기 지식으로).

모델이 무엇을 보고 정하나 — 게이트웨이가 툴에 붙여주는 **설명문**:

> *"Search the public web for **current, factual, or recent** information. Use this when the answer may depend on events, data, docs, or facts that are **recent or external**."*

이게 판단 근거다. "이럴 때 써라" 라고 가이드만 주고 결정은 모델에게 맡긴다. (설명문 끝에는 "한 턴에 몇 건, 몇 라운드까지" 라는 검색 예산 한 문장이 더 붙는다 — 아래 "멈추는 조건".)

---

## 전체 흐름 (직원이 질문 하나 던졌을 때)

```text
  ┌─ PC  (Claude Code / Cowork) ───────────┐
  │ prompt: "stock price yesterday?"       │
  └────────────────────┬───────────────────┘
                       │ 직원은 web search 설정을 한 적이 없다
                       │
                       ▼
  ┌─ gateway-proxy  (us-west-2) ───────────┐
  │ (1) app web-search toggle ON? (5-3)    │
  └────────────────────┬───────────────────┘
                       │ OFF -> 그대로 통과 (아래 전부 skip)
                       │ ON  -> web_search 툴을 요청에 붙임
                       │        (프롬프트는 읽지 않는다)
                       ▼
  ┌─ (2) model  (Bedrock, us.anthropic.*) ─┐
  │ prompt + web_search tool available     │
  │ (3) model decides by itself            │
  └────────────────────┬───────────────────┘
                       │
                       ├─────────────────────────────────────────────────┐
                       ▼                                                 ▼
  ┌─ A: can answer from own knowledge ─────┐        ┌─ B: needs fresh/external info ─────────┐
  │ no search                              │        │ calls web_search tool                  │◀┐
  └────────────────────┬───────────────────┘        └────────────────────┬───────────────────┘ │
                       │                                                 │                     │
                       │                                                 ▼                     │
                       │              loop within   ┌─ (4) gateway-proxy intercepts the call ┐ │
                       │              the budget    │ SigV4 / IRSA (AWS_IAM)                 │ │
                       │              (results->B)  └────────────────────┬───────────────────┘ │
                       │                                                 │                     │
                       │                                                 ▼                     │
                       │                            ┌─ Bedrock AgentCore Gateway (us-east-1) ┐ │
                       │                            │ MCP endpoint                           │ │
                       │                            └────────────────────┬───────────────────┘ │
                       │                                                 │                     │
                       │                                                 ▼                     │
                       │                            ┌─ managed WebSearch connector ──────────┐ │
                       │                            │ Amazon web index (not Tavily/Google)   │ │
                       │                            │ results -> back to the model           ├─┘
                       │                            └────────────────────┬───────────────────┘
                       │                                                 │
                       ├─────────────────────────────────────────────────┘
                       ▼
  ┌─ model writes the final answer ────────┐
  │ (searched or not)                      │
  └────────────────────┬───────────────────┘
                       │
                       ▼
  ┌─ (5) gateway-proxy builds the response ┐
  │ A (no search): answer passes as is     │
  │ B (searched) : turns joined into ONE   │
  │   + trace lines + search record        │
  └────────────────────┬───────────────────┘
                       │
                       ▼
  ┌─ PC screen ────────────────────────────┐
  │ one answer (+ trace lines if searched) │
  └────────────────────────────────────────┘

  (1) 이 앱(claude-code, cowork)의 웹서치 토글 · (2)(3) 모델이 스스로 검색 여부를 판단
  (4) gateway-proxy 가 툴 호출을 가로채 AgentCore Gateway(us-east-1) 의 관리형 WebSearch 커넥터
      (Amazon 자체 인덱스)로 보내고, 결과를 모델에 되먹인다(검색 예산 안에서 반복)
  (5) A(검색 안 함): 모델의 답을 그대로 통과시킨다 — 덧붙이는 것 없음
      B(검색함): 여러 번의 모델 호출을 하나의 답으로 이어 붙이고, 검색 흔적 줄과
      검색 기록(다음 질문에서 모델이 다시 보는 것)을 덧붙여 앱에 보낸다
  PC screen: 하나의 답. 검색했을 때만 검색마다 흔적 한 줄이 함께 보인다
```

> 이 그림은 생성기로 만든 것이다 — 손으로 고치지 말 것(폭 계산이 깨진다). 생성기는 운영자 내부 repo 에 있다.

핵심 3가지:

- **①의 토글**만 운영자가 켜면 된다 (§5-3). 직원은 아무것도 안 한다.
- **③의 판단**은 모델이 한다. 키워드 규칙이 아니다.
- **④ 이후**는 전부 AWS 안 — gateway-proxy → **AgentCore Gateway** → 관리형 커넥터 → Amazon 웹 인덱스. 질의가 제3자(구글·Tavily)로 안 나간다.

---

## 화면에 보이는 것 — 🔎 줄

검색이 일어나면 답변 안에 검색마다 한 줄이 보인다. 게이트웨이가 넣는 줄이다.

```
🔎 [gateway web_search] "TSMC Q2 2026 revenue results" — 5 results (investor.tsmc.com, fool.com)
```

- `— N results (…)` : 검색이 실행됐고 결과 N건, 괄호 안은 대표 출처
- `— failed (…)` : 검색을 시도했지만 실패 — 모델은 자기 지식으로 답한다
- `— skipped …` / `— not run …` : 상한에 걸려 실행하지 않은 검색 (아래 "멈추는 조건")

`[gateway web_search]` 표시가 있으면 **게이트웨이의 서버측 검색**이다. 앱에 자체 검색 도구가 따로 있으면 그쪽 흔적과 구분하는 용도다. 이 줄이 하나도 없으면 모델이 검색 없이 답한 것이다.

---

## 왜 게이트웨이가 대신 하나 (직원이 설정 안 하는 이유)

Bedrock 은 Anthropic 의 네이티브 서버측 web search 를 **지원하지 않는다**. 그래서 게이트웨이가 그걸 **흉내낸다** — 툴을 주입하고, 모델의 호출을 가로채 AWS 검색을 부르고, 여러 번 오간 대화를 **하나의 답변으로 봉합**한다. 직원 눈에는 그냥 Claude 가 알아서 검색해 답한 것처럼 보인다.

---

## 다음 질문에서도 기억하게 하는 법 — 검색 기록

모델은 기억이 없다. 매 요청마다 **앱이 보내 주는 대화 기록**만 본다. 그런데 검색 결과는 게이트웨이 안에서만 오갔으니 앱의 기록에는 없다. 그대로 두면 다음 질문에서 모델은 자기가 검색했다는 사실도, 무엇을 찾았는지도 모른다 — "방금 출처가 뭐였지?" 에 같은 검색을 다시 하거나, "저는 검색한 적이 없습니다" 라고 답한다.

그래서 게이트웨이는 응답에 **검색 기록**을 함께 넣는다. Anthropic API 가 원래 쓰는 검색 기록 형식 그대로라서, 앱은 이것을 보관했다가 다음 요청에 되돌려 준다.

```
request 1:  app <- gateway : answer + search record (query, urls, excerpts)
            app keeps the record in its chat history
request 2:  app -> gateway : history (with the record) + new question
            gateway        : turns the record back into "you searched this, this came back"
            model          : can cite its sources, no need to search again
```

- **기록에 담기는 것**: 검색어, 결과의 제목·URL·날짜, 그리고 모델이 그 턴에 본 것과 같은 길이의 발췌.
- **이 방식을 받는 앱**: 기록을 그대로 되돌려 주는 것이 확인된 앱만 — Cowork, Claude Code. 설정 `WEB_SEARCH_TRACE_NATIVE_CLIENTS` 로 정한다. 그 밖의 앱에는 🔎 줄만 남는다(모델은 "검색했다" 는 사실만 알고 내용은 모른다).
- **대가**: 검색 1건마다 대화에 약 3~4천 토큰이 남아 이후 요청마다 다시 읽힌다(아래 "비용 감각").

---

## 앱의 자체 도구와 함께 쓸 때

Cowork·Claude Code 는 파일 쓰기, 할 일 목록 같은 **자체 도구**를 갖고 있고, 모델은 검색과 이 도구들을 섞어 쓴다. 두 상황에서 검색이 도구의 발목을 잡지 않게 한다. 둘 다 기본으로 켜져 있고, 스위치로 끌 수 있다.

- **한 턴에 도구와 검색을 같이 부를 때** (`WEB_SEARCH_MIXED_TURN_RUN`)
  - 끔: 검색은 실행되지 않고, 모델이 다음 요청에서 같은 검색을 다시 낸다(시간·토큰 낭비).
  - 켬: 검색도 실행해 그 기록을 같은 응답에 싣는다. 검색 기록을 되돌려 주는 앱에서만 동작한다.
- **검색 예산을 다 쓴 뒤** (`WEB_SEARCH_FINAL_TURN_SOFT`)
  - 끔: 다음 턴에 도구를 전부 금지하고 "지금 답하라" 고 한다 — "검색해서 파일로 저장해줘" 가 저장 없이 끝난다.
  - 켬: **검색만** 거절하고 앱의 도구는 그대로 쓸 수 있다. 그래도 검색을 고집하면 그때 전부 금지한다.

---

## 멈추는 조건 (무한 검색 방지)

모델이 검색→읽기→또 검색을 반복할 수 있어서 상한이 있다. 한 **요청**의 검색 예산 = **턴당 건수 × 검색 턴 수**.

- **턴당 검색 수** — 기본 **3**. 넘는 검색은 실행하지 않고 "이번 턴 한도" 라고 알려 준다.
- **검색 턴 수** — 기본 **2**. 다 쓰면 위 "예산을 다 쓴 뒤" 동작으로 넘어가 답을 쓰게 한다.
- **전체 마감** — **90초**. 넘으면 더 검색하지 않고 답한다.
- **결과 크기** — 검색 1회당 결과 수(기본 5)와 글자 수(기본 12,000)에도 상한이 있다.

값을 바꿀 때는 `update-scripts/17-set-websearch-caps.sh` 를 쓴다(재빌드 없음 — 아래 "설정 스위치"). 예산은 **요청 단위**라서, 빈칸이 남으면 "나머지도 찾아줘" 한마디로 새 예산이 생긴다. 모델은 못 찾은 값을 지어내지 않고 "미확인 — 검색 예산 소진" 처럼 표기한다.

---

## 비용 감각

> 2026-09 실측 · Opus 5 · US 리전 단가 · 검색 커넥터 요금은 별도. 조건이 다르면 달라진다.

- **검색 1건 ≈ $0.05~0.07.** 검색 자체보다, 결과가 대화에 남아 이후 호출마다 다시 읽히는 토큰과 검색 턴마다 한 번 더 도는 모델 호출이 비용이다.
- **검색 1건 = 대화 +3~4천 토큰.** 검색이 많은 대화는 그만큼 빨리 컨텍스트가 찬다.
- **검색이 낀 대화에서 검색 몫은 총비용의 약 20~35%.** 더 큰 항목은 앱의 큰 시스템 프롬프트(도구 목록 포함 5~8만 토큰)를 캐시에 다시 쓰는 경우다 — 5분 넘게 쉬었다 이어 가거나, 앱의 도구 목록이 바뀔 때.
- **예산을 늘리면 모델은 그만큼 더 검색한다.** 검색 턴 2→3 실험: 좁은 질문은 검색 +50%·비용 +47% 에 답은 같았고, 8개 회사를 비교하는 넓은 질문에서만 빈칸이 줄었다.

---

## 설정 스위치 한눈에

모두 gateway-proxy 의 환경 변수이고, `17-set-websearch-caps.sh` 로 바꾼 뒤 `install-eks.sh` 롤아웃으로 적용한다 — **이미지 재빌드 없음**. 앱이나 모델이 업데이트돼 문제가 생기면 같은 방법으로 되돌린다.

| 환경 변수 | 뜻 | 기본값 |
|---|---|---|
| `WEB_SEARCH_MAX_SEARCHES_PER_TURN` | 턴당 검색 수 | **3** |
| `WEB_SEARCH_MAX_ITERATIONS` | 검색 턴 수 | **2** |
| `WEB_SEARCH_MAX_RESULTS_DEFAULT` | 검색 1회의 결과 수 상한 | **5** |
| `WEB_SEARCH_MAX_RESULT_CHARS` | 검색 1회의 글자 수 상한 | **12000** |
| `WEB_SEARCH_TRACE_MODE` | `native`(검색 기록 + 🔎 줄) \| `text`(🔎 줄만) | `native` |
| `WEB_SEARCH_TRACE_NATIVE_CLIENTS` | 검색 기록을 받을 앱 목록 | `cowork,claude-code` |
| `WEB_SEARCH_MIXED_TURN_RUN` | 한 턴에 도구와 검색을 같이 부르면 검색도 실행 (0\|1) | 1 |
| `WEB_SEARCH_FINAL_TURN_SOFT` | 예산 소진 뒤에도 앱의 도구는 허용 (0\|1) | 1 |

- **기본값은 하나다** — 환경 변수를 주지 않았을 때 gateway-proxy 가 쓰는 값과, `config.env` 에서 키를 비워 두면 17 스크립트가 넣는 값이 같다(gateway-proxy 1.0.80 부터). 아무것도 설정하지 않은 설치가 위 값으로 동작한다.
- **그 전 이미지**의 기본값은 4 · 5 · 10 · 60000 · `text` · `cowork` · 0 · 0 이었다 — 옛 이미지를 쓰면 17 스크립트로 위 값을 넣는다.
- **끄거나 되돌릴 때**: `WEB_SEARCH_TRACE_MODE="text"`(검색 기록 끔) · `WEB_SEARCH_MIXED_TURN_RUN="0"` · `WEB_SEARCH_FINAL_TURN_SOFT="0"`.

---

## 검증 — "정말 검색했나?"

- **화면** — 답변의 🔎 줄.
- **CloudWatch** — AgentCore 게이트웨이의 `tools/call` 호출 수 (DB·비번 불필요). 명령은 [§5-4](../install-guide.md#5-4-검증-6-클라이언트-설치-후).
- **DB** — `usage.usage_logs.web_search_count` (성공한 검색만 카운트).
- **gateway-proxy 로그** — `kubectl logs` 에 검색마다 이벤트가 남는다.
  - `web_search.evidence_built` — 검색 1건 실행(요청 건수·남긴 건수·크기)
  - `web_search.native_blocks_emitted` · `web_search.inbound_native_rewritten` — 검색 기록을 내보냄 · 되돌아온 것을 복원함
  - `web_search.mixed_turn_ran` · `web_search.final_turn` · `web_search.search_refused_budget` — 도구와 같은 턴의 검색 · 예산 소진 턴 · 거절된 검색
  - `web_search.failed` — 검색 실패(warning)
- **회귀 테스트** — `update-scripts/18-websearch-client-sim.py` 가 Cowork·Claude Code 를 흉내 내 시나리오를 돌리고 합격/불합격을 판정한다. 앱·모델이 업데이트된 뒤, 검색 코드를 바꾼 뒤에 돌린다(실제 모델·검색 비용 발생).

> 예전 버전은 성공한 검색이 로그를 남기지 않았다. `web_search.evidence_built` 가 안 보이면 이미지가 옛 버전인지부터 확인한다.

---

## 한눈에 (요약 카드)

- **직원이 설정하나?** — 아니다. 운영자가 토글만 켠다(§5-3).
- **어떤 단어가 검색을 켜나?** — 없다. 모델이 스스로 판단.
- **검색은 어디서?** — AWS 관리형(us-east-1). 구글·Tavily 아님.
- **질의가 밖으로 나가나?** — 아니다. AWS 안에서 처리(zero egress).
- **검색했는지 화면에서 아나?** — 안다. 검색마다 🔎 한 줄.
- **다음 질문에서 출처를 기억하나?** — Cowork·Claude Code 는 기억한다(검색 기록을 되돌려 줌).
- **몇 번까지 검색?** — 요청당 턴당 건수 × 턴 수. 기본 3 × 2, 마감 90초.
- **검색 1건 비용은?** — 약 $0.05~0.07 + 대화에 3~4천 토큰 (2026-09, Opus 5 실측).
- **확인은?** — 🔎 줄 · CloudWatch `tools/call` · `usage_logs` · gateway-proxy 로그.

---

## 더 깊이

- 구현: `gateway-proxy/src/app/services/web_search_loop.py` — 파일 맨 위 설명에 섹션 목차가 있다. 읽는 순서는 `run_web_search_loop` → `_anthropic_stream` → `_do_search` → `_native_search_blocks` → `_rewrite_inbound_native_blocks`.
- 검색 커넥터 호출: `gateway-proxy/src/app/services/agentcore_mcp_client.py`
- 테스트: `gateway-proxy/tests/regression/test_high_websearch_*.py`
