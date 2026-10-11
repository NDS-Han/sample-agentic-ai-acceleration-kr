# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import json
import urllib.parse
import uuid

import structlog
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.observability.metrics import GatewayMetrics
from app.services.downgrade_loader import DowngradePolicyLoader, apply_chain
from app.services.router_service import RouterService, check_client_model_scope
from app.services.thinking_normalizer import normalize_thinking

logger = structlog.get_logger(__name__)

DOWNGRADE_PATH_PREFIXES = (
    "/v1/messages",
    "/v1/chat",
    "/v1/completions",
    "/v1/responses",  # Codex(Responses API) — body 에 model 필드가 있어 재작성 가능
    "/model/",
)

_OPENAI_PATH_PREFIXES = ("/v1/chat", "/v1/completions", "/v1/responses")

# Bedrock-native 경로 — `/model/{model_id}/{op}` (model_id 는 path param, 인코딩됨).
_BEDROCK_MODEL_OPS = (
    "invoke",
    "invoke-with-response-stream",
    "converse",
    "converse-stream",
)


def _path_eligible(path: str) -> bool:
    return any(path.startswith(p) for p in DOWNGRADE_PATH_PREFIXES)


def _is_openai_path(path: str) -> bool:
    return any(path.startswith(p) for p in _OPENAI_PATH_PREFIXES)


def _model_from_bedrock_path(path: str) -> tuple[str, str] | None:
    """`/model/{model_id}/{op}` 에서 (디코딩된 model_id, op) 를 꺼낸다.

    body 에 `model` 필드가 없는 Bedrock-native 요청용 — 다운그레이드 체인은
    alias 단위라 path segment 도 같은 해석(resolve_bedrock_model)을 거친다.
    """
    if not path.startswith("/model/"):
        return None
    for op in _BEDROCK_MODEL_OPS:
        suffix = "/" + op
        if path.endswith(suffix):
            mid = path[len("/model/") : -len(suffix)]
            if mid:
                return urllib.parse.unquote(mid), op
    return None


class DowngradeMiddleware:
    """Pure ASGI 미들웨어 — request body의 model alias를
    팀 다운그레이드 정책에 따라 rewrite한다.

    체인 위치: auth → budget → DowngradeMiddleware → rate_limit → router
    """

    def __init__(
        self,
        app: ASGIApp,
        loader: DowngradePolicyLoader | None = None,
        router: RouterService | None = None,
    ) -> None:
        self.app = app
        self.loader = loader or DowngradePolicyLoader()
        self.router = router or RouterService()
        self._metrics: GatewayMetrics | None = None

    def _get_metrics(self) -> GatewayMetrics | None:
        # GatewayMetrics는 OTel meter provider 초기화 후에만 생성. lazy.
        if self._metrics is None:
            try:
                self._metrics = GatewayMetrics()
            except Exception:
                self._metrics = None
        return self._metrics

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if not _path_eligible(path):
            await self.app(scope, receive, send)
            return

        logger.info("downgrade_mw_entered", path=path)

        state = scope.setdefault("state", {})

        # Cowork(client='cowork')는 routing profile 이 모델을 무조건 cowork-opus(Mantle)로
        # override 하므로(messages.py:_select_backend) 다운그레이드가 실효가 없다. 여기서
        # body model 을 바꿔봐야 _select_backend 가 무시 → 무의미한 DB 룩업 + body 재작성
        # + resolve_bedrock_model(cowork-opus 는 BEDROCK_MANTLE) lookup_failed 로그만 남는다.
        # 명시적으로 skip 한다. (다운그레이드는 사실상 Claude Code/Bedrock 경로 전용.)
        if state.get("client") == "cowork":
            logger.info("downgrade_mw_skip_cowork")
            await self.app(scope, receive, send)
            return

        auth_context = state.get("auth_context")
        budget_status = state.get("budget_status")
        team_id = getattr(auth_context, "team_id", None) if auth_context else None
        if not budget_status or not team_id:
            logger.info(
                "downgrade_mw_skip_precheck",
                has_budget_status=bool(budget_status),
                team_id=str(team_id) if team_id else None,
                has_auth_context=bool(auth_context),
            )
            await self.app(scope, receive, send)
            return

        body_bytes = await _drain_body(receive)
        try:
            payload = json.loads(body_bytes) if body_bytes else None
        except json.JSONDecodeError:
            logger.info("downgrade_mw_skip_invalid_json")
            await self.app(scope, _replay(body_bytes, receive), send)
            return

        original = payload.get("model") if isinstance(payload, dict) else None
        model_in_path = False
        bedrock_op = ""
        if not isinstance(original, str) or not original:
            # Bedrock-native `/model/{id}/{op}` — 모델이 body 가 아니라 path 에
            # 있다. 그대로 넘기면 이 경로의 요청은 다운그레이드가 영영 적용되지
            # 않는다(E2E S2-10). 재작성은 아래에서 scope["path"] 로 한다.
            from_path = _model_from_bedrock_path(path)
            if from_path is not None:
                original, bedrock_op = from_path
                model_in_path = True
        if not isinstance(original, str) or not original:
            logger.info("downgrade_mw_skip_no_model_field", payload_type=type(payload).__name__)
            await self.app(scope, _replay(body_bytes, receive), send)
            return

        # 정책 from_alias 는 standard alias (예: 'claude-sonnet-4-6') 로 저장되지만
        # 클라이언트가 보내는 model 값은 변형(예: 'global.anthropic.claude-sonnet-4-6',
        # 'claude-sonnet-4-6[1m]', cross-region prefix 등) 일 수 있다. router 와
        # 동일한 alias resolver 로 standard alias 로 정규화한 뒤 매칭한다.
        # 정규화 실패(미등록 alias)면 raw 문자열 그대로 매칭에 시도 — 기존 동작 유지.
        session_factory = state.get("_session_factory")
        # 디그레이드 반영 실효 redis — REDIS_DEGRADED 시 raw _redis 로 모델 해소
        # 하면 alias 캐시 조회마다 소켓 타임아웃 대기. 키 부재(테스트 앱) 시
        # `_redis` 폴백.
        redis = state.get("_redis_effective", state.get("_redis"))

        # 경로별 모델 해소자 — /v1/responses 는 Codex 평면(BEDROCK_MANTLE_OPENAI +
        # BEDROCK_RUNTIME_OPENAI)을 받아야 해서 resolve_openai_model(OPENMODEL +
        # RUNTIME_OPENAI)로는 Mantle alias 가 항상 미스였다(E2E S3-M5).
        def _resolver_for_path(p: str):
            if p.startswith("/v1/responses"):
                return self.router.resolve_codex_model
            if _is_openai_path(p):
                return self.router.resolve_openai_model
            return self.router.resolve_bedrock_model

        _resolve_for_path = _resolver_for_path(path)

        normalized = original
        _resolved_ok = True
        try:
            if session_factory is not None:
                async with session_factory() as db:
                    cfg = await _resolve_for_path(redis, db, original)
            else:
                cfg = await _resolve_for_path(redis, None, original)
            normalized = cfg.alias or original
        except Exception:
            # 미등록/INACTIVE/DB 미가용 — raw 매칭으로 fallback
            normalized = original
            _resolved_ok = False

        # ⚠️ /v1/responses 특수(E2E S3-M5-3): Codex 는 보통 미등록 이름
        #    (`gpt-5.5-codex` 등)을 보내고 openai_compat._resolve_model 이 조용히
        #    profile.default_model 로 서빙한다. 요청 문자열로는 정책이 절대 매칭되지
        #    않아 이 경로의 요청은 예산 강등이 한 번도 적용되지 않았다 — 해석에
        #    실패했을 때만 실제 서빙 모델(default_model)을 체인 평가 기준으로 삼는다.
        #    재작성된 payload["model"] 은 라우터에서 usable ref 로 해석돼 대상이
        #    실제로 서빙된다.
        if (
            path.startswith("/v1/responses")
            and not _resolved_ok
            and normalized == original
        ):
            _prof_loader = getattr(
                getattr(scope.get("app"), "state", None), "routing_profile_loader", None
            )
            _client_id = state.get("client")
            if _prof_loader is not None and _client_id:
                try:
                    if session_factory is not None:
                        async with session_factory() as db:
                            _prof = await _prof_loader.load(redis, db, _client_id)
                    else:
                        _prof = await _prof_loader.load(redis, None, _client_id)
                    _default = getattr(_prof, "default_model", None)
                    if isinstance(_default, str) and _default:
                        normalized = _default
                except Exception:
                    # 프로파일 조회 실패는 raw 매칭 그대로 — 기존 동작 유지.
                    pass

        try:
            if session_factory is not None:
                async with session_factory() as db:
                    rules = await self.loader.get_active_rules(
                        redis,
                        db,
                        _coerce_uuid(team_id),
                    )
            else:
                rules = await self.loader.get_active_rules(
                    redis,
                    None,
                    _coerce_uuid(team_id),
                )
        except Exception as exc:
            logger.warning(
                "downgrade_lookup_failed",
                reason=type(exc).__name__,
                exc_info=True,
            )
            metrics = self._get_metrics()
            if metrics:
                metrics.downgrade_lookup_failed_total.add(1, {"reason": type(exc).__name__})
            await self.app(scope, _replay(body_bytes, receive), send)
            return

        current_pct = int(getattr(budget_status, "threshold_pct", 0) or 0)
        effective, hops = apply_chain(normalized, rules, current_pct)
        if effective == normalized and normalized.endswith("[1m]"):
            # `[1m]` 변형은 같은 모델의 컨텍스트 파생 alias — 예산 정책 키는 보통
            # base alias(`claude-sonnet-4-6`)에 걸린다. 그대로 두면 1M 컨텍스트
            # 사용자만 예산 강등을 영구 우회한다(E2E S3-M5). 변형 자체 규칙이
            # 먼저 매칭됐으면 그쪽이 이긴다(위 호출이 이미 바꿨으므로 여긴 미스만).
            effective, hops = apply_chain(normalized[:-4], rules, current_pct)

        logger.info(
            "downgrade_mw_evaluated",
            team_id=str(team_id),
            original_model=original,
            normalized_model=normalized,
            effective_model=effective,
            hops=hops,
            current_pct=current_pct,
            rules_count=len(rules),
            rules=[
                {"from": r.from_alias, "to": r.to_alias, "th": r.threshold_pct}
                for r in rules
            ],
        )

        if effective == normalized:
            await self.app(scope, _replay(body_bytes, receive), send)
            return

        # 강등 대상이 실제로 서빙 가능한지 검증한다(E2E S3-M6). 대상이 미등록/
        # INACTIVE/다른 provider/이 앱 미허용이면, 원래 모델이면 성공했을 요청이
        # 재작성 뒤 404/400 이 된다 — 강등 자체가 가용성을 해치는 최악의 모드다.
        # 해석 캐시(최대 300s)라 추가 비용은 사실상 없다.
        try:
            if session_factory is not None:
                async with session_factory() as db:
                    _target_cfg = await _resolve_for_path(redis, db, effective)
            else:
                _target_cfg = await _resolve_for_path(redis, None, effective)
            check_client_model_scope(_target_cfg, state.get("client"))
        except Exception:
            logger.info(
                "downgrade_mw_skip_target_unusable",
                target=effective,
                normalized=normalized,
            )
            await self.app(scope, _replay(body_bytes, receive), send)
            return

        # 강등 대상이 이 사용자의 allowed_models 밖이면 재작성하지 않는다 —
        # 써 본 적도 없는 모델로 바뀐 뒤 라우터의 모델 ACL 에서 400/403 이
        # 나는 것보다, 자격이 있는 원래 모델로 진행하는 편이 낫다(E2E S2-10).
        allowed_models = getattr(auth_context, "allowed_models", None)
        if allowed_models is not None and effective not in allowed_models:
            logger.info(
                "downgrade_mw_skip_target_not_allowed",
                target=effective,
                normalized=normalized,
            )
            await self.app(scope, _replay(body_bytes, receive), send)
            return

        if not model_in_path:
            payload["model"] = effective

        if isinstance(payload, dict):
            # 강등 대상 모델이 받는 형태로 `thinking` 을 맞춘다.
            #
            # ⚠️ 예전에는 `effective.startswith("claude-haiku-4-5")` 일 때 **지웠다.** 두 가지가
            #    틀렸다: (1) 운영자가 만든 alias(예: team-haiku-cheap)는 그 접두사로 시작하지
            #    않아 판정을 빠져나가고 강등 뒤 400 이 됐다. (2) 지우는 것은 haiku 가 실제로
            #    받는 `{"type":"enabled"}` 까지 없애서, 사용자는 200 을 받으면서 extended
            #    thinking 만 조용히 잃는다.
            #
            # ⚠️ 이 계층은 모델 **해석 전**이라 provider_model_id 를 모른다 — alias 로 계열을
            #    추정한다(_family_from_alias). 모르는 alias 는 그대로 통과시킨다(fail-open).
            _before = json.dumps(payload.get("thinking"), sort_keys=True)
            payload = normalize_thinking(
                payload, None, alias=effective, request_id=state.get("request_id", "")
            )
            if json.dumps(payload.get("thinking"), sort_keys=True) != _before:
                logger.info("downgrade_thinking_normalized", target=effective, original=original)

            # ensure_ascii=False: 한국어/일본어 content 보존; separators: 컴팩트
            new_body = json.dumps(
                payload, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
        else:
            # path-model 요청은 body 없이 올 수 있다(잘못된 converse 호출 등) —
            # body 는 그대로 재생하고 path 만 재작성한다.
            new_body = body_bytes
        state["downgraded_from"] = original

        # Content-Length header 갱신 — body byte 길이가 바뀌었음
        new_headers = [
            (k, v) for (k, v) in scope.get("headers", []) if k.lower() != b"content-length"
        ]
        new_headers.append((b"content-length", str(len(new_body)).encode()))
        scope = dict(scope)
        scope["headers"] = new_headers

        if model_in_path:
            # 모델은 path segment — 같은 segment 를 effective alias 로 교체한다.
            # 라우터가 segment 를 resolve_bedrock_model 로 해석하므로 alias 도 OK.
            new_path = f"/model/{urllib.parse.quote(effective, safe='')}/{bedrock_op}"
            scope["path"] = new_path
            scope["raw_path"] = new_path.encode("utf-8")

        metrics = self._get_metrics()
        if metrics:
            metrics.downgrade_applied_total.add(1, {"from_alias": original, "to_alias": effective})
            metrics.downgrade_chain_depth.record(hops)

        await self.app(scope, _replay(new_body, receive), send)


async def _drain_body(receive: Receive) -> bytes:
    chunks: list[bytes] = []
    while True:
        message = await receive()
        if message["type"] == "http.request":
            chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                break
        else:
            # disconnect 등은 그대로 흡수
            break
    return b"".join(chunks)


def _replay(body: bytes, original_receive: Receive) -> Receive:
    """body 1회 재생 후에는 원본 receive 로 위임.

    이전 구현은 두 번째 호출부터 `http.disconnect` 를 반환했는데,
    StreamingResponse 의 listen_for_disconnect 가 이를 즉시 client 끊김
    으로 오해해 stream task 를 cancel 시키는 버그가 있었다. 원본 receive
    로 위임하면 진짜 disconnect 이벤트를 기다리게 되어 streaming 정상 동작.
    """
    delivered = False

    async def receive() -> Message:
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await original_receive()

    return receive


def _coerce_uuid(value):
    if isinstance(value, uuid.UUID):
        return value
    return uuid.UUID(str(value))
