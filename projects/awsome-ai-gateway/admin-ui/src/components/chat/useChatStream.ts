// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import type { StreamEvent } from './types';
import { redirectToLoginIfUnauthorized } from '@/lib/utils/unauthorized';

// admin-ui server-side proxy 경유 (api.ts 와 동일). SSE 스트림도 pass-through.
const API_BASE = '/api/chat-proxy';

// '\n\n' 프레임 경계가 영원히 안 오면 버퍼가 무한 커진다 — 악의적/버그 상류의
// 메모리 공격을 막기 위해 상한을 두고 초과 시 스트림을 에러로 끝낸다.
// 정상 프레임(리포트 JSON 포함)은 수백 KB 이하라 4 MiB 는 충분히 크다.
// UTF-16 code unit 수 기준 상한(정확한 바이트 아님) — 목적은 메모리 상한이라
// 코드유닛 카운트로 충분하다. 이름에 BYTES 를 쓰지 않는다(claude R17 nit).
const MAX_SSE_BUFFER_CHARS = 4 * 1024 * 1024;

export interface ChatStreamOptions {
  onEvent: (event: StreamEvent) => void;
  onError?: (error: Error) => void;
}

/** 세션 TTL 만료(410) — 호출부가 새 세션을 만들어 재시도할 수 있게 구분한다. */
export class SessionExpiredError extends Error {
  constructor() {
    super('Chat session expired');
    this.name = 'SessionExpiredError';
  }
}

/** 이 세션에 분석이 진행 중(409) — Stop 후 재전송하라는 의미. 원문 `HTTP 409: {json}`
 *  을 채팅 에러로 노출하지 않게 typed error 로 구분한다(R2-N2). */
export class AnalysisInProgressError extends Error {
  constructor() {
    super('Analysis already in progress');
    this.name = 'AnalysisInProgressError';
  }
}

/** reattach 의 결과 — 'no-stream' 은 이 pod 에 릴레이가 없다는 뜻(멀티 레플리카
 *  환경에서 다른 pod 가 소유 중이거나 분석 완료). 호출부는 history 폴링으로
 *  폴백해야 한다(R25-S10 — 예전엔 조용히 무시해 진행 중 답변이 증발했다). */
export type ReattachResult = 'attached' | 'no-stream' | 'unauthorized' | 'failed';

/**
 * SSE 수신 hook. AgentCore 가 실제로 어떤 이벤트 형태를 보내는지에 따라 파싱
 * 룰이 달라질 수 있어 단순/관대하게 구현. event:/data: 표준 SSE 만 처리.
 */
export function useChatStream({ onEvent, onError }: ChatStreamOptions) {
  const [isStreaming, setIsStreaming] = useState(false);
  const abortRef = useRef<AbortController | null>(null);

  // N-C3 — 언마운트(/chat 이탈) 시 진행 중 SSE fetch 를 반드시 끊는다.
  // 안 끊으면 복귀할 때마다 reattach 연결이 누적되고, dev(HTTP/1.1) 에선
  // 브라우저 per-host 6연결 고갈로 UI 가 정지할 수 있다.
  useEffect(() => {
    return () => {
      abortRef.current?.abort();
      abortRef.current = null;
    };
  }, []);

  const send = useCallback(
    async (
      sessionId: string,
      content: string,
      screenContext?: unknown,
      mode: 'quick' | 'deep' = 'quick'
    ) => {
      abortRef.current?.abort();
      const controller = new AbortController();
      abortRef.current = controller;
      setIsStreaming(true);

      // N-C7 — done 프레임 없이 EOF 가 오면 "완결" 로 보이는 함정을 막는다.
      let sawDone = false;
      const trackDone = (e: StreamEvent) => {
        if (e.type === 'done') sawDone = true;
        onEvent(e);
      };

      try {
        const payload: Record<string, unknown> = { content, mode };
        if (screenContext) payload.screen_context = screenContext;
        const localeCookie = document.cookie.match(/(?:^|; )locale=([^;]*)/);
        payload.language = localeCookie?.[1] === 'en' ? 'en' : 'ko';
        const response = await fetch(
          `${API_BASE}/admin/chat/sessions/${sessionId}/messages`,
          {
            method: 'POST',
            credentials: 'include',
            headers: {
              'Content-Type': 'application/json',
              Accept: 'text/event-stream',
            },
            body: JSON.stringify(payload),
            signal: controller.signal,
          }
        );

        // ⚠️ 401 은 세션이 죽은 것이라 스트림을 기다릴 이유가 없다. 아래 generic 분기로
        //    내려보내면 onError 가 `HTTP 401: {...}` 를 채팅창에 흘리는데, 사용자가 할 수
        //    있는 행동(재로그인)이 전혀 안내되지 않는다. 로그인으로 보내고 조용히 끝낸다.
        //    (finally 가 남아 있으므로 isStreaming 은 정상적으로 풀린다.)
        if (redirectToLoginIfUnauthorized(response)) {
          return;
        }

        // 410 Gone — 세션 TTL 만료(R17). 일반 HTTP 에러로 흘리면 사용자는
        //    "HTTP 410" 만 보고 재시도 방법을 모른다. 호출부(ChatLayout)가 새
        //    세션을 만들어 같은 메시지를 재전송하도록 typed error 로 던진다.
        if (response.status === 410) {
          throw new SessionExpiredError();
        }

        // 409 — 진행 중 분석(Stop 직후 재전송·더블클릭·크로스파드 활성 클레임).
        // 원문 노출 대신 typed error — 호출부가 i18n 메시지로 바꾼다(R2-N2).
        if (response.status === 409) {
          throw new AnalysisInProgressError();
        }

        if (!response.ok || !response.body) {
          throw new Error(`HTTP ${response.status}: ${await response.text()}`);
        }

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });

          // SSE 메시지 = '\n\n' 구분 — 완결 프레임을 먼저 다 뽑고 남은 꼬리만
          // 상한 검사한다(한 read 에 프레임 여러 개가 와도 오탐 없이).
          let idx;
          while ((idx = buffer.indexOf('\n\n')) >= 0) {
            const block = buffer.slice(0, idx);
            buffer = buffer.slice(idx + 2);
            parseSseBlock(block, trackDone);
          }
          if (buffer.length > MAX_SSE_BUFFER_CHARS) {
            // 잘린 프레임이 무한 누적 — 상류/메모리를 닫고 에러로 끝낸다.
            await reader.cancel().catch(() => {});
            throw new Error('SSE frame exceeded size limit — malformed stream');
          }
        }
        // tail flush — 스트림 종료 시 디코더에 남은 partial UTF-8 도 토해낸다.
        buffer += decoder.decode();
        if (buffer.trim()) parseSseBlock(buffer, trackDone);
        if (!sawDone) {
          // done 없는 EOF — 프록시/pod 종료로 끊긴 미완성 종료. pending 버블을
          // 조용히 '완료' 로 두면 잘린 답변이 정상 답변처럼 보인다(R25-N-C7).
          onEvent({
            type: 'error',
            error: '스트림이 완료 신호 없이 끊겼습니다',
            error_type: 'StreamTruncated',
          } as StreamEvent);
        }
      } catch (e) {
        // SessionExpiredError/AnalysisInProgressError 는 호출부가 재시도·안내
        // 판단해야 하므로 재전파 — onError 로 뭉개면 typed 처리가 안 탄다.
        if (e instanceof SessionExpiredError) throw e;
        if (e instanceof AnalysisInProgressError) throw e;
        if ((e as Error).name !== 'AbortError') {
          onError?.(e as Error);
        }
      } finally {
        // N-C1 — stale cleanup 방어: 이 호출이 시작한 뒤 새 스트림(send/reattach)이
        //    abortRef 를 교체했다면, 내 finally 가 새 스트림의 상태를 지우면 안 된다.
        //    예전엔 무조건 덮어써서 reattach 대기 중 전송하면 Stop 버튼이 사라졌다.
        if (abortRef.current === controller) {
          setIsStreaming(false);
          abortRef.current = null;
        }
      }
    },
    [onEvent, onError]
  );

  // S12 — Stop 은 서버 측 분석도 멈춰야 한다. 브라우저 스트림만 닫으면
  //  AgentCore 가 토큰을 끝까지 소비하고 다음 질문이 그 뒤에 줄을 선다.
  //  소유자 검증된 POST /cancel 로 producer 를 취소(부분 답변은 "[취소됨]"으로 영속화).
  // R2-N2 — fire-and-forget 이면 Stop 직후 재전송이 취소 반영 전에 도착해 409 가
  //  난다. 호출부가 await 할 수 있게 Promise 를 돌려준다(실패해도 로컬 abort 는
  //  이미 됐으므로 에러는 삼키고 로그만 남긴다).
  const cancel = useCallback(async (sessionId?: string) => {
    abortRef.current?.abort();
    if (!sessionId) return;
    try {
      await fetch(`${API_BASE}/admin/chat/sessions/${sessionId}/cancel`, {
        method: 'POST',
        credentials: 'include',
      });
    } catch (e) {
      console.warn('[chat] cancel request failed:', e);
    }
  }, []);

  // 진행 중 분석 재구독(§56 핸드오프) — 다른 메뉴에 다녀온 뒤 서버가 background
  // 로 계속 돌리던 스트림을 GET /stream 으로 이어받는다(이미 발행분 재생+실시간).
  // 404 는 **두 가지**다: (a) 분석 완료/없음 — history 가 결과를 보여줌,
  // (b) 다른 pod 에서 실행 중(멀티 레플리카 — 릴레이는 프로세스 로컬). 호출부가
  // 후자를 구분해 history 폴링으로 폴백할 수 있게 결과를 반환한다(R25-S10).
  const reattach = useCallback(
    async (sessionId: string): Promise<ReattachResult> => {
      abortRef.current?.abort();
      const controller = new AbortController();
      abortRef.current = controller;
      try {
        const response = await fetch(
          `${API_BASE}/admin/chat/sessions/${sessionId}/stream`,
          {
            method: 'GET',
            credentials: 'include',
            headers: { Accept: 'text/event-stream' },
            signal: controller.signal,
          }
        );
        if (response.status === 404) return 'no-stream'; // 이 pod 에 활성 스트림 없음
        // 401 — 세션 사망. send() 와 같이 로그인으로 보낸다(R17 — 과거엔 조용히
        // return 해서 스트림 부재 원인이 안 보였다).
        if (redirectToLoginIfUnauthorized(response)) return 'unauthorized';
        if (!response.ok || !response.body) return 'failed';
        setIsStreaming(true);

        let sawDone = false;
        const trackDone = (e: StreamEvent) => {
          if (e.type === 'done') sawDone = true;
          onEvent(e);
        };

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          let idx;
          while ((idx = buffer.indexOf('\n\n')) >= 0) {
            const block = buffer.slice(0, idx);
            buffer = buffer.slice(idx + 2);
            parseSseBlock(block, trackDone);
          }
          if (buffer.length > MAX_SSE_BUFFER_CHARS) {
            // reattach 는 조용히 포기하되 상류 연결은 반드시 닫는다.
            await reader.cancel().catch(() => {});
            return 'failed';
          }
        }
        // tail flush — send() 와 동일하게 마지막 미완결 블록도 처리.
        buffer += decoder.decode();
        if (buffer.trim()) parseSseBlock(buffer, trackDone);
        if (!sawDone) {
          // done 없는 EOF — 재구독 스트림도 중간 절단될 수 있다. 스피너가
          // 영구 회전하지 않게 미완성 상태를 명시한다(R25-N-C7).
          onEvent({
            type: 'error',
            error: '스트림이 완료 신호 없이 끊겼습니다',
            error_type: 'StreamTruncated',
          } as StreamEvent);
        }
        return 'attached';
      } catch {
        // 재구독 실패는 무해(완료분은 history 로 복원됨)
        return 'failed';
      } finally {
        // N-C1 — stale cleanup 방어(send 와 같은 이유).
        if (abortRef.current === controller) {
          setIsStreaming(false);
          abortRef.current = null;
        }
      }
    },
    [onEvent]
  );

  return { send, cancel, reattach, isStreaming };
}

/**
 * SSE 블록 하나(`event:` + `data:`) → StreamEvent.
 *
 * ⚠️ **이벤트 이름(`event:`) 이 payload 의 `type` 보다 우선한다.** 예전엔
 * `{ type: event, ...parsed }` 로 합쳐서 payload 의 `type` 이 이벤트 이름을 덮어썼다.
 * admin-api 의 에러 프레임 본문이 `{"error": …, "type": "ClientError"}` 였기 때문에
 * 최종 이벤트가 `type: 'ClientError'` 가 되어, ChatLayout 의 `case 'error'` 가 영원히
 * 잡히지 않고 `default` 로 **조용히 버려졌다** — AgentCore 호출이 실패해도 사용자에게는
 * 아무 메시지 없이 pending 스피너만 계속 돌았다. (백엔드는 본문 키를 `error_type` 으로
 * 바꿨고, 여기서도 구조적으로 덮이지 않게 못박는다 — 이중 방어.)
 *
 * `event:` 줄이 없는 프레임(AgentCore 직결 형태)은 payload 의 `type` 을 그대로 쓴다.
 */
export function parseSseBlock(block: string, onEvent: (e: StreamEvent) => void) {
  let event = ''; // '' = event: 줄 없음
  // SSE 스펙: data: 줄이 여러 개면 '\n' 으로 이어 붙인다. 예전엔 trim 후 그냥
  // 연결해 멀티라인 payload(행이 JSON 문자열 중간에서 끊긴 경우 포함)가
  // 깨졌다. 앞의 선택적 공백 하나만 떼고(스펙) 줄바꿈으로 join 한다.
  const dataParts: string[] = [];
  for (const line of block.split('\n')) {
    if (line.startsWith('event:')) event = line.slice(6).trim();
    else if (line.startsWith('data:')) {
      const v = line.slice(5);
      dataParts.push(v.startsWith(' ') ? v.slice(1) : v);
    }
  }
  const data = dataParts.join('\n');
  if (!data) return;
  let parsed: unknown;
  try {
    parsed = JSON.parse(data);
  } catch {
    onEvent({ type: 'text', chunk: data } as StreamEvent);
    return;
  }
  // 문자열/숫자 등 비객체 JSON 은 스프레드하면 문자 인덱스로 흩어진다
  // (`{...'hi'}` → `{0:'h',1:'i'}`) — 텍스트로 취급한다.
  if (typeof parsed !== 'object' || parsed === null) {
    onEvent({ type: 'text', chunk: String(parsed) } as StreamEvent);
    return;
  }
  const body = parsed as Record<string, unknown>;
  const type = event || (typeof body.type === 'string' ? body.type : 'message');
  onEvent({ ...body, type } as StreamEvent);
}
