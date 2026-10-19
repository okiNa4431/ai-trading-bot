# Phase 8.5：100証券の証券ID・履歴統合

## 現在の判定

**仕組みは合成データで検証。出荷登録簿は証拠不足で通信前停止する。100証券の実データ成功・企業行動確認完了を意味しない。** Codexは実Alpaca通信を追加実行していない。既存20・100ティッカー・MMC/MRSH Workflowは維持する。

100個の内部IDと101個の別名を `config/security-identities-100.yaml` に固定する。MMC/MRSHは同じ `sec-000001` を維持。他の99個のIDもファイルに明示し、実行時にティッカーから生成したり、並び順で振り直したりしない。これは現在の研究サンプルであり、過去時点の投資可能ユニバースではない。

登録簿は既存 `TickerAssignment` / `CorporateEvent` を保持するレビュー層。検証済み定義だけを既存 `SecurityRegistry` に渡し、期間の照合・rawの対応付け・重複と欠損の検査は既存 `identify_batch` を再利用する。別の証券解決処理は作らない。取引所はNYSE/NASDAQと未知を表現できるが、未知の取引所は有効な証券登録簿に変換できない。

## 確認状況（2026年10月20日 JST）

| 区分 | 登録上確認済み | 未確認 |
|---|---:|---:|
| 証券同一性 | 1 | 99 |
| ティッカー期間 | 1 | 99 |
| 企業行動の期間レビュー | 0 | 100 |
| 新しい証券IDとの対応を伴う実履歴検証 | 1 | 99 |

各証券に独立した4区分の状態・根拠・確認日・確認対象期間・注記を記録する。確認済みにはHTTPSの根拠URL、確認日、対象期間が必須。実行窓をカバーしない、確認日が観測日（JST）より未来、同一性・期間・企業行動のいずれかが未確認なら停止する。実履歴は今回の検証の出力なので事前成功を要求しないが、未確認状態を明示する。

MMC/MRSHは[会社公式発表](https://www.marsh.com/en/corp/about/news/marsh-mclennan-to-change-nyse-symbol-to-mrsh.html)と[実行39277179081](https://github.com/okiNa4431/ai-trading-bot/actions/runs/39277179081)を参照。成功ログは1証券・2別名、221行／取得、221日、200応答2回、ページ2、再試行0、保存なし。対象窓は実行時刻と当該コミットのカレンダー処理から2025年12月1日～2026年10月16日と算定した。rawデータを保存・転記していない。企業行動監査の成功ではない。

他の99証券は、固定リストに含まれることを同一性の証拠としない。取引所は未確認としてnull、期間開始は暫定研究窓、同一性フラグはfalse。過去の99ティッカー完全取得の集計報告は、新登録簿での個別ID対応・企業行動レビューの代用にしない。未確認を確認済みに変更するには、各証券の会社・取引所資料等の根拠、確認日、確認範囲、必要な別名とイベントをレビューする。

HONについては[会社の2026年6月29日発表](https://www.honeywell.com/us/en/news/press-releases/2026/06/honeywell-technologies-launches-independent-pure-play-automation-company-following-honeywell-aerospace-spin-off)に基づく併合と分社化を別イベントで記録した。企業行動の全期間レビューはUNKNOWNのまま。これらは未対応なので、他の証拠を揃えても期間内イベントとして停止する。配当を含む未登録イベントがないとは仮定していない。レビュー条件を緩めたり、イベントを消して通したりしない。

## 取得・品質条件

新規経路だけ `READ_ONLY_100_IDENTITIES_NO_STORAGE` と `ALPACA_100_IDENTITIES_LIVE_ENABLED=true` を要求する。既存市場データ有効化ゲート、main・専用workflow_dispatch・GitHub-hosted runner・Environment限定も維持する。承認と証拠の検査はSecrets読取・HTTP transport生成より前。一般の市場データゲート、AI、DB、売買は有効化しない。

GET `/v2/stocks/bars`、1Day/IEX/raw/USD/asof=-、直近221確定NYSE営業日。rawの観測ティッカー・出所・OHLCV・時刻は書き換えない。101別名を100＋1へ分割し、終端トークンまで取得後、証券ID×日付が一意に22,100行となることを要求する。別名ごとの221行は要求しない。期間外、未知別名、境界欠損、重複、不正OHLCV、未確定、2回の差分、35%超の連続終値変化で停止する。価格変化は同じ証券の隣接営業日だけを比較する。

[公式API仕様](https://docs.alpaca.markets/us/reference/stockbars)の1ページ上限は全銘柄合計10,000本。通常は最初の100別名で3ページ、残り1別名で1ページ、計4／取得・8／2取得。各チャンクは `ceil(別名数×221/10000)+1` の上限とし、4＋2＝最大6ページ／取得。上限未満のページでも次トークンがあれば継続する。短いページがさらに続けば停止。

既存の保守的な上限を拡大せず、120req/min、各要求2回まで再試行、全チャンク合計12試行／取得・24試行／2取得、HTTP timeout20秒・共有経過時間300秒・Workflow10分を維持。6ページ全てが2回再試行できる予算ではなく、試行予算を先に使い切れば停止する。二つの取得をまたいでリクエスト数・期限を共有する。経過時間ガードはHTTP前後と最終検査で判定し、通信中の制限はHTTP/runner timeoutも併用。既存Workflowと同じconcurrencyグループを使う。

## 集計ログ

`security_count=100`、`alias_count=101`、`sessions=221`、`expected_rows=22100` が固定。成功時は `received_rows_per_pass=22100`、`complete=true`、欠損・重複・異常・再取得差分・不連続が0。

`checks.evidence` と、1回目／2回目の `checks.paging`・`checks.completeness`、`checks.repeat_consistency`・`checks.discontinuity` はPASSED/FAILED/NOT_RUN。未算定件数はnull。出荷状態ならevidence=FAILED、requests=0、後続はNOT_RUN。`*_confirmed` は登録上の確認済み件数、`*_scoped_confirmed` は今回の窓をカバーする件数であり、過去の確認を現在の窓へ自動延長しない。

失敗時の欠損・重複・異常件数は検査できた範囲の値。途中停止で全データの総数は保証しない。要求・ページ・再試行・HTTPステータス・処理時間を集計し、本文・ヘッダー・価格・証券別履歴・生データは出力しない。artifact/cache/DB/Pagesへ市場データを保存しない。

`received_raw_rows` は最後の取得でraw検証を通過した受信行数、`received_rows_per_pass` は証券IDへ対応付けできた行数（失敗時は停止までの件数）。どちらも未受信・未検査の行を含めない。確認日より後の期間を確認済みとして登録することも拒否する。

## 次の手動操作

1. 現時点では実行しても証拠不足で停止する。先に登録簿の99証券の同一性・期間と、100証券の企業行動レビューを進める。証拠の対象期間と現在の221日窓も確認する。未対応イベントの実装は別レビューが必要。
2. レビュー条件が揃った後、Environment `alpaca-research` のEnvironment variablesに `ALPACA_100_IDENTITIES_LIVE_ENABLED=true` をユーザーが登録する。既存の `ALPACA_MARKET_DATA_LIVE_ENABLED=true`、Paperキー2個、main限定も必要。同名のRepository/Organization変数は作らない。Codexは設定しない。
3. 新Workflow **Phase 8.5 Alpaca 100-security identities (manual)** をmainから `READ_ONLY_100_IDENTITIES_NO_STORAGE` で手動起動する。既存3Workflowはそのまま維持する。
4. 集計と検査状態を確認し、失敗を自動再送・成功扱いにしない。必要なら新変数をfalseに戻す。市場データをAIや仮想売買へ接続しない。

## 500→2,000証券で残る課題

個別証拠と企業行動の継続更新、複数上場・株式クラス・再利用ティッカーの識別、提供元との証券対応、未対応配当・分割・分社化、100別名単位の要求予算と実測ページ、実行時間・メモリ、別名・有効期間の索引化が必要。現在の固定100/101検証・24試行上限のまま拡大しない。Supabase migration・実AI・定期実行・paper-liveは今回変更しない。
