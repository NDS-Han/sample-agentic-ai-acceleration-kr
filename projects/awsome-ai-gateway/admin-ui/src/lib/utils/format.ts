// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * 공용 통화 포맷 — 앱 전체에서 USD 를 한 가지 규칙으로 표시한다.
 *
 * 예전엔 페이지마다 `$${n.toFixed(2)}`(그룹핑 없음), `.toFixed(4)`, 로컬
 * `fmtUsd2` 등 4종이 혼재해 같은 "총비용"이 화면마다 다르게 보였다.
 * 기본은 2자리+천단위 그룹핑; 소액(크레딧/토큰당 단가급)은 precision 을 올린다.
 */
export function fmtUsd(n: number, precision = 2): string {
  if (!Number.isFinite(n)) return '$0.00';
  return `$${n.toLocaleString('en-US', {
    minimumFractionDigits: precision,
    maximumFractionDigits: precision,
  })}`;
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
  return d.toLocaleTimeString(locale, { timeZone });
}
