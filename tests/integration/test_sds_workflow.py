"""Integration coverage for lease isolation, modes, and degradation paths."""

from __future__ import annotations

import json

import pytest

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from src.graph.domain_workflow_graph import RevisionConsistencyLlmNode
from src.graph.graph import SdsRevisionEvidenceGraph
from src.services.service import SdsRevisionStore, wire_services


def _setup(payload: dict, *, revision_status: str = "draft", llm=None):
    store = SdsRevisionStore(
        cases={"case-a": {"tenant_id": "tenant-a", "org_id": "org-a", "revision_status": revision_status, "rule_edition_ref": "rule-a"}}
    )
    ref = store.payloads.raw_store.put(json.dumps(payload), scope={"tenant_id": "tenant-a", "org_id": "org-a"}, session_id="session-a")
    store.payloads.bind(ref, tenant_id="tenant-a", org_id="org-a", case_ref="case-a", session_id="session-a", correlation_id="corr-a")
    graph = SdsRevisionEvidenceGraph(wire_services({"store": store, "llm": llm, "top_k": 8, "max_retry": 3, "timeout_s": 30, "memory_enabled": False, "hitl": {"enabled": False}}))
    ctx = InvocationContext(session_id="session-a", correlation_id="corr-a", caller_trust_level=TrustLevel.VERIFIED_EXTERNAL, hitl_allowed=False)
    envelope = json.dumps({"mode": "invoke", "tenant_id": "tenant-a", "org_id": "org-a", "case_ref": "case-a", "payload_ref": ref, "rule_edition_ref": "rule-a"})
    return graph, store, ref, ctx, envelope


def test_AT_11_kb_empty_and_llm_absence_are_successful_degradation():
    """AT-11: KB empty and LLM unavailable are successful degradation paths."""
    graph, _store, _ref, ctx, envelope = _setup({"sections": {"1": "Product identity"}})
    result = graph.invoke(envelope, ctx=ctx)
    assert result["status"] == "success"
    assert {"kb_empty", "llm_unavailable"} <= set(result["output"]["degradation_reason"])


@pytest.mark.parametrize("revision_status", ["approved", "published", "superseded"])
def test_AT_10_authoritative_non_draft_case_is_rejected(revision_status):
    """AT-10: every authoritative terminal revision status is rejected."""
    graph, _store, _ref, ctx, envelope = _setup({"sections": {"1": "Product identity"}}, revision_status=revision_status)
    assert graph.invoke(envelope, ctx=ctx)["status"] == "error"


def test_AT_10_unknown_case_ref_is_rejected_fail_closed():
    """AT-10 / High-1 regression: no authoritative case must never become draft."""
    graph, store, ref, ctx, _envelope = _setup({"sections": {"1": "Product identity"}})
    store.payloads.bind(ref, tenant_id="tenant-a", org_id="org-a", case_ref="unknown-case", session_id="session-a", correlation_id="corr-a")
    envelope = json.dumps(
        {"mode": "invoke", "tenant_id": "tenant-a", "org_id": "org-a", "case_ref": "unknown-case", "payload_ref": ref, "rule_edition_ref": "rule-a"}
    )
    result = graph.invoke(envelope, ctx=ctx)
    assert result["status"] == "error"
    assert store.resolve_case(tenant_id="tenant-a", org_id="org-a", case_ref="unknown-case") is None


def test_AT_13_read_lease_allows_three_reads_in_one_invocation():
    """AT-13: Stage 1, Stage 3, and provenance preparation may all resolve."""
    _graph, store, ref, _ctx, _envelope = _setup({"sections": {"1": "Product identity"}})
    args = dict(payload_ref=ref, tenant_id="tenant-a", org_id="org-a", case_ref="case-a", session_id="session-a", correlation_id="corr-a")
    assert all(store.resolve_payload(**args) for _ in range(3))


def test_AT_14_read_lease_rejects_scope_mismatch_and_close():
    """AT-14: scope mismatch and a closed lease cannot replay payload text."""
    _graph, store, ref, _ctx, _envelope = _setup({"sections": {"1": "Product identity"}})
    args = dict(payload_ref=ref, tenant_id="tenant-a", org_id="org-a", case_ref="case-a", session_id="session-a", correlation_id="corr-a")
    with pytest.raises(ValueError):
        store.resolve_payload(**{**args, "org_id": "other-org"})
    store.payloads.close(ref, tenant_id="tenant-a", org_id="org-a", case_ref="case-a", session_id="session-a", correlation_id="corr-a")
    with pytest.raises(ValueError):
        store.resolve_payload(**args)


def test_AT_15b_llm_cannot_return_verifier_owned_fields():
    """AT-15b: finding_id/span_locator/provenance are verifier-owned."""
    class FakeLlm:
        def complete(self, _messages):
            return {
                "content": json.dumps(
                    {
                        "sections": [
                            {
                                "section_no": 1,
                                "observation_kind": "missing_evidence",
                                "observation": "確認してください。",
                                "source_id": "srcA",
                                "finding_id": "verifier-owned",
                            }
                        ],
                        "revision_claims": [], "gaps": [], "inconsistencies": [], "verification_questions": [],
                    }
                )
            }

    store = SdsRevisionStore()
    ref = store.payloads.raw_store.put("{}", scope={"tenant_id": "tenant-a", "org_id": "org-a"}, session_id="session-a")
    store.payloads.bind(ref, tenant_id="tenant-a", org_id="org-a", case_ref="case-a", session_id="session-a", correlation_id="corr-a")
    node = RevisionConsistencyLlmNode(config={"llm": FakeLlm()}, store=store)
    result = node.execute(
        {
            "payload_ref": ref,
            "tenant_id": "tenant-a",
            "org_id": "org-a",
            "case_ref": "case-a",
            "session_id": "session-a",
            "correlation_id": "corr-a",
            "source_catalog": [{"source_id": "srcA", "section_no": 1, "source_kind": "revision_draft"}],
        }
    )
    assert result["status"] == "error"


def test_AT_15a_llm_catalog_source_id_is_accepted():
    """AT-15a: Stage 3 may cite only a source ID present in source_catalog."""
    class FakeLlm:
        def complete(self, _messages):
            return {
                "content": json.dumps(
                    {
                        "sections": [{"section_no": 1, "observation_kind": "missing_evidence", "observation": "確認してください。", "source_id": "srcA"}],
                        "revision_claims": [], "gaps": [], "inconsistencies": [], "verification_questions": [],
                    }
                )
            }

    store = SdsRevisionStore()
    ref = store.payloads.raw_store.put("{}", scope={"tenant_id": "tenant-a", "org_id": "org-a"}, session_id="session-a")
    store.payloads.bind(ref, tenant_id="tenant-a", org_id="org-a", case_ref="case-a", session_id="session-a", correlation_id="corr-a")
    result = RevisionConsistencyLlmNode(config={"llm": FakeLlm()}, store=store).execute(
        {"payload_ref": ref, "tenant_id": "tenant-a", "org_id": "org-a", "case_ref": "case-a", "session_id": "session-a", "correlation_id": "corr-a", "source_catalog": [{"source_id": "srcA", "section_no": 1, "source_kind": "revision_draft"}]}
    )
    assert result["status"] == "success"
    assert result["llm_findings"]["sections"][0]["source_id"] == "srcA"


def test_AT_15c_catalog_external_source_is_dropped_with_degradation_reason():
    """AT-15c: a catalog-external source is dropped before S-3."""
    class FakeLlm:
        def complete(self, _messages):
            return {
                "content": json.dumps(
                    {
                        "sections": [{"section_no": 1, "observation_kind": "missing_evidence", "observation": "確認してください。", "source_id": "outside"}],
                        "revision_claims": [],
                        "gaps": [],
                        "inconsistencies": [],
                        "verification_questions": [],
                    }
                )
            }

    store = SdsRevisionStore()
    ref = store.payloads.raw_store.put("{}", scope={"tenant_id": "tenant-a", "org_id": "org-a"}, session_id="session-a")
    store.payloads.bind(ref, tenant_id="tenant-a", org_id="org-a", case_ref="case-a", session_id="session-a", correlation_id="corr-a")
    result = RevisionConsistencyLlmNode(config={"llm": FakeLlm()}, store=store).execute(
        {
            "payload_ref": ref,
            "tenant_id": "tenant-a",
            "org_id": "org-a",
            "case_ref": "case-a",
            "session_id": "session-a",
            "correlation_id": "corr-a",
            "source_catalog": [{"source_id": "srcA", "section_no": 1, "source_kind": "revision_draft"}],
        }
    )
    assert result["llm_findings"]["sections"] == []
    assert "llm_schema_violation" in result["degradation_reason"]


def test_AT_18_provider_failure_reaches_the_caller_as_a_machine_readable_code():
    """AT-18: a provider exception must surface error_code at the public boundary.

    Regression for the inner->outer projection defect. Three separate hops each
    dropped the value: the state schema did not declare the channel, the inner
    get_output() did not project it, and the GraphNode propagated a SubgraphError
    instead of merging a failure. The operator saw status=error whose only detail
    was a traceback containing absolute filesystem paths. A node-level unit test
    cannot catch this -- only an end-to-end invoke can.
    """

    class ExplodingLlm:
        def complete(self, _messages):
            raise RuntimeError("provider exploded")

    graph, _store, _ref, ctx, envelope = _setup({"sections": {"1": "Product identity"}}, llm=ExplodingLlm())

    result = graph.invoke(envelope, ctx=ctx)
    serialized = json.dumps(result, ensure_ascii=False, default=str)

    assert result["status"] == "error"
    assert result.get("error_code") == "llm_call_failed", (
        f"inner error_code did not reach the caller: {result.get('error_code')!r}"
    )
    # The provider's own message and the inner traceback must never be republished.
    assert "provider exploded" not in serialized
    assert "Traceback" not in serialized
    # error_log is an operator.add channel; the caller must not see duplicates.
    log = result.get("error_log") or []
    assert len(log) == len(set(log))


def test_AT_18b_unrecognised_inner_failure_degrades_to_the_generic_code():
    """AT-18 control: the code allowlist must not pass through arbitrary text.

    Pairs with AT-18. Without this, a classifier that simply echoed the inner
    error_log would satisfy AT-18 while republishing raw exception strings.
    """

    class UnknownFailureLlm:
        def complete(self, _messages):
            return {"content": "UNPARSEABLE_MARKER_9f3a not json at all"}

    graph, _store, _ref, ctx, envelope = _setup({"sections": {"1": "Product identity"}}, llm=UnknownFailureLlm())
    result = graph.invoke(envelope, ctx=ctx)
    serialized = json.dumps(result, ensure_ascii=False, default=str)

    assert "UNPARSEABLE_MARKER_9f3a" not in serialized
    if result["status"] == "error":
        assert result.get("error_code") in {"llm_call_failed", "llm_output_parse_failed", "subgraph_failed"}


@pytest.mark.parametrize("bad_source_id", [{"nested": "srcA"}, ["srcA"], "", 7, None])
def test_AT_19_non_string_llm_source_id_is_a_handled_schema_violation(bad_source_id):
    """AT-19: an LLM-authored source id of the wrong type must not escape as TypeError.

    The membership test `item.get(key) not in known` hashes the value, so an
    unhashable id (dict/list) raised `TypeError: unhashable type` straight out of
    the node -- surfacing as an opaque framework error rather than a handled
    schema violation. LLM output is untrusted input and must fail closed.
    """
    class FakeLlm:
        def complete(self, _messages):
            return {
                "content": json.dumps(
                    {
                        "sections": [{"section_no": 1, "observation_kind": "missing_evidence", "observation": "確認してください。", "source_id": bad_source_id}],
                        "revision_claims": [], "gaps": [], "inconsistencies": [], "verification_questions": [],
                    }
                )
            }

    store = SdsRevisionStore()
    ref = store.payloads.raw_store.put("{}", scope={"tenant_id": "tenant-a", "org_id": "org-a"}, session_id="session-a")
    store.payloads.bind(ref, tenant_id="tenant-a", org_id="org-a", case_ref="case-a", session_id="session-a", correlation_id="corr-a")
    node = RevisionConsistencyLlmNode(config={"llm": FakeLlm()}, store=store)

    result = node.execute(
        {
            "payload_ref": ref, "tenant_id": "tenant-a", "org_id": "org-a", "case_ref": "case-a",
            "session_id": "session-a", "correlation_id": "corr-a",
            "source_catalog": [{"source_id": "srcA", "section_no": 1, "source_kind": "revision_draft"}],
        }
    )
    assert result["status"] == "error"

