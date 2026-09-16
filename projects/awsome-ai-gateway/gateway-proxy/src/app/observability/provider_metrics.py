# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""프로바이더 호출 결과 지표 — 라벨 구성과 기록 헬퍼.

왜 이 모듈이 따로 있나
----------------------
``gateway_provider_error_total`` 은 여러 곳에서 올라간다(``/v1/messages`` 경로,
``/model/*`` 패스스루, 스트리밍 중간 실패). **모델별 에러율은 모든 호출 지점이
같은 라벨 키를 만들 때만 의미가 있다** — 한 곳이 ``model`` 을 빼면 그 지점의
실패는 다른 시계열에 섞여 어느 모델이 죽는지 알 수 없게 된다. 그래서 라벨 구성을
각 호출부에 복제하지 않고 여기 둔다.

⚠️ 분모가 없으면 에러율을 낼 수 없다
------------------------------------
이 모듈이 추가되기 전 ``gateway_provider_error_total`` 은 **선언만 되어 있고 어디서도
증가하지 않았다**(실측: 지표 24개 선언 중 6개가 그 상태). 그리고 분모에 해당하는
``gateway_provider_request_total`` 은 아예 없었다. 카운터가 한 번도 증가하지 않으면
Prometheus 에는 시계열 자체가 없고, ``rate(gateway_provider_error_total[5m]) > 0``
같은 알람은 **영구히 거짓**이며 대시보드는 "No data" 를 띄운다 — 사람은 그걸
"에러 없음" 으로 읽는다. 지표가 없는 것보다 나쁘다.

카디널리티 계약
---------------
라벨은 도메인이 유한한 값으로만 제한한다: provider(5), model alias(수십),
status(수십), error_code(수십), client(3), is_stream(2).
**user_id 나 team_id 는 절대 넣지 않는다** — 이 Prometheus 는 prometheus-adapter 를
통해 HPA 도 먹이므로, 카디널리티 폭발은 관측성과 함께 오토스케일링을 죽인다.

모든 헬퍼는 fail-safe 다. 지표 백엔드가 없거나 예외를 던지더라도 추론 경로를 절대
막지 않는다 — 관측을 위해 서비스를 죽이는 것은 거래가 성립하지 않는다.
"""

from __future__ import annotations

import structlog

logger = structlog.get_logger(__name__)

# 스트리밍 중간 실패용 의사(pseudo) status. 실패 시점에 HTTP 200 이 이미 나가 있어
# 보고할 실제 상태코드가 없고, ``gateway_error_total`` 은 이 실패를 아예 보지 못한다
# (미들웨어가 보는 응답 상태는 200 이다).
STATUS_STREAM_ERROR = "stream_error"
STATUS_STREAM_TIMEOUT = "stream_timeout"


def _provider_value(provider) -> str:
    """ProviderType enum 이든 DB 의 원시 문자열이든 라벨 문자열로."""
    return getattr(provider, "value", None) or str(provider or "unknown")


def build_provider_labels(
    *,
    model_config=None,
    provider=None,
    model: str | None = None,
    provider_model_id: str | None = None,
    client: str | None = None,
    is_stream: bool | None = None,
) -> dict:
    """request/error 카운터가 공유하는 라벨 집합.

    ``model_config``(ModelConfigSchema)가 일반적인 진입점이고, 명시 인자는 느슨한 값만
    가진 호출부(스트리밍 훅 등)를 위한 것이다.
    """
    if model_config is not None:
        provider = provider if provider is not None else getattr(model_config, "provider", None)
        model = (
            model
            or getattr(model_config, "alias", None)
            or getattr(model_config, "provider_model_id", None)
        )
        provider_model_id = provider_model_id or getattr(model_config, "provider_model_id", None)

    labels = {
        "provider": _provider_value(provider),
        "model": model or "unknown",
        "provider_model_id": provider_model_id or "unknown",
        "client": client or "other",
    }
    if is_stream is not None:
        labels["is_stream"] = "true" if is_stream else "false"
    return labels


def record_provider_request(metrics, labels: dict) -> None:
    """분모를 올린다. 절대 raise 하지 않는다."""
    if metrics is None:
        return
    try:
        metrics.provider_request_total.add(1, labels)
    except Exception:
        logger.warning("provider_request_metric_failed")


def record_provider_error(
    metrics, labels: dict, *, status: str | int, error_code: str | None = None
) -> None:
    """분자를 올린다. 절대 raise 하지 않는다.

    ``error_code`` 는 프로바이더 자신의 실패 이름(Bedrock 의
    ``InternalServerException`` vs ``ThrottlingException`` 등)이다. 이게 있어야
    "AWS 쪽 장애" 와 "우리 쿼터 소진" 을 CloudWatch 를 손으로 대조하지 않고 구분할 수
    있다 — status 만으로는 둘 다 500/429 로 뭉개진다.
    """
    if metrics is None:
        return
    try:
        metrics.provider_error_total.add(
            1,
            {**labels, "status": str(status), "error_code": error_code or "unknown"},
        )
    except Exception:
        logger.warning("provider_error_metric_failed")


def record_cache_hit(metrics, *, kind: str) -> None:
    """캐시 히트 1건. ``kind`` 는 유한 도메인이어야 한다(auth_context / model / model_list).

    히트율은 Redis 부하와 DB 폴백 빈도를 함께 설명하는 유일한 신호다 — 히트가 갑자기
    떨어지면 그 다음에 오는 것은 DB 커넥션 고갈이다.
    """
    if metrics is None:
        return
    try:
        metrics.cache_hits_total.add(1, {"kind": kind})
    except Exception:
        logger.warning("cache_hit_metric_failed")


def record_rate_limit_hit(metrics, *, scope: str, limit_type: str) -> None:
    """429 를 낸 순간 1건. ``scope``/``limit_type`` 모두 유한 도메인이다.

    ⚠️ 이 카운터가 없는 동안 "사용자가 스로틀되고 있는가" 를 알 수 있는 방법이
       액세스 로그 grep 뿐이었다. ``gateway_error_total{status="429"}`` 로는 어느
       스코프(USER/TEAM/GLOBAL)의 어떤 한도(rpm/tpm/cpm/cph)인지 구분되지 않아,
       "한 사용자가 자기 한도를 치는 중" 과 "전역 한도가 모두를 막는 중" 이
       같은 시계열로 보인다 — 대응이 정반대인 두 상황이다.
    """
    if metrics is None:
        return
    try:
        metrics.rate_limit_hits_total.add(1, {"scope": scope, "limit_type": limit_type})
    except Exception:
        logger.warning("rate_limit_hit_metric_failed")
