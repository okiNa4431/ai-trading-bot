# アーキテクチャ

```text
CLI → YAML設定 → collect → MarketDataProvider
                              ↓
                     YahooFinanceProvider
                              ↓
                    OHLCV検証・PriceBar
                              ↓
                     ParquetStore → Parquet
```

## モジュールの責務

- config.py: 設定読込、銘柄・市場・通貨の整合性検証。
- models.py: UTC付き未調整PriceBarとOHLCVの不変条件。
- provider.py: Yahoo API、タイムアウト、リトライ、要求間隔、変換と検証。
- storage.py: パーティション検証、排他書込み、差分統合、原子的ファイル置換。
- service.py: 取得期間、重複期間の再取得、銘柄ごとの失敗継続。
- cli.py: 引数、終了コード、保存状況と結果の表示。

## データ契約とフロー

auto_adjust=False、back_adjust=False、repair=Falseで取得します。調整後終値は取り込みません。
source=yahoo、adjusted=false、UTCのfetched_atを各行に保存します。調整済み系列を同じ保存先に混ぜません。
主キーはsymbol・interval・UTC timestamp。marketとcurrencyも保存先と照合します。重複入力はエラー、既存履歴との重複は新規取得が優先です。取得時刻だけの変化は更新件数に含めません。

Yahooのタイムゾーン付きインデックスをUTCに変換し、再サンプリングしません。日本の昼休みは埋めず、米国夏時間は返却オフセットを引き継ぎます。時間足は開始1時間後、日足は市場現地の翌日0時以降を確定と扱います。短い最終バーも保守的に待ちます。取引所カレンダーによる確定判定は未実装です。

必須列欠損、NaT、タイムゾーン欠損、不正OHLC、非有限値、負の出来高、重複は銘柄・足のバッチ全体を失敗させます。出来高0は許容します。隣接終値50%超の変化はログ警告のみで、分割等を勝手に修正しません。取引予定からの欠落バー検出は今後の課題です。

## 保存と運用

market=JP/symbol=7203.T/interval=1h/bars.parquet形式で保存します。ルートに.write.lockを排他作成し、同時書込みは待機せず失敗します。一時Parquetの完成後にos.replaceで置換し、途中失敗では既存ファイルを維持します。対象はローカルファイルシステムです。
強制終了後のロックは稼働プロセスがないことを確認して手動解除してください。
updateは設定範囲内の末尾数本から取得し、設定の履歴範囲外の古い欠損は自動補完しません。履歴範囲外のギャップは警告します。

Actionsの3日間artifactは永続保存ではありません。実行ごとに初期履歴を取得し、前回データの自動復元やGitへのpushはしません。

## 将来の拡張

Geminiへ渡す入力は検証済み履歴から別サービスで作成する予定です。PaperBrokerとGitHub PagesはPhase 2で実装しました（詳細は後半）。Supabaseによる永続保存と運用集計のWeb表示は未実装です。

実売買への移行時はMarketDataProviderを契約済みデータ源へ置換し、実ブローカー、取引所カレンダー、株式分割・配当、為替、監査記録、注文冪等性、リスク上限・停止機構を追加します。未調整価格だけで損益を計算しないでください。

## 総合検証後の検証境界

保存APIでもPriceBarを再検証し、検証を省略するmodel_copy/model_constructで作られた不正値を拒否します。既存Parquetでは全フィールド（source・adjusted・fetched_atを含む）の存在を必須とし、欠落を既定値で補完しません。空の既存ファイルも異常として保存を中断します。
保存失敗は銘柄・市場・足・例外種別をログに残します。PydanticやYAMLのエラー表示に入力値を含めず、将来の認証情報を誤って設定してもその値を表示しません。

# Phase 2アーキテクチャ

## モジュールと境界

```text
CLI paper → Broker Protocol → PaperBroker
                               ├─ execution: 取引所カレンダー・後続確定バー選択
                               ├─ ledger: Decimalの現金・平均原価・通貨別損益
                               ├─ valuation: 同一基準時刻の価格/FX・円建て時価
                               ├─ risk: 約定後の買付上限
                               └─ PortfolioRepository Protocol
                                      └─ SQLiteRepository
ParquetStore → PriceBar ──────────────→ execution / valuation
```

- `broker/base.py`: 残高、保有、注文、取消、約定の共通契約。LiveBrokerは未実装。
- `broker/paper.py`: 注文受付と約定を分離し、全量約定・拒否・保留・取消を管理。
- `broker/execution.py`: Yahooのバー開始ラベルを解釈し、JPX/NYSEカレンダーで取引可能時刻を検証。判断に使える確定済み終値と、執行基準の後続始値を区別。
- `portfolio/models.py`: 金額はDecimal、時刻はUTC。注文のコスト条件、価格・為替の出所と時刻、約定・交換・台帳・評価のモデル。
- `portfolio/ledger.py`: 取得手数料を含む原価総額と平均取得単価。部分売却時の残原価、全売却時の原価残高ゼロ、通貨別株式確定損益。
- `portfolio/valuation.py`: 価格や為替の不足・未来・古さを明示的に拒否。正常な総資産を捏造せず、円建て比率超過を記録。
- `risk/manager.py`: コスト反映後の買い注文で10%・80%・最低現金20%を検証。設定による緩和も不可。売却を自動指示しない。
- `persistence/sqlite_repository.py`: 永続化インターフェースとSQLite実装。Parquetとは独立。
- `paper_cli.py`: ローカル操作。固定為替を指定するCLIは必ずSIMULATION表示。

## 約定時刻と因果関係

注文時刻はバー開始時刻と区別します。1hはバー開始が注文より厳密に後、日足は市場現地0時ラベルに対応するセッション寄付が注文より後でなければ使いません。
最初の適切な後続バーの始値に不利なスリッページを適用し、全量約定だけを扱います。休日、昼休み、出来高0、不正データ、未確定バーでは約定しません。

バーの確定判定はPhase 1に合わせて時間足を保守的に1時間待ち、日足はセッション終了まで待ちます。さらに、取得時刻がバー確定より前のスナップショットは、後で時刻が進んでも確定データに昇格させません。
履歴モードは後日取得された完全な履歴を使えますが、判断用のknown_marksは評価時刻までに確定した終値だけです。ペーパーモードは取得時刻も注文より後かつ処理時刻以前であることを要求し、古いデータを拒否します。
約定履歴にはprice_at（基準価格時刻）、observed_at（バー確定確認時刻）、price_fetched_at（実取得時刻）、processed_at（約定処理時刻）、modeを別々に保存します。

historicalは価格発生時点へ会計を進め、paperは処理時点へ進めます。以降のイベントや評価が過去へ戻ることを拒否します。過去時点の口座状態を復元するイベントリプレイは未実装です。複数注文は処理対象の価格時刻順に明示実行してください。
買付リスクはその時点で観測できる保有株の価格と為替で評価し、対象銘柄だけは約定基準の始値を使います。将来バーの終値や高値を買付判断に渡しません。

## 会計・為替・整合性

JPY/USDは別残高です。初期100万円のみで追加入金を設けません。為替交換は明示イベントで、固定レートと市場観測レートの出所をモデルで区別します。固定レートはデモ用で実レートとして扱いません。
買付代金・手数料は不利方向へ切上げ、売却代金は切下げます。JPYは1円、USDは0.01ドル単位。スリッページ、手数料、丸めコストを約定に、為替・丸め費用を交換履歴に記録します。評価額は両替ではないため、円換算の端数も保持します。
平均取得単価と原価総額を分け、全売却では残りの原価全体を使用し端数が蓄積しないようにします。株式確定/評価損益はネイティブ通貨で保持し、為替費用と混同しません。円換算の詳細な損益要因分解は将来拡張です。

SQLiteにはaccounts、balances、positions、orders、executions、fx_exchanges、cash_ledger、valuationsを保存します。金額はTEXTのDecimal表現です。BEGIN IMMEDIATEで並行更新を直列化し、注文の状態・約定・台帳・評価・残高/株数を1トランザクションで更新します。例外・割込み時はrollbackし、同じorder_idは再約定しません。異なる内容でのID再利用は拒否します。注文以外のイベント履歴は追記のみです。接続は初期化と各操作の終了時に明示closeします。

## Web・CI・公開境界

React / TypeScript / Vite / Tailwindの静的ページをweb/に置き、base=/ai-trading-bot/でGitHub Pagesへデプロイします。公開内容はプロジェクト概要と開発状態だけで、架空の成績やSQLiteの口座データは含めません。Pagesには静的ビルド成果物だけをアップロードします。
CIはWindows/UbuntuのPythonテストとUbuntuの型チェック・本番ビルド・4画面幅のブラウザテストです。paper口座はローカル実行専用であり、Actionsによる定期売買や口座永続化は設けません。

## 将来の連携

Geminiは観測可能な時刻で切った価格と評価を入力し、注文要求をBrokerへ渡す層として追加できます。Supabase移行時はPortfolioRepositoryとトランザクション契約を実装します。LiveBroker移行時は実気配・約定イベント・取引単位・注文冪等性・認証・リスク停止機構を別途検証する必要があります。
yfinanceのOHLCVは執行可能な気配値ではなく、仮想約定は実市場と異なります。配当・株式分割は未対応であり、未調整価格による長期成績の正確性を保証しません。
取引所カレンダーは[pandas_market_calendars](https://pandas-market-calendars.readthedocs.io/en/stable/calendars.html)、GitHub Pages構成は[Viteの公式手順](https://vite.dev/guide/static-deploy.html)を参照しています。

価格の時刻price_atと取得・観測可能時刻available_atを分離しています。一括取得で全バーのfetched_atが同じでも最新の価格時刻を選び、古い価格を再取得しただけで鮮度を更新しません。
