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
* 그래프 — 시드 고정 무작위 배치(N_NODES 개, 최소 간격 MIN_GAP)의 성좌 메시 +
  게이트웨이 허브. 허브로 들어오는 엣지(N_CLIENTS)·나가는 엣지(N_MODELS)에
  트래픽 점이 흐른다. 문구 영역(COPY_ZONE)에는 노드를 두지 않는다.
* 움직임 — SMIL(animate/animateMotion)은 검증에서 거부되므로 <style> 의 CSS
  @keyframes 로만 만든다: 트래픽 점은 엣지를 따라 translate, 허브 링은
  퍼지며 사라지고, 일부 노드는 깜빡인다. 각 요소의 정지 위치(cx/cy)는
  애니메이션 중간 프레임이라 CSS 를 무시하는 렌더러에서는 정지 그래프로
  보인다. prefers-reduced-motion 이면 애니메이션을 끈다.
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

# ── 그래프 ─────────────────────────────────────────────────────────────────
# 패널 비율이 넓어(폼 칼럼을 뺀 나머지 전부) 가로로 긴 좌표계를 쓴다 — slice 로
# 채우므로 넓은 화면에선 위아래가, 좁은 화면에선 좌우가 잘린다.
# 그래프는 페이지 전체 너비의 좌표계(xMinYMid slice)로 그리고 패널 모양으로
# 잘라낸다(clipPath) — 중첩 svg 의 CSS width(calc)는 Chrome 이 무시해 패널과
# 그래프 폭이 어긋났다(넓은 화면: 패널 오른쪽이 빔, 중간 폭: 흰 영역으로 삐져나옴).
GRAPH_W, GRAPH_H = 1600, 800
# 게이트웨이 — 문구(왼쪽 가운데) 아래·오른쪽. 높이가 배율을 정하는 중간 폭
# (1024~1280px)에서도 패널 안쪽에 남도록 왼쪽으로 당겨 둔다.
HUB = (540, 560)
SEED = 7                    # 고정 시드 — 재생성해도 같은 그림(커밋 diff 안정)
N_NODES = 110               # 배경 성좌 노드 수
MIN_GAP = 62                # 노드 간 최소 거리
N_CLIENTS = 12              # 허브로 들어오는 엣지(트래픽 점이 흐름)
N_MODELS = 6                # 허브에서 나가는 엣지
# 문구가 놓이는 영역(그래프 좌표 근사) — 노드를 두지 않아 글자가 묻히지 않게
COPY_ZONE = (0, 280, 480, 520)
MODEL_X_MAX = HUB[0] + 280  # 모델 노드는 중간 폭 화면에서도 보이는 범위


def _layout():
    """시드 고정 포아송 디스크 근사 — 고르게 흩어진 노드 + 근접 이웃 엣지."""
    import math
    import random
    rnd = random.Random(SEED)
    pts = []
    tries = 0
    while len(pts) < N_NODES and tries < 20000:
        tries += 1
        x, y = rnd.uniform(24, GRAPH_W - 24), rnd.uniform(24, GRAPH_H - 24)
        zx0, zy0, zx1, zy1 = COPY_ZONE
        if zx0 <= x <= zx1 and zy0 <= y <= zy1:
            continue
        if math.dist((x, y), HUB) < 90:
            continue
        if all(math.dist((x, y), q) >= MIN_GAP for q in pts):
            pts.append((round(x), round(y)))
    # 근접 이웃 2개씩 — 성좌 메시
    mesh = set()
    for i, p in enumerate(pts):
        near = sorted(range(len(pts)), key=lambda j: math.dist(p, pts[j]))[1:3]
        for j in near:
            mesh.add((min(i, j), max(i, j)))
    left = sorted((i for i, p in enumerate(pts) if p[0] < HUB[0] - 120),
                  key=lambda i: math.dist(pts[i], HUB))
    right = sorted((i for i, p in enumerate(pts)
                    if HUB[0] + 60 < p[0] < MODEL_X_MAX),
                   key=lambda i: math.dist(pts[i], HUB))
    # 허브에 가까운 왼쪽 노드들 — 트래픽 점이 엇갈리도록 각도 순 정렬
    clients = sorted(left[:N_CLIENTS],
                     key=lambda i: math.atan2(pts[i][1] - HUB[1], pts[i][0] - HUB[0]))
    models = right[:N_MODELS]
    twinkle = set(rnd.sample(range(len(pts)), 10))
    return pts, sorted(mesh), clients, models, twinkle


def _line(p, q, stroke, width):
    return (f'<line x1="{p[0]}" y1="{p[1]}" x2="{q[0]}" y2="{q[1]}" '
            f'stroke="{stroke}" stroke-width="{width}"/>')


PACKET_KEYFRAMES: list[str] = []


def _packet(p, q, color, t, dur):
    """엣지 p→q 위 t(0~1) 지점의 트래픽 점 — 정지 위치가 곧 애니메이션의 한 프레임.

    CSS 로 시작 노드(-t)에서 끝 노드(1-t)까지 translate 한다. 음수 delay 로
    로드 순간부터 t 지점을 지나는 중인 것처럼 보이게 해 점들이 엇갈린다."""
    (x1, y1), (x2, y2) = p, q
    dx, dy = x2 - x1, y2 - y1
    n = len(PACKET_KEYFRAMES)
    PACKET_KEYFRAMES.append(
        f"@keyframes pk{n}{{"
        f"0%{{transform:translate({-dx * t:.1f}px,{-dy * t:.1f}px);opacity:0}}"
        "12%{opacity:1}85%{opacity:1}"
        f"100%{{transform:translate({dx * (1 - t):.1f}px,{dy * (1 - t):.1f}px);opacity:0}}}}"
        f".pk{n}{{animation:pk{n} {dur:.1f}s linear {-dur * t:.2f}s infinite}}")
    return (f'<circle class="pk{n}" cx="{x1 + dx * t:.1f}" cy="{y1 + dy * t:.1f}" '
            f'r="2.6" fill="{color}"/>')


def graph() -> str:
    pts, mesh, clients, models, twinkle = _layout()
    PACKET_KEYFRAMES.clear()
    el = [_line(pts[i], pts[j], "#252525", 1) for i, j in mesh]
    el += [_line(pts[c], HUB, "#2b3735", 1.1) for c in clients]
    el += [_line(HUB, pts[m], "#24504b", 1.2) for m in models]
    for k, c in enumerate(clients):
        el.append(_packet(pts[c], HUB, "#5eead4", (0.15 + 0.29 * k) % 1,
                          2.8 + 0.35 * (k % 4)))
    for k, m in enumerate(models):
        el.append(_packet(HUB, pts[m], "#2dd4bf", (0.3 + 0.37 * k) % 1,
                          2.0 + 0.3 * (k % 3)))
    hub_linked = set(clients) | set(models)
    for i, (x, y) in enumerate(pts):
        if i in models:
            stroke, fill, r = "#2f6e67", "#1c2624", 4.5
        elif i in hub_linked:
            stroke, fill, r = "#3d4a48", "#1c1c1c", 4
        else:
            stroke, fill, r = "#333333", "#1a1a1a", 3
        cls = ' class="tw"' if i in twinkle else ""
        el.append(f'<circle{cls} cx="{x}" cy="{y}" r="{r}" fill="{fill}" '
                  f'stroke="{stroke}" stroke-width="1.3"/>')
    hx, hy = HUB
    el.append(f'<circle class="ring" cx="{hx}" cy="{hy}" r="26" fill="none" '
              f'stroke="#2dd4bf" stroke-width="1" opacity="0.18"/>')
    el.append(f'<circle cx="{hx}" cy="{hy}" r="9" fill="#0f766e" '
              f'stroke="#5eead4" stroke-width="1.6"/>')
    return "".join(el)


# 폼 카드 402px + 양옆 81px — Cognito main-layout 실측값(위 docstring)
FORM_COLUMN_PX = 402 + 81 * 2
# 이보다 좁으면 패널 문구가 폼 카드와 겹친다 — 전체 어두운 배경으로 전환
NARROW_PX = 1024

LAYOUT_STYLE = (
    f".panel{{width:calc(100% - {FORM_COLUMN_PX}px)}}"
    ".copy-narrow{display:none}"
    f"@media (max-width:{NARROW_PX - 1}px){{.panel{{width:100%}}"
    ".copy{display:none}.copy-narrow{display:inline}}"
)
MOTION_STYLE = (
    ".ring{transform-box:fill-box;transform-origin:center;"
    "animation:ring 2.8s ease-out infinite}"
    "@keyframes ring{0%{transform:scale(.4);opacity:.55}"
    "100%{transform:scale(1.6);opacity:0}}"
    ".tw{animation:tw 5s ease-in-out infinite}"
    "@keyframes tw{0%,100%{opacity:1}50%{opacity:.45}}"
)
REDUCED_MOTION = (
    "@media (prefers-reduced-motion:reduce){"
    ".ring,.tw,[class^=pk]{animation:none}}"
)


def build() -> str:
    g = graph()  # PACKET_KEYFRAMES 를 채운다
    style = LAYOUT_STYLE + MOTION_STYLE + "".join(PACKET_KEYFRAMES) + REDUCED_MOTION
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="100%" height="100%">'
        f'<style>{style}</style>'
        '<rect width="100%" height="100%" fill="#ffffff"/>'
        '<defs><clipPath id="panel-clip">'
        '<rect class="panel" width="50%" height="100%"/></clipPath></defs>'
        f'<rect class="panel" width="50%" height="100%" fill="{PANEL}"/>'
        # 그래프 — 페이지 전체 좌표계로 그리고 패널(clipPath) 밖은 잘라낸다
        '<g clip-path="url(#panel-clip)">'
        '<svg x="0" y="0" width="100%" height="100%" '
        f'viewBox="0 0 {GRAPH_W} {GRAPH_H}" '
        f'preserveAspectRatio="xMinYMid slice">{g}</svg></g>'
        # 문구 — 잘리지 않게 왼쪽 가운데 앵커
        '<svg class="copy" x="0" y="0" width="50%" height="100%" viewBox="0 0 640 800" '
        'preserveAspectRatio="xMinYMid meet">'
        f'<text x="56" y="372" fill="#ffffff" font-family="{FONT}" '
        f'font-size="40" font-weight="700" letter-spacing="-0.8">{HEADING[0]}</text>'
        f'<text x="56" y="420" fill="#ffffff" font-family="{FONT}" '
        f'font-size="40" font-weight="700" letter-spacing="-0.8">{HEADING[1]}</text>'
        f'<text x="56" y="462" fill="#b4b4b4" font-family="{FONT}" '
        f'font-size="16">{SUBTITLE}</text></svg>'
        # 하단 문구 — 왼쪽 아래 앵커
        '<svg class="copy" x="0" y="0" width="50%" height="100%" viewBox="0 0 640 800" '
        'preserveAspectRatio="xMinYMax meet">'
        f'<text x="56" y="752" fill="#a1a1a1" font-family="{FONT}" '
        f'font-size="12">{FOOTER}</text></svg>'
        # 좁은 화면 — 가운데 문구는 폼 카드에 가려지므로 숨기고, 폼 위쪽 여백
        # (Cognito main-layout 위 162px)에 한 줄로 둔다. 데스크톱 창을 줄인
        # 경우 기준(휴대폰 가로 모드 같은 낮은 높이는 고려하지 않음).
        '<g class="copy-narrow">'
        f'<text x="40" y="72" fill="#ffffff" font-family="{FONT}" '
        f'font-size="24" font-weight="700" letter-spacing="-0.4">'
        f'{HEADING[0]} {HEADING[1]}</text>'
        f'<text x="40" y="100" fill="#b4b4b4" font-family="{FONT}" '
        f'font-size="14">{SUBTITLE}</text></g>'
        '</svg>\n'
    )


if __name__ == "__main__":
    out = Path(__file__).with_name("background.svg")
    out.write_text(build(), encoding="utf-8")
    print(f"wrote {out} ({out.stat().st_size} bytes)")
