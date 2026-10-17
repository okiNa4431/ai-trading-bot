# Phase 5.5 運用前検証報告

2026-10-17 JST。基点 `17e0c55`。**実相場の検証は未完了、定期運用開始不可。** 利用者から「個別許諾なし・実データ取得を無効のまま、無料提供元調査と合成データでの安全基盤検証を進める」と指示を受け、その範囲で実装した。

## データ提供元と取得範囲

[無料データ調査](phase55-data-sources.md)で8候補の無料枠・対応市場・時間足・遅延・保持条件・公式リンクを比較した。0円で全要件を満たす構成は確認できていない。特に日本株の現在時間足と企業行動が不足し、他社も保持・外部AI等の未確認点がある。利用可能と推定して有効化しない。

新たな市場APIからの株価・為替・企業行動取得0件、実Gemini通信0件。公式規約・仕様ページの調査閲覧は、取引用データ収集とは別である。今回のFX・XML・株価パーサのテスト入力は全て合成値。原本やテスト利益をPagesへ公開しない。

## 変更ファイルと動作

| 対象 | 内容 |
| --- | --- |
| `data_access.py`、`provider.py`、`market_inputs.py`、`scripts/paper_cycle.py` | 実データcycleだけでなく、直接CLI／Adapterの株価・FX・企業行動も通信前に拒否。解除入力なし。fixture・health・reconcileを維持 |
| `reference_fx.py` | Twelve Data FX／ECB日次参照の未接続アダプター。ヘッダー認証・サイズ制限・再試行なし・異常値と時刻検証。ECBの日付から新鮮な価格時刻を捏造しない |
| `preflight.py`、`scripts/phase55.py` | 固定JP／US隔離口座の冪等な準備、読取監査、実カレンダーの候補窓。準備をHOLD成功と呼ばない |
| `scripts/phase55_monitor.py`、`config/phase55-expectations.json` | 明示した予定の欠落検出・異常終了・スコープ内API予約・Environment保護の読取確認。予定初期空・外部監視未設定を明示 |
| `phase55-validation.yml`、`phase55-monitor.yml` | workflow_dispatchのみ。Geminiキーなし、取引jobなし |
| 既存`fetch-market-data.yml`、`persistent-decision.yml`、`test-gemini.yml` | 実データの経路に停止ゲート。artifactは停止状態JSONだけ、生データを含めない |
| `cycle.py` | 最初の後続足が古い場合、都合のよい新しい後続足を選ばず停止 |
| `supabase/phase55-monitoring.sql` | 所有者向けSELECTのみ。DB総量・世代数・全口座共通API予約。DDLなし |
| `tests/test_phase55.py`、`tests/test_operations_pg.py`、既存Adapter試験 | 取得停止・為替契約・時刻・欠落・承認保護・安全約定・新規準備の非破壊性を追加。既存Adapter試験は許諾境界だけモック、実通信禁止とデータ検証条件は維持 |
| Phase 5.5文書・README | 提供元比較・運用チェック・外部backup復旧・残存条件 |

## テスト

ローカル：384件PASS、41件skip（実PostgreSQL）、28.88秒。Ruff・format・mypy（48ソース）PASS。初回に既存Workflowのartifact契約が失敗したため、生データ公開を戻さず停止状態だけのJSON artifactで修正し、全テストを再実行した。追加PGテストのcursor誤用も修正し、実DBで41件PASSを確認した。監査の保存確認失敗を非0終了にする回帰テストも追加した。最新CI結果は完了後に追記する。

Phase 5の既存377件とWeb13件を維持する。DB排他・fencing・中断・応答喪失・quota・合成売買を検証し、実相場の成功と取り違えない。

新規口座は `test-phase5-real-jp` / `test-phase5-real-us`、初回のみ各100万円・USD0で準備する。prepare時にpaper-liveと既存fixture両口座の読取hashを前後比較し、既存口座を変更しない。実Supabaseの新規migrationは不要。

実Supabaseでの準備：

| 市場 | Workflow | 準備時刻（UTC） | 結果 |
| --- | --- | --- | --- |
| JP | [39010447556](https://github.com/okiNa4431/ai-trading-bot/actions/runs/39010447556) | 2026-10-17 06:35:13.592933 | PREPARED_NOT_VALIDATED、注文0・約定0・Gemini0 |
| US | [39010487522](https://github.com/okiNa4431/ai-trading-bot/actions/runs/39010487522) | 2026-10-17 06:35:52.507839 | PREPARED_NOT_VALIDATED、注文0・約定0・Gemini0 |

両実行で `protected_scopes_unchanged: true`、台帳照合成功。対象銘柄・実データ観測時刻・約定時刻・実相場損益はまだ存在せず、取得成功や運用利益として報告しない。

| 追加確認 | Workflow | 実結果 |
| --- | --- | --- |
| JP監査 | [39010613071](https://github.com/okiNa4431/ai-trading-bot/actions/runs/39010613071) | 既存データ保持true、未解決0、注文・約定・判断・API予約0 |
| US監査 | [39010614976](https://github.com/okiNa4431/ai-trading-bot/actions/runs/39010614976) | 同上。新口座の両替も0 |
| 独立監視 | [39010628172](https://github.com/okiNa4431/ai-trading-bot/actions/runs/39010628172) | 観測成功、readinessはNOT_READY。承認用Environmentは両方未作成、予定未登録、総容量／egress／backup未確認を検出 |
| JP準備の再実行 | [39010704766](https://github.com/okiNa4431/ai-trading-bot/actions/runs/39010704766) | ALREADY_PREPARED、資金・口座作成日時を再設定せず |
| 停止中HOLDの拒否試験 | [39010729089](https://github.com/okiNa4431/ai-trading-bot/actions/runs/39010729089) | 期待通りfailure。MarketAccessDisabledで認証・初期化・実データ通信前に拒否。実市場HOLDは未実施 |
| 異常終了の検知試験 | [39010752861](https://github.com/okiNa4431/ai-trading-bot/actions/runs/39010752861) | 期待通り非0終了。上記拒否runをABNORMAL_WORKFLOW_ENDとして検出。DB未解決0、既存保持trueを再確認。成功扱いで隠さない |

監査の最新スコープJSONは各1425 bytesだったが、DB総容量・過去世代・請求対象転送量ではない。非公開口座残高は初期各100万円・USD0、約定損益0。市場価格・観測・約定の実データは保存していない。Phase 5 fixture口座も金融操作していない。

## 未実施・未確認

| 項目 | 状態・条件 |
| --- | --- |
| 実データmock HOLD | 未実施。提供元権利・接続有効化が未完了 |
| 実データ注文・約定・評価・翌営業日持越し | 未実施。現在は土曜日。過去時刻で注文せず、複数営業日を完了扱いしない |
| 実Gemini | 未実施。入力の権利・外部AI送信範囲・残枠確認後、最小1通信から。BUY/SELLを強制しない |
| DB総量・請求対象egress | 未確認。最新JSONサイズと区別。所有者SQL／Dashboardで実数確認が必要 |
| 外部バックアップ・復元 | 手順を準備。実DBの暗号化dump／外部復元訓練は未実施 |
| Environment承認保護 | phase55-validation／phase5-recoveryとも未作成・required reviewersなしと実測。取引有効化前に設定・承認動作の確認が必要 |
| 外部欠落監視 | 未設定。手動ActionsだけではGitHub全面停止を検知できない |

## 次の手動実行条件

[運用チェック手順](phase55-operations.md)に、権利が解決した場合の最短HOLD候補（JP10/19 16:45〜17:15 JST、US10/20 06:15〜06:45 JST）と、以降の別営業日BUY・後続約定・持越し・SELLを記載した。予約や解除許可ではない。保持／削除設計、配信遅延、データ品質、承認保護を確認後に現在のカレンダーで再計算する。

**判定：取得停止下の準備・合成回帰検証の段階。Phase 5.5実相場検証は未完了、定期運用は開始不可。** 許諾付きデータ一式、実使用量・外部復元・承認保護・監視・複数営業日の検証が残る。cron、実注文、paper-liveの金融操作を有効化しない。
