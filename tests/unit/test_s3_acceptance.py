"""AT-01..08 and AT-16 closed-schema regression coverage."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from framework.errors import SecurityViolationError
from framework.utils.config_loader import load_config
from src.services.service import wire_services
from src.nodes.main_node import SdsRevisionWorkflowGraphNode
from tests.unit.test_main_node import _valid_result


def _gate(value: dict) -> dict:
    runtime = load_config(str(Path(__file__).parents[2] / "config" / "config.yaml"))
    return SdsRevisionWorkflowGraphNode(config=wire_services(runtime))._extra_security_gate_output(value)


@pytest.mark.parametrize(
    "case_id, mutate",
    [
        ("AT-01a", lambda value: None),
        ("AT-02a", lambda value: None),
        ("AT-03a", lambda value: None),
        ("AT-04a", lambda value: None),
        ("AT-05a", lambda value: None),
        ("AT-06a", lambda value: None),
        ("AT-07a", lambda value: None),
        ("AT-08a", lambda value: None),
    ],
)
def test_acceptance_accepts_valid_invoke_output(case_id, mutate):
    result = _valid_result()
    mutate(result)
    gated = _gate(result)
    assert gated["gate_kind"] == "invoke", case_id
    assert "_verify_case_sources" not in gated, "AT-12"
    assert "_verify_scope" not in gated, "AT-12"


@pytest.mark.parametrize(
    "case_id, mutate",
    [
        ("AT-01b", lambda value: value["revision_brief"].__setitem__("overall_verdict", "x")),
        ("AT-01c", lambda value: value["evidence_map"][0].__setitem__("extra", "x")),
        ("AT-02b", lambda value: value["revision_brief"]["sections"][0].__setitem__("classification_final", "x")),
        ("AT-02c", lambda value: value["revision_brief"]["sections"][0].__setitem__("Classification-Final", "x")),
        ("AT-02d", lambda value: value["revision_brief"]["sections"][0].__setitem__("分類確定", "x")),
        ("AT-03b", lambda value: value.__setitem__("evidence_map", [])),
        ("AT-03g", lambda value: _wrong_but_real_source(value)),
        ("AT-03h", lambda value: value["evidence_map"][0].__setitem__("span_locator", "other.locator")),
        ("AT-03j", lambda value: value["revision_brief"]["sections"][0].__setitem__("source_id", "")),
        ("AT-03c", lambda value: value["revision_brief"]["sections"][0].__setitem__("source_id", "not-real")),
        ("AT-03d", lambda value: value["_verify_case_sources"][0].__setitem__("tenant_id", "other-tenant")),
        ("AT-03e", lambda value: value["revision_brief"]["sections"][0].__setitem__("source_id", "perspective-ref")),
        ("AT-03f", lambda value: value["evidence_map"][0].__setitem__("provenance", "")),
        ("AT-04b", lambda value: value.__setitem__("cited_source_ids", ["forged"])),
        ("AT-05b", lambda value: _add_claim_with_enum(value, "compliant")),
        ("AT-05c", lambda value: _add_question_with_enum(value, "release_decision")),
        ("AT-06b", lambda value: value["revision_brief"]["sections"][0].__setitem__("section_no", 17)),
        ("AT-07b", lambda value: value["evidence_map"][0].__setitem__("provenance", "sk-" + "a" * 20)),
        ("AT-08b", lambda value: value.pop("gate_kind")),
        ("AT-16", lambda value: value.__setitem__("degradation_reason", ["free text"])),
    ],
)
def test_acceptance_rejects_closed_schema_violations(case_id, mutate):
    result = copy.deepcopy(_valid_result())
    mutate(result)
    with pytest.raises(SecurityViolationError):
        _gate(result)


def _wrong_but_real_source(value: dict) -> None:
    other = copy.deepcopy(value["_verify_case_sources"][0])
    other["source_id"] = "srcB"
    value["_verify_case_sources"].append(other)
    value["evidence_map"][0]["source_id"] = "srcB"
    value["cited_source_ids"] = ["srcA", "srcB"]


def _add_claim_with_enum(value: dict, enum: str) -> None:
    value["revision_brief"]["revision_claims"].append(
        {"finding_id": "f002", "section_no": 2, "claim_text": "確認してください。", "support_status": enum, "source_id": "srcA"}
    )
    value["evidence_map"].append(
        {"finding_id": "f002", "source_id": "srcA", "section_no": 2, "span_locator": "sections.2", "provenance": "revision_draft", "supports": "claim"}
    )


def _add_question_with_enum(value: dict, enum: str) -> None:
    value["revision_brief"]["verification_questions"].append(
        {"finding_id": "f002", "section_no": 2, "question_kind": enum, "question": "確認できますか。", "source_id": "srcA"}
    )
    value["evidence_map"].append(
        {"finding_id": "f002", "source_id": "srcA", "section_no": 2, "span_locator": "sections.2", "provenance": "revision_draft", "supports": "question"}
    )


def test_AT_03i_requires_two_distinct_inconsistency_evidence_rows():
    """AT-03i: source_id_a/source_id_b require distinct evidence rows."""
    result = _valid_result()
    source_b = copy.deepcopy(result["_verify_case_sources"][0])
    source_b.update({"source_id": "srcB", "section_no": 3, "span_locator": "sections.3"})
    result["_verify_case_sources"].append(source_b)
    result["revision_brief"]["sections"] = []
    result["evidence_map"] = [
        {
            "finding_id": "f001",
            "source_id": "srcA",
            "section_no": 2,
            "span_locator": "sections.2",
            "provenance": "revision_draft",
            "supports": "inconsistency",
        }
    ]
    result["revision_brief"]["inconsistencies"] = [
        {
            "finding_id": "f001",
            "section_no_a": 2,
            "section_no_b": 3,
            "description": "節間の記載を確認してください。",
            "source_id_a": "srcA",
            "source_id_b": "srcB",
        }
    ]
    result["cited_source_ids"] = ["srcA", "srcB"]
    with pytest.raises(SecurityViolationError):
        _gate(result)


def test_AT_08c_accepts_ingest_without_verifier_records():
    """AT-08c: ingest is valid without invoke-only verifier state."""
    assert _gate({"gate_kind": "ingest", "status": "success", "ingest_summary": {"written_ids": [], "rejected": []}})["gate_kind"] == "ingest"


def test_AT_08d_rejects_ingest_with_verifier_records():
    """AT-08d: ingest must reject an unnecessary source registry."""
    with pytest.raises(SecurityViolationError):
        _gate({"gate_kind": "ingest", "status": "success", "ingest_summary": {"written_ids": [], "rejected": []}, "_verify_case_sources": []})
