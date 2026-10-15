// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

'use client';

import { useCallback, useRef, useState } from 'react';
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

/**
 * SSE 수신 hook. AgentCore 가 실제로 어떤 이벤트 형태를 보내는지에 따라 파싱
 * 룰이 달라질 수 있어 단순/관대하게 구현. event:/data: 표준 SSE 만 처리.
 */
export function useChatStream({ onEvent, onError }: ChatStreamOptions) {
  const [isStreaming, setIsStreaming] = useState(false);
  const abortRef = useRef<AbortController | null>(null);

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
            parseSseBlock(block, onEvent);
          }
          if (buffer.length > MAX_SSE_BUFFER_CHARS) {
            // 잘린 프레임이 무한 누적 — 상류/메모리를 닫고 에러로 끝낸다.
            await reader.cancel().catch(() => {});
            throw new Error('SSE frame exceeded size limit — malformed stream');
          }
        }
        // tail flush — 스트림 종료 시 디코더에 남은 partial UTF-8 도 토해낸다.
        buffer += decoder.decode();
        if (buffer.trim()) parseSseBlock(buffer, onEvent);
      } catch (e) {
        // SessionExpiredError 는 호출부가 재시도 판단해야 하므로 재전파 —
        //        onError 로 뭉개면 새 세션 생성 경로가 안 탄다.
        if (e instanceof SessionExpiredError) throw e;
        if ((e as Error).name !== 'AbortError') {
          onError?.(e as Error);
        }
      } finally {
        setIsStreaming(false);
        abortRef.current = null;
      }
    },
    [onEvent, onError]
  );

  const cancel = useCallback(() => {
    abortRef.current?.abort();
  }, []);

  // 진행 중 분석 재구독(§56 핸드오프) — 다른 메뉴에 다녀온 뒤 서버가 background
  // 로 계속 돌리던 스트림을 GET /stream 으로 이어받는다(이미 발행분 재생+실시간).
  // 404(활성 스트림 없음 — 완료/만료)는 정상: history 가 이미 결과를 보여줌.
  const reattach = useCallback(
    async (sessionId: string) => {
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
        if (response.status === 404) return; // 진행 중 분석 없음 — 무시
        // 401 — 세션 사망. send() 와 같이 로그인으로 보낸다(R17 — 과거엔 조용히
        // return 해서 스트림 부재 원인이 안 보였다).
        if (redirectToLoginIfUnauthorized(response)) return;
        if (!response.ok || !response.body) return;
        setIsStreaming(true);
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
            parseSseBlock(block, onEvent);
          }
          if (buffer.length > MAX_SSE_BUFFER_CHARS) {
            // reattach 는 조용히 포기하되 상류 연결은 반드시 닫는다.
            await reader.cancel().catch(() => {});
            return;
          }
        }
        // tail flush — send() 와 동일하게 마지막 미완결 블록도 처리.
        buffer += decoder.decode();
        if (buffer.trim()) parseSseBlock(buffer, onEvent);
      } catch {
        // 재구독 실패는 무해(완료분은 history 로 복원됨)
      } finally {
        setIsStreaming(false);
        abortRef.current = null;
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
