"""Injected domain services and short-lived SDS payload leases.

The template owns only the narrow interfaces used by its nodes.  Production
deployments inject DB, retrieval, and LLM implementations through
``wire_services``; the in-memory defaults are intentionally deterministic and
are useful for local execution and tests only.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from src.payload_store import PayloadStore, PayloadStoreError


ALLOWED_REVISION_STATUSES = frozenset({"draft", "under_internal_review", "pending_approval"})


@dataclass(frozen=True)
class PayloadLease:
    """Authoritative bindings for a server-issued payload reference."""

    tenant_id: str
    org_id: str
    case_ref: str
    session_id: str
    correlation_id: str


class SdsPayloadLeaseStore:
    """Adds case/correlation read-lease binding around the supplied part."""

    def __init__(self, payload_store: PayloadStore | None = None) -> None:
        self._payload_store = payload_store or PayloadStore(scope_keys=("tenant_id", "org_id"), ttl_seconds=300)
        self._leases: dict[str, PayloadLease] = {}

    @property
    def raw_store(self) -> PayloadStore:
        """Expose the common part to the entry scrubber and row pipeline."""

        return self._payload_store

    def bind(
        self,
        ref: str,
        *,
        tenant_id: str,
        org_id: str,
        case_ref: str,
        session_id: str,
        correlation_id: str,
    ) -> None:
        self._leases[ref] = PayloadLease(tenant_id, org_id, case_ref, session_id, correlation_id)

    def resolve(
        self,
        ref: str,
        *,
        tenant_id: str,
        org_id: str,
        case_ref: str,
        session_id: str,
        correlation_id: str,
    ) -> Any:
        lease = self._leases.get(ref)
        expected = PayloadLease(tenant_id, org_id, case_ref, session_id, correlation_id)
        if lease != expected:
            raise PayloadStoreError("payload_ref_scope_mismatch")
        # A read lease intentionally permits more than one resolve within this
        # invocation (Stage 1, Stage 3, and provenance/display preparation).
        return self._payload_store.resolve(
            ref,
            scope={"tenant_id": tenant_id, "org_id": org_id},
            session_id=session_id,
            consume=False,
        )

    def close(
        self,
        ref: str,
        *,
        tenant_id: str,
        org_id: str,
        case_ref: str,
        session_id: str,
        correlation_id: str,
    ) -> None:
        lease = self._leases.get(ref)
        expected = PayloadLease(tenant_id, org_id, case_ref, session_id, correlation_id)
        if lease != expected:
            raise PayloadStoreError("payload_ref_scope_mismatch")
        self._payload_store.delete(ref, scope={"tenant_id": tenant_id, "org_id": org_id}, session_id=session_id)
        self._leases.pop(ref, None)


@dataclass
class SdsRevisionStore:
    """Narrow repository seam used by stages; implementations are injectable."""

    cases: dict[str, dict[str, Any]] = field(default_factory=dict)
    payloads: SdsPayloadLeaseStore = field(default_factory=SdsPayloadLeaseStore)
    ingested_rows: list[dict[str, Any]] = field(default_factory=list)
    feedback_events: list[dict[str, Any]] = field(default_factory=list)

    def resolve_case(self, *, tenant_id: str, org_id: str, case_ref: str) -> dict[str, Any] | None:
        case = self.cases.get(case_ref)
        if case is None:
            # The case table is authoritative for both status and scope.  A
            # missing record must never be promoted to a synthetic draft.
            return None
        if case.get("tenant_id") != tenant_id or case.get("org_id") != org_id:
            return None
        return dict(case)

    def resolve_payload(self, **scope: str) -> Any:
        return self.payloads.resolve(
            scope["payload_ref"],
            tenant_id=scope["tenant_id"],
            org_id=scope["org_id"],
            case_ref=scope["case_ref"],
            session_id=scope["session_id"],
            correlation_id=scope["correlation_id"],
        )

    def write_perspective(self, state: dict, row: dict[str, Any]) -> str:
        verified_scope = {
            "tenant_id": state.get("tenant_id"),
            "org_id": state.get("org_id"),
            "case_ref": state.get("case_ref"),
            "rule_edition_ref": state.get("rule_edition_ref"),
        }
        if any(not _clean_scope_value(value) for value in verified_scope.values()):
            raise ValueError("review perspective write requires verified tenant/org/case/rule scope")
        record_id = f"perspective-{len(self.ingested_rows) + 1}"
        # Verified State owns partition fields.  A row's self-reported scope
        # must not survive sanitization or override the authenticated scope.
        self.ingested_rows.append({**row, "record_id": record_id, **verified_scope})
        return record_id

    def apply_feedback(self, state: dict, feedback: dict[str, Any]) -> str:
        event_id = f"feedback-{len(self.feedback_events) + 1}"
        self.feedback_events.append(
            {
                "event_id": event_id,
                "tenant_id": state["tenant_id"],
                "org_id": state["org_id"],
                "case_ref": state["case_ref"],
                "approved": True,
                "feedback": dict(feedback),
            }
        )
        return event_id


def wire_services(runtime_config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Return a fully wired runtime config without selecting concrete vendors.

    ``llm`` and ``retrieval`` remain injection seams; an absent LLM is a
    supported deterministic fallback, while a store is always supplied so a
    first invocation cannot fail solely because server startup omitted wiring.
    """

    config = dict(runtime_config or {})
    config.setdefault("llm", None)
    config.setdefault("retrieval", None)
    if config.get("store") is None:
        config["store"] = SdsRevisionStore()
    config.setdefault("rule_edition_ref", "configured-rule-edition")
    return config


def _clean_scope_value(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and value == value.strip()
        and "\n" not in value
        and "\r" not in value
    )
