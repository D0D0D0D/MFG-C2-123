"""HTTP entry adapter: authenticate, scrub, lease, then invoke the graph."""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from framework.schemas.invocation_context import InvocationContext
from framework.secrets.context import bound_secrets
from framework.utils.config_loader import load_config
from shared.secrets import factory as secrets_factory
from src.entry_adapter import auth as entry_auth
from src.graph.graph import SdsRevisionEvidenceGraph
from src.ingest_row_pipeline import CheckpointScrubPolicy, scrub_checkpoint_envelope
from src.services.service import wire_services


_ROOT = Path(__file__).resolve().parents[2]
_RUNTIME_CONFIG = wire_services(load_config(str(_ROOT / "config" / "config.yaml")))

app = FastAPI(title="SDSRevisionEvidenceAgent")
_SECRETS_PROVIDER = secrets_factory(namespace="mfg", agent_name="SDSRevisionEvidenceAgent")

_MODE_OPERATIONS = {
    "invoke": entry_auth.Operation.READ_INVOKE,
    "ingest": entry_auth.Operation.WRITE_INGEST,
    "feedback": entry_auth.Operation.WRITE_FEEDBACK,
}

_SCRUB_POLICY = CheckpointScrubPolicy(
    safe_keys=frozenset({"mode", "case_ref", "rule_edition_ref", "submitted_at"}),
    scope_keys=frozenset({"tenant_id", "org_id"}),
    enum_value_keys={
        "mode": frozenset({"ingest", "invoke", "feedback"}),
    },
)


# ── Stage 5 smoke provisioning ───────────────────────────────────────────────
# Accepted residual risk (reviewed 2026-07-29): these markers are ordinary
# environment strings, so a deployment that copies the CI environment wholesale
# could in principle satisfy them.  The structurally stronger fix — a CI-only
# app factory, leaving this module unconditionally fail-closed — is not
# available today: the scaffold-distributed CI configuration launches
# `uvicorn src.api.server:app` verbatim, and every template repository shares
# that line.  Requiring three CI-only markers *plus* the auth token *plus* an
# empty store is the hardest posture reachable from the shipped entry point.
# Revisit if the distributed CI ever
# allows a separate smoke entry point.


def _is_provisional_stg_smoke_job() -> bool:
    """True only inside the ephemeral ``deploy-stg`` provisional smoke job.

    ``INVOKE_AUTH_TOKEN`` is NOT a staging discriminator — it is the ordinary
    standalone caller-auth token, so a real standalone deployment sets it too.
    Binding the scope fallback to it alone would let leaked or broadly-scoped
    ``STG_DEFAULT_*`` variables manufacture an authenticated tenant identity in
    production.  Require instead the provenance only that CI job has: a GitLab
    runner (``CI=true``), the job named exactly ``deploy-stg``, and the
    job-scoped ``STG_MOCK_MODE=true`` marking the in-job ephemeral deploy.
    """

    return (
        os.environ.get("CI") == "true"
        and os.environ.get("CI_JOB_NAME") == "deploy-stg"
        and os.environ.get("STG_MOCK_MODE") == "true"
    )


def _stg_smoke_scope() -> dict[str, str] | None:
    """Stage 5 smoke only: one placeholder tenant/org partition, else ``None``.

    Guards (all required): the process is the provisional ``deploy-stg`` job;
    ``INVOKE_AUTH_TOKEN`` is set so the smoke request is still token-
    authenticated; and both ``STG_DEFAULT_*`` values are present and clean.
    Outside that job this returns ``None``, so the shipped fail-closed posture —
    no caller scope, 403 — is unchanged for every real deployment.
    """

    if not _is_provisional_stg_smoke_job() or not os.environ.get("INVOKE_AUTH_TOKEN"):
        return None
    tenant_id = os.environ.get("STG_DEFAULT_TENANT_ID", "")
    org_id = os.environ.get("STG_DEFAULT_ORG_ID", "")
    if not _clean_scope_value(tenant_id) or not _clean_scope_value(org_id):
        return None
    return {"tenant_id": tenant_id, "org_id": org_id}


def _clean_scope_value(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and value == value.strip()
        and "\n" not in value
        and "\r" not in value
    )


def _verified_scope_or_stg_default(request: Request) -> dict[str, object]:
    """Resolve this AG's scope without exposing the part's broader STG fallback.

    The common adapter intentionally supports token-gated STG defaults. This AG
    additionally requires deploy-stg CI provenance, so an entirely unstamped
    request returns an invalid scope outside that job to prevent fallback.
    """

    unstamped = object()
    stamped = {
        key: getattr(request.state, key, unstamped)
        for key in entry_auth.SCOPE_KEYS
    }
    if all(value is unstamped for value in stamped.values()):
        return _stg_smoke_scope() or {key: None for key in entry_auth.SCOPE_KEYS}
    return {
        key: (None if value is unstamped else value)
        for key, value in stamped.items()
    }


app.state.verify_scope = _verified_scope_or_stg_default


def _provision_stg_smoke_case(config: dict) -> None:
    """Stage 5 smoke only: seed one placeholder case so the smoke can reach a node.

    ``pre_process`` resolves the case fail-closed for *every* mode, and the
    shipped store starts empty (``wire_services`` builds the in-memory default
    only when no store was injected; the customer's real case repository is
    supplied through that same seam).  Without a case the smoke can only ever
    prove the denial path.  Seeding is therefore bound to the same CI provenance
    as the scope fallback and additionally requires the store to be
    **completely empty** — a store holding any case is left untouched.

    Outside the provisional ``deploy-stg`` job this is a no-op, so no STG-only
    data can reach a production process.
    """

    scope = _stg_smoke_scope()
    if scope is None:
        return
    case_ref = os.environ.get("STG_DEFAULT_CASE_REF", "").strip()
    if not case_ref:
        return
    store = config.get("store")
    if store is None or getattr(store, "cases", None) != {}:
        return
    store.cases[case_ref] = {
        "tenant_id": scope["tenant_id"],
        "org_id": scope["org_id"],
        "revision_status": "draft",
        "rule_edition_ref": os.environ.get("STG_DEFAULT_RULE_EDITION_REF", "").strip(),
    }


_provision_stg_smoke_case(_RUNTIME_CONFIG)


class InvokeRequest(BaseModel):
    """The payload is stashed before graph invocation and never enters State."""

    mode: str
    case_ref: str
    payload: dict | list | str
    rule_edition_ref: str = ""
    session_id: str = Field(default="", max_length=128)


def _ctx(
    request: Request,
    *,
    operation: entry_auth.Operation,
    session_id: str,
    correlation_id: str,
):
    """Return verified invocation context.

    This template uses tenant_id/org_id as real customer-KB partition keys, so
    entry-adapter's verified-state scope seam is required.  Client headers are
    intentionally never used as a scope source.
    """

    auth = entry_auth.authenticate(request, operation=operation)
    return auth, InvocationContext(
        session_id=session_id,
        correlation_id=correlation_id,
        caller_trust_level=auth.trust,
        caller_id=auth.principal["caller_id"],
        hitl_allowed=False,
    )


def _build_agent(*, trusted_scope: dict[str, str], session_id: str) -> SdsRevisionEvidenceGraph:
    """Build one graph after authentication so trusted context is request-local."""

    config = {
        **_RUNTIME_CONFIG,
        "trusted_scope": dict(trusted_scope),
        "session_id": session_id,
    }
    request_agent = SdsRevisionEvidenceGraph(config=config)
    request_agent.compile()
    request_agent.provision_secrets(_SECRETS_PROVIDER)
    return request_agent


@app.post("/invoke")
async def invoke(req: InvokeRequest, request: Request):
    if req.mode not in {"ingest", "invoke", "feedback"}:
        raise HTTPException(status_code=422, detail="Unsupported mode.")
    session_id = req.session_id or str(uuid4())
    correlation_id = str(uuid4())
    operation = _MODE_OPERATIONS[req.mode]
    auth, ctx = _ctx(
        request,
        operation=operation,
        session_id=session_id,
        correlation_id=correlation_id,
    )
    scope = auth.scope
    request_agent = _build_agent(trusted_scope=scope, session_id=session_id)
    raw_envelope = {
        "mode": req.mode,
        "tenant_id": scope["tenant_id"],
        "org_id": scope["org_id"],
        "case_ref": req.case_ref,
        "rule_edition_ref": req.rule_edition_ref,
        "payload": req.payload,
    }
    store = _RUNTIME_CONFIG["store"]
    # Run the common allowlist scrubber before invocation.  Its temporary
    # payload is discarded below because SDS review needs the original document
    # as one opaque payload value rather than row-level scrub fragments.
    scrubbed = scrub_checkpoint_envelope(
        json.dumps(raw_envelope, ensure_ascii=False, default=str),
        store.payloads.raw_store,
        session_id=session_id,
        trusted_scope=scope,
        policy=_SCRUB_POLICY,
    )
    scrubbed_ref = json.loads(scrubbed)["payload_ref"]
    payload_ref = store.payloads.raw_store.put(
        json.dumps(req.payload, ensure_ascii=False, default=str), scope=scope, session_id=session_id
    )
    store.payloads.raw_store.delete(scrubbed_ref, scope=scope, session_id=session_id)
    canonical = json.dumps(
        {
            "mode": req.mode,
            "tenant_id": scope["tenant_id"],
            "org_id": scope["org_id"],
            "case_ref": req.case_ref,
            "payload_ref": payload_ref,
            **({"rule_edition_ref": req.rule_edition_ref} if req.rule_edition_ref else {}),
        },
        ensure_ascii=False,
    )
    # All client-supplied payload_ref values were removed by the scrubber.  The
    # lease binds only this newly minted ref to one invocation and its case.
    store.payloads.bind(
        payload_ref,
        tenant_id=scope["tenant_id"],
        org_id=scope["org_id"],
        case_ref=req.case_ref,
        session_id=session_id,
        correlation_id=correlation_id,
    )
    try:
        with bound_secrets(request_agent._secrets_provider):
            return request_agent.invoke(
                canonical,
                ctx=ctx,
                input_context={"configurable": {"tenant_id": scope["tenant_id"], "org_id": scope["org_id"]}},
            )
    finally:
        store.payloads.close(
            payload_ref,
            tenant_id=scope["tenant_id"],
            org_id=scope["org_id"],
            case_ref=req.case_ref,
            session_id=session_id,
            correlation_id=correlation_id,
        )


@app.get("/health")
def health():
    return {"status": "ok", "agent": "SDSRevisionEvidenceAgent"}
