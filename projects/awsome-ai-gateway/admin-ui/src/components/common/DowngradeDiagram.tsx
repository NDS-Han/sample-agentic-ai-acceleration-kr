'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import type { ModelListItem } from '@/types/entities';

export interface DowngradeDiagramRule {
  from_model_alias: string;
  to_model_alias: string;
  threshold_pct: string | number;
  /** 읽기 전용 뷰에서 edgeTag 라벨링에 쓰는 스코프(TEAM/USER 등). */
  scope?: string;
}

/**
 * 다운그레이드 규칙(from→to)을 DAG로 시각화 — 노드=모델(output 단가 표기),
 * 엣지=규칙(% 라벨). 노드 깊이는 relaxation으로 계산: 어떤 규칙의 to 인 노드는
 * max(from 깊이)+1 레이어에 놓여 a→b→c 체인도 세 열로 정렬된다.
 * edgeTag 를 넘기면 % 라벨 뒤에 스코프 등의 꼬리표를 붙인다 (읽기 전용 카드용).
 */
export function DowngradeDiagram({
  rules,
  models,
  formatOutPrice,
  edgeTag,
}: {
  rules: DowngradeDiagramRule[];
  models: ModelListItem[];
  formatOutPrice: (_m: ModelListItem) => string;
  edgeTag?: (_rule: DowngradeDiagramRule) => string | null;
}) {
  const depth = new Map<string, number>();
  for (const r of rules) {
    if (!depth.has(r.from_model_alias)) depth.set(r.from_model_alias, 0);
  }
  for (let pass = 0; pass < rules.length; pass++) {
    for (const r of rules) {
      const d = (depth.get(r.from_model_alias) ?? 0) + 1;
      if ((depth.get(r.to_model_alias) ?? -1) < d) depth.set(r.to_model_alias, d);
    }
  }

  const maxDepth = Math.max(0, ...depth.values());
  const layerCount = maxDepth + 1;
  const priceOf = (alias: string) =>
    models.find(m => m.alias === alias)?.output_price_per_1k ?? 0;

  const layers: string[][] = Array.from({ length: layerCount }, () => []);
  for (const [alias, d] of depth) layers[d].push(alias);
  for (const l of layers) l.sort((a, b) => priceOf(b) - priceOf(a));

  const pos = new Map<string, { l: number; i: number; n: number }>();
  layers.forEach((nodes, l) =>
    nodes.forEach((a, i) => pos.set(a, { l, i, n: nodes.length })),
  );

  // 칩은 컬럼 폭의 86% (중앙 정렬) — 엣지는 칩의 좌/우 끝에 닿도록 보정.
  const chipPad = (100 / layerCount) * 0.07;
  const edgeX1 = (l: number) => ((l + 1) / layerCount) * 100 - chipPad;
  const edgeX2 = (l: number) => (l / layerCount) * 100 + chipPad;
  const nodeY = (i: number, n: number) => ((i + 0.5) / n) * 100;
  const height = Math.max(1, ...layers.map(l => l.length)) * 52;

  return (
    <div className="rounded-xl border border-border/60 bg-muted/20 px-3 py-3">
      <div className="relative" style={{ height }}>
        {layers.map((nodes, l) => (
          <div
            key={l}
            className="absolute top-0 bottom-0 flex flex-col justify-around"
            style={{ left: `${(l / layerCount) * 100}%`, width: `${100 / layerCount}%` }}
          >
            {nodes.map(alias => {
              const m = models.find(mm => mm.alias === alias);
              return (
                <div
                  key={alias}
                  className="mx-auto w-[86%] rounded-lg border border-border/60 bg-background/90 px-2 py-1 text-center shadow-sm"
                >
                  <div className="truncate font-mono text-[11px]">{alias}</div>
                  <div className="text-[9px] tabular-nums text-muted-foreground">
                    {m ? formatOutPrice(m) : '—'}
                  </div>
                </div>
              );
            })}
          </div>
        ))}

        <svg
          className="absolute inset-0 h-full w-full"
          viewBox="0 0 100 100"
          preserveAspectRatio="none"
          aria-hidden="true"
        >
          <defs>
            <marker
              id="dg-arrow"
              viewBox="0 0 10 10"
              refX="9"
              refY="5"
              markerWidth="5"
              markerHeight="5"
              orient="auto-start-reverse"
            >
              <path d="M0,0 L10,5 L0,10 z" className="fill-muted-foreground" />
            </marker>
          </defs>
          {rules.map((r, idx) => {
            const a = pos.get(r.from_model_alias);
            const b = pos.get(r.to_model_alias);
            if (!a || !b) return null;
            const x1 = edgeX1(a.l);
            const y1 = nodeY(a.i, a.n);
            const x2 = edgeX2(b.l);
            const y2 = nodeY(b.i, b.n);
            const dx = Math.max((x2 - x1) * 0.5, 4);
            return (
              <path
                key={idx}
                d={`M ${x1} ${y1} C ${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}`}
                fill="none"
                strokeWidth="1.5"
                vectorEffect="non-scaling-stroke"
                className="stroke-muted-foreground/60"
                markerEnd="url(#dg-arrow)"
              />
            );
          })}
        </svg>

        {rules.map((r, idx) => {
          const a = pos.get(r.from_model_alias);
          const b = pos.get(r.to_model_alias);
          if (!a || !b) return null;
          const mx = (edgeX1(a.l) + edgeX2(b.l)) / 2;
          const my = (nodeY(a.i, a.n) + nodeY(b.i, b.n)) / 2;
          const tag = edgeTag?.(r);
          return (
            <span
              key={`t${idx}`}
              className="absolute -translate-x-1/2 -translate-y-1/2 rounded-full border border-border/60 bg-background px-1.5 py-px text-[9px] font-semibold tabular-nums text-muted-foreground whitespace-nowrap"
              style={{ left: `${mx}%`, top: `${my}%` }}
            >
              {r.threshold_pct}%{tag ? ` · ${tag}` : ''}
            </span>
          );
        })}
      </div>
    </div>
  );
}
