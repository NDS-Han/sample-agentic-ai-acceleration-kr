// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { codeToHtml } from 'shiki';

/**
 * 서버 전용 코드 하이라이팅 — shiki dual theme(github-light/github-dark).
 * 생성 HTML 은 --shiki-light/--shiki-dark CSS 변수를 싣고, globals.css 의
 * .dark 셀렉터가 테마를 전환한다. 클라이언트 번들에 하이라이터를 싣지 않는다.
 */
export async function highlightCode(code: string, lang: 'bash' | 'python'): Promise<string> {
  return codeToHtml(code, {
    lang,
    themes: { light: 'github-light', dark: 'github-dark' },
    defaultColor: false,
  });
}
