"""Flat, checkpoint-safe state contracts for SDS revision evidence work."""

from __future__ import annotations

from typing import NotRequired

from framework.schemas.agent_state import AgentState


class SdsRevisionState(AgentState, total=False):
    """Outer graph state.

    Values deliberately remain JSON-shaped.  In particular, neither request
    bodies nor service/credential objects may be added here because this state
    is eligible for framework checkpointing.
    """

    mode: str
    tenant_id: str
    org_id: str
    case_ref: str
    validated_input: dict
    payload_ref: str
    rule_edition_ref: str
    sections_present: dict
    format_check: dict
    recall_refs: list[dict]
    retrieved_records: list[dict]
    case_sources: list[dict]
    source_catalog: list[dict]
    llm_findings: dict
    revision_brief: dict
    evidence_map: list[dict]
    cited_source_ids: list[str]
    requires_human_review: bool
    degradation_reason: list[str]
    ingest_summary: dict
    feedback_summary: dict
    gate_kind: str
    # Structured failure metadata. These MUST be declared here: LangGraph
    # derives its state channels from this TypedDict, so a key a node returns
    # but the schema does not declare is silently discarded — which is how a
    # node-level error_code reaches the caller as None.
    error_code: NotRequired[str]
    error_type: NotRequired[str]


class SdsRevisionWorkflowState(SdsRevisionState, total=False):
    """Inner Cat2 workflow state; kept distinct for routing annotations."""

    _stage1_record: dict


# Compatibility import for MR-A scaffold consumers; new code uses the explicit
# SDS state name above.
State = SdsRevisionState
