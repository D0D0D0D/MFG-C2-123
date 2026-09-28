"""Cat2 GraphNode boundary and S-3 SDS provenance gate."""

from __future__ import annotations

import json
import unicodedata
from typing import Any, ClassVar

from framework.errors import SecurityViolationError
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.security.credential_detector import detect_credentials
from shared.utils.audit_logger import emit_trace_event
from src.cat2_graph_skeleton import DomainWorkflowGraphNode, ModeSpec
from src.evidence_gate import ProvenanceContract, validate_provenance
from src.graph.domain_workflow_graph import (
    DEGRADATION_VALUES,
    GAP_KINDS,
    OBSERVATION_KINDS,
    QUESTION_KINDS,
    SUPPORT_STATUSES,
    SdsRevisionWorkflowGraph,
)


FORBIDDEN_KEY_ALIASES = {
    "classification_final": ("分類確定", "分類決定", "分類判定", "GHS分類確定", "最終分類"),
    "safety_phrase": ("安全文言", "注意喚起語", "危険有害性情報", "注意書き"),
    "release": ("出荷可否", "リリース判定", "発行可否", "公開可否", "承認可否"),
}
_FORBIDDEN_CANONICAL = frozenset({"classificationfinal", "safephraseauthoring", "safetyphrase", "release", "releasedecision"})
_FORBIDDEN_ALIASES = frozenset(alias for aliases in FORBIDDEN_KEY_ALIASES.values() for alias in aliases)


# Structured failure metadata is projected for every mode. Without this the
# inner node's error_code is dropped at merge_output() and the operator sees
# only status=error with no machine-readable reason.
_FAILURE_FIELDS = {
    "error_code": "error_code",
    "error_type": "error_type",
    "error_log": "error_log",
}


# Inner failure codes that may be surfaced to the caller verbatim. This is an
# ALLOWLIST, not a filter: several inner `_error()` call sites interpolate raw
# exception text (`{exc}`) into error_log, so forwarding that log unchanged would
# republish provider-authored strings at the public boundary. Anything not
# recognised here degrades to the generic code.
_KNOWN_SUBGRAPH_FAILURES = ("llm_call_failed", "llm_output_parse_failed")
_GENERIC_SUBGRAPH_FAILURE = "subgraph_failed"


def _classify_subgraph_failure(inner_error_log: object) -> str:
    """Map the inner error_log onto a known code without echoing its content."""
    if not isinstance(inner_error_log, (list, tuple)):
        return _GENERIC_SUBGRAPH_FAILURE
    for entry in inner_error_log:
        if not isinstance(entry, str):
            continue
        for known in _KNOWN_SUBGRAPH_FAILURES:
            if known in entry:
                return known
    return _GENERIC_SUBGRAPH_FAILURE


# Domain content that S-3 must always gate. A result carrying none of these and
# a status of error is a pure failure projection with nothing to inspect.
_CONTENT_KEYS = frozenset(
    {"revision_brief", "evidence_map", "cited_source_ids", "ingest_summary",
     "feedback_summary", "_verify_case_sources", "_verify_scope"}
)


class SdsRevisionWorkflowGraphNode(DomainWorkflowGraphNode):
    """Outer ``main`` node with a cached SDS workflow and fail-closed S-3 gate."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL
    propagate_hitl: ClassVar[bool] = False
    # "handle" (not the inherited "propagate"): a propagated SubgraphError
    # reaches the caller as a bare status=error whose error_log carries the
    # full traceback, including absolute filesystem paths, while the inner
    # node's error_code is lost entirely. Handling it lets on_subgraph_error
    # below project a normalised, machine-readable failure instead.
    error_strategy: ClassVar[str] = "handle"
    mode_specs: ClassVar[dict[str, ModeSpec]] = {
        "invoke": ModeSpec(
            gate_kind="invoke",
            fields={
                "revision_brief": "revision_brief",
                "evidence_map": "evidence_map",
                "requires_human_review": "requires_human_review",
                "degradation_reason": "degradation_reason",
                **_FAILURE_FIELDS,
            },
        ),
        "ingest": ModeSpec(gate_kind="ingest", fields={"ingest_summary": "ingest_summary", **_FAILURE_FIELDS}),
        "feedback": ModeSpec(gate_kind="feedback", fields={"feedback_summary": "feedback_summary", **_FAILURE_FIELDS}),
    }

    def _build_subgraph(self) -> SdsRevisionWorkflowGraph:
        return SdsRevisionWorkflowGraph(config=self._parent_config())

    def _parent_config(self) -> dict[str, Any]:
        return {
            "llm": self._config.get("llm"),
            "store": self._config.get("store"),
            "retrieval": self._config.get("retrieval"),
            "top_k": self._config.get("top_k"),
            "rule_edition_ref": self._config.get("rule_edition_ref"),
            "trusted_scope": self._config.get("trusted_scope"),
            "session_id": self._config.get("session_id"),
            "memory_enabled": False,
            "hitl": {"enabled": False},
        }

    def extract_input(self, state: dict) -> str:
        """Serialize only the canonical, non-sensitive envelope for the inner graph."""

        keys = ("mode", "tenant_id", "org_id", "case_ref", "payload_ref", "rule_edition_ref", "validated_input")
        envelope = {key: state[key] for key in keys if key in state}
        return json.dumps(envelope, ensure_ascii=False, default=str, sort_keys=True)

    def merge_output(self, state: dict, sub_result: dict) -> dict:
        delta = super().merge_output(state, sub_result)
        # S-4: the outer main node owns the subgraph boundary, so it emits its
        # own domain event.  Counts only -- never the merged content itself.
        # ``subgraph_status`` is deliberately not named ``status``: this fires
        # before the outer S-3 gate, so a value of "success" means the inner
        # workflow succeeded, not that the invocation passed output gating.
        emit_trace_event(
            "sds_subgraph_merged",
            {
                "mode": state.get("mode"),
                "gate_kind": delta.get("gate_kind"),
                "subgraph_status": delta.get("status"),
                "section_count": len(((delta.get("revision_brief") or {}).get("sections")) or []),
                "evidence_count": len(delta.get("evidence_map") or []),
            },
            state,
        )
        if state.get("mode") == "invoke" and delta.get("status") != AgentStatus.ERROR.value:
            # Explicit verifier envelope: this is the only way the GraphNode
            # S-3 hook receives current-case source data without adding it to
            # parent state.  The hook pops both keys before returning.
            delta["_verify_case_sources"] = sub_result.get("case_sources")
            delta["_verify_scope"] = {
                "tenant_id": state.get("tenant_id"),
                "org_id": state.get("org_id"),
                "case_ref": state.get("case_ref"),
            }
            delta["cited_source_ids"] = sub_result.get("cited_source_ids")
        return delta

    def on_subgraph_error(self, state: dict, error: Exception) -> dict:
        """Project a subgraph failure without leaking exception text.

        The skeleton default interpolates ``{error}``, which for a SubgraphError
        embeds the inner traceback (absolute paths included) into a checkpointed
        state channel. Only the exception *type* and a fixed code are safe to
        keep; the provider's own message is never admitted.
        """
        code = _classify_subgraph_failure(getattr(error, "error_log", None))
        emit_trace_event(
            "sds_subgraph_failed",
            {"mode": state.get("mode"), "error_code": code, "error_type": type(error).__name__},
            state,
        )
        return {
            "status": AgentStatus.ERROR.value,
            "error_code": code,
            "error_type": type(error).__name__,
            "error_log": [f"SdsRevisionWorkflowGraphNode: {code}"],
        }

    def _extra_security_gate_output(self, result: dict) -> dict:
        """Enforce S-3 closed schemas and current-case source correspondence."""

        if result == {}:  # inherited upstream-error pass-through skeleton
            return result
        if result.get("status") == AgentStatus.ERROR.value and not (_CONTENT_KEYS & set(result)):
            # Failure projection carrying only status/error_* — no domain content
            # exists to gate. The membership test is an allowlist of content keys,
            # so any future content key is gated by default rather than exempted.
            return result
        gate_kind = result.get("gate_kind")
        if gate_kind not in {"invoke", "ingest", "feedback"}:
            raise SecurityViolationError("S-3 gate_kind is missing or unknown")
        if gate_kind == "invoke":
            _validate_invoke_envelope(result)
            contract = ProvenanceContract(
                gate_kind="invoke",
                cited_ids_key="cited_source_ids",
                retrieved_records_key="_verify_case_sources",
                evidence_map_key="evidence_map",
                record_id_keys=("source_id",),
            )
            validate_provenance(result, contract)
            _validate_finding_evidence_correspondence(result)
            _scan_nested_credentials(result)
            result.pop("_verify_case_sources", None)
            result.pop("_verify_scope", None)
            result.pop("cited_source_ids", None)
        else:
            _validate_non_invoke_envelope(result, gate_kind)
            _scan_nested_credentials(result)
        return result


def _validate_invoke_envelope(result: dict) -> None:
    allowed = {
        "gate_kind",
        "status",
        "revision_brief",
        "evidence_map",
        "requires_human_review",
        "degradation_reason",
        "cited_source_ids",
        "_verify_case_sources",
        "_verify_scope",
    }
    _closed_keys(result, allowed, "invoke result")
    required = {"revision_brief", "evidence_map", "requires_human_review", "_verify_case_sources", "_verify_scope", "cited_source_ids"}
    if not required <= set(result):
        raise SecurityViolationError("invoke verifier envelope is incomplete")
    if result.get("requires_human_review") is not True:
        raise SecurityViolationError("invoke requires_human_review must be true")
    _validate_degradation(result.get("degradation_reason"))
    _scan_forbidden_keys(result["revision_brief"])
    _scan_forbidden_keys(result["evidence_map"])
    _validate_brief(result["revision_brief"])
    _validate_evidence_map(result["evidence_map"])
    if not isinstance(result["_verify_case_sources"], list) or not isinstance(result["_verify_scope"], dict):
        raise SecurityViolationError("invoke verifier data has invalid type")
    scope = result["_verify_scope"]
    if set(scope) != {"tenant_id", "org_id", "case_ref"} or not all(_nonempty(scope[key]) for key in scope):
        raise SecurityViolationError("invoke verifier scope is incomplete")
    if not isinstance(result["cited_source_ids"], list) or not all(_nonempty(value) for value in result["cited_source_ids"]):
        raise SecurityViolationError("invoke cited_source_ids must be a non-empty-string list")


def _validate_non_invoke_envelope(result: dict, gate_kind: str) -> None:
    if "_verify_case_sources" in result or "_verify_scope" in result:
        raise SecurityViolationError("non-invoke output must not carry current-case verifier records")
    if gate_kind == "ingest":
        _closed_keys(result, {"gate_kind", "status", "ingest_summary", "degradation_reason"}, "ingest result")
        summary = result.get("ingest_summary")
        if not isinstance(summary, dict):
            raise SecurityViolationError("ingest summary is required")
        _closed_keys(summary, {"written_ids", "rejected"}, "ingest summary")
        if not isinstance(summary.get("written_ids"), list) or not isinstance(summary.get("rejected"), list):
            raise SecurityViolationError("ingest summary fields have invalid types")
        for item in summary["rejected"]:
            if not isinstance(item, dict):
                raise SecurityViolationError("ingest rejected item must be object")
            _closed_keys(item, {"row_ref", "reason_kind"}, "ingest rejected item")
            if set(item) != {"row_ref", "reason_kind"} or item["reason_kind"] not in {
                "quality_gate_failed", "missing_required_field", "unsupported_format", "scope_mismatch", "duplicate"
            }:
                raise SecurityViolationError("ingest rejected item violates closed schema")
    else:
        _closed_keys(result, {"gate_kind", "status", "feedback_summary", "degradation_reason"}, "feedback result")
        summary = result.get("feedback_summary")
        if not isinstance(summary, dict):
            raise SecurityViolationError("feedback summary is required")
        _closed_keys(summary, {"applied_count", "audit_event_ids"}, "feedback summary")
        if (
            not isinstance(summary.get("applied_count"), int)
            or isinstance(summary["applied_count"], bool)
            or summary["applied_count"] < 0
            or not isinstance(summary.get("audit_event_ids"), list)
        ):
            raise SecurityViolationError("feedback summary violates closed schema")
    _validate_degradation(result.get("degradation_reason"))


def _validate_brief(brief: Any) -> None:
    if not isinstance(brief, dict):
        raise SecurityViolationError("revision_brief must be an object")
    categories = {"sections", "revision_claims", "gaps", "inconsistencies", "verification_questions"}
    if set(brief) != categories:
        raise SecurityViolationError("revision_brief has undeclared or missing categories")
    schemas: dict[str, tuple[set[str], dict[str, set[str]], tuple[str, ...]]] = {
        "sections": (
            {"finding_id", "section_no", "observation_kind", "observation", "source_id"},
            {"observation_kind": OBSERVATION_KINDS},
            ("section_no", "observation", "source_id"),
        ),
        "revision_claims": (
            {"finding_id", "section_no", "claim_text", "support_status", "source_id"},
            {"support_status": SUPPORT_STATUSES},
            ("section_no", "claim_text", "source_id"),
        ),
        "gaps": (
            {"finding_id", "section_no", "gap_kind", "description", "source_id"},
            {"gap_kind": GAP_KINDS},
            ("section_no", "description", "source_id"),
        ),
        "inconsistencies": (
            {"finding_id", "section_no_a", "section_no_b", "description", "source_id_a", "source_id_b"},
            {},
            ("section_no_a", "section_no_b", "description", "source_id_a", "source_id_b"),
        ),
        "verification_questions": (
            {"finding_id", "section_no", "question_kind", "question", "source_id"},
            {"question_kind": QUESTION_KINDS},
            ("section_no", "question", "source_id"),
        ),
    }
    for category, items in brief.items():
        if not isinstance(items, list):
            raise SecurityViolationError(f"revision_brief.{category} must be an array")
        keys, enums, required = schemas[category]
        for item in items:
            if not isinstance(item, dict) or set(item) != keys:
                raise SecurityViolationError(f"revision_brief.{category} item violates closed schema")
            if not _nonempty(item.get("finding_id")):
                raise SecurityViolationError("finding_id is required")
            for key in required:
                if key.startswith("section_no"):
                    if not _section(item.get(key)):
                        raise SecurityViolationError("section number is outside GHS 1..16")
                elif key in {"observation", "claim_text", "description"}:
                    if not _prose(item.get(key), 1000):
                        raise SecurityViolationError("bounded prose field is invalid")
                elif key == "question":
                    if not _prose(item.get(key), 500):
                        raise SecurityViolationError("bounded question field is invalid")
                elif not _nonempty(item.get(key)):
                    raise SecurityViolationError("finding source_id must be non-empty")
            for enum_key, allowed in enums.items():
                if item.get(enum_key) not in allowed:
                    raise SecurityViolationError(f"{enum_key} is outside its closed enum")


def _validate_evidence_map(evidence: Any) -> None:
    if not isinstance(evidence, list):
        raise SecurityViolationError("evidence_map must be an array")
    required = {"finding_id", "source_id", "section_no", "span_locator", "provenance", "supports"}
    for item in evidence:
        if not isinstance(item, dict) or set(item) != required:
            raise SecurityViolationError("evidence_map item violates closed schema")
        if not _nonempty(item.get("finding_id")) or not _nonempty(item.get("source_id")):
            raise SecurityViolationError("evidence IDs must be non-empty")
        if not _section(item.get("section_no")):
            raise SecurityViolationError("evidence section number is outside GHS 1..16")
        if not _nonempty(item.get("span_locator")) or not _nonempty(item.get("provenance")):
            raise SecurityViolationError("evidence locator/provenance must be non-empty")
        if item.get("supports") not in {"section", "claim", "gap", "inconsistency", "question"}:
            raise SecurityViolationError("evidence supports is outside closed enum")


def _validate_finding_evidence_correspondence(result: dict) -> None:
    sources = result["_verify_case_sources"]
    scope = result["_verify_scope"]
    source_by_id = {source.get("source_id"): source for source in sources if isinstance(source, dict)}
    evidence_by_finding: dict[str, list[dict]] = {}
    for evidence in result["evidence_map"]:
        source = source_by_id.get(evidence["source_id"])
        if not isinstance(source, dict):
            raise SecurityViolationError("evidence source_id is not a current-case record")
        for key in ("section_no", "span_locator", "provenance"):
            if evidence[key] != source.get(key):
                raise SecurityViolationError("evidence attributes do not exactly match current-case source")
        if any(source.get(key) != scope[key] for key in ("tenant_id", "org_id", "case_ref")):
            raise SecurityViolationError("case source does not match verified scope")
        evidence_by_finding.setdefault(evidence["finding_id"], []).append(evidence)
    expected: dict[str, set[str]] = {}
    for category, items in result["revision_brief"].items():
        for item in items:
            source_ids = {item["source_id_a"], item["source_id_b"]} if category == "inconsistencies" else {item["source_id"]}
            expected[item["finding_id"]] = source_ids
            rows = evidence_by_finding.get(item["finding_id"], [])
            if {row["source_id"] for row in rows} != source_ids:
                raise SecurityViolationError("finding/evidence source sets are not an exact match")
            if category == "inconsistencies" and len(rows) != 2:
                raise SecurityViolationError("inconsistency must use separate evidence rows for source A and B")
    if set(evidence_by_finding) != set(expected):
        raise SecurityViolationError("evidence_map contains an orphan or missing finding")


def _scan_forbidden_keys(value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = _normalize_key(str(key))
            if normalized in _FORBIDDEN_CANONICAL or key in _FORBIDDEN_ALIASES:
                raise SecurityViolationError("forbidden conclusion field is present")
            _scan_forbidden_keys(child)
    elif isinstance(value, list):
        for child in value:
            _scan_forbidden_keys(child)


def _scan_nested_credentials(value: Any) -> None:
    if isinstance(value, str):
        found = detect_credentials(value)
        if found:
            raise SecurityViolationError(f"credential pattern {found[0]['type']} detected in nested output")
    elif isinstance(value, dict):
        for child in value.values():
            _scan_nested_credentials(child)
    elif isinstance(value, list):
        for child in value:
            _scan_nested_credentials(child)


def _normalize_key(key: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFKC", key).casefold() if ch not in {"-", "_", " "})


def _closed_keys(value: dict, allowed: set[str], location: str) -> None:
    unexpected = set(value) - allowed
    if unexpected:
        raise SecurityViolationError(f"{location} has undeclared key(s): {sorted(unexpected)}")


def _validate_degradation(value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, list) or any(item not in DEGRADATION_VALUES for item in value):
        raise SecurityViolationError("degradation_reason must use its closed enum array")


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _section(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 16


def _prose(value: Any, maximum: int) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= maximum
