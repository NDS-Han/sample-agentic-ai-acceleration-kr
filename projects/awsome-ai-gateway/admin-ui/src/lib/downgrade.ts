// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

export interface DowngradeRuleLike {
  from_model_alias: string;
  to_model_alias: string;
  threshold_pct: number;
}

export interface ChainResult {
  /** 최종 도달 alias (규칙 미발화 시 원래 alias 그대로). */
  effective: string;
  hops: number;
  /** 방문한 alias 순서(요청 모델 → … → 최종). */
  path: string[];
  /** 실제로 발화한 규칙의 입력 배열 인덱스(순서대로) — 다이어그램 하이라이트용. */
  ruleIdx: number[];
}

/**
 * gateway-proxy `app/services/downgrade_loader.py` 의 `apply_chain` 과 동일한
 * 판정을 내리는 TS 포트 — 사용률 시뮬레이터가 게이트웨이 런타임과 같은 결과를
 * 보여주기 위함이다. 느슨하게 재구현하면 시뮬레이터가 "자신감 있는 거짓말"이 된다.
 *
 * 게이트웨이 의미론(파이썬 원본과 1:1 대응):
 * - 현재 **팀 월간 예산 사용률** pct 가 규칙 threshold_pct 이상이면 from→to 로 hop.
 * - hop 후 to 를 새 alias 로 다시 매칭 — 같은 %로 이어진 체인은 한 요청에서
 *   다단 강등된다(opus→sonnet@80 + sonnet→haiku@80, pct=80 이면 opus 가 곧장
 *   haiku 로 2-hop).
 * - 같은 from_alias 의 규칙이 여럿이면 **입력 순서 첫 매칭만** 사용된다
 *   (DB 는 활성 from_alias 유니크 제약으로 이 상태를 막지만, 편집 중인 폼 상태는
 *   제약 밖이다 — 포트도 원본처럼 첫 매칭으로 동작).
 * - visited set 으로 사이클 방지, maxDepth 로 무한 체인 방어.
 */
export function applyChain(
  alias: string,
  rules: DowngradeRuleLike[],
  pct: number,
  maxDepth = 5,
): ChainResult {
  const visited = new Set([alias]);
  const path = [alias];
  const ruleIdx: number[] = [];
  let hops = 0;
  for (let i = 0; i < maxDepth; i++) {
    const idx = rules.findIndex(
      r => r.from_model_alias === alias && pct >= r.threshold_pct,
    );
    if (idx === -1 || visited.has(rules[idx].to_model_alias)) break;
    visited.add(rules[idx].to_model_alias);
    alias = rules[idx].to_model_alias;
    path.push(alias);
    ruleIdx.push(idx);
    hops++;
  }
  return { effective: alias, hops, path, ruleIdx };
}
