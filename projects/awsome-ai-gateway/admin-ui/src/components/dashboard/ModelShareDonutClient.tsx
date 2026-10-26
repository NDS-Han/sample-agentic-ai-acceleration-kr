'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import { useEffect, useMemo, useRef, useState } from 'react';
import { useTranslations } from 'next-intl';
import { Cell, Pie, PieChart } from 'recharts';
import { fmtUsd } from '@/lib/utils/format';
import type { ModelShareResponse, TeamOption } from '@/lib/actions/dashboard';
import { CATEGORICAL_PALETTE, useChartTheme } from '@/lib/utils/chartTheme';
import { modelDisplay } from '@/lib/utils/modelLabel';
import { teamDisplayName } from '@/lib/utils/trendSeries';
import { redirectToLoginIfUnauthorized } from '@/lib/utils/unauthorized';

// 항목 구분용 다색 카테고리 팔레트 (색맹 안전, 라이트/다크 공통).
const COLORS = CATEGORICAL_PALETTE;

interface Props {
  initialData: ModelShareResponse;
  teams: TeamOption[];
  period: string;
  client?: string;  // 대시보드 앱 필터(?client=) — team 변경 재조회 시에도 유지.
}

export function ModelShareDonutClient({ initialData, teams, period, client }: Props) {
  const t = useTranslations('dashboard');
  const [teamId, setTeamId] = useState<string>('all');
  const [data, setData] = useState<ModelShareResponse>(initialData);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [hoverIndex, setHoverIndex] = useState<number | null>(null);
  const theme = useChartTheme();
  const isFirstRender = useRef(true);

  useEffect(() => {
    // 첫 렌더는 SSR 로 받은 initialData 를 그대로 쓴다 — fetch 생략.
    if (isFirstRender.current) {
      isFirstRender.current = false;
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError(null);
    const params = new URLSearchParams({ period, team_id: teamId });
    if (client && client !== 'all') params.set('client', client);
    fetch(`/api/dashboard/model-share?${params}`)
      .then(async (r) => {
        // ⚠️ 401 은 화면에 찍을 문자열이 아니다. 이 fetch 는 문서 내비게이션이 아니라
        //    필터 변경으로 도는 것이어서 middleware 의 만료 분기가 아예 실행되지 않는다
        //    (middleware 는 `/api/` 를 공개 경로로 통과시킨다). 예전엔 여기서
        //    "조회 실패: HTTP 401" 을 렌더하고 **낡은 숫자를 그대로 남겼다** —
        //    사용자는 로그인으로 돌아갈 길조차 없었다. 세션이 죽었으면 로그인으로 보낸다.
        //    403(권한 부족)은 이 분기에 걸리지 않고 아래 에러 문구로 간다 — 정상이다.
        if (redirectToLoginIfUnauthorized(r)) return null;
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return (await r.json()) as ModelShareResponse;
      })
      .then((next) => {
        // next === null 은 401 리다이렉트 경로 — 낡은 데이터를 덮어쓰지 않고 빠진다.
        if (!cancelled && next) setData(next);
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof Error ? e.message : t('fetchFailedShort'));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [teamId, period, client]);

  // 모델 수가 늘면 도넛 조각은 슬라이버가 되고 범례는 세로로 넘친다 —
  // 상위 TOP_N 개만 남기고 나머지는 "기타" 한 조각/한 행으로 합친다.
  const TOP_N = 7;
  const displayModels: ((typeof data.models)[number] & { otherCount?: number })[] =
    useMemo(() => {
      // 8개면 8개 전부 보여주는 게 낫다 — "기타 1개 모델"은 숨기는 값보다 못하다.
      if (data.models.length <= TOP_N + 1) return data.models;
      const rest = data.models.slice(TOP_N);
      return [
        ...data.models.slice(0, TOP_N),
        {
          model_alias: '__other__',
          display_name: null,
          cost_usd: rest.reduce((s, m) => s + m.cost_usd, 0),
          share_pct: rest.reduce((s, m) => s + m.share_pct, 0),
          otherCount: rest.length,
        },
      ];
    }, [data]);

  // 데이터 재조회(team 변경 등)로 목록이 짧아지면 hover 인덱스가 범위를 벗어난다 —
  // 이펙트로 지우면 한 프레임 어긋남이 보이므로 사용 시점에 clamp 한다.
  const hi = hoverIndex != null && hoverIndex < displayModels.length ? hoverIndex : null;

  // canvas fillStyle 은 CSS 변수를 못 읽어 'var(--muted-foreground)' 는 검정으로
  // 떨어진다 — 캔버스 조각색에는 chartTheme 이 해석한 실제 색을 쓴다.
  // DOM 점은 별도로 CSS 클래스(bg-muted-foreground)를 쓴다 — 인라인에 해석값을
  // 박으면 SSR(항상 라이트 폴백)과 클라이언트 값이 어긋나 하이드레이션 불일치가 남는다.
  const sliceColor = (i: number, isOther: boolean) =>
    isOther ? theme.textMuted : COLORS[i % COLORS.length];

  const pieData = useMemo(
    () =>
      displayModels.map((m, i) => ({
        name:
          m.model_alias === '__other__'
            ? t('otherModels', { count: m.otherCount ?? 0 })
            : modelDisplay(m.model_alias, m.display_name),
        value: m.cost_usd,
        fill: sliceColor(i, m.model_alias === '__other__'),
      })),
    // sliceColor 는 theme 에 닫혀 있다 — 테마 전환 시 재계산.
    [displayModels, theme, t],
  );



  const isEmpty = data.models.length === 0 || data.total_cost_usd === 0;

  return (
    <div className="glass glass-hover rounded-apple p-5 h-full">
      <div className="flex items-start justify-between gap-3 mb-3">
        <div>
          <div className="text-sm font-semibold tracking-tight">{t('modelShareTitle')}</div>
          <div className="text-xs text-muted-foreground">{t('modelShareSubtitle')}</div>
        </div>
        <div className="flex items-center gap-2">
          <label htmlFor="team-select" className="text-xs text-muted-foreground">
            {t('scope')}
          </label>
          <select
            id="team-select"
            value={teamId}
            onChange={(e) => setTeamId(e.target.value)}
            className="rounded-apple-sm border border-input bg-background px-2 py-1 text-xs interactive focus:outline-none focus:ring-1 focus:ring-ring"
            disabled={loading}
          >
            <option value="all">{t('scopeAll')}</option>
            {teams.map((team) => (
              <option key={team.id} value={team.id}>
                {teamDisplayName({ team: team.name, dept_name: team.department_name })}
              </option>
            ))}
          </select>
        </div>
      </div>

      {error && (
        <p className="text-sm text-destructive mb-2">{t('fetchFailedWith', { error })}</p>
      )}

      {isEmpty ? (
        <div className="flex items-center justify-center h-64 text-sm text-muted-foreground">
          {t('noUsageForScope')}
        </div>
      ) : (
        <div className="flex flex-col items-center gap-4 lg:flex-row lg:items-center">
          {/* lg 부턴 도넛 왼쪽 + 범례 오른쪽 가로 배치. 범례는 도넛 높이(14rem)로
              cap 하고 스크롤 — 범례가 아무리 길어도 카드 높이가 늘어나지 않아
              옆 비용 추이 카드와 균형이 유지된다. 모바일은 종전대로 세로 스택. */}
          <div className="relative h-56 w-56 shrink-0">
            {/* 캔버스 툴팁은 중앙 오버레이를 가린다 — 호버 상세는 중앙 표시가 담당.
                activeShape 가 호버 조각을 바깥으로 튀어나오게 한다(hoverOffset 대응). */}
            <PieChart width={224} height={224}>
              <Pie
                data={pieData}
                dataKey="value"
                nameKey="name"
                innerRadius="62%"
                outerRadius="98%"
                stroke="none"
                paddingAngle={0}
                startAngle={90}
                endAngle={-270}
                onMouseEnter={(_, i) => setHoverIndex(i)}
                onMouseLeave={() => setHoverIndex(null)}
                isAnimationActive
              >
                {pieData.map((p, i) => (
                  <Cell key={i} fill={p.fill} />
                ))}
              </Pie>
            </PieChart>
            {/* 가운데: 기본은 점유율 1위(models 는 비용 desc 정렬, [0]=1위),
                조각 호버 시에는 그 조각의 수치로 바뀐다 — 툴팁을 대체. */}
            {(() => {
              const centerItem = (hi != null ? displayModels[hi] : null) ?? displayModels[0];
              const centerIdx = hi ?? 0;
              const isOther = centerItem?.model_alias === '__other__';
              return (
                <div className="absolute inset-0 flex flex-col items-center justify-center px-6 text-center pointer-events-none">
                  {centerItem && (
                    <span
                      className={`mb-1 h-2.5 w-2.5 rounded-full${isOther ? ' bg-muted-foreground' : ''}`}
                      style={{ backgroundColor: isOther ? undefined : COLORS[centerIdx % COLORS.length] }}
                      aria-hidden="true"
                    />
                  )}
                  <p className="text-2xl font-bold leading-none tracking-tight">
                    {centerItem ? `${centerItem.share_pct.toFixed(0)}%` : '—'}
                  </p>
                  <p className="mt-1 max-w-full truncate text-xs font-medium text-foreground">
                    {centerItem
                      ? isOther
                        ? t('otherModels', { count: centerItem.otherCount ?? 0 })
                        : modelDisplay(centerItem.model_alias, centerItem.display_name)
                      : ''}
                  </p>
                  <p className="mt-0.5 text-[10px] text-muted-foreground tabular-nums">
                    {hi != null && centerItem
                      ? fmtUsd(centerItem.cost_usd)
                      : t('topShare', { total: data.total_cost_usd.toLocaleString('en-US', {
                          minimumFractionDigits: 2,
                          maximumFractionDigits: 2,
                        }) })}
                  </p>
                </div>
              );
            })()}
          </div>
          <ul className="w-full space-y-1 lg:max-h-56 lg:min-w-0 lg:flex-1 lg:overflow-y-auto lg:pr-1">
            {displayModels.map((m, i) => {
              const isOther = m.model_alias === '__other__';
              const label = isOther
                ? t('otherModels', { count: m.otherCount ?? 0 })
                : modelDisplay(m.model_alias, m.display_name);
              return (
              <li
                key={m.model_alias}
                className={`grid grid-cols-[minmax(0,1fr)_4.25rem_2.75rem] items-baseline gap-2 text-sm rounded-apple-sm px-2 py-0.5 -mx-2 transition-colors ${i === hi ? 'bg-accent' : ''}`}
              >
                <div className="flex items-start gap-2 min-w-0">
                  <span
                    className={`w-3 h-3 rounded-full flex-shrink-0 mt-1${isOther ? ' bg-muted-foreground' : ''}`}
                    style={{ backgroundColor: isOther ? undefined : COLORS[i % COLORS.length] }}
                  />
                  {/* truncate 금지 — 모델명이 "Claude …" 로 잘려 구분이 안 된다. 줄바꿈 허용.
                      하이픈 토큰(claude-haiku-5-5)은 nowrap — 토큰 중간에서 끊기며
                      3줄이 되던 현상(L5) 방지. 공백에서만 줄바꿈된다. */}
                  <span className="font-medium break-words leading-snug" title={label}>
                    {label.split(' ').map((tok, ti) => (
                      <span key={ti} className={tok.includes('-') ? 'whitespace-nowrap' : undefined}>
                        {tok}{ti < label.split(' ').length - 1 ? ' ' : ''}
                      </span>
                    ))}
                  </span>
                </div>
                <span className="tabular-nums text-xs text-right">
                  {fmtUsd(m.cost_usd)}
                </span>
                <span className="text-muted-foreground tabular-nums text-xs text-right">
                  {m.share_pct.toFixed(1)}%
                </span>
              </li>
              );
            })}
          </ul>
        </div>
      )}
    </div>
  );
}