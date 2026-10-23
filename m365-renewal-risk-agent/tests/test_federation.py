import pytest

from scripts.deploy_federation import provider_config, TENANT, TOKEN_EXCHANGE_AUDIENCE


def test_trust_is_restricted_to_hosting_tenant_and_identity():
    principal = "33333333-3333-4333-8333-333333333333"
    cfg = provider_config(
        principal, f"https://login.microsoftonline.com/{TENANT}/v2.0", TOKEN_EXCHANGE_AUDIENCE,
    )
    assert cfg["oidc"]["allowedAudiences"] == [TOKEN_EXCHANGE_AUDIENCE]
    assert cfg["attributeMapping"] == {"google.subject": "assertion.oid"}
    assert principal in cfg["attributeCondition"] and TENANT in cfg["attributeCondition"]


@pytest.mark.parametrize("issuer,audience", [
    ("https://untrusted.example", TOKEN_EXCHANGE_AUDIENCE),
    (f"https://login.microsoftonline.com/{TENANT}/v2.0", "https://graph.microsoft.com"),
    (f"https://login.microsoftonline.com/{TENANT}/v2.0", "api://AzureADTokenExchange"),
])
def test_unexpected_identity_provider_is_rejected(issuer, audience):
    with pytest.raises(ValueError):
        provider_config("33333333-3333-4333-8333-333333333333", issuer, audience)
