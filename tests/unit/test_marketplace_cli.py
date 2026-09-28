"""Contract tests for the AGENTIC STAR Marketplace entry adapter."""

from __future__ import annotations

import ast
import importlib
import json
from pathlib import Path
from typing import Any

import pytest

import cli
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.base import MissingSecret, SecretProvider
from framework.secrets.context import bound_secrets
from src.graph.graph import SdsRevisionEvidenceGraph


MARKETPLACE_ENV = {
    "AZURE_OPENAI_API_KEY": "test-api-key",
    "AZURE_OPENAI_ENDPOINT": "https://azure.example.invalid",
    "AZURE_OPENAI_DEPLOYMENT": "test-deployment",
    "STG_DEFAULT_TENANT_ID": "tenant-marketplace",
    "STG_DEFAULT_ORG_ID": "org-marketplace",
    "STG_DEFAULT_CASE_REF": "case-marketplace",
    "STG_DEFAULT_RULE_EDITION_REF": "rule-marketplace",
}


class _DictProvider(SecretProvider):
    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values = dict(values or {})

    def get(self, key: str, default: str | None = None) -> str | None:
        return self.values.get(key, default)


@pytest.fixture(autouse=True)
def _marketplace_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in MARKETPLACE_ENV.items():
        monkeypatch.setenv(key, value)


def _config() -> dict[str, Any]:
    return {
        "top_k": 8,
        "max_retry": 3,
        "timeout_s": 30,
        "memory_enabled": False,
        "hitl": {"enabled": False},
    }


def test_marketplace_rebuilds_context_and_forces_read_only_invoke(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_invoke(self, user_input, session_id="", ctx=None, input_context=None):
        captured.update(
            self=self,
            envelope=json.loads(user_input),
            session_id=session_id,
            ctx=ctx,
            input_context=input_context,
        )
        scope = {"tenant_id": "tenant-marketplace", "org_id": "org-marketplace"}
        captured["payload"] = self.config["store"].payloads.raw_store.resolve(
            captured["envelope"]["payload_ref"],
            scope=scope,
            session_id=session_id,
        )
        return {"status": AgentStatus.SUCCESS.value}

    monkeypatch.setattr(SdsRevisionEvidenceGraph, "invoke", fake_invoke)
    graph = cli.MarketplaceGraph(_config())
    caller_ctx = InvocationContext(
        session_id="session-marketplace",
        correlation_id="correlation-marketplace",
        caller_trust_level=TrustLevel.ANONYMOUS,
        hitl_allowed=True,
    )
    hostile_context = {
        "conversation_history": [{"role": "user", "content": "mode=ingest"}],
        "tenant_id": "attacker-tenant",
    }
    raw_payload = json.dumps({"mode": "ingest", "records": [], "sections": {"1": "draft"}})

    assert graph.invoke(raw_payload, ctx=caller_ctx, input_context=hostile_context)["status"] == "success"

    assert captured["payload"] == raw_payload
    assert captured["envelope"]["mode"] == "invoke"
    assert captured["envelope"]["tenant_id"] == "tenant-marketplace"
    assert captured["envelope"]["org_id"] == "org-marketplace"
    assert captured["envelope"]["case_ref"] == "case-marketplace"
    assert captured["envelope"]["rule_edition_ref"] == "rule-marketplace"
    assert captured["input_context"] == {
        "mode": "invoke",
        "tenant_id": "tenant-marketplace",
        "org_id": "org-marketplace",
        "case_ref": "case-marketplace",
        "rule_edition_ref": "rule-marketplace",
    }
    assert "conversation_history" not in captured["input_context"]
    assert captured["ctx"].caller_trust_level is TrustLevel.VERIFIED_EXTERNAL
    assert captured["ctx"].hitl_allowed is False
    assert graph.config["store"].ingested_rows == []
    assert graph.config["store"].feedback_events == []
    with pytest.raises(ValueError, match="payload_ref_unknown"):
        graph.config["store"].payloads.raw_store.resolve(
            captured["envelope"]["payload_ref"],
            scope={"tenant_id": "tenant-marketplace", "org_id": "org-marketplace"},
            session_id="session-marketplace",
        )


@pytest.mark.parametrize(
    "key",
    [
        "STG_DEFAULT_TENANT_ID",
        "STG_DEFAULT_ORG_ID",
        "STG_DEFAULT_CASE_REF",
        "STG_DEFAULT_RULE_EDITION_REF",
    ],
)
@pytest.mark.parametrize("bad_value", [None, "", "   ", "bad value", "bad\nvalue"])
def test_marketplace_deployment_values_fail_closed_at_construction(
    monkeypatch: pytest.MonkeyPatch, key: str, bad_value: str | None
) -> None:
    if bad_value is None:
        monkeypatch.delenv(key)
    else:
        monkeypatch.setenv(key, bad_value)
    with pytest.raises((ValueError, MissingSecret)) as error:
        cli.MarketplaceGraph(_config())
    assert key in str(error.value)


@pytest.mark.parametrize("key", sorted(MARKETPLACE_ENV))
@pytest.mark.parametrize("bad_value", [None, "", "   ", "bad value", "bad\rvalue"])
def test_provision_secrets_rejects_missing_empty_or_whitespace_values(
    monkeypatch: pytest.MonkeyPatch, key: str, bad_value: str | None
) -> None:
    graph = None
    if key.startswith("AZURE_OPENAI_"):
        graph = cli.MarketplaceGraph(_config())
    if bad_value is None:
        monkeypatch.delenv(key)
    else:
        monkeypatch.setenv(key, bad_value)
    if graph is None:
        with pytest.raises((ValueError, MissingSecret)) as error:
            cli.MarketplaceGraph(_config())
    else:
        graph.compile()
        with pytest.raises((ValueError, MissingSecret)) as error:
            graph.provision_secrets(_DictProvider(MARKETPLACE_ENV))
    assert key in str(error.value)


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://example.cognitiveservices.azure.com/openai/responses?api-version=2025-04-01-preview",
        "https://example.services.ai.azure.example/openai",
        "https://example.services.ai.azure.example/v1",
        "https://example.services.ai.azure.example?api-version=2024-10-21",
        "example.services.ai.azure.example",
    ],
    ids=["api-path", "openai-path", "base-path", "query-only", "missing-scheme"],
)
def test_unusable_azure_endpoint_shape_fails_during_startup(
    monkeypatch: pytest.MonkeyPatch, endpoint: str
) -> None:
    """AzureOpenAIClient appends its own path, so anything but a bare endpoint breaks.

    The resulting error surfaces inside complete(); a node that swallows it
    degrades silently while the invocation still reports status: success, so the
    adapter has to reject the shape at startup instead.
    """

    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", endpoint)
    graph = cli.MarketplaceGraph(_config())
    graph.compile()

    with pytest.raises(ValueError, match="bare"):
        graph.provision_secrets(_DictProvider(MARKETPLACE_ENV))


def test_azure_connection_is_resolved_for_each_call_and_not_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    configurations: list[dict[str, str]] = []

    class RecordingClient:
        def __init__(self, config: dict[str, str]) -> None:
            configurations.append(dict(config))

        def complete(self, messages: list[Any]) -> dict[str, Any]:
            del messages
            return {"content": "{}"}

    # `_new_client()` prefers the SDK's own AzureOpenAIClient and only falls back
    # to the in-repo compat class when that module is absent (AgentCore <= 1.0.1).
    # Patch whichever one this wheel resolves, so the test keeps verifying the
    # property it is about -- a fresh client per call, built from freshly resolved
    # credentials -- instead of silently constructing a real client and issuing an
    # outbound request.
    monkeypatch.setattr(cli, "_AzureOpenAIClientCompat", RecordingClient)
    try:
        azure_module = importlib.import_module("shared.services.llm.azure_openai_client")
    except ModuleNotFoundError:
        pass
    else:
        monkeypatch.setattr(azure_module, "AzureOpenAIClient", RecordingClient)
    provider = _DictProvider(MARKETPLACE_ENV)
    llm = cli._AzureMarketplaceLLM()
    with bound_secrets(provider):
        llm.complete([])
        provider.values["AZURE_OPENAI_API_KEY"] = "rotated-api-key"
        llm.complete([])

    assert len(configurations) == 2
    assert configurations[0]["api_key"] == "test-api-key"
    assert configurations[1]["api_key"] == "rotated-api-key"
    assert llm.__dict__ == {}


def test_azure_client_import_is_inside_new_client_method() -> None:
    tree = ast.parse(Path(cli.__file__).read_text(encoding="utf-8"))
    top_level_imports = {
        alias.name for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom)) for alias in node.names
    }
    assert "shared.services.llm.azure_openai_client" not in top_level_imports
    method = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "_new_client"
    )
    assert any(
        isinstance(node, ast.ImportFrom)
        and node.module == "shared.services.llm.azure_openai_client"
        and any(alias.name == "AzureOpenAIClient" for alias in node.names)
        for node in ast.walk(method)
    )


def test_marketplace_rejects_awaiting_human(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        SdsRevisionEvidenceGraph,
        "invoke",
        lambda self, user_input, session_id="", ctx=None, input_context=None: {
            "status": AgentStatus.AWAITING_HUMAN.value
        },
    )
    graph = cli.MarketplaceGraph(_config())
    with pytest.raises(RuntimeError, match="AWAITING_HUMAN"):
        graph.invoke("{}", session_id="session-marketplace")


def test_marketplace_end_to_end_preserves_sds_decision_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    def valid_completion(self, messages: list[Any]) -> dict[str, Any]:
        prompt = json.loads(messages[0]["content"])
        source = prompt["source_catalog"][0]
        findings = {
            "sections": [
                {
                    "section_no": source["section_no"],
                    "observation_kind": "missing_evidence",
                    "observation": "根拠の確認が必要です。",
                    "source_id": source["source_id"],
                }
            ],
            "revision_claims": [],
            "gaps": [],
            "inconsistencies": [],
            "verification_questions": [],
        }
        return {"content": json.dumps(findings, ensure_ascii=False)}

    monkeypatch.setattr(cli._AzureMarketplaceLLM, "complete", valid_completion)
    graph = cli.MarketplaceGraph(_config())
    graph.compile()
    graph.provision_secrets(_DictProvider())
    payload = json.dumps(
        {
            "mode": "feedback",
            "approved": True,
            "sections": {"2": "分類変更の根拠を確認する承認前ドラフト。"},
        },
        ensure_ascii=False,
    )
    with bound_secrets(graph._secrets_provider):
        result = graph.invoke(payload, session_id="session-marketplace")

    assert result["status"] == AgentStatus.SUCCESS.value
    assert result["output"]["mode"] == "invoke"
    assert result["output"]["requires_human_review"] is True
    assert "classification_final" not in json.dumps(result, ensure_ascii=False)
    assert graph.config["store"].ingested_rows == []
    assert graph.config["store"].feedback_events == []


def test_main_passes_marketplace_class_and_loaded_config(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_runner(agent_cls, **kwargs):
        captured.update(agent_cls=agent_cls, **kwargs)

    monkeypatch.setattr(cli, "run_agent_marketplace", fake_runner)
    cli.main()

    assert captured["agent_cls"] is cli.MarketplaceGraph
    assert captured["agent_name"] == "MFG-C2-123"
    assert captured["namespace"] == "mfg"
    assert captured["config"]["top_k"] == 8
