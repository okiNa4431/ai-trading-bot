# AI vs オルカン — AI Trading Bot

Phase 8：[100銘柄の読取専用検証手順](docs/phase8-readonly-100.md)。20銘柄と別の手動Workflow・承認を使用します。市場データはメモリだけで処理し、実通信はユーザーが起動します。

Phase 7：[初回20銘柄読取の設定・手順](docs/phase7-readonly.md)。専用コード許可のみ有効化し、Environment変数と手動承認を別途必須にしました。実読取Workflowはユーザーだけが起動します。一般取得・実AI・DB書込・定期実行は引き続き停止中です。

Geminiが日本株・米国株を仮想売買するポートフォリオと、オルカンへの一括投資の成績比較を最終目標とします。Phase 1の市場データ基盤、Phase 2の仮想口座・売買エンジンに加え、Phase 3で定量スクリーニング・Gemini判断・注文計画を実装しました。既定はmock / dry-runで、実資金の注文や定期売買は行いません。現在は仮想運用準備中です。

## Phase 5.5：実市場データ取得は停止中

2026-10-19：[実市場統合・外部AI送信直前までの報告](docs/integration-report.md)と[追加SQLの影響・手順](docs/research-migration.md)を参照してください。観測時刻付きscreening→内部JSON→rule/mock→仮想注文／後続約定→私的CAS保存を接続しました。実Alpaca・本番migration／書込・実AI・cronは未起動。実SDK transportも停止し、合成SDK mockだけを許可します。既存口座と公開Webは変更しません。

Phase 6の[読取専用Workflow・契約確認・初回実行手順](docs/phase6-readonly.md)を準備しました。`alpaca-readonly.yml` はmain・手動・最大20銘柄・一時メモリ処理のみです。アカウント適用契約と初回実行許可が未確認のため、専用設定も `live_enabled: false` のままです。実通信Workflowは今回起動しません。

Alpaca Basicの[接続準備・利用条件・Secrets登録手順](docs/alpaca-preparation.md)を追加しました。日足IEX Providerは実通信ゲートで停止中です。`uv run python scripts/alpaca_mock_probe.py` と手動 `alpaca-mock.yml` は、秘密キー不要のモックHTTPだけで20→100→500→2,000銘柄を検証します。キーの登録は実通信の有効化ではありません。

2026-10-18：初期研究方針をUSスイングへ変更しました。[実装・比較報告](docs/swing-implementation-report.md)を参照してください。2,000銘柄の日次50→15選別、A日次／B条件監視／C毎時を合成データで比較します。Aを基準とし、旧時間足計画は比較・回帰用に維持します。`uv run python scripts/swing_research.py --output data/swing-new-run`で新規隔離研究口座とJSON／Markdownを生成できます。実運用は有効化しません。

初期運用候補はUS 5〜10銘柄・15分取得／1時間AI判断です。[設計・無料枠試算](docs/us-hourly-plan.md)と[提供元への問い合わせ下書き](docs/us-hourly-provider-inquiries.md)を追加しました。新構成はmockでのみ検証します。利用者承認によりBot側のAI上限は8回/日・200回/月へ変更し、実API通信・実データ取得・cronは有効化していません。

個別メールを避ける方針の[公式公開規約による再調査](docs/us-data-public-terms-review.md)を参照してください。通常のAPI利用・内部保存と、無料Gemini側の二次利用・保持・成績公開を分けて判定し、メールを一律の必須手順にはしません。

利用者の指示により、許諾確認・接続の再審査までは実相場の売買cycle、データ取得Workflow、実データを使うGemini試験を拒否します。環境変数やWorkflow入力で解除できません。合成fixture・CI・読取監査は利用できます。以下の過去の取得例は、現段階での起動許可ではありません。

[無料データ提供元比較](docs/phase55-data-sources.md)、[Phase 5.5報告](docs/phase55-report.md)、[手動運用・バックアップ手順](docs/phase55-operations.md)を参照してください。`phase55-validation.yml` のprepareは新規隔離口座を準備するだけで、実市場HOLDや売買の成功ではありません。cron・継続口座の金融操作・未許諾データ公開は無効のままです。

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

## Phase 3：定量スクリーニングとGemini判断

既存の市場データ・仮想口座を再利用し、特徴量→候補選定→構造化AI判断→注文計画を実装しました。既定は**mock + dry-run**です。Geminiは数量決定・為替交換・約定の権限を持ちません。

```powershell
uv sync --locked
uv run trading-bot fetch --market JP --interval 1d
uv run trading-bot fetch --market JP --interval 1h
uv run trading-bot screen --market JP
uv run trading-bot screen --market US
uv run trading-bot paper init
uv run trading-bot ai decide --market JP --dry-run
uv run trading-bot ai decide --market US --dry-run
uv run trading-bot ai history
uv run trading-bot ai usage
```

screenには保存済みの1d・1hが必要です。ai decideには初期化済みの仮想口座が必要です。データ不足や、保有株・USD現金の評価に必要な価格/為替が不足する場合、正常な総資産を捏造せず停止します。`screen` 単体では口座未初期化でも分析できます。
`--at` はUTCオフセット付きの判断時刻を指定できます。その時刻までに確定・取得されていないバーは使いません。後で取得した履歴から過去の観測状態を再構成するバックテスト機能はありません。

### Geminiへの明示的な接続

[Google公式モデル情報](https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash-lite)と[料金表](https://ai.google.dev/gemini-api/docs/pricing)を2026-10-15に確認し、初期モデルを `gemini-3.5-flash-lite`、thinking levelを `minimal` としています。最初の候補 `gemini-2.5-flash` は実接続で404となりました。[公式の利用制限](https://ai.google.dev/gemini-api/docs/deprecations)を確認し、新規プロジェクト向けの無料枠対応モデルへ設定を明示的に変更しました。実行時の自動フォールバックはありません。公式SDK `google-genai` のDeveloper API、構造化JSONを使用します。モデルIDとthinking levelは `config/ai.yaml` で変更できます。

```powershell
uv run trading-bot ai decide --market JP --provider gemini --dry-run --decision-id my-analysis
```

ローカルでは環境変数 `GEMINI_API_KEY` または `.env` を使用します。既存の環境変数を優先し、mockでは.env読込もAPIキーも不要です。GitHub Actionsでは登録済みSecretを利用するので再入力は不要です。キーはソース・設定・出力へ書かないでください。

実APIへの接続と注文登録は独立した許可です。Gemini通信が成功しただけでは注文を登録しません。
保存済みの同じ判断を確認したうえで登録する場合は次のようにします。

```powershell
uv run trading-bot ai decide --market JP --provider gemini --decision-id my-analysis --submit-orders
```

成功済みの同じIDではAIを再呼出しせず、保存済み計画を利用します。口座・未約定注文・ルールが変わった場合やデータが古くなった場合は登録を拒否し、新しい判断を求めます。同一IDの登録は冪等で、注文はPENDINGとなるだけです。約定はPhase 2のpaper executeで別途実行します。`--dry-run` と `--submit-orders` の併用はできません。
モデルや市場・判断時刻を変えて同じIDを流用することはできません。失敗した判断の自動再送もしないため、原因を解決した後に新しいIDで実行してください。

### 指標・スコア・注文数量

詳細な式は [設計文書](docs/architecture.md) に記載しています。SMA5/20、Wilder RSI14/ATR14、日次/時間足リターン、相対出来高、年率換算ボラティリティ、平均売買代金を計算します。
最低21日足・2時間足が必要です。時間足は直近2本が1時間間隔であることを要求し、間隔が不規則な場合に1時間の変化率を捏造しません。再サンプリングは行いません。
不自然な未調整価格の不連続は除外し、指標不能な保有銘柄はHOLD専用の評価対象として残します。保有株の不連続で評価自体が信頼できない場合はAI前で停止します。

設定は `config/strategy.yaml`。6要因の仮説的スコアで各市場上位5〜10銘柄を選び、その市場の保有銘柄を上限外でも追加します。全保有銘柄は市場をまたいでもポートフォリオ評価へ含めます。重みは収益性が実証されたものではありません。銘柄数の拡張はuniverse.yamlだけで可能です。

OrderPlannerは目標の**取引後比率**を株数へ切り下げ、日本株単元、米国整数株、スリッページ・手数料・通貨別現金を検査します。既存PENDING BUYと新しいBUYの予算を累積予約し、未約定SELLの売却代金や自動FXを買付余力に含めません。実際の後続価格による約定時にもPaperBrokerのリスク検査が働きます。
HOLDでは注文せず、未保有SELL、単元未満、予算不足、FX不足などは理由を表示します。

### APIの費用・制限

`config/ai.yaml` の日次・月次・実行単位の上限はローカルの安全上限で、AI Studioのアカウント別無料枠を表す保証値ではありません。実際のアカウントの無料枠・課金設定はAI Studio側で確認してください。同じモデルでも課金済みプロジェクトでは課金が発生し得るため、アプリだけで無料を保証できません。

- SDK内部リトライを無効化し、アプリのリトライも上限付きです。既定はリトライ0・1実行1リクエスト。
- タイムアウトや429もリクエスト予約数に含めます。有料モデルへの自動切替、Vertexへの自動移行、課金設定変更はしません。
- 入力は生OHLCVでなく特徴量です。UTF-8バイト数＋システム指示・スキーマ＋余裕を保守的なトークン相当上限として検査します。追加のcount_tokens通信は行いません。
- 出力トークン・出力バイト数・タイムアウトも制限します。入力/出力/思考/総トークンはSDKから返された数値を記録し、取得できない値はnullです。
- JSON・未知/重複銘柄・無効アクション・範囲外/矛盾する目標比率を拒否します。不正出力を再整形して強引に注文にはしません。
- 判断・入力ハッシュ/スナップショット・応答・計画・登録結果・エラー・使用量は同じローカルSQLiteの別テーブルへ保存します。秘密値をエラーメッセージや保存応答へ出さないようにします。

### GitHub Actions：手動接続テスト

Actions → **Test Gemini decision** → **Run workflow**。
`market=JP/US`、`mode=mock/gemini` を選びます。既定はJP/mockです。最初にmockを成功させてからgeminiを選んでください。
保護されたmainの確定コミットだけをcheckoutし、実APIステップだけに `GEMINI_API_KEY` Secretを渡します。通常CI・mockへSecretを渡しません。push / pull_request / cronでGeminiを呼びません。

手動テストは対象市場の2銘柄、90日の日足・7日の時間足をPhase 1で取得し、一時口座100万円で分析します。**最大1回の実API呼出し、dry-run固定、注文登録・約定禁止**です。結果はActions Summaryに候補数、判断、BUY/SELL/HOLD件数、注文計画/見送り理由、トークン、状態を表示します。Parquet・SQLite・秘密情報のartifactは作りません。
ActionsのDBは使い捨てで、日次/月次カウンターも実行間では継承されません。永続した利用上限と口座状態は将来のSupabase導入後に扱います。今回のActionsでは1実行1呼出しとGoogle側の制限を併用します。継続運用の正本にはしません。

mainが未保護の場合、Secretを扱うWorkflowは実行しません。リポジトリのmain保護設定を有効にしてから使用してください。APIキーを表示・再入力する必要はありません。

Phase 3でも定期売買・実証券会社接続・Supabase・オルカン実比較は行いません。公開Webは運用準備中の案内だけで、手動テストの出力を継続運用実績として掲載しません。


# Phase 4: Supabase永続化・比較・公開ダッシュボード

Supabaseを継続口座の正本とし、SQLiteは開発・オフラインテスト用として維持します。
定期実行・実証券会社への注文はありません。`persistent-decision.yml` は手動・dry-run固定です。

## 初回migration（SQL Editorで一度だけ）

登録済みの `SUPABASE_URL` / `SUPABASE_SECRET_KEY` / `SUPABASE_PUBLISHABLE_KEY` はData API用で、SQL管理接続の認証情報ではありません。キーの再入力は不要です。
Supabase Dashboardの対象プロジェクト → SQL Editorで、**`supabase/bootstrap.sql` 全体**を一度実行してください。これは `supabase/migrations/` から生成され、単一トランザクションで適用されます。既存口座を削除・リセットしません。

Actionsから適用したい場合だけ、所有者権限のPostgreSQL接続URIを `SUPABASE_DB_URL` Secretへ設定し、`Migrate Supabase` を手動実行します（接続パスワード、pooler/DBホスト、ユーザー、DB名を含むURIが必要）。未設定なら適用せず終了します。Data API SecretをDBパスワードとして流用しません。URIやSecretをログへ表示しません。

## 手動Workflow

1. `Phase 4 persistence smoke` を同じ `test-phase4-smoke` で2回実行。隔離した合成データ専用口座にのみ注文・約定を作り、残高・履歴・使用枠・再接続・二重約定防止・RLSを確認します。本番相当口座は触りません。
2. `Persistent manual decision` を `JP / mock` で実行。`paper-live` がなければ初回100万円を作り、あればそのまま使います。実株価2銘柄を取得し、判断履歴と評価を永続保存します。
3. 同じWorkflowを再実行。口座作成時刻・残高・以前の判断・API使用量が維持されます。`gemini` の選択時だけGemini Secretを使い、最大1通信・リトライ0。予約はSupabaseの全口座共通枠に先に保存します。
4. 公開専用テーブルへ安全な派生情報だけを反映し、Pagesから読みます。公開APIは読み取りだけです。Secretを渡さないmockと通常CIは実Geminiを呼びません。

実APIの予約はUTC暦日・暦月単位。エラー・中断予約も消費したままで、自動返金しません。テスト口座の予約も同じプロジェクトの共有枠へ含めます。テスト口座削除で予約が消えて枠が戻るのを防ぐため、API予約が残るテスト口座は削除を拒否します。

## CLI

引数は各操作の後に指定します。Supabase利用時は.envまたは環境変数を読みます。SQLite利用時は従来のDBを破壊しません。

```text
uv run trading-bot db status --backend supabase
uv run trading-bot db initialize --backend supabase
uv run trading-bot db verify --backend supabase
uv run trading-bot benchmark update --backend supabase --csv /path/to/official-nav.csv
uv run trading-bot benchmark initialize --backend supabase --accepted-on YYYY-MM-DD --purchase-date YYYY-MM-DD --application-calendar /private/reviewed-calendar.json
uv run trading-bot benchmark status --backend supabase
uv run trading-bot portfolio snapshot --backend supabase
uv run trading-bot portfolio performance --backend supabase
uv run trading-bot dashboard publish --backend supabase
uv run trading-bot operate --backend supabase --market JP --provider mock
```

status / verify / benchmark status / portfolio performanceは読み取り専用。initialize / update / snapshot / publish / operateは書き込みです。`db initialize` は初回のみ資金を作り、初期化済み口座を変更しません。operateは保存済み市場データを使い、注文を登録しません。Actions版は先に実データを取得します。

隔離する場合は `--account test-my-check --test-account` を両方指定します。通常口座の削除操作はありません。テスト口座だけ `db delete-test --backend supabase --account test-my-check --test-account --confirm-account test-my-check` で削除できます（使用枠履歴がある場合は監査のため保存）。SQLiteは `--backend sqlite --db data/paper/dev.sqlite3` のパスごとに分離します。モデル内の従来の `account_id=paper` は維持し、Supabaseの外部スコープキーで口座を分離します。

## オルカンの実基準価額

対象はeMAXIS Slim 全世界株式（オール・カントリー）、運用会社コード253425です。ETF2559や指数を代替しません。
[運用会社の商品ページ](https://www.am.mufg.jp/fund/253425.html)の「設定来データ」から、公式のCP932 CSVを取得できます。2026-10-17の通常HTTP取得は成功しました（調査用Web経路では403）。認証回避は行いません。[サイト利用条件](https://www.am.mufg.jp/use.html)第7項は無断使用・複製・改変を禁止しており、継続自動取得・クラウド保存・派生値の公衆公開を包括的に許す記載は確認できません。自動取得・定期取得は有効にせず、所有者の非公開ローカル検算に限定しました。商用利用・共有・Supabaseへの実NAV転送は別途条件を確認してください。

公式ダウンロードファイルを変換せずに取り込む場合：

```text
uv run trading-bot benchmark update --csv data/benchmark/253425.csv --csv-format mufg --db data/benchmark/private.sqlite3
uv run trading-bot benchmark compare --csv data/benchmark/253425.csv --csv-format mufg --started-at 2026-10-05T00:00:00+00:00 --accepted-on 2026-10-05 --purchase-date 2026-10-06 --valuation-date 2026-10-16 --application-calendar data/benchmark/reviewed-calendar.json
```

`compare` は実CSVによる**回顧的な現金保有シナリオとの計算検証**で、継続口座・AI成績・スナップショットを書き換えません。表示結果も非公開情報です。初期資金双方100万円、整数口、端数現金、購入手数料0・税引前で計算します。全日付を検証し、分配金・欠落日があれば拒否します。CIのCSVはすべて合成値で、実データは `data/` 配下のみです。

CSVには実際の公表時刻がないため `publication_time_basis=observed_by` とし、`published_at=fetched_at=取込時刻` を保守的な可用時刻として保存します。これは実公表時刻ではありません。[通常18時30分頃という案内](https://faq1.am.mufg.jp/answer/660f62c3fe5ca95281bcf0db/)を各日の実公表時刻に置き換えません。原本SHA-256・公式URL・基準日・1万口単位・生NAVを保存し、再投資NAVを代用しません。再取込では既存の初回観測を維持し、値の訂正は拒否します。

`reviewed-calendar.json` は所有者が公式商品ページの「お申込み不可日」、最新目論見書、販売会社の締切を照合して作成します。形式は `valid_from` / `valid_to`（ISO日付）、`closed_dates`（海外・販売会社の申込不可日）、`shortened_dates`（11:30締切日、通常は空配列）、`cutoff_jst`（通常 `15:30:00`、販売会社が早ければその時刻）、`source_url`（公式URL）、`observed_at`（タイムゾーン付き確認時刻）。日本の休業日は別途検証します。カレンダーの範囲不足・締切後・海外休業日を拒否し、最初に申込可能な日の翌国内営業日を適用NAV日とします。JPX売買停止を投信休業日と混同しません。

継続口座は2026-10-16 23:36 JST開始です。公式カレンダー（2026-10-16基準）では10月19日も香港休業で申込不可のため、原則締切に従う仮想比較では10月20日受付・10月21日NAVが最初の候補です。販売会社の条件を再確認し、該当する実NAVが公表・取得されるまで購入を初期化しません。過去日での遡及購入は行いません。

従来の正規化CSV形式はUTF-8、次のヘッダーを指定します。数値のサンプルはテスト専用であり、実際の基準価額ではありません。実データをコード・Gitへ含めません。

```text
fund_id,nav_date,nav_jpy,published_at,source_url,distribution_jpy,derivative_publication_allowed
```

- fund_idは253425、nav_jpyは**1万口あたりJPY**。published_atは確認した公表時刻をタイムゾーン付きで指定し、fetched_atは取込時に記録します。
- source_urlは運用会社のHTTPS正式URL。HTMLスクレイピングはしません。日付・数値・公表時刻・重複を検証し、ファイル全体が正常な場合だけ取り込みます。
- derivative_publication_allowedは明示的な `true` / `false`。公開権限を確認できていなければfalse。falseでは比較派生値もWebへ出しません。単にフラグを変えても権利を取得したことにはなりません。
- 初回購入にはレビュー済みの申込カレンダー・受付日・適用NAV日を指定します。原則15:30締切と海外休場に加え、販売会社の異なる締切も確認してください。該当NAVがなければ前日の価格で購入したことにしません。
- 整数口数=floor(100万円×10000÷適用基準価額)、未投資端数は現金。信託報酬は公表NAVに反映済みとして二重控除せず、購入手数料0・税引前・追加投資なし・売却なしです。
- 分配金がある場合は専用台帳実装前に比較を停止します。データ訂正は黙って上書きせず拒否し、監査付き訂正操作を今後追加します。
- 比較は同じJST評価日の、公表・取得とも評価時刻以前のNAVだけを使用します。未更新なら比較値null・不完全と記録します。前回NAVを新しい確定値として保存しません。

## 評価・公開・復旧

スナップショットは口座＋UTC基準時刻で一意、日次表示はJST各日の最後の評価。訂正には理由が必要で、以前の全スナップショットを別レコードへ残します。JPY/USDの確定/含み損益・売買コストは通貨別に表示し、歴史的コストを現在FXで混ぜません。勝率は売却約定単位（損益>0）の割合で、勝敗のない時はnull。年率リターン・Sharpeは期間/頻度が未検証のため未表示です。

DB内部はbot_privateスキーマ＋RLS＋権限剥奪。service_roleも内部テーブルへ直接書かず、所有者権限の狭いRPCだけ実行します。匿名/認証済み利用者は公開専用 `bot_dashboard` のSELECTだけが可能です。公開テーブルは内部テーブルの所有者権限ビューではありません。

PagesはURLとPublishable Keyだけを環境変数から生成し、Secret Key・Geminiキー・DB URIをビルドへ渡しません。公開設定ファイルはGit対象外。Webは公開テーブルへのGETだけを行い、欠損・通信障害・未運用を区別します。API返却本文の内部エラーは表示しません。

バックアップはDB所有者がSupabase公式のdump手順で取得し、暗号化して非公開保存してください。Freeプランのバックアップ/PITRを前提にしません。復旧は別プロジェクトへスキーマ→内部データ→シーケンスを復元し、`db verify`・注文/約定件数・使用枠・口座作成日時を照合してから接続先を切り替えます。未完了のRUNNING判断やRESERVED枠を勝手に再送・削除しません。通常Workflowに全DBリセットはありません。

無料枠は[Supabase公式](https://supabase.com/docs/guides/platform/billing-on-supabase)のDB500MB、[転送量](https://supabase.com/docs/guides/platform/manage-your-usage/egress)の非キャッシュ5GB/月等（2026-10-16確認）。[GitHub Actions](https://docs.github.com/en/actions/concepts/billing-and-usage)は公開リポジトリの標準ホストrunnerを利用します。ダッシュボードでDB容量・転送量・Googleの実利用量を監視してください。Botの制限は課金0円の保証ではなく、プラン変更・有料モデルへの自動切り替えはありません。

Geminiの推定費用はconfig/ai.yamlの標準有料料金（入力0.30 USD/100万、出力2.50 USD/100万）×SDKの入力・総トークン差からDecimalで計算します。これは無料枠適用後の実請求額ではなく有料換算の参考値です。メタデータ不明ならnull。モデル変更時は公式料金表を確認して単価も変更してください。

Phase 4以降、従来のTest Gemini decisionの実Geminiモードもtest-gemini-smokeという隔離Supabase口座を利用し、共有API枠を経由します。旧SQLiteの使い捨て枠でホスト上の上限を迂回しません。mockは従来どおり通信なしの使い捨てテストです。

# Phase 5：手動・隔離口座限定

追加SQLは [影響範囲](docs/phase5-migration-impact.md)を確認したうえで、SQL Editorで `supabase/phase5-upgrade.sql` を適用します。既存口座をリセットせず、非公開の適用前コピーを作成します。Data APIキーではDDLを実行できません。

**新Workflowはすべてworkflow_dispatchのみ。cronはありません。** `paper-live` の注文・約定・資金移動は新経路でも旧RPCでもDBが拒否します。Phase 5は `test-phase5-*` だけで動作します。

```text
gh workflow run paper-healthcheck.yml -f account=test-phase5-manual
gh workflow run paper-cycle.yml -f mode=cycle -f market=US -f kind=observe
```

通常の `cycle` は実Yahoo株価とUSD/JPYを使い、市場セッション・実行時間帯外ではSKIPPEDです。初期値はobserve / mock / submit=false。判断はセッション終了75分後から30分以内、約定処理は取引時間と確定足取得を考慮します。日米各2銘柄＋保有/未約定銘柄に限定し、原データは公開せず非公開DBの世代管理付きJSONに保存します。大きいユニバースや定期取得を始める前に容量・転送量と利用条件を再確認してください。

`kind=decision` / `submit=true` でtest口座へ仮想注文を登録します。米株用の初回仮想両替が必要な場合のみ、`fund_jpy=100000` を明示します（初期値0）。`kind=execute` / `submit=true` を**別実行**し、注文時刻後に開始した確定バーで約定します。適格バーがなければPENDINGを維持し、必要なバーが欠落していれば停止します。同じslotの再実行や異なる設定での再利用で新規注文を増やしません。

休場日にも行える合成データの統合検証は、`mode=fixture` で明確に区別します。実市場の運用実績ではありません。

```text
gh workflow run paper-cycle.yml -f mode=fixture -f market=JP -f fixture_step=1
```

同じコミットでstepを1→2→3→4→5と、前の成功を確認して別々に起動します。順にBUY登録/PENDING→BUY約定→HOLD→SELL登録/PENDING→SELL約定です。USも同様。初期対象は `test-phase5-fixture-jp` / `test-phase5-fixture-us`。新しい検証系列が必要なら `account=test-phase5-fixture-jp-v2` のような専用口座を明示し、過去の記録は消しません。合成価格・合成FXのみで、Geminiは呼びません。

`paper-reconcile.yml` は初期状態では読取照合だけです。復旧mutationはRepository Variable `PHASE5_RECOVERY_APPROVED=true` と、required reviewersを設定した `phase5-recovery` Environmentを管理者が構成した後に限ります。初期状態では有効化していません。`apply_review=true`、対象run_id、調査理由を指定し、resume=falseで停止、resume=trueで調査済みの試行をSKIPPEDとして閉じて将来の別slotを許可します。古い試行を再送/再約定する操作ではなく、共有API枠も返却しません。

Supabase障害・期限切れ・通信結果不明は別DBへfallbackせず停止。未解決試行は新しいIDでも再実行できません。企業行動は検出して停止する段階であり、分割調整や配当入金の自動会計は行いません。オルカン初回適用NAV待ち・未許諾データの非公開を維持します。
