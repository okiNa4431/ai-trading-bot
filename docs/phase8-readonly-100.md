# Phase 8：100銘柄の一時読取・品質検証

追記：[欠損診断と検査状態](phase8-missing-diagnostics.md)を追加。未実施の不連続・再取得検査はnullとNOT_RUNで表示する。

## 範囲と根拠

Phase 7の実行 [39264065289](https://github.com/okiNa4431/ai-trading-bot/actions/runs/39264065289) は成功。利用者の報告では20銘柄×221日、各4,420行、欠損・差分・再試行0、200応答2回、約1.94秒。今回の100銘柄はまだ実通信していない。モック結果は実市場での品質・利益の証明ではない。

Alpacaサポート #376491 に関する利用者の確認は、個人・非商用研究、Basic上限内、再配布なしの範囲で扱う。回答本文・担当者情報・キーは保存しない。実通信は利用者が手動で起動する。Supabase保存、スクリーニング、仮想売買、外部AI、Pages公開、cronは追加しない。

## 固定ユニバース

`config/alpaca-readonly-100.yaml` に100銘柄を固定。最初の20銘柄は旧設定と同一・同順。追加は大型の事業会社を中心に、情報技術・通信・一般消費財・生活必需品・金融・医療・資本財・エネルギー・公益などへ分散した研究サンプル。ETF・投信を選定していない。

これは現時点の流動性順位や上場資格をAPI照合したリストではない。全100社の現在の普通株区分、継続上場、221日分のIEX取引、企業行動は未確認。現在の固定銘柄を過去の投資可能ユニバースとはみなさず、生存者バイアスがある。取引停止、改称、分割・分社化等で欠損や不連続が発生し得る。成功のための自動除外・補完・銘柄入替はしない。変更は設定差分をレビューする。

銘柄のサイズ・重複・形式検証は `research_universe.py` に分離した。既存の一般売買用 `config/universe.yaml` は変更しない。500・2,000銘柄をこの承認で取得することはできない。

## ページングと上限

[公式Historical bars仕様](https://docs.alpaca.markets/us/reference/stockbars)を確認。`limit=10000` は銘柄ごとではなくページ全体の本数上限。銘柄→時刻の順に返るため、単一銘柄がページ境界をまたぐ。次ページがある場合は必ず `next_page_token` を追う。ページが上限未満でも完了とは判定しない。

100×221=22,100本なので、上限まで返る通常ケースでは3ページ／取得。短いページへの余裕1ページを含め、**既存と同じ4ページ／取得**を採用した。短いページがさらに続く場合は安全停止し、実測に基づく別レビューが必要。APIが常に上限本数を返す保証はない。

2回取得・各ページ2回まで再試行なので、4×3=12 HTTP試行／取得、合計24試行。20銘柄の上限を5倍にはしない。120req/minのプロセス共通pacer、HTTP timeout20秒、リダイレクト禁止、全取得処理300秒の経過時間ガード、Workflow全体10分を設定。経過時間は各HTTPの前後と最終品質判定で検証し、実行中のHTTPを強制中断するものではない（HTTP timeoutとrunner timeoutも併用）。両読取Workflowは同じconcurrencyグループで重複実行しない。別の手動クライアントによるキー利用量は管理対象外。

GET先、1Day/IEX/raw/USD/asof=-、221確定営業日、2回の同条件比較は維持する。改称前シンボルへの自動マッピングは行わない。日足確定は翌NY暦日00:15以降。35%超の連続終値変化は企業行動の可能性を含めて停止し、原因を断定しない。

## ユーザーの実行手順

1. mainのCI成功を確認する。
2. Environment `alpaca-research` のmain限定、Paperキー2個、Environment変数 `ALPACA_MARKET_DATA_LIVE_ENABLED=true` を確認する。Repository/Organizationに同名変数を作成しない。Codexは設定変更しない。
3. Actions → **Phase 8 Alpaca 100-stock read-only validation (manual)** → Run workflow → `main` → approval **`READ_ONLY_100_NO_STORAGE`** として手動起動する。
4. 20銘柄は従来Workflow・`READ_ONLY_20_NO_STORAGE` を使用する。承認文字列とWorkflow・profileが違えば通信前停止。実行後は必要に応じEnvironment変数をfalseへ戻す。

## 集計ログの見方

成功条件：`symbol_count=successful_symbols=100`、`sessions=221`、`expected_rows=received_rows_per_pass=22100`、`missing_rows=duplicate_rows=invalid_rows=repeat_revisions=discontinuities=0`、`complete=true`。通常ページ数6・要求数6だが、短いページや再試行で増え得る。`pages`、`requests`、`retries`、HTTPステータス件数、処理時間も確認する。

`zero_volume_rows` と `low_iex_volume_rows`（出来高100未満の行数）はIEXの少量取引を把握する診断値で、0出来高だけで無効にはしない。市場全体の出来高ではない。成功しても企業行動確認済み・戦略投入可能とは判定しない。

失敗：終了コード1、`STOPPED`、`complete=false`。`received_rows`、欠損件数・率、`incomplete_symbols`、`empty_symbols`、重複・異常値・差分・不連続件数を確認する。受信・欠損は最後の取得の検証済み行を基準とし、通信途中停止の欠損数は「未受信」も含む。原因をIEXの非取引と断定しない。重複は異常値にも含まれ、異常値件数は停止までに発見した件数で全データの総数ではない。構造不正や通信障害は集計不能な場合がある。要求・ページ・再試行は実行全体の値、last_pass項目は最後の取得のみ。

401/403はキー・権限、429は利用量と待機、その他は集計結果を確認し、ユーザーが再実行を判断する。自動再送、条件緩和、バー補完はしない。本文・キー・価格を貼り付けない。生OHLCV、銘柄別履歴はstdout/artifact/cache/DB/Pagesへ保存しない。既存口座・API履歴も変更しない。

## 次段階の条件

100銘柄の実測ページ数・欠損の分類・再取得整合性を先に確認する。500→2,000では100銘柄単位バッチ、ページ・要求・時間予算、メモリと2回比較の負荷、他処理とのキー利用量、上場資格と企業行動情報を再設計・検証する。企業行動の正規化、実市場由来の外部AI送信、DB保存、取引接続の承認は今回の範囲外。
