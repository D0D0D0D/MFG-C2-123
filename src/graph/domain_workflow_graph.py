"""Inner fixed workflow for SDS revision evidence generation.

The workflow keeps raw SDS material behind ``SdsPayloadLeaseStore``.  Only
opaque references, source locators, and bounded public findings cross its
GraphNode boundary.
"""

from __future__ import annotations

import json
import unicodedata
from collections.abc import Mapping
from typing import Any, ClassVar

from framework.errors import ConfigError, SecurityViolationError
from framework.graph.base_graph import BaseGraph
from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from langgraph.graph import END, START
from shared.utils.audit_logger import emit_trace_event
from src.ingest_row_pipeline import IngestRowPipelineNode, RowPipelineHooks
from src.llm_draft_stage import DraftStageHooks, LlmDraftStageNode
from src.scoped_retrieval import ScopedRetrievalNode
from src.schemas.state import SdsRevisionWorkflowState
from src.services.service import SdsRevisionStore


DEGRADATION_VALUES = frozenset(
    {"kb_empty", "kb_low_quality", "llm_unavailable", "llm_schema_violation", "partial_sections_submitted"}
)
OBSERVATION_KINDS = frozenset(
    {"missing_evidence", "section_mismatch", "unsupported_claim", "stale_reference", "format_deviation"}
)
GAP_KINDS = frozenset(
    {
        "no_supporting_source",
        "source_predates_revision",
        "source_scope_mismatch",
        "quantitative_value_unsupported",
        "supplier_document_missing",
    }
)
SUPPORT_STATUSES = frozenset({"supported", "partially_supported", "unsupported"})
QUESTION_KINDS = frozenset(
    {
        "confirm_source_currency",
        "confirm_supplier_document_scope",
        "confirm_quantitative_basis",
        "confirm_section_alignment",
        "confirm_sop_step_applied",
    }
)
FINDING_CATEGORIES = ("sections", "revision_claims", "gaps", "inconsistencies", "verification_questions")
# Key kinds, stated explicitly rather than inferred from a name prefix: a future
# prose key such as ``source_id_note`` would be silently classified as a source
# reference, and a key in none of these sets must fail loudly at build time.
MIN_SECTION, MAX_SECTION = 1, 16  # GHS SDS section numbering
SECTION_KEYS = frozenset({"section_no", "section_no_a", "section_no_b"})
SOURCE_ID_KEYS = frozenset({"source_id", "source_id_a", "source_id_b"})
# Category -> (enum field, allowed values).  ``inconsistencies`` carries no enum.
CATEGORY_ENUMS: dict[str, tuple[str, frozenset[str]]] = {
    "sections": ("observation_kind", OBSERVATION_KINDS),
    "revision_claims": ("support_status", SUPPORT_STATUSES),
    "gaps": ("gap_kind", GAP_KINDS),
    "verification_questions": ("question_kind", QUESTION_KINDS),
}
# Prose ceilings enforced by ``_valid_raw_item``; surfaced to the model so it
# does not have to guess where a finding will be rejected for length.
PROSE_LIMITS = {"observation": 1000, "claim_text": 1000, "description": 1000, "question": 500}


class DeterministicCheckNode(FunctionNode):
    """Stage 1: construct the current-case source registry without conclusions."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, *, store: SdsRevisionStore) -> None:
        super().__init__()
        self._store = store

    def execute(self, state: dict) -> dict:
        envelope = _parse_envelope(state)
        if envelope is None:
            return _error("inner canonical envelope is malformed")
        try:
            payload = self._store.resolve_payload(
                payload_ref=envelope["payload_ref"],
                tenant_id=envelope["tenant_id"],
                org_id=envelope["org_id"],
                case_ref=envelope["case_ref"],
                session_id=state["session_id"],
                correlation_id=state["correlation_id"],
            )
        except Exception as exc:  # Payload errors are intentionally fail-closed.
            return _error(f"SDS payload reference cannot be resolved: {exc}")
        payload_data = _coerce_payload(payload)
        if payload_data is None:
            return _error("SDS payload is not a supported structured document")

        sections = _sections_from_payload(payload_data)
        try:
            sources, catalog = _build_case_sources(payload_data, sections, envelope)
        except SecurityViolationError as exc:
            return _error(f"supplier source registry rejected: {exc}")
        present = {str(index): index in sections for index in range(1, 17)}
        format_check = {
            "section_numbers_valid": all(1 <= index <= 16 for index in sections),
            "revision_history_present": bool(payload_data.get("revision_history")),
        }
        degradation = ["partial_sections_submitted"] if len(sections) < 16 else []
        emit_trace_event(
            "sds_deterministic_check_complete",
            {"case_ref": envelope["case_ref"], "source_count": len(sources), "section_count": len(sections)},
            state,
        )
        return {
            "mode": envelope["mode"],
            "tenant_id": envelope["tenant_id"],
            "org_id": envelope["org_id"],
            "case_ref": envelope["case_ref"],
            "payload_ref": envelope["payload_ref"],
            "rule_edition_ref": envelope.get("rule_edition_ref"),
            "validated_input": envelope.get("validated_input", {}),
            "sections_present": present,
            "format_check": format_check,
            "case_sources": sources,
            "source_catalog": catalog,
            "_stage1_record": {"source_count": len(sources), "section_count": len(sections)},
            "status": AgentStatus.SUCCESS.value,
            **({"degradation_reason": degradation} if degradation else {}),
        }


class KbRecallNode(ScopedRetrievalNode):
    """Stage 2: scoped recall of non-conclusive historical perspectives."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict) -> dict:
        result = super().execute(state)
        if result.get("status") == AgentStatus.ERROR.value:
            return result
        records = result.get("retrieved_records", [])
        recall_refs = [
            {
                "perspective_ref": record.get("source_id"),
                "section_no": record.get("metadata", {}).get("section_no"),
                "provenance": record.get("metadata", {}).get("source_doc", "internal_kb"),
            }
            for record in records
            if isinstance(record, dict)
        ]
        degradation = _degradation_list(state.get("degradation_reason"))
        if not records:
            degradation.append("kb_empty")
        if any(record.get("metadata", {}).get("quality_flag") == "low" for record in records if isinstance(record, dict)):
            degradation.append("kb_low_quality")
        emit_trace_event("sds_kb_recall_complete", {"recall_count": len(recall_refs)}, state)
        return {
            "retrieved_records": records,
            "retrieval_rejected": result.get("retrieval_rejected", []),
            "recall_refs": recall_refs,
            "status": AgentStatus.SUCCESS.value,
            **({"degradation_reason": _unique(degradation)} if degradation else {}),
        }


class RevisionConsistencyLlmNode(LlmDraftStageNode):
    """Stage 3: bounded-prose LLM findings with catalog-bound source IDs."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, *, config: dict[str, Any], store: SdsRevisionStore) -> None:
        self._store = store
        super().__init__(
            config=config,
            hooks=DraftStageHooks(
                build_prompt=lambda state: _build_prompt(state, store),
                build_fallback=_build_fallback,
                build_evidence=lambda _state, _draft: ([], []),
            ),
        )

    def execute(self, state: dict) -> dict:
        # Resolve inside the same lease before drafting.  The raw value is used
        # only in this stack frame; the prompt hook resolves it again to create
        # an ephemeral excerpt catalog and never writes that text into State.
        try:
            self._store.resolve_payload(
                payload_ref=state["payload_ref"],
                tenant_id=state["tenant_id"],
                org_id=state["org_id"],
                case_ref=state["case_ref"],
                session_id=state["session_id"],
                correlation_id=state["correlation_id"],
            )
        except Exception as exc:
            return _error(f"SDS payload read lease failed before drafting: {exc}")

        result = super().execute(state)
        if result.get("status") == AgentStatus.ERROR.value:
            return result
        draft = result.get("draft")
        if not isinstance(draft, dict):
            return _error("LLM draft is not a JSON object")
        try:
            findings, dropped = _validate_and_filter_llm_findings(draft, state.get("source_catalog", []))
        except SecurityViolationError as exc:
            return _error(f"LLM response schema violation: {exc}")
        degradation = _degradation_list(state.get("degradation_reason"))
        raw_reason = result.get("degradation_reason")
        if raw_reason:
            degradation.append("llm_unavailable")
        if dropped:
            degradation.append("llm_schema_violation")
        emit_trace_event(
            "sds_revision_draft_complete",
            {"finding_count": _finding_count(findings), "dropped_count": dropped},
            state,
        )
        return {
            "llm_findings": findings,
            "status": AgentStatus.SUCCESS.value,
            **({"degradation_reason": _unique(degradation)} if degradation else {}),
        }


class ClaimSourceVerifyNode(FunctionNode):
    """Stage 4: inject authoritative provenance and deterministic finding IDs."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, *, store: SdsRevisionStore) -> None:
        super().__init__()
        self._store = store

    def execute(self, state: dict) -> dict:
        # Third permitted resolve in the invocation read lease.  A locator may
        # be displayed only after this authoritative source preparation step.
        try:
            self._store.resolve_payload(
                payload_ref=state["payload_ref"],
                tenant_id=state["tenant_id"],
                org_id=state["org_id"],
                case_ref=state["case_ref"],
                session_id=state["session_id"],
                correlation_id=state["correlation_id"],
            )
        except Exception as exc:
            return _error(f"SDS payload read lease failed during provenance preparation: {exc}")
        try:
            brief, evidence, cited = _render_verified_brief(
                state.get("llm_findings", {}), state.get("case_sources", [])
            )
        except SecurityViolationError as exc:
            return _error(f"claim/source verification failed: {exc}")
        emit_trace_event("sds_claim_source_verified", {"finding_count": len(cited)}, state)
        result: dict[str, Any] = {
            "revision_brief": brief,
            "evidence_map": evidence,
            "cited_source_ids": cited,
            "case_sources": state.get("case_sources", []),
            "requires_human_review": True,
            "status": AgentStatus.SUCCESS.value,
        }
        degradation = _degradation_list(state.get("degradation_reason"))
        if degradation:
            result["degradation_reason"] = _unique(degradation)
        return result


class AccumulateIngestNode(IngestRowPipelineNode):
    """Ingest SOP/review-perspective rows through the supplied pipeline part."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL
    required_fields: ClassVar[tuple[str, ...]] = ("perspective_ref", "section_no", "rule_edition_ref")
    required_any_fields: ClassVar[tuple[tuple[str, ...], ...]] = (("perspective_text",),)

    def __init__(
        self,
        *,
        store: SdsRevisionStore,
        trusted_scope: Mapping[str, str] | None = None,
        session_id: str | None = None,
    ) -> None:
        self._store = store
        super().__init__(
            payload_store=store.payloads.raw_store,
            trusted_scope=trusted_scope,
            session_id=session_id,
            hooks=RowPipelineHooks(
                sanitize=_sanitize_ingest_row,
                normalize=_normalize_ingest_row,
                write=store.write_perspective,
            )
        )

    def execute(self, state: dict) -> dict:
        raw = super().execute(state)
        if raw.get("status") == AgentStatus.ERROR.value:
            return raw
        source = raw.get("ingest_summary", {})
        rejected = []
        for item in source.get("rejected", []):
            reasons = item.get("reasons", []) if isinstance(item, dict) else []
            reason = "missing_required_field" if any(str(value).startswith("missing_") for value in reasons) else "quality_gate_failed"
            rejected.append({"row_ref": str(item.get("index", "unknown")), "reason_kind": reason})
        summary = {"written_ids": source.get("written_ids", []), "rejected": rejected}
        emit_trace_event("sds_ingest_accumulated", {"written_count": len(summary["written_ids"])}, state)
        return {"ingest_summary": summary, "status": AgentStatus.SUCCESS.value}


class AccumulateFeedbackNode(FunctionNode):
    """Persist explicitly approved feedback without using an LLM."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, *, store: SdsRevisionStore) -> None:
        super().__init__()
        self._store = store

    def execute(self, state: dict) -> dict:
        try:
            payload = self._store.resolve_payload(
                payload_ref=state["payload_ref"],
                tenant_id=state["tenant_id"],
                org_id=state["org_id"],
                case_ref=state["case_ref"],
                session_id=state["session_id"],
                correlation_id=state["correlation_id"],
            )
        except Exception as exc:
            return _error(f"feedback payload cannot be resolved: {exc}")
        feedback = _coerce_payload(payload)
        if not isinstance(feedback, dict) or feedback.get("approved") is not True:
            return _error("feedback requires explicit approved=true")
        event_id = self._store.apply_feedback(state, feedback)
        emit_trace_event("sds_feedback_accumulated", {"event_id": event_id}, state)
        return {
            "feedback_summary": {"applied_count": 1, "audit_event_ids": [event_id]},
            "status": AgentStatus.SUCCESS.value,
        }


class SdsRevisionWorkflowGraph(BaseGraph):
    """The seven-method ``BaseGraph`` implementation for the Cat2 inner flow."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config=config)

    @property
    def name(self) -> str:
        return "sds_revision_workflow"

    @property
    def state_schema(self) -> type:
        return SdsRevisionWorkflowState

    def _validate_config(self) -> None:
        required = ("llm", "store", "retrieval", "top_k", "rule_edition_ref")
        missing = [key for key in required if key not in self.config]
        if missing:
            raise ConfigError(f"SdsRevisionWorkflowGraph requires config keys: {missing}")
        if not isinstance(self.config["top_k"], int) or self.config["top_k"] < 1:
            raise ConfigError("SdsRevisionWorkflowGraph top_k must be a positive integer")
        required_store_methods = ("resolve_case", "resolve_payload", "write_perspective", "apply_feedback")
        if any(not callable(getattr(self.config["store"], method, None)) for method in required_store_methods):
            raise ConfigError("SdsRevisionWorkflowGraph store does not provide the required repository seam")

    def _extra_initial_state(self) -> dict:
        # START routing needs the declared mode before any node runs.  The
        # canonical envelope itself contains no SDS body.
        return {}

    def register_nodes(self) -> None:
        store = self.config["store"]
        self._nodes = {
            "deterministic_check": DeterministicCheckNode(store=store),
            "kb_recall": KbRecallNode(
                scope_keys=("tenant_id", "org_id", "rule_edition_ref"),
                backend=self.config["retrieval"],
                normalize_query=_normalize_query,
                top_k=self.config["top_k"],
            ),
            "revision_consistency_llm": RevisionConsistencyLlmNode(config=self.config, store=store),
            "claim_source_verify": ClaimSourceVerifyNode(store=store),
            "accumulate_ingest": AccumulateIngestNode(
                store=store,
                trusted_scope=self.config.get("trusted_scope"),
                session_id=self.config.get("session_id"),
            ),
            "accumulate_feedback": AccumulateFeedbackNode(store=store),
        }

    def add_edges(self) -> None:
        self._sg.add_conditional_edges(START, self.route)
        self._sg.add_conditional_edges("deterministic_check", self._route_after_deterministic_check)
        self._sg.add_edge("kb_recall", "revision_consistency_llm")
        self._sg.add_edge("revision_consistency_llm", "claim_source_verify")
        self._sg.add_edge("claim_source_verify", END)
        self._sg.add_edge("accumulate_ingest", END)
        self._sg.add_edge("accumulate_feedback", END)

    def route(self, state: SdsRevisionWorkflowState) -> str:
        if state.get("status") == AgentStatus.ERROR.value:
            return END
        envelope = _parse_envelope(state)
        if envelope is None:
            return END
        mode = envelope.get("mode")
        if mode == "invoke":
            return "deterministic_check"
        if mode == "ingest":
            return "deterministic_check"
        if mode == "feedback":
            return "accumulate_feedback"
        return END

    def _route_after_deterministic_check(self, state: SdsRevisionWorkflowState) -> str:
        if state.get("status") == AgentStatus.ERROR.value:
            return END
        if state.get("mode") == "invoke":
            return "kb_recall"
        if state.get("mode") == "ingest":
            return "accumulate_ingest"
        return END

    def get_output(self, state: SdsRevisionWorkflowState) -> dict:
        mode = state.get("mode")
        result: dict[str, Any] = {"status": state.get("status", AgentStatus.ERROR.value)}
        if mode == "invoke":
            for key in (
                "revision_brief",
                "evidence_map",
                "cited_source_ids",
                "case_sources",
                "requires_human_review",
                "degradation_reason",
            ):
                if key in state:
                    result[key] = state[key]
        elif mode == "ingest" and "ingest_summary" in state:
            result["ingest_summary"] = state["ingest_summary"]
        elif mode == "feedback" and "feedback_summary" in state:
            result["feedback_summary"] = state["feedback_summary"]
        for key in ("error_code", "error_type"):
            if state.get(key):
                result[key] = state[key]
        if state.get("error_log"):
            result["error_log"] = state["error_log"]
        return result


def _parse_envelope(state: Mapping[str, Any]) -> dict[str, Any] | None:
    raw = state.get("user_input")
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else None
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _coerce_payload(payload: Any) -> dict[str, Any] | None:
    parsed = _coerce_json_value(payload)
    return parsed if isinstance(parsed, dict) else None


def _coerce_json_value(payload: Any) -> Any:
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, list):
        return payload
    if isinstance(payload, str):
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            return None
    return None


def _sections_from_payload(payload: Mapping[str, Any]) -> dict[int, str]:
    raw = payload.get("sections", {})
    sections: dict[int, str] = {}
    if isinstance(raw, Mapping):
        entries = raw.items()
    elif isinstance(raw, list):
        entries = ((item.get("section_no"), item.get("text")) for item in raw if isinstance(item, Mapping))
    else:
        entries = ()
    for number, text in entries:
        try:
            section_no = int(number)
        except (TypeError, ValueError):
            continue
        if 1 <= section_no <= 16 and isinstance(text, str) and text.strip():
            sections[section_no] = unicodedata.normalize("NFKC", text).strip()
    return sections


def _build_case_sources(
    payload: Mapping[str, Any], sections: Mapping[int, str], envelope: Mapping[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    sources: list[dict[str, Any]] = []
    catalog: list[dict[str, Any]] = []
    draft_ids = {f"src-draft-sec{section_no:02d}" for section_no in sections}
    for section_no in sorted(sections):
        source_id = f"src-draft-sec{section_no:02d}"
        source = _source_record(source_id, section_no, f"sections.{section_no}", "revision_draft", envelope)
        sources.append(source)
        catalog.append({"source_id": source_id, "section_no": section_no, "source_kind": "revision_draft"})
    supplied = payload.get("supplier_sources", [])
    if isinstance(supplied, list):
        declared_ids = [item.get("source_id") for item in supplied if isinstance(item, Mapping) and item.get("source_id")]
        if any(not isinstance(source_id, str) for source_id in declared_ids):
            raise SecurityViolationError("supplier source_id must be a string when supplied")
        if len(declared_ids) != len(set(declared_ids)) or set(declared_ids) & draft_ids:
            raise SecurityViolationError("supplier source_id duplicates or collides with a server source")
        for index, item in enumerate(supplied, start=1):
            if not isinstance(item, Mapping):
                continue
            # Source identity and provenance are server-owned.  Client
            # literals can contain supplier names/composition and must never
            # become an authoritative case registry value or public evidence.
            source_id = f"src-supplier-{index:03d}"
            section_no = item.get("section_no", 1)
            if not isinstance(section_no, int) or isinstance(section_no, bool) or not 1 <= section_no <= 16:
                section_no = 1
            source = _source_record(
                source_id,
                section_no,
                f"supplier_source.{index}",
                "supplier_evidence",
                envelope,
            )
            source["provenance"] = "supplier_source_record"
            sources.append(source)
            catalog.append({"source_id": source_id, "section_no": section_no, "source_kind": "supplier_evidence"})
    return sources, catalog


def _source_record(source_id: str, section_no: int, locator: str, source_kind: str, envelope: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "source_id": source_id,
        "section_no": section_no,
        "span_locator": locator,
        "provenance": source_kind,
        "source_kind": source_kind,
        "tenant_id": envelope["tenant_id"],
        "org_id": envelope["org_id"],
        "case_ref": envelope["case_ref"],
    }


def _normalize_query(value: Any) -> str:
    if isinstance(value, Mapping):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return str(value or "")


def _response_contract() -> dict[str, Any]:
    """Describe the closed response schema that ``_validate_and_filter_llm_findings`` enforces.

    Built from the very constants the validator checks against — ``_raw_item_keys``,
    ``CATEGORY_ENUMS`` and ``PROSE_LIMITS`` — so the prompt cannot drift away from
    what is actually accepted.  Naming the categories alone is not enough: a live
    model has no way to guess the field names, answers in its own shape, and every
    finding is then rejected as a schema violation (observed 2026-08-12 against a
    real provider, where a materially correct review was discarded wholesale).
    """
    item_schema: dict[str, dict[str, Any]] = {}
    for category in FINDING_CATEGORIES:
        enum_entry = CATEGORY_ENUMS.get(category)
        spec: dict[str, Any] = {}
        for key in sorted(_raw_item_keys(category)):
            if enum_entry is not None and key == enum_entry[0]:
                spec[key] = sorted(enum_entry[1])
            elif key in SECTION_KEYS:
                spec[key] = f"integer, GHS section {MIN_SECTION}..{MAX_SECTION}"
            elif key in SOURCE_ID_KEYS:
                spec[key] = "must be one of the source_id values listed in source_catalog"
            elif key in PROSE_LIMITS:
                spec[key] = f"non-blank prose, at most {PROSE_LIMITS[key]} characters"
            else:  # pragma: no cover - guarded by test_every_validated_key_is_classified
                raise ConfigError(f"response contract cannot describe unclassified key {key!r}")
        item_schema[category] = spec
    return {
        "all_free_text_requires_source_id": True,
        "allowed_categories": list(FINDING_CATEGORIES),
        "output_format": (
            "Return only a JSON object whose top-level keys are exactly the five categories below. "
            "Each category holds an array of objects whose keys are exactly those given in item_schema — "
            "no extra keys, no missing keys. Cite a source by placing its source_id in the source-id field "
            "named for that category (source_id, or source_id_a and source_id_b for inconsistencies); "
            "do not write citations inline in the prose. An item citing an unknown source_id is dropped."
        ),
        "server_assigned_fields": (
            "Never emit finding_id, span_locator or provenance. The server assigns them; "
            "emitting one is rejected as a schema violation."
        ),
        "item_schema": item_schema,
    }


def _build_prompt(state: dict, store: SdsRevisionStore) -> str:
    payload = store.resolve_payload(
        payload_ref=state["payload_ref"],
        tenant_id=state["tenant_id"],
        org_id=state["org_id"],
        case_ref=state["case_ref"],
        session_id=state["session_id"],
        correlation_id=state["correlation_id"],
    )
    payload_data = _coerce_payload(payload)
    if payload_data is None:
        raise SecurityViolationError("payload cannot be resolved for the LLM prompt")
    return json.dumps(
        {
            "task": "Draft a non-conclusive SDS revision evidence review. Do not determine classification, author safety phrases, release, or final legal applicability.",
            "rule_edition_ref": state.get("rule_edition_ref"),
            "sop_perspectives": state.get("recall_refs", []),
            # Excerpts exist only in this local prompt object.  They are never
            # returned by a node or placed in the checkpointer-backed State.
            "source_catalog": _prompt_catalog(state.get("source_catalog", []), payload_data),
            "response_contract": _response_contract(),
        },
        ensure_ascii=False,
        default=str,
    )


def _prompt_catalog(catalog: list[dict], payload: Mapping[str, Any]) -> list[dict]:
    """Attach raw excerpts to prompt-only copies of server-issued references."""

    sections = _sections_from_payload(payload)
    supplier_sources = payload.get("supplier_sources", [])
    result: list[dict] = []
    for item in catalog:
        if not isinstance(item, dict):
            continue
        source_id = item.get("source_id")
        excerpt = ""
        if isinstance(source_id, str) and source_id.startswith("src-draft-sec"):
            excerpt = sections.get(item.get("section_no"), "")
        elif isinstance(source_id, str) and source_id.startswith("src-supplier-") and isinstance(supplier_sources, list):
            try:
                index = int(source_id.rsplit("-", 1)[1]) - 1
            except (ValueError, IndexError):
                index = -1
            if 0 <= index < len(supplier_sources) and isinstance(supplier_sources[index], Mapping):
                raw = supplier_sources[index].get("text", supplier_sources[index].get("excerpt", ""))
                excerpt = raw if isinstance(raw, str) else ""
        result.append({**item, "excerpt": unicodedata.normalize("NFKC", excerpt)[:1000]})
    return result


def _build_fallback(state: dict) -> dict[str, Any]:
    catalog = state.get("source_catalog", [])
    if not catalog:
        return {key: [] for key in FINDING_CATEGORIES}
    first = catalog[0]
    source_id = first["source_id"]
    section_no = first["section_no"]
    return {
        "sections": [
            {
                "section_no": section_no,
                "observation_kind": "missing_evidence",
                "observation": "提出された改訂ドラフトについて、根拠の最新性を担当者が確認してください。",
                "source_id": source_id,
            }
        ],
        "revision_claims": [],
        "gaps": [],
        "inconsistencies": [],
        "verification_questions": [
            {
                "section_no": section_no,
                "question_kind": "confirm_source_currency",
                "question": "この節を支える供給者根拠の発行日と適用範囲を確認できますか。",
                "source_id": source_id,
            }
        ],
    }


def _validate_and_filter_llm_findings(draft: dict[str, Any], catalog: list[dict]) -> tuple[dict[str, list[dict]], int]:
    categories = FINDING_CATEGORIES
    if set(draft) != set(categories):
        raise SecurityViolationError("LLM finding object has undeclared or missing top-level keys")
    known = {item.get("source_id") for item in catalog if isinstance(item, dict)}
    output: dict[str, list[dict]] = {key: [] for key in categories}
    dropped = 0
    for category in categories:
        items = draft[category]
        if not isinstance(items, list):
            raise SecurityViolationError(f"LLM field {category} must be an array")
        for item in items:
            if not isinstance(item, dict):
                raise SecurityViolationError(f"LLM {category} item must be an object")
            if {"finding_id", "span_locator", "provenance"} & set(item):
                raise SecurityViolationError("LLM must not create finding_id, span_locator, or provenance")
            expected = _raw_item_keys(category)
            if set(item) != expected:
                raise SecurityViolationError(f"LLM {category} item does not match closed response schema")
            source_keys = ("source_id_a", "source_id_b") if category == "inconsistencies" else ("source_id",)
            # Type-check before the membership test. These values are authored by
            # the LLM, so an unhashable one (dict/list) raises TypeError out of the
            # set lookup and escapes as a framework error rather than a handled
            # schema violation. A non-str id can never match the server-built
            # catalog anyway, so rejecting it here loses no legitimate finding.
            if any(not isinstance(item.get(key), str) or not item.get(key) for key in source_keys):
                raise SecurityViolationError(f"LLM {category} item has a non-string or empty source id")
            if any(item.get(key) not in known for key in source_keys):
                dropped += 1
                continue
            if not _valid_raw_item(category, item):
                raise SecurityViolationError(f"LLM {category} item has invalid enum, section, or prose")
            output[category].append(dict(item))
    return output, dropped


def _raw_item_keys(category: str) -> set[str]:
    return {
        "sections": {"section_no", "observation_kind", "observation", "source_id"},
        "revision_claims": {"section_no", "claim_text", "support_status", "source_id"},
        "gaps": {"section_no", "gap_kind", "description", "source_id"},
        "inconsistencies": {"section_no_a", "section_no_b", "description", "source_id_a", "source_id_b"},
        "verification_questions": {"section_no", "question_kind", "question", "source_id"},
    }[category]


def _valid_raw_item(category: str, item: Mapping[str, Any]) -> bool:
    """Validate a raw LLM item against the same constants the prompt declares.

    Reading ``CATEGORY_ENUMS`` / ``PROSE_LIMITS`` here rather than restating each
    enum and ceiling per branch is what makes the declaration binding: a change
    that widens what the validator accepts necessarily widens what the prompt
    announces, so the two cannot drift into disagreement.
    """
    enum_entry = CATEGORY_ENUMS.get(category)
    if enum_entry is not None:
        # ``in frozenset`` alone raises TypeError on an unhashable value, and a model
        # answering in JSON can put a list or dict here.  Require ``str`` first so an
        # off-contract type is refused through the normal fail-closed path instead of
        # crashing the node with an unhandled TypeError.
        value = item.get(enum_entry[0])
        if not isinstance(value, str) or value not in enum_entry[1]:
            return False
    for key in _raw_item_keys(category):
        value = item.get(key)
        if key in SECTION_KEYS:
            if not _section(value):
                return False
        elif key in PROSE_LIMITS:
            if not _prose(value, PROSE_LIMITS[key]):
                return False
    return True


def _render_verified_brief(findings: Mapping[str, list[dict]], sources: list[dict]) -> tuple[dict, list[dict], list[str]]:
    by_source = {item.get("source_id"): item for item in sources if isinstance(item, dict)}
    categories = FINDING_CATEGORIES
    if set(findings) != set(categories):
        raise SecurityViolationError("missing verified LLM finding category")
    brief: dict[str, list[dict]] = {key: [] for key in categories}
    evidence: list[dict] = []
    cited: list[str] = []
    number = 1
    for category in categories:
        for raw in findings[category]:
            finding_id = f"f{number:03d}"
            number += 1
            item = {"finding_id": finding_id, **raw}
            brief[category].append(item)
            source_ids = [raw["source_id_a"], raw["source_id_b"]] if category == "inconsistencies" else [raw["source_id"]]
            for source_id in source_ids:
                source = by_source.get(source_id)
                if not isinstance(source, dict):
                    raise SecurityViolationError(f"finding cites absent case source {source_id!r}")
                evidence.append(
                    {
                        "finding_id": finding_id,
                        "source_id": source_id,
                        "section_no": source["section_no"],
                        "span_locator": source["span_locator"],
                        "provenance": source["provenance"],
                        "supports": _supports_name(category),
                    }
                )
                cited.append(source_id)
    return brief, evidence, _unique(cited)


def _supports_name(category: str) -> str:
    return {
        "sections": "section",
        "revision_claims": "claim",
        "gaps": "gap",
        "inconsistencies": "inconsistency",
        "verification_questions": "question",
    }[category]


def _sanitize_ingest_row(_state: dict, row: dict[str, Any]) -> dict[str, Any]:
    allowed = {"perspective_ref", "section_no", "rule_edition_ref", "perspective_text", "source_ref", "provenance", "quality_flag"}
    return {key: row[key] for key in allowed if key in row}


def _normalize_ingest_row(_state: dict, row: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(row)
    if isinstance(normalized.get("perspective_text"), str):
        normalized["perspective_text"] = " ".join(normalized["perspective_text"].split())
    return normalized


def _error(message: str) -> dict:
    return {"status": AgentStatus.ERROR.value, "error_log": [message]}


def _section(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and MIN_SECTION <= value <= MAX_SECTION


def _prose(value: Any, maximum: int) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= maximum


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _degradation_list(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    return [item for item in value or [] if item in DEGRADATION_VALUES]


def _finding_count(findings: Mapping[str, list[dict]]) -> int:
    return sum(len(value) for value in findings.values())
