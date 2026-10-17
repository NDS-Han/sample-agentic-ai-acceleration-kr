# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""로그용 이메일 마스킹 — PII(수신자 주소)를 로그에 평문으로 남기지 않는다.

ses_sender 만 쓰던 것을 핸들러/스위퍼 로그도 같은 형태로 가리도록 공용 모듈로 뺐다.
"""


def mask_email(address: str) -> str:
    """도메인은 운영 디버깅(사내/외부 도메인 구분)에 필요해 보존하고,
    로컬 파트만 첫 글자 + *** 로 가린다."""
    local, _, domain = address.partition("@")
    if not domain:
        return "***"
    return f"{local[:1]}***@{domain}"


_EMAIL_RE = None


def mask_emails_in_text(text: str) -> str:
    """임의 문자열 안의 이메일 주소를 전부 mask_email 형태로 가린다.

    SES/SMTP 오류 메시지는 수신자 주소를 평문으로 포함한다
    (``SMTPRecipientsRefused``, "not verified" 등) — 로그와
    ``error_message`` 컬럼에 그대로 저장하면 수신자 PII 가 남는다.
    """
    global _EMAIL_RE
    if _EMAIL_RE is None:
        import re

        _EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
    return _EMAIL_RE.sub(lambda m: mask_email(m.group(0)), text)
