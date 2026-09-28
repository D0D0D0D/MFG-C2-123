# MFG-C2-123 — SDSRevisionEvidenceAgent

> **Category**: Cat 2 (domain workflow)
> **Industry**: MFG

## Overview

SDSRevisionEvidenceAgent reviews a **pre-approval draft revision** of a Safety Data Sheet against
the supplier evidence offered for it, and returns a cited brief: which revised statements are
supported, where evidence is missing, where sections contradict one another, and what a reviewer
should verify before the revision goes forward.

An SDS revision is rarely wrong in one place. A hazard statement changed in section 2 has
consequences in handling, first aid and transport; the failure that matters is the section nobody
updated to match. The agent reads the revision as a whole and reports its findings in five
categories — sections, revision claims, gaps, inconsistencies and verification questions — each
carrying the source ids and span locators it rests on.

What the agent produces is review support and nothing more, and the boundary is structural rather
than advisory:

- **`requires_human_review` is always `true`.** It is not computed from confidence; it is a
  constant of the output contract. There is no path through this agent that produces a
  self-approving result.
- **Fields that would express a determination do not exist in the schema.** Classifying a
  chemical, authoring safety wording, releasing an SDS and deciding regulatory applicability are
  out of scope — and out of the response shape, not merely out of the prompt.
- **Only approval-pending drafts are accepted.** A case whose revision status is already approved
  or published is refused at intake, so the agent cannot be pointed at a released document.
- **Citations are verified against the case's own source registry.** Each finding names a source
  id and a span; an id or span that is not in the registry for this case is not published. The
  model writes the prose, and the deterministic layer decides which prose may be shown.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

## Integration

Provide:

- an AgentCore runtime;
- authentication middleware that establishes a **verified `tenant_id` and `org_id`**. Both are
  real partition keys here, not decoration: a pre-approval SDS revision is unpublished, and must
  not be readable from another organisation inside the same customer. Client headers are never
  read as a scope source;
- the customer's SDS case store, and optionally a knowledge base of past review perspectives;
- optionally a `BaseLLM` implementation through `config["llm"]`. `config.yaml` ships
  `llm: null`, so the default deployment runs the deterministic path, sets
  `degradation_reason: llm_unavailable`, and still returns `status: success` — a degraded review
  is a normal outcome here, not an error.

Runtime parameters read from `config/config.yaml` (defaults in parentheses):

| Key | Meaning |
|---|---|
| `top_k` (`8`) | Past review perspectives recalled. Higher than the usual default because SDS documents are long and multi-section |
| `temperature` (`0.0`) | Deterministic-leaning generation, appropriate for product-safety text |
| `max_retry` (`3`), `timeout_s` (`30`) | Framework retry and external-call timeout |
| `llm` (`null`) | The `BaseLLM` injection seam. No concrete client is named in the template |
| `memory_enabled` (`false`), `hitl.enabled` (`false`) | No checkpointer — review is a flag on a completed response, not a suspended run |

The template does not fetch documents, does not parse PDF or spreadsheet binaries, does not
classify chemicals, and does not publish anything. Input is structured text.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent resolves its secrets through the platform, and every route requires a verified tenant/org scope that the platform establishes. Without it, no route is reachable (see *Behaviour without the platform* below). Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Supplied by the platform's own package registry at build time. It is not published on public PyPI, so `pip install -e .` alone does not fetch it. |
| Python | >=3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

The module-level work at import is deliberately small: the runtime config is loaded and the secret
provider is created, but the **graph is built per request**, after authentication, so that the
verified scope is request-local and never shared between callers. The server therefore starts
without a platform, and `/health` answers `200`.

`POST /invoke` does not get far without one. Scope comes only from verified request state, so a
caller with no middleware behind it resolves to no `tenant_id` / `org_id` and is refused — there
is no header a client can set to supply its own. A bearer `INVOKE_AUTH_TOKEN` authenticates a
caller but establishes no identity, so it does not substitute for verified scope on the write
modes. The test suite runs without a platform connection, using injected fakes.

## Input

`POST /invoke` takes `mode` (`invoke` | `ingest` | `feedback`), `case_ref`, `payload`, and
optionally `rule_edition_ref` and `session_id`. An unsupported mode is **422**.

- **`invoke`** — review a revision. `payload` carries the revised `sections` and the
  `supplier_sources` offered as evidence for them. Accepted only when the case's revision status
  is an approval-pending draft.
- **`ingest`** — load past review perspectives and revision SOP content into the knowledge base.
- **`feedback`** — record review points a human confirmed, with an audit event.

```bash
curl -X POST https://<your-gateway>/invoke \
  -H 'Content-Type: application/json' \
  -d '{
    "mode": "invoke",
    "case_ref": "case-demo",
    "rule_edition_ref": "ghs-approved-edition",
    "payload": {
      "sections": {"2": "改訂した危険有害性の記載"},
      "supplier_sources": [{"section_no": 2, "text": "供給者根拠"}]
    },
    "session_id": "example-session"
  }'
```

The example assumes a gateway that establishes the caller's verified tenant and organisation.
Sent to a bare standalone server it is refused, by design.

The SDS body never enters graph state. The adapter stashes the payload in a short-lived store and
passes an opaque reference, leased to this one invocation and case and released when the call
finishes. A `payload_ref` supplied by a client is stripped before anything is minted, so a caller
cannot name someone else's stashed document.

## Output

The standard AgentCore envelope is preserved. For `invoke`, `output` carries every declared field
— a response missing one fails the output gate rather than being returned partial:

```json
{
  "mode": "invoke",
  "gate_kind": "draft",
  "revision_brief": {
    "sections": [],
    "revision_claims": [],
    "gaps": [],
    "inconsistencies": [],
    "verification_questions": []
  },
  "evidence_map": [],
  "requires_human_review": true
}
```

`ingest` returns `ingest_summary`; `feedback` returns `feedback_summary`. `degradation_reason`
names any stage that ran short — an absent model or an empty knowledge base narrows the brief and
says so. Values above are illustrative.

## Security and Limitations

Every node requires verified-external trust, and every store query is scoped by the verified
`tenant_id` and `org_id` at the service boundary rather than by a caller-supplied filter.

The document body is isolated from state and from checkpoints. It lives only in the short-lived
payload store, behind a lease bound to one invocation, one case and one session; the envelope that
reaches the graph carries a reference. Internal keys are scanned out of the formatted response
before it is returned, so a reference cannot leak outward through an echoed field.

Provenance is verified rather than trusted. Every cited source id and span locator is checked
against the source registry built for this case from the submitted revision and the supplier
evidence; a citation the registry does not contain is not published. Credential scanning on the
way out recurses through nested structures, so burying content deeper does not evade it.

Known limits:

- **The comparison basis is the evidence submitted with the case** plus the recalled perspectives,
  not a full reading of GHS, HazCom or REACH Annex II. Those frameworks are the context a human
  reviewer applies; the agent organises evidence for that review.
- **A brief reporting no gaps has not established that the revision is correct** — only that this
  analysis found none against the evidence provided.
- **Recall quality depends on an accumulated knowledge base.** An empty one still yields a brief,
  with the shortfall named.
- **Verified citation proves provenance, not truth.** A supported claim quotes a source that says
  that; whether the source is right is outside the agent's reach.
- **Default stores are in-process.** Nothing persists across a restart until real backends are
  wired.

Output is machine-generated and assistive. Classification, safety wording, release of the SDS and
the final regulatory determination remain with qualified people.

## Release

- Version: 1.0.0
- Category: fixed multi-step domain workflow (Cat 2), document-generation pattern
- Generation mode: `llm` (with a deterministic path when no model is injected)
- HITL: not enabled — `requires_human_review` is always true on a completed response
- Persistence: injectable case / knowledge-base seams plus a short-lived payload store; no
  framework checkpointer
- External API clients: deployment-injected; none bundled

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection, using injected fakes. Running the agent itself starts
cleanly with `uvicorn src.api.server:app`, but its routes need a gateway that establishes verified
tenant and organisation scope — see *Behaviour without the platform*.

## Documentation

`docs/02_design.md` carries the three-mode graph layout, the state schema, the output contract,
the payload-isolation design and the security-gate placement, together with the design decision
record. `docs/03_test_spec.md` records the test specification and the verification results.

## Project Structure

```
src/          agent implementation (nodes, services, schemas)
tests/        unit, integration and boundary tests
config/       agent configuration
docs/         design and test specifications
```

See `docs/02_design.md` for the design and `docs/03_test_spec.md` for the test specification.

## Customising

1. Adjust `config/` for your own environment and policies.
2. Replace the knowledge sources and sample data with your own.
3. Review the node implementations under `src/nodes/` for domain-specific logic.
4. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.

---
