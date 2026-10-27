import unittest
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestServer

from test_config import USER
from user_auth import UserAuthorizationError, verify_entra_identity


class EntraIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.status = 200
        self.profile = {"id": USER}
        self.headers = []

        async def me(request):
            self.headers.append(request.headers.get("Authorization"))
            return web.json_response(self.profile, status=self.status)

        app = web.Application()
        app.router.add_get("/me", me)
        self.server = TestServer(app)
        await self.server.start_server()
        self.endpoint = patch("user_auth.GRAPH_ME", str(self.server.make_url("/me")))
        self.endpoint.start()

    async def asyncTearDown(self):
        self.endpoint.stop()
        await self.server.close()

    async def test_token_verified_by_graph_without_decoding_it(self):
        await verify_entra_identity("opaque-unit-test-token", USER)
        assert self.headers == ["Bearer opaque-unit-test-token"]

    async def test_empty_token_does_not_contact_graph(self):
        with self.assertRaises(UserAuthorizationError):
            await verify_entra_identity("", USER)
        assert not self.headers

    async def test_expired_token_and_redirects_fail_closed(self):
        for status in (401, 403, 307, 500):
            self.status = status
            with self.assertRaises(UserAuthorizationError):
                await verify_entra_identity("unit-test-token", USER)

    async def test_different_user_and_invalid_graph_responses_fail_closed(self):
        for profile in (
            {"id": "44444444-4444-4444-8444-444444444444"},
            {"id": "not-an-object-id"}, {"id": None}, {}, [],
        ):
            self.profile = profile
            with self.assertRaises(UserAuthorizationError):
                await verify_entra_identity("unit-test-token", USER)
