// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * 공용 통화 포맷 — 앱 전체에서 USD 를 한 가지 규칙으로 표시한다.
 *
 * 예전엔 페이지마다 `$${n.toFixed(2)}`(그룹핑 없음), `.toFixed(4)`, 로컬
 * `fmtUsd2` 등 4종이 혼재해 같은 "총비용"이 화면마다 다르게 보였다.
 *
 * 규칙(기본 precision=2):
 *  - |n| ≥ $0.01 또는 0 → 소수 2자리 + 천단위 그룹핑 ($1,234.50, $0.16)
 *  - 0 < |n| < $0.01 → 유효숫자 2개 ($0.0071, $0.000078). 예전엔 소수 6자리
 *    고정이라 $0.007100 처럼 의미 없는 0 이 붙었다. '$0.00' 으로 뭉개면 사용
 *    자체가 없어 보이므로 소액도 값이 보이게 한다.
 * precision 을 3 이상으로 명시하면 그 자릿수 고정(채팅 버블 등 기존 호출 유지).
 */
export function fmtUsd(n: number, precision = 2): string {
  if (!Number.isFinite(n)) return '$0.00';
  if (precision <= 2 && n !== 0 && Math.abs(n) < 0.01) {
    const small = smallUsd(n);
    if (small) return small;
  }
  return `$${n.toLocaleString('en-US', {
    minimumFractionDigits: precision,
    maximumFractionDigits: precision,
  })}`;
}

/** $0.01 미만 — 유효숫자 2개. 반올림 결과가 $0.01 에 닿으면 null(2자리 규칙으로). */
function smallUsd(n: number): string | null {
  const rounded = Number(n.toPrecision(2));
  if (Math.abs(rounded) >= 0.01) return null;
  return `$${n.toLocaleString('en-US', {
    minimumSignificantDigits: 2,
    maximumSignificantDigits: 2,
  })}`;
}

/**
 * 단가(per-1M 토큰 등) 포맷 — fmtUsd 와 같은 규칙에 \"단가 정밀도 보존\"만 더한다.
 *  - ≥ $0.01: 최소 소수 2자리, 끝 0 을 지우며 최대 maxFraction 자리
 *    ($2.00, $0.10, $0.1375 — Haiku 캐시 쓰기 $0.1375/M 이 $0.14 로 잘리면 안 됨, US-19)
 *  - < $0.01: fmtUsd 와 동일하게 유효숫자 2개
 */
export function fmtUsdRate(n: number, maxFraction = 5): string {
  if (!Number.isFinite(n)) return '$0.00';
  if (n !== 0 && Math.abs(n) < 0.01) {
    const small = smallUsd(n);
    if (small) return small;
  }
  return `$${n.toLocaleString('en-US', {
    minimumFractionDigits: 2,
    maximumFractionDigits: maxFraction,
  })}`;
}

/**
 * 차트 축 눈금용 USD — 눈금 간격(step)의 소수 자릿수로 모든 눈금을 맞춘다
 * ($0.00, $0.05, $0.10 …). 1,000 이상 간격은 k 접미사.
 */
export function fmtUsdTick(v: number, step: number): string {
  if (!Number.isFinite(v)) return '';
  if (step >= 1000) {
    const k = v / 1000;
    return `$${k.toLocaleString('en-US', { maximumFractionDigits: fractionDigits(step / 1000) })}k`;
  }
  const d = fractionDigits(step);
  return `$${v.toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d })}`;
}

function fractionDigits(step: number): number {
  if (!(step > 0)) return 0;
  const s = String(Number(step.toPrecision(12)));
  if (s.includes('e-')) return Number(s.split('e-')[1]);
  const i = s.indexOf('.');
  return i < 0 ? 0 : s.length - i - 1;
}

/**
 * nice ticks — 0 부터 max 를 덮는 1·2·2.5·5×10ⁿ 간격 눈금.
 * 예: max 0.135 → [0, 0.05, 0.1, 0.15]. recharts 기본 눈금은 도메인을 균등
 * 분할해 $0.135, $0.09 같은 어중간한 값이 나온다.
 */
export function niceTicks(max: number, target = 4): number[] {
  if (!Number.isFinite(max) || max <= 0) return [0, 1];
  const raw = max / target;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const norm = raw / mag;
  const unit = norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 2.5 ? 2.5 : norm <= 5 ? 5 : 10;
  const step = unit * mag;
  const count = Math.max(1, Math.ceil(max / step - 1e-9));
  return Array.from({ length: count + 1 }, (_, i) => Number((i * step).toPrecision(12)));
}

/**
 * 지연시간 — 1초 미만은 ms(정수), 이상은 초 소수 1자리. 55931 → '55.9s'.
 * 0/음수/비유한은 데이터 없음이라 '—'.
 */
export function fmtLatencyMs(ms: number | null | undefined): string {
  if (ms == null || !Number.isFinite(ms) || ms <= 0) return '—';
  if (ms < 1000) return `${Math.round(ms).toLocaleString('en-US')}ms`;
  return `${(ms / 1000).toLocaleString('en-US', { minimumFractionDigits: 1, maximumFractionDigits: 1 })}s`;
}

/**
 * 비율 목록을 반올림해도 합이 정확히 100 이 되게 한다(최대 나머지 방식).
 * 각자 toFixed(1) 하면 99.95 + 0.05 → 100.0 + 0.1 = 100.1% 가 된다.
 * 입력 합이 0 이면 그대로 반올림만 한다.
 */
export function roundSharesTo100(shares: number[], decimals = 1): number[] {
  const scale = 10 ** decimals;
  const sum = shares.reduce((s, v) => s + (Number.isFinite(v) ? v : 0), 0);
  if (!(sum > 0)) return shares.map((v) => Math.round(v * scale) / scale);
  const scaled = shares.map((v) => ((Number.isFinite(v) ? v : 0) / sum) * 100 * scale);
  const floors = scaled.map(Math.floor);
  let remain = Math.round(100 * scale) - floors.reduce((s, v) => s + v, 0);
  const order = scaled
    .map((v, i) => ({ i, frac: v - floors[i] }))
    .sort((a, b) => b.frac - a.frac);
  for (const { i } of order) {
    if (remain <= 0) break;
    floors[i] += 1;
    remain -= 1;
  }
  return floors.map((v) => v / scale);
}

/** 보정된 비율 표시 — 실제 값이 0 보다 큰데 반올림이 0 이면 '<0.1%'. */
export function fmtSharePct(rounded: number, raw: number, decimals = 1): string {
  if (rounded === 0 && raw > 0) return `<${(1 / 10 ** decimals).toFixed(decimals)}%`;
  return `${rounded.toFixed(decimals)}%`;
}

/**
 * 날짜/시각 포맷 — 표는 YYYY-MM-DD(모호성 없음), 시각 포함은 locale 짧은 형식.
 * timeZone 은 항상 리포팅 TZ 를 넘긴다 — 브라우저 로컬이면 운영자마다 다른
 * 날짜가 보이고, 집계 버킷(REPORTING_TIMEZONE 경계)과 어긋난다.
 */
export function fmtDate(iso: string, timeZone?: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).formatToParts(d);
  const map = Object.fromEntries(parts.map((p) => [p.type, p.value]));
  return `${map.year}-${map.month}-${map.day}`;
}

export function fmtDateTime(iso: string, locale: string, timeZone?: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString(locale, {
    timeZone,
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  });
}

/**
 * 토큰 수 컴팩트 포맷 — 컨텍스트 윈도우/최대 출력 표시용.
 * Intl compact 는 ko 로케일에서 '25.6만' 같이 로케일 종속 단위로 나와
 * 표 안에서 열 폭과 의미가 흔들린다 — K/M 고정 접미사로 통일한다.
 * 반올림 기준: 262,144 → '262K'(가장 가까운 K), 1,048,576 → '1M'.
 */
export function fmtTokensCompact(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n) || n <= 0) return '—';
  // B 분기 필수 — 없으면 10억 이상이 '1050M'처럼 다른 위젯(대시보드 KPI의
  // '1.05B')과 다른 단위로 렌더된다.
  if (n >= 1_000_000_000) {
    const b = n / 1_000_000_000;
    return `${b >= 10 ? Math.round(b) : Math.round(b * 10) / 10}B`;
  }
  if (n >= 1_000_000) {
    const m = n / 1_000_000;
    return `${m >= 10 ? Math.round(m) : Math.round(m * 10) / 10}M`;
  }
  if (n >= 1_000) return `${Math.round(n / 1_000)}K`;
  return `${n}`;
}

export function fmtTime(iso: string, locale: string, timeZone?: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleTimeString(locale, { timeZone, hour12: false });
}
