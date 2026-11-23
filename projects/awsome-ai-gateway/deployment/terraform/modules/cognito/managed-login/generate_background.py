#!/usr/bin/env python3
# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""Managed Login 페이지 배경(PAGE_BACKGROUND) SVG 생성기.

확정 시안 C(분할 레이아웃) — 왼쪽 절반: 어두운 브랜드 패널(노드·엣지 그래프
애니메이션 + 영문 문구), 오른쪽 절반: 흰 바탕(폼이 놓이는 자리).

* 루트 svg 에 viewBox 를 두지 않는다 — 고유 비율이 없는 SVG 는 CSS
  background 에서 영역 전체를 채우므로 어떤 화면 비율에서도 왼쪽 패널이
  정확히 50% 가 된다. 그래프는 패널 안의 중첩 svg(slice)로, 문구는
  잘리지 않게 meet 로 따로 앵커한다.
* Cognito Managed Login 에셋 검증은 SVG 의 animate/animateMotion 요소를
  거부한다 — 트래픽 점을 엣지 위에 정적으로 배치해 흐름만 남긴다
  (style/script 모두 사용하지 않는다).
* 문구는 이미지에 구워지므로 언어 전환을 따르지 않는다 → 영어만 쓴다.
* 폼 정렬 — Cognito 폼(categories.form.location.horizontal=END)은 너비
  402px 카드에 오른쪽 여백 81px 로 놓인다(2026-11 실측). 패널을 반반으로
  자르면 폼이 흰 영역의 오른쪽으로 치우치므로, 흰 영역을 폼 카드 + 양옆
  여백(81px)인 FORM_COLUMN_PX 폭으로 두고 나머지를 패널로 채운다 —
  CSS width: calc() (SVG2 기하 속성). CSS 를 무시하는 렌더러에서는 width
  속성(50%)으로 떨어진다.
* 좁은 화면(NARROW_PX 미만) — 반반 경계에 폼 카드가 걸치므로, 패널이 화면
  전체를 덮고 문구는 숨겨 어두운 그래프 위에 흰 폼 카드만 뜨게 한다.
  (media query 는 SVG 뷰포트 = 배경 영역 = 페이지 너비 기준)

재생성: python3 generate_background.py  (background.svg 를 덮어쓴다)
"""
from pathlib import Path

HEADING = ("LLM Gateway", "Dashboard")
SUBTITLE = "Usage, budgets and API keys — all in one place."
FOOTER = "Internal use only"

PANEL = "#171717"
FONT = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif"

NODES = {
    "c1": (80, 120), "c2": (210, 70), "c3": (330, 175),
    "c4": (110, 610), "c5": (250, 705), "c6": (385, 625),
    "hub": (470, 420),
    "m1": (590, 205), "m2": (605, 420), "m3": (585, 640),
    "p1": (515, 75), "p2": (530, 760),
}
MESH = [("c1", "c2"), ("c2", "c3"), ("c4", "c5"), ("c5", "c6"), ("m1", "p1"),
        ("m3", "p2"), ("c3", "p1"), ("c6", "p2"), ("m1", "m2"), ("m2", "m3")]
CLIENTS = ["c1", "c2", "c3", "c4", "c5", "c6"]
MODELS = ["m1", "m2", "m3"]
TWINKLE = {"c2", "c5", "m2", "p1"}


def _line(a, b, stroke, width):
    (x1, y1), (x2, y2) = NODES[a], NODES[b]
    return (f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" '
            f'stroke="{stroke}" stroke-width="{width}"/>')


def _packet(a, b, color, t):
    """엣지 위 t(0~1) 지점의 정지 트래픽 점 — 애니메이션의 한 프레임."""
    (x1, y1), (x2, y2) = NODES[a], NODES[b]
    return (f'<circle cx="{x1 + (x2 - x1) * t:.1f}" cy="{y1 + (y2 - y1) * t:.1f}" '
            f'r="2.8" fill="{color}"/>')


def graph() -> str:
    el = [_line(a, b, "#262626", 1) for a, b in MESH]
    el += [_line(c, "hub", "#2b3332", 1.2) for c in CLIENTS]
    el += [_line("hub", m, "#24504b", 1.2) for m in MODELS]
    # client→hub·hub→model 방향으로 엇갈린 위치의 정지 트래픽 점들
    el += [_packet(c, "hub", "#5eead4", 0.2 + 0.25 * (i % 3))
           for i, c in enumerate(CLIENTS)]
    el += [_packet("hub", m, "#2dd4bf", 0.35 + 0.3 * i)
           for i, m in enumerate(MODELS)]
    for k, (x, y) in NODES.items():
        if k == "hub":
            continue
        stroke = "#2f6e67" if k.startswith("m") else "#3a3a3a"
        # twinkle 노드는 살짝 밝은 채움으로 구분
        fill = "#2a4a44" if k in TWINKLE else "#1c1c1c"
        el.append(f'<circle cx="{x}" cy="{y}" r="4" fill="{fill}" '
                  f'stroke="{stroke}" stroke-width="1.4"/>')
    hx, hy = NODES["hub"]
    el.append(f'<circle cx="{hx}" cy="{hy}" r="22" fill="none" '
              f'stroke="#2dd4bf" stroke-width="1" opacity="0.18"/>')
    el.append(f'<circle cx="{hx}" cy="{hy}" r="8" fill="#0f766e" '
              f'stroke="#5eead4" stroke-width="1.6"/>')
    return "".join(el)


# 폼 카드 402px + 양옆 81px — Cognito main-layout 실측값(위 docstring)
FORM_COLUMN_PX = 402 + 81 * 2
# 이보다 좁으면 패널 문구가 폼 카드와 겹친다 — 전체 어두운 배경으로 전환
NARROW_PX = 1024

STYLE = (
    f".panel{{width:calc(100% - {FORM_COLUMN_PX}px)}}"
    f"@media (max-width:{NARROW_PX - 1}px){{.panel{{width:100%}}"
    ".copy{display:none}}"
)


def build() -> str:
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="100%" height="100%">'
        f'<style>{STYLE}</style>'
        '<rect width="100%" height="100%" fill="#ffffff"/>'
        f'<rect class="panel" width="50%" height="100%" fill="{PANEL}"/>'
        # 그래프 — 패널을 꽉 채우고 넘치는 부분은 잘라낸다
        '<svg class="panel" x="0" y="0" width="50%" height="100%" viewBox="0 0 640 800" '
        f'preserveAspectRatio="xMidYMid slice">{graph()}</svg>'
        # 문구 — 잘리지 않게 왼쪽 가운데 앵커
        '<svg class="panel copy" x="0" y="0" width="50%" height="100%" viewBox="0 0 640 800" '
        'preserveAspectRatio="xMinYMid meet">'
        f'<text x="56" y="372" fill="#ffffff" font-family="{FONT}" '
        f'font-size="40" font-weight="700" letter-spacing="-0.8">{HEADING[0]}</text>'
        f'<text x="56" y="420" fill="#ffffff" font-family="{FONT}" '
        f'font-size="40" font-weight="700" letter-spacing="-0.8">{HEADING[1]}</text>'
        f'<text x="56" y="462" fill="#b4b4b4" font-family="{FONT}" '
        f'font-size="16">{SUBTITLE}</text></svg>'
        # 하단 문구 — 왼쪽 아래 앵커
        '<svg class="panel copy" x="0" y="0" width="50%" height="100%" viewBox="0 0 640 800" '
        'preserveAspectRatio="xMinYMax meet">'
        f'<text x="56" y="752" fill="#a1a1a1" font-family="{FONT}" '
        f'font-size="12">{FOOTER}</text></svg>'
        '</svg>\n'
    )


if __name__ == "__main__":
    out = Path(__file__).with_name("background.svg")
    out.write_text(build(), encoding="utf-8")
    print(f"wrote {out} ({out.stat().st_size} bytes)")
