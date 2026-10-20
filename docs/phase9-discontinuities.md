# Phase 9：不連続の原因候補診断

## 判断と実装

既存100証券の登録簿・221営業日・101別名・35%超の終値変化閾値は維持する。
従来の品質診断と厳格な証券経路は従来どおり不連続で停止する。
新しい独立Workflowのみ、不連続以外の検証に合格したraw観測をRAMに保持し、同一条件で2回取得する。
不連続があれば、再取得一致でもSTOPPED・終了コード1、diagnostic_complete=false、strategy_approved=false。
未確定足、欠損、重複、異常OHLCV、期間不整合、ページ破損、上限超過では2回目へ進まない。
市場データを調整・補完・保存・AI送信せず、診断結果の下流拒否型と実行ゲートを再利用する。

## 公開情報の調査（2026-10-20確認）

以下は公表イベントの確認であり、今回の4不連続との因果関係の確認ではない。
Alpacaの生データを外部検索に送信していない。現在の集計だけでは発生ティッカー・境界日を特定できず、4件の原因はUNKNOWN。

| 対象 | 種別 | 公表日 | 効力・価格基準日 | 根拠と状態 |
|---|---|---|---|---|
| HON | 分社化（Aerospace） | 2026-06-29完了発表 | 2026-06-29 00:01 NY | [完了発表](https://www.sec.gov/Archives/edgar/data/773840/000077384026000084/exhibit991-pressrelease629.htm)。既存catalogのspinoffを維持 |
| HON | 株式併合1対2 | 2026-06-29完了発表 | 2026-06-29 00:02 NY | [公式併合情報](https://investor.honeywell.com/reverse-stock-split)。既存catalogではsplitとして登録。分社化と別イベントであり、単独の価格比率から判別しない |
| NOW | 株式分割1対5 | 2025-12-05承認開示 | 法的効力12/17、分割後取引12/18 | [SEC提出資料](https://www.sec.gov/Archives/edgar/data/1373715/000137371525000321/now-20251205.htm)、[翌年決算の効力日説明](https://newsroom.servicenow.com/press-releases/details/2026/ServiceNow-Reports-Fourth-Quarter-and-Full-Year-2025-Financial-Results-Board-of-Directors-Authorizes-Additional-5B-for-Share-Repurchase-Program/default.aspx)。診断候補はバー境界の12/18。候補のreview_statusはUNKNOWNのまま |
| NFLX | 株式分割1対10 | 2025-10-30 | 2025-11-14実施、11/17分割後取引 | [公式発表](https://ir.netflix.net/investor-news-and-events/financial-releases/press-release-details/2025/Netflix-Announces-Ten-For-One-Stock-Split/default.aspx)。現行221日窓の開始より前。候補照合コードへ追加しない |
| HON | Solstice分社化 | 2025-10-30完了発表 | 2025-10-30 | [公式完了発表](https://www.honeywell.com/us/en/news/press-releases/2025/10/honeywell-completes-spin-off-of-solstice-advanced-materials)。現行窓外。6月イベントと混同しない |
| MMC/MRSH | ティッカー変更 | 既存登録根拠を維持 | 2026-01-14 | 既存catalog。株式分割・配当ではなく別名境界。別名間の価格系列は結合しない |

配当・特別配当、通常の市場変動、IEX観測の特性も可能性として残る。根拠なしに分類しない。
NOWの公開候補は既存CorporateActionCandidateを再利用し、同一性登録簿や確認済み企業行動レビューを書き換えない。
日付しか確認できない公表情報はpublished_onへ保持し、公表時刻を捏造しない。observed_atは候補構築時刻であり、歴史時点に既知だったことの証明ではない。

## ログの意味

- discontinuities / discontinuity_count：35%超の連続観測ペア数。
- discontinuity_symbol_count：該当ティッカー数。
- upward_discontinuity_count / downward_discontinuity_count：方向別件数。
- additional_same_symbol_discontinuities：各ティッカーの2件目以降の合計。
- known_event_window_matches：後側観測日から±3暦日以内に公表イベント候補がある不連続数。
- published_event_boundary_matches：前側観測日より後、後側観測日以前に候補適用日がある件数。
- unexplained_discontinuities：適用境界の候補がない件数。候補がある場合も因果関係は未確定。
- quality_counts_per_pass：実施できた各回の集計。最上位の件数は最後に検査できた取得回。両回の合算ではない。
- checks：不連続があればalias_qualityはFAILEDのまま。pagingとrepeat_consistencyは独立にPASSED/FAILED/NOT_RUN。未実施を成功と表さない。

同日にsplitとspinoffがあっても一致観測は1件として数える。近傍一致と境界一致は排他的ではない。
一致は機械的な日付整合だけ。株価、比率、ティッカー別発生日、レスポンスは出力しない。
完全な企業行動カタログではないため、unexplainedは「通常市場変動の証明」ではない。

## ユーザーによる手動実行

1. CI成功を確認する。Environment `alpaca-research` に `ALPACA_DISCONTINUITIES_LIVE_ENABLED=true` を追加する。
2. 既存 `ALPACA_MARKET_DATA_LIVE_ENABLED=true`、`ALPACA_100_DIAGNOSTIC_LIVE_ENABLED=true`、Paperキー2個も必要。
3. Actionsの **Alpaca 100-security discontinuity diagnosis (manual)** をmainから起動する。
4. 承認入力は **READ_ONLY_100_DISCONTINUITIES_NO_STORAGE** の完全一致。
5. 不連続を検出すると、再取得一致でも赤いSTOPPEDが正しい。checksと各回の集計を読む。自動再送しない。
6. 診断後は必要に応じ追加変数をfalseへ戻す。

固定GETホスト・endpoint、IEX/raw/1Day/asof=-、取得窓・要求パラメーター、共有concurrency、120req/min・24試行・300秒の上限を維持する。
Secretsは専用実行stepだけ。市場データのログ・artifact・cache・DB・Pages・AI・売買への出力は禁止。
Codexは実APIを起動せず、Supabase、paper-live、cronを変更しない。

## 戦略投入までの残課題

日付整合の集計だけでは4件それぞれを確定できない。必要なら別承認で公開イベント単位の安全な集計を設計する。
提供元のraw価格・企業行動適用仕様、効力時刻、正式な証券同一性、分割比率、配当権利・通貨、分社化配分を根拠と照合する必要がある。
調整系列とraw系列、保有数・原価・配当・新株の会計、改訂履歴を別途実装・テストするまで戦略投入は不許可。
今回、企業行動の無視・自動補正・確認済みへの昇格・実運用開始は行わない。
