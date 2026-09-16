# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
#
# codex-box — OpenAI Codex CLI 를 LLM Gateway(GPT-5.6, Responses API)로 붙이는 격리 컨테이너.
# 호스트 맥 환경(~/.codex 등)을 건드리지 않는다. VK 는 런타임 env(GATEWAY_VK)로 주입,
# 게이트웨이 주소(GW_URL)도 env 로 받아 entrypoint 가 ~/.codex/config.toml 을 생성한다.
#
# GPT-5.6 은 Bedrock 의 두 plane(mantle / 표준 runtime)으로 서비스되고 plane 별 alias 가
# 따로 등록돼 있다. 어느 plane 을 쓸지는 이미지가 아니라 런타임 env 로 고른다:
#   GW_PLANE=mantle|runtime (기본 mantle)  GW_TIER=sol|terra|luna (기본 terra)
#   GW_MODEL=<alias> 로 전체 override, GW_CONTEXT_WINDOW 로 context window override.
# plane 별 alias·과금·폴백 동작은 entrypoint.sh 상단 주석 참고.
FROM public.ecr.aws/docker/library/node:20-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      git ca-certificates ripgrep less gettext-base \
    && rm -rf /var/lib/apt/lists/*

# Codex CLI (OpenAI)
RUN npm install -g @openai/codex && npm cache clean --force

# node:20-slim 은 uid 1000 'node' 유저를 기본 제공 — 그걸 비root 로 사용.
USER node
WORKDIR /work

# entrypoint: env(GW_URL, GATEWAY_VK, GW_PLANE/GW_TIER)로 codex config 를 생성한 뒤 codex 실행.
# config.toml: 게이트웨이를 OpenAI provider 로, wire_api=responses(GPT-5.x Responses API),
# env_key=GATEWAY_VK 로 Bearer 인증.
#
# ⚠️ model 문자열은 더 이상 "무시" 되지 않는다. 0025/0032 로 두 plane 의 GPT-5.6 alias 가
# 카탈로그에 실재하므로, 서버는 그 값이 ACTIVE alias 로 resolve 되면 그것을 쓴다(=plane 이
# 클라이언트 값으로 결정된다). resolve 안 되는 문자열일 때만 종전처럼 routing profile 의
# default_model 로 폴백한다. VK scope 검사는 그대로 적용된다 — entrypoint.sh 주석 참고.
COPY --chown=node:node entrypoint.sh /home/node/entrypoint.sh
ENTRYPOINT ["/home/node/entrypoint.sh"]
