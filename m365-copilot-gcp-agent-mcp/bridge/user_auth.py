from uuid import UUID

import aiohttp

GRAPH_ME = "https://graph.microsoft.com/v1.0/me?$select=id"


class UserAuthorizationError(RuntimeError):
    pass


async def verify_entra_identity(token: str, expected_user: str) -> None:
    if not token:
        raise UserAuthorizationError("Entra user token is missing")
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
        async with session.get(
            GRAPH_ME, headers={"Authorization": f"Bearer {token}"}, allow_redirects=False
        ) as response:
            if response.status != 200:
                raise UserAuthorizationError(
                    f"Entra user verification failed: Graph HTTP {response.status}"
                )
            profile = await response.json()
    if not isinstance(profile, dict) or not isinstance(profile.get("id"), str):
        raise UserAuthorizationError("Graph did not return a user object ID")
    try:
        actual_user = str(UUID(profile["id"]))
    except ValueError as error:
        raise UserAuthorizationError("Graph returned an invalid user object ID") from error
    if actual_user != expected_user:
        raise UserAuthorizationError("Signed-in Entra user does not match activity sender")
