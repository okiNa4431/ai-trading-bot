# AI vs オルカン — AI Trading Bot

Geminiが日本株・米国株を仮想売買するポートフォリオと、オルカンへの一括投資の成績比較を最終目標とします。Phase 1はデータ取得・検証・保存・CLI・テスト・GitHub Actionsの基盤です。売買やAI連携はまだ実装しません。

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

Phase 2以降にGemini、PaperBroker、ポートフォリオ・為替・オルカン比較、Supabase永続化、GitHub Pagesを検討します。[設計文書](docs/architecture.md)を参照してください。

ソフトウェアは [MIT License](LICENSE) です。価格データの権利は含みません。

## Phase 1総合検証

通常のpytestはネットワークを遮断して実行します。総合検証結果と修正内容は [検証報告](docs/phase1-test-report.md) に記録しています。
実ネットワークを使う任意の検証は次のコマンドです（CIからは呼びません）。

```powershell
uv run python scripts/verify_live.py
```

対象は7203.T、6758.T、AAPL、MSFTです。5つのCLI操作を一時保存先で実行し、既存の市場データには書き込みません。検証用Parquetは終了時に削除し、件数・期間のJSONとCLIログはGit管理外の `data/phase1-audit/` に残します。
