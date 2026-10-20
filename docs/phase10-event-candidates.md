# Phase 10：公開企業行動候補とメモリ内照合

## 結論と承認境界

4件の実観測の原因は未確定。今回CodexはAlpaca通信を行わない。
公開情報だけを調査し、rawバーの価格・異常境界・派生指標を検索、AI、調査サービスへ送らない。
公式カタログは部分的な調査結果であり、100証券の網羅的企業行動確認や証券同一性の承認ではない。
全候補のreview_statusとprovider_applicationはUNKNOWN。公開資料を読んだ事実はevidence_status=PUBLIC_SOURCE_REVIEWEDと区別する。
価格調整・補完・不連続の無視・売買承認は実装しない。

## カタログと調査結果

`config/public-corporate-actions.yaml` に7イベント・5種別を記録する。
企業名、株式クラス、ティッカーヒントと任意のsecurity_id_hintを分け、イベントID・版・修正元、公表日／公表時刻、観測時刻、法的効力日、価格境界日、公式条件・比率・根拠を保持する。
情報源は企業IR、SEC、取引所等の公式資料を優先する。今回、企業行動APIへの接続はない。

| 対象 | 公開イベント | 公表日 | 法的効力・分配 | バー照合候補日 | 根拠 |
|---|---|---|---|---|---|
| NOW | 1対5株式分割 | 2025-12-05 | 2025-12-17 | 2025-12-18 | [企業IR](https://newsroom.servicenow.com/press-releases/details/2025/ServiceNow-Shareholders-Approve-5-for-1-Stock-Split/default.aspx)、[SEC](https://www.sec.gov/Archives/edgar/data/1373715/000137371525000321/now-20251205.htm) |
| HON | Aerospace分社化：旧HON2株にHONA1株 | 2026-06-29完了発表 | 2026-06-29 | 同日 | [企業IR](https://www.honeywell.com/us/en/news/press-releases/2026/06/honeywell-technologies-launches-independent-pure-play-automation-company-following-honeywell-aerospace-spin-off) |
| HON | 2株を1株へ併合 | 2026-06-29完了発表 | 2026-06-29 | 同日 | [公式併合情報](https://investor.honeywell.com/reverse-stock-split)。分社化と別イベント |
| BKNG | 1対25株式分割 | 2026-02-18 | 2026-04-02 | 2026-04-06 | [企業IR](https://www.bookingholdings.com/wp-content/uploads/2026/02/Q4-25-BKNG-Earnings-Release.pdf)、[OCC公表](https://infomemo.theocc.com/infomemos?number=58460)、[実施確認SEC](https://www.sec.gov/Archives/edgar/data/1075531/000107553126000024/q1-26bkngearningsrelease.htm)。実施日と権利落ち日を混同しない |
| CMCSA Class A | 25株にVersant Class A 1株を分配 | 2026-01-05完了発表 | 2026-01-02取引終了後 | 2026-01-05 | [企業IR](https://www.cmcsa.com/news-releases/news-release-details/comcast-announces-completion-separation-versant-media-group-inc)、[Nasdaq](https://classic.nasdaqtrader.com/TraderNews.aspx?id=ECA2025-683)。Class Bを同一扱いしない |
| MMC | MRSHへのティッカー変更 | 2025-12-29 | 2026-01-14 | 同日 | [企業公式](https://www.marsh.com/en/corp/about/news/marsh-mclennan-to-change-nyse-symbol-to-mrsh.html)。分割ではない。別名を跨ぐraw系列は結合しない |
| BKNG | 通常配当USD10.50／株 | 2026-02-18 | 権利落ち日UNKNOWN | UNKNOWN | 上記企業IR。基準日3/6・支払日3/31から権利落ち日を推定しない。境界照合から除外し、情報不足件数に含める |

特別配当・その他のイベントを表現できる型と合成テストはあるが、今回の4件に対応する公式事実は確認できていない。
「通常配当」「特別配当」「分割」「併合」「分社化」「ティッカー変更」を別kindで管理する。
既存Phase 9の候補関数・証券登録簿は維持し、Phase 10カタログへ自動昇格・統合しない。既存Workflowは変更しない。

## 時刻・版と過去時点の情報

公表時刻が分からない資料はpublished_at=null、日付のみpublished_onに記録する。架空の午前0時で埋めない。
observed_atはその資料版を確認した時刻であり、イベント日当時にBotが知っていた時刻ではない。
現時点の事後調査として照合し、これを過去の判断入力へ流用しない。
公表日しか分からなければ同日の取引前に利用可能だったとは判定できない。将来のバックテストでは取引所時間・公表時刻・観測時刻を別々に検証する。
同一event_idの修正版は古い版を残し、revisionとsupersedes_revisionで直線的に接続する。重複版、欠落版、同じIDで企業・株式クラス・ティッカーヒントが変わる修正は拒否する。
active(at)は指定調査時刻までに観測された最新版だけを選び、後の訂正を過去時点へ遡及適用しない。

## 分類と集計

公開イベントの候補適用日が、前側の観測日より後・後側の観測日以前にある場合だけ候補にする。
旧・新ティッカーや異なる株式クラスを価格から推測して接続しない。公開条件の比率は保持するがrawの比率に近いことだけで原因を確定しない。

| 状態 | 現在の判定 |
|---|---|
| UNKNOWN | 候補なし、適用日不明など。通常の市場変動とも断定しない |
| EVENT_CANDIDATE | 公開資料・条件・株式クラス等がある単一候補と境界が一致。因果関係は未確認 |
| AMBIGUOUS | 同日に複数候補、公開根拠や条件の不足など |
| VERIFIED_CORPORATE_ACTION | 状態定義のみ。今回この状態への昇格経路はない |
| POSSIBLE_MARKET_MOVE | 状態定義のみ。網羅的調査・独立した根拠がないので今回価格変化だけで付与しない |

VERIFIEDには、手動の証券同一性レビューに加え、提供元raw系列の適用仕様、権利落ち／効力時刻、比率、分社化配分、配当条件などの根拠・適用範囲が必要。今回の候補型はprovider_application=UNKNOWNに固定する。

ログは各取得回の集計のみ。最上位件数は最後に検査できた回、quality_counts_per_passが回ごとの件数。

- discontinuity_count、matched_split_candidates、matched_reverse_split_candidates、matched_spinoff_candidates、matched_dividend_candidates、matched_ticker_change_candidates。
- unmatched_discontinuities、multiple_candidate_matches、unknown_count、event_candidate_count、ambiguous_count、possible_market_move_count。
- verified_event_countは0、unresolved_event_countは分類した不連続数。これらは観測単位で、公表資料を読んだイベント数ではない。
- public_catalog_events、public_catalog_incomplete_boundaries、public_catalog_exhaustive=false、automatic_verification_enabled=false。
- checks.public_catalog、checks.event_classification、従来のpaging・alias_quality・repeat_consistency等。未実施はNOT_RUN、未算定は件数を出さない／既存null。

種別ごとの候補件数は重複し得る。同日の分社化＋併合はmultiple_candidate_matches=1、ambiguous_count=1、unresolved_event_count=1であり、2件の解決ではない。
従来のpublished_event_boundary_matchesは旧登録情報の集計で、Phase 10拡張カタログの種別集計と同じ母集団ではない。
候補照合のPASSEDは「分類を実施した」の意味で、原因確認や品質合格ではない。

## 独立した手動Workflow

1. CI成功と利用条件を確認する。
2. Environment `alpaca-research` へユーザーが **ALPACA_EVENT_CANDIDATES_LIVE_ENABLED=true** を追加する。
3. 既存ALPACA_MARKET_DATA_LIVE_ENABLED、ALPACA_100_DIAGNOSTIC_LIVE_ENABLED、ALPACA_DISCONTINUITIES_LIVE_ENABLEDもtrue、Paperキー2個も必要。
4. **Alpaca 100-security public event candidates (manual)** をmainで手動起動し、**READ_ONLY_100_EVENT_CANDIDATES_NO_STORAGE** を入力する。
5. 不連続がある場合は再取得一致でもSTOPPED・終了コード1、diagnostic_complete=false、strategy_approved=false。失敗を成功へ読み替えない。
6. 必要に応じ新変数をfalseに戻す。自動再送しない。

workflow_dispatch、main、GitHub-hosted runner、既存concurrencyを維持。固定GET bars endpoint、IEX/raw/1Day/asof=-、221営業日、同一条件2回、120req/min・24試行・300秒・10分runnerの上限は変更しない。
欠損、重複、異常OHLCV、期間不整合などは企業行動分類前に停止する。不連続は35%超のまま保持する。
Secretsは専用実行stepのみ。ログ・artifact・cache・DB・Pages・外部AIに生市場データを出さない。
返却型ReadOnlyQualityReportとpurpose=diagnostic_onlyを維持し、AI・注文・約定・評価・保存の拒否ゲートを再利用する。
Codexは実Workflowを起動せず、Supabase、paper-live、API履歴、実Gemini、cronを変更しない。

## 2,000証券への更新方式と次の段階

候補収集→ソース版の正規化→訂正照合→人のレビュー→別PRでのカタログ更新とする。
発行体ID、内部security_id、上場市場、株式クラス、期間付きティッカーを別管理する。新たな同一性を候補から自動確定しない。
公式資料の版・訂正・取消、適用期間、実際の公表時刻と初回観測を保存し、将来の収集アダプターは公開資料だけを扱う。
企業行動APIを増設する際は契約・料金・第三者提供・保持条件とendpointの許可を先に確認し、別承認を得る。今回追加API接続はない。
大規模化にはイベント／別名／期間の索引、網羅性・遅延の監視、株式クラス別条件、再利用ティッカー、訂正の再評価が必要。
原因を確認しても、raw・調整系列の分離、分割による株数・原価、分社化新株と配分、配当権利・現金・税・通貨、注文の扱いを実装・検証するまでは仮想売買へ投入しない。
