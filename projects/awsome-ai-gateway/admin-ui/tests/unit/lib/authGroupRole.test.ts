// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * parseJWT 의 그룹 → 역할 매핑, 그리고 **admin-api 와 같은 정책인지**.
 *
 * 배경 — IdP id_token 에는 `role` 클레임이 없다(IdP 는 groups 를 준다). 그래서
 * 예전 parseJWT 는 `payload['role']` 만 읽어 role 이 undefined 가 되고,
 * checkPagePermission 이 등재되지 않은 역할을 거부해 **로그인 직후 모든 페이지가
 * /403** 이었다. 같은 토큰으로 admin-api 는 ADMIN 을 인가하고 있었으니, API 는
 * 통과·UI 는 차단이라는 최악의 조합이었다(관리자 잠김).
 *
 * ⚠️ env 이름은 admin-api 와 **같아야** 한다(`ADMIN_GROUPS`, `OIDC_GROUPS_CLAIM`).
 *    이름이 갈리면 Helm 값 하나로 둘을 맞출 수 없고, 그 순간 같은 잠김이 재발한다.
 *    아래 마지막 describe 가 admin-api 소스를 읽어 그 이름을 대조한다.
 */

import { readFileSync } from 'node:fs';
import { basename, resolve } from 'node:path';

import { afterEach, describe, expect, it, vi } from 'vitest';

import { parseJWT } from '@/lib/auth';

function b64url(obj: unknown): string {
  return Buffer.from(JSON.stringify(obj)).toString('base64url');
}

function token(payload: Record<string, unknown>): string {
  return `header.${b64url(payload)}.sig`;
}

describe('parseJWT — 그룹 → 역할 (IdP id_token 경로)', () => {
  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it('role 클레임이 있으면 그대로 쓴다 (내부 admin JWT / dev 토큰 무변경)', () => {
    vi.stubEnv('ADMIN_GROUPS', 'GatewayAdmin');
    // groups 로는 ADMIN 이 아닌데 role 클레임은 TEAM_LEADER — 클레임이 이긴다.
    const t = token({ sub: 'u', role: 'TEAM_LEADER', groups: ['Other'] });
    expect(parseJWT(t).role).toBe('TEAM_LEADER');
  });

  it('role 이 없고 관리자 그룹에 속하면 ADMIN', () => {
    vi.stubEnv('ADMIN_GROUPS', 'GatewayAdmin');
    const t = token({ sub: 'u', email: 'a@b.c', groups: ['GatewayAdmin'] });
    expect(parseJWT(t).role).toBe('ADMIN');
  });

  it('관리자 그룹이 아니면 ADMIN 이 아니다 (fail-closed)', () => {
    vi.stubEnv('ADMIN_GROUPS', 'GatewayAdmin');
    const t = token({ sub: 'u', email: 'a@b.c', groups: ['Developers'] });
    expect(parseJWT(t).role).not.toBe('ADMIN');
  });

  it('groups claim 이름을 설정으로 읽는다 — Cognito 는 cognito:groups', () => {
    vi.stubEnv('ADMIN_GROUPS', 'GatewayAdmin');
    vi.stubEnv('OIDC_GROUPS_CLAIM', 'cognito:groups');
    const t = token({ sub: 'u', 'cognito:groups': ['GatewayAdmin'] });
    expect(parseJWT(t).role).toBe('ADMIN');
  });

  it('claim 이름을 설정하지 않으면 groups 를 본다 (기본값)', () => {
    vi.stubEnv('ADMIN_GROUPS', 'GatewayAdmin');
    const t = token({ sub: 'u', groups: ['GatewayAdmin'] });
    expect(parseJWT(t).role).toBe('ADMIN');
  });

  it('claim 이름이 어긋나면 승격하지 않는다 (조용한 오탐 방지)', () => {
    vi.stubEnv('ADMIN_GROUPS', 'GatewayAdmin');
    vi.stubEnv('OIDC_GROUPS_CLAIM', 'cognito:groups');
    // 토큰은 `groups` 로 주는데 설정은 `cognito:groups` 를 본다 → 매칭 없음
    const t = token({ sub: 'u', groups: ['GatewayAdmin'] });
    expect(parseJWT(t).role).not.toBe('ADMIN');
  });

  it('그룹 개명 후 옛 기본 그룹은 ADMIN 이 아니다', () => {
    // 이게 원래 사고의 형태다 — 한쪽만 개명하면 다른 쪽이 잠긴다.
    vi.stubEnv('ADMIN_GROUPS', 'GatewayAdmin');
    const t = token({ sub: 'u', groups: ['ClaudeAdmin'] });
    expect(parseJWT(t).role).not.toBe('ADMIN');
  });

  it('ADMIN_GROUPS 를 안 쓰면 그룹으로 승격하지 않는다', () => {
    const t = token({ sub: 'u', groups: ['GatewayAdmin'] });
    expect(parseJWT(t).role).not.toBe('ADMIN');
  });

  describe('CSV / JSON 파싱 — admin-api _split_csv 와 같은 규약', () => {
    it('콤마 구분 목록', () => {
      vi.stubEnv('ADMIN_GROUPS', 'Ops , GatewayAdmin , Sec');
      const t = token({ sub: 'u', groups: ['GatewayAdmin'] });
      expect(parseJWT(t).role).toBe('ADMIN');
    });

    it('JSON 배열', () => {
      vi.stubEnv('ADMIN_GROUPS', '["Ops","GatewayAdmin"]');
      const t = token({ sub: 'u', groups: ['GatewayAdmin'] });
      expect(parseJWT(t).role).toBe('ADMIN');
    });

    it('깨진 JSON 은 로그인을 막지 않고 "관리자 그룹 없음" 으로 취급', () => {
      vi.stubEnv('ADMIN_GROUPS', '["Ops",');
      const t = token({ sub: 'u', groups: ['GatewayAdmin'] });
      expect(() => parseJWT(t)).not.toThrow();
      expect(parseJWT(t).role).not.toBe('ADMIN');
    });
  });

  describe('그룹이 문자열 하나로 오는 IdP', () => {
    it('정확히 일치하면 ADMIN', () => {
      vi.stubEnv('ADMIN_GROUPS', 'GatewayAdmin');
      const t = token({ sub: 'u', groups: 'GatewayAdmin' });
      expect(parseJWT(t).role).toBe('ADMIN');
    });

    it('부분 문자열은 승격하지 않는다 — ReadOnly 그룹이 관리자가 되면 안 된다', () => {
      vi.stubEnv('ADMIN_GROUPS', 'GatewayAdmin');
      const t = token({ sub: 'u', groups: 'GatewayAdminReadOnly' });
      expect(parseJWT(t).role).not.toBe('ADMIN');
    });
  });

  it('parseJWT 의 다른 필드는 그대로다', () => {
    vi.stubEnv('ADMIN_GROUPS', 'GatewayAdmin');
    const t = token({
      sub: 'idp-subject-1',
      email: 'kim@example.com',
      name: 'Kim',
      groups: ['GatewayAdmin'],
      exp: 1893456000,
    });
    const s = parseJWT(t);
    expect(s.user_id).toBe('idp-subject-1');
    expect(s.email).toBe('kim@example.com');
    expect(s.display_name).toBe('Kim');
    expect(s.expires_at).toBe('1893456000');
  });
});

/**
 * 두 서비스가 **같은 env 이름**을 쓰는지 소스로 대조한다.
 *
 * 이 대조가 없으면 한쪽 이름만 바꿔도 테스트는 전부 통과하고, 배포 후에야
 * "API 는 되는데 UI 는 403" 으로 드러난다.
 */
describe('admin-api 와 env 이름 정합성', () => {
  function repoRoot(): string {
    // vitest 는 admin-ui 에서 돌린다(package.json test 스크립트). import.meta.url 은
    // Vite 가 /@fs/... 로 바꿔 쓸 수 없으므로 cwd 를 쓴다.
    const cwd = process.cwd();
    if (basename(cwd) !== 'admin-ui') {
      throw new Error(`cwd 가 admin-ui 가 아니다(${cwd}) — admin-ui 에서 vitest 를 돌려야 한다`);
    }
    return resolve(cwd, '..');
  }

  const CONFIG_PY = 'admin-api/src/app/core/config.py';
  const IDENTITY_PY = 'admin-api/src/app/core/oidc_identity.py';
  const AUTH_TS = 'admin-ui/src/lib/auth.ts';

  function read(rel: string): string {
    const p = resolve(repoRoot(), rel);
    const text = readFileSync(p, 'utf-8');
    // 대조군 — 빈 파일/경로 오류를 "일치" 로 오판하지 않는다.
    expect(text.length).toBeGreaterThan(200);
    return text;
  }

  it.each(['ADMIN_GROUPS', 'OIDC_GROUPS_CLAIM'])(
    '%s 를 admin-api 와 admin-ui 가 모두 쓴다',
    (name) => {
      expect(read(CONFIG_PY)).toContain(name);
      expect(read(AUTH_TS)).toContain(name);
    },
  );

  it('admin-api 의 역할 정책이 두 클레임을 모두 본다', () => {
    const identity = read(IDENTITY_PY);
    expect(identity).toContain('ADMIN_GROUPS');
    expect(identity).toContain('ADMIN_EMAILS');
  });

  it('admin-ui 는 NEXT_PUBLIC_ 접두사로 관리자 그룹을 노출하지 않는다', () => {
    // 클라이언트 번들에 관리자 그룹 이름을 굽지 않는다. parseJWT 는 서버 전용이다.
    expect(read(AUTH_TS)).not.toContain('NEXT_PUBLIC_ADMIN_GROUPS');
  });
});
