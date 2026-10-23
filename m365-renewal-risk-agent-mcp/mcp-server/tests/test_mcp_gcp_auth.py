import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from google.auth.exceptions import RefreshError

from gcp_auth import AzureSubjectToken, gcp_credentials

AUDIENCE = (
    "//iam.googleapis.com/projects/123456789/locations/global/"
    "workloadIdentityPools/bridge-test/providers/azure"
)
ENV = {
    "GCP_WORKLOAD_IDENTITY_AUDIENCE": AUDIENCE,
    "GCP_AZURE_TOKEN_RESOURCE": "api://22222222-2222-4222-8222-222222222222",
    "IDENTITY_ENDPOINT": "http://169.254.129.2:8081/msi/token",
    "IDENTITY_HEADER": "unit-test-header",
}


def test_local_adc_keeps_existing_scopes(monkeypatch):
    default = Mock(return_value=(object(), "test-project"))
    monkeypatch.setattr("gcp_auth.google.auth.default", default)
    assert gcp_credentials({}) is default.return_value[0]
    default.assert_called_once_with(scopes=["https://www.googleapis.com/auth/cloud-platform"])


def test_managed_identity_token_is_exchanged_at_google_sts():
    credentials = gcp_credentials(ENV)
    request = Mock(side_effect=[
        SimpleNamespace(status=200, data=json.dumps({"access_token": "azure-subject"}).encode()),
        SimpleNamespace(status=200, data=json.dumps({
            "access_token": "google-access", "expires_in": 3600, "token_type": "Bearer",
        }).encode()),
    ])
    credentials.refresh(request)
    assert credentials.token == "google-access"
    azure, google = request.call_args_list
    assert azure.args[0].startswith(ENV["IDENTITY_ENDPOINT"])
    assert azure.kwargs["headers"] == {"X-IDENTITY-HEADER": "unit-test-header"}
    assert azure.kwargs["allow_redirects"] is False
    assert "api-version=2019-08-01" in azure.args[0]
    assert google.kwargs["url"] == "https://sts.googleapis.com/v1/token"
    assert b"subject_token=azure-subject" in google.kwargs["body"]


def test_user_assigned_identity_and_builtin_exchange_audience():
    env = {**ENV, "GCP_AZURE_TOKEN_RESOURCE": "api://AzureADTokenExchange",
           "AZURE_CLIENT_ID": "33333333-3333-4333-8333-333333333333"}
    assert gcp_credentials(env) is not None
    request = Mock(return_value=SimpleNamespace(
        status=200, data=b'{"access_token":"opaque-test-token"}',
    ))
    assert AzureSubjectToken(env, env["GCP_AZURE_TOKEN_RESOURCE"]).get_subject_token(
        None, request
    ) == "opaque-test-token"
    assert "client_id=" + env["AZURE_CLIENT_ID"] in request.call_args.args[0]


@pytest.mark.parametrize("change", [
    {"GCP_WORKLOAD_IDENTITY_AUDIENCE": ""},
    {"GCP_WORKLOAD_IDENTITY_AUDIENCE": "https://untrusted.example"},
    {"GCP_AZURE_TOKEN_RESOURCE": "https://graph.microsoft.com"},
    {"IDENTITY_ENDPOINT": "https://untrusted.example/token"},
    {"IDENTITY_ENDPOINT": "http://127.0.0.1@untrusted.example/token"},
    {"IDENTITY_HEADER": ""},
])
def test_hosted_identity_fails_closed(change):
    with pytest.raises(ValueError):
        gcp_credentials({**ENV, **change})


@pytest.mark.parametrize("status,body", [
    (403, b"denied"), (302, b""), (200, b"not json"),
    (200, b"[]"), (200, b'{"access_token":""}'),
])
def test_identity_failures_are_not_tokens(status, body):
    request = Mock(return_value=SimpleNamespace(status=status, data=body))
    with pytest.raises(RefreshError):
        AzureSubjectToken(ENV, ENV["GCP_AZURE_TOKEN_RESOURCE"]).get_subject_token(None, request)
