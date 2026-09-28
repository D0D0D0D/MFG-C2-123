"""Mode-scoped public output envelope for SDS revision evidence."""

from __future__ import annotations

from typing import Any, ClassVar

from framework.errors import SecurityViolationError
from framework.schemas.trust_level import TrustLevel
from shared.security.credential_detector import detect_credentials
from shared.utils.audit_logger import emit_trace_event
from src.output_envelope import OutputEnvelopeNode


class PostProcessNode(OutputEnvelopeNode):
    """Project only the reviewed mode result into framework ``output``."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL
    gate_kind_by_mode: ClassVar[dict[str, str]] = {
        "invoke": "invoke",
        "ingest": "ingest",
        "feedback": "feedback",
    }
    required_fields_by_mode: ClassVar[dict[str, tuple[str, ...]]] = {
        "invoke": ("mode", "gate_kind", "revision_brief", "evidence_map", "requires_human_review"),
        "ingest": ("mode", "gate_kind", "ingest_summary"),
        "feedback": ("mode", "gate_kind", "feedback_summary"),
    }

    def execute(self, state: dict) -> dict:
        result = super().execute(state)
        emit_trace_event("sds_output_envelope_ready", {"mode": state.get("mode", "")}, state)
        return result

    def format_invoke(self, state: dict) -> dict[str, Any]:
        return {
            "revision_brief": state.get("revision_brief", {}),
            "evidence_map": state.get("evidence_map", []),
            "requires_human_review": True,
        }

    def format_ingest(self, state: dict) -> dict[str, Any]:
        return {"ingest_summary": state.get("ingest_summary", {})}

    def format_feedback(self, state: dict) -> dict[str, Any]:
        return {"feedback_summary": state.get("feedback_summary", {})}

    def _sanitize_client_echoes(self, formatted: dict[str, Any]) -> dict[str, Any]:
        if _contains_forbidden_internal_key(formatted):
            raise SecurityViolationError("post-process output contains an internal verifier or payload reference")
        _scan_credentials(formatted)
        return super()._sanitize_client_echoes(formatted)


def _contains_forbidden_internal_key(value: Any) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            if key.startswith("_verify_") or key == "payload_ref":
                return True
            if _contains_forbidden_internal_key(child):
                return True
    elif isinstance(value, list):
        return any(_contains_forbidden_internal_key(child) for child in value)
    return False


def _scan_credentials(value: Any) -> None:
    if isinstance(value, str):
        found = detect_credentials(value)
        if found:
            raise SecurityViolationError(f"credential pattern {found[0]['type']} detected in formatted output")
    elif isinstance(value, dict):
        for child in value.values():
            _scan_credentials(child)
    elif isinstance(value, list):
        for child in value:
            _scan_credentials(child)
