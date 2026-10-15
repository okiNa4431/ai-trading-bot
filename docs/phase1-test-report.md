# Phase 1 総合検証・修正報告

実施日: 2026-10-15（日本時間）。Windows / Python 3.12.15。

## A. 結果

| 項目 | 結果 | 備考 |
| --- | --- | --- |
| uv sync | PASS | 45パッケージ、uv.lock変更なし |
| uv lock --check | PASS | 設定とlockが整合 |
| pytest -v | PASS | 69件。開始時25件、追加44件 |
| Ruff | PASS | check / format --check |
| mypy src | PASS | 7モジュール |
| 日本株の実取得 | PASS | 7203.T・6758.T、日足と時間足 |
| 米国株の実取得 | PASS | AAPL・MSFT、日足と時間足 |
| Parquet保存 | PASS | 初回保存、読込一致、重複防止、既存行の訂正 |
| 差分更新 | PASS | 過去timestampを保持し、再取得で重複なし |
| 異常データ検出 | PASS | 欠損、NaN/Inf、負値、不正OHLC、時刻、重複、銘柄、空データ |
| CLI | PASS | 指定5操作を実通信で実行。異常終了コードもモックで確認 |
| 障害耐性 | PASS | タイムアウト、429、接続障害、部分失敗、割込み、権限拒否、破損ファイル |
| GitHub Actions CI | PASS | Windows・Ubuntuとも69テスト、Ruff、mypy成功 |
| GitHub Actions手動取得 | PASS | ALL市場の日足・時間足、各20銘柄取得とartifact保存成功 |
| Linux実行 | PASS | GitHub ActionsのUbuntu runnerでCI・実取得成功 |

### 実取得結果（UTC）

下表は取得後のParquetを読み直して確認した件数と期間です。市場が閉まっているために最新timestampが当日でないことは失敗条件にしていません。

| 銘柄 | 足 | 件数 | 最初 | 最後 |
| --- | --- | ---: | --- | --- |
| 7203.T | 1d | 486 | 2024-10-14 15:00 | 2026-10-13 15:00 |
| 7203.T | 1h | 126 | 2026-09-16 00:00 | 2026-10-15 06:00 |
| 6758.T | 1d | 486 | 2024-10-14 15:00 | 2026-10-13 15:00 |
| 6758.T | 1h | 126 | 2026-09-16 00:00 | 2026-10-15 06:00 |
| AAPL | 1d | 501 | 2024-10-15 04:00 | 2026-10-14 04:00 |
| AAPL | 1h | 154 | 2026-09-15 13:30 | 2026-10-14 19:30 |
| MSFT | 1d | 501 | 2024-10-15 04:00 | 2026-10-14 04:00 |
| MSFT | 1h | 154 | 2026-09-15 13:30 | 2026-10-14 19:30 |

合計2,534行、8ファイル。OHLCV・UTC・有限値・整列・主キー重複・未調整フラグ・取得元・時間足の確定判定を検査しました。
日本の昼休みと米国夏時間の時刻保持も架空データの回帰テストで検証しています。

### 実行方法と記録

`uv sync`、`uv lock --check`、`uv run pytest -v`、`uv run ruff check .`、
`uv run ruff format --check .`、`uv run mypy src` を実際に実行しました。
ローカルのuvは `.tools/bin/uv.exe`、Pythonは `.runtime/` にあり、Git管理対象外です。

実通信は `uv run python scripts/verify_live.py` 相当で実施しました。
一時的な4銘柄のuniverseと保存先を作り、次のCLI引数を実行しています。

```text
fetch --market JP --interval 1d
fetch --market US --interval 1h
fetch --market ALL --interval 1h
update --market ALL
status
```

すべて終了コード0。通常のconfig/universe.yamlや運用データには書き込んでいません。
テスト用Parquetは自動削除。CLIログ5本、results.json、最終検査ログ6本、checks.json、
GitHub Workflow数確認結果はGit管理外の `data/phase1-audit/` に残しています。
自動テストはYahooの入口とPythonソケットを遮断し、実通信テストとは分離しています。

## B. 発見・修正した不具合

開始時の既存25テストは成功しました。総合テスト追加後に9件の失敗を再現し、以下を修正しました。検証条件の緩和や不正値の補正は行っていません。

| 不具合 | 原因・影響範囲 | 修正 | 回帰結果 |
| --- | --- | --- | --- |
| 保存済みメタデータ欠落の見逃し | readがPydantic既定値を使い、adjusted等の欠落を補完。空スキーマも空履歴扱いで更新可能 | 全PriceBar列の存在と列重複を検査。空の保存済みファイルを拒否し、元ファイルを維持 | バイナリ破損・空スキーマ・adjusted欠落の3ケース成功 |
| 検証を迂回したモデルの保存 | model_copy(update=...)は再検証しないため、不正OHLCVを保存できた | 保存境界でmodel_dumpから再検証 | 負のcloseを拒否し、ファイル非作成を確認 |
| 設定値の表示漏洩 | ValidationError全文にinput_valueが含まれた。YAMLエラーにも原文が含まれ得る | 検証エラーは項目名・種別だけ、YAMLは行・列だけ表示 | ダミー秘密値が出ないことを確認 |
| 検証理由の不足 | ログがValidationErrorという種別のみで、不正なフィールドを特定できない | 入力値を含めずフィールド名とエラー種別を記録。時刻・重複の理由を保持 | open異常の調査可能なログを確認 |
| 保存API自身の失敗ログ不足 | saveは例外を返すだけで、単独利用時にログなし | 市場・銘柄・足・例外種別を記録後に再送出 | 書込み・置換・権限拒否の保存保護とログを確認 |
| 重複足・無限の待ち時間設定を許容 | intervals重複とrequest_interval=Infinityの検査がなかった | 足の一意性と有限値を設定境界で検証 | 両方拒否。通常設定は引き続き成功 |

修正後に該当回帰テスト、続いて全69件・静的解析を再実行し、すべて成功しました。

## C. 今回の変更ファイル

| ファイル | 変更内容 |
| --- | --- |
| src/trading_bot/config.py | 重複足と非有限設定を拒否 |
| src/trading_bot/provider.py | 安全で具体的な検証エラー理由 |
| src/trading_bot/storage.py | 保存境界の再検証、スキーマ検査、保存失敗ログ |
| src/trading_bot/cli.py | 設定/YAMLエラーから入力値を除外 |
| tests/conftest.py | 自動テストのネットワーク遮断 |
| tests/test_integration.py | 総合・回帰テスト44件 |
| scripts/verify_live.py | 一時保存先での実通信CLI検証 |
| README.md | 総合検証と実通信検証の実行方法 |
| docs/architecture.md | 保存境界・メタデータ・エラー表示の契約 |
| docs/phase1-test-report.md | 本報告 |

pyproject.toml、uv.lock、既存テストの検証条件、既存Git設定は変更していません。

## D. 残存課題・環境制約

- 追検証でGitHub上の実行を確認しました。2026-10-15、ユーザーの明示許可を受けてmainへpushしました。
- ローカルWSLは未導入ですが、GitHub ActionsのUbuntuとWindowsの両環境でCIが成功しています。
- yfinance/NumPy由来のDeprecationWarningがありますが、今回の実取得は成功しています。ライブラリ更新時に再検証してください。
- 日足は市場現地の翌日0時、時間足は開始1時間後という保守的な確定判定です。日本の短い最終バーなどで反映が遅れる場合があります。
- 取引所カレンダーによる厳密な欠損判定は未実装。長いギャップは警告します。
- 権限拒否・429・通信障害・中断はモックで意図的に再現しました。実際のACL変更や外部サービスへの過剰リクエストは行っていません。
- 公開対象ファイルにParquet・.env・認証ファイルを含めず、取得データと実行ログはGit管理外です。

## E. 最終判定

**PASS：Phase 2に進める。**

ローカルの全テスト、実通信、保存・更新・異常系は成功し、今回発見した不具合は修正済みです。
GitHub ActionsとLinuxでの実行確認も完了しました。Phase 2の設計・開発に進む土台は整っています。
Phase 2の損益計算には未調整価格の株式分割・配当と為替の扱いを追加する必要があります。


## GitHub Actions追検証（2026-10-15）

対象コードのコミット: `6fa3940006c3f967045536ff554fbf50d7fa7cb0`。

- [CI: Windows / Ubuntu](https://github.com/okiNa4431/ai-trading-bot/actions/runs/38735653371): 両OSで69テスト・Ruff・mypy成功。
- [時間足の手動取得](https://github.com/okiNa4431/ai-trading-bot/actions/runs/38735646791): completed=20 failed=0。
- [日足の手動取得](https://github.com/okiNa4431/ai-trading-bot/actions/runs/38735677136): completed=20 failed=0。
- artifactをダウンロードしてParquetStoreで全ファイルを読込検証。時間足20ファイル・2,800行、日足20ファイル・9,870行。
- artifactは設定どおり3日保持。実データはリポジトリへコミットしていません。
- pushによるCI起動とworkflow_dispatchによる手動起動を確認。pull_requestイベント自体の実起動は未実施です。
- 使用中のActionsにはNode.jsランタイムの非推奨警告がありますが、今回の全ジョブは成功しています。
