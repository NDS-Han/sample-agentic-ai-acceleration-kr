// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * AdminAPIClient — server-side only.
 *
 * Wraps the admin-api backend with automatic cookie forwarding and
 * structured error handling (PERF-01: cache: 'no-store').
 */

import { cookies } from 'next/headers';
import { APIError } from '@/lib/utils/retry';
import { formatErrorDetail } from '@/lib/utils/errorDetail';
import {
  LOGIN_PATH,
  UNAUTHORIZED_ERROR_CODE,
  UNAUTHORIZED_MESSAGE,
} from '@/lib/utils/unauthorized';

const ADMIN_API_URL = process.env.ADMIN_API_URL || 'http://admin-api:8080';
// R17-S6 — admin-api 행 시 요청이 무기한 걸리지 않게 기본 상한.
const UPSTREAM_TIMEOUT_MS = 15_000;

/**
 * 401 전용 오류. 호출부가 "인증이 죽었다"(로그인으로 보내야 함)와 "권한이 없다"(403,
 * '—' 로 렌더해야 함)를 구분할 수 있게 타입을 나눈다.
 *
 * ⚠️ APIError 를 상속하는 게 핵심이다. 기존 호출부는 전부 `err instanceof APIError` 로
 *    메시지를 뽑고(예: lib/actions/keys.ts 의 toErrorMessage), withRetry 도
 *    `err instanceof APIError` 로 재시도 여부를 본다(retryOn 기본 [503] 이라 401 은
 *    즉시 재던짐). 별도 클래스로 떼어냈다면 그 경로들이 조용히 "예기치 않은 오류" 로
 *    퇴화한다. 상속이라 기존 동작은 100% 그대로고, 구분이 필요한 곳만 좁혀 보면 된다.
 *
 * ⚠️ 여기서 next/navigation 의 redirect() 를 던지지 않는 이유: api-client 는 서버
 *    컴포넌트와 서버 액션에서 같이 쓰인다. 서버 액션들은 `try { … } catch (err) { return
 *    { success: false, error: toErrorMessage(err) } }` 형태라(lib/actions/keys.ts),
 *    redirect() 가 던지는 NEXT_REDIRECT 를 그대로 삼켜서 리다이렉트는 일어나지 않고
 *    사용자에겐 "NEXT_REDIRECT" 문자열이 토스트로 뜬다. 즉 지금보다 더 나빠진다.
 */
export class UnauthorizedError extends APIError {
  /** 호출부가 보낼 곳을 하드코딩하지 않도록 같이 실어 준다. */
  readonly loginUrl = LOGIN_PATH;

  constructor(message: string = UNAUTHORIZED_MESSAGE, details: unknown = null) {
    super(401, UNAUTHORIZED_ERROR_CODE, message, details);
    this.name = 'UnauthorizedError';
  }
}

/** 좁히기 헬퍼 — 호출부가 클래스를 직접 import 하지 않아도 되게. */
export function isUnauthorized(err: unknown): err is UnauthorizedError {
  return err instanceof UnauthorizedError;
}


class AdminAPIClient {
  private async fetch<T>(path: string, options?: RequestInit): Promise<T> {
    const cookieStore = await cookies();
    const adminJwt = cookieStore.get('admin_jwt')?.value;
    const url = `${ADMIN_API_URL}${path}`;

    // R17-S3 — 서버 액션 인자는 클라이언트가 마음대로 정한다. 경로 보간이
    // 인코딩 없이 들어가면(예: `../..`, `%2e%2e`) URL 정규화가 상위 디렉터리로
    // 탈출해 사용자 쿠키를 붙인 요청이 의도 밖의 admin-api 경로로 나간다.
    // 호출부 인코딩과 별개로, 정규화된 최종 경로가 /admin/ 아래인지 여기서
    // 한 번 더 확인한다(심층 방어 — chat-proxy 와 같은 계약).
    const normalized = new URL(url).pathname;
    // '..'/'/.'/`%2e` 세그먼트와 `#`/`?` 절단은 정규화된 pathname 을 원본과
    //    다르게 만든다 — 일치 검사가 위협군을 거절한다
    //    (deleteRateLimitAction('..','x') → DELETE /admin/x,
    //    scopeId='%2e%2e/keys/<id>#' → DELETE /admin/keys/<id> 차단;
    //    claude R17 #4). `#` 는 양쪽이 같은 지점에서 잘려 비교로 못 잡으므로
    //    별도 거절; `?` 절단은 진짜 쿼리스트링과 구분 불가라 호출부 인코딩이
    //    계약이다(claude R17-2 S1). 인코딩된 값은 pathname 에 그대로 남아 오탐 없음.
    const rawPath = path.split('?')[0].split('#')[0];
    if (normalized !== rawPath || path.includes('#')) {
      throw new APIError(
        400,
        'INVALID_PATH',
        `admin-api path was normalized differently (traversal/truncation): ${normalized}`,
      );
    }
    // /admin/ 접두 밖의 허용 경로 — CLI 다운로드 목록은 admin-api 의 /cli 라우터다.
    //    정확히 이 경로만 예외로 둔다(와일드카드/접두 허용은 탈출 경로를 다시 연다).
    const allowedOutsideAdmin = normalized === '/cli/downloads';
    if (!normalized.startsWith('/admin/') && !allowedOutsideAdmin) {
      throw new APIError(
        400,
        'INVALID_PATH',
        `admin-api path escaped allowed prefix: ${normalized}`,
      );
    }

    const response = await fetch(url, {
      ...options,
      cache: 'no-store',
      // R17-S6 — upstream(admin-api) 행/지연이 RSC 렌더와 프록시를 무기한
      // 붙잡지 않게 기본 타임아웃을 둔다. 호출자 signal 과 병합(무시 금지).
      signal: options?.signal
        ? AbortSignal.any([options.signal, AbortSignal.timeout(UPSTREAM_TIMEOUT_MS)])
        : AbortSignal.timeout(UPSTREAM_TIMEOUT_MS),
      headers: {
        'Content-Type': 'application/json',
        // R17 nit — 쿠키 전체(oidc_verifier/state/admin_role 등)를 넘기지 말고
        // 인증에 필요한 admin_jwt 만 전달한다.
        Cookie: adminJwt ? `admin_jwt=${adminJwt}` : '',
        ...options?.headers,
      },
    });

    if (!response.ok) {
      let errorBody: {
        error_code?: string;
        message?: string;
        // ⚠️ FastAPI/pydantic 의 기본 422 는 detail 이 **배열**
        //    ([{loc,msg,type}, ...])이다. 문자열로 가정하고 그대로 메시지에 넣으면
        //    토스트에 "[object Object]" 만 떠서 어느 필드가 틀렸는지 알 수 없었다.
        //    admin-api 는 이제 {"error": {...}} 로 정규화하지만, 게이트웨이/프록시
        //    계층이 원본 422 를 그대로 흘릴 수 있으니 클라이언트에서도 방어한다.
        detail?: unknown;
        // error.details — 409 confirmation_required 의 영향 목록(warnings) 등
        //    구조화된 부가 정보. top-level detail 만 넘기면 이 정보가 유실돼
        //    확인 다이얼로그가 영향받는 사용자/앱을 표시하지 못한다(R25-S7).
        error?: { code?: string; message?: string; details?: unknown };
      } = {};
      try {
        errorBody = (await response.json()) as typeof errorBody;
      } catch {
        // Response body may not be JSON — use empty defaults
      }

      const detailText = formatErrorDetail(errorBody.detail);

      const message =
        errorBody.message ??
        errorBody.error?.message ??
        detailText ??
        `Request failed with status ${response.status}`;

      // ⚠️ 401 은 generic throw **앞에서** 갈라야 한다. 예전엔 분기가 아예 없어서 만료된
      //    세션과 권한 부족이 같은 APIError 로 뭉개졌고, 호출부는 둘을 구분할 수단이
      //    없었다. 403 은 이 분기에 들어오지 않는다(=== 401 엄격 비교) — TEAM_LEADER 가
      //    admin 전용 엔드포인트에서 받는 403 은 로그인으로 튕길 일이 아니다.
      if (response.status === 401) {
        throw new UnauthorizedError(message, detailText);
      }

      throw new APIError(
        response.status,
        errorBody.error_code ?? errorBody.error?.code ?? 'UNKNOWN_ERROR',
        message,
        errorBody.error?.details ?? detailText,
      );
    }

    // 204 No Content — return undefined cast as T
    if (response.status === 204) {
      return undefined as unknown as T;
    }

    return response.json() as Promise<T>;
  }

  async get<T>(
    path: string,
    params?: Record<string, string | number | undefined>
  ): Promise<T> {
    let url = path;

    if (params) {
      const search = new URLSearchParams();
      for (const [key, value] of Object.entries(params)) {
        if (value !== undefined) {
          search.set(key, String(value));
        }
      }
      const queryString = search.toString();
      if (queryString) {
        url = `${path}?${queryString}`;
      }
    }

    return this.fetch<T>(url, { method: 'GET' });
  }

  async post<T>(path: string, body: unknown): Promise<T> {
    return this.fetch<T>(path, {
      method: 'POST',
      body: JSON.stringify(body),
    });
  }

  async put<T>(path: string, body: unknown): Promise<T> {
    return this.fetch<T>(path, {
      method: 'PUT',
      body: JSON.stringify(body),
    });
  }

  async patch<T>(path: string, body: unknown): Promise<T> {
    return this.fetch<T>(path, {
      method: 'PATCH',
      body: JSON.stringify(body),
    });
  }

  async delete<T>(path: string): Promise<T> {
    return this.fetch<T>(path, { method: 'DELETE' });
  }
}

export const adminAPI = new AdminAPIClient();
