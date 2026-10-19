# 実市場統合：外部AI送信直前までの実装報告

2026-10-19 JST。実Alpaca通信0回、実AI通信0回、実Supabase migration／書込0回。既存paper-live・API利用履歴・公開Webデータは未変更。以下の実行証拠は合成fixtureと使い捨てDBであり、実市場の利益実績ではない。

## 独立コミットと再利用

1. 2713627：外部AI境界。実SDK transportは無効。合成入力＋明示的SDK mockだけで応答・503/429/timeout・使用量記録を試験。
2. 9a4a30c：追加専用の非公開研究schema、CAS保存・実行照合・RLS試験。
3. c62d54f：観測時刻付き日足bridge、JSON Schemaで検証する内部AI入力。
4. 後続の統合コミット：分離した注文／約定cycle、ルール比較、費用・成績、非公開保存の結合試験。途中で見つけたステージ状態の引継ぎ欠落と約定価格ログも修正。

再利用したのはPhase 6の固定GET/raw/IEX/ページング・レート上限、日次50→15選別、既存OrderPlannerとPaperBroker、通貨別損益・コスト、NAV適用日／公表日／権利検証。既存の一般取得ゲートは解除していない。

## 完成した接続

`ObservedDailyData.collect` は承認済みのPhase 6 Providerだけを受け取り、raw日足を実際のfetched_atのまま指標行列へ変換する。独立した市場参照系列、出所と期間を持つ企業行動の検証記録、観測済みUSD/JPYを別入力として要求する。足を架空の時間足やFXへ展開しない。

分析には201セッション以上の全列と、その全観測がas_ofまでに利用可能だったことが必要。最新の完了済みセッションも必須。重複、欠損、異常OHLCV、不連続、未来の銘柄metadata、未確認／古い企業行動記録、古い／未来のFXは停止する。保有・未約定銘柄は評価から落とさない。出来高0自体をデータ不正とせず、売買適格性・停止可能性は別に扱う。

`stage_cycle` は口座状態を明示的にメモリへ複製する。既存broker/plannerで注文・約定・残高・保有・費用・評価を計算し、成功した状態だけを `commit_cycle` → 私的RPCでまとめて保存する。Supabase障害のfallbackではない。DB読取に失敗すればステージ処理も保存も開始しない。

- 対象は新規 `test-integration-*` のみ。paper-liveはPythonとSQLの両方で拒否。
- 初期資金100万円。初回の仮想両替は明示額・最大80万円・初回decisionだけ。自動FXなし。
- 1銘柄10%・株式80%・現金20%の既存制限を注文計画と約定の両方で適用。手数料・spread・slippage・FX費用を残高と成績へ反映。
- `kind=decision` はmock HOLD／rule／momentumまたは検証用mock JSONで注文受付のみ。`kind=execute` は別runで後続の確定済みバーを処理。適格バーがなければPENDINGを維持。
- 市場・完了セッション・kindのslotを状態へ記録し、別run IDや後刻の二重decisionも拒否。account CASで競合・期限の古い書込を拒否。
- raw履歴のsplitや、口座開始後の配当・上場廃止・停止等の未対応イベントは停止。分割調整・配当入金を勝手に行わない。過去の非保有期間の配当で利益を加算しない。

## 内部AI入力・送信禁止

`build_draft` は銘柄・指標・保有・未約定・リスク制限を含む私的JSONと、送信予定のcompact JSONを生成する。Pydantic契約からJSON Schemaを生成し、Draft 2020-12 validatorで実際に検証する。応答もschemaと既存の意味検証を通し、重複JSON key、未知銘柄、BUY/SELL/HOLD以外、矛盾したtarget、過大応答を拒否する。

入力のUTF-8 bytes＋system/schema＋余裕を保守的token-equivalentとして記録。出力予算と設定済み単価から標準有料換算を見積もる。正確なtokenizer計測・請求額・現在の無料枠保証ではない。実count_tokens呼出しも行わない。

市場由来／origin不明はGeminiのpayload生成・キー読込・枠予約より前に拒否。syntheticへ付け替えてもAlpaca特徴量が含まれていれば拒否。さらに実SDK client自体を作る経路を停止し、明示的なunittest mock factoryとmock clientしか受け付けない。一般CLI・旧Gemini Workflow・無料接続を含めて実送信できる解除設定は追加していない。API枠・既存履歴は変更しない。

内部draft・生OHLCV・価格hash・口座state・私的resultをstdout／Actions output／artifact／cacheへ出さない。brokerの約定価格ログを削除し、観測値検証の例外も入力を含めない。新しい手動 `integration-fixture.yml` は合成fixtureのみ・Secretsなし・DBなし・artifactなし。

## 成績比較

同じ日付の複数cycleを1日の最終評価へまとめ、初期100万円からのリターン・DD等を私的集計する。JPYとUSDの費用は通貨別に保持し、過去コストを現在FXで円換算した架空の内訳は作らない。intraday観測はなくDDは観測点間の値。短期標本の年率リターン等は未評価を維持する。平均保有期間の集計値は未算出だが、ルール判断用の実営業日保有年齢は計算する。

`compare_private` は同一初期資金・期間・データ履歴・費用規則でのみrule／momentum／mock結果を比較する。NAVは `compare_all_country` を接続し、適用日・公表時刻・内部利用権が揃わなければ未評価とする。投資初期NAVを捏造しない。NAV未初期化／欠損時の超過リターンはnull。

公開用の価格・系列・projectionを生成しない。利用者の限定的な許諾確認を集計成績一般の無制限公開許可へ拡張せず、再構成可能性の確認が終わるまでpublication flagはfalse。提供元サポートの非公開回答本文・担当者情報は保存・公開していない。

## SQLと手動操作

新規SQLと影響範囲は [research-migration.md](research-migration.md)。**本番未適用**。所有者の明示的承認後に追加SQLだけを実行し、新規隔離口座への書込も別に承認する。既存口座やAPI履歴の初期化は不要。保持期限・削除・backupも別の運用承認で決める。secret値の再共有は不要。

原本は内容ごとのimmutable vintageとして重複排除し、最初の観測時刻を保持する。指標・内部draft・口座状態・注文・約定・実行結果は同一commitで整合する。応答喪失時は自動再送せず `store.reconcile(run_id)` で既知の結果を私的に照合する。不明なら停止。公開roleはschema・table・RPCへアクセスできず、RLSもdeny by default。

runには当該入力の原本ID集合も非公開で固定する。価格がA→B→Aに戻っても、そのrunが使用したvintageを特定できる。原本rowのfetched_atは保存された最初の観測であり、すべての再取得時刻を表すものではない。runの入力にその時点の利用可能時刻を保持し、再取得を過去の公表時刻へ偽装しない。

## 次の実データ検証と未完了事項

1. 初回Alpaca通信の明示的承認後にだけ、Phase 6の契約記録・live_enabledを別変更で更新し、20銘柄の読取検証を手動実行する。今回はfalseのまま。
2. 独立市場参照、出所付きの企業行動coverage、鮮度を満たす観測FXを私的に用意する。これらの**実API取得adapter・Basic権限／遅延の実測は未完了**。空イベントを確認済みと仮定せず、日次参照FXを新鮮な取引FXへ置換しない。固定20銘柄のPhase 6リストには市場参照が含まれないため、追加取得のscopeも別レビューが必要。
3. SQL適用と私的書込の承認後に、新規隔離口座で `ObservedDailyData.collect` → `commit_cycle(..., kind='decision', strategy='mock')`。次の営業日データ取得後、別runで `kind='execute'`。初回BUY/SELL検証は明示的mock JSONとruleを区別する。
4. 20銘柄の複数営業日で品質・整合性・容量を測定してから100→500→2,000へ進む。分析bridgeは2,000まで扱えるが、Phase 6の通信上限20は解除していない。50MB/commitを超える初回投入には分割stage／公開しない保管設計が別途必要。無料DB容量・転送で継続できる保証は未検証。
5. 2,000普通株の分類・sector・point-in-timeユニバース、実NAV初期購入と比較可能日、非復元性の公開レビューは残る。現在のmetadataだけで過去の全市場構成を保証しない。

外部Gemini送信、実証券注文、cron、paper-live金融操作は、この手順でも開始しない。実市場での利益・実Geminiの判断品質は未検証である。
