"""The Stage 5 smoke provisioning must never open a hole in a real deployment.

``deploy-stg`` runs the shipped entry adapter against an empty in-memory store,
so the smoke needs a caller scope and one case to reach any node at all.  Both
are provisioned only inside the provisional ``deploy-stg`` job.  These tests pin
that boundary: ``INVOKE_AUTH_TOKEN`` alone must never be enough, because a real
standalone deployment sets it too.
"""

from __future__ import annotations

import importlib

import pytest


CI_PROVENANCE = {
    "CI": "true",
    "CI_JOB_NAME": "deploy-stg",
    "STG_MOCK_MODE": "true",
}
STG_DEFAULTS = {
    "INVOKE_AUTH_TOKEN": "smoke-token",
    "STG_DEFAULT_TENANT_ID": "stg-tenant",
    "STG_DEFAULT_ORG_ID": "stg-org",
    "STG_DEFAULT_CASE_REF": "stg-case",
    "STG_DEFAULT_RULE_EDITION_REF": "stg-rule",
}
ALL_ENV_KEYS = set(CI_PROVENANCE) | set(STG_DEFAULTS)


def _load_server(monkeypatch, env: dict):
    for key in ALL_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    import src.api.server as server

    return importlib.reload(server)


@pytest.fixture(autouse=True)
def _restore_server_module():
    """Reload the module with a clean environment so later tests are unaffected."""

    yield
    import src.api.server as server

    importlib.reload(server)


def test_scope_and_case_are_provisioned_inside_the_deploy_stg_job(monkeypatch):
    server = _load_server(monkeypatch, {**CI_PROVENANCE, **STG_DEFAULTS})
    assert server._is_provisional_stg_smoke_job() is True
    assert server._stg_smoke_scope() == {"tenant_id": "stg-tenant", "org_id": "stg-org"}
    assert server._RUNTIME_CONFIG["store"].cases["stg-case"]["tenant_id"] == "stg-tenant"


@pytest.mark.parametrize(
    "dropped_key",
    sorted(CI_PROVENANCE),
)
def test_missing_any_ci_provenance_marker_disables_provisioning(monkeypatch, dropped_key):
    """Every provenance marker is load-bearing; two out of three is not enough."""

    env = {**CI_PROVENANCE, **STG_DEFAULTS}
    env.pop(dropped_key)
    server = _load_server(monkeypatch, env)
    assert server._is_provisional_stg_smoke_job() is False
    assert server._stg_smoke_scope() is None
    assert server._RUNTIME_CONFIG["store"].cases == {}


def test_auth_token_alone_does_not_provision(monkeypatch):
    """A real standalone deployment sets INVOKE_AUTH_TOKEN and must stay closed."""

    server = _load_server(monkeypatch, STG_DEFAULTS)
    assert server._stg_smoke_scope() is None
    assert server._RUNTIME_CONFIG["store"].cases == {}


def test_provisioning_requires_the_auth_token(monkeypatch):
    env = {**CI_PROVENANCE, **STG_DEFAULTS}
    env.pop("INVOKE_AUTH_TOKEN")
    server = _load_server(monkeypatch, env)
    assert server._stg_smoke_scope() is None


@pytest.mark.parametrize("blank_key", ["STG_DEFAULT_TENANT_ID", "STG_DEFAULT_ORG_ID"])
def test_blank_or_padded_scope_values_do_not_provision(monkeypatch, blank_key):
    env = {**CI_PROVENANCE, **STG_DEFAULTS, blank_key: "  "}
    server = _load_server(monkeypatch, env)
    assert server._stg_smoke_scope() is None


def test_case_is_not_seeded_when_the_store_already_holds_cases(monkeypatch):
    """A populated store is a real deployment; never mutate its case table."""

    server = _load_server(monkeypatch, {**CI_PROVENANCE, **STG_DEFAULTS})
    existing = {"real-case": {"tenant_id": "real", "org_id": "real", "revision_status": "draft"}}
    config = {"store": type(server._RUNTIME_CONFIG["store"])(cases=dict(existing))}
    server._provision_stg_smoke_case(config)
    assert config["store"].cases == existing


@pytest.mark.parametrize(
    "stamped",
    [
        {"tenant_id": "attacker-tenant"},          # org_id absent
        {"org_id": "attacker-org"},                # tenant_id absent
        {"tenant_id": "attacker-tenant", "org_id": ""},   # explicitly blank
        {"tenant_id": None, "org_id": None},       # stamped, but broken verifier
    ],
)
def test_partially_stamped_scope_is_never_rescued_by_the_smoke_fallback(monkeypatch, stamped):
    """A broken verifier must 403 — the smoke default must not complete it.

    Rescuing a partial scope would let a half-working upstream verifier be
    silently upgraded to the placeholder tenant, which is a cross-tenant risk.
    Only a request with *no* scope attribute at all may use the fallback.
    """

    import json
    from pathlib import Path

    from fastapi.testclient import TestClient
    from starlette.middleware.base import BaseHTTPMiddleware

    server = _load_server(monkeypatch, {**CI_PROVENANCE, **STG_DEFAULTS})

    class _PartialStamp(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            for key, value in stamped.items():
                setattr(request.state, key, value)
            return await call_next(request)

    server.app.add_middleware(_PartialStamp)
    payload = json.loads(
        (Path(__file__).parents[2] / "deploy" / "invoke_payload.json").read_text(encoding="utf-8")
    )
    response = TestClient(server.app).post(
        "/invoke", json=payload, headers={"Authorization": "Bearer smoke-token"}
    )
    assert response.status_code == 403, response.text


def test_production_invoke_stays_fail_closed(monkeypatch):
    """End-to-end: without CI provenance the smoke payload is refused."""

    import json
    from pathlib import Path

    from fastapi.testclient import TestClient

    server = _load_server(monkeypatch, STG_DEFAULTS)
    payload = json.loads(
        (Path(__file__).parents[2] / "deploy" / "invoke_payload.json").read_text(encoding="utf-8")
    )
    response = TestClient(server.app).post(
        "/invoke", json=payload, headers={"Authorization": "Bearer smoke-token"}
    )
    assert response.status_code == 403


def test_deploy_stg_invoke_reaches_success(monkeypatch):
    """The read-only smoke exercises Stage 1-4 and returns review findings.

    The env values here mirror the CI variables the job sets, so this also pins
    that ``STG_DEFAULT_CASE_REF`` matches ``deploy/invoke_payload.json``: a
    mismatch would seed a case the payload never asks for and the smoke would
    fail closed on ``pre_process``.
    """

    import json
    from pathlib import Path

    from fastapi.testclient import TestClient

    payload = json.loads(
        (Path(__file__).parents[2] / "deploy" / "invoke_payload.json").read_text(encoding="utf-8")
    )
    env = {
        **CI_PROVENANCE,
        **STG_DEFAULTS,
        "STG_DEFAULT_CASE_REF": payload["case_ref"],
        "STG_DEFAULT_RULE_EDITION_REF": payload["rule_edition_ref"],
    }
    server = _load_server(monkeypatch, env)
    response = TestClient(server.app).post(
        "/invoke", json=payload, headers={"Authorization": "Bearer smoke-token"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success", body
    assert body["output"]["revision_brief"]["sections"], body
    assert body["output"]["evidence_map"], body
    assert body["output"]["requires_human_review"] is True


def test_deploy_stg_token_cannot_select_ingest(monkeypatch):
    """N-6: STG-default scope does not turn the smoke token into a writer."""

    from fastapi.testclient import TestClient

    server = _load_server(monkeypatch, {**CI_PROVENANCE, **STG_DEFAULTS})
    response = TestClient(server.app).post(
        "/invoke",
        headers={"Authorization": "Bearer smoke-token"},
        json={"mode": "ingest", "case_ref": "stg-case", "payload": {"records": []}},
    )
    assert response.status_code == 403
