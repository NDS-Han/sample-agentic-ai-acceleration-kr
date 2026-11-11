"""Web provisioning verifies identity without issuing or replacing CLI keys."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from app.core.db import get_db_session
from app.routers import auth_oidc
from app.services.oidc_service import OIDCService, OIDCAuthError
from app.core.oidc_verifier import OIDCVerifyError

class WebProvisionTests(unittest.IsolatedAsyncioTestCase):
    async def test_verified_user_is_provisioned_without_key(self):
        verifier = MagicMock()
        verifier.verify_async = AsyncMock(return_value={
            "sub": "subject", "email": "test@example.test", "email_verified": True,
            "cognito:groups": ["ClaudeAdmin", "Claude_platform_backend"],
        })
        keys = MagicMock()
        keys.issue_key = AsyncMock()
        svc = OIDCService(verifier, keys, MagicMock(), MagicMock())
        svc._resolve_team = AsyncMock(return_value="team")
        user = SimpleNamespace(id="user", role="ADMIN", is_active=True,
                               is_service_account=False, team_source="sync")
        svc._upsert_user = AsyncMock(return_value=(user, True, False))
        settings = SimpleNamespace(OIDC_USER_ID_CLAIM="sub", OIDC_EMAIL_CLAIM="email",
            OIDC_NAME_CLAIM="name", OIDC_GROUPS_CLAIM="cognito:groups", OIDC_REQUIRED_GROUP="")
        with patch("app.services.oidc_service.get_settings", return_value=settings), \
             patch.object(svc, "_derive_role", return_value="ADMIN"):
            result = await svc.provision_user(AsyncMock(), token="signed-token")
        self.assertIs(result, user)
        verifier.verify_async.assert_awaited_once()
        keys.issue_key.assert_not_awaited()
        self.assertEqual(svc._upsert_user.await_args.kwargs["sso_subject"], "subject")

    async def test_invalid_token_cannot_provision(self):
        verifier = MagicMock()
        verifier.verify_async = AsyncMock(side_effect=OIDCVerifyError("invalid signature"))
        svc = OIDCService(verifier, MagicMock(), MagicMock(), MagicMock())
        svc._upsert_user = AsyncMock()
        with self.assertRaises(OIDCAuthError):
            await svc.provision_user(AsyncMock(), token="invalid")
        svc._upsert_user.assert_not_awaited()

    async def test_endpoint_requires_bearer_and_enforces_rate_limit(self):
        app = FastAPI()
        app.include_router(auth_oidc.router)
        svc = MagicMock()
        svc.provision_user = AsyncMock(return_value=SimpleNamespace(id="u", role="ADMIN"))
        app.state.oidc_service = svc
        app.state.redis = AsyncMock()
        app.state.redis.incr.return_value = 1
        async def db():
            yield AsyncMock()
        app.dependency_overrides[get_db_session] = db
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            missing = await client.post("/v1/auth/provision")
            self.assertEqual(missing.status_code, 401)
            svc.provision_user.assert_not_awaited()
            ok = await client.post("/v1/auth/provision", headers={"Authorization":"Bearer signed"})
            self.assertEqual(ok.status_code, 200)
            self.assertEqual(ok.json(), {"user_id":"u", "role":"ADMIN"})
            app.state.redis.incr.return_value = 11
            limited = await client.post("/v1/auth/provision", headers={"Authorization":"Bearer signed"})
            self.assertEqual(limited.status_code, 429)
            self.assertEqual(svc.provision_user.await_count, 1)
