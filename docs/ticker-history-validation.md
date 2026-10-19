# MMC/MRSH：期間付きティッカーの独立検証

## 実装範囲

既存20・100銘柄の設定とWorkflowは変更しない。新しい手動Workflowは**1証券・2ティッカーのみ**を調べる。100銘柄全体への証券ID統合や、MRSHの実履歴が正常であることはまだ検証していない。Codexは実通信を起動しない。

`security_identity.py` の内部ID `sec-000001` はティッカーと独立した、このリポジトリで固定管理する普通株のID。提供元・市場・取引所・通貨・ティッカー有効期間 `[開始, 終了)` と、証券IDを分離する。旧MMCは2026年1月14日未満、新MRSHは同日以降。旧期間の開始2025年12月1日は今回レビューした検証窓の下限であり、上場日ではない。それ以前へ勝手に外挿しない。

根拠は[Marshの2025年12月29日公式発表](https://www.marsh.com/en/corp/about/news/marsh-mclennan-to-change-nyse-symbol-to-mrsh.html)。同発表は2026年1月14日の変更、NYSE上場・CUSIPの継続を説明する。CUSIP番号そのものを推定・登録していない。根拠・適用日・手動確認済み状態をコードで管理し、未確認の同一性を受け入れない。提供元の実際の履歴対応を保証する情報ではない。

期間の重複・欠落、未知のID、曖昧なティッカー割当て、未確認の同一性、対応する変更イベントの欠落は拒否する。raw PriceBarを改名・調整しない。`IdentifiedObservation` は内部IDと元のPriceBar参照を保持し、観測ティッカー・出所・取得時刻・時刻・OHLCVを残す。

ティッカー変更・split・dividend・spinoffは別イベント型。今回処理できるのは確認済みティッカー変更だけ。他の登録イベントが期間内にあれば停止する。未登録企業行動を網羅的に検出するAPIは追加していない。35%超のraw終値不連続でも停止し、配当・分割調整を代用しない。

## 履歴検証

固定GET `/v2/stocks/bars` に `symbols=MMC,MRSH`、1Day/IEX/raw/USD/asof=- を指定し、直近221確定NYSE営業日を2回取得する。全期間を両別名で問い合わせ、APIの返却を推測して片方を補完しない。

別名単位では221行を要求しない（正しい有効期間の外にはバーがない想定）。その代わり、全raw行を期間・提供元・市場・通貨で一意な証券へ対応付け、**証券ID×想定営業日がちょうど1行**になることを要求する。これは欠損の許容ではなく、ティッカー変更を表現する別の厳密な検証である。

旧新の両方に同日バーがある、期間外へバーが返る、改名済み履歴が両方へ複製される、未知の別名・別証券が返る、1日でも不足する場合は停止する。同値バーであっても黙って重複排除しない。2回の観測は取得時刻だけを除き、内部ID・元のティッカー・全raw項目を比較する。成功時の想定は1証券×221行／取得。成功しても、未登録企業行動なし・戦略投入可能とは判定しない。

120req/min、HTTP timeout20秒、4ページ・12試行／取得、各要求2回までの再試行、全体24試行・経過時間300秒ガード、Workflow10分を維持する。経過時間ガードはHTTPの前後で判定し、通信中の強制中断はHTTP/runner timeoutで制限する。既存の読取Workflowとconcurrencyを共有する。

## ユーザーによる次の手動操作

1. mainのCI成功と今回の変更範囲を確認する。
2. GitHub Settings → Environments → `alpaca-research` → **Environment variables** に、新たに `ALPACA_TICKER_HISTORY_LIVE_ENABLED=true` を設定する。Codexは設定しない。既存の `ALPACA_MARKET_DATA_LIVE_ENABLED=true` とPaperキー2個も必要。同名のRepository/Organization変数は作らない。
3. Actions → **Alpaca MMC-MRSH identity history (manual)** → Run workflow → main → **`READ_ONLY_MMC_MRSH_NO_STORAGE`** を入力する。既存20・100銘柄の承認文字列では拒否する。
4. 成功ログの `security_count=1`、`alias_count=2`、`sessions=221`、`received_rows_per_pass=221`、`complete=true`、HTTP件数・ページ・再試行を確認する。rawデータや別名別履歴は出さない。
5. 失敗は `STOPPED` と `complete=false`。権限・HTTP状態と静的な失敗分類を確認し、レスポンス本文やキーを共有しない。欠損補完・asof変更・別名入替で通さない。必要なら別途集計診断をレビューする。再試行はユーザーが判断する。
6. 確認後は新しいEnvironment変数をfalseへ戻すか削除して停止できる。

新経路は既存100銘柄Workflowを成功させる変更ではない。まずMRSH配信と境界を実測し、必要なら別レビューで残り99銘柄の証券ID登録と100証券全体の検証へ進む。新しい承認は他の取得経路・DB・Gemini・売買を許可しない。

市場データはプロセスメモリのみ。ログは集計だけ、artifact/cache/DB/Pagesへの保存なし。生データを含む失敗例外も出さない。既存Supabase・paper-live・API履歴、実Gemini、注文、cronは変更しない。

失敗時の `failed_stage` は固定の分類だけを出す。`AUTHORIZATION` は承認・変数・キー等の初期条件、`REGISTRY` は期間定義、`FETCH` はHTTP・ページ・raw検証、`IDENTITY` は期間対応・重複・欠損、`REPEAT_COMPARISON` は再取得差分、`DISCONTINUITY` は価格不連続、`OBSERVATION_TIME` は観測時刻または最終時間制限の段階。例外本文や個別の価格を表示するものではない。
