# AI vs オルカン — AI Trading Bot

Geminiが日本株・米国株を仮想売買するポートフォリオと、オルカンへの一括投資の成績比較を最終目標とします。Phase 1の市場データ基盤に加え、Phase 2で仮想口座・売買エンジン・SQLite保存・簡易Webページを実装しました。AI判断や実資金の注文は行いません。現在は仮想運用準備中です。

## 環境構築

Python 3.12、Windows / Ubuntuを使用します。リポジトリのルートで実行してください。
[uv公式手順](https://docs.astral.sh/uv/getting-started/installation/)を参照してください。Windows PowerShellでは以下でも導入できます。

```powershell
python -m pip install uv==0.13.1
uv python install 3.12
uv sync --locked
```

Ubuntuでもuv導入後のコマンドは同じです。pyproject.tomlで直接依存の範囲、uv.lockで全依存の確定バージョンを管理します。通常は `--locked` を使ってください。Phase 1はAPIキー不要で、.envを読みません。.env.exampleは将来用の注意書きです。

## 実行例

```powershell
uv run trading-bot fetch --market JP --interval 1d
uv run trading-bot fetch --market US --interval 1h
uv run trading-bot fetch --market ALL --interval 1h
uv run trading-bot update --market ALL
uv run trading-bot status
```

少量の確認には `fetch --market JP --symbol 7203.T --interval 1h` を使えます。
日足は730日、時間足は30日が初期値です。足の省略時は設定中の全足を取得します。updateは既存履歴の末尾5本を再取得して統合し、未保存なら初期履歴を取得します。
終了コードは成功0、取得の一部/全失敗1、設定/引数エラー2。失敗した銘柄・足は `FAILED` に表示し、他銘柄は継続します。
statusは銘柄・市場・足・保存件数・最古/最新のUTC日時を表示します。

`config/settings.yaml` で提供元、足、期間、保存先、タイムアウト、再試行数、要求間隔、ログレベルを設定できます。保存先の相対パスは設定ファイルの親の親が基準です。設定指定はサブコマンドより前です。

```powershell
uv run trading-bot --settings config/settings.yaml --universe config/universe.yaml status
```

`config/universe.yaml` は日本・米国各10銘柄です。追加はYAMLのみで可能です。日本株は7203.T、米国株はAAPL、BerkshireはBRK-BのYahoo形式です。
Parquetは `data/market/market=JP/symbol=7203.T/interval=1h/bars.parquet` などに保存します。実データや秘密情報はGit管理から除外しています。

## テスト

```powershell
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest
```

自動テストは架空のDataFrameとモックのみで動き、株価APIに接続しません。実データ確認はCLIで別途行います。

## GitHub Actions

push / pull_request時のCIでWindows・Ubuntuの両方を検査します。
デフォルトブランチへの反映後、GitHubのActions → Fetch market data → Run workflowでmarketとintervalを選びます。手動実行のみです。
結果は実行サマリー、Parquetとログは3日間のartifactです。一部失敗でも取得成功分はartifactに残しますが、ワークフローは失敗表示になります。
データはGitへpushせず、次の実行にも自動継承しません。無料枠の条件は利用アカウントで確認してください。

## 制限と今後

Yahoo Finance/yfinanceにはレート制限、期間制限、データ遅延・欠損があり、可用性は保証されません。データの利用・公開・再配布の権利はソフトウェアのライセンスとは別です。
[yfinance実装](https://github.com/ranaroussi/yfinance/blob/main/yfinance/scrapers/history.py)のAPIで未調整OHLCVを要求します。時間足要求は保守的に最大60日、既定30日に制限します。
返却バーを再サンプリングしません。時間足は開始1時間後、日足は現地翌日まで待つ保守的な確定判定です。取引所カレンダーによる欠損検査、配当・分割補正、長期停止後の古い欠損の自動補完は今後の課題です。
強制終了後に `.write.lock` が残る場合は、実行中の書込みがないことを確認してから手動除去してください。

Phase 2でPaperBroker・ポートフォリオ・為替管理・GitHub Pagesを実装しました。今後はGemini判断・オルカン実データ比較・Supabase永続化を検討します。[設計文書](docs/architecture.md)を参照してください。

ソフトウェアは [MIT License](LICENSE) です。価格データの権利は含みません。

## Phase 1総合検証

通常のpytestはネットワークを遮断して実行します。総合検証結果と修正内容は [検証報告](docs/phase1-test-report.md) に記録しています。
実ネットワークを使う任意の検証は次のコマンドです（CIからは呼びません）。

```powershell
uv run python scripts/verify_live.py
```

対象は7203.T、6758.T、AAPL、MSFTです。5つのCLI操作を一時保存先で実行し、既存の市場データには書き込みません。検証用Parquetは終了時に削除し、件数・期間のJSONとCLIログはGit管理外の `data/phase1-audit/` に残します。

## Phase 2：仮想口座・売買エンジン

初期口座はJPY 1,000,000 / USD 0。現物のみで、入金・空売り・レバレッジはありません。日本株100株、米国株1株が既定単位です。`config/paper.yaml` の `lot_sizes` で銘柄ごとに変更できます。
株価によっては100株で10%上限を超え、トヨタなどの注文が拒否されます。これは正常なリスク制限です。

```powershell
uv run trading-bot paper init
uv run trading-bot paper balance
uv run trading-bot paper positions
uv run trading-bot paper orders
uv run trading-bot paper history
uv run trading-bot paper buy --symbol 7203.T --quantity 100 --order-id example-buy
uv run trading-bot paper cancel --order-id example-buy
uv run trading-bot paper sell --symbol 7203.T --quantity 100
```

buy/sellは注文を受け付けるだけです。価格未取得やリスク判定前の注文はPENDINGです。保有がない売却、単位違反はREJECTEDです。再度initしても残高をリセット・追加入金しません。
SQLiteの既定保存先は `data/paper/account.sqlite3`。別口座の検証には `paper --db data/demo/account.sqlite3 init` のように、paperの直後に保存先を指定します。複数口座間の送金はありません。実行中のDBをコピーせず、SQLiteのバックアップAPI等を使用してください。

### 明示的な円・ドル交換

下記は**固定レートを使ったシミュレーション**です。市場の実レートではありません。現在の口座より前の時刻を指定すると拒否します。

```powershell
$at = (Get-Date).ToUniversalTime().ToString('o')
uv run trading-bot paper fx --from-currency JPY --amount 100000 --usd-jpy 150 --rate-at $at --at $at
```

USD→JPYは `--from-currency USD`。既定コストは片道0.25%です。元通貨の指定額を引き落とし、コスト控除後の受取額をJPYは1円、USDは1セント単位で切り捨てます。端数を含む費用は `cost_in_source` に記録します。JPY額は整数、USD額はセント単位で指定してください。為替の自動交換はしません。

### 約定と評価を明示的に実行

```powershell
uv run trading-bot paper execute --order-id YOUR_ORDER_ID --mode paper --at 2026-10-15T14:00:00Z --usd-jpy 150 --rate-at 2026-10-15T14:00:00Z
uv run trading-bot paper value --mode paper --at 2026-10-15T14:00:00Z --usd-jpy 150 --rate-at 2026-10-15T14:00:00Z
```

日時・注文IDは例です。実際の注文より後の評価時刻、利用する固定レートの観測時刻を明示してください。JPYだけの口座では為替指定は不要です。
既存のParquetから既定で1hを使います。`--interval 1d` も可能です。事前にPhase 1のfetch/updateで価格を保存してください。紙の運用で口座状態を更新できるのはローカル環境のみです。

- `--mode historical`: 決定論的な履歴シミュレーション。注文時刻より後の最初の適切なバーの始値を基準にし、そのバーの確定を確認できる時刻まで待って処理します。取得時刻が評価時刻より後でも履歴として使えますが、取得時に未確定だったスナップショットは使いません。
- `--mode paper`: 注文後に取得した、確定済みの後続バーのみ。未来の取得時刻や古い観測値は使いません。yfinanceは実際に執行できる気配値ではなく、約定差・遅延があります。
- timestampはバー開始ラベルです。日足の現地0時ラベルは取引所の寄付時刻に対応させます。NYSE/JPXカレンダーで休日・早引け・昼休み・夏時間を判定し、独自の時間足再サンプリングはしません。
- 足がない、出来高0、未確定、価格/為替が不足・古い場合はPENDINGを維持します。不正データは例外として拒否します。古い終値への強制約定はしません。
- 過去の口座状態を復元するリプレイ機能はありません。履歴モードでは約定・為替イベントを時刻順に処理してください。過去の状態変更や評価への逆行を拒否します。
- 買いは+0.1%、売りは−0.1%の不利なスリッページ。手数料率と固定額を設定できます。代金・手数料の端数は不利方向へ丸め、約定履歴に記録します。
- 約定後のJPY時価で1銘柄10%以下、株式80%以下、現金20%以上を検査します。設定はより厳しくできますが、仕様より緩めることはできません。値動きによる超過は評価履歴とログに残し、自動売却しません。
- 評価に必要な全保有株の確定価格とUSD/JPYが揃わないと、総資産の正常値を出しません。既定の株価最大経過時間は24時間、為替は1時間です。休場をまたいで古くなる場合も安全側に停止するため、条件に応じて設定してください。
- 平均取得単価には買い手数料を含め、部分売却時の残存原価と通貨別確定損益を管理します。株式の損益は銘柄の通貨別、為替費用は交換履歴として分離します。為替損益の詳細な円換算要因分解は今後の対象です。

### 再現可能なデモとテスト

```powershell
uv run python scripts/demo_paper.py
uv run pytest -v
uv run ruff check .
uv run mypy src
```

デモは架空のJP株価300/400円・US株価100ドルと固定USD/JPY=150を時刻付きで表示し、日本株購入・部分売却・為替交換・米国株購入・評価・再読込を検証します。データベースは一時ディレクトリで自動削除し、通常の口座やParquetは変更しません。出力された損益は実績ではありません。

## GitHub Pages / Web

公開URL: https://okina4431.github.io/ai-trading-bot/
React・TypeScript・Vite・Tailwind CSSの案内ページです。初期仮想資金や開発段階を掲載し、架空の実績は表示しません。SQLiteや口座履歴をWebへ公開しません。
Node.js 24を使用し、`web/package-lock.json` をコミットします。

```powershell
cd web
npm ci
npm run dev
npm run build
npx playwright install chromium
npm test
```

`build` は型チェックを含みます。Playwrightで360/390/768/1440pxの表示、横はみ出し、リンク、実行時エラーを検査します。Viteのbaseは `/ai-trading-bot/` です。
GitHubの Settings → Pages → Build and deployment → Sourceを **GitHub Actions** に設定してください。
その後mainへのweb変更のpush、またはActions → Deploy GitHub Pages → Run workflowで公開できます。初回はgithub-pages環境の承認条件も確認してください。CIではWindows・UbuntuのPython検証とUbuntuのWeb検証を実行します。

## Phase 2の制限

SQLiteはローカル運用専用です。Actionsには売買を実行する定期Workflowを設けず、実行間のSQLite永続化を想定しません。
配当・株式分割は未対応です。未調整価格を使った長期シミュレーションの正確な運用成績は保証しません。Gemini判断、スクリーニング、オルカン実データ比較、Supabase、ログイン、PWA、実証券会社APIは対象外です。
取引所カレンダーとデータベンダーのバー形式には差があり得るため、将来のデータ源切替時には再検証が必要です。ライブラリ由来の非推奨警告も現在残っています。

Phase 2の変更ファイル・テスト・CI・公開結果は [Phase 2実装報告](docs/phase2-report.md) に記録しています。
