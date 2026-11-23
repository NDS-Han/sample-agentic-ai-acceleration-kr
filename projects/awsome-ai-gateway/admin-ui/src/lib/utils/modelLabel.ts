// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

// App (client) → human label. Single source of truth; previously inline in ClientShareDonutClient.
// 제품명은 고유명사라 번역하지 않는다. 'other' 는 로케일마다 달라 여기 두지 않고
// 호출부가 i18n 문자열(dashboard.clientOther)을 otherLabel 로 넘긴다.
export const CLIENT_LABELS: Record<string, string> = {
  'claude-code': 'Claude Code',
  cowork: 'Cowork',
  codex: 'Codex',
};

export function labelFor(client: string, otherLabel?: string): string {
  if (client === 'other' && otherLabel) return otherLabel;
  return CLIENT_LABELS[client] ?? client;
}

// Model display: prefer the curated display_name; fall back to the raw alias when null/empty.
export function modelDisplay(alias: string, displayName?: string | null): string {
  return displayName && displayName.trim() ? displayName : alias;
}
