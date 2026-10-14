# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.exceptions import ForbiddenError, STSVerificationError
from app.models.auth import User, UserRole
from app.schemas.cli import STSRequestPayload, VirtualKeyIssueRequest
from app.services.cli_service import CLIService
from app.services.key_service import KeyService


@pytest.fixture
def mock_key_service() -> AsyncMock:
    svc = AsyncMock(spec=KeyService)
    svc.issue_key = AsyncMock(return_value=MagicMock(
        virtual_key="vk-test123",
        expires_at=MagicMock(),
    ))
    return svc


@pytest.fixture
def cli_service(mock_key_service: AsyncMock) -> CLIService:
    return CLIService(key_service=mock_key_service)


class TestSSRFPrevention:
    async def test_rejects_non_sts_host(self, cli_service: CLIService, mock_session: AsyncMock):
        data = VirtualKeyIssueRequest(
            sts_request=STSRequestPayload(
                url="https://evil.com/?Action=GetCallerIdentity",
                headers={},
            ),
            device_name="test",
        )

        with patch("app.services.cli_service.get_settings") as mock_settings:
            mock_settings.return_value.ALLOWED_STS_REGIONS = ["ap-northeast-2"]

            with pytest.raises(STSVerificationError, match="host not allowed"):
                await cli_service.verify_sts_and_issue_key(mock_session, data=data)

    async def test_rejects_non_getcalleridentity_action(self, cli_service: CLIService, mock_session: AsyncMock):
        data = VirtualKeyIssueRequest(
            sts_request=STSRequestPayload(
                url="https://sts.ap-northeast-2.amazonaws.com/?Action=AssumeRole",
                headers={},
            ),
            device_name="test",
        )

        with patch("app.services.cli_service.get_settings") as mock_settings:
            mock_settings.return_value.ALLOWED_STS_REGIONS = ["ap-northeast-2"]

            with pytest.raises(STSVerificationError, match="GetCallerIdentity"):
                await cli_service.verify_sts_and_issue_key(mock_session, data=data)

    async def test_accepts_valid_sts_global_endpoint(self, cli_service: CLIService, mock_session: AsyncMock):
        data = VirtualKeyIssueRequest(
            sts_request=STSRequestPayload(
                url="https://sts.amazonaws.com/?Action=GetCallerIdentity",
                headers={"Authorization": "AWS4-HMAC-SHA256 ..."},
            ),
            device_name="test",
        )

        sts_xml = """<GetCallerIdentityResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/">
            <GetCallerIdentityResult>
                <Arn>arn:aws:sts::123456789:assumed-role/AllowedRole/user@corp.com</Arn>
            </GetCallerIdentityResult>
        </GetCallerIdentityResponse>"""

        with patch("app.services.cli_service.get_settings") as mock_settings, \
             patch("app.services.cli_service.httpx") as mock_httpx:
            settings = mock_settings.return_value
            settings.ALLOWED_STS_REGIONS = ["ap-northeast-2"]
            settings.ALLOWED_IAM_ROLES = ["AllowedRole"]
            settings.ALLOWED_AWS_ACCOUNTS = ["123456789"]
            settings.DEFAULT_TEAM_ID = str(uuid.uuid4())
            settings.DEFAULT_USER_BUDGET_USD = 100.0

            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.text = sts_xml

            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock()
            mock_httpx.AsyncClient.return_value = mock_client

            with patch("app.services.cli_service.UserRepository") as MockUserRepo, \
                 patch("app.services.cli_service.BudgetRepository") as MockBudgetRepo:
                user = MagicMock(spec=User)
                user.id = uuid.uuid4()
                user.email = "user@corp.com"
                user.role = UserRole.DEVELOPER
                user.team_id = uuid.UUID(settings.DEFAULT_TEAM_ID)
                user.is_active = True

                MockUserRepo.return_value.get_by_sso_subject = AsyncMock(return_value=user)
                MockBudgetRepo.return_value.get_first_active_config = AsyncMock(return_value=None)

                result = await cli_service.verify_sts_and_issue_key(mock_session, data=data)

        assert result.user_id == str(user.id)


class TestIAMRoleCheck:
    async def test_rejects_disallowed_iam_role(self, cli_service: CLIService, mock_session: AsyncMock):
        data = VirtualKeyIssueRequest(
            sts_request=STSRequestPayload(
                url="https://sts.ap-northeast-2.amazonaws.com/?Action=GetCallerIdentity",
                headers={},
            ),
            device_name="test",
        )

        sts_xml = """<GetCallerIdentityResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/">
            <GetCallerIdentityResult>
                <Arn>arn:aws:sts::123456789:assumed-role/ForbiddenRole/user</Arn>
            </GetCallerIdentityResult>
        </GetCallerIdentityResponse>"""

        with patch("app.services.cli_service.get_settings") as mock_settings, \
             patch("app.services.cli_service.httpx") as mock_httpx:
            settings = mock_settings.return_value
            settings.ALLOWED_STS_REGIONS = ["ap-northeast-2"]
            settings.ALLOWED_IAM_ROLES = ["AllowedRole"]
            settings.ALLOWED_AWS_ACCOUNTS = ["123456789"]

            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.text = sts_xml

            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock()
            mock_httpx.AsyncClient.return_value = mock_client

            with pytest.raises(ForbiddenError, match="ForbiddenRole"):
                await cli_service.verify_sts_and_issue_key(mock_session, data=data)


class TestAutoProvisioning:
    async def test_auto_provisions_new_user(self, cli_service: CLIService, mock_session: AsyncMock):
        data = VirtualKeyIssueRequest(
            sts_request=STSRequestPayload(
                url="https://sts.ap-northeast-2.amazonaws.com/?Action=GetCallerIdentity",
                headers={},
            ),
            device_name="test",
        )

        sts_xml = """<GetCallerIdentityResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/">
            <GetCallerIdentityResult>
                <Arn>arn:aws:sts::123456789:assumed-role/AllowedRole/newuser@corp.com</Arn>
            </GetCallerIdentityResult>
        </GetCallerIdentityResponse>"""

        default_team_id = str(uuid.uuid4())

        with patch("app.services.cli_service.get_settings") as mock_settings, \
             patch("app.services.cli_service.httpx") as mock_httpx:
            settings = mock_settings.return_value
            settings.ALLOWED_STS_REGIONS = ["ap-northeast-2"]
            settings.ALLOWED_IAM_ROLES = ["AllowedRole"]
            settings.ALLOWED_AWS_ACCOUNTS = ["123456789"]
            settings.DEFAULT_TEAM_ID = default_team_id
            settings.DEFAULT_USER_BUDGET_USD = 100.0

            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.text = sts_xml

            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock()
            mock_httpx.AsyncClient.return_value = mock_client

            with patch("app.services.cli_service.UserRepository") as MockUserRepo, \
                 patch("app.services.cli_service.BudgetRepository") as MockBudgetRepo:
                # User not found → auto-provision
                MockUserRepo.return_value.get_by_sso_subject = AsyncMock(return_value=None)

                new_user = MagicMock(spec=User)
                new_user.id = uuid.uuid4()
                new_user.email = "newuser@corp.com"
                new_user.role = UserRole.DEVELOPER
                new_user.team_id = uuid.UUID(default_team_id)
                new_user.is_active = True
                MockUserRepo.return_value.create_user = AsyncMock(return_value=new_user)

                MockBudgetRepo.return_value.upsert_config = AsyncMock()
                MockBudgetRepo.return_value.get_first_active_config = AsyncMock(return_value=None)

                result = await cli_service.verify_sts_and_issue_key(mock_session, data=data)

        MockUserRepo.return_value.create_user.assert_called_once()
        assert result.user_id == str(new_user.id)


class TestAwsAccountCheck:
    """H1: 계정 allowlist — role 이름만으로는 외부 계정 우회를 못 막는다."""

    async def test_rejects_foreign_account_even_with_allowed_role_name(
        self, cli_service: CLIService, mock_session: AsyncMock
    ):
        """공격자 계정의 'AllowedRole' role 도 거부한다 — 계정이 경계다."""
        data = VirtualKeyIssueRequest(
            sts_request=STSRequestPayload(
                url="https://sts.amazonaws.com/?Action=GetCallerIdentity",
                headers={"Authorization": "AWS4-HMAC-SHA256 ..."},
            ),
            device_name="test",
        )
        sts_xml = """<GetCallerIdentityResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/">
            <GetCallerIdentityResult>
                <Arn>arn:aws:sts::999888777:assumed-role/AllowedRole/attacker</Arn>
            </GetCallerIdentityResult>
        </GetCallerIdentityResponse>"""

        with patch("app.services.cli_service.get_settings") as mock_settings, \
             patch("app.services.cli_service.httpx") as mock_httpx:
            settings = mock_settings.return_value
            settings.ALLOWED_STS_REGIONS = ["ap-northeast-2"]
            settings.ALLOWED_IAM_ROLES = ["AllowedRole"]
            settings.ALLOWED_AWS_ACCOUNTS = ["123456789"]

            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.text = sts_xml
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock()
            mock_httpx.AsyncClient.return_value = mock_client

            with pytest.raises(ForbiddenError, match="999888777"):
                await cli_service.verify_sts_and_issue_key(mock_session, data=data)

    async def test_rejects_iam_user_from_foreign_account(
        self, cli_service: CLIService, mock_session: AsyncMock
    ):
        """IAM user ARN 은 role 검사를 우회한다 — 계정 검사만이 막는다."""
        data = VirtualKeyIssueRequest(
            sts_request=STSRequestPayload(
                url="https://sts.amazonaws.com/?Action=GetCallerIdentity",
                headers={"Authorization": "AWS4-HMAC-SHA256 ..."},
            ),
            device_name="test",
        )
        sts_xml = """<GetCallerIdentityResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/">
            <GetCallerIdentityResult>
                <Arn>arn:aws:iam::999888777:user/attacker</Arn>
            </GetCallerIdentityResult>
        </GetCallerIdentityResponse>"""

        with patch("app.services.cli_service.get_settings") as mock_settings, \
             patch("app.services.cli_service.httpx") as mock_httpx:
            settings = mock_settings.return_value
            settings.ALLOWED_STS_REGIONS = ["ap-northeast-2"]
            settings.ALLOWED_IAM_ROLES = ["AllowedRole"]
            settings.ALLOWED_AWS_ACCOUNTS = ["123456789"]

            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.text = sts_xml
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock()
            mock_httpx.AsyncClient.return_value = mock_client

            with pytest.raises(ForbiddenError, match="999888777"):
                await cli_service.verify_sts_and_issue_key(mock_session, data=data)

    async def test_empty_account_list_rejects(
        self, cli_service: CLIService, mock_session: AsyncMock
    ):
        """R16: ALLOWED_AWS_ACCOUNTS 빈 리스트는 fail-closed — 계정 무관 VK 거부.

        과거엔 "비어 있으면 미적용(하위호환)" 이라 설정 누락 배포가 임의 AWS
        계정에 VK + 자동 프로비저닝을 열어줬다.
        """
        data = VirtualKeyIssueRequest(
            sts_request=STSRequestPayload(
                url="https://sts.amazonaws.com/?Action=GetCallerIdentity",
                headers={"Authorization": "AWS4-HMAC-SHA256 ..."},
            ),
            device_name="test",
        )
        sts_xml = """<GetCallerIdentityResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/">
            <GetCallerIdentityResult>
                <Arn>arn:aws:sts::999888777:assumed-role/AllowedRole/user@corp.com</Arn>
            </GetCallerIdentityResult>
        </GetCallerIdentityResponse>"""

        with patch("app.services.cli_service.get_settings") as mock_settings, \
             patch("app.services.cli_service.httpx") as mock_httpx:
            settings = mock_settings.return_value
            settings.ALLOWED_STS_REGIONS = ["ap-northeast-2"]
            settings.ALLOWED_IAM_ROLES = ["AllowedRole"]
            settings.ALLOWED_AWS_ACCOUNTS = []
            settings.DEFAULT_TEAM_ID = str(uuid.uuid4())
            settings.DEFAULT_USER_BUDGET_USD = 100.0

            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.text = sts_xml
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock()
            mock_httpx.AsyncClient.return_value = mock_client

            with pytest.raises(ForbiddenError, match="allowlist is empty"):
                await cli_service.verify_sts_and_issue_key(mock_session, data=data)

    async def test_iam_user_rejected_when_role_allowlist_set(
        self, cli_service: CLIService, mock_session: AsyncMock
    ):
        """R16: ALLOWED_IAM_ROLES 설정 시 IAM user ARN 은 role 검사 우회로 거부."""
        data = VirtualKeyIssueRequest(
            sts_request=STSRequestPayload(
                url="https://sts.amazonaws.com/?Action=GetCallerIdentity",
                headers={"Authorization": "AWS4-HMAC-SHA256 ..."},
            ),
            device_name="test",
        )
        sts_xml = """<GetCallerIdentityResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/">
            <GetCallerIdentityResult>
                <Arn>arn:aws:iam::123456789:user/mallory</Arn>
            </GetCallerIdentityResult>
        </GetCallerIdentityResponse>"""

        with patch("app.services.cli_service.get_settings") as mock_settings, \
             patch("app.services.cli_service.httpx") as mock_httpx:
            settings = mock_settings.return_value
            settings.ALLOWED_STS_REGIONS = ["ap-northeast-2"]
            settings.ALLOWED_IAM_ROLES = ["AllowedRole"]
            settings.ALLOWED_AWS_ACCOUNTS = ["123456789"]
            settings.DEFAULT_TEAM_ID = str(uuid.uuid4())
            settings.DEFAULT_USER_BUDGET_USD = 100.0

            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.text = sts_xml
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock()
            mock_httpx.AsyncClient.return_value = mock_client

            with pytest.raises(ForbiddenError, match="IAM user"):
                await cli_service.verify_sts_and_issue_key(mock_session, data=data)

    async def test_empty_role_list_rejects(
        self, cli_service: CLIService, mock_session: AsyncMock
    ):
        """R16-M2: ALLOWED_IAM_ROLES 빈 리스트도 fail-closed.

        과거엔 `if settings.ALLOWED_IAM_ROLES:` 가드 하나라 비어 있으면 역할
        검사·IAM-user 거부가 통째로 스킵돼 허용 계정 안의 임의 assumed-role
        (EC2/Lambda/CI role)이나 IAM user 가 VK 를 발급받을 수 있었다.
        """
        data = VirtualKeyIssueRequest(
            sts_request=STSRequestPayload(
                url="https://sts.amazonaws.com/?Action=GetCallerIdentity",
                headers={"Authorization": "AWS4-HMAC-SHA256 ..."},
            ),
            device_name="test",
        )
        sts_xml = """<GetCallerIdentityResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/">
            <GetCallerIdentityResult>
                <Arn>arn:aws:sts::123456789:assumed-role/AnyRole/user@corp.com</Arn>
            </GetCallerIdentityResult>
        </GetCallerIdentityResponse>"""

        with patch("app.services.cli_service.get_settings") as mock_settings, \
             patch("app.services.cli_service.httpx") as mock_httpx:
            settings = mock_settings.return_value
            settings.ALLOWED_STS_REGIONS = ["ap-northeast-2"]
            settings.ALLOWED_IAM_ROLES = []
            settings.ALLOWED_AWS_ACCOUNTS = ["123456789"]
            settings.DEFAULT_TEAM_ID = str(uuid.uuid4())
            settings.DEFAULT_USER_BUDGET_USD = 100.0

            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.text = sts_xml
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock()
            mock_httpx.AsyncClient.return_value = mock_client

            with pytest.raises(ForbiddenError, match="allowlist is empty"):
                await cli_service.verify_sts_and_issue_key(mock_session, data=data)
