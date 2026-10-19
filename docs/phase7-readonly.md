# Phase 7：初回読取検証の手順

Alpacaサポート問い合わせ #376491 に関する利用者の確認を根拠に、個人・非商用のIEX日足読取と一時的な内部検証に限って専用経路を準備した。回答本文・担当者情報は保存しない。他の利用者や用途への一般的な許諾を意味しない。

## 承認境界

`config/alpaca-readonly.yaml` の専用許可は有効。ただし、Environment変数の完全一致 `true`、承認入力、main、専用workflow_dispatch、GitHubホストrunnerのすべてが必要。一般の市場データ停止ゲート、外部AI送信禁止は維持する。注文、スクリーニング、Supabase保存、Pages更新は行わない。cronは追加しない。

Codexは実読取Workflowを起動しない。今回のテストはMockTransportによる合成データのみ。

## ユーザーによる設定・初回実行

1. GitHubリポジトリの Settings → Environments → `alpaca-research` を開く。Deployment branchesはmainのみを維持する。
2. **Environment variables** → Add environment variable から、名前 `ALPACA_MARKET_DATA_LIVE_ENABLED`、値 `true`（小文字・空白なし）を登録する。Secretではない。同名のRepository/Organization変数は作成しない（既存なら整理する）。GitHubのvarsには上位スコープへのフォールバックがあるため、Environmentだけで管理する。
3. Environment Secretsの `ALPACA_API_KEY_ID` と `ALPACA_API_SECRET_KEY` がPaper用であることを確認する。値をログや問い合わせへ貼らない。
4. Actions → **Phase 6 Alpaca read-only validation (manual)** → Run workflow → branch `main` を選び、approvalに `READ_ONLY_20_NO_STORAGE` を正確に入力して実行する。Environment承認保護がある場合は内容を確認して承認する。
5. 初回確認後はEnvironment変数を `false` に戻すか削除して通信を再び停止できる。Codexはこれらの設定を変更しない。

## 取得内容と確認項目

固定20銘柄、直近の確定済み221 NYSE営業日を同期間で2回取得する。GET先は `https://data.alpaca.markets/v2/stocks/bars` のみ。`timeframe=1Day`、`feed=iex`、`adjustment=raw`。翌NY暦日00:15以降を確定条件とする。

上限は120リクエスト/分、各取得12リクエスト・4ページ・各要求2回までの再試行、全体24リクエスト。無料Basicの利用可能権限もユーザーの口座で確認する。

成功ログでは `symbol_count=20`、`sessions=221`、`expected_rows=received_rows_per_pass=4420`、`success_rate=1`、`missing_rate=0`、`repeat_revisions=0` を確認する。`requests`、`pages`（JSONとして受信したページ数）、`retries`、`http_status_counts`、`http_response_seconds`、`total_seconds`、観測時刻・対象営業日・確定期限も確認する。失敗時の `last_pass_pages` / `last_pass_retries` は最後の取得だけの値で、`requests` は全体の値。

価格・OHLCV・銘柄別履歴・HTTP本文・認証ヘッダーは出力しない。市場データはプロセスメモリだけで処理し、artifact/cache/DBへ保存しない。ログには集計値だけが残る。

## 失敗時

終了コード1と `STOPPED` は失敗。承認・変数・Secrets不足なら通信前に停止する。401/403ではPaperキーとBasic権限を確認する。429やtimeoutでは実行回数・ステータスを確認し、時間を置いてユーザーが再実行を判断する。Workflowの自動再送はしない。欠損・重複・異常値・未完ページ・再取得不一致・大幅価格不連続は条件を緩めず調査する。エラー本文やSecretを公開しない。

成功しても `strategy_ready=false`、`corporate_actions_verified=false`、`full_market_volume=false`。IEX出来高は市場全体の出来高ではない。企業行動は検証済みと扱わず、スイング戦略へ接続しない。100→500→2,000銘柄への拡張は別レビューで、ユニバース・企業行動・欠損例外・要求予算と品質を検証してから行う。
