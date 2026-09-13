# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""per-app 모델 허용목록: model_aliases.allowed_clients

Revision ID: 0035
Revises: 0034
Create Date: 2026-09-13

## 무엇을 추가하나

``model.model_aliases.allowed_clients TEXT[]`` — "어떤 앱이 이 모델을 쓸 수 있나" 라는
**모델 × 앱** 인가 축이다.

이미 있는 ``auth.user_allowed_clients``(0010)와 **다른 질문**이다:

    auth.user_allowed_clients   사용자 U 가 앱 codex 를 쓸 수 있나        (사용자 × 앱)
    model_aliases.allowed_clients  앱 codex 가 모델 opus-5 를 쓸 수 있나  (모델 × 앱)

기존 축만으로는 "codex 는 haiku 만, claude-code 는 전 모델" 같은 정책을 표현할 수 없다.
그래서 이건 중복이 아니라 새 축이다. 두 게이트는 AND 로 걸린다(둘 다 통과해야 허용).

## 3-상태 의미론 — 이 필드의 유일한 정의

    NULL (미설정)   제한 없음. 지금 앱과 **나중에 추가될 앱** 전부 허용.
    {}   (빈 배열)  명시적으로 빈 허용목록 → **어떤 앱도 이 모델을 쓸 수 없다.**
    {codex}         허용목록. 목록 밖의 client 는 거부되며 'other'/NULL 도 거부.

⚠️ ``{}`` 를 "전부 허용" 으로 읽으면 안 된다. 원본 구현이 처음에 그렇게 했고 그것이
   결함이었다: ``{}`` 는 운영자가 콘솔에서 **마지막 앱의 체크를 해제**했을 때 만들어지는
   값이다(앱 정책 패널의 체크박스를 다 끄거나, 모델 편집 대화상자의 멀티셀렉트를 비우면
   ``[]`` 가 전송된다). 화면은 "허용된 앱 없음" 으로 보여주는데 게이트는 **모든 앱**을
   통과시켰다 — 접근제어 필드는 빈 경우에 fail-**closed** 여야 한다.

   그래서 판정은 ``is None`` 으로 한다. 파이썬의 falsiness(``if not allowed``)를 쓰면
   ``None`` 과 ``[]`` 가 같아져 정확히 그 결함으로 되돌아간다.

## 왜 0035 인가 (번호 충돌 기록)

원본 레포에서 이 마이그레이션의 revision 은 ``0016`` 이었다. 이 트리의 ``0016`` 은
Codex enum 추가라 **완전히 다른 내용이고 같은 (revision, down_revision) 쌍**을 갖는다.
그 파일을 그대로 가져오면 alembic 이 "Multiple head revisions are present" 로
**exit 255** 이고, 마이그레이션 Job 은 ``set -e`` 아래에서 **아무것도 적용하지 못한다**
(실 alembic CLI 로 재현 확인). 파일명이 달라도 마찬가지다 — alembic 은 파일명이 아니라
파일 안의 ``revision`` 값을 본다.

⚠️ 원본의 0001~0015 를 같이 복사하면 안 된다 — 그 쪽 0009 에는 이 트리가 이미 스크럽한
   실 계정 ID 와 옛 브랜드 문자열이 남아 있다.

## 되돌리기

컬럼만 떨어뜨린다. 정책 데이터는 함께 사라지므로, downgrade 후 다시 upgrade 하면
모든 모델이 "제한 없음"(NULL) 으로 돌아간다 — 즉 **되돌리는 방향은 접근을 넓힌다.**
운영 중 downgrade 는 그 사실을 알고 해야 한다.
"""

from alembic import op

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # IF NOT EXISTS: 같은 컬럼을 손으로 넣어 둔 환경에서도 멱등.
    op.execute("ALTER TABLE model.model_aliases ADD COLUMN IF NOT EXISTS allowed_clients TEXT[]")

    # ⚠️ 기본값을 넣지 않는다. DEFAULT '{}' 를 붙이면 기존 모든 모델이 "빈 허용목록"
    #    = **어떤 앱도 쓸 수 없음** 이 되어 게이트웨이가 모든 요청을 거부한다.
    #    NULL(제한 없음)이 유일하게 하위호환인 기본값이다.


def downgrade() -> None:
    op.execute("ALTER TABLE model.model_aliases DROP COLUMN IF EXISTS allowed_clients")
