"""Unit checks for the Cat2 GraphNode boundary."""

from framework.errors import SecurityViolationError

from src.nodes.main_node import SdsRevisionWorkflowGraphNode


def _valid_result() -> dict:
    source = {
        "source_id": "srcA",
        "section_no": 2,
        "span_locator": "sections.2",
        "provenance": "revision_draft",
        "tenant_id": "tenant-a",
        "org_id": "org-a",
        "case_ref": "case-a",
    }
    return {
        "gate_kind": "invoke",
        "status": "success",
        "revision_brief": {
            "sections": [
                {
                    "finding_id": "f001",
                    "section_no": 2,
                    "observation_kind": "missing_evidence",
                    "observation": "根拠の確認が必要です。",
                    "source_id": "srcA",
                }
            ],
            "revision_claims": [],
            "gaps": [],
            "inconsistencies": [],
            "verification_questions": [],
        },
        "evidence_map": [
            {
                "finding_id": "f001",
                "source_id": "srcA",
                "section_no": 2,
                "span_locator": "sections.2",
                "provenance": "revision_draft",
                "supports": "section",
            }
        ],
        "requires_human_review": True,
        "cited_source_ids": ["srcA"],
        "_verify_case_sources": [source],
        "_verify_scope": {"tenant_id": "tenant-a", "org_id": "org-a", "case_ref": "case-a"},
    }


class TestMainNode:
    def setup_method(self):
        self.node = SdsRevisionWorkflowGraphNode(config={})

    def test_accepts_valid_bounded_prose_and_drops_verifier_keys(self):
        result = self.node._extra_security_gate_output(_valid_result())
        assert result["revision_brief"]["sections"][0]["observation"] == "根拠の確認が必要です。"
        assert "_verify_case_sources" not in result
        assert "_verify_scope" not in result

    def test_rejects_unknown_gate_kind(self):
        result = _valid_result()
        result["gate_kind"] = "unknown"
        try:
            self.node._extra_security_gate_output(result)
        except SecurityViolationError:
            pass
        else:
            raise AssertionError("unknown gate_kind must fail closed")

    def test_execute_method_is_inherited_from_reviewed_graphnode_skeleton(self):
        assert "_invoke_impl" not in SdsRevisionWorkflowGraphNode.__dict__
