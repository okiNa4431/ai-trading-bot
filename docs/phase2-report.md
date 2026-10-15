# Phase 2 実装・検証報告

実施日: 2026-10-15（日本時間）。**判定: PASS — Phase 3に進める。**

## 実装結果

Phase 1のデータ取得・検証・保存と既存69テストを維持し、JPY 100万円 / USD 0から始まるローカル仮想口座を追加しました。注文受付と約定を分離し、実資金・証券会社API・Gemini判断には接続しません。

| 新規モジュール・ファイル | 役割 |
| --- | --- |
| broker/base.py | 将来のブローカー交換を想定したProtocol |
| broker/paper.py | 注文、取消、全量約定、明示FX、口座操作 |
| broker/execution.py | 後続確定バー、営業日・昼休み・夏時間・早引け、観測可能な価格の選択 |
| portfolio/models.py | Decimal・UTCの口座、保有、注文、約定、為替、台帳、評価契約 |
| portfolio/ledger.py | 平均原価、通貨別現金・株式確定損益、残原価 |
| portfolio/valuation.py | 時価・含み損益・JPY総資産・比率・超過記録 |
| risk/manager.py | 買付後10%・80%・最低現金20%の制限 |
| persistence/sqlite_repository.py | 永続化ProtocolとSQLiteトランザクション実装 |
| paper_cli.py | ローカル仮想口座CLI |
| config/paper.yaml | 売買・為替コスト、鮮度、単位、厳格化可能なリスク制限 |
| scripts/demo_paper.py | 固定為替・架空価格・一時DBによる再現デモ |
| tests/test_paper.py | Phase 2の58追加テスト |
| web/ | React・TypeScript・Vite・Tailwindの静的案内ページ、lockとブラウザテスト |
| .github/workflows/deploy-pages.yml | GitHub Pagesのビルド・デプロイ |

変更した既存ファイルはcli.py、pyproject.toml・uv.lock、.gitignore、CI Workflow、README.md、architecture.mdです。各新規パッケージの__init__.pyも追加しています。既存のテストファイルや正常な市場データは変更していません。

## 取引と時間の安全性

- 現物のみ。空売り・信用・追加入金・暗黙の通貨交換なし。
- 日本株100株・米国株整数株が既定。銘柄ごとの単位変更が可能。
- BUY / SELLの不利なスリッページ、率・固定手数料、FXコストと端数を履歴へ記録。
- リスクは全保有株と残高を同じ時刻の価格・為替で円建て評価し、コスト控除後で判定。10%・80%・最低現金20%を設定で緩和できません。
- 値動きによる超過はログ・評価履歴に記録し、自動で売却しません。
- 判断以前の価格、出来高0、閉場・休場・未確定バーでは約定しません。観測不足ならPENDING。
- historical / paperを区別し、価格時刻・バー確定時刻・取得時刻・処理時刻を保存。取得途中のバーは時刻が進んでも確定扱いにしません。
- 取得時刻が同じ一括データでも価格時刻で最新を選びます。再取得時刻で古い価格の鮮度をごまかしません。
- 注文、約定、残高、保有、台帳、評価を同じSQLiteトランザクションで更新。例外はrollback、同じIDの再実行は二重約定なし。
- 株式損益をJPY/USDで分け、為替交換費用は別履歴。平均原価に加え残原価総額を保持し、全売却時に端数を残しません。

## 実行例

```powershell
uv run python scripts/demo_paper.py
uv run trading-bot paper init
uv run trading-bot paper balance
uv run trading-bot paper positions
uv run trading-bot paper orders
uv run trading-bot paper history
uv run trading-bot paper buy --symbol 7203.T --quantity 100 --order-id example-buy
uv run trading-bot paper cancel --order-id example-buy
```

buyはPENDINGの受付だけです。売却はpaper sell、為替交換はpaper fx、約定はpaper execute、評価はpaper valueで明示実行します。詳細な日時・固定レート・Parquet指定はREADMEに掲載しています。

デモは架空のJP価格300/400 JPY、US価格100 USD、固定USD/JPY=150とその時刻を表示します。日本株200株購入→100株部分売却→明示FX→米国株2株購入→評価→DB再読込を実行し、JPY株式確定損益9,930を検証しました。**この金額は架空のテスト結果で、実績ではありません。** 一時DBは実行終了時に削除されます。

## テスト・CI

| 検査 | 結果 |
| --- | --- |
| 既存Phase 1テスト | 69件すべてPASS |
| Phase 2追加 | 58件すべてPASS |
| Python全体 | 127件PASS、ネットワーク遮断下で実行 |
| Ruff / format | PASS |
| mypy src | PASS、20モジュール |
| uv sync --locked | PASS、50パッケージ |
| TypeScript | PASS、アプリ・設定・ブラウザテストを含む |
| Vite本番ビルド | PASS |
| Playwright | 360 / 390 / 768 / 1440pxの4件PASS |
| Windows / Ubuntu CI | 両環境127件PASS、Ruff・mypy成功 |
| Ubuntu Web CI | ビルド・4ブラウザテストPASS |

[最終コードのCI](https://github.com/okiNa4431/ai-trading-bot/actions/runs/38740301741)
（コードコミット9b912d3）。初回CI後の追加レビュー修正も再実行して確認済みです。
複数スレッドによる同一注文の同時実行も1約定となり、台帳の通貨別合計が残高に一致することを確認しました。

検証中に修正した問題は、WindowsのSQLite初期接続の解放漏れ、未確定スナップショットの後日昇格防止、同時取得時刻での最新価格選択、古い価格の再取得時の鮮度判定です。それぞれ回帰テストを追加しました。

## GitHub Pages

**公開済み: https://okina4431.github.io/ai-trading-bot/**

[デプロイ成功](https://github.com/okiNa4431/ai-trading-bot/actions/runs/38740002407)。PagesをGitHub Actions方式で有効化し、静的成果物のアップロード・公開を確認しました。
公開URLを実ブラウザで開き、HTTP 200、390pxでの横はみ出しなし、ページ/アセットエラーなしを確認しています。運用準備中とGemini未接続を明示し、架空の運用成績・実際の口座データは表示しません。

## 残存課題とPhase 3

- 配当・株式分割は未対応。未調整価格による長期成績の正確性は保証しません。
- yfinanceは執行可能な気配値ではありません。ペーパー約定は確定後に確認する遅延したシミュレーションです。
- カレンダーはベンダーの時刻ラベルと差があり得ます。今後のデータ源変更時に再検証してください。
- 株価24時間・FX1時間という既定の鮮度制限により、休場明け等でPENDINGになることがあります。古いデータで強制約定しません。
- 過去の口座状態を復元するイベントリプレイは未実装。履歴モードの状態変更は時刻順が必要です。
- 為替損益の詳細な円換算要因分解、Supabase、Gemini判断、オルカン実比較は次の段階です。
- SQLiteはローカル正式サポート。Actionsで定期売買や口座永続化を行いません。
- カレンダー/NumPyおよびActionsのランタイムに非推奨警告がありますが、今回の検査は成功しています。

Phase 3へ進む技術的な阻害要因はありません。Geminiにはknown_marks等で時刻を切った観測済み情報だけを渡し、生成した注文にも既存の単位・現金・比率検査を必ず適用してください。
