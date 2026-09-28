"""PB-1 / TC-05: every node emits at least one domain S-4 audit event.

The S-4 audit layer requires ``emit_trace_event()`` inside ``execute()`` for *every*
node, whether or not the node has a side effect.  ``node_start`` /
``node_complete`` / ``node_error`` are framework-emitted and therefore do not
satisfy the requirement.

This is a runtime proof rather than a static scan: the outer ``main`` node
delegates to a reusable part and shipped with no domain event at all while the
whole suite stayed green, so only observing a real invoke catches the gap.
"""

from __future__ import annotations

import json

import pytest
import shared.utils.audit_logger as audit_logger
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel

from src.graph.graph import SdsRevisionEvidenceGraph
from src.services.service import SdsRevisionStore, wire_services


FRAMEWORK_EVENTS = frozenset({"node_start", "node_complete", "node_error"})

# Which *module binding* must emit which event.  Attributing each event to the
# module that owns the node is the point of this test: asserting only on the set
# of event names lets one node cover for another's missing event, which is
# exactly how the outer main node shipped with no S-4 event while green.
EXPECTED_EVENT_SOURCES = {
    "src.nodes.pre_process_node": {"sds_envelope_validated"},
    "src.nodes.main_node": {"sds_subgraph_merged"},
    "src.nodes.post_process_node": {"sds_output_envelope_ready"},
    "src.graph.domain_workflow_graph": {
        "sds_deterministic_check_complete",
        "sds_kb_recall_complete",
        "sds_revision_draft_complete",
        "sds_claim_source_verified",
    },
}

EXPECTED_DOMAIN_EVENTS = {
    event for events in EXPECTED_EVENT_SOURCES.values() for event in events
}


def _run_invoke(monkeypatch) -> list[tuple[str, str]]:
    """Invoke the graph and return ``(emitting_module, event_type)`` pairs."""

    seen: list[tuple[str, str]] = []
    original = audit_logger.emit_trace_event

    def make_spy(module_name):
        def spy(event_type, payload=None, state=None):
            seen.append((module_name, event_type))
            return original(event_type, payload, state)

        return spy

    # Patch each module's own binding separately so the emitter is attributable.
    import sys

    patched = set()
    for name, module in list(sys.modules.items()):
        if getattr(module, "emit_trace_event", None) is original:
            monkeypatch.setattr(module, "emit_trace_event", make_spy(name), raising=False)
            patched.add(name)

    # A product module that never bound the symbol would make its expectations
    # vacuous, so fail loudly instead of passing silently.
    missing_bindings = set(EXPECTED_EVENT_SOURCES) - patched
    assert not missing_bindings, f"modules did not bind emit_trace_event: {sorted(missing_bindings)}"

    store = SdsRevisionStore(
        cases={
            "case-a": {
                "tenant_id": "tenant-a",
                "org_id": "org-a",
                "revision_status": "draft",
                "rule_edition_ref": "rule-a",
            }
        }
    )
    ref = store.payloads.raw_store.put(
        json.dumps({"sections": {"1": "Product identity"}}),
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
    graph = SdsRevisionEvidenceGraph(wire_services({"store": store, "top_k": 8}))
    ctx = InvocationContext(
        session_id="session-a",
        correlation_id="corr-a",
        caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
        hitl_allowed=False,
    )
    envelope = json.dumps(
        {
            "mode": "invoke",
            "tenant_id": "tenant-a",
            "org_id": "org-a",
            "case_ref": "case-a",
            "payload_ref": ref,
            "rule_edition_ref": "rule-a",
        }
    )
    result = graph.invoke(envelope, ctx=ctx)
    assert result["status"] == "success", result
    return seen


@pytest.mark.parametrize(
    "module_name, event_name",
    sorted((module, event) for module, events in EXPECTED_EVENT_SOURCES.items() for event in events),
)
def test_pb1_event_is_emitted_by_its_owning_node(monkeypatch, module_name, event_name):
    """PB-1: the node that owns the step is the node that emits its event.

    Asserting the (module, event) pair — not just the event name — is what makes
    this a real proof: another module emitting the same name cannot satisfy it.
    """

    emitted = _run_invoke(monkeypatch)
    assert (module_name, event_name) in emitted, (
        f"{event_name} was not emitted by {module_name}; "
        f"observed: {sorted(set(emitted))}"
    )


def test_pb1_domain_events_are_distinct_from_framework_events(monkeypatch):
    """TC-05: templates must not re-emit the framework lifecycle events."""

    emitted = _run_invoke(monkeypatch)
    domain = [event for _module, event in emitted if event not in FRAMEWORK_EVENTS]
    assert domain, "no domain audit event was emitted at all"
    assert EXPECTED_DOMAIN_EVENTS <= set(domain)
    # ``node_start``/``node_complete`` legitimately come from the framework's
    # ``BaseNode.__call__``.  The prohibition is on *template* code re-emitting
    # them from ``execute()``, so attribute by module rather than by name.
    duplicated = sorted(
        {
            (module, event)
            for module, event in emitted
            if event in FRAMEWORK_EVENTS and module.startswith("src.")
        }
    )
    assert not duplicated, f"template re-emitted framework lifecycle events: {duplicated}"


@pytest.mark.parametrize("event_name", sorted(EXPECTED_DOMAIN_EVENTS))
def test_pb1_each_expected_event_fires_exactly_once(monkeypatch, event_name):
    """A duplicated domain event signals a node emitting inside a loop."""

    emitted = [event for _module, event in _run_invoke(monkeypatch)]
    assert emitted.count(event_name) == 1, f"{event_name} fired {emitted.count(event_name)} times"
