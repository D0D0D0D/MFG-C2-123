# Template Design Specification — MFG-C2-123 SDSRevisionEvidenceAgent

> 対象業務: 化学メーカー／輸入商社の製品安全（product stewardship）担当者が、**承認前の SDS 改訂ドラフト**と供給者根拠を、GHS/HazCom/REACH Annex II 及び顧客の社内改訂 SOP に照らして証拠レビューする工程。
> 承認済み提案（2026-07-23）に基づく設計書。

## Position in AgentCore Architecture

- **Agent Class**: `SdsRevisionEvidenceGraph`（`src/graph/graph.py`。設計フェーズでは scaffold 既定の `Graph` のままで、実装フェーズで `src/api/server.py` の importer と同一コミットで改名する — 下記「config 設計値」参照）
- **L1 Base**: **`AgentBaseGraph`**（L1 直継承）— 本テンプレートは固定 5 ノード backbone を用い、自律ループ（`AutonomousBaseGraph`）は使わない
- **Three-Layer Separation**:
  - State: flat TypedDict composition（Pydantic 不使用 — msgpack 非互換）
  - Node: L1 継承（Template Method: `execute(self, state) -> dict` のみ override）
  - Graph: composition（`register_nodes()` によるノード登録）

## Architecture Overview

### 3 モード

本テンプレートは 1 エンドポイント上で 3 つのモードを提供する。モードは入力の `mode` フィールドで指定し、**モード分岐は backbone の辺に持ち込まない** — `pre_process → main` は無条件辺であり、`route()` は `main` の後に `status` を見るだけである。モード分岐は `main`（`GraphNode`）の**内部**で行う。

| mode | 用途 | LLM | 主な出力 |
|------|------|-----|---------|
| `ingest` | 社内改訂 SOP・過去の SDS 改訂レビュー観点を構造化して DB / KB に蓄積する | 使わない | `ingest_summary`（`written_ids` / `rejected[]`） |
| `invoke` | 承認前 SDS 改訂ドラフトを GHS/REACH・社内 SOP に照らして照合し、レビューブリーフを起草する | Stage 3 のみ | `revision_brief` + `evidence_map` |
| `feedback` | 人間が確定・編集した論点を、明示承認と監査イベント付きで蓄積する | 使わない | `feedback_summary` |

`ingest` / `feedback` は LLM を経由しない（決定論経路）。蓄積は監査対象の書込であり、LLM の推論を挟む余地がないという設計判断による。

### Node Configuration（outer graph — 固定 backbone）

| Node | 責務 | Input State | Output State | Inherits/Overrides |
|------|------|-------------|--------------|-------------------|
| `initialize` | correlation_id / trace_id 採番 | — | `correlation_id`, `trace_id` | `InitializeNode`（framework 既定・変更しない） |
| `pre_process` | テナント/組織解決 fail-closed・モード検証・入力 allowlist・`payload_ref` の**サーバ発行性検証**（本文の隔離自体は入口 adapter で完了済み） | `user_input`, `input_context` | `mode`, `tenant_id`, `org_id`, `case_ref`, `validated_input`, `payload_ref` | `FunctionNode` → `PreProcessNode` |
| `main` | 内側 `SdsRevisionWorkflowGraph` を実行し、モードごとの結果だけを親へ返す。S-3 provenance 検証をこの境界で実施 | 上記すべて | モードスコープの結果フィールド | **`GraphNode`** → `SdsRevisionWorkflowGraphNode` |
| `post_process` | `formatted_output` エンベロープ組立・縮退の畳み込み | main の結果 | `formatted_output`, `requires_human_review` | `FunctionNode` → `PostProcessNode` |
| `finalize` | 実行時間・最終 status 確定 | — | `execution_time` | `FinalizeNode`（framework 既定・変更しない） |

> `initialize` / `finalize` は `super().register_nodes()` が自動注入する。テンプレートは `pre_process` / `main` / `post_process` の 3 スロットのみ埋める（いずれかが欠けると `compile()` が `MissingNodeError`）。`add_edges()` / `route()` は override しない。

### Node Configuration（inner `SdsRevisionWorkflowGraph` — 独自トポロジ）

内側グラフは `BaseGraph` を直接継承し、`initialize` / `finalize` を持たない（外側 backbone が持つため）。

| Node | Stage | 責務 | LLM |
|------|-------|------|-----|
| `deterministic_check` | Stage 1 | GHS 16 節の必須欄の有無・書式・改訂履歴欄の定型要件を validator で検証。あわせて**原文を canonical text に正規化し `source_id` / `span_locator` を採番して `case_sources` を構築**する（Stage 3 の LLM はこの `source_id` を引用として引く）。**意味的な整合結論は出さない** | なし |
| `kb_recall` | Stage 2 | 同一テナント/組織で絞った KB から、過去 SDS 改訂レビューの**照合観点**（source-linked・非結論的）を想起 | なし |
| `revision_consistency_llm` | Stage 3 | 改訂ドラフト × GHS/HazCom/REACH × 社内 SOP を当てはめ、改訂主張・節間整合・根拠不足を抽出。**所見の文章を執筆し、各所見に `source_id` と種別 enum を必ず付す** | **あり** |
| `claim_source_verify` | Stage 4 | Stage 3 の生出力から公開ブリーフを組み立てる — `finding_id` を決定論採番し、`span_locator` / `provenance` を `case_sources`（正本）から注入して `evidence_map` を構築。あわせて `source_id` 実在性と finding→evidence の完全一致を機械検証。不一致は fail-closed | なし |
| `accumulate_ingest` | — | 品質ゲート → サニタイズ → 正規化 → 書込 → `ingest_summary` | なし |
| `accumulate_feedback` | — | 承認済み論点の反映 + S-4 監査イベント | なし |

### inner `SdsRevisionWorkflowGraph` の `BaseGraph` ABC 実装（7 メソッド）

`BaseGraph` の `__abstractmethods__` は **7 つ**である（2026-07-27 に released wheel で実機確認: `name` / `state_schema` / `_validate_config` / `register_nodes` / `add_edges` / `route` / `get_output`）。3 つではない。全件を実装する。

| メソッド | 実装内容 |
|---|---|
| `name` | `"sds_revision_workflow"` |
| `state_schema` | `SdsRevisionWorkflowState`（内側専用の flat TypedDict。**`AgentState` で注釈しない**） |
| `_validate_config` | `llm` / `store` / `retrieval` / `top_k` / `rule_edition_ref` の存在検証。欠落は `ConfigError` |
| `register_nodes` | 上表 6 ノードを `self._nodes` へ直接代入。**`super().register_nodes()` は呼ばない**（内側は `initialize` / `finalize` を持たない） |
| `add_edges` | `add_conditional_edges(START, self.route)` + 各モードの直列辺（上の Data Flow 図のとおり） |
| `route` | `state["mode"]` と `state["status"]` を見てモード経路を返す。**引数は `SdsRevisionWorkflowState` で注釈**（`AgentState` 注釈だと射影でカスタム欄が消え誤 route する） |
| `get_output` | モードごとに、そのモードで実際に生成されたキーだけを返す。`AgentState` ではなく自スキーマで注釈する |

**inner `get_output()` と outer `merge_output()` の対応**:

| mode | inner `get_output()` が返すキー | outer `merge_output()` が親 state へ載せるキー |
|---|---|---|
| `invoke` | `revision_brief`, `evidence_map`, `cited_source_ids`, `case_sources`(検証用), `degradation_reason` | `revision_brief`, `evidence_map`, `requires_human_review`, `degradation_reason`, `gate_kind="invoke"`（`case_sources` は S-3 検証後に `pop`） |
| `ingest` | `ingest_summary` | `ingest_summary`, `gate_kind="ingest"` |
| `feedback` | `feedback_summary` | `feedback_summary`, `gate_kind="feedback"` |

`merge_output()` は**実行されたモードのフィールドだけを返す**（未実行モードのキーを `None` で埋めない）。`error_log` は非空時のみ、`node_history` は返さない（reducer 管理のため二重化する）。

### Data Flow

```
外側（framework 固定 backbone）:
START → initialize → pre_process → main → {route} → post_process → finalize → END
                                             ↓ (retry, max 3)
                                          pre_process

内側 SdsRevisionWorkflowGraph（mode による分岐は START の conditional edge のみ）:
invoke:   START → deterministic_check → kb_recall → revision_consistency_llm → claim_source_verify → END
ingest:   START → deterministic_check → accumulate_ingest → END
feedback: START → accumulate_feedback → END
error:    START → END（上流 error のパススルー）
```

> 内側グラフの分岐関数（`route`）と `get_output()` は **自スキーマ（`SdsRevisionWorkflowState`）で state を注釈する**。`AgentState` で注釈すると射影でカスタム欄が消え、誤 route する（Business Travel Hotel & Plan Recommendation Agent の実機事故）。

### 入口 adapter における機微本文の隔離契約（🔴 グラフ入場前に完了させる）

**`pre_process` での隔離では遅い。** SDK 1.0.0 の `BaseGraph.invoke()` は、ノードが 1 つも走る前に
`user_input` と `input_context` を **そのまま `initial_state` に載せる**（2026-07-27 に released wheel の
`inspect.getsource(BaseGraph.invoke)` で実機確認）。したがって `graph.invoke()` に SDS 改訂ドラフト全文を
渡した時点で、checkpointer 有効時にはその生本文が checkpoint に載る。`pre_process` がその後で
`payload_ref` に置き換えても、**入場時点の state はすでに永続化対象**である。

したがって隔離は **`src/api/server.py`（入口 adapter）が担う**。

| 段階 | 責務 |
|---|---|
| 入口 adapter（グラフ外） | リクエスト本文を受領 → **即座に** payload store へ保存し `payload_ref` を発行 → `graph.invoke()` には **`payload_ref` / `case_ref` / 列挙値・識別子だけ**を渡す |
| 入口 adapter の起動時 | 🔴 **`config/config.yaml` をロードして型検証し `Graph(config=runtime_config)` で生成する**。scaffold 既定の `src/api/server.py` は `Graph()` を**設定なしで**生成しており、新設 runtime config を読まない。実装フェーズでこのロードを実装する（未実装のままだと `top_k` / `temperature` / `timeout_s` が設計値どおりに効かない） |
| `graph.invoke()` の引数 | `user_input` = canonical envelope の直列化（**本文を含まない**）。`input_context` にも本文を入れない |
| `pre_process` | 受け取った `payload_ref` が**サーバ発行のものか**を検証する（クライアント由来の ref は無条件破棄）。テナント/組織の解決と fail-closed |
| 本文の解決 | 本文が必要な Stage（Stage 1 / Stage 3）で `payload_ref` から store 経由で読み出し、**ノード戻り値には載せない** |

- `payload_ref` は **サーバ発行・英字のみ**（数字を含む ref は S-2 の PII ゲートが `[MASKED]` に破壊書換する）。
- **PB-5 で invoke 前後の checkpoint を実検査**し、SDS 本文・組成・供給者名が checkpoint に現れないことを証明する（設計上「入れていない」という主張ではなく、実 checkpointer での検査で示す）。

#### 🔴 read lease — 原文は 1 回の invocation 内で複数回読まれる

原文は **Stage 1（欄・書式検証）/ Stage 3（当てはめ）/ 出力表示（`source_id` + `span_locator` → 引用本文の解決）** の
最低 3 箇所で必要になる。素朴な **single-use トークン**にすると 2 回目以降が読めず破綻する。

| 契約 | 内容 |
|---|---|
| 発行 | `payload_ref` の発行は 1 回（入口 adapter）。ref 自体は再利用されない |
| 読み出し | 同一 invocation（同一 `correlation_id` + `case_ref` + scope）に**閉じた read lease** を張り、その内側では複数回 `resolve()` してよい |
| scope 束縛 | lease は `tenant_id` / `org_id` / `case_ref` / `correlation_id` に束縛。scope 不一致の `resolve()` は拒否 |
| 失効 | invocation 終了時、または TTL 到達で失効。以後の `resolve()` は拒否（replay 防止） |
| 本文の扱い | `resolve()` の戻り値は **その場で使い切る**。State / ノード戻り値 / `result` に載せない |

S-3 の provenance 検証（`source_id` + `span_locator` から原文を引く）も、この lease 経由で payload store の
resolver を呼ぶ。`case_sources` は `payload_ref` と `span_locator` を持つだけで**本文は保持しない**。

### AGENTIC STAR Marketplace entry adapter

リポジトリ直下の `cli.py` は `SdsRevisionEvidenceGraph` を継承した `MarketplaceGraph` を公開する。
Marketplace runner の単一文字列入力は SDS payload として短命 store に退避し、graph には
`mode="invoke"`、デプロイ所有の識別子、サーバ発行 `payload_ref` だけを含む canonical envelope を渡す。
runner が渡す `input_context`（`conversation_history` を含む）はマージせず破棄し、同じデプロイ所有識別子だけで
再構築する。したがって Marketplace から `ingest` / `feedback` の書込経路は選択できない。

State 参照を outer/inner graph と node 実装から追跡した結果は次のとおり。`input_context.get(...)` の検索だけには
依存していない。

| 読取元 | 読む State |
|---|---|
| framework outer lifecycle | `user_input`, `input_context`, `session_id`, `correlation_id`, `caller_trust_level`, `caller_id`, `hitl_allowed`, `status`, retry/HITL/trace fields |
| `PreProcessNode` | `user_input` の canonical envelope（`mode`, `tenant_id`, `org_id`, `case_ref`, `payload_ref`, `rule_edition_ref`） |
| `SdsRevisionWorkflowGraphNode` | `mode`, `tenant_id`, `org_id`, `case_ref`, `payload_ref`, `rule_edition_ref`, `validated_input`, `status`。S-3 では inner result の `revision_brief`, `evidence_map`, `cited_source_ids`, `case_sources`, `requires_human_review`, `degradation_reason` も読む |
| `PostProcessNode` | `status`, `mode`, `revision_brief`, `evidence_map`, `ingest_summary`, `feedback_summary`, `degradation_reason` |
| inner routing | `user_input` の envelope と、Stage 1 後の `state.get("mode")` / `status` |
| `DeterministicCheckNode` | envelope 全項目、`session_id`, `correlation_id` |
| `KbRecallNode` / `ScopedRetrievalNode` | `tenant_id`, `org_id`, `rule_edition_ref`, `_stage1_record`（fallback は `validated_input`）、`retrieved_records`, `retrieval_rejected`, `degradation_reason`, `status` |
| `RevisionConsistencyLlmNode` | `payload_ref`, tenant/org/case、session/correlation、`rule_edition_ref`, `source_catalog`, `recall_refs`, `degradation_reason`, `status` |
| `ClaimSourceVerifyNode` | payload と scope/trace の各 ref、`llm_findings`, `case_sources`, `degradation_reason` |
| write-only inner nodes | ingest は `payload_ref` と row/rule/scope、feedback は payload と tenant/org/case/session/correlation を読む（Marketplace からは到達不能） |

Marketplace で invoke を成立させる最小の adapter 所有値は
`mode="invoke"`、`tenant_id`、`org_id`、`case_ref`、`rule_edition_ref`、`payload_ref` と、SDK が
`InvocationContext` から State に置く `session_id` / `correlation_id` / `caller_trust_level` / `hitl_allowed` である。
scope は `SCOPE_KEYS` と `STG_DEFAULT_SCOPE_ENVS` の宣言からデータ駆動で解決し、case/rule edition を含めて
未設定・空・空白文字入りを constructor で拒否する。`rule_edition_ref` を adapter の既定値で補完しない。

Azure LLM は credential を保持しない facade を compile 前に `config["llm"]` へ注入する。API key / endpoint /
deployment は `provision_secrets()` で起動時検証したうえ、各 LLM operation で `current_secrets()` から再解決し、
毎回新しい `AzureOpenAIClient` を作る。SDK 1.0.1 に無い `load_agent_config`、Marketplace bootstrap、EnvProvider、
Azure client は存在確認付きの互換経路を持つ。runner の契約どおり compile 後に secrets を provision し、context は
`VERIFIED_EXTERNAL` / `hitl_allowed=false` に固定する。万一 `AWAITING_HUMAN` が返れば成功として公開せず失敗にする。

この adapter は Stage 1〜4 の責務を変えない。SDS 節の存在・source registry・provenance 対応は決定論的に処理し、
LLM は閉じた schema の非結論的な所見文だけを生成する。分類確定、安全文言、release、最終法令適用は引き続き
S-3 で禁止し、規則版はデプロイ指定値を判定前提として使用する。

### Node 別 `required_trust_level`（S-1）

`BaseNode` の既定は `ANONYMOUS`（`GraphNode` も同じ既定）— 2026-07-27 に実機確認。したがって**外側・内側のすべてのノードでクラス本体に明示宣言する**。暗黙の継承に委ねない。

| Node | 層 | required_trust_level | 根拠 |
|------|---|---------------------|------|
| `pre_process` | outer | `VERIFIED_EXTERNAL` | テナント解決と入力受領。匿名呼び出しを許さない |
| `main`（`SdsRevisionWorkflowGraphNode`） | outer | `VERIFIED_EXTERNAL` | `GraphNode` の既定は `ANONYMOUS` のため**明示宣言が必須** |
| `post_process` | outer | `VERIFIED_EXTERNAL` | 顧客データ由来の出力を組み立てる |
| `deterministic_check` | inner | `VERIFIED_EXTERNAL` | `payload_ref` から本文を解決する |
| `kb_recall` | inner | `VERIFIED_EXTERNAL` | 顧客専用 KB を検索する |
| `revision_consistency_llm` | inner | `VERIFIED_EXTERNAL` | LLM を呼び、コストを発生させる |
| `claim_source_verify` | inner | `VERIFIED_EXTERNAL` | 正本データを参照して検証する |
| `accumulate_ingest` | inner | `VERIFIED_EXTERNAL` | 顧客 DB / KB へ**書き込む** |
| `accumulate_feedback` | inner | `VERIFIED_EXTERNAL` | 顧客 DB へ書き込み監査イベントを発行する |

有効値は `ANONYMOUS` / `VERIFIED_EXTERNAL` / `INTERNAL` の 3 つのみ。宣言形は `required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL`。

**TC-08 の対象**: 上記 9 ノード全件。各ノードについて `node(state)`（`__call__` 経由）で不足 trust を与え、**`execute()` が呼ばれないこと**を証明する（`node.execute(state)` の直接呼び出しは TC-08 を満たさない）。

### State Definition

`src/schemas/state.py` に `class SdsRevisionState(AgentState, total=False)` として定義する flat TypedDict。

| Field | Type | Purpose | Required |
|-------|------|---------|----------|
| `mode` | `str` | `ingest` / `invoke` / `feedback` | ✅ |
| `tenant_id` | `str` | テナント境界キー。未解決は fail-closed | ✅ |
| `org_id` | `str` | 同一社内の組織境界キー。tenant だけでは同社他組織へ未公開情報が漏れる | ✅ |
| `case_ref` | `str` | SDS 改訂案件の参照 ID（生データではない） | ✅ |
| `validated_input` | `dict` | 検証済み入力の**非機微部分のみ**（識別子・列挙値・日付） | ✅ |
| `payload_ref` | `str` | 機微本文（改訂ドラフト全文・組成・供給者情報）への**サーバ発行**参照。checkpoint に生データを残さない | — |
| `rule_edition_ref` | `str` | 顧客が承認した規則版（GHS 各国版 / REACH / HazCom）の参照 | ✅(invoke) |
| `sections_present` | `dict` | Stage 1: GHS 16 節の欄の有無 | — |
| `format_check` | `dict` | Stage 1: 書式・改訂履歴欄の定型検証結果 | — |
| `recall_refs` | `list[dict]` | Stage 2: 想起した過去照合観点（source ID + provenance のみ） | — |
| `retrieved_records` | `list[dict]` | Stage 2: 検索結果の標準形（`evidence-gate` 部品の契約キー） | — |
| `case_sources` | `list[dict]` | 現案件の source 台帳（`source_id` / `section_no` / `span_locator` / `provenance`。S-3 検証の正本）。**inner に閉じ、outer state へは載せない。本文は保持せず `payload_ref` + locator のみ** | — |
| `source_catalog` | `list[dict]` | Stage 1 が構築した source 一覧（`source_id` / `section_no` / `source_kind` / `excerpt`）。Stage 3 の prompt 入力。**inner に閉じる** | — |
| `llm_findings` | `dict` | Stage 3: LLM の生出力（所見文 + `source_id` + 種別 enum）。Stage 4 が `finding_id` 採番と `evidence_map` 構築を行うまでの中間形。**inner に閉じ、公開しない** | — |
| `revision_brief` | `dict` | Stage 4 verifier が組み立てた公開ブリーフ（閉じた schema・`finding_id` は verifier 採番） | — |
| `evidence_map` | `list[dict]` | Stage 4: finding → source の対応表（閉じた schema・正本から注入） | — |
| `cited_source_ids` | `list[str]` | Stage 4: 引用された source ID の一覧（実在検証対象） | — |
| `requires_human_review` | `bool` | 製品安全責任者のレビューを要する旨。**`invoke` モードでのみ出力し、常に `true`**（`ingest` / `feedback` は蓄積処理であり人間レビュー対象の成果物を生まないため出力しない） | invoke のみ |
| `degradation_reason` | `list[str]` | 縮退時の理由。固定 enum（`kb_empty` / `kb_low_quality` / `llm_unavailable` / `llm_schema_violation` / `partial_sections_submitted`）の配列 | — |
| `ingest_summary` | `dict` | `written_ids` / `rejected[]` | — |
| `feedback_summary` | `dict` | 反映件数・監査イベント ID | — |

**State Constraints (mandatory):**
- Flat TypedDict のみ（primitives + JSON-serializable 型）
- State に JWT / API キー / credential を置かない（checkpoint DB への平文漏えい）
- `InvocationContext` は `config["configurable"]` 経由のみ（State に格納しない・直接構築しない。取得は `InvocationContext.from_state(state)`）
- Pydantic モデル / dataclass / 任意 Python オブジェクトを置かない（msgpack 非互換）
- **`tenant_id` / `org_id` は State 経由**。SDK 1.0.0 の `InvocationContext` は `correlation_id` / `session_id` / `thread_id` / `parent_trace_id` / `caller_id` / `caller_trust_level` / `secrets` / `hitl_allowed` のみを持ち、**`tenant_id` フィールドは存在しない**（2026-07-27 に released wheel を venv へ入れて実機確認）

## Framework Utilization

### Shared Components Used
- [x] InvocationContext（`correlation_id`, `session_id`, `caller_trust_level`, `secrets`）— `InvocationContext.from_state(state)` で取得
- [ ] ConnectionPolicy — 使用しない
- [x] SecurityViolationError — S-2/S-3 の拒否条件で送出
- [x] S-2: `_extra_security_gate_input()` — モード allowlist・入力形式 allowlist・機微本文の隔離確認
- [x] S-3: `_extra_security_gate_output()` — 出力 schema の閉じた allowlist 検証・claim→source 対応・禁止フィールド排除
- [x] S-4: `emit_trace_event()` — 各 `execute()` 内で最低 1 件のドメインイベント（`node_start` / `node_complete` / `node_error` は framework が自動発行するため**重複させない**）
- [x] **Substrate reuse**: PII / credential 検出は `framework.security` の `detect_pii` / `detect_credentials`（`FunctionNode` が自動実行）に委ね、正規表現を自前実装しない。LLM 呼び出しは `shared.services.llm` の `BaseLLM` 実装を `config["llm"]` から注入する（具体クライアントをテンプレートに書かない）

> **S-2/S-3 gate behaviour by node type (ADR-017):**
> - `FunctionNode` サブクラス（`pre_process` / `post_process`）→ framework の `@final` ゲートが常に自動実行。拡張は `_extra_security_gate_input()` / `_extra_security_gate_output()` のみ
> - `GraphNode`（`main`）→ 既定は no-op。本テンプレートは **`_extra_security_gate_output()` に provenance 検証を実装する**（下記 S-3 節の理由参照）
> - `_extra_security_gate_output(result)` は **`state` を受け取らない**（2026-07-27 に released wheel で実機確認: `(self, result: dict) -> dict`）。モード判定は `merge_output()` が `result` に宣言した `gate_kind` に依存させる

### Composition Pattern

- **Pattern**: **GraphNode（in-process subgraph）** — `main` スロットに内側 `SdsRevisionWorkflowGraph` を包む
- **Composition target**: `src/graph/domain_workflow_graph.py` の `SdsRevisionWorkflowGraph`
- **Error propagation strategy**: **`propagate`**（既定）— 内側のエラーは `SubgraphError` として親へ伝播させる。`on_subgraph_error()` は実装しない
- **`propagate_hitl`**: `False`（既定・HITL 無効のため）
- **subgraph のキャッシュ**: `get_subgraph()` で `self._subgraph` にキャッシュする（同一 request 内で再生成しない）。outer graph 自体は認証後に request-scoped で構築し、`trusted_scope` / `session_id` を ingest node のコンストラクタへ渡す

## HITL 方針

`hitl.enabled: false`。`interrupt()` による対話的差戻しは実装しない（`docs/01_proposal.md` の HITL Gate 表と一致）。

ただしこれは「人間のレビューが不要」という意味ではない。安全設計の核は**出力契約による構造的排除**であり、`invoke` モードの出力は `requires_human_review: true` を必ず含む。PB-7 は **auto-waived — non-HITL** として扱う（`hitl.enabled: true` が無いためスタブは自動 skip）。`hitl.enabled: false` に伴い `memory_enabled: false`（checkpointer 不要）。

## Import Isolation Confirmation
- [x] Template は agenticstar-platform SDK（Level 0）を import しない
- [x] import 先は `framework/` と `shared/` のみ（`agents/base/` は不要）
- [x] 他テンプレート（sibling directory）を import しない
- [x] 部品の import identity は **`src.<part>` に統一**（top-level import は editable install の shim に依存し、非 editable 経路で `ModuleNotFoundError` になる）。root `conftest.py` は repo root のみを `sys.path` に載せる

## データ層設計（design-entry blocker (a) / (d)）

### DB schema（顧客専用接続内・原記録・monotonic append）

監査用の**原記録**であり、モデルには見せない。

| テーブル | 主なカラム | 用途 |
|---|---|---|
| `sds_revision_cases` | `case_ref`(PK), `tenant_id`, `org_id`, `product_ref`, `rule_edition_ref`, `revision_status`, `revision_reason_ref`, `created_at` | 改訂案件の台帳。`revision_status` が**承認前かどうかの正本**（下記「規則参照インターフェース」参照） |
| `revision_sops` | `sop_ref`(PK), `tenant_id`, `org_id`, `sop_edition`, `rule_edition_ref`, `section_scope`, `requirement_text_ref`, `quality_flag` | 社内改訂 SOP の構造化 |
| `review_perspectives` | `perspective_ref`(PK), `tenant_id`, `org_id`, `rule_edition_ref`, `source_ref`, `section_no`, `perspective_text`, `provenance`, `quality_flag` | 過去レビューから抽出した**照合観点**（結論ではない） |
| `case_sources` | `source_id`(PK), `tenant_id`, `org_id`, `case_ref`, `source_kind`, `span_locator`, `provenance`, `payload_ref` | **現案件**の供給者根拠・改訂ドラフト節の台帳。S-3 の claim→source 検証はここを正本に引く |
| `feedback_audit_events` | `event_id`(PK), `tenant_id`, `org_id`, `case_ref`, `actor_ref`, `action`, `approved`, `occurred_at` | S-4 監査証跡（append only） |

すべてのテーブルに `tenant_id` と `org_id` を持ち、**両方をクエリ条件に強制する**。スコープ引数を省略可能にしない（既定値での全件検索を作らない）。

> **`case_sources` の位置づけ**: 過去 KB から想起した `recall_refs` は**照合観点**であって現案件の根拠ではない。現案件の改訂主張を支持する source は `case_sources` に登録されたものだけである（下記 S-3 拒否条件 3 の「実在」はこの台帳を指す）。

### KB projection（retrieval-safe・model 可視）

KB に入れるのは、上記 `review_perspectives` から射影した**承認済み・source-linked・非結論的**な照合観点だけである。

KB document は 2 層に分ける。

| 層 | フィールド | model 可視 | 用途 |
|---|---|---|---|
| **index metadata（scope keys）** | `tenant_id`, `org_id`, `rule_edition_ref`, `case_ref` | **不可視** | 検索の絞り込みに**必須**。model には渡さない |
| **retrieval payload（許可フィールド）** | `perspective_ref`, `section_no`, `perspective_text`（照合観点の文）, `source_ref`, `provenance` | 可視 | LLM プロンプトに載る |

| 禁止フィールド（KB に置かない） |
|---|
| `classification_final`（分類確定結論）, `safety_phrase`（安全文言本文）, `release_decision`, 供給者名・組成比率などの機微原文, SDS 全文 |

**検索 API の契約**: `search(tenant_id, org_id, rule_edition_ref, query, top_k)` は **全 scope key を必須引数**とし、いずれかが欠落・空なら**呼び出しを拒否する**（既定値・`None` 許容・省略可能引数にしない）。テナント/組織の解決失敗は fail-closed（`status="error"`）。さらに backend は保存レコード由来の `tenant_id` / `org_id` / `rule_edition_ref` を各 hit の top-level または metadata に返す。欠落・部分欠落・要求 scope との不一致・top-level/metadata 間の競合は `scoped-retrieval` が hit 単位で拒否する。

**原文の無制限ベクトル化は禁止**。分類確定 / 安全文言 / release の結論ラベルはプロンプトにも検索軸にも与えない（過去結論の間接継承の遮断）。

### 顧客データ統治（blocker (d)）

| 項目 | 契約 |
|---|---|
| 最小 export | `revision_sops`（1 版）+ 過去レビューの照合観点。SDS 全文の一括投入は要求しない |
| アクセス権 | 顧客 IAM。テンプレート側は書込 credential（SDS 発行系）を**保持しない** |
| 保持・削除 | 顧客のデータ統治契約に従う。原記録の訂正・削除は顧客側で実施し、KB projection は再射影で追随 |
| 監査 | `feedback_audit_events` に append。S-4 `emit_trace_event()` と対応付ける |
| 永続層の PB テスト | PII / credential が索引化されないこと、テナント A → B が不可視であることを PB テストで実証する（State 非格納 PB だけでは守れない） |

## 規則参照インターフェース（design-entry blocker (b)）

- 入力は **「承認前の SDS 改訂ドラフト」に固定**する。既承認 SDS の配布・版管理のみの形式チェック用途を受け付けない（受け付けると意味的核が消え、GHS チェックリスト Tool に縮退する）
- 🔴 **`revision_status` はクライアントの自己申告を信用しない**。リクエストが `revision_status: "draft"` と名乗るだけで通るなら、既承認 SDS を「draft」と申告して素通りさせられ、入力固定が担保にならない。**サーバが `case_ref` から顧客台帳（`sds_revision_cases.revision_status`）の authoritative な状態を解決**し、許可 enum 以外を拒否する。
  - 許可 enum: `draft`（作成中）/ `under_internal_review`（社内レビュー中）/ `pending_approval`（承認上申中）
  - 拒否 enum: `approved`（承認済）/ `published`（発行済）/ `superseded`（失効）— これらは `SecurityViolationError` で拒否
  - リクエスト envelope に含まれる `revision_status` は**参照値として無視**する（台帳の値が唯一の正）
- 規則版は **顧客が承認した 1 版**（`rule_edition_ref`）を与える設計とし、AG に全地域規制を解釈させない。規則本文は config / KB 側に置き、コードにハードコードしない
- 照合ロジックは Stage 1（決定論: 16 節の欄・書式）と Stage 3（意味的当てはめ）に明確に分離する。**Stage 1 で意味的結論を出さない**

## S-3 出力安全契約（design-entry blocker (c)・testable schema）

### 実装場所

**`SdsRevisionWorkflowGraphNode._extra_security_gate_output(result)`**（`GraphNode` 境界）に実装する。理由は、照合の**正本**（現案件の source 台帳 = 機微データ本体）を `PostProcessNode` まで運ぶと outer state が checkpointer に永続化され、正本が checkpoint DB に平文で残るためである。`GraphNode` フックなら検証に正本を使い、**検証後に正本キーを落として返せる**。

### 🔴 検証用 envelope 契約（`_extra_security_gate_output` が `state` を取れない制約への対処）

実機確認のとおり `_extra_security_gate_output(result)` は **`result` しか受け取らない**。したがって親 state の `retrieved_records` / `tenant_id` / `org_id` / `case_ref` を**フックの中から読むことはできない**。検証に必要なものは、`merge_output()` が `result` に**明示的に載せて渡す**しかない。

`merge_output()` が返す dict（＝フックが受け取る `result`）は、公開キーと **verifier 専用の一時キー**を併せ持つ。

| キー | 種別 | 用途 | フック通過後 |
|---|---|---|---|
| `gate_kind` | 公開 | `invoke` / `ingest` / `feedback` の宣言。モード分岐の唯一の根拠 | 残す |
| `revision_brief` / `evidence_map` | 公開（invoke のみ） | 出力本体 | 残す（検証後） |
| `requires_human_review` / `degradation_reason` | 公開 | — | 残す |
| `ingest_summary` / `feedback_summary` | 公開（該当モードのみ） | 出力本体 | 残す（検証後） |
| `_verify_case_sources` | **一時（invoke のみ）** | 現案件の source 台帳（`source_id` / `section_no` / `span_locator` / `provenance` / `case_ref`） | **`pop` して破棄** |
| `_verify_scope` | **一時（invoke のみ）** | `tenant_id` / `org_id` / `case_ref`（越境検査用） | **`pop` して破棄** |

**🔴 モード別の分岐契約（一律 `raise` にしない）**: `ingest` / `feedback` は provenance 検証の対象がないため `_verify_*` を返さない。「一時キーの欠落は `raise`」を無条件に適用すると**両モードが必ず gate で失敗する**。よってフックはまず `gate_kind` を厳格に検証し、そのうえでモードごとに分岐する。

```
gate_kind が未宣言 / 未知値               → raise（fail-open に逃げない）
gate_kind == "invoke"
    _verify_case_sources / _verify_scope が無い → raise
    → 拒否条件 1〜8 を実行 → 検証後に _verify_* を pop
gate_kind == "ingest" / "feedback"
    _verify_* が存在する                        → raise（不要な正本の持ち出し）
    → 該当 summary の閉じた schema 検証 + 入れ子 credential 走査のみ実行
```

フックは検証に一時キーを使い、**戻り値からは `_verify_*` を `pop` して公開キーだけを返す**。これにより正本は outer state に到達せず checkpoint に残らない。

### 閉じた schema（allowlist）

### 出力設計 — LLM が所見を書き、引用 ID の実在検証で担保する（テンプレート共通の既定）

> ## 🔒 出力生成の正本契約（authoritative contract）
>
> **採用 = (b) bounded prose — LLM が所見の文章を執筆する。**
> 実装・受入テスト・S-3 ゲートは**すべてこの契約に従う**。
> (a) structured rendering（LLM に文章を書かせない案）は**不採用**であり、本書中の該当記述は
> `<details>「不採用案の記録」` ブロック内の**参考情報**である（実装対象ではない）。
> 本文中で両者を対比している箇所は、**不採用理由の説明**であって契約の二重定義ではない。
> residual risk の扱いは本節末尾および「非結論性の担保」節に明記する。

**採用方式（2026-07-27 確定）**: テンプレート共通の既定である**穏当設計**を採る — LLM が所見の文章を書き、
`evidence_map` + 引用 ID 実在検証で人間レビューを支援する。構造化描画（LLM に文章を書かせず enum と ID だけを
返させ、表示文を決定論生成する案）は**採らない**。

> **判断理由**: 構造化描画は分類確定・release の示唆を構造的に不可能にできる反面、**enum の粒度に論点が丸まり、
> enum に無い種類の問題を AG が見つけても伝える手段が無くなる**。SDS 改訂レビューは「担当者が気づきにくい
> 節間の食い違い」を言語化することに価値があり、表現力の低下は**使い勝手（＝この AG の価値そのもの）を損なう**。
> ユーザー裁定: 「A がいい。正直 B まですると使い勝手が落ちる」。
>
> したがって非結論性の担保は **①出力キーの閉じた allowlist ②禁止キーの再帰的排除 ③全所見に引用 ID 必須 +
> claim→source 対応検証 ④分類確定/release に相当する出力フィールドの不在**で行う（denylist は主防御にしない）。

自由文フィールドは以下に限定し、**それぞれ引用 ID を伴うことを必須**とする。

| フィールド | 型 | 担保 |
|---|---|---|
| `observation` / `description` / `question` | 自由文（LLM が執筆） | **`source_id` を非空で必須**。claim→source 対応を機械検証（拒否条件 3） |
| `observation_kind` / `gap_kind` | 固定 enum | 所見の種別は enum で構造化し、UI 側でグルーピング・絞り込みに使える |
| `support_status` | 固定 enum 3 値 | `supported` / `partially_supported` / `unsupported`。`compliant` / `approved` のような結論値を持たない |

`verification_questions[].question` も自由文とするが、**`question_kind`（固定 enum）を併記**させる。
`question_kind` の enum には **classification / release / safety-phrase を対象とする値を置かない** —
質問の「種別」を構造化することで、分類確定・release を問う質問が**集計・検出可能**になる。

> **注意（この設計の限界を正直に記す）**: 自由文である以上、LLM が `observation` に
> 「分類は確定してよい」と書く可能性を **構造的にゼロにはできない**。本設計はそれを
> ①出力スキーマに結論フィールドが無いこと ②全所見に引用 ID と対応検証が要ること
> ③`requires_human_review: true` が常に立つこと で緩和する。**これは製品安全上の緩和策であって、
> 法令遵守を確立するものではない**（最終判定は有資格者）。

<details>
<summary>不採用案の記録 — 構造化描画（LLM は ID と enum だけを返す）</summary>

以下は設計レビューで**不採用**となった案の記録。将来「1 文字の誤りも許されない」要件が出た場合の参照用。

#### （不採用案の詳細）LLM は「文字列」を書かない — ID と enum だけを返す

閉じた**キー** schema は「未宣言のキーを弾く」だけで、値の中身は素通りする。`observation` に
「本製品の分類は確定であり release 承認可」と書かれても全キー検査は通る。かといって
「自由文を疑問形に限る」も担保にならない — **「分類を確定して release してよいですか？」は疑問形のまま
禁止対象を示唆する**。

したがって本テンプレートは **LLM に文章を書かせない**。LLM の出力は **ID と固定 enum のみ**とし、
人間が読む文面は gate 通過後に **renderer が決定論的に組み立てる**。

| 要素 | LLM が返すもの | 表示文の生成 |
|---|---|---|
| 引用 | `quote_id`（Stage 1 が採番した canonical span の ID） | renderer が `quote_id` から原文を決定論的に解決して表示 |
| 所見の種別 | `observation_kind` / `gap_kind`（固定 enum） | renderer が enum → 定型文にマップ |
| 支持度 | `support_status`（固定 enum 3 値） | 同上 |
| 確認質問 | `question_kind`（固定 enum）+ `quote_id` | renderer が enum → 定型の疑問文テンプレートに展開 |

`question` を自由文にしない。`question_kind` の enum には **classification / release / safety-phrase を
対象とする値を置かない**（構造的に「分類を確定してよいか」を出力できない）。

#### 🔴 LLM 出力面と検証済み出力面を分ける（任意文字列を LLM に採番させない）

`finding_id` / `source_id` / `section_no` / `span_locator` / `provenance` を **LLM に書かせない**。
これらを LLM 出力に含めると「任意文字列を 2 箇所に同じ値で書けば整合する」抜け道が残り、
`span_locator` / `provenance` も**存在するだけ**で正本との一致が問われなくなる。

| 面 | 誰が作る | 含まれるもの |
|---|---|---|
| **`llm_findings`（Stage 3 の生出力）** | LLM | `quote_id`（`quote_catalog` 内の ID のみ）+ 固定 enum のみ。**それ以外のキーを持たない** |
| **`revision_brief` / `evidence_map`（公開出力）** | Stage 4 verifier | `finding_id` は verifier が**決定論的に採番**（例 `f001`…）。`source_id` / `section_no` / `span_locator` / `provenance` は **`quote_id` を鍵に `case_sources`（正本）から verifier が注入** |

`llm_findings` の schema（LLM に渡す response schema）:

```yaml
llm_findings:
  type: object
  additionalProperties: false
  required: [sections, revision_claims, gaps, inconsistencies, verification_questions]
  properties:
    sections:
      type: array
      items: {type: object, additionalProperties: false, required: [observation_kind, quote_id],
              properties: {observation_kind: {type: string, enum: [missing_evidence, section_mismatch, unsupported_claim, stale_reference, format_deviation]}, quote_id: {type: string}}}
    revision_claims:
      type: array
      items: {type: object, additionalProperties: false, required: [claim_quote_id, support_status],
              properties: {claim_quote_id: {type: string}, support_status: {type: string, enum: [supported, partially_supported, unsupported]}}}
    gaps:
      type: array
      items: {type: object, additionalProperties: false, required: [gap_kind, quote_id],
              properties: {gap_kind: {type: string, enum: [no_supporting_source, source_predates_revision, source_scope_mismatch, quantitative_value_unsupported, supplier_document_missing]}, quote_id: {type: string}}}
    inconsistencies:
      type: array
      items: {type: object, additionalProperties: false, required: [quote_id_a, quote_id_b],
              properties: {quote_id_a: {type: string}, quote_id_b: {type: string}}}
    verification_questions:
      type: array
      items: {type: object, additionalProperties: false, required: [question_kind, quote_id],
              properties: {question_kind: {type: string, enum: [confirm_source_currency, confirm_supplier_document_scope, confirm_quantitative_basis, confirm_section_alignment, confirm_sop_step_applied]}, quote_id: {type: string}}}
```

`section_no` すら LLM は書かない — `quote_id` が属する節は `case_sources` に登録済みだからである。
`degradation_reason` も自由文にせず **固定 enum**（`kb_empty` / `kb_low_quality` / `llm_unavailable` /
`llm_schema_violation` / `partial_sections_submitted`）とし、複数該当時は enum の配列で返す。

#### `quote_catalog` — LLM が意味のある選択をするための入力

ID だけを渡しても LLM は選べない。Stage 1 が採番した span を、**原文抜粋つきのカタログ**として prompt に渡す。
LLM は**このカタログ内の `quote_id` だけを選択可能**とする（catalog 外の ID は拒否対象）。

```yaml
quote_catalog:            # prompt へ渡す（出力ではない）
  - quote_id: "qa001"
    source_id: "src-draft-sec02"
    section_no: 2
    source_kind: "revision_draft"      # revision_draft | supplier_sds | analysis_certificate | internal_sop
    canonical_excerpt: "…（Stage 1 が正規化した原文抜粋）…"
```

これにより「LLM が原文を読んで判断する」情報量は落ちない。落ちるのは
**LLM が文字列を著述する自由度だけ**である（それが本設計の狙い）。

> **なぜ `quote_id` 方式か（逐語引用の直接生成をやめた理由）**: LLM に引用文そのものを返させて NFKC 部分文字列
> 照合する方式は、PDF の改行・ハイフネーション・OCR 揺れ・多言語表記で**偽拒否**を量産し、縮退経路が不安定になる。

（不採用案の記録はここまで）

</details>

### 閉じた schema（allowlist・実行可能な JSON Schema）— 採用版

自由文（`observation` / `description` / `question`）を持つが、**すべて `source_id` を非空で必須**とし、
`finding_id` で `evidence_map` と紐づける。`finding_id` は Stage 4 verifier が決定論採番し、
`span_locator` / `provenance` は `case_sources`（正本）から verifier が注入する（LLM に採番させない）。

```yaml
revision_brief:
  type: object
  additionalProperties: false
  required: [sections, revision_claims, gaps, inconsistencies, verification_questions]
  properties:
    sections:
      type: array
      items:
        type: object
        additionalProperties: false
        required: [finding_id, section_no, observation_kind, observation, source_id]
        properties:
          finding_id:       {type: string}
          section_no:       {type: integer, minimum: 1, maximum: 16}
          observation_kind: {type: string, enum: [missing_evidence, section_mismatch, unsupported_claim, stale_reference, format_deviation]}
          observation:      {type: string, minLength: 1, maxLength: 1000}   # LLM が執筆
          source_id:        {type: string, minLength: 1}
    revision_claims:
      type: array
      items:
        type: object
        additionalProperties: false
        required: [finding_id, section_no, claim_text, support_status, source_id]
        properties:
          finding_id:     {type: string}
          section_no:     {type: integer, minimum: 1, maximum: 16}
          claim_text:     {type: string, minLength: 1, maxLength: 1000}     # LLM が執筆
          support_status: {type: string, enum: [supported, partially_supported, unsupported]}
          source_id:      {type: string, minLength: 1}
    gaps:
      type: array
      items:
        type: object
        additionalProperties: false
        required: [finding_id, section_no, gap_kind, description, source_id]
        properties:
          finding_id:  {type: string}
          section_no:  {type: integer, minimum: 1, maximum: 16}
          gap_kind:    {type: string, enum: [no_supporting_source, source_predates_revision, source_scope_mismatch, quantitative_value_unsupported, supplier_document_missing]}
          description: {type: string, minLength: 1, maxLength: 1000}        # LLM が執筆
          source_id:   {type: string, minLength: 1}
    inconsistencies:
      type: array
      items:
        type: object
        additionalProperties: false
        required: [finding_id, section_no_a, section_no_b, description, source_id_a, source_id_b]
        properties:
          finding_id:   {type: string}
          section_no_a: {type: integer, minimum: 1, maximum: 16}
          section_no_b: {type: integer, minimum: 1, maximum: 16}
          description:  {type: string, minLength: 1, maxLength: 1000}       # LLM が執筆
          source_id_a:  {type: string, minLength: 1}
          source_id_b:  {type: string, minLength: 1}
    verification_questions:
      type: array
      items:
        type: object
        additionalProperties: false
        required: [finding_id, section_no, question_kind, question, source_id]
        properties:
          finding_id:    {type: string}
          section_no:    {type: integer, minimum: 1, maximum: 16}
          # classification / release / safety_phrase を対象とする値は意図的に持たない
          question_kind: {type: string, enum: [confirm_source_currency, confirm_supplier_document_scope, confirm_quantitative_basis, confirm_section_alignment, confirm_sop_step_applied]}
          question:      {type: string, minLength: 1, maxLength: 500}       # LLM が執筆
          source_id:     {type: string, minLength: 1}

evidence_map:
  type: array
  items:
    type: object
    additionalProperties: false
    required: [finding_id, source_id, section_no, span_locator, provenance, supports]
    properties:
      finding_id:   {type: string}      # revision_brief の各 finding と 1:N で対応
      source_id:    {type: string}
      section_no:   {type: integer, minimum: 1, maximum: 16}
      span_locator: {type: string}      # verifier が case_sources から注入
      provenance:   {type: string}      # verifier が case_sources から注入
      supports:     {type: string, enum: [section, claim, gap, inconsistency, question]}
```

すべての brief item は `finding_id` を持ち、`evidence_map` は `finding_id` で紐づく。これにより
「どの論点がどの source に裏付けられるか」が順序依存なしに追跡できる。

### `ingest` / `feedback` モードの出力 schema

```yaml
ingest_summary:
  type: object
  additionalProperties: false
  required: [written_ids, rejected]
  properties:
    written_ids: {type: array, items: {type: string}}
    rejected:
      type: array
      items:
        type: object
        additionalProperties: false
        required: [row_ref, reason_kind]
        properties:
          row_ref:     {type: string}
          reason_kind: {type: string, enum: [quality_gate_failed, missing_required_field, unsupported_format, scope_mismatch, duplicate]}

feedback_summary:
  type: object
  additionalProperties: false
  required: [applied_count, audit_event_ids]
  properties:
    applied_count:   {type: integer, minimum: 0}
    audit_event_ids: {type: array, items: {type: string}}
```

両モードとも**自由文フィールドを持たない**（理由は `reason_kind` の enum で表す）。

### 全モード共通の envelope フィールド

```yaml
degradation_reason:       # 全モードで返しうる。自由文にしない
  type: array
  items:
    type: string
    enum: [kb_empty, kb_low_quality, llm_unavailable, llm_schema_violation, partial_sections_submitted]

requires_human_review:    # invoke モードのみ・常に true
  type: boolean
  const: true

gate_kind:
  type: string
  enum: [invoke, ingest, feedback]
```

### 非結論性の担保は「構造」で行う（denylist に依存しない）

**採用契約は (b) bounded prose**（下記「出力設計」節・「閉じた schema — 採用版」・Design Decision Record と同一）。
LLM は所見の文章を執筆する。その上で、分類確定・安全文言・release を出させない担保は、禁止語 denylist ではなく
**①出力キーの閉じた allowlist ②禁止キーの再帰的排除 ③全所見に `source_id` 非空必須 + claim→source 対応検証
④分類確定 / release に相当する出力フィールドの不在**という構造で行う。本テンプレートは結論文言 denylist を
主防御にしない（denylist を主防御にすると語彙の網羅性競争になり、かつ硬化させると正当な所見まで潰す）。

> **residual risk の明示（bounded prose を選んだ以上、ここは正直に書く）**: 自由文フィールドが存在する以上、
> LLM が `observation` 等に「分類は確定してよい」と書く可能性を **構造的にゼロにはできない**。本設計はこれを
> 上記 ①〜④ と `requires_human_review: true` の常時付与で**緩和**する。**これは製品安全上の緩和策であって、
> 法令遵守を確立するものではない**（分類確定・安全文言・release・法令適用の最終判定は有資格者に残る）。
>
> **なぜ構造化描画（LLM に文章を書かせない案）を採らなかったか**: 残存リスクをゼロにできる反面、所見が enum の
> 粒度に丸まり、**enum に無い種類の問題を AG が見つけても伝える手段が無くなる**。SDS 改訂レビューの価値は
> 「担当者が気づきにくい節間の食い違いを言語化すること」にあり、表現力の低下はこの AG の価値そのものを損なう。
> ユーザー裁定（2026-07-27）: 「A がいい。正直 B まですると使い勝手が落ちる」。不採用案の詳細は上記
> `<details>` ブロックに記録として残置してある。

### `_extra_security_gate_output()` の拒否条件

1. **閉じた allowlist 違反** — `revision_brief` / `evidence_map` に宣言外のキーがある、または各要素に未宣言のキーがある → `SecurityViolationError`
2. **禁止キーの再帰的排除** — 禁止キーが**入れ子のどの階層にも**現れないこと。照合は 2 段構え。
   - **(a) 正規化照合**: NFKC 正規化 + casefold + 記号除去（`-` / `_` / 空白）後に、正準名 `classificationfinal` / `safephraseauthoring` / `safetyphrase` / `release` / `releasedecision` と一致するか
   - **(b) alias map 照合**: NFKC / casefold は**英日翻訳をしない**ため、日本語別名は正規化では捕まらない。下記を**定数表として全件列挙**し、実装・テストの双方がこの定数を読む（`等` で濁さない）。

   ```python
   FORBIDDEN_KEY_ALIASES = {          # 正準名 -> 別名（全件・追加は MR で明示）
       "classification_final": ("分類確定", "分類決定", "分類判定", "GHS分類確定", "最終分類"),
       "safety_phrase":        ("安全文言", "注意喚起語", "危険有害性情報", "注意書き"),
       "release":              ("出荷可否", "リリース判定", "発行可否", "公開可否", "承認可否"),
   }
   ```
3. **finding→evidence の完全一致契約** — 各 finding の `finding_id` に対し `evidence_map` に行が存在するだけでは不十分（ID がすべて実在しても**誤対応**が通ってしまう）。以下を**集合一致**で検証する。
   - **(i) source 集合の一致**: finding が持つ `source_id`（`inconsistencies` は `source_id_a` / `source_id_b`）の集合 = 同一 `finding_id` を持つ `evidence_map` 行の `source_id` 集合（**過不足なく一致**）。finding が `srcA` を引いているのに evidence 行が `srcB` を持つ、といったズレを拒否する
   - **(ii) 正本との一致**: 各 `evidence_map` 行の `source_id` / `section_no` / `span_locator` / `provenance` が、`_verify_case_sources` が持つ値と**完全一致**すること（存在確認では足りない）
   - **(iii) scope 一致**: `_verify_scope` の `tenant_id` / `org_id` / `case_ref` と一致すること
   - **(iv) `inconsistencies` の A/B 分離**: `source_id_a` と `source_id_b` は **別々の `evidence_map` 行**として検証する（1 行にまとめない）
   - **(v) 自由文フィールドの根拠必須**: `observation` / `claim_text` / `description` / `question` を持つ要素は `source_id` を**非空**で持つこと。空文字・空白のみは拒否
   - 🔴 **過去 KB 由来の `recall_refs` / `perspective_ref` は現案件 finding の根拠に使えない**。照合観点であって現案件の証拠ではないため、`source_id` として現れたら拒否する
4. **`source_id` の実在検証** — brief 中の全 `source_id` / `source_id_a` / `source_id_b` が `_verify_case_sources` に登録済みの ID として実在すること。捏造 ID は拒否
   - あわせて **Stage 3 の生出力段階**で、LLM が返した `source_id` が Stage 1 の source 一覧に無ければその finding を drop し `degradation_reason: llm_schema_violation` を立てる（gate に到達させる前に落とす）
5. **enum の閉包** — `observation_kind` / `gap_kind` / `support_status` / `question_kind` / `evidence_map[].supports` / `ingest_summary.rejected[].reason_kind` が宣言 enum の値以外なら拒否
6. **`section_no` の範囲** — `1`〜`16`（GHS 16 節）の整数であること。範囲外・非整数は拒否
7. **ネストした全 string leaf の credential 検査** — framework 既定の S-3 は **`result` の top-level string しか** `detect_credentials` に掛けない（2026-07-27 に `inspect.getsource(FunctionNode._security_gate_output)` で実機確認）。`GraphNode` は既定 no-op。よって **`revision_brief` / `evidence_map` の入れ子 string leaf を再帰的に走査**し、`framework.security.detect_credentials` を呼ぶ。**自前の正規表現は書かない**（substrate reuse — framework が提供する検出器を再実装しない）
8. **`gate_kind` の宣言必須** — `merge_output()` が `result["gate_kind"]` に `invoke` / `ingest` / `feedback` を宣言する。欠落・未知値は `raise`（キー有無の分岐で fail-open に逃げない）

### 実装時の gate-bypass 先回り

| # | 塞ぐ穴 | 対処 |
|---|---|---|
| 1 | allowlist の免除条件に `.*` を書く | 免除は「構造的にラベル **かつ** 述語を持たない」場合のみ。長さで切らない |
| 2 | `payload_ref` をクライアントから受け取る | `payload_ref` は**サーバ発行の内部トークン**。クライアント由来は無条件破棄し、常にスクラブする（冪等）。早期 return で `raw_input` をそのまま返さない |
| 3 | checkpoint スクラバを denylist で書く | スクラバは **allowlist**（`safe_keys` = ID・列挙値・日付のみ）で書く |
| 4 | opaque reference に `uuid4()` を使う | `payload_ref` は**英字のみ**で発行する。数字を含む ref は S-2 の PII ゲートが `[MASKED]` に破壊書換し、本番で低頻度の 422 を引き起こす（Outsourcing SLA Deviation RCA Rebuttal-Brief Agent で真因特定・約 0.58%/envelope） |

### 受入テストの列挙（blocker (c) — 設計時点で ID と期待結果を固定する）

「実装フェーズで書く」では blocker (c)（schema / API / **テスト**へ落とす）を満たさないため、ここで列挙する。実装フェーズは下表（**40 ケース**）をそのまま `docs/03_test_spec.md` に展開する。reject 側は **本番 `config/config.yaml` + `config/agent.yaml` を読ませた状態**で実行する（本番 config を再現しないテストは fail-closed バグを素通りさせる）。

| Test ID | 入力 | 期待結果 |
|---|---|---|
| AT-01a | 全キーが宣言どおりの `revision_brief` | accept |
| AT-01b | `revision_brief` に未宣言キー `overall_verdict` を追加 | reject（拒否条件 1） |
| AT-01c | `evidence_map[0]` に未宣言キーを追加 | reject（拒否条件 1） |
| AT-02a | 禁止キーを含まない出力 | accept |
| AT-02b | `sections[0]` の入れ子に `classification_final` を追加 | reject（拒否条件 2） |
| AT-02c | 別名 `Classification-Final`（NFKC + casefold で一致） | reject（拒否条件 2） |
| AT-02d | 日本語別名 `分類確定`（**alias map による検出**。NFKC/casefold では一致しないことの回帰） | reject（拒否条件 2） |
| AT-03a | 全 `finding_id` に `evidence_map` 行が存在し quote 集合も一致 | accept |
| AT-03b | `finding_id` に対応する `evidence_map` 行が無い | reject（拒否条件 3-i） |
| AT-03g | finding が `srcA` を引き、同一 `finding_id` の evidence 行は `srcB`（**両方実在**） | reject（拒否条件 3-i・誤対応。ID 実在だけでは通さない回帰） |
| AT-03h | `evidence_map` 行の `span_locator` が正本 `case_sources` の値と**異なる**（実在はする） | reject（拒否条件 3-ii・完全一致でないと通さない回帰） |
| AT-03i | `inconsistencies` の `source_id_a` / `source_id_b` が単一 evidence 行にまとめられている | reject（拒否条件 3-iv・A/B は別行） |
| AT-03j | `observation` が非空だが `source_id` が空文字 | reject（拒否条件 3-v・自由文は根拠必須） |
| AT-03c | 実在しない `source_id`（捏造 ID） | reject（拒否条件 3） |
| AT-03d | 他テナント / 他 `case_ref` の `source_id` | reject（拒否条件 3・越境） |
| AT-03e | **過去 KB の `perspective_ref` を `source_id` に使用** | reject（拒否条件 3・照合観点は根拠にならない） |
| AT-03f | `source_id` は実在するが `provenance` / `span_locator` 欠落 | reject（拒否条件 3） |
| AT-04a | 全 `source_id` が `_verify_case_sources` に登録済み | accept |
| AT-04b | 捏造 `source_id` | reject（拒否条件 4） |
| AT-05a | `observation_kind` が宣言 enum 内 | accept |
| AT-05b | `support_status: "compliant"`（結論値） | reject（拒否条件 5） |
| AT-05c | `question_kind` に未宣言値（release 関連を指定しようとする） | reject（拒否条件 5） |
| AT-06a | `section_no: 8` | accept |
| AT-06b | `section_no: 17` / 非整数 | reject（拒否条件 6） |
| AT-07a | credential を含まない出力 | accept |
| AT-07b | `evidence_map[0].provenance` の**入れ子**に API キー様文字列 | reject（拒否条件 7・framework 既定では検出されない経路） |
| AT-08a | `gate_kind: "invoke"` + `_verify_*` あり | accept |
| AT-08b | `gate_kind` 欠落 / 未知値 | reject（拒否条件 8） |
| AT-08c | `gate_kind: "ingest"` + `_verify_*` **なし** | accept（モード別分岐の回帰。一律 raise でないこと） |
| AT-08d | `gate_kind: "ingest"` なのに `_verify_case_sources` を同梱 | reject（不要な正本の持ち出し） |
| AT-09 | クライアントが `payload_ref` を送りつける | サーバ発行 ref に置換され、送られた ref は破棄される |
| AT-10 | `case_ref` の台帳 `revision_status` が `approved` | reject（`SecurityViolationError`・入力固定） |
| AT-11 | KB 空・LLM 不達 | accept（`status="success"` + `degradation_reason` 記録・縮退経路） |
| AT-12 | フック通過後の `result` | `_verify_*` キーが存在しないこと |
| AT-13 | 同一 invocation 内で原文を 3 回 `resolve()`（Stage1 / Stage3 / S-3 provenance 検証） | accept（read lease・single-use でないことの回帰） |
| AT-14 | lease 失効後 / scope 不一致の `resolve()` | reject（replay 防止） |
| AT-15a | Stage 3 出力が `source_catalog` 内の `source_id` のみを引く | accept |
| AT-15b | Stage 3 出力に `finding_id` / `span_locator` / `provenance` を含めて返す | schema 違反で拒否（これらは verifier が採番・注入する回帰） |
| AT-15c | Stage 3 出力が catalog 外の `source_id` を返す | 当該 finding を drop + `degradation_reason: llm_schema_violation` |
| AT-16 | `degradation_reason` に enum 外の自由文 | reject（enum 閉包） |

## 適用部品指定表（共通部品ライブラリからの写経）

実装フェーズは下記部品を写経ツールで取り込み、`adaptation_points` を本テンプレート仕様で埋める。**部品がカバーする骨格を再発明しない**。

| 部品 ID | version | 役割 | 本テンプレでの adaptation |
|---|---|---|---|
| `cat2-graph-skeleton` | 0.1.4 | Cat2 外側 `GraphNode` 骨格（mode 分岐 / merge_output / lazy+cache subgraph） | `mode_specs` = `invoke`/`ingest`/`feedback` の `gate_kind` とフィールド map。`_parent_config` に `llm` / `store` / `retrieval` / `top_k` / `rule_edition_ref`。`propagate_hitl=False`。`extract_input` は canonical envelope の直列化 |
| `output-envelope` | 0.1.2 | `post_process` の `formatted_output` エンベロープ | `format_invoke` = `revision_brief` + `evidence_map` + `requires_human_review`。`format_ingest` = `ingest_summary`。縮退は `degradation_reason` に畳み込む |
| `evidence-gate` | 0.3.0 | S-3 provenance 検証（`GraphNode` 境界） | `ProvenanceContract.gate_kind="invoke"` / `cited_ids_key="cited_source_ids"` / `evidence_map_key="evidence_map"` / `record_id_keys` は**現案件台帳 `_verify_case_sources`** を指す（過去 KB の `retrieved_records` ではない）。逐語引用検証（拒否条件 4）は StrictCoveragePolicy 相当で追加 |
| `scoped-retrieval` | 0.3.0 | Stage 2 のスコープ付き検索 | `tenant_id` + `org_id` + `rule_edition_ref` を fail-closed 必須にし、hit に保存レコード由来の全 scope があることも検証。safe projection = KB 許可フィールドのみ。`top_k` は config。KB 空でも縮退して継続 |
| `llm-draft-stage` | 0.3.0 | Stage 3 の LLM 構造化出力生成 | `config["llm"]` 注入シーム（具体クライアントを書かない）。未配線だけ deterministic fallback。provider/auth/config/timeout/response 変換障害は安定 `error_code` で fail-closed とし、例外文を State/trace に載せない |
| `ingest-row-pipeline` | 0.4.0 | `ingest` の行データパイプライン + checkpoint スクラバ | 実 `PayloadStore.resolve()` を直結し、認証後の `trusted_scope` / `session_id` を request-scoped node のコンストラクタへ注入。allowlist 外 scalar は型を問わず退避 |
| `payload-store` | 0.4.0 | 機微本文の短命・scope bound 隔離 | `tenant`/`org`/`session` binding + TTL。**ref は英字のみ**で発行（S-2 PII ゲート衝突の回避）。🔴 **single-use ではなく scope-bound read lease**（下記） |
| `entry-adapter` | 0.4.0 | FastAPI entry の AUTH + SCOPE adapter | `SCOPE_KEYS=("tenant_id", "org_id")`。payload `mode` を `Operation` へ明示 mapping し、standalone/STG token は `READ_INVOKE` のみに制限。**クライアントヘッダ由来の scope は受け付けない** |

> 部品同梱テストは `tests/parts/{part-id}/` に置き、**削除・弱体化しない**。結合テストは `parts/TESTING.md` から `docs/03_test_spec.md` へ取り込む。写経忠実性は部品検証ツールで機械検証する。適用時の実 version は実装着手時点の部品レジストリを正とする。

## 想定ユースケースとユーザビリティ設計レビュー

**想定シーン**: 原料サプライヤーの組成変更通知を受け、製品安全担当が製品 X の SDS 改訂ドラフト（16 節）を作成した。改訂理由と供給者根拠（分析証明書・サプライヤー SDS）を添えて、承認上申の前に自己レビューしたい。

設計を書く過程で、ユーザビリティ観点から以下を修正した。

| # | 当初案 | 問題 | 修正後 |
|---|---|---|---|
| 1 | 入力に GHS 16 節すべての構造化 JSON を要求 | 担当者は改訂した節だけを手元に持つ。全節の構造化は過剰要求で、入力コストが導入障壁になる | **改訂した節のみ**の提出を許し、未提出節は Stage 1 で「未確認」として扱う（欠落と断定しない）。全節提出は任意 |
| 2 | 出力を単一の散文ブリーフ | 受け取った担当者が「どの節のどの主張を直せばよいか」を読み解く必要がある | 出力を `sections` / `revision_claims` / `gaps` / `inconsistencies` / `verification_questions` に**分離**し、節番号で引けるようにした。そのまま修正作業のチェックリストになる。各所見に種別 enum が付くため、UI 側でグルーピング・絞り込みもできる |
| 3 | KB が空だと `invoke` を拒否 | 導入初期は過去レビュー観点がゼロ。初日から使えないと定着しない | KB が薄い / 空でも **Stage 1 + 当該改訂の照合だけでドラフトを生成**し、`degradation_reason` に「過去観点なし」を記録して継続する（`status` は `success`） |
| 4 | LLM 不達時はエラー返却 | 担当者の作業が完全に止まる | Stage 1 の決定論結果 + Stage 2 の照合観点だけを返す縮退経路を持ち、`degradation_reason` で明示する |

**エラー / フォールバック方針**: テナント / 組織の解決失敗のみ fail-closed（`status="error"`）。それ以外（KB 空・LLM 不達・過去観点の品質不足）は縮退して `success` + `degradation_reason` を返す。

## Design Decision Record

| Decision | Option A | Option B | Chosen | Rationale |
|----------|----------|----------|--------|-----------|
| L1 base type | `AgentBaseGraph` | `AutonomousBaseGraph` | **A** | モードごとに固定パイプライン。エージェント自身が次の行動を決める必要がない |
| Composition pattern | Standalone `FunctionNode` | `GraphNode`（内側 subgraph） | **B** | 3 モード × 4 ステージを backbone の 3 スロットに畳むには内側グラフが要る。S-3 の正本を checkpoint に載せない利点もある |
| S-3 ゲートの置き場所 | `PostProcessNode` | `GraphNode` 境界 | **B** | 照合の正本（機微データ本体）を outer state に載せずに検証でき、検証後に落として返せる |
| 非結論性の担保 | 禁止語 denylist | 閉じた schema（allowlist） | **B** | denylist は語彙の網羅性競争になり、硬化させると正当な所見を潰す。構造で担保する |
| **出力生成方式** | LLM が所見を執筆し evidence_map + 引用 ID 検証で支援（**テンプレート共通の既定**） | LLM は ID + enum のみ返し renderer が決定論生成（構造化描画） | **A**（2026-07-27 裁定） | 構造化描画は enum の粒度に論点が丸まり、enum に無い問題を伝える手段が無くなる＝**使い勝手（この AG の価値そのもの）が落ちる**。ユーザー裁定「A がいい。正直 B まですると使い勝手が落ちる」 |
| `finding_id` / `span_locator` / `provenance` の採番 | LLM が返す | verifier が決定論採番・正本から注入 | **B** | 任意文字列を LLM に採番させると「同じ値を 2 箇所に書けば整合する」抜け道が残り、`span_locator` / `provenance` が存在確認だけで素通りする |
| テナントキーの経路 | `InvocationContext.tenant_id` | State（`tenant_id`/`org_id`） | **B** | SDK 1.0.0 の `InvocationContext` に `tenant_id` は**存在しない**（実機確認済み） |
| HITL | `interrupt()` 対話ループ | 出力契約による構造的排除 | **B** | MFG は HITL Gate 対象業種外。`requires_human_review` + 結論フィールド排除で目的を達成する |
| 過去結論の扱い | 過去の分類確定結論も KB に置く | 照合観点のみ射影 | **B** | 結論を検索軸に置くと間接継承が起き、AG が過去判断を追認する |

### 部品リフレッシュ retrofit の判断（2026-08-12）

共通部品 6 点の更新適用（パイロット1件目）に伴う判断。

| Decision | Option A | Option B | Chosen | Rationale |
|----------|----------|----------|--------|-----------|
| LLM への応答契約の伝え方 | カテゴリ名のみ伝える（従来実装） | 検証側の定数（`_raw_item_keys` / `CATEGORY_ENUMS` / `PROSE_LIMITS` / `MIN_SECTION`・`MAX_SECTION`）から `_response_contract()` を生成して宣言 | **B** | 従来は実プロバイダが自分の語彙で答え、**全所見が schema violation で破棄され `output: null`** になった（2026-08-12 に実 SDS 改訂ドラフト + `supplier_sources` で実測）。検証側の定数から生成することで、プロンプトが受理条件からドリフトできない |
| 応答契約テストの立て方 | 宣言と定数の一致を assert | 宣言から機械的に組み立てた応答を検証器へ通す + 実装から独立な不変条件（section 境界・指示文・カテゴリ一覧・禁止フィールド）を検査 | **B** | 一致 assert は同じ定数を参照するため常に真＝無意味。ただし振る舞い検査だけでも、宣言側の数値・指示文・カテゴリ一覧を削ると全緑で通った（レビュー指摘を変異 4 種で再現）。両方が要る。変異 11 種で赤を確認 |
| schema violation 時の observability | この MR で `error_code` / `degradation_reason` を返すよう直す | 別 Issue にしてリフレッシュ・バッチで横断対処 | **B**（2026-08-12 判断） | 同型の穴は他 AG にもある可能性が高く、個別対処では AG ごとに形がバラつく。横断で型を揃える |
| 実 LLM 検証の実行条件 | repo 内でモデルを起動 | repo 外の一時ディレクトリで起動 | **B** | repo 内だと検証用のモデルが `src/` を読んで期待スキーマを当ててしまい、本欠陥が**偽陽性で隠れた**（実際に隠れていた）。本番の LLM はソースを読めないので repo 外が正しい条件 |
| STG 再デプロイ / リリース系列への反映 | この改版に続けて実施 | 統合ブランチまでで止める | **B** | リリース済みテンプレートのため、STG 再デプロイとリリース基準線への反映は別途の承認事項として扱う |

**運用判断（同日）**: ①この retrofit MR は外部レビューに回す（released 済み AG の runtime 変更であり、
変更点が LLM 挙動の中核であるため。パイロット1件目は以後 31 本の型になる）。②同型欠陥の洗い出しは
機械調査ではなくリフレッシュ・バッチの標準手順（「業務の中核が走る入力で実 LLM 1 回転」）で消化する
— プロンプト構築の形が AG ごとに異なり、静的な突き合わせでは当たらないため。

## 近接テンプレートとの差異（design-entry blocker (e)）

| 観点 | Manufacturing Supplier Corrective-Action Evidence Brief Agent（供給者 CAPA） | Logistics Dangerous-Goods Document Consistency Agent（DG 申告） | 本 MFG-C2-123（SDS 改訂） |
|---|---|---|---|
| 参照規則体系 | SCAR / 品質協定 | IATA DGR / IMDG / ADR | **GHS / HazCom / REACH Annex II** |
| 入力ソース | 供給者の是正措置報告 × 品質協定 | 既申告 DG 情報 × SDS/包装証明 | **承認前 SDS 改訂ドラフト × 供給者根拠** |
| 判断の核 | 是正措置の因果連鎖の十分性 | 輸送書類間の申告整合 | **改訂主張と各節の意味的整合・節間矛盾** |
| KB projection | CAPA 評価観点 | DG 差戻し照合観点 | **SDS 改訂レビュー観点（節単位）** |
| 出力物 | CAPA 証拠ブリーフ | DG 整合レビューブリーフ | **SDS 整合ブリーフ（分類確定 / 安全文言 / release を持たない）** |

規則体系・入力・KB スキーマ・出力が異なり、config 複製では吸収されない固有 schema である。なお Logistics Dangerous-Goods Document Consistency Agent の設計書にも本テンプレートとの比較表があり、双方向で境界が明文化されている。

## Design-Entry Blocker（承認記録 §12）の解決状況

| # | blocker | 解決 |
|---|---|---|
| (a) | DB / KB schema と責務分離 | 「データ層設計」節 — 原記録 5 テーブル（`case_sources` 含む）+ KB の 2 層（不可視 scope keys / 可視 payload）+ 検索 API の全 scope key 必須契約 |
| (b) | 入力の承認前ドラフト固定 IF・照合ロジック | 「規則参照インターフェース」節 — **`revision_status` をサーバが台帳から解決**（自己申告を信用しない）・許可/拒否 enum・規則版 1 版固定・Stage 1/3 の分離 |
| (c) | S-3 安全契約の schema / API / テスト化 | 「S-3 出力安全契約」節 — 検証用 envelope 契約 + 閉じた schema（`evidence_map` 含む）+ 自由文への引用 ID 必須 + 8 拒否条件 + **受入テスト 40 ケースを ID 付きで列挙** |
| (d) | 顧客データ統治契約 | 「顧客データ統治」表 + **入口 adapter における機微本文の隔離契約** + 永続層 PB テスト（PB-5 で実 checkpoint を検査） |
| (e) | 近接テンプレートとの差異明文化 | 「近接テンプレートとの差異」比較表 |

> (f)（PoC の記録）は提案承認前の受入条件であり、Design 入場条件ではない。承認は 2026-07-23 に取得済み。

## config 設計値（manifest / runtime の分離）

**`agent.yaml` は静的 manifest**（identity・entry point・requires）であり、**runtime で調整する値は `config/config.yaml`** に置く。runtime 値を manifest に混ぜると manifest ツールの契約を壊す。

```yaml
# config/agent.yaml — 静的 manifest
agent:
  id: "MFG-C2-123"
  name: "SDSRevisionEvidenceAgent"
  category: "Cat 2"
  industry: "MFG"
  base_type: "AgentBaseGraph"          # 継承クラス名をそのまま書く
  module: "src.graph.graph"
  class: "Graph"                        # 実装フェーズで SdsRevisionEvidenceGraph へ改名し
                                        # src/api/server.py の import と同一コミットで揃える
  required_trust_level: "VERIFIED_EXTERNAL"
  requires:
    extras: []
    secrets: []                         # LLM / DB の credential は実装フェーズで確定し宣言
```

```yaml
# config/config.yaml — runtime パラメータ
max_retry: 3
timeout_s: 30
top_k: 8                                # 多節・長文のため既定より大きめ
temperature: 0.0                        # 製品安全文書のため決定論寄り
memory_enabled: false
hitl:
  enabled: false
```

> - **entry point の `class` は設計フェーズでは scaffold の `Graph` のまま**にする。`src/api/server.py` が `from src.graph.graph import Graph` で読むため、実装前に将来のクラス名を宣言すると AgentRegistry の lazy import が壊れる。改名は実装フェーズで importer とセットで行う。
> - `requires.secrets` は実装フェーズで LLM クライアント・DB 接続の具体が決まった時点で実値化する。secrets は必ず `ctx.secrets.require()` で取得し、`os.environ` は使わない（唯一の例外は `src/api/server.py` の入口認証トークン `INVOKE_AUTH_TOKEN`）。

## SDK 実機確認記録

以下は **2026-07-27** に `agenticstar-agentcore==1.0.0`（released wheel）を venv へ導入し、`inspect` で実体確認した結果である（推測ではない）。

| 確認項目 | 結果 | 設計への反映 |
|---|---|---|
| `InvocationContext` の実フィールド | `correlation_id` / `session_id` / `thread_id` / `parent_trace_id` / `caller_id` / `caller_trust_level` / `secrets` / `hitl_allowed`。**`tenant_id` なし** | テナントキーは State 経由（`tenant_id` / `org_id`） |
| `AgentBaseGraph.get_output()` | `state["formatted_output"] or state["result"]` を `output` に載せる。**カスタム欄は透過しない** | `post_process` で `formatted_output` へ畳み込む |
| `FunctionNode._extra_security_gate_output` | `(self, result: dict) -> dict` — **`state` を受け取らない** | モード判定は `result["gate_kind"]` の宣言に依存させる |
| `FunctionNode._extra_security_gate_input` | `(self, state: AgentState) -> AgentState` | S-2 は state を見て検査できる |
| `GraphNode` の既定 | `error_strategy="propagate"` / `propagate_hitl=False` | 既定のまま使用（設計書に記録） |
| **`BaseGraph.invoke()` の初期 state** | ノード実行前に `user_input` / `input_context` を **そのまま `initial_state` に載せる** | 機微本文の隔離は **`pre_process` では遅い** → 入口 adapter で `payload_ref` 化（「入口 adapter における機微本文の隔離契約」節） |
| **`BaseGraph.__abstractmethods__`** | `name` / `state_schema` / `_validate_config` / `register_nodes` / `add_edges` / `route` / `get_output` の **7 つ** | inner graph は 7 メソッド全件を実装（「inner の `BaseGraph` ABC 実装」節） |
| **`FunctionNode._security_gate_output` の既定走査範囲** | `result` の **top-level string のみ** `detect_credentials` に掛ける。入れ子は走査しない。`GraphNode` は既定 no-op | 入れ子 string leaf を再帰走査して `detect_credentials` を呼ぶ（S-3 拒否条件 7） |
| **`GraphNode.required_trust_level` の既定** | `TrustLevel.ANONYMOUS` | outer/inner の全ノードでクラス本体に明示宣言（S-1 表） |

## 共通部品リフレッシュの判断記録（2026-08-14 · Batch 4-4）

| # | 論点 | 判断 | 反映先 |
|---|---|---|---|
| R-1 | 部品の版上げ | `evidence-gate` 0.2.0→**0.3.0**（`projection.py` が追加）/ `ingest-row-pipeline` 0.3.1→**0.4.0**。他 6 部品は既に最新。`verify_part` **SUSPECT 0**（19 ファイル） | `src/` 各部品・適用部品指定表 |
| R-2 | `evidence-gate` 0.3.0 の `projection.py` を本番配線するか | **配線しない。** 当 AG は `_response_contract()`（検証側の定数からプロンプトを機械生成する型・AT-17 で 87 件検証）で**閉じた応答スキーマ**を既に強制しており、typed projection はこれと重複する。ただし配線しない代わりに、projection が担うはずだった**型の健全性は自前で閉じた**（R-4） | 本表・`docs/03` AT-19 |
| 🔴 R-3 | `SCOPE_KEYS` | **`("tenant_id","org_id")` 維持＋根拠を明記**（2026-08-14 裁定）。テンプレート共通既定の「区画なし」でも、写経残りの旧固定 2 軸でもない。`org_id` は**同一顧客内の組織境界**として設計に明文の根拠がある（承認前 SDS 改訂は未公開であり、同社他組織から引けてはならない）。DB 5 テーブル全部が両列を持ち、検索契約が両方を必須引数にしているため**設計不変条件**。マルチテナント SaaS 分離ではない（顧客分離はデプロイの DB/KB 資格情報） | `src/entry_adapter/auth.py` の adaptation コメント・`_ctx()` docstring |
| 🔴 R-4 | **`error_code` が inner→outer で消失**（レビュー High） | **修正。** provider 例外時、`LlmDraftStageNode` は `error_code="llm_call_failed"` を記録するのに呼び出し元には `status: error` しか届かず、`error_log` には**絶対パス入りの traceback** が載っていた。3 箇所で落ちていた: ①state schema が channel を宣言しておらず LangGraph が破棄 ②inner `get_output()` が射影しない ③`GraphNode` が `SubgraphError` を propagate するため `merge_output` に到達しない。①②③を塞ぎ、`error_strategy="handle"` ＋ `on_subgraph_error` で**正規化した失敗**を射影する。inner `error_log` は**そのまま転送しない** — `_error()` の 7 箇所が `{exc}` で生の例外文を埋め込むため、既知コードの **allowlist** で分類する | `src/schemas/state.py` / `src/graph/domain_workflow_graph.py` / `src/nodes/main_node.py` / `src/graph/graph.py` |
| 🔴 R-5 | **LLM 由来 `source_id` の型で `TypeError`**（レビュー Medium） | **修正。** `item.get(key) not in known` は値を hash するため、LLM が `source_id: {...}` を返すと `TypeError: unhashable type` がノードの外へ抜け、**扱い済みのスキーマ違反ではなく framework error** になっていた。membership 判定の前に `str` かつ非空を検査する。非 str の id はサーバ構築の catalog と一致し得ないので、正当な finding は失われない | `src/graph/domain_workflow_graph.py` |
| R-6 | scaffold 同梱物 | `scripts/` 全 23 本・CI 設定・PB-7 stub を正本と **byte-identical** に同期。PB-7 は旧世代が `config/agent.yaml` の `hitl.enabled` を読んでおり、当 AG は `config.yaml` 側に持つため**将来 HITL を有効化しても永久に SKIPPED（偽の緑）**になる状態だった | `scripts/`・CI 設定・`tests/proof_of_boundary/` |
| R-7 | manifest | AgentRegistry 準拠の root-level 形式へ。旧 `agent:` ネストは loader から**不可視**で、`namespace`/`enabled` が既定値に落ち registry key が `default/unknown@latest` に縮退する。`module`+`class` 分割は `rsplit` 前提の loader で `KeyError`→503。旧 9 キーは値を維持し、`namespace: mfg` / `enabled: true` を追加。runtime 移設対象は 0 件（既に `config.yaml`） | `config/agent.yaml` |
| R-8 | SDK 1.0.0 → **1.0.1[anthropic]** | scaffold CI 正本への同期に伴う版上げ。`.venv` に実インストールして全 528 件を実行し緑を確認（当初 `--full` の出力を誤読して検証済みと述べたが、実際には `.venv` は 1.0.0 のままだった。レビュー指摘で判明し実測し直した）。`[anthropic]` extra は `langchain-anthropic==1.4.3` を足すが、当 AG は `config["llm"]` 注入シームのみで `AnthropicClient` を直接 import しない | CI 設定 |

### 上流の共通部品ライブラリへの申し送り（本改版では直さない）

| 部品 | 内容 |
|---|---|
| `evidence-gate` 0.3.0 | `strict.py` の docstring が raw 文字列でないため `DeprecationWarning: invalid escape sequence '\d'` が出る（将来の Python では SyntaxError）。当 AG・Travel Supplier Sustainability Claim Evidence Validator の両方で発生 |
| `evidence-gate` 0.3.0 | `project_typed_finding()` が `dict` / `list[dict]` 宣言に対して**再帰しない**ため、ネストした未宣言キーが素通りする（Travel Supplier Sustainability Claim Evidence Validator のレビューでも同一指摘） |
| `cat2-graph-skeleton` 0.1.4 | 既定の `on_subgraph_error` が `f"...: {error}"` で**例外文をそのまま埋め込む**。`SubgraphError` では traceback ごと state channel に載る。当 AG は override で回避した |
