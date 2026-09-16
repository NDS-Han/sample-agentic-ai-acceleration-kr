// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * 에러 본문의 `detail` 을 사람이 읽을 수 있는 한 줄로. 문자열이면 그대로,
 * pydantic 422 배열이면 'field: reason; field: reason' 으로 접는다.
 * 어떤 입력에도 "[object Object]" 를 만들지 않는다. 값이 없으면 undefined.
 */
export function formatErrorDetail(detail: unknown): string | undefined {
  if (detail == null) return undefined;
  if (typeof detail === 'string') return detail || undefined;
  if (Array.isArray(detail)) {
    const parts = detail
      .map((item) => {
        if (typeof item === 'string') return item;
        if (item && typeof item === 'object') {
          const rec = item as { loc?: unknown; msg?: unknown };
          const loc = Array.isArray(rec.loc)
            ? rec.loc.slice(1).map(String).join('.') || String(rec.loc[0] ?? '')
            : '';
          const msg = typeof rec.msg === 'string' ? rec.msg : JSON.stringify(item);
          return loc ? `${loc}: ${msg}` : msg;
        }
        return String(item);
      })
      .filter(Boolean);
    return parts.length ? parts.join('; ') : undefined;
  }
  if (typeof detail === 'object') {
    const rec = detail as { msg?: unknown; message?: unknown };
    if (typeof rec.msg === 'string') return rec.msg;
    if (typeof rec.message === 'string') return rec.message;
    try {
      return JSON.stringify(detail);
    } catch {
      return undefined;
    }
  }
  return String(detail);
}
