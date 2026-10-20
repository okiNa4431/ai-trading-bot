# 読取品質診断と戦略承認の分離

## 設計判断

「バーを安全に観測できるか」と「同一証券の投資判断に使えるか」は別の承認で扱う。既存Phase 8.5の `require_reviewed`、証券ID登録簿、企業行動停止、既存Workflowは維持する。新経路はUNKNOWNを許容した**ティッカー単位の品質調査専用**であり、未確認の別名を証券IDに解決・結合しない。登録簿の状態・根拠・確認日は一切書き換えない。

fbeefceのCIは全4ジョブ成功を確認した。新経路では同じ登録簿について、厳格な経路は通信前停止し、診断経路だけが別の明示承認でraw取得できることを合成テストで確認する。

## 未確認でも読取診断に限って許容する項目

| 項目 | 診断での扱い | 戦略への扱い |
|---|---|---|
| 単一別名の証券同一性・取引所 | UNKNOWNのまま、そのティッカーを観測する | 不許可 |
| 単一別名の継続上場・全期間履歴 | 221日を調べ、欠損は補完せず停止 | 不許可 |
| 企業行動の期間レビュー | UNKNOWNのまま取得し、既知イベント件数も明示 | 不許可 |
| 登録済みの未対応併合・配当・分社化 | rawを調整せず観測する。35%超不連続など品質違反は停止 | 不許可 |
| 過去レビューが現在の窓をカバーしない | 確認状態を延長せず、診断のみに限定 | 不許可 |

単独の未確認別名の暫定期間を、上場期間の証拠として扱わない。全要求窓の観測を行う。未確認の有限境界や、窓内で始まる暫定別名がある場合は曖昧なので通信前停止する。

MMC/MRSHは、公表済み変更日と確認済みの別名関係について既存 `SecurityRegistry` の構造検証を使う。既知の期間外へ返ったバーは拒否する。ただしこの診断では、二つの価格系列を証券IDへ結合しない。過去の確認を未来の証券同一性・企業行動クリアランスへ拡張した意味ではない。

## 引き続き停止する条件

承認・Environment変数・main・専用workflow_dispatch・GitHub-hosted runner・Paperキーの不足。未確認の複数別名対応、矛盾・重複・欠落した期間、未確認の有限境界、未知のティッカー、要求窓外・既知の有効期間外、重複、欠損、不正OHLCV、未確定足、ページ不整合、上限超過、再取得差分、35%超のティッカー内終値不連続で停止する。

企業行動を理由に価格を調整したり、価格不連続を無視したりしない。既知イベントを理由に期待バーを削除しない。生の取引がない日はその事実を欠損として報告し、架空のバーを作らない。出来高0だけでは無効にしない。

## 型と実行境界

`run` が外へ返すのは凍結した `ReadOnlyQualityReport` だけ。rawバー・DailyBatch・証券系列は関数内のメモリに閉じ込め、reportには集計JSONだけを入れる。バーへの変換メソッド、スクリーン入力、注文入力は提供しない。

`purpose=diagnostic_only`、`strategy_approved=false` を常に付ける。Gemini判断、ObservedDailyData、PaperBrokerの注文受付・約定・資産評価、PrivateResearchStore保存の各入口は、report本体とシリアライズしたpurpose付き集計を副作用前に拒否する。既存の市場由来データの外部AI送信禁止も維持する。集計だけを得たことは戦略承認ではなく、UNKNOWNを確認済みに昇格する処理もない。

一般の市場データ取得停止ゲートは変更しない。既存の厳格な100証券経路は依然として証拠不足で停止する。既存の20・100ティッカー・MMC/MRSH・Phase 8.5 Workflowも変更しない。

## 100証券サンプルの手動診断

1. CI成功と診断・戦略の違いを確認する。
2. Environment `alpaca-research` の**Environment variables**へ、ユーザーが `ALPACA_100_DIAGNOSTIC_LIVE_ENABLED=true` を登録する。既存の `ALPACA_MARKET_DATA_LIVE_ENABLED=true` とPaperキー2個も必要。同名のRepository/Organization変数は作らない。厳格な経路用の `ALPACA_100_IDENTITIES_LIVE_ENABLED` を代用しない。
3. **Alpaca 100-security alias quality diagnostic (manual)** をmainから `READ_ONLY_100_DIAGNOSTIC_NO_STORAGE` で起動する。Codexは実通信を起動しない。
4. 成功時は `status=DIAGNOSTIC_PASSED`、`diagnostic_complete=true`、`strategy_approved=false`。`requested_security_count=100` はサンプルの規模であり、100証券の同一性を確認したという意味ではない。現在の既知境界では101別名の期待バー合計は22,100／回。これは別名スコープの品質であり、証券ID単位の完全性ではない。
5. `identity_confirmed`・`corporate_actions_confirmed` 等は変更せず集計する。`known_non_ticker_events`・`known_ticker_events` は登録済み公表イベントの件数で、網羅的な調査完了を意味しない。`checks.security_completeness`、`checks.corporate_action_clearance`、`checks.strategy_approval` はNOT_RUNのまま。
6. 失敗はSTOPPED・終了コード1。`checks.scope`、2回分のpaging/alias_quality、repeat_consistencyで停止段階を確認する。未算定件数はnull。欠損・重複・異常・不連続は検査できた範囲の集計であり、途中停止なら全データの総数は保証しない。
7. 実行後は必要に応じ新変数をfalseへ戻す。再実行はユーザーが判断し、自動再送はしない。

GETのみ、固定 `/v2/stocks/bars`、1Day/IEX/raw/USD/asof=-、直近221確定NYSE営業日、同条件2回取得を維持。100＋1別名チャンク、通常4ページ／回、上限4＋2ページ／回、12試行／回・24試行／全体、120req/min、共有300秒・HTTP20秒・Workflow10分を再利用する。既存読取Workflowとconcurrencyを共有する。

APIキー、ヘッダー、レスポンス本文、OHLCV、個別価格履歴をログに出さない。ログは集計のみ。市場データをartifact/cache/DB/Pages/Geminiへ出力せず、仮想売買へ接続しない。Supabase、paper-live、API履歴、cronを変更しない。

## 企業行動調査の自動化設計

今回追加するのはofflineの `CorporateActionCandidate` 契約と検証のみ。liveの企業行動取得・定期実行は追加しない。将来の手動調査経路は次の流れとする。

1. 利用条件とアクセス条件を確認した会社IR・取引所公表・公的提出資料、または承認済み提供元のアダプターが根拠を収集する。現行barsホストの許可を別endpointの許可へ流用しない。
2. 分割、配当、分社化、ティッカー変更を別kindに正規化する。提供元、根拠参照、発表時刻、観測時刻、有効開始・終了日、ティッカー／証券IDの「候補」を保持する。タイムゾーンなし時刻や矛盾した期間は拒否する。
3. rawの価格変化だけでは原因を特定できないのでkind=unknownのシグナルに留める。配当落ちや分割と自動断定しない。候補は常にreview_status=UNKNOWNで、CONFIRMEDを設定できない。
4. 複数の根拠・版・修正を照合し、人が同一性と適用日をレビューする。分割比率、配当通貨・権利落ち日、分社化の配分、旧新証券の関係はイベント別に検証する。承認済み登録簿への昇格は別のレビュー操作にする。
5. 公表済み時刻と観測時刻を区別して先読みを防ぐ。現在のティッカーを過去ユニバースへ遡及適用しない。将来の保存やlive APIは別承認が必要。

## 2,000銘柄への課題

証券・上場・株式クラスの個別証拠、別名再利用、企業行動アダプターの利用条件・取得遅延・改訂対応、API要求とページの実測予算、共有レート、メモリ、比較・期間検索の索引化、診断から戦略への承認経路が必要。現在の100/101固定スコープを環境変数だけで拡大しない。診断成功だけで自動運用を有効にしない。
