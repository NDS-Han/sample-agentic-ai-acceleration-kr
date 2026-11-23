#!/usr/bin/env python3
# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""Managed Login 페이지 배경(PAGE_BACKGROUND) SVG 생성기.

확정 시안 C(분할 레이아웃) — 왼쪽 절반: 어두운 브랜드 패널(노드·엣지 그래프
애니메이션 + 영문 문구), 오른쪽 절반: 흰 바탕(폼이 놓이는 자리).

* 루트 svg 에 viewBox 를 두지 않는다 — 고유 비율이 없는 SVG 는 CSS
  background 에서 영역 전체를 채우므로 어떤 화면 비율에서도 왼쪽 패널이
  정확히 50% 가 된다. 그래프는 패널 안의 중첩 svg(slice)로, 문구는
  잘리지 않게 meet 로 따로 앵커한다.
* 애니메이션은 SMIL(animateMotion/animate) — Cognito 브랜딩 SVG 허용
  목록에 있고 script 는 쓰지 않는다. prefers-reduced-motion 이면 움직이는
  점을 숨겨 정지 그래프만 남긴다.
* 문구는 이미지에 구워지므로 언어 전환을 따르지 않는다 → 영어만 쓴다.

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


def _pulse(a, b, color, dur, begin):
    (x1, y1), (x2, y2) = NODES[a], NODES[b]
    return (f'<circle class="pulse" r="2.8" fill="{color}" opacity="0">'
            f'<animateMotion path="M{x1} {y1} L{x2} {y2}" dur="{dur}s" '
            f'begin="{begin}s" repeatCount="indefinite"/>'
            f'<animate attributeName="opacity" values="0;1;1;0" '
            f'keyTimes="0;0.12;0.85;1" dur="{dur}s" begin="{begin}s" '
            f'repeatCount="indefinite"/></circle>')


def graph() -> str:
    el = [_line(a, b, "#262626", 1) for a, b in MESH]
    el += [_line(c, "hub", "#2b3332", 1.2) for c in CLIENTS]
    el += [_line("hub", m, "#24504b", 1.2) for m in MODELS]
    el += [_pulse(c, "hub", "#5eead4", round(3.2 + 0.4 * (i % 3), 1), round(i * 0.7, 1))
           for i, c in enumerate(CLIENTS)]
    el += [_pulse("hub", m, "#2dd4bf", round(2.2 + 0.3 * i, 1), round(0.9 + i * 1.1, 1))
           for i, m in enumerate(MODELS)]
    for k, (x, y) in NODES.items():
        if k == "hub":
            continue
        stroke = "#2f6e67" if k.startswith("m") else "#3a3a3a"
        tw = (f'<animate class="twinkle" attributeName="opacity" values="1;0.45;1" '
              f'dur="{4 + len(k)}s" repeatCount="indefinite"/>') if k in TWINKLE else ""
        el.append(f'<circle cx="{x}" cy="{y}" r="4" fill="#1c1c1c" '
                  f'stroke="{stroke}" stroke-width="1.4">{tw}</circle>')
    hx, hy = NODES["hub"]
    el.append(f'<circle class="pulse" cx="{hx}" cy="{hy}" r="10" fill="none" '
              f'stroke="#2dd4bf" stroke-width="1" opacity="0">'
              f'<animate attributeName="r" values="9;36" dur="2.8s" repeatCount="indefinite"/>'
              f'<animate attributeName="opacity" values="0.55;0" dur="2.8s" '
              f'repeatCount="indefinite"/></circle>')
    el.append(f'<circle cx="{hx}" cy="{hy}" r="8" fill="#0f766e" '
              f'stroke="#5eead4" stroke-width="1.6"/>')
    return "".join(el)


def build() -> str:
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="100%" height="100%">'
        '<style>@media (prefers-reduced-motion: reduce){.pulse{display:none}}</style>'
        '<rect width="100%" height="100%" fill="#ffffff"/>'
        f'<rect width="50%" height="100%" fill="{PANEL}"/>'
        # 그래프 — 패널을 꽉 채우고 넘치는 부분은 잘라낸다
        '<svg x="0" y="0" width="50%" height="100%" viewBox="0 0 640 800" '
        f'preserveAspectRatio="xMidYMid slice">{graph()}</svg>'
        # 문구 — 잘리지 않게 왼쪽 가운데 앵커
        '<svg x="0" y="0" width="50%" height="100%" viewBox="0 0 640 800" '
        'preserveAspectRatio="xMinYMid meet">'
        f'<text x="56" y="372" fill="#ffffff" font-family="{FONT}" '
        f'font-size="40" font-weight="700" letter-spacing="-0.8">{HEADING[0]}</text>'
        f'<text x="56" y="420" fill="#ffffff" font-family="{FONT}" '
        f'font-size="40" font-weight="700" letter-spacing="-0.8">{HEADING[1]}</text>'
        f'<text x="56" y="462" fill="#b4b4b4" font-family="{FONT}" '
        f'font-size="16">{SUBTITLE}</text></svg>'
        # 하단 문구 — 왼쪽 아래 앵커
        '<svg x="0" y="0" width="50%" height="100%" viewBox="0 0 640 800" '
        'preserveAspectRatio="xMinYMax meet">'
        f'<text x="56" y="752" fill="#a1a1a1" font-family="{FONT}" '
        f'font-size="12">{FOOTER}</text></svg>'
        '</svg>\n'
    )


if __name__ == "__main__":
    out = Path(__file__).with_name("background.svg")
    out.write_text(build(), encoding="utf-8")
    print(f"wrote {out} ({out.stat().st_size} bytes)")
