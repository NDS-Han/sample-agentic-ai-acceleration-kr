// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * BrandLogo — LLM Gateway 마크의 단일 출처(§60). Sidebar 와 favicon(app/icon.svg)이
 * 같은 path 를 쓰도록 해 디자인 드리프트를 막는다.
 *
 * 현재 마크: "그래프 노드" — 세 노드와 그 사이 엣지. 로그인 페이지(Managed Login)
 * 배경의 노드·엣지 그래프와 같은 모티프.
 * 색은 테마를 따른다: 라이트는 진한 teal(흰 배경 대비), 다크는 밝은 teal.
 * app/icon.svg 는 같은 도형을 prefers-color-scheme 로 칠한다(탭 바는 앱 테마가
 * 아니라 브라우저/OS 테마를 따르므로).
 * 보관용 이전 마크: brand/ghost-gradient.svg, brand/aperture-*.svg.
 */

interface BrandLogoProps {
  /** 마크 한 변(px). */
  size?: number;
  className?: string;
}

export function BrandLogo({ size = 24, className }: BrandLogoProps) {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      width={size}
      height={size}
      viewBox="0 0 32 32"
      aria-hidden="true"
      className={className}
    >
      <path
        d="M9 21 L22 10 L24 23 Z"
        fill="none"
        strokeWidth={1.8}
        strokeLinejoin="round"
        className="stroke-[#0f766e] dark:stroke-[#2dd4bf]"
      />
      <circle cx="9" cy="21" r="3.4" className="fill-[#0f766e] dark:fill-[#2dd4bf]" />
      <circle cx="22" cy="10" r="3.4" className="fill-[#0d9488] dark:fill-[#5eead4]" />
      <circle cx="24" cy="23" r="2.8" className="fill-[#14b8a6] dark:fill-[#14b8a6]" />
    </svg>
  );
}
