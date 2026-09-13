// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * `/apps` 화면이 **도달 가능한지**, 그리고 두 축의 `allowed_clients` 가 섞이지 않는지.
 *
 * 왜 라우트 배선을 테스트하나
 * ---------------------------
 * 이 앱에서 새 페이지를 죽이는 가장 쉬운 방법은 라우트만 추가하고
 * `PAGE_PERMISSIONS` 항목을 빼먹는 것이다. `checkPagePermission` 은 등재되지 않은
 * 경로를 **default-deny** 하므로, ADMIN 을 포함한 **모든 역할**이 /403 으로 튕긴다.
 * 컴포넌트도 서버 액션도 완벽한데 화면이 통째로 열리지 않고, 원인이 권한 표에 있다는
 * 것은 코드를 봐서는 드러나지 않는다.
 *
 * 두 번째 배선: 사이드바 항목의 `allowedRoles` 가 `PAGE_PERMISSIONS` 와 어긋나면
 *   - 좁으면 메뉴에 안 보이고(있는 기능을 아무도 찾지 못한다),
 *   - 넓으면 보이지만 클릭하면 /403 이다(고장으로 읽힌다).
 *
 * 세 번째: `apps` i18n 네임스페이스. 키가 없으면 next-intl 이 키 문자열을 그대로
 * 렌더하거나 throw 한다 — 특히 "전면 거부" 경고 문구가 빠지면 운영자가 위험한 액션을
 * 경고 없이 수행한다.
 */

import { readFileSync } from 'node:fs';
import { basename, resolve } from 'node:path';

import { describe, expect, it } from 'vitest';

// ⚠️ 두 심볼의 위치가 다르다: 권한 표는 lib/permissions, 판정 함수는 lib/auth.
//    한 곳에서 import 하려 하면 TS2305 다.
import { checkPagePermission } from '@/lib/auth';
import { PAGE_PERMISSIONS } from '@/lib/permissions';
import { UserRole } from '@/types/enums';

function uiRoot(): string {
  const cwd = process.cwd();
  if (basename(cwd) !== 'admin-ui') {
    throw new Error(`cwd 가 admin-ui 가 아니다(${cwd}) — admin-ui 에서 vitest 를 돌려야 한다`);
  }
  return cwd;
}

function read(rel: string): string {
  const text = readFileSync(resolve(uiRoot(), rel), 'utf-8');
  // 대조군 — 경로 오류를 "일치" 로 오판하지 않는다.
  expect(text.length).toBeGreaterThan(100);
  return text;
}

describe('/apps 라우트 도달성', () => {
  it('PAGE_PERMISSIONS 에 등재돼 있다', () => {
    expect(Object.keys(PAGE_PERMISSIONS)).toContain('/apps');
  });

  it('ADMIN 은 통과한다', () => {
    expect(checkPagePermission('/apps', UserRole.ADMIN)).toBe(true);
  });

  it('ADMIN 외 역할은 거부된다 — 백엔드가 require_admin 이므로', () => {
    // 넓히면 페이지는 열리지만 조회·저장이 전부 403 인 화면이 된다.
    expect(checkPagePermission('/apps', UserRole.TEAM_LEADER)).toBe(false);
    expect(checkPagePermission('/apps', UserRole.DEVELOPER)).toBe(false);
  });

  it('하위 경로도 같은 권한을 받는다 (prefix 매칭)', () => {
    expect(checkPagePermission('/apps/codex', UserRole.ADMIN)).toBe(true);
    expect(checkPagePermission('/apps/codex', UserRole.DEVELOPER)).toBe(false);
  });

  it('대조군 — 등재되지 않은 경로는 default-deny 다', () => {
    // 이 성질이 곧 "PAGE_PERMISSIONS 를 빼먹으면 전 역할 /403" 의 근거다.
    expect(checkPagePermission('/definitely-not-a-page', UserRole.ADMIN)).toBe(false);
  });

  it('라우트 파일이 실재한다', () => {
    expect(read('src/app/apps/page.tsx')).toContain('AppPolicyPanel');
  });
});

describe('사이드바 항목이 권한과 일치한다', () => {
  const sidebar = () => read('src/components/layout/Sidebar.tsx');

  it('/apps 항목이 있다', () => {
    expect(sidebar()).toContain("href: '/apps'");
  });

  it('allowedRoles 가 PAGE_PERMISSIONS 와 같다', () => {
    const src = sidebar();
    const idx = src.indexOf("href: '/apps'");
    expect(idx).toBeGreaterThan(-1);
    // 그 항목 객체의 allowedRoles 만 잘라 본다.
    const tail = src.slice(idx, idx + 400);
    const m = /allowedRoles:\s*\[([^\]]*)\]/.exec(tail);
    expect(m, 'allowedRoles 를 찾지 못했다').toBeTruthy();
    const roles = m![1]
      .split(',')
      .map((r) => r.trim())
      .filter(Boolean);

    // PAGE_PERMISSIONS['/apps'] 는 [ADMIN] 이므로 사이드바도 ADMIN 하나여야 한다.
    expect(PAGE_PERMISSIONS['/apps']).toEqual([UserRole.ADMIN]);
    expect(roles).toHaveLength(1);
    expect(roles[0]).toContain('ADMIN');
  });
});

describe('apps i18n 네임스페이스', () => {
  const NEEDED = [
    'noneBadge',
    'noneHint',
    'confirmLastTitle',
    'confirmLastMessage',
  ];

  it.each(['ko', 'en'])('%s: 전면 거부 관련 키가 있다', (locale) => {
    const msgs = JSON.parse(read(`messages/${locale}.json`)) as Record<string, unknown>;
    const apps = msgs['apps'] as Record<string, string> | undefined;
    expect(apps, `messages/${locale}.json 에 apps 네임스페이스가 없다`).toBeTruthy();
    for (const key of NEEDED) {
      expect(apps![key], `apps.${key} 가 없다 — 전면 거부 경고가 사라진다`).toBeTruthy();
    }
  });

  it.each(['ko', 'en'])('%s: nav.apps 가 있다', (locale) => {
    const msgs = JSON.parse(read(`messages/${locale}.json`)) as Record<string, unknown>;
    const nav = msgs['nav'] as Record<string, string>;
    expect(nav['apps'], 'nav.apps 가 없으면 메뉴 라벨이 키 문자열로 렌더된다').toBeTruthy();
  });

  it('패널이 쓰는 모든 t() 키가 양쪽 로케일에 있다', () => {
    const src = read('src/components/apps/AppPolicyPanel.tsx');
    const keys = [...src.matchAll(/t\('([a-zA-Z.]+)'/g)].map((m) => m[1]);
    expect(keys.length, 't() 호출을 찾지 못했다 — 이 검사가 공허하다').toBeGreaterThan(5);
    for (const locale of ['ko', 'en']) {
      const msgs = JSON.parse(read(`messages/${locale}.json`)) as Record<string, unknown>;
      const apps = msgs['apps'] as Record<string, string>;
      const missing = keys.filter((k) => !(k in apps));
      expect(missing, `${locale} 에 없는 키: ${missing.join(', ')}`).toEqual([]);
    }
  });
});

describe('두 allowed_clients 축이 섞이지 않는다', () => {
  it('모델 축은 3-state 판별자를 쓴다', async () => {
    const { modelAppScope } = await import('@/lib/constants/gateway');
    expect(modelAppScope(null)).toEqual({ kind: 'unrestricted' });
    expect(modelAppScope(undefined)).toEqual({ kind: 'unrestricted' });
    expect(modelAppScope([])).toEqual({ kind: 'none' });
    expect(modelAppScope(['codex'])).toEqual({ kind: 'list', clients: ['codex'] });
  });

  it('사용자 축의 collapse 는 전체 선택을 [] 로 접는다 (의미가 반대다)', async () => {
    const { collapseAllowedClients, CLIENTS } = await import('@/lib/constants/gateway');
    // 전체 선택 → [] = "제한 없음". 모델 축에서 [] 는 전면 거부다.
    expect(collapseAllowedClients([...CLIENTS])).toEqual([]);
    expect(collapseAllowedClients(['codex'])).toEqual(['codex']);
  });

  it('두 축의 빈 배열 의미가 서로 다르다는 것 자체를 못 박는다', async () => {
    const { modelAppScope, expandAllowedClients, CLIENTS } = await import(
      '@/lib/constants/gateway'
    );
    // 사용자 축: [] → 전체 허용으로 펼친다
    expect(expandAllowedClients([])).toEqual([...CLIENTS]);
    // 모델 축: [] → 'none'(전면 거부)
    expect(modelAppScope([]).kind).toBe('none');
  });

  it('패널이 모델 축 헬퍼를 쓰고 사용자 축 헬퍼를 쓰지 않는다', () => {
    const src = read('src/components/apps/AppPolicyPanel.tsx');
    expect(src).toMatch(/modelAppScope|modelAllowsClient/);
    // ⚠️ expandAllowedClients 는 사용자 축 전용이다. 모델 축에 쓰면 [] 가
    //    "전체 앱 허용" 으로 펼쳐져 전면 거부가 뒤집힌다.
    expect(src).not.toContain('expandAllowedClients');
    expect(src).not.toContain('collapseAllowedClients');
  });
});
