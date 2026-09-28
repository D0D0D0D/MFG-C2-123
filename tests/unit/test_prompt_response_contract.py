"""プロンプトの宣言どおりに答えたものが、検証を実際に通ることを固定する。

なぜ要るか（2026-08-12 の実測）:
  ``_validate_and_filter_llm_findings`` は ``_raw_item_keys`` の閉じたキー集合を厳密一致で
  要求し、外れた項目は ``SecurityViolationError`` になる。ところがプロンプトはカテゴリ名しか
  伝えておらず、実プロバイダは自分の語彙（``review`` / ``evidence`` / ``gap``）で答えて全件
  破棄された。業務的に的確なレビューが丸ごと捨てられ ``output`` は ``null`` になる。
  テストは全て緑のままだった — fake LLM が常に正しい形を返すため、
  「プロンプトが契約を伝えていない」欠陥はどのテストにも触れない。

検査の立て方:
  宣言と定数の一致を見ても意味がない（同じ定数から生成しているので常に一致する）。
  **宣言から機械的に応答を組み立てて検証器に通す** — 宣言が実態とズレた瞬間に赤くなる。
"""
from __future__ import annotations

import json
import re

import pytest
from framework.errors import SecurityViolationError

from src.graph import domain_workflow_graph as dwg
from src.nodes import main_node
from src.graph.domain_workflow_graph import (
    CATEGORY_ENUMS,
    FINDING_CATEGORIES,
    MAX_SECTION,
    MIN_SECTION,
    PROSE_LIMITS,
    SECTION_KEYS,
    SOURCE_ID_KEYS,
    _raw_item_keys,
    _response_contract,
    _valid_raw_item,
    _validate_and_filter_llm_findings,
)

_UNSET = object()   # None は「未指定」ではなく「None を入れる」なので sentinel で区別する
SOURCE_ID = "src-draft-sec02"
CATALOG = [{"source_id": SOURCE_ID, "section_no": 2, "source_kind": "revision_draft"}]


def _item_from_declaration(category: str, *, enum_value: object = _UNSET) -> dict:
    """プロンプトの item_schema だけを見て項目を組み立てる（実装を覗かない）."""
    spec = _response_contract()["item_schema"][category]
    item: dict = {}
    for key, declared in spec.items():
        if isinstance(declared, list):
            item[key] = declared[0] if enum_value is _UNSET else enum_value
        elif "integer" in declared:
            item[key] = 2
        elif "source_id" in declared:
            item[key] = SOURCE_ID
        else:
            item[key] = "宣言どおりに書いた本文"
    return item


def _draft(category: str, item: dict) -> dict:
    return {key: ([item] if key == category else []) for key in FINDING_CATEGORIES}


@pytest.mark.parametrize("category", FINDING_CATEGORIES)
def test_an_answer_built_only_from_the_declaration_is_accepted(category):
    """宣言に従って答えれば通ること。キー名・型・カテゴリ名がズレたら赤。"""
    accepted, dropped = _validate_and_filter_llm_findings(
        _draft(category, _item_from_declaration(category)), CATALOG
    )
    assert dropped == 0
    assert len(accepted[category]) == 1


@pytest.mark.parametrize("category", sorted(CATEGORY_ENUMS))
def test_every_declared_enum_value_is_actually_accepted(category):
    """宣言した enum 値が1つでも実際には弾かれるなら赤（宣言が実態より広い）。"""
    for value in _response_contract()["item_schema"][category][CATEGORY_ENUMS[category][0]]:
        accepted, _ = _validate_and_filter_llm_findings(
            _draft(category, _item_from_declaration(category, enum_value=value)), CATALOG
        )
        assert len(accepted[category]) == 1, f"{category}: 宣言値 {value!r} が拒否された"


# 宣言外の enum 値の負例。任意文字列は試せない（有限標本）ので、
# 「validator だけを 1 語ぶん広げる」変異は原理的に取り逃す — その穴は残る前提で、
# 現実に書かれうる語（拡張枠・その他・未知）を並べて実効性を上げる。
UNDECLARED_ENUM_VALUES = [
    "not_a_declared_kind", "custom_extra", "other", "unknown", "misc", "n/a", "",
]


@pytest.mark.parametrize("category", sorted(CATEGORY_ENUMS))
@pytest.mark.parametrize("value", UNDECLARED_ENUM_VALUES)
def test_an_undeclared_enum_value_is_rejected(category, value):
    """宣言外の値が通るなら、宣言が実態より狭い（モデルに嘘の制約を与えている）。"""
    with pytest.raises(SecurityViolationError):
        _validate_and_filter_llm_findings(
            _draft(category, _item_from_declaration(category, enum_value=value)), CATALOG
        )


@pytest.mark.parametrize("category", sorted(CATEGORY_ENUMS))
def test_a_non_string_enum_value_is_rejected(category):
    for value in (None, 123, True, ["x"]):
        with pytest.raises(SecurityViolationError):
            _validate_and_filter_llm_findings(
                _draft(category, _item_from_declaration(category, enum_value=value)), CATALOG
            )


@pytest.mark.parametrize("category", FINDING_CATEGORIES)
def test_a_key_absent_from_the_declaration_is_rejected(category):
    """宣言に無いキーを足すと拒否される＝宣言はキーの完全な一覧である。"""
    item = _item_from_declaration(category)
    item["severity"] = "high"
    with pytest.raises(SecurityViolationError):
        _validate_and_filter_llm_findings(_draft(category, item), CATALOG)


@pytest.mark.parametrize("category", FINDING_CATEGORIES)
def test_dropping_a_declared_key_is_rejected(category):
    """宣言したキーを1つ落とすと拒否される＝宣言に必須でないキーは無い。"""
    for key in sorted(_raw_item_keys(category)):
        item = _item_from_declaration(category)
        item.pop(key)
        with pytest.raises(SecurityViolationError):
            _validate_and_filter_llm_findings(_draft(category, item), CATALOG)


@pytest.mark.parametrize("category", FINDING_CATEGORIES)
def test_declared_prose_limit_is_the_boundary_actually_enforced(category):
    """「最大 N 文字」の N が実際の受理境界であること（N は可・N+1 は不可）。"""
    spec = _response_contract()["item_schema"][category]
    prose_keys = [key for key, declared in spec.items()
                  if isinstance(declared, str) and "at most" in declared]
    assert prose_keys, f"{category} に prose フィールドの宣言が無い"
    for key in prose_keys:
        limit = PROSE_LIMITS[key]
        assert f"at most {limit} characters" in spec[key]
        item = _item_from_declaration(category)
        item[key] = "あ" * limit
        assert _valid_raw_item(category, item), f"{category}.{key}: 宣言上限ちょうどが拒否された"
        item[key] = "あ" * (limit + 1)
        assert not _valid_raw_item(category, item), f"{category}.{key}: 宣言上限超過が受理された"


def test_server_assigned_fields_are_named_and_actually_rejected():
    """server 採番フィールドを名指しで禁止し、実際に出すと拒否されること。"""
    text = _response_contract()["server_assigned_fields"]
    for field in ("finding_id", "span_locator", "provenance"):
        assert field in text
        item = _item_from_declaration("sections")
        item[field] = "x"
        with pytest.raises(SecurityViolationError):
            _validate_and_filter_llm_findings(_draft("sections", item), CATALOG)


def test_build_prompt_actually_embeds_the_contract():
    """契約関数を作っただけで ``_build_prompt`` に配線し忘れる変異を捕まえる。"""
    from src.graph import domain_workflow_graph as dwg

    class _Store:
        def resolve_payload(self, **_kwargs):
            return {"sections": {"2": "本文"}}

    state = {
        "payload_ref": "ref-1", "tenant_id": "t", "org_id": "o", "case_ref": "c",
        "session_id": "s", "correlation_id": "x", "rule_edition_ref": "rule-1",
        "source_catalog": [], "recall_refs": [],
    }
    prompt = json.loads(dwg._build_prompt(state, _Store()))
    assert prompt["response_contract"] == _response_contract()
    assert "item_schema" in prompt["response_contract"], "カテゴリ名だけの旧実装に戻っている"


# --- 宣言の内容そのものへの不変条件 --------------------------------------------
# ここから下は「同じ定数を再計算した一致 assert」ではなく、実装が何であろうと
# 成り立っていなければならない性質を見る。上の振る舞い検査だけでは、宣言側の
# 数値・指示文・カテゴリ一覧を削っても全緑のまま通ってしまう（2026-08-12 の
# レビューで指摘され、変異 4 種で再現した）。

@pytest.mark.parametrize("category", FINDING_CATEGORIES)
def test_declared_section_range_is_the_boundary_actually_enforced(category):
    """宣言した ``N..M`` の両端が受理され、その外側が拒否されること。

    検証器の範囲だけを 1..15 に狭めても、item を常に section 2 で組み立てていると
    気づけない。宣言文から範囲を読み取って両端と外側を実際に通す。
    """
    spec = _response_contract()["item_schema"][category]
    section_fields = [key for key in spec if key in SECTION_KEYS]
    assert section_fields, f"{category} に section フィールドの宣言が無い"
    for key in section_fields:
        low, high = (int(part) for part in re.search(r"(\d+)\.\.(\d+)", spec[key]).groups())
        for accepted in (low, high):
            item = _item_from_declaration(category)
            item[key] = accepted
            assert _valid_raw_item(category, item), f"{category}.{key}: 宣言範囲内の {accepted} が拒否された"
        for rejected in (low - 1, high + 1):
            item = _item_from_declaration(category)
            item[key] = rejected
            assert not _valid_raw_item(category, item), f"{category}.{key}: 宣言範囲外の {rejected} が受理された"


@pytest.mark.parametrize("category", sorted(CATEGORY_ENUMS))
def test_enum_values_belonging_to_another_category_are_rejected(category):
    """他カテゴリの enum 値も拒否されること。

    未宣言値の負例が 1 種類だけだと、「その 1 語だけ弾き、別の未宣言値は通す」
    という変異をすり抜ける。実在する隣接カテゴリの語彙で試す。
    """
    declared = set(_response_contract()["item_schema"][category][CATEGORY_ENUMS[category][0]])
    foreign = [value for other, (_field, values) in CATEGORY_ENUMS.items() if other != category
               for value in values if value not in declared]
    assert foreign, "他カテゴリ由来の非宣言値が見つからない"
    for value in foreign:
        with pytest.raises(SecurityViolationError):
            _validate_and_filter_llm_findings(
                _draft(category, _item_from_declaration(category, enum_value=value)), CATALOG
            )


def test_contract_states_the_instructions_the_model_depends_on():
    """指示文そのものを落としたり反転したりすると赤くなること。"""
    contract = _response_contract()
    assert contract["all_free_text_requires_source_id"] is True
    assert contract["allowed_categories"] == list(FINDING_CATEGORIES)
    fmt = contract["output_format"]
    assert "exactly" in fmt, "キーの厳密一致を要求する指示が消えている"
    assert "unknown source_id is dropped" in fmt
    forbid = contract["server_assigned_fields"]
    assert "Never emit" in forbid, "server 採番フィールドの禁止が禁止でなくなっている"


def test_every_validated_key_is_classified_by_the_contract_builder():
    """検証器のキーがすべて種別に分類されること（未分類は生成時に落ちる）。"""
    classified = SECTION_KEYS | SOURCE_ID_KEYS | set(PROSE_LIMITS)
    for category in FINDING_CATEGORIES:
        enum_field = CATEGORY_ENUMS.get(category, (None, None))[0]
        unclassified = _raw_item_keys(category) - classified - {enum_field}
        assert not unclassified, f"{category}: 種別未定義のキー {unclassified}"


def test_prose_declaration_states_the_non_blank_requirement():
    """空白だけの本文は実際に拒否されるので、宣言も非空白を述べること。"""
    spec = _response_contract()["item_schema"]["sections"]
    assert "non-blank" in spec["observation"]
    item = _item_from_declaration("sections")
    item["observation"] = "   "
    assert not _valid_raw_item("sections", item)


# --- 業務不変条件（実装をどう変えても動かせない性質） -----------------------
# GHS/JIS Z 7253 の SDS は 16 節。この 1..16 は設計値ではなく法定の節番号なので、
# 定数化した MIN_SECTION / MAX_SECTION を動かせば宣言も検証も一緒に動いてしまう
# （MAX_SECTION=17 に変異しても 83 件が全緑になった。2026-08-12 再レビュー）。
# ここは値そのものと、他レイヤが同じ範囲を使っていることを固定する。

def test_section_range_is_the_ghs_sixteen_sections():
    assert (MIN_SECTION, MAX_SECTION) == (1, 16), "GHS SDS は 16 節。設計値ではなく法定の節番号"


@pytest.mark.parametrize("section_no", [1, 8, 16])
def test_every_layer_accepts_the_same_section_numbers(section_no):
    """節番号の受理範囲が層ごとにズレていないこと（内側で通って最終 gate で落ちる状態を防ぐ）。"""
    assert dwg._section(section_no)
    assert main_node._section(section_no)
    assert dwg._sections_from_payload({"sections": {str(section_no): "本文"}}) == {section_no: "本文"}


@pytest.mark.parametrize("section_no", [0, 17])
def test_every_layer_rejects_the_same_out_of_range_numbers(section_no):
    assert not dwg._section(section_no)
    assert not main_node._section(section_no)
    assert dwg._sections_from_payload({"sections": {str(section_no): "本文"}}) == {}


@pytest.mark.parametrize("category", FINDING_CATEGORIES)
def test_section_fields_reject_bool_and_float(category):
    """``True`` は int のサブクラスなので、素直に書くと節番号 1 として通る。"""
    spec = _response_contract()["item_schema"][category]
    for key in [k for k in spec if k in SECTION_KEYS]:
        for bad in (True, False, 2.0, "2"):
            item = _item_from_declaration(category)
            item[key] = bad
            assert not _valid_raw_item(category, item), f"{category}.{key}: {bad!r} が節番号として受理された"


@pytest.mark.parametrize("category", FINDING_CATEGORIES)
def test_every_prose_field_rejects_blank(category):
    """非空白要求が prose フィールド全部に掛かっていること（1 つだけ緩む変異を捕まえる）。"""
    spec = _response_contract()["item_schema"][category]
    prose_keys = [k for k, v in spec.items() if isinstance(v, str) and "at most" in v]
    assert prose_keys
    for key in prose_keys:
        item = _item_from_declaration(category)
        item[key] = "  \u3000 "
        assert not _valid_raw_item(category, item), f"{category}.{key}: 空白のみが受理された"


def test_deterministic_fallback_covers_exactly_the_declared_categories():
    """LLM 未設定経路の出力キーが宣言カテゴリと一致すること（カテゴリ追加時に片方だけ増えるのを防ぐ）。"""
    empty = dwg._build_fallback({"source_catalog": []})
    assert set(empty) == set(FINDING_CATEGORIES)
    seeded = dwg._build_fallback(
        {"source_catalog": [{"source_id": SOURCE_ID, "section_no": 2, "source_kind": "revision_draft"}]}
    )
    assert set(seeded) == set(FINDING_CATEGORIES)
    for category, items in seeded.items():
        for item in items:
            assert set(item) == _raw_item_keys(category), f"{category}: fallback が閉じた schema から外れている"
            assert _valid_raw_item(category, item), f"{category}: fallback 自身が検証を通らない"
