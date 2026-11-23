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
  return (
    new Intl.DateTimeFormat(locale, { timeZone, timeZoneName: 'short' })
      .formatToParts(at)
      .find((p) => p.type === 'timeZoneName')?.value ?? ''
  );
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

/** 캡션용 이름 — 'IST (Asia/Kolkata)'. 약어가 IANA 이름과 같으면(UTC) 한 번만. */
export function tzZoneText(timeZone: string, at?: Date): string {
  const abbr = tzAbbr(timeZone, at);
  return abbr === timeZone ? timeZone : `${abbr} (${timeZone})`;
}

/** 시각 문자열 뒤에 그 시점의 짧은 TZ 이름 — '11/06, 09:13 GMT+9'. */
export function withTzAbbr(text: string, timeZone: string, iso?: string): string {
  if (!text) return text;
  return `${text} ${tzAbbr(timeZone, iso ? new Date(iso) : undefined)}`;
}
