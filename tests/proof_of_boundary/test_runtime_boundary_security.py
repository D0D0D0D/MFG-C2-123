"""Runtime proofs for payload isolation, scope persistence, and S-1 trust."""

from __future__ import annotations

import json
from collections.abc import Mapping
from unittest.mock import Mock

import pytest
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from langgraph.checkpoint.memory import InMemorySaver

from src.graph.domain_workflow_graph import (
    AccumulateFeedbackNode,
    AccumulateIngestNode,
    ClaimSourceVerifyNode,
    DeterministicCheckNode,
    KbRecallNode,
    RevisionConsistencyLlmNode,
)
from src.graph.graph import SdsRevisionEvidenceGraph
from src.nodes.main_node import SdsRevisionWorkflowGraphNode
from src.nodes.post_process_node import PostProcessNode
from src.nodes.pre_process_node import PreProcessNode
from src.services.service import SdsRevisionStore, wire_services


MARKER = "トリメチルアミン CAS 75-50-3 供給者=DowSpecialty社 濃度42.7%"


def _graph_with_payload(payload: dict, *, llm=None, memory_enabled: bool = False):
    store = SdsRevisionStore(
        cases={"case-a": {"tenant_id": "tenant-a", "org_id": "org-a", "revision_status": "draft", "rule_edition_ref": "rule-a"}}
    )
    ref = store.payloads.raw_store.put(json.dumps(payload), scope={"tenant_id": "tenant-a", "org_id": "org-a"}, session_id="session-a")
    store.payloads.bind(ref, tenant_id="tenant-a", org_id="org-a", case_ref="case-a", session_id="session-a", correlation_id="corr-a")
    graph = SdsRevisionEvidenceGraph(
        wire_services({"store": store, "llm": llm, "top_k": 8, "max_retry": 3, "timeout_s": 30, "memory_enabled": memory_enabled, "hitl": {"enabled": False}})
    )
    ctx = InvocationContext(session_id="session-a", correlation_id="corr-a", thread_id="thread-a", caller_trust_level=TrustLevel.VERIFIED_EXTERNAL, hitl_allowed=False)
    envelope = json.dumps({"mode": "invoke", "tenant_id": "tenant-a", "org_id": "org-a", "case_ref": "case-a", "payload_ref": ref, "rule_edition_ref": "rule-a"})
    return graph, store, ctx, envelope


def test_high_a_supplier_literals_never_become_public_provenance():
    """Client source metadata cannot overwrite the server source registry."""
    class FakeLlm:
        def complete(self, _messages):
            return {
                "content": json.dumps(
                    {
                        "sections": [{"section_no": 1, "observation_kind": "missing_evidence", "observation": "確認してください。", "source_id": "src-supplier-001"}],
                        "revision_claims": [], "gaps": [], "inconsistencies": [], "verification_questions": [],
                    }
                )
            }

    graph, _store, ctx, envelope = _graph_with_payload(
        {
            "sections": {"1": "製品識別", "2": "区分1へ変更"},
            "supplier_sources": [{"source_id": "supplier-client-id", "provenance": "Acme Chemical Japan KK", "span_locator": "組成 42.7% トリメチルアミン", "source_kind": "supplier_sds", "text": MARKER}],
        },
        llm=FakeLlm(),
    )
    output = graph.invoke(envelope, ctx=ctx)["output"]
    evidence = output["evidence_map"][0]
    assert evidence == {
        "finding_id": "f001",
        "source_id": "src-supplier-001",
        "section_no": 1,
        "span_locator": "supplier_source.1",
        "provenance": "supplier_source_record",
        "supports": "section",
    }
    assert "Acme Chemical Japan KK" not in repr(output)
    assert "42.7%" not in repr(output)
    assert MARKER not in repr(output)


def test_high_a_colliding_client_source_id_is_rejected():
    graph, _store, ctx, envelope = _graph_with_payload(
        {"sections": {"1": "製品識別"}, "supplier_sources": [{"source_id": "src-draft-sec01", "text": "x"}]}
    )
    assert graph.invoke(envelope, ctx=ctx)["status"] == "error"


def test_pb5_runtime_marker_absent_from_node_result_output_checkpoint_and_audit(caplog):
    """PB-5: real checkpointer inspection proves raw SDS text never enters State."""
    graph, _store, ctx, envelope = _graph_with_payload(
        {"sections": {"1": MARKER}, "supplier_sources": [{"text": MARKER}]}, memory_enabled=True
    )
    stage1 = DeterministicCheckNode(store=_store).execute(
        {"user_input": envelope, "session_id": ctx.session_id, "correlation_id": ctx.correlation_id}
    )
    checkpointer = InMemorySaver()
    graph.compile(checkpointer=checkpointer)
    with caplog.at_level("INFO", logger="agentcore.audit"):
        result = graph.invoke(envelope, ctx=ctx)
    snapshots = list(checkpointer.list({"configurable": {"thread_id": ctx.thread_id}}))
    checkpoint_text = repr(snapshots)
    main_node = graph._nodes["main"]
    inner = main_node.get_subgraph()
    inner_state = inner.get_output if inner is not None else None
    assert inner_state is not None  # ensure the cached inner workflow was exercised
    assert MARKER not in repr(stage1)
    assert MARKER not in repr(result)
    assert MARKER not in checkpoint_text
    assert MARKER not in caplog.text
    state_values = [snapshot.checkpoint.get("channel_values", {}) for snapshot in snapshots]
    assert all(MARKER not in repr(value) for value in state_values)


class _ScopedBackend:
    def __init__(self, records: list[dict]):
        self.records = records
        self.calls: list[dict[str, str]] = []

    def search(self, *, scope: Mapping[str, str], query: str, top_k: int):
        self.calls.append(dict(scope))
        return [record for record in self.records if all(record.get(key) == value for key, value in scope.items())][:top_k]


def test_high_b_perspective_write_uses_verified_scope_and_cross_scope_recall_is_empty():
    store = SdsRevisionStore()
    record_id = store.write_perspective(
        {"tenant_id": "tenant-a", "org_id": "org-a", "case_ref": "case-a", "rule_edition_ref": "rule-a"},
        {"source_id": "perspective-a", "tenant_id": "tenant-b", "org_id": "org-b", "rule_edition_ref": "rule-b", "perspective_text": "確認観点"},
    )
    assert store.ingested_rows[0]["record_id"] == record_id
    assert {key: store.ingested_rows[0][key] for key in ("tenant_id", "org_id", "case_ref", "rule_edition_ref")} == {
        "tenant_id": "tenant-a", "org_id": "org-a", "case_ref": "case-a", "rule_edition_ref": "rule-a"
    }
    backend = _ScopedBackend(store.ingested_rows)
    node = KbRecallNode(scope_keys=("tenant_id", "org_id", "rule_edition_ref"), backend=backend, normalize_query=lambda value: str(value), top_k=8)
    result = node.execute({"tenant_id": "tenant-b", "org_id": "org-b", "rule_edition_ref": "rule-b", "validated_input": {"case_ref": "case-b"}})
    assert result["retrieved_records"] == []
    assert backend.calls == [{"tenant_id": "tenant-b", "org_id": "org-b", "rule_edition_ref": "rule-b"}]


def test_medium3_rule_edition_is_required_and_backend_mismatch_is_rejected():
    class BadBackend(_ScopedBackend):
        def search(self, *, scope: Mapping[str, str], query: str, top_k: int):
            self.calls.append(dict(scope))
            return self.records

    backend = BadBackend(
        [{"source_id": "perspective-a", "tenant_id": "tenant-a", "org_id": "org-a", "rule_edition_ref": "rule-old", "metadata": {"rule_edition_ref": "rule-old"}}]
    )
    node = KbRecallNode(scope_keys=("tenant_id", "org_id", "rule_edition_ref"), backend=backend, normalize_query=lambda value: str(value), top_k=8)
    missing = node.execute({"tenant_id": "tenant-a", "org_id": "org-a", "validated_input": {"case_ref": "case-a"}})
    assert missing["status"] == "error"
    assert backend.calls == []
    mismatched = node.execute({"tenant_id": "tenant-a", "org_id": "org-a", "rule_edition_ref": "rule-new", "validated_input": {"case_ref": "case-a"}})
    assert mismatched["retrieved_records"] == []
    assert mismatched["retrieval_rejected"]


def test_r5_retrieval_rejects_missing_conflicting_and_cross_partition_scope():
    """R-5: stored hit scope is mandatory and must match in every copy."""

    records = [
        {"source_id": "missing-all"},
        {"source_id": "partial", "tenant_id": "tenant-a"},
        {
            "source_id": "metadata-conflict",
            "tenant_id": "tenant-a",
            "org_id": "org-a",
            "rule_edition_ref": "rule-a",
            "metadata": {"org_id": "org-b"},
        },
        {
            "source_id": "cross-partition",
            "tenant_id": "tenant-b",
            "org_id": "org-b",
            "rule_edition_ref": "rule-a",
        },
        {
            "source_id": "valid",
            "tenant_id": "tenant-a",
            "org_id": "org-a",
            "rule_edition_ref": "rule-a",
            "metadata": {"section_no": "2"},
        },
    ]
    backend = _ScopedBackend(records)
    # Deliberately bypass filtering to model a broken adapter. Returned scope
    # still comes from each stored record; it is never copied from the request.
    backend.search = lambda *, scope, query, top_k: records[:top_k]
    node = KbRecallNode(
        scope_keys=("tenant_id", "org_id", "rule_edition_ref"),
        backend=backend,
        normalize_query=lambda value: str(value),
        top_k=8,
    )
    state = {
        "tenant_id": "tenant-a",
        "org_id": "org-a",
        "rule_edition_ref": "rule-a",
        "validated_input": {"case_ref": "case-a"},
    }
    result = node.execute(state)
    assert [record["source_id"] for record in result["retrieved_records"]] == ["valid"]
    assert len(result["retrieval_rejected"]) == 4

    backend.search = lambda *, scope, query, top_k: records[:4]
    degraded = node.execute(state)
    assert degraded["status"] == "success"
    assert degraded["retrieved_records"] == []
    assert "kb_empty" in degraded["degradation_reason"]


def test_i7_ingest_node_connects_directly_to_real_payload_store():
    """I-7: no resolver glue or domain resolve_rows override is needed."""

    scope = {"tenant_id": "tenant-a", "org_id": "org-a"}
    store = SdsRevisionStore()
    ref = store.payloads.raw_store.put(
        json.dumps(
            {
                "records": [
                    {
                        "perspective_ref": "perspective-a",
                        "section_no": "2",
                        "rule_edition_ref": "rule-a",
                        "perspective_text": "第2節の供給者根拠を確認する。",
                    }
                ]
            }
        ),
        scope=scope,
        session_id="session-a",
    )
    node = AccumulateIngestNode(
        store=store,
        trusted_scope=scope,
        session_id="session-a",
    )
    result = node.execute(
        {
            "payload_ref": ref,
            "tenant_id": "tenant-a",
            "org_id": "org-a",
            "case_ref": "case-a",
            "rule_edition_ref": "rule-a",
        }
    )
    assert "resolve_rows" not in AccumulateIngestNode.__dict__
    assert result["status"] == "success"
    assert result["ingest_summary"]["written_ids"] == ["perspective-1"]


def test_i8_state_cannot_supply_missing_trusted_payload_context():
    """I-8: state scope/session cannot repair missing constructor wiring."""

    scope = {"tenant_id": "tenant-a", "org_id": "org-a"}
    store = SdsRevisionStore()
    ref = store.payloads.raw_store.put(
        json.dumps({"records": []}),
        scope=scope,
        session_id="session-a",
    )
    node = AccumulateIngestNode(store=store)
    with pytest.raises(ValueError, match="trusted_scope"):
        node.resolve_rows(
            {
                "payload_ref": ref,
                "tenant_id": "tenant-a",
                "org_id": "org-a",
                "trusted_scope": scope,
                "session_id": "session-a",
            }
        )
    assert store.payloads.raw_store.resolve(
        ref,
        scope=scope,
        session_id="session-a",
    ) is not None


def _llm_boundary_fixture(llm):
    store = SdsRevisionStore()
    ref = store.payloads.raw_store.put(
        json.dumps({"sections": {"1": "製品識別"}}),
        scope={"tenant_id": "tenant-a", "org_id": "org-a"},
        session_id="session-a",
    )
    store.payloads.bind(
        ref,
        tenant_id="tenant-a",
        org_id="org-a",
        case_ref="case-a",
        session_id="session-a",
        correlation_id="corr-a",
    )
    state = {
        "payload_ref": ref,
        "tenant_id": "tenant-a",
        "org_id": "org-a",
        "case_ref": "case-a",
        "rule_edition_ref": "rule-a",
        "session_id": "session-a",
        "correlation_id": "corr-a",
        "source_catalog": [
            {"source_id": "src-draft-sec01", "section_no": 1, "source_kind": "revision_draft"}
        ],
    }
    return RevisionConsistencyLlmNode(config={"llm": llm}, store=store), state


def test_l4_unconfigured_llm_degrades_but_provider_failure_is_error(monkeypatch):
    """L-4: only the intentionally absent injection seam may fall back."""

    unconfigured, state = _llm_boundary_fixture(None)
    degraded = unconfigured.execute(state)
    assert degraded["status"] == "success"
    assert "llm_unavailable" in degraded["degradation_reason"]

    fallback = Mock(side_effect=AssertionError("fallback must not run"))
    monkeypatch.setattr("src.graph.domain_workflow_graph._build_fallback", fallback)

    class BrokenProvider:
        def __init__(self, exc):
            self.exc = exc

        def complete(self, _messages):
            raise self.exc

    failures = [
        PermissionError("auth failure"),
        ValueError("configuration failure"),
        TimeoutError("timeout failure"),
        RuntimeError("provider runtime failure"),
    ]
    for exc in failures:
        configured, configured_state = _llm_boundary_fixture(BrokenProvider(exc))
        failed = configured.execute(configured_state)
        assert failed["status"] == "error"
        assert failed["error_code"] == "llm_call_failed"
        assert "draft" not in failed and "llm_findings" not in failed

    class ExplodingResponse:
        def __str__(self):
            raise TypeError("response conversion failure")

    class BrokenConversion:
        def complete(self, _messages):
            return ExplodingResponse()

    configured, configured_state = _llm_boundary_fixture(BrokenConversion())
    failed = configured.execute(configured_state)
    assert failed["status"] == "error"
    assert failed["error_code"] == "llm_output_parse_failed"
    assert "draft" not in failed and "llm_findings" not in failed
    fallback.assert_not_called()


def test_l5_llm_exception_text_never_reaches_result_state_or_audit(caplog):
    """L-5: provider and response-conversion exception text stay ephemeral."""

    markers = ("provider-credential-AKIAFAKE", "response-credential-sk-fake")

    class ProviderFailure:
        def complete(self, _messages):
            raise RuntimeError(markers[0])

    class ExplodingResponse:
        def __str__(self):
            raise ValueError(markers[1])

    class ConversionFailure:
        def complete(self, _messages):
            return ExplodingResponse()

    with caplog.at_level("INFO", logger="agentcore.audit"):
        results = []
        states = []
        for llm in (ProviderFailure(), ConversionFailure()):
            node, state = _llm_boundary_fixture(llm)
            result = node.execute(state)
            results.append(result)
            states.append({**state, **result})

    assert [result["error_code"] for result in results] == [
        "llm_call_failed",
        "llm_output_parse_failed",
    ]
    serialized = repr({"results": results, "states": states, "audit": caplog.text})
    assert all(marker not in serialized for marker in markers)


@pytest.mark.parametrize(
    "factory",
    [
        lambda store: PreProcessNode(store=store),
        lambda store: SdsRevisionWorkflowGraphNode(config={"store": store, "llm": None, "retrieval": None, "top_k": 8, "rule_edition_ref": "rule-a"}),
        lambda store: PostProcessNode(),
        lambda store: DeterministicCheckNode(store=store),
        lambda store: KbRecallNode(scope_keys=("tenant_id", "org_id", "rule_edition_ref"), backend=None, normalize_query=lambda value: str(value), top_k=8),
        lambda store: RevisionConsistencyLlmNode(config={"llm": None}, store=store),
        lambda store: ClaimSourceVerifyNode(store=store),
        lambda store: AccumulateIngestNode(store=store),
        lambda store: AccumulateFeedbackNode(store=store),
    ],
)
def test_tc08_all_nine_nodes_deny_anonymous_before_execute(factory, monkeypatch):
    """TC-08: every outer and inner node blocks ANONYMOUS via __call__."""
    node = factory(SdsRevisionStore())
    spy = Mock(return_value={"status": "success"})
    monkeypatch.setattr(node, "execute", spy)
    result = node({"caller_trust_level": TrustLevel.ANONYMOUS.value, "correlation_id": "tc08"})
    assert result["status"] == "error"
    spy.assert_not_called()
