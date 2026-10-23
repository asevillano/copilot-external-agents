import hashlib

from scripts.deploy_gcp import ROOT, add_member, operation_schemas, staged_source


def test_packaging_includes_only_unmodified_original_source_and_entrypoint():
    with staged_source() as (directory, hashes):
        names = {
            str(path.relative_to(directory)).replace("\\", "/")
            for path in directory.rglob("*") if path.is_file()
        }
        assert names == {
            "renewal_risk_agent/__init__.py", "renewal_risk_agent/agent.py",
            "gcp_entrypoint.py", "requirements.txt",
        }
        source = ROOT / "gcp_agent" / "renewal_risk_agent" / "agent.py"
        assert hashes["agent.py"] == hashlib.sha256(source.read_bytes()).hexdigest()
        assert (directory / "renewal_risk_agent" / "agent.py").read_bytes() == source.read_bytes()
    assert not directory.exists()


def test_only_three_transport_methods_are_exposed():
    schemas = operation_schemas()
    assert [schema["name"] for schema in schemas] == [
        "async_create_session", "async_delete_session", "async_stream_query"
    ]
    assert schemas[-1]["api_mode"] == "async_stream"
    assert schemas[-1]["parameters"]["required"] == ["user_id", "session_id", "message"]


def test_iam_addition_preserves_existing_members_conditions_and_etag():
    policy = {
        "etag": "unchanged",
        "bindings": [
            {"role": "roles/example", "members": ["existing"]},
            {"role": "roles/conditional", "members": ["existing"],
             "condition": {"expression": "true"}},
        ],
    }
    assert add_member(policy, "roles/example", "new")
    assert not add_member(policy, "roles/example", "new")
    assert add_member(policy, "roles/conditional", "new")
    assert policy["etag"] == "unchanged"
    assert policy["bindings"][0]["members"] == ["existing", "new"]
    assert policy["bindings"][1]["members"] == ["existing"]
