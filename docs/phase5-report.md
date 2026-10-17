# Phase 5 実装・検証報告

更新日：2026-10-17 JST。手動・隔離口座専用。定期運用の開始を許可する報告ではない。

## 実装

- `operations.py`：JPX/NYSEセッション、営業日と処理スロット由来の実行ID、DB lease/fencing、段階別状態、健全性要約。
- `market_inputs.py`：確定足の差分取得、非公開の不変データ世代とハッシュ、USD/JPYの取得時刻・観測時刻・鮮度、分割・配当検知。未解決イベントは取得窓から外れても保持する。
- `cycle.py`：既存スクリーニング・OrderPlanner・PaperBroker・DecisionEngineを統合。判断／注文登録と、別実行による後続確定足での約定を分離。後続足なしはPENDING、先行する足が欠損すれば停止する。
- `paper_cycle.py`：手動サイクル、読取専用の健全性確認・照合、明示的な管理者復旧。デフォルトはmock/HOLD・注文登録なし。実Geminiは1要求、再試行0。共有API枠を維持する。
- `phase5_fixture.py`：明示した合成価格・固定為替による5段階のBUY／約定／HOLD／SELL／約定。実相場・実Geminiの検証とは区別する。
- `20261017000100_operations.sql`、`phase5-upgrade.sql`：追加テーブル・RPC・ガード。適用影響は [migration説明](phase5-migration-impact.md) を参照。
- `paper-cycle.yml`、`paper-healthcheck.yml`、`paper-reconcile.yml`：すべてworkflow_dispatchのみ。test-phase5-*限定、main保護、金融writer直列化。復旧は初期無効で、利用前にrepository variableとEnvironmentの承認保護設定が必要。
- `ci.yml`：既存Windows・Ubuntu・Webに加え、実PostgreSQL上で新しい排他・障害・売買検証を実行。

DB停止時の別DBへの切替はない。期限切れrunnerのmutationをDBで拒否し、未確定の結果は自動再送しない。paper-liveの金融mutationはDBでも拒否する。元口座・履歴・API枠の削除やリセットは行わない。

## 計画との対応・限定

初期の小規模な手動検証では、Storageの代わりにprivate schema内の不変JSON世代を採用した。コミット済み世代だけを読取り、価格バーを再検証してハッシュを比較する。1世代4MB上限。非公開データをGit・公開artifact・Pagesへ保存しない。総容量の監視・世代保持／回収方針とStorage移行は、定期運用開始前の課題である。

公開用のテスト要約はprivate側に保持する。今回の実装は一般公開への自動昇格を行わない。オルカン実NAV・派生比較値は公開しない。休場・実行窓外はCLIで理由付きSKIPPEDとし、注文処理しない。

## 取得元

株価は既存YahooFinanceProvider（yfinance、未調整OHLCV）、企業行動はYahooのDividends／Stock Splits、為替はYahoo `JPY=X` の確定済み5分足。価格時刻を取得リクエスト時刻で代用しない。API提供保証はなく、欠損・遅延・取得不能時は安全停止する。取得データの公開許諾を新たに確認したという意味ではなく、価格・為替・企業行動の原本を公開出力しない。

2026-10-16 15:52 UTCの実取得ではUSD/JPY価格時刻15:50 UTC、取得時点の経過約124秒を確認。7203.Tの企業行動1件、AAPLは0件を取得した。「0件」と通信失敗を区別する。この取得確認は売買サイクル全体の実データ検証とは別である。今回の実Gemini呼出しは0件。

## テスト

既存318 Python・13 Webを維持し、実DBの並列claim、runner停止、lease失効、古いtoken、DB時刻、金融commit応答喪失、API予約応答喪失、RLS、JP／US複数実行売買などを追加した。503／429／timeoutはモックで最小呼出しと枠維持を確認する。

ローカル最終実行：339 passed、38 skipped（実PostgreSQL用）、29.62秒。Ruff・mypy（45ソース）とPGliteによるmigration／CAS／rollback／権限検証もPASS。初回のsandbox内実行は一時ディレクトリの制約で中断し、権限を確保した再実行で上記結果を確認した。

実装コミット `16aaaee` の [CI実行39004143657](https://github.com/okiNa4431/ai-trading-bot/actions/runs/39004143657) は全4ジョブ成功。Windows／Ubuntuの共通339件、PostgreSQL 17の38件、Web 13件がPASS。Pythonの重複を除いた合計は377件（既存318件＋新規59件）。実PostgreSQLは11.55秒、Webは6.2秒。APIの実通信はCIに含めない。

検証中に、合成シナリオの過去注文へ未来のFXを使う不備、JP翌営業日の最初の適格足を00:30 UTCとしていた不備を発見し修正した。paper-modeの時刻検証と欠落足検知を維持し、正しい合成時刻で日米往復を再検証した。検証条件を緩めて通していない。

合成データの期待値（初期100万円）：

| 市場 | 別実行での結果 | 最終残高・実現損益 |
| --- | --- | --- |
| JP | 400株BUY登録→約定→HOLD→SELL登録→約定 | JPY 1,001,898、実現損益JPY 1,898 |
| US | 明示的10万円仮想両替、2株BUY登録→約定→HOLD→SELL登録→約定 | JPY 900,000、USD 674.49、実現損益USD 9.49 |

USの最終評価は固定150円/USDで1,001,173.50円。両替費用も含む。これは合成データの会計検算であり、実運用リターンではない。

## 実Supabase適用・完了条件

適用前の [healthcheck実行39004150204](https://github.com/okiNa4431/ai-trading-bot/actions/runs/39004150204) はmigration不足を検出し、終了コード1で安全停止した。その後、所有者が追加専用SQLをSQL Editorで実行した。[適用後healthcheck実行39007534885](https://github.com/okiNa4431/ai-trading-bot/actions/runs/39007534885) は成功し、`pre_migration_data_preserved: true`、未解決0件、初期テスト口座未作成を確認した。所有者のDDL操作を除き、適用確認は読取専用である。

実Supabase上の5段階×日米の手動Workflow検証は全段階成功。対象は `test-phase5-fixture-jp/us` のみで、合成価格・固定為替・mock戦略を明示する。単体テスト用SQLiteやCIのPostgreSQLとは別の検証である。実Geminiの使用枠予約や通信は行っていない。

| 段階 | JPの手動実行 | USの手動実行 | 結果（各市場） |
| --- | --- | --- | --- |
| BUY登録 | [39007575363](https://github.com/okiNa4431/ai-trading-bot/actions/runs/39007575363) | [39007830857](https://github.com/okiNa4431/ai-trading-bot/actions/runs/39007830857) | 注文1・約定0・PENDING 1 |
| 後続足でBUY約定 | [39007683023](https://github.com/okiNa4431/ai-trading-bot/actions/runs/39007683023) | [39007866421](https://github.com/okiNa4431/ai-trading-bot/actions/runs/39007866421) | 注文1・約定1・PENDING 0 |
| HOLD | [39007722018](https://github.com/okiNa4431/ai-trading-bot/actions/runs/39007722018) | [39007902187](https://github.com/okiNa4431/ai-trading-bot/actions/runs/39007902187) | 追加注文なし |
| SELL登録 | [39007758599](https://github.com/okiNa4431/ai-trading-bot/actions/runs/39007758599) | [39007941471](https://github.com/okiNa4431/ai-trading-bot/actions/runs/39007941471) | 注文2・約定1・PENDING 1 |
| 後続足でSELL約定 | [39007795190](https://github.com/okiNa4431/ai-trading-bot/actions/runs/39007795190) | [39008022784](https://github.com/okiNa4431/ai-trading-bot/actions/runs/39008022784) | 注文2・約定2・PENDING 0 |

全10実行で `verified: true`、評価作成成功。各段階は別runnerで実行し、Supabaseの状態を次の実行に引き継いだ。

完了した同一実行IDを別Workflowで再起動した [JP 39008076452](https://github.com/okiNa4431/ai-trading-bot/actions/runs/39008076452)、[US 39008099019](https://github.com/okiNa4431/ai-trading-bot/actions/runs/39008099019) は、どちらも `ALREADY_COMPLETED` で正常終了。完了slotを再処理しないことを実サービスでも確認した。

## 残存条件

現段階で定期運用開始は不可。実相場を使った新規判断から翌営業日の後続足約定までの検証は、市場の実行窓に合わせて別途確認する。復旧Environmentの管理者承認設定、外部バックアップと復旧訓練、総容量監視、実行欠落の外部監視も定期運用前の条件とする。テスト成功だけでscheduleや継続口座への注文を有効化しない。
