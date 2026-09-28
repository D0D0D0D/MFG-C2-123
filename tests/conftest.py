"""実 LLM 1 回転（work/scripts/mrb_live_llm_check.py）が要求する fixture。

このスクリプトは `agent_config` / `case_payload` を repo 側の fixture から解決する。
この AG は invoke 入力が payload_ref 経由（事前に payload store へ put + bind が要る）
なので、payload を直接投げる形では graph まで届かない。実運用と同じ経路を通すため、
統合テストと同じ payload store + bind 経路で ref を渡す。
"""
from __future__ import annotations

import json

import pytest

from src.services.service import SdsRevisionStore, wire_services

_LIVE_SCOPE = {"tenant_id": "tenant-live", "org_id": "org-live"}
_CASE_REF = "case-live"
_SESSION_ID = "live-1"
_RULE_EDITION_REF = "rule-live"


class _LiveRetrievalBackend:
    def search(self, *, scope, query, top_k):
        del query
        if (
            scope.get("tenant_id") != _LIVE_SCOPE["tenant_id"]
            or scope.get("org_id") != _LIVE_SCOPE["org_id"]
            or scope.get("rule_edition_ref") != _RULE_EDITION_REF
        ):
            return []
        return [
            {
                "source_id": "perspective-live-001",
                "source_kind": "internal_perspective",
                "summary": "改訂案の危険有害性分類、ばく露防止、廃棄上の注意は根拠確認が必要。",
                "tenant_id": _LIVE_SCOPE["tenant_id"],
                "org_id": _LIVE_SCOPE["org_id"],
                "rule_edition_ref": _RULE_EDITION_REF,
                "metadata": {
                    **_LIVE_SCOPE,
                    "rule_edition_ref": _RULE_EDITION_REF,
                    "source_doc": "sds-review-playbook",
                    "section_no": 2,
                },
            }
        ][:top_k]


def _live_payload() -> dict:
    sections = {
        str(index): f"第{index}節の改訂案。供給者資料と社内レビューで根拠確認が必要。"
        for index in range(1, 17)
    }
    sections["2"] = "危険有害性の要約。分類変更の根拠とラベル要素の整合確認が必要。"
    sections["8"] = "ばく露防止及び保護措置。許容濃度と局所排気の記載更新を確認する。"
    sections["13"] = "廃棄上の注意。廃棄区分と委託先条件の差分確認が必要。"
    return {
        "sections": sections,
        "revision_history": [{"date": "2026-08-01", "summary": "GHS分類と保護措置を改訂"}],
        "supplier_sources": [
            {
                "source_id": "srcA",
                "section_no": 2,
                "source_kind": "supplier_sds",
                "quote": "分類変更の根拠とラベル要素の整合確認が必要。",
            }
        ],
    }


@pytest.fixture
def _live_store_and_ref():
    store = SdsRevisionStore(
        cases={
            _CASE_REF: {
                **_LIVE_SCOPE,
                "revision_status": "draft",
                "rule_edition_ref": _RULE_EDITION_REF,
            }
        }
    )
    ref = store.payloads.raw_store.put(
        json.dumps(_live_payload(), ensure_ascii=False),
        scope=_LIVE_SCOPE,
        session_id=_SESSION_ID,
    )
    store.payloads.bind(
        ref,
        **_LIVE_SCOPE,
        case_ref=_CASE_REF,
        session_id=_SESSION_ID,
        correlation_id="live-1",
    )
    return store, ref


@pytest.fixture
def agent_config(_live_store_and_ref):
    store, _ = _live_store_and_ref
    return wire_services(
        {
            "store": store,
            "top_k": 8,
            "max_retry": 3,
            "timeout_s": 30,
            "retrieval": _LiveRetrievalBackend(),
            "memory_enabled": False,
            "hitl": {"enabled": False},
        }
    )


@pytest.fixture
def case_payload(_live_store_and_ref):
    _store, ref = _live_store_and_ref
    return {
        "mode": "invoke",
        **_LIVE_SCOPE,
        "case_ref": _CASE_REF,
        "payload_ref": ref,
        "rule_edition_ref": _RULE_EDITION_REF,
    }
