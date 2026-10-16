# Phase 4 実装・検証報告

検証日: 2026-10-16（Asia/Tokyo）。基点はPhase 3の `b9b37c7`。

## 1. 作成・変更したファイル

| ファイル | 内容 |
| --- | --- |
| `src/trading_bot/persistence/supabase_repository.py` | Data API通信、バッファ付きSession、CAS原子commit、共有利用枠 |
| `src/trading_bot/persistence/sqlite_repository.py` | benchmark・snapshot・公開用テーブルの非破壊追加 |
| `src/trading_bot/persistence/decision_repository.py` | Repository契約へ一般化、Supabase共有予約の利用 |
| `src/trading_bot/benchmark.py` | 公式出所CSV、NAV検証、整数口数と端数現金、同日比較 |
| `src/trading_bot/performance.py` | Decimal成績・ドローダウン・監査付き評価スナップショット |
| `src/trading_bot/dashboard.py` | 公開専用の情報選別・version付き公開 |
| `src/trading_bot/phase4_cli.py`, `cli.py` | DB / benchmark / portfolio / dashboard / operate操作 |
| `src/trading_bot/strategy/{models,gemini,security}.py`, `config/ai.yaml` | 推定費用・秘匿情報除去の拡張 |
| `supabase/migrations/20261016000100_trading.sql` | 内部口座・NUMERIC列・イベント・原子的RPC・権限 |
| `supabase/migrations/20261016000200_public.sql` | 公開専用テーブル・SELECT RLS・公開RPC |
| `supabase/bootstrap.sql`, `scripts/build_bootstrap.py` | SQL Editor向けの一括適用SQLと生成処理 |
| `scripts/{migrate_supabase,phase4_smoke,persistent_decision,test_gemini}.py` | migration・隔離永続化確認・継続dry-run |
| `scripts/verify_sql.mjs` | ローカルPostgreSQL互換環境でのSQL/RLS実行検証 |
| `.github/workflows/{migrate-supabase,phase4-smoke,persistent-decision,test-gemini}.yml` | main限定の手動Workflow、cronなし |
| `.github/workflows/{ci,deploy-pages}.yml` | 実PostgreSQL CIと公開可能設定だけのPagesビルド |
| `web/src/{dashboard.tsx,dashboard.css,vite-env.d.ts,main.tsx}` | モバイル優先の読み取り専用ダッシュボード |
| `web/scripts/runtime-config.mjs` | URL・Publishable Keyだけを環境変数から生成 |
| `tests/phase4_helpers.py`, `tests/test_phase4_persistence.py` | SQLite / RPC共通契約・障害・Secret検証 |
| `tests/test_benchmark_performance.py`, `tests/test_postgres.py` | 比較計算・実PostgreSQL競合/RLS/UTC枠 |
| `tests/test_gemini_strategy.py`, `web/tests/{dashboard,site}.spec.ts` | 費用換算・新旧Web回帰テスト |
| `.env.example`, `.gitignore`, `pyproject.toml`, `uv.lock` | 環境設定・公開設定除外・依存固定 |
| `README.md`, `docs/architecture.md`, 本報告書 | 運用・公開・復旧・検証結果 |

## 2. Supabase設計

内部は `bot_private.accounts / balances / positions / records`。口座スコープのPK/FK、一意イベントキー、非負残高・数量・状態CHECK、NUMERIC金額/価格/FX、timestamptzを採用した。recordsは既存のPydanticイベント契約をJSONBで保持し、金融値のNUMERIC列も持つ。JSON内のDecimalも文字列表現なのでfloatへ変換しない。

口座IDはDBスコープで区別し、既存モデル内の `paper` IDを維持する。継続口座は `paper-live`、試験口座は明示フラグ＋`test-` prefixを必須とした。通常口座の削除/リセット操作はない。

## 3. migration適用結果

- ローカルPGlite: SQLを実行して適用成功。CAS、rollback、anon/authenticated権限、公開SELECTを検証。
- CIのPostgreSQL 17: migrationを実際に適用して専用テストを実行。
- **実Supabase: 適用成功を動作確認。** 最初は `Supabase migration missing; apply tracked SQL in SQL Editor` で停止した。その後、ユーザーがSQL Editorでbootstrap.sqlを実行し、実際の口座・注文・約定・予約・公開操作が成功した。
- SQL Editorで `supabase/bootstrap.sql` 全体を一度実行する手順を用意した。キー再入力は不要。Actions適用を選ぶ場合のみ、追加の所有者接続URI `SUPABASE_DB_URL` が必要。登録済み4 Secretの値は取得・表示していない。

## 4. RepositoryとSQLite互換性

Session / PortfolioRepositoryを維持し、既存PaperBroker・ledger・valuation・risk・DecisionEngineを再利用。Supabaseは一貫した口座/イベント/versionのread RPC→Python共通計算→単一commit RPCで保存する。複数REST書込みへ置換していない。

PostgreSQLはwriter共通advisory lock、口座行ロック、version比較、台帳/保有/原価照合、注文・約定FKと状態遷移検証を行う。古いversionなら全体を拒否し、部分反映しない。ネットワーク例外時は秘密を含まない診断だけを返し、最終書込みだけの自動再送はしない。

## 5. 口座・注文・約定の永続化

SQLite/RPC共通契約と実PostgreSQLで、初期化一回、再接続、JPY/USD・保有・注文・約定・FX・現金台帳・判断の保存を確認した。約定後の再実行でも注文・約定は1件だけ。検証用の100株・合成価格による買付後残高も共通ロジックと一致した。

実Supabaseでも同じ隔離口座のWorkflowを2回実行し、残高989990 JPY、注文1件・約定1件、利用枠予約1件（API通信なしの試験用エラー予約）を維持した。通常口座とは分離しており、試験用の価格を公開していない。継続口座paper-liveも初回のみ100万円で作成され、その後の3実行で作成日時2026-10-16T14:36:07.259522Z・残高100万円が維持された。

## 6. 同時実行・二重注文防止

実PostgreSQLで2つの約定が同じ元残高を読み取る競合を再現し、片方だけがcommit、他方はRepositoryConflict、約定1件・残高949950 JPYを確認した。途中に不整合残高を入れると注文状態の変更も含めrollbackする。終端注文のPENDINGへの逆行やテスト口座からの公開も拒否する。

## 7. API使用量の永続化

API前にSupabaseのRPCが全口座共通の日次/月次枠をUTCサーバー時刻で確認・予約する。同時予約でも上限を超えず、別口座でも共有する。成功・エラー・中断予約を保持し、再実行で枠を戻さない。使用量不明はnull。テスト削除による枠の払い戻しも禁止した。従来のtest-gemini.yml実APIモードも隔離Supabase口座経由へ変更し、使い捨てSQLiteによる共有上限の迂回を防いだ。

標準有料単価による参考換算費用をDecimalで保存する。これは無料枠適用後の実請求額ではない。SDKの総トークン数−入力数を出力側としてreasoning分も含め、情報不明ならnull。無料適用や請求0円を保証しない。[Google公式料金表](https://ai.google.dev/gemini-api/docs/pricing)の3.5 Flash-Lite入力0.30/出力2.50 USD（各100万token）を2026-10-16に確認した。

実Supabaseの継続Gemini dry-runに成功。モデルgemini-3.5-flash-lite、入力1801・出力163・合計1964 token、thoughtはnull、標準有料相当0.0009478 USDを保存した。2銘柄ともHOLD（理由：現金比率維持のため非保有を継続）、注文登録0件。次のmock実行でもAPI使用量は1→1のまま維持され、追加Gemini通信は0回だった。

## 8. オルカンの取得元・取得方法

[運用会社の商品ページ](https://www.am.mufg.jp/fund/253425.html)を対象とする。今回は403となり、robots・継続自動取得・公開再配布条件を確認できなかった。制限を迂回するスクレイピングは行っていない。

指示書で認められた代替として、正式出所の手動CSV取込と交換可能なBenchmarkProviderを実装した。NAV日、公表/観測日時、ファンドID253425、1万口JPY価格、分配金、派生値の公開許可を検証する。ETF・指数を代替しない。実NAVを取得・保存できたとは報告しない。

## 9. 比較計算結果

テスト専用の合成NAVで、100万円×10000÷20000=500000口、同価格評価100万円、22000評価110万円、Bot100万円との差は超過リターン−10%と確認した。これは実運用成績ではなく、自動テストの計算例である。

購入適用日は口座開始の翌JP営業日以降を明示し、該当日のNAVがなければ前日値で買わない。端数口数を切り下げ残額を現金にする。NAVに反映済みの信託報酬は二重控除しない。分配金がある場合は専用台帳がないため比較を停止する。

同じJST評価日の公表/観測済みNAVのみ比較。欠損時はnull＋不完全理由を保存する。snapshotは口座＋時刻で一意、訂正理由と旧評価全体を監査履歴に残す。

## 10. GitHub Pages

[公開ダッシュボード](https://okina4431.github.io/ai-trading-bot/)へ反映済み。[初回デプロイ](https://github.com/okiNa4431/ai-trading-bot/actions/runs/38914814246)はSUCCESS。HTTP 200と390px実表示を確認した。[完成版デプロイ](https://github.com/okiNa4431/ai-trading-bot/actions/runs/38918380665)もSUCCESS。

概要・資産推移（期間切替/表）・保有・売買・AI判断・API利用を実装。最終正常更新を表示し、更新失敗時は取得済み情報を保持する。空データ・不正応答・不正通貨・AI文章のHTML注入をブラウザテストした。取引/比較データがまだない状態で架空の利益・履歴は表示しない。SQL適用後、継続口座の実際の仮想状態（初期100万円・約定0件・AI判断3回・実API1回）が公開されることを実ブラウザで確認した。まだ仮想売買は開始しておらず、実証券注文もない。オルカン比較値は欠損として「—」表示のままである。

## 11. 公開権限・RLS

内部schemaの権限を剥奪し、RPCはservice_roleのみ。公開専用 `bot_dashboard` はanon/authenticated SELECTだけ、INSERT/UPDATE/DELETE不可。内部への所有者権限ビューは作らない。実PostgreSQLで両ロールの拒否と公開SELECTを実行確認した。

公開DTOには資産派生値・仮想保有/約定・AI説明・集計使用量だけを選別する。生NAV/OHLCV、入力スナップショット、raw response、内部ID/ハッシュ、機密エラーを含めない。未許諾NAVでは比較派生値も公開しない。Pagesへ渡すのはURL/Publishable Keyだけで、Secret/Gemini/DB URIはビルドへ渡さない。

実SupabaseでもPublishable Keyによる公開SELECTが成功し、内部read/delete RPCと公開INSERT/UPDATE/DELETEの5操作が拒否された。公開Webは一般利用者と同じキーで実データを読み取った。

## 12. テスト件数

| 項目 | 結果 |
| --- | --- |
| 既存Python 195件 | 成功・維持 |
| 追加オフラインPython 57件 | 成功 |
| 実PostgreSQL専用23件 | CIで23件成功 |
| ローカルpytest -v | 252成功、23skip（通常PostgreSQLサーバーなし） |
| uv sync --locked | 成功、65パッケージ |
| Ruff check / format --check | 成功 |
| mypy src | 成功、41ソース |
| Web型チェック/本番ビルド | 成功 |
| Webブラウザ | 13件成功、4画面幅・異常応答含む |
| PGlite SQL実行 | migration・CAS・rollback・RLS成功 |

全Pythonのユニークテスト数は275件。通常のWindows/Ubuntuジョブでは23件skipし、専用の実PostgreSQLジョブで実行する。skipを成功件数へ含めない。市場カレンダー/NumPy依存のDeprecationWarning（最終ローカル1963件）があり、依存更新時の確認課題として残す。

## 13. CIと修正

[CI f723e7d](https://github.com/okiNa4431/ai-trading-bot/actions/runs/38915465694)はWindows・Ubuntu・Web・PostgreSQL 20件すべて成功。

最初のPostgreSQL CIは公開拒否のSQLSTATE40001がRepositoryConflictへ変換されるのに、テスト側がpsycopg.Errorを期待して失敗した。検証条件を緩めず正しい公開例外型を明示して修正した。追加UTC境界テストではSQL文字列の `{at}` がpsycopgのformat変数と解釈される失敗を修正した。SQL本体の境界判定を変更して通したわけではない。

[完成版CI f1add3f](https://github.com/okiNa4431/ai-trading-bot/actions/runs/38918380486)はWindows・Ubuntu各252件、実PostgreSQL専用23件、Web13件すべて成功。ユニークなPython275件を全て実行成功させた。UTC境界の試験用時刻もRPCと同じJSON時刻形式に修正した。

## 14. 実Supabase Workflow

[Phase 4 persistence smoke run 38915142870](https://github.com/okiNa4431/ai-trading-bot/actions/runs/38915142870)はFAILED。安全な診断は `Supabase migration missing; apply tracked SQL in SQL Editor`。初期化前のreadで停止し、Gemini通信は0回。

SQL適用後、以下の手動実行で継続口座と隔離口座を確認した。

| Workflow | 結果・証跡 |
| --- | --- |
| 隔離永続化1回目 | [38916936111](https://github.com/okiNa4431/ai-trading-bot/actions/runs/38916936111) SUCCESS |
| 同じ隔離口座2回目 | [38917049060](https://github.com/okiNa4431/ai-trading-bot/actions/runs/38917049060) SUCCESS、残高/注文/約定/予約を維持 |
| 継続口座mock | [38917054349](https://github.com/okiNa4431/ai-trading-bot/actions/runs/38917054349) SUCCESS、HOLD2・API0・注文0 |
| 継続口座Gemini | [38917212570](https://github.com/okiNa4431/ai-trading-bot/actions/runs/38917212570) SUCCESS、HOLD2・API1・注文0 |
| Gemini後の別mock | [38917446239](https://github.com/okiNa4431/ai-trading-bot/actions/runs/38917446239) SUCCESS、使用量1→1、口座作成日時不変 |
| 旧テストの共有枠対応後 | [38918380538](https://github.com/okiNa4431/ai-trading-bot/actions/runs/38918380538) FAILED、Google HTTP 503、失敗予約を保存・注文0 |
| 同経路の手動再確認1回 | [38918600079](https://github.com/okiNa4431/ai-trading-bot/actions/runs/38918600079) FAILED、Google HTTP 503、失敗予約を保存・注文0 |

旧テストの再確認は上記1回で止めた。各WorkflowのAPI通信は1回だけで、SDK内/アプリ内の自動リトライは0。2件の503はGoogle側のエラーとして保存し、token/費用はnull。有料fallbackや使用枠の返却はしていない。継続口座の実Gemini成功とは分けて報告する。

## 15. 無料枠・制約

Supabase Freeは[公式のDB容量](https://supabase.com/docs/guides/platform/billing-on-supabase)500MB、[非キャッシュ転送](https://supabase.com/docs/guides/platform/manage-your-usage/egress)5GB/月等を確認した。実プラン状態は未確認。[公開リポジトリの標準Actions runner](https://docs.github.com/en/actions/concepts/billing-and-usage)とGitHub Pagesを利用する。自動有料移行や有料モデルfallbackはない。

全口座writer直列化とイベント一括読込は小規模向け。運用拡大時には容量監視、ページング/アーカイブが必要。バックアップはFreeの自動復旧に依存せず、所有者が暗号化した非公開dumpを保管し、別環境で整合性を確認する手順をREADMEへ記載した。

## 16. 未解決事項

1. オルカン実NAVの取得・利用/派生公開許可、公表時刻、販売会社に応じた適用日を確認する。自動取得は未実装。
2. 実NAVを使った比較結果の保存・公開を確認する（口座/判断/利用量の継続dry-run・公開は確認済み）。
3. NAV訂正・分配金、配当/分割の完全処理、運用データのバックアップ手順を本番相当環境で検証する。
4. 短期間の年率・Sharpeは表示していない。定期自動売買は開始していない。
5. Google側503が2回発生した旧手動テスト経路は、提供元の回復後に再確認が必要。アプリは失敗記録・枠消費・注文なしの安全停止を確認済み。

## 17. 最終判定

**CONDITIONAL PASS：Supabase永続化・複数実行間の口座/使用量維持・実Gemini dry-run・RLS・公開を確認済み。オルカン実NAVの継続取得/公開条件と実比較は未完了。旧手動Geminiテストの最終再確認は提供元503で失敗。**

Phase 5の定期自動運用は、上記の実環境検証を完了するまで開始しない。現在のWebは実際の初期仮想口座とAI判断を表示しているが、売買後の運用成績や実オルカン比較が完成したとは扱わない。
