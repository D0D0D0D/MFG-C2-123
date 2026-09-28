"""Outer input validation and authoritative SDS case resolution."""

from __future__ import annotations

import json
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.payload_store import is_reference
from src.services.service import ALLOWED_REVISION_STATUSES, SdsRevisionStore


_MODES = frozenset({"invoke", "ingest", "feedback"})
_ENVELOPE_KEYS = frozenset({"mode", "tenant_id", "org_id", "case_ref", "payload_ref", "rule_edition_ref"})


class PreProcessNode(FunctionNode):
    """Validate only the server-scrubbed, non-sensitive invocation envelope."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, *, store: SdsRevisionStore | None = None) -> None:
        super().__init__()
        self._store = store or SdsRevisionStore()

    def _extra_security_gate_input(self, state: dict) -> dict:
        """Fail closed before the inner graph can receive an unsafe envelope."""

        raw = state.get("user_input")
        try:
            envelope = json.loads(raw) if isinstance(raw, str) else None
        except json.JSONDecodeError:
            envelope = None
        if not isinstance(envelope, dict) or set(envelope) - _ENVELOPE_KEYS:
            return _input_error(state, "canonical SDS envelope is missing or contains undeclared fields")
        mode = envelope.get("mode")
        if mode not in _MODES:
            return _input_error(state, "mode is not one of invoke, ingest, feedback")
        for key in ("tenant_id", "org_id", "case_ref"):
            if not _clean_identifier(envelope.get(key)):
                return _input_error(state, f"{key} is missing or malformed")
        # payload_ref is deliberately never accepted from a caller as a raw
        # value: the entry adapter has already discarded it and minted this
        # letter-only reference while scrubbing the request body.
        if not is_reference(envelope.get("payload_ref")):
            return _input_error(state, "payload_ref is not a server-issued reference")
        edition = envelope.get("rule_edition_ref")
        if edition is not None and not _clean_identifier(edition):
            return _input_error(state, "rule_edition_ref is malformed")
        return state

    def execute(self, state: dict) -> dict:
        envelope = json.loads(state["user_input"])
        tenant_id = envelope["tenant_id"]
        org_id = envelope["org_id"]
        case_ref = envelope["case_ref"]
        case = self._store.resolve_case(tenant_id=tenant_id, org_id=org_id, case_ref=case_ref)
        if case is None:
            emit_trace_event("sds_case_scope_denied", {"case_ref": case_ref}, state)
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["SDS case is not available in the verified tenant/org scope"],
            }
        status = case.get("revision_status")
        if status not in ALLOWED_REVISION_STATUSES:
            emit_trace_event("sds_revision_status_denied", {"case_ref": case_ref, "status": status}, state)
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["SDS revision is not an approval-pending draft"],
            }

        mode = envelope["mode"]
        rule_edition_ref = envelope.get("rule_edition_ref") or case.get("rule_edition_ref")
        if mode == "invoke" and not _clean_identifier(rule_edition_ref):
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["invoke requires an authoritative rule_edition_ref"],
            }
        validated_input: dict[str, Any] = {
            "mode": mode,
            "case_ref": case_ref,
            "revision_status": status,
        }
        if rule_edition_ref:
            validated_input["rule_edition_ref"] = rule_edition_ref
        emit_trace_event("sds_envelope_validated", {"mode": mode, "case_ref": case_ref}, state)
        return {
            "mode": mode,
            "tenant_id": tenant_id,
            "org_id": org_id,
            "case_ref": case_ref,
            "payload_ref": envelope["payload_ref"],
            "rule_edition_ref": rule_edition_ref,
            "validated_input": validated_input,
            "status": AgentStatus.SUCCESS.value,
        }


def _clean_identifier(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and value == value.strip()
        and "\n" not in value
        and "\r" not in value
        and len(value) <= 64
    )


def _input_error(state: dict, message: str) -> dict:
    rejected = dict(state)
    rejected["status"] = AgentStatus.ERROR.value
    rejected["error_log"] = [f"PreProcessNode S-2: {message}"]
    return rejected
