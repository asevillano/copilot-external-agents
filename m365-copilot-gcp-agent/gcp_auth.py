from ipaddress import ip_address
import json
import os
import re
from typing import Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import UUID

import google.auth
from google.auth.exceptions import RefreshError
from google.auth import identity_pool


class AzureSubjectToken(identity_pool.SubjectTokenSupplier):
    def __init__(self, env: Mapping[str, str], resource: str):
        self.env = env
        endpoint = urlsplit(env.get("IDENTITY_ENDPOINT", ""))
        host = endpoint.hostname or ""
        try:
            local = host == "localhost" or ip_address(host).is_loopback or (
                ip_address(host).is_link_local
            )
        except ValueError:
            local = False
        if (
            endpoint.scheme != "http" or not local or endpoint.username
            or endpoint.password or endpoint.fragment
        ):
            raise ValueError("IDENTITY_ENDPOINT must be the local Azure managed identity endpoint")
        if not env.get("IDENTITY_HEADER"):
            raise ValueError("Azure IDENTITY_HEADER is missing")
        params = dict(parse_qsl(endpoint.query))
        params.update({"resource": resource, "api-version": "2019-08-01"})
        if env.get("AZURE_CLIENT_ID"):
            params["client_id"] = str(UUID(env["AZURE_CLIENT_ID"]))
        self.url = urlunsplit(endpoint._replace(query=urlencode(params)))

    def get_subject_token(self, context, request) -> str:
        response = request(
            self.url, method="GET", headers={"X-IDENTITY-HEADER": self.env["IDENTITY_HEADER"]},
            timeout=15, allow_redirects=False,
        )
        if response.status != 200:
            raise RefreshError(f"Azure managed identity returned HTTP {response.status}")
        try:
            body = json.loads(response.data)
        except (ValueError, UnicodeDecodeError):
            raise RefreshError("Azure managed identity returned invalid JSON") from None
        token = body.get("access_token") if isinstance(body, dict) else None
        if not isinstance(token, str) or not token.strip():
            raise RefreshError("Azure managed identity returned no access token")
        return token


def gcp_credentials(env: Mapping[str, str] | None = None):
    env = os.environ if env is None else env
    scopes = ["https://www.googleapis.com/auth/cloud-platform"]
    audience = env.get("GCP_WORKLOAD_IDENTITY_AUDIENCE", "").strip()
    if not audience:
        if env.get("IDENTITY_ENDPOINT"):
            raise ValueError("Azure hosting requires GCP_WORKLOAD_IDENTITY_AUDIENCE")
        credentials, _ = google.auth.default(scopes=scopes)
        return credentials
    if not re.fullmatch(
        r"//iam\.googleapis\.com/projects/[0-9]+/locations/global/"
        r"workloadIdentityPools/[a-z0-9-]+/providers/[a-z0-9-]+", audience,
    ):
        raise ValueError("Invalid GCP_WORKLOAD_IDENTITY_AUDIENCE")
    resource = env.get("GCP_AZURE_TOKEN_RESOURCE", "").strip()
    if resource != "api://AzureADTokenExchange" and not re.fullmatch(
        r"api://[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", resource,
    ):
        raise ValueError("GCP_AZURE_TOKEN_RESOURCE must identify an Entra token exchange API")
    return identity_pool.Credentials(
        audience=audience, subject_token_type="urn:ietf:params:oauth:token-type:jwt",
        token_url="https://sts.googleapis.com/v1/token", scopes=scopes,
        subject_token_supplier=AzureSubjectToken(env, resource),
    )
