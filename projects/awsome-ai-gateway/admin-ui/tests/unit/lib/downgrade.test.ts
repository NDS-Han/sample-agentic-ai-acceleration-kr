// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { applyChain, type DowngradeRuleLike } from '@/lib/downgrade';

// 이 파일의 기대값은 gateway-proxy app/services/downgrade_loader.py 의
// apply_chain 과 1:1 로 대응한다 — 시뮬레이터가 런타임과 같은 판정을
// 보여주는 것이 전부이므로, 판정을 바꾸려면 양쪽을 같이 바꿔야 한다.

const RULES: DowngradeRuleLike[] = [
  { from_model_alias: 'opus', to_model_alias: 'sonnet', threshold_pct: 80 },
  { from_model_alias: 'opus[1m]', to_model_alias: 'sonnet', threshold_pct: 80 },
  { from_model_alias: 'sonnet', to_model_alias: 'haiku', threshold_pct: 90 },
];

describe('applyChain — gateway apply_chain 포트', () => {
  it('임계치 미달이면 강등 없음', () => {
    const r = applyChain('opus', RULES, 79);
    expect(r).toEqual({ effective: 'opus', hops: 0, path: ['opus'], ruleIdx: [] });
  });

  it('임계치 도달 시 한 단계 강등', () => {
    const r = applyChain('opus', RULES, 85);
    expect(r.effective).toBe('sonnet');
    expect(r.hops).toBe(1);
    expect(r.path).toEqual(['opus', 'sonnet']);
    expect(r.ruleIdx).toEqual([0]);
  });

  it('높은 임계치 도달 시 한 요청에서 다단 강등', () => {
    // opus→sonnet@80 + sonnet→haiku@90, pct=95 → opus 가 곧장 haiku 로 (2 hops)
    const r = applyChain('opus', RULES, 95);
    expect(r.effective).toBe('haiku');
    expect(r.hops).toBe(2);
    expect(r.path).toEqual(['opus', 'sonnet', 'haiku']);
    expect(r.ruleIdx).toEqual([0, 2]);
  });

  it('같은 임계치 체인은 도달 즉시 최하위로 점프', () => {
    const flat: DowngradeRuleLike[] = [
      { from_model_alias: 'opus', to_model_alias: 'sonnet', threshold_pct: 80 },
      { from_model_alias: 'sonnet', to_model_alias: 'haiku', threshold_pct: 80 },
    ];
    const r = applyChain('opus', flat, 80);
    expect(r.effective).toBe('haiku');
    expect(r.hops).toBe(2);
  });

  it('매칭 규칙이 없는 모델은 그대로', () => {
    const r = applyChain('haiku', RULES, 100);
    expect(r.effective).toBe('haiku');
    expect(r.hops).toBe(0);
  });

  it('사이클 규칙에서 visited set 으로 무한 루프 방지', () => {
    const cyc: DowngradeRuleLike[] = [
      { from_model_alias: 'a', to_model_alias: 'b', threshold_pct: 50 },
      { from_model_alias: 'b', to_model_alias: 'a', threshold_pct: 50 },
    ];
    const r = applyChain('a', cyc, 100);
    expect(r.effective).toBe('b');
    expect(r.hops).toBe(1);
  });

  it('maxDepth 초과 체인은 5-hop 에서 절단', () => {
    const deep: DowngradeRuleLike[] = Array.from({ length: 7 }, (_, i) => ({
      from_model_alias: `m${i}`,
      to_model_alias: `m${i + 1}`,
      threshold_pct: 10,
    }));
    const r = applyChain('m0', deep, 100);
    expect(r.hops).toBe(5);
    expect(r.effective).toBe('m5');
    expect(r.path).toHaveLength(6);
  });

  it('같은 from 의 규칙이 여럿이면 첫 매칭만 사용 (입력 순서)', () => {
    const dup: DowngradeRuleLike[] = [
      { from_model_alias: 'a', to_model_alias: 'b', threshold_pct: 80 },
      { from_model_alias: 'a', to_model_alias: 'c', threshold_pct: 50 },
    ];
    // pct=60: 첫 규칙(80) 미달이지만 두 번째(50) 도달 — 파이썬 next() 와 동일하게
    // "from 매칭 + 임계치 통과" 순서 검사이므로 idx=1 이 선택된다.
    const r = applyChain('a', dup, 60);
    expect(r.effective).toBe('c');
    expect(r.ruleIdx).toEqual([1]);
  });
});
