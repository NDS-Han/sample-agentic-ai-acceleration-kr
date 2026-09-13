# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""클라이언트(앱) 식별자의 **단일 출처**.

이 집합이 세 곳에 복제돼 있었다:

    services/budget_service.py            _ALLOWED_CLIENTS = ("claude-code", "cowork", "codex")
    services/user_allowed_client_service.py  _VALID_CLIENTS = {...}
    services/routing_profile_service.py      _VALID_CLIENTS = {...}

복제 자체보다 **어긋났을 때의 실패 방식**이 문제다. 네 번째 앱을 붙일 때 한 곳을 빼먹으면:

  * 예산 쪽만 빠지면 → 그 앱에 per-app 예산을 만들 수 없다(400). 눈에 띈다.
  * 인가 쪽만 빠지면 → 그 앱을 허용목록에 넣을 수 없다(400). 눈에 띈다.
  * 그런데 **게이트웨이 쪽 집합**(gateway-proxy 의 PER_APP_BUDGET_CLIENTS)과 어긋나면
    조용하다 — 한도가 그냥 평가되지 않는다.

그래서 admin-api 안의 세 벌은 여기로 모으고, 서비스 경계를 넘는 정합성은 테스트로
못 박는다(tests/regression 의 client 집합 대조 테스트).

⚠️ 여기에 앱을 추가할 때 함께 봐야 하는 곳:
  * ``gateway-proxy`` ``PER_APP_BUDGET_CLIENTS`` (예산 평가 게이트)
  * ``cost-recorder-worker`` ``_PER_APP_CLIENTS`` (per-app 누적 행 — 이게 없으면 그 앱의
    카운터를 Redis 유실 후 복원할 수 없다)
  * ``db/init`` 의 ``budget_usages`` CHECK 제약(허용 client 값)
"""

from __future__ import annotations

#: 게이트웨이가 식별하는 앱들. ``"other"`` 는 **의도적으로 제외**한다 — 그것은
#: "분류되지 않음" 을 뜻하는 관측용 라벨이고, 정책을 붙일 대상이 아니다.
#: 정책 필드에 ``other`` 를 허용하면 미분류 트래픽 전체가 한 정책을 공유하게 된다.
VALID_CLIENTS: frozenset[str] = frozenset({"claude-code", "cowork", "codex"})

#: 순서가 필요한 곳(응답 정렬, 화면 표시)을 위한 안정된 순서.
CLIENT_ORDER: tuple[str, ...] = ("claude-code", "cowork", "codex")


def validate_clients(values: list[str] | None) -> list[str] | None:
    """client 목록을 검증한다. ``None`` 은 그대로 통과(의미가 다르다 — 아래 참조).

    ⚠️ ``None`` 과 ``[]`` 를 **같게 다루면 안 된다.** 이 값이 들어가는 정책 필드들의
       canonical 의미는:

         ``None``   미설정 = 제한 없음
         ``[]``     명시적으로 빈 허용목록 = 아무도 허용되지 않음
         non-empty  허용목록

       그래서 falsiness(``if not values``)로 판정하면 "제한 없음" 과 "전면 거부" 가
       한 값이 되고, 그 혼동은 접근제어를 fail-**open** 방향으로 무너뜨린다.
    """
    if values is None:
        return None
    bad = sorted(set(values) - VALID_CLIENTS)
    if bad:
        raise ValueError(f"invalid clients: {bad}; allowed: {sorted(VALID_CLIENTS)}")
    # 중복 제거 + 안정된 순서. 같은 정책을 두 번 저장했을 때 값이 달라 보이면 안 된다.
    return [c for c in CLIENT_ORDER if c in set(values)]
