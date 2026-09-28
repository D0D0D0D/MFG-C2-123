"""AT-09 and API-surface regression tests for the SDS entry adapter."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from src.api import server as server_module
from src.api.server import InvokeRequest
from src.entry_adapter import Operation
from src.ingest_row_pipeline import CheckpointScrubPolicy, scrub_checkpoint_envelope
from src.payload_store import PayloadStore
from src.services.service import SdsRevisionStore, wire_services


def test_AT_09_client_payload_ref_is_discarded_and_server_ref_is_reissued():
    """AT-09: a caller cannot select an existing vault record with payload_ref."""
    store = PayloadStore(scope_keys=("tenant_id", "org_id"))
    scope = {"tenant_id": "tenant-a", "org_id": "org-a"}
    forged_ref = store.put(json.dumps({"sections": {"1": "other body"}}), scope=scope, session_id="session-a")
    canonical = scrub_checkpoint_envelope(
        json.dumps({"mode": "invoke", "case_ref": "case-a", "payload_ref": forged_ref, "payload": {"sections": {"1": "submitted body"}}}),
        store,
        session_id="session-a",
        trusted_scope=scope,
        policy=CheckpointScrubPolicy(
            safe_keys=frozenset({"mode", "case_ref"}),
            scope_keys=frozenset({"tenant_id", "org_id"}),
            enum_value_keys={"mode": frozenset({"invoke", "ingest", "feedback"})},
        ),
    )
    assert json.loads(canonical)["payload_ref"] != forged_ref


def test_AT_09_server_invoke_ignores_client_payload_ref(monkeypatch):
    """AT-09 end-to-end: the FastAPI adapter always uses its newly minted ref."""
    store = SdsRevisionStore(
        cases={"case-a": {"tenant_id": "tenant-a", "org_id": "org-a", "revision_status": "draft", "rule_edition_ref": "rule-a"}}
    )
    config = wire_services({"store": store, "top_k": 8, "max_retry": 3, "timeout_s": 30, "memory_enabled": False, "hitl": {"enabled": False}})
    monkeypatch.setattr(server_module, "_RUNTIME_CONFIG", config)
    monkeypatch.setattr(server_module.app.state, "verify_scope", lambda _request: {"tenant_id": "tenant-a", "org_id": "org-a"}, raising=False)
    monkeypatch.setenv("INVOKE_AUTH_TOKEN", "mock-entry-token")
    forged_ref = "not-a-server-ref"
    with TestClient(server_module.app) as client:
        response = client.post(
            "/invoke",
            headers={"authorization": "Bearer mock-entry-token"},
            json={"mode": "invoke", "case_ref": "case-a", "payload_ref": forged_ref, "payload": {"sections": {"1": "submitted body"}}},
        )
    assert response.status_code == 200
    assert response.json()["status"] == "success"


def test_mode_is_mapped_to_the_declared_operation():
    """N-6: the shared endpoint authenticates the payload mode, not its URL."""

    assert server_module._MODE_OPERATIONS == {
        "invoke": Operation.READ_INVOKE,
        "ingest": Operation.WRITE_INGEST,
        "feedback": Operation.WRITE_FEEDBACK,
    }


def test_token_with_verified_scope_cannot_select_ingest(monkeypatch):
    """N-6: a standalone token stays read-only even with a valid scope seam."""

    monkeypatch.setattr(
        server_module.app.state,
        "verify_scope",
        lambda _request: {"tenant_id": "tenant-a", "org_id": "org-a"},
        raising=False,
    )
    monkeypatch.setenv("INVOKE_AUTH_TOKEN", "mock-entry-token")
    response = TestClient(server_module.app).post(
        "/invoke",
        headers={"authorization": "Bearer mock-entry-token"},
        json={"mode": "ingest", "case_ref": "case-a", "payload": {"records": []}},
    )
    assert response.status_code == 403


def test_agent_factory_binds_ingest_context_per_request(monkeypatch):
    """I-7/I-8: trusted scope/session live on distinct request graphs."""

    config = wire_services(
        {
            "store": SdsRevisionStore(),
            "top_k": 8,
            "max_retry": 3,
            "timeout_s": 30,
            "memory_enabled": False,
            "hitl": {"enabled": False},
        }
    )
    monkeypatch.setattr(server_module, "_RUNTIME_CONFIG", config)
    first = server_module._build_agent(
        trusted_scope={"tenant_id": "tenant-a", "org_id": "org-a"},
        session_id="session-a",
    )
    second = server_module._build_agent(
        trusted_scope={"tenant_id": "tenant-b", "org_id": "org-b"},
        session_id="session-b",
    )

    first_ingest = first._nodes["main"].get_subgraph()._nodes["accumulate_ingest"]
    second_ingest = second._nodes["main"].get_subgraph()._nodes["accumulate_ingest"]
    assert first is not second
    assert first_ingest is not second_ingest
    assert first_ingest._trusted_scope == {"tenant_id": "tenant-a", "org_id": "org-a"}
    assert first_ingest._session_id == "session-a"
    assert second_ingest._trusted_scope == {"tenant_id": "tenant-b", "org_id": "org-b"}
    assert second_ingest._session_id == "session-b"


def test_low1_revision_status_is_not_an_invoke_request_field():
    """The API cannot invite a caller to self-report an authoritative status."""
    assert "revision_status" not in InvokeRequest.model_fields
