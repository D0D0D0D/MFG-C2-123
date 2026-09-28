"""AGENTIC STAR Marketplace entry adapter for MFG-C2-123."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.base import SecretProvider
from framework.secrets.context import current_secrets
from shared.services.llm.base_llm import BaseLLM

try:
    from framework.utils.config_loader import load_agent_config
except ImportError:  # AgentCore 1.0.0 / 1.0.1 compatibility
    from framework.utils.config_loader import load_config

    def load_agent_config(agent_dir: Path) -> dict[str, Any]:
        config_path = agent_dir / "config" / "config.yaml"
        return load_config(str(config_path)) if config_path.exists() else {}


try:
    from shared.bootstrap import run_agent_marketplace
except ModuleNotFoundError as exc:  # AgentCore 1.0.0 / 1.0.1 compatibility
    if exc.name != "shared.bootstrap":
        raise
    _BOOTSTRAP_IMPORT_ERROR: ModuleNotFoundError | None = exc

    def run_agent_marketplace(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise RuntimeError(
            "The installed AgentCore wheel does not provide Marketplace bootstrap support"
        ) from _BOOTSTRAP_IMPORT_ERROR
else:
    _BOOTSTRAP_IMPORT_ERROR = None


try:
    from shared.secrets.env_provider import EnvProvider
except ModuleNotFoundError as exc:  # AgentCore 1.0.0 / 1.0.1 compatibility
    if exc.name != "shared.secrets.env_provider":
        raise

    class EnvProvider(SecretProvider):  # type: ignore[no-redef]
        """Minimal process-environment provider for pre-1.0.2 wheels."""

        def __init__(self, namespace: str = "", agent_name: str = "") -> None:
            self._namespace = namespace
            self._agent_name = agent_name

        def get(self, key: str, default: str | None = None) -> str | None:
            return os.environ.get(key, default)


from src.entry_adapter import SCOPE_KEYS, STG_DEFAULT_SCOPE_ENVS
from src.graph.graph import SdsRevisionEvidenceGraph
from src.services.service import ALLOWED_REVISION_STATUSES, SdsRevisionStore

_NAMESPACE = "mfg"
_AGENT_NAME = "MFG-C2-123"
_PROVIDER_AGENT_NAME = "mfg_c2_123"

_AZURE_OPENAI_SECRET_KEYS = (
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_ENDPOINT",
    "AZURE_OPENAI_DEPLOYMENT",
)
_CASE_REF_ENV = "STG_DEFAULT_CASE_REF"
_RULE_EDITION_REF_ENV = "STG_DEFAULT_RULE_EDITION_REF"
_DEPLOYMENT_ENV_KEYS = (
    *tuple(STG_DEFAULT_SCOPE_ENVS[key] for key in SCOPE_KEYS),
    _CASE_REF_ENV,
    _RULE_EDITION_REF_ENV,
)
_REQUIRED_ENV_KEYS = (*_AZURE_OPENAI_SECRET_KEYS, *_DEPLOYMENT_ENV_KEYS)
_ALLOWED_ENV_KEYS = frozenset(_REQUIRED_ENV_KEYS)


class _AllowlistedEnvironmentProvider(EnvProvider):  # type: ignore[misc]
    """Expose only this single-agent container's declared environment values."""

    def __init__(self) -> None:
        super().__init__(namespace=_NAMESPACE, agent_name=_PROVIDER_AGENT_NAME)

    def get(self, key: str, default: str | None = None) -> str | None:
        if key not in _ALLOWED_ENV_KEYS:
            return default
        value = super().get(key)
        return value if value is not None else default


def _require_clean(provider: SecretProvider, key: str) -> str:
    """Resolve one required deployment value without repairing it."""

    value = provider.require(key)
    if not value or any(character.isspace() for character in value):
        raise ValueError(f"Marketplace environment variable {key!r} must be non-empty and contain no whitespace")
    return value


def _require_azure_endpoint(provider: SecretProvider) -> str:
    """Reject ``AZURE_OPENAI_ENDPOINT`` values that are not a bare Azure resource endpoint.

    ``AzureOpenAIClient`` appends its own ``/openai/v1/`` path, so anything
    beyond a scheme and host silently produces a broken request URL, and the
    failure surfaces inside ``complete()``. A node that swallows that exception
    degrades quietly while the invocation still reports ``status: success``,
    which is why this has to be a startup check. The value is reported here,
    never repaired.
    """

    endpoint = _require_clean(provider, "AZURE_OPENAI_ENDPOINT")
    parts = urlsplit(endpoint)
    try:
        port = parts.port
    except ValueError:
        port = -1
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or port == -1
        or parts.path.strip("/")
        or parts.query
        or parts.fragment
        or any(character.isspace() for character in endpoint)
    ):
        raise ValueError(
            "Marketplace environment variable 'AZURE_OPENAI_ENDPOINT' must be a bare Azure "
            "resource endpoint -- scheme and host only, with no path (such as "
            "'/openai'), query or fragment"
        )
    return endpoint


def _deployment_context(provider: SecretProvider) -> dict[str, str]:
    """Resolve the complete State envelope context from deployment settings."""

    if not isinstance(SCOPE_KEYS, tuple) or len(set(SCOPE_KEYS)) != len(SCOPE_KEYS):
        raise RuntimeError("Marketplace SCOPE_KEYS must be a tuple without duplicates")
    if not isinstance(STG_DEFAULT_SCOPE_ENVS, dict) or set(STG_DEFAULT_SCOPE_ENVS) != set(SCOPE_KEYS):
        raise RuntimeError("Marketplace STG_DEFAULT_SCOPE_ENVS must exactly match SCOPE_KEYS")
    scope = {state_key: _require_clean(provider, STG_DEFAULT_SCOPE_ENVS[state_key]) for state_key in SCOPE_KEYS}
    return {
        **scope,
        "case_ref": _require_clean(provider, _CASE_REF_ENV),
        "rule_edition_ref": _require_clean(provider, _RULE_EDITION_REF_ENV),
    }


class _AzureOpenAIClientCompat(BaseLLM):
    """Small Azure chat-completion client for AgentCore 1.0.1."""

    def __init__(self, config: dict[str, str]) -> None:
        self._config = dict(config)

    def complete(self, messages: list[Any]) -> dict[str, Any]:
        endpoint = self._config["azure_endpoint"].rstrip("/")
        deployment = quote(self._config["azure_deployment"], safe="")
        request = Request(
            f"{endpoint}/openai/deployments/{deployment}/chat/completions?api-version=2024-10-21",
            data=json.dumps({"messages": messages}).encode("utf-8"),
            headers={"api-key": self._config["api_key"], "content-type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=30) as response:  # noqa: S310 - deployment-owned endpoint
            payload = json.loads(response.read().decode("utf-8"))
        choices = payload.get("choices", []) if isinstance(payload, dict) else []
        message = choices[0].get("message", {}) if choices else {}
        content = message.get("content", "") if isinstance(message, dict) else ""
        return {
            "content": content,
            "tool_calls": message.get("tool_calls", []) if isinstance(message, dict) else [],
            "model": payload.get("model", self._config["azure_deployment"]),
            "usage": payload.get("usage", {}),
        }

    def stream(self, messages: list[Any]) -> Iterator[str]:
        content = self.complete(messages).get("content", "")
        if content:
            yield str(content)

    def bind_tools(self, tools: list[Any]) -> "_AzureOpenAIClientCompat":
        if tools:
            raise NotImplementedError("Azure compatibility client does not support tool binding")
        return self


class _AzureMarketplaceLLM(BaseLLM):
    """Create a fresh Azure client and resolve credentials for every operation."""

    @staticmethod
    def _new_client() -> BaseLLM:
        provider = current_secrets()
        values = {key: _require_clean(provider, key) for key in _AZURE_OPENAI_SECRET_KEYS}

        # Azure is the only invocation-time import. Framework, security, graph,
        # and State imports remain eager for the runner's worker-thread path.
        try:
            from shared.services.llm.azure_openai_client import AzureOpenAIClient
        except ModuleNotFoundError as exc:  # AgentCore 1.0.0 / 1.0.1 compatibility
            if exc.name != "shared.services.llm.azure_openai_client":
                raise
            AzureOpenAIClient = _AzureOpenAIClientCompat  # type: ignore[misc,assignment]

        return cast(
            BaseLLM,
            AzureOpenAIClient(
                {
                    "api_key": values["AZURE_OPENAI_API_KEY"],
                    "azure_endpoint": values["AZURE_OPENAI_ENDPOINT"],
                    "azure_deployment": values["AZURE_OPENAI_DEPLOYMENT"],
                }
            ),
        )

    def complete(self, messages: list[Any]) -> dict[str, Any]:
        return cast(dict[str, Any], self._new_client().complete(messages))

    def stream(self, messages: list[Any]) -> Iterator[str]:
        yield from self._new_client().stream(messages)

    def bind_tools(self, tools: list[Any]) -> BaseLLM:
        return self._new_client().bind_tools(tools)


def _configure_marketplace_case(config: dict[str, Any], deployment: dict[str, str]) -> None:
    """Bind the one read-only Marketplace case without overriding a real record."""

    store = config.get("store")
    if not isinstance(store, SdsRevisionStore):
        raise RuntimeError("Marketplace requires the SDS revision store")
    case_ref = deployment["case_ref"]
    existing_raw = store.cases.get(case_ref)
    if existing_raw is None:
        store.cases[case_ref] = {
            "tenant_id": deployment["tenant_id"],
            "org_id": deployment["org_id"],
            "revision_status": "draft",
            "rule_edition_ref": deployment["rule_edition_ref"],
        }
        return
    expected = {
        "tenant_id": deployment["tenant_id"],
        "org_id": deployment["org_id"],
        "rule_edition_ref": deployment["rule_edition_ref"],
    }
    if any(existing_raw.get(key) != value for key, value in expected.items()):
        raise ValueError("Marketplace case record does not match deployment scope and rule edition")
    if existing_raw.get("revision_status") not in ALLOWED_REVISION_STATUSES:
        raise ValueError("Marketplace case record is not an approval-pending draft")


class MarketplaceGraph(SdsRevisionEvidenceGraph):
    """Marketplace-only read boundary, payload lease, scope, and Azure wiring."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        # The runner compiles before provision_secrets(). Validate deployment
        # identity now and give nodes a credential-free per-call LLM facade.
        deployment = _deployment_context(_AllowlistedEnvironmentProvider())
        marketplace_config = dict(config or {})
        marketplace_config.update(
            {
                "llm": _AzureMarketplaceLLM(),
                "rule_edition_ref": deployment["rule_edition_ref"],
                "trusted_scope": {key: deployment[key] for key in SCOPE_KEYS},
                "memory_enabled": False,
                "hitl": {"enabled": False},
            }
        )
        super().__init__(config=marketplace_config)
        _configure_marketplace_case(self.config, deployment)

    def invoke(
        self,
        user_input: str,
        session_id: str = "",
        ctx: InvocationContext | None = None,
        input_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # Marketplace conversation_history is untrusted free text outside the
        # SDS S-2 envelope. Rebuild context only from deployment-owned values.
        del input_context
        if not isinstance(user_input, str) or not user_input.strip():
            raise ValueError("Marketplace SDS input must be one non-empty plain string")
        deployment = _deployment_context(_AllowlistedEnvironmentProvider())
        scope = {key: deployment[key] for key in SCOPE_KEYS}
        base_ctx = (
            ctx
            if ctx is not None
            else (InvocationContext(session_id=session_id) if session_id else InvocationContext())
        )
        marketplace_ctx = replace(
            base_ctx,
            session_id=base_ctx.session_id or session_id,
            caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
            hitl_allowed=False,
        )
        if not marketplace_ctx.session_id:
            raise ValueError("Marketplace invocation requires a non-empty session_id")

        store = self.config.get("store")
        if not isinstance(store, SdsRevisionStore):
            raise RuntimeError("Marketplace requires the SDS revision store")
        payload_ref = store.payloads.raw_store.put(
            user_input,
            scope=scope,
            session_id=marketplace_ctx.session_id,
        )
        bound = False
        try:
            store.payloads.bind(
                payload_ref,
                tenant_id=deployment["tenant_id"],
                org_id=deployment["org_id"],
                case_ref=deployment["case_ref"],
                session_id=marketplace_ctx.session_id,
                correlation_id=marketplace_ctx.correlation_id,
            )
            bound = True
            canonical = json.dumps(
                {
                    "mode": "invoke",
                    "tenant_id": deployment["tenant_id"],
                    "org_id": deployment["org_id"],
                    "case_ref": deployment["case_ref"],
                    "payload_ref": payload_ref,
                    "rule_edition_ref": deployment["rule_edition_ref"],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            result = cast(
                dict[str, Any],
                super().invoke(
                    canonical,
                    session_id=marketplace_ctx.session_id,
                    ctx=marketplace_ctx,
                    input_context={
                        "mode": "invoke",
                        **scope,
                        "case_ref": deployment["case_ref"],
                        "rule_edition_ref": deployment["rule_edition_ref"],
                    },
                ),
            )
            if result.get("status") == AgentStatus.AWAITING_HUMAN.value:
                raise RuntimeError("AWAITING_HUMAN is not supported by AGENTIC STAR Marketplace")
            return result
        finally:
            if bound:
                store.payloads.close(
                    payload_ref,
                    tenant_id=deployment["tenant_id"],
                    org_id=deployment["org_id"],
                    case_ref=deployment["case_ref"],
                    session_id=marketplace_ctx.session_id,
                    correlation_id=marketplace_ctx.correlation_id,
                )
            else:
                store.payloads.raw_store.delete(
                    payload_ref,
                    scope=scope,
                    session_id=marketplace_ctx.session_id,
                )

    def provision_secrets(self, provider: SecretProvider) -> None:
        # The runner's provider may expose dotenv or other agents' keys. Compile
        # has completed; startup validates only this adapter's process env.
        del provider
        environment_provider = _AllowlistedEnvironmentProvider()
        for key in _REQUIRED_ENV_KEYS:
            _require_clean(environment_provider, key)
        _deployment_context(environment_provider)
        _require_azure_endpoint(environment_provider)
        super().provision_secrets(environment_provider)


def main() -> None:
    run_agent_marketplace(
        MarketplaceGraph,
        agent_name=_AGENT_NAME,
        namespace=_NAMESPACE,
        config=load_agent_config(Path(__file__).resolve().parent),
    )


if __name__ == "__main__":
    main()
