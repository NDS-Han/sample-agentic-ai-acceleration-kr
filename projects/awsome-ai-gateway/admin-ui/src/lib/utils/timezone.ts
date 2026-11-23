// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * 시간대 라벨 — 화면에 시각/날짜 기준을 적어 두 시계(브라우저 TZ vs 리포팅 TZ)를
 * 헷갈리지 않게 한다.
 *
 *  - 한 시점(이벤트·감사·본문 로그 시각 등)은 보는 사람의 브라우저 TZ 로 그리고
 *    짧은 이름을 붙인다('11/06 09:13 GMT+9').
 *  - 집계 경계(일/월 버킷, 시간 축, 예산 기간)는 REPORTING_TIMEZONE(배포 리전)
 *    기준이며 캡션으로 한 번 적는다('Dates in IST (Asia/Kolkata)').
 *
 * 이름은 항상 IANA TZ 에서 Intl 로 유도한다 — 약어를 하드코딩하지 않는다.
 */

// 'GMT+9', 'GMT-5:30', 'UTC+1' 처럼 오프셋만 있는 이름.
const OFFSET_ONLY = /^(GMT|UTC)[+\-−]/;

// 약어를 가진 로캘 순서 — en-US 는 미국 존(PST/EST), en-GB 는 유럽(CET/BST),
// en-IN 은 IST 를 약어로 낸다. ICU 가 해당 로캘에서 통용되는 약어만 주므로
// 오프셋보다 읽기 쉬운 이름이 있을 때만 그것을 쓴다(없으면 'GMT+9' 그대로).
const ABBR_LOCALES = ['en-US', 'en-GB', 'en-IN'] as const;

function shortName(locale: string, timeZone: string, at: Date): string {
  return nameOf(locale, timeZone, at, 'short');
}

/**
 * IANA TZ 의 짧은 이름 — 'KST'/'IST'/'PST', 약어가 없으면 'GMT+9' 같은 오프셋.
 * at 은 서머타임 판정용 시점(기본 지금). 잘못된 TZ 면 입력을 그대로 돌려준다.
 */
export function tzAbbr(timeZone: string, at: Date = new Date()): string {
  const when = Number.isNaN(at.getTime()) ? new Date() : at;
  let offsetName = '';
  try {
    for (const loc of ABBR_LOCALES) {
      const name = shortName(loc, timeZone, when);
      if (!name) continue;
      if (!OFFSET_ONLY.test(name)) return name;
      offsetName ||= name;
    }
  } catch {
    return timeZone;
  }
  return offsetName || timeZone;
}

function nameOf(locale: string, timeZone: string, at: Date, style: 'short' | 'shortGeneric' | 'shortOffset'): string {
  return (
    new Intl.DateTimeFormat(locale, { timeZone, timeZoneName: style })
      .formatToParts(at)
      .find((p) => p.type === 'timeZoneName')?.value ?? ''
  );
}

/** 'GMT-7' 처럼 그 시점의 UTC 오프셋. 잘못된 TZ 면 ''. */
export function tzOffset(timeZone: string, at: Date = new Date()): string {
  try {
    return nameOf('en-US', timeZone, at, 'shortOffset');
  } catch {
    return '';
  }
}

/**
 * 그 해에 서머타임이 있는 존인지 — 1월 1일과 7월 1일의 오프셋이 다르면 DST.
 * (남반구도 두 날 중 하나가 서머타임이라 같은 판정이 된다.)
 */
export function tzHasDst(timeZone: string, at: Date = new Date()): boolean {
  const y = (Number.isNaN(at.getTime()) ? new Date() : at).getUTCFullYear();
  const jan = tzOffset(timeZone, new Date(Date.UTC(y, 0, 1, 12)));
  const jul = tzOffset(timeZone, new Date(Date.UTC(y, 6, 1, 12)));
  return !!jan && !!jul && jan !== jul;
}

// 'PT'/'ET' 같은 짧은 일반명. 'Germany Time' 같은 지명형은 일반명이 아니다.
const GENERIC_ABBR = /^[A-Z]{1,5}$/;

/**
 * 날짜와 무관한(DST 독립) 존 이름 — 열 제목·캡션처럼 여러 시점을 한 번에 덮는 자리용.
 *
 *  - DST 없는 존(Asia/Kolkata·Asia/Seoul·UTC): tzAbbr 그대로('IST', 'GMT+9', 'UTC').
 *  - DST 존: Intl 'shortGeneric' 이 진짜 일반명('PT', 'ET')을 주면 그것, 아니면 IANA
 *    이름('Europe/Berlin'). 일반명이 계절 약어와 같으면(en-GB 의 Berlin='CET' 는
 *    겨울 약어와 같다) 여름 행에 틀린 이름이 되므로 일반명으로 치지 않는다.
 *
 * 'now' 기준 약어(PST)를 열 제목에 쓰면 DST 전환을 낀 30일 표에서 옛 행(PDT)과
 * 제목이 어긋난다 — 행별 정확한 약어·오프셋은 셀 툴팁(tzExact)이 맡는다.
 */
export function tzZoneLabel(timeZone: string, at: Date = new Date()): string {
  try {
    if (!tzHasDst(timeZone, at)) return tzAbbr(timeZone, at);
    const y = (Number.isNaN(at.getTime()) ? new Date() : at).getUTCFullYear();
    const jan = new Date(Date.UTC(y, 0, 1, 12));
    const jul = new Date(Date.UTC(y, 6, 1, 12));
    for (const loc of ABBR_LOCALES) {
      const generic = nameOf(loc, timeZone, jan, 'shortGeneric');
      if (!GENERIC_ABBR.test(generic)) continue;
      const seasonal = [nameOf(loc, timeZone, jan, 'short'), nameOf(loc, timeZone, jul, 'short')];
      if (!seasonal.includes(generic)) return generic;
    }
    return timeZone;
  } catch {
    return timeZone;
  }
}

/**
 * 한 시점의 정확한 이름 — 'PDT (GMT-7)', 'IST (GMT+5:30)', 오프셋뿐이면 'GMT+9'.
 * 셀 툴팁용(열 제목은 tzZoneLabel 의 DST 독립 이름).
 */
export function tzExact(timeZone: string, at: Date = new Date()): string {
  const abbr = tzAbbr(timeZone, at);
  // ICU 는 영오프셋을 'GMT+0' 이 아니라 'GMT' 로 줄 때도 있다 — 같은 이름으로 본다.
  const off = tzOffset(timeZone, at).replace(/^GMT\+0$/, 'GMT');
  return !off || abbr === off || abbr === timeZone ? abbr : `${abbr} (${off})`;
}

/**
 * 캡션용 이름 — 'IST (Asia/Kolkata)', 'ET (America/New_York)', 'Europe/Berlin'.
 * DST 독립(tzZoneLabel) — 'EST'/'EDT' 처럼 계절 약어를 캡션에 박지 않는다.
 * 이름이 IANA 이름과 같으면(UTC·Europe/Berlin) 한 번만.
 */
export function tzZoneText(timeZone: string, at?: Date): string {
  const label = tzZoneLabel(timeZone, at);
  return label === timeZone ? timeZone : `${label} (${timeZone})`;
}

/** 시각 문자열 뒤에 그 시점의 짧은 TZ 이름 — '11/06, 09:13 GMT+9'. */
export function withTzAbbr(text: string, timeZone: string, iso?: string): string {
  if (!text) return text;
  return `${text} ${tzAbbr(timeZone, iso ? new Date(iso) : undefined)}`;
}
