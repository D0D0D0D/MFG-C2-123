# テスト仕様 — MFG-C2-123 SDSRevisionEvidenceAgent

## 方針

外部 LLM・DB・KB はすべて注入シームの fake で置換し、テストからネットワークへ接続しない。SDS 本文は
payload store にだけ置き、State・checkpoint・公開出力に現れないことを境界テストで検証する。

## Framework compliance

| ID | 検証 | 期待結果 | 実装テスト |
|---|---|---|---|
| TC-01 | `SdsRevisionState` は flat TypedDict | JSON 直列化可能、credential field なし | `test_state_safety.py` |
| TC-02 | malformed envelope / S-3 違反 | fail-closed (`SecurityViolationError` または error status) | `test_s3_acceptance.py` |
| TC-03 | credential が State にない | 静的 State safety pass | `test_state_safety.py` |
| TC-04 | InvocationContext | `configurable` 以外に保存しない | `test_sds_workflow.py` |
| TC-05 | lifecycle event の重複なし | node_start/complete/error を実装側で発行しない | `test_pb_invoke_order.py` |
| TC-06 | S-2 final gate | override は import 時 TypeError | `test_framework_compliance_tc06_tc07.py` |
| TC-07 | S-3 final gate | override は import 時 TypeError | `test_framework_compliance_tc06_tc07.py` |
| TC-08 | outer/inner 9 ノードの trust | ANONYMOUS では execute が一度も呼ばれない。加えて PB-6 の常設フィクスチャで **`execute()` が呼ばれなかったこと**を証明する（`test_pb_invoke_order.py` の `test_s1_denial_refuses_execution_before_execute` — `_PrivilegedTrustGateFixture` を不足 trust で `node(state)` 呼びし、`status=error` / `error_log[0]` の `S-1 trust gate denied` / `events == ["s1_denied"]` / `execute` 未呼び出し を assert。正本 docs/03 の TC-08 要求） | `test_runtime_boundary_security.py::test_tc08_all_nine_nodes_deny_anonymous_before_execute` |

## Proof of boundary

| ID | 境界 | 期待結果 | 実装テスト |
|---|---|---|---|
| PB-1 | audit | ドメインイベントを各 stage が発行 | unit/integration node tests |
| PB-2 | State serialization | primitives のみ | `test_state_safety.py` |
| PB-3 | external services | LLM/KB/DB は injection fake | `test_sds_workflow.py` |
| PB-4 | import isolation | Level 0 import なし | `test_import_isolation.py` |
| PB-5 | checkpoint safety | 実 InMemorySaver・node return・公開出力・監査イベントに原文 marker がない | `test_runtime_boundary_security.py::test_pb5_runtime_marker_absent_from_node_result_output_checkpoint_and_audit` |
| PB-6 | invoke order | S-1 → S-2 → execute → S-3 の順 | `test_pb_invoke_order.py` |
| PB-7 | HITL | **Auto-waived — non-HITL** (`hitl.enabled: false`) | conditional skip |

## 受入テスト

| ID | 期待結果 |
|---|---|
| AT-01a/01b/01c | declared schema のみ accept、brief/evidence の未宣言キー reject |
| AT-02a/02b/02c/02d | 正常 key accept、canonical/別名を含む classification key reject |
| AT-03a/03b/03c/03d/03e/03f/03g/03h/03i/03j | finding と evidence の source 集合・正本属性・scope・A/B 行・非空根拠を完全一致で検証 |
| AT-04a/04b | current-case source ID の実在のみ許可 |
| AT-05a/05b/05c | observation/support/question enum 閉包 |
| AT-06a/06b | section 1..16 の整数のみ |
| AT-07a/07b | nested string leaf の credential を拒否 |
| AT-08a/08b/08c/08d | gate_kind 宣言必須、ingest/feedback では verifier key を拒否 |
| AT-09 | client payload_ref は scrub され server ref に置換 |
| AT-10 | case table の approved/published/superseded は error |
| AT-11 | KB 空/LLM 不達は success + degradation enum |
| AT-12 | S-3 通過後 `_verify_*` を pop |
| AT-13 | 同一 lease で Stage 1/3/4 の複数 resolve が可能 |
| AT-14 | lease close 後・scope 不一致は fail-closed |
| AT-15a | catalog 内 source ID のみ accept |
| AT-15b | LLM の finding_id/span_locator/provenance は schema reject |
| AT-15c | catalog 外 source ID の finding は drop + `llm_schema_violation` |
| AT-16 | `degradation_reason` は固定 enum のみ |
| AT-17 | プロンプトの `response_contract` が検証側の受理条件（キー集合・enum・prose 上限）を宣言する |
| AT-18 | provider 例外時、inner の `error_code` が呼び出し元まで届く（traceback / 生の例外文は載らない） |
| AT-18b | 未知の inner 失敗は allowlist で汎用コードに縮退する（AT-18 の対照実験） |
| AT-19 | LLM 由来 `source_id` が非 str / 空なら扱い済みのスキーマ違反として fail-closed（`TypeError` を漏らさない） |

### 指摘対応の回帰テスト対応表

| ID | 実テスト |
|---|---|
| AT-03i | `tests/unit/test_s3_acceptance.py::test_AT_03i_requires_two_distinct_inconsistency_evidence_rows` |
| AT-08c/AT-08d | `tests/unit/test_s3_acceptance.py::test_AT_08c_*` / `test_AT_08d_*` |
| AT-09 | `tests/unit/test_entry_adapter_acceptance.py::test_AT_09_server_invoke_ignores_client_payload_ref` |
| AT-10 | `tests/integration/test_sds_workflow.py::test_AT_10_authoritative_non_draft_case_is_rejected` および `test_AT_10_unknown_case_ref_is_rejected_fail_closed` |
| AT-11 | `tests/integration/test_sds_workflow.py::test_AT_11_kb_empty_and_llm_absence_are_successful_degradation` |
| AT-13/AT-14 | `tests/integration/test_sds_workflow.py::test_AT_13_*` / `test_AT_14_*` |
| AT-15a/AT-15b/AT-15c | `tests/integration/test_sds_workflow.py::test_AT_15a_*` / `test_AT_15b_*` / `test_AT_15c_*` |
| AT-17 | `tests/unit/test_prompt_response_contract.py`（全 87 件） |
| AT-18 / AT-18b | `tests/integration/test_sds_workflow.py::test_AT_18_provider_failure_reaches_the_caller_as_a_machine_readable_code` / `::test_AT_18b_unrecognised_inner_failure_degrades_to_the_generic_code` |
| AT-19 | `tests/integration/test_sds_workflow.py::test_AT_19_non_string_llm_source_id_is_a_handled_schema_violation`（5 パラメータ） |

`test_runtime_boundary_security.py` は supplier metadata の server-side canonicalization、ingest の verified scope 永続化、
tenant/org/rule edition の3軸 retrieval scope、実 checkpointer の原文 marker 非残留も結合で確認する。

主な実装箇所は `tests/unit/test_s3_acceptance.py` と
`tests/integration/test_sds_workflow.py`。各 reject ケースは本番と同じ
`config/config.yaml` の runtime 値（top_k=8、temperature=0.0、max_retry=3、timeout_s=30）を前提にする。

## 実行結果

実行コマンド: `.venv/bin/python -m pytest -q`

実行結果（2026-07-28）: socket bind を要する scaffold 自己回帰テストを除き **417 passed, 2 skipped**。
PB-7 の 2 skip は non-HITL 設計による想定結果である。sandbox は `HTTPServer` の bind を許可しないため、
未改変の `scripts/test_stg_invoke_evidence.py` 11件はオーケストレーション基盤/CI 環境で実行する。

実行結果（2026-08-12）: **498 passed, 2 skipped**。AT-17（87 件）を追加した。
プロンプトが応答スキーマを宣言していない欠陥は、fake LLM が常に正しい形を返すため既存 413 件では
検出できなかった。AT-17 は宣言と定数の一致ではなく、**宣言から機械的に組み立てた応答を
`_validate_and_filter_llm_findings` へ通す**形で検査する（一致 assert は同じ定数を参照するため常に真＝無意味）。
変異 11 種（検証側だけ独自 enum に戻す／宣言から source_id を隠す／宣言 prose 上限をずらす／
server 採番フィールドの禁止文言を消す・反転する／プロンプトを旧実装に戻す／検証器の section 上限を狭める／
output_format を空にする／allowed_categories を落とす／検証器が他カテゴリの enum も許可する／
prose の非空白要求を外す）で赤を確認済み。うち後半 6 種はレビュー指摘を再現して足したもので、
振る舞い検査だけでは全緑のまま通っていた。

さらに再レビューで、**GHS の節番号 1..16 が業務不変条件として固定されていない**ことが判明した
（`MAX_SECTION=17` へ変異しても 83 件が全緑。宣言も検証も同じ定数を見るため一緒に動く一方、
source 生成と最終 S-3 は 1..16 直書きで、宣言どおり section 17 を返すと内側で受理され最終 gate で落ちる）。
値そのものと、`dwg._section` / `main_node._section` / `_sections_from_payload` の受理範囲の一致を固定した。
併せて `_valid_raw_item` を `CATEGORY_ENUMS` / `PROSE_LIMITS` を読む形に統一し、「検証器だけを広げる」変異を
書けなくした。この過程で **非 str の enum 値（list / dict）が `TypeError` でノードを落とす**実装欠陥
（旧実装から存在）が見つかり、`SecurityViolationError` の fail-closed 経路へ寄せた。

⚠️ **限界の申告**: 宣言外 enum の負例は 7 種 + 非 str 4 種の**有限標本**であり、
「検証器だけを未知の 1 語ぶん広げる」変異は原理的に取り逃す。また実 LLM の所見は実行ごとに揺れる
（同一入力 3 回で、成分の分類を混合物へ適用する根拠を問う観点は 2 回出て 1 回出なかった）。
人間レビュー前提の設計なので致命的ではないが、**同一入力での再現性は保証しない**。

実行結果（2026-08-14 · 部品リフレッシュ Batch 4-4）: **528 passed, 2 skipped**（SDK は CI 正本と同じ
`agenticstar-agentcore[anthropic]==1.0.1` を `.venv` に実インストールして実行）。AT-18 / AT-18b / AT-19 を追加した。

`error_code` が呼び出し元まで届かない欠陥は、**ノード単体テストでは原理的に検出できない**（ノードは正しく
`error_code` を返しており、失われるのは state channel 宣言・inner `get_output()` 射影・`GraphNode` の
propagate という 3 つの hop）。end-to-end の `invoke()` でしか出ない。変異検査で有効性を確認済み:
①外側 `get_output()` の override を戻す ②`error_strategy` を `propagate` に戻す
③失敗分類器を allowlist ではなく inner `error_log` の素通しにする — **3 種とも赤**。
AT-19 は型ガードを外すと 5 パラメータ全部が赤。

### 実 LLM 1 回転（2026-08-14）

`work/scripts/mrb_live_llm_check.py`（注入シームに実 LLM クライアントを刺して `invoke` を 1 回実行）。
出力は縮退エンベロープではなく、`revision_brief` / `evidence_map` / `mode` / `gate_kind` に**非空の値**が入った
構造化ブリーフだった（`degradation_reason` は `partial_sections_submitted` / `kb_empty` で、入力が部分節・KB 空という
テスト入力の性質どおり）。テンプレートの `src/` は一切変更していない（一時テストを生成 → 実行 → 削除する方式）。

## llm-draft-stage 0.3.0 の回帰テスト（LLM 出力の複数 JSON）

AGENTIC STAR の登録モデルが draft JSON を改行区切りで 2 回連結して返す、または JSON の後ろに `{` `}` を含む散文を付ける応答で、
Stage の parse が `Extra data` になり fail-closed していた。部品 `llm-draft-stage` を 0.3.0 に更新し、複数候補を走査して
一意な非空 object だけを受理する（相異なる複数は `AmbiguousJsonOutput` で fail-closed・`{}` / NaN / 配列は不受理）。
対応テスト: `tests/parts/llm-draft-stage/test_draft_node.py`（実物 fixture `fixtures/gpt54mini_duplicated_decision.txt` を含む）。
