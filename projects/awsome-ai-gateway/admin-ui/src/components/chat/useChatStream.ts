// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

'use client';

import { useCallback, useRef, useState } from 'react';
import type { StreamEvent } from './types';
import { redirectToLoginIfUnauthorized } from '@/lib/utils/unauthorized';

// admin-ui server-side proxy 경유 (api.ts 와 동일). SSE 스트림도 pass-through.
const API_BASE = '/api/chat-proxy';

export interface ChatStreamOptions {
  onEvent: (event: StreamEvent) => void;
  onError?: (error: Error) => void;
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

          // SSE 메시지 = '\n\n' 구분
          let idx;
          while ((idx = buffer.indexOf('\n\n')) >= 0) {
            const block = buffer.slice(0, idx);
            buffer = buffer.slice(idx + 2);
            parseSseBlock(block, onEvent);
          }
        }
        // tail flush
        if (buffer.trim()) parseSseBlock(buffer, onEvent);
      } catch (e) {
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
        }
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
  let data = '';
  for (const line of block.split('\n')) {
    if (line.startsWith('event:')) event = line.slice(6).trim();
    else if (line.startsWith('data:')) data += line.slice(5).trim();
  }
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
