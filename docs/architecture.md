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

# Phase 3アーキテクチャ

## フローと責務

```text
Parquet (1d / 1h) → causal confirmed filter → indicators → weighted screen
                                                        + held symbols
                                                               ↓
SQLite account / pending orders → point-in-time JPY valuation → DecisionContext
                                                               ↓
                         TradingStrategy (mock default / explicit Gemini)
                                                               ↓
                strict JSON + semantic validation → OrderPlanner (dry-run default)
                                                               ↓ explicit submit only
              atomic saved-plan verification + PaperBroker.place_order (PENDING)
```

screening/はデータ処理・指標・候補選定、strategy/はプロバイダー契約・公式SDK通信・構造化応答検証、orchestration/は口座評価・数量計画・明示的登録を担当します。AIはquantityを返さず、ニュース検索やコード実行のツールも与えません。Phase 2のexecuteは呼びません。

## 指標の定義

評価時刻as_ofまでに確定し、fetched_at <= as_ofで取得済みのバーだけを利用します。取得時に未確定だったバーも拒否します。市場の窓はPhase 2のwindowを再利用し、休日・昼休み・夏時間・早引けを扱います。後日取得したデータで当時利用できたように振る舞うことはありません。

日足の終値をC、出来高をVとします。最低21確定日足・2確定時間足が必要です。

- 直近1時間: 直近確定1h終値 / 直前確定1h終値 − 1。開始時刻差が正確に1時間でなければ計算不能とし、休場をまたぐ変化を1時間と偽りません。
- 直近1日: 最新確定日足終値 / 前確定取引日終値 − 1（暦日でなく取引日間）。
- SMA5/20: 最新5/20確定取引日終値の単純平均。
- RSI14: 日次差分から上昇幅・下落幅を作り、最初の14差分の平均を初期値にWilder更新 `(旧平均×13 + 新値)/14`。RSI=100−100/(1+平均上昇/平均下落)。両者0なら50、下落だけ0なら100。
- ATR14: TR=max(high−low, |high−前close|, |low−前close|)。前closeのある日から14TR平均を初期値に同じWilder更新。
- 相対出来高: 最新日足V / その前20日足Vの平均。分母0は計算不能。最新V=0はそれだけで無効にしません。
- ボラティリティ: 最新20日次単純リターンの標本標準偏差(ddof=1)×sqrt(252)。
- 平均売買代金: 最新20日足のclose×volumeの平均。JPY/USDネイティブ通貨。

SMA・RSI・ATRは未調整系列です。日次/時間足の急変や、最新日足から時間足への不連続がmax_price_jump（既定35%）を超えると除外します。値は補正しません。日足の最大経過は既定7日、時間足は1日です。

## スコアと候補

clip(x)=max(−1,min(1,x))。

| 要因 | 正規化式 |
| --- | --- |
| trend | clip((最新日足close/SMA20−1)/0.1) |
| momentum | clip(日次return/0.05) |
| volume | clip((相対出来高−1)/2) |
| liquidity | clip(log10(max(平均売買代金/市場閾値,1e−12))/3) |
| volatility | clip(1−年率volatility20) |
| quality | clip(1−2×時間足経過秒/許容経過秒) |

市場閾値は既定JP=1億JPY、US=1千万USD。スコアは重み付き和÷重み合計で、既定重みは順に0.25/0.20/0.15/0.15/0.15/0.10です。これは研究用仮説で、利益を実証した戦略ではありません。降順・同点はsymbol昇順で上位top_k（5〜10）を選び、その市場の保有銘柄を上限外でも追加します。指標不能の保有銘柄は理由付き・HOLD専用で残します。他市場の保有も総資産評価には必ず含めます。

## AI契約・権限・費用

DecisionContextは特徴量、候補スコア、観測/取得時刻・データ源、通貨別残高、保有・取得原価、含み損益、JPY総資産、各比率、FXとリスク条件を持ちます。入力ハッシュはランダムな評価IDを除いて計算します。モデルはconfig/ai.yamlだけで指定し、明示した無料枠確認済みモデル以外を拒否します。無料枠の実際の適用や課金はGoogle側のアカウント設定に依存し、アプリは課金状態を変更しません。

TradingStrategyはdecide(context)->DecisionResult。mockはHOLDを返し通信しません。Geminiはgoogle-genaiのDeveloper APIを固定し、SDK内リトライ・自動関数呼出しを無効化します。ツール・外部検索・有料フォールバックはありません。APIキーは明示したgemini操作の時だけ.envまたは環境変数から読み、環境変数が優先です。

入力はシステム指示・JSON特徴量・JSONスキーマのUTF-8バイト数に1024を加えた保守的トークン相当値で制限します。count_tokensの別API通信はしません。出力トークン、出力バイト数、タイムアウト、アプリ内リトライ、日次/月次/判断IDごとの最大リクエストを制限します。API前に予約し、失敗/タイムアウトも枠を消費します。サーバーの429では停止します。

応答はJSON重複キーを含め厳密にパースし、Pydanticと文脈検証を行います。未知/重複/欠落銘柄、余計なフィールド、無効action、0〜0.10外のweight、現在比率と矛盾するBUY/SELLを拒否します。HOLDのweightは無視し、未保有SELLのweight=0は注文を作りません。理由文は未信頼の参考情報で、注文権限を持ちません。

## 数量と登録

目標JPY額=target_weight×判断時の総資産。目標と現在評価額の差を価格・FX・単位で割り、株数を切り下げます。SELLでも小さな超過を単元へ切り上げて過剰に売りません。
BUYは二分探索で手数料・スリッページ反映後に目標比率、10%・80%・現金20%を守る最大単元数を決めます。既存PENDING BUYと新規BUYの見積資金・持高を累積予約し、同じsymbolのPENDING注文があれば重複計画しません。PENDING/新規SELLの代金は買付余力へ加算しません。ドル残高不足時にJPYを勝手に両替しません。

registerは明示オプションだけです。保存した口座・未約定注文・ルールのハッシュを現状態と比較し、保存済みAI判断から計画を再検証します。候補株価・FXの鮮度も登録時に再確認します。呼出元が変更したmodel_copyでなくDBの保存済み計画が正本です。ネイティブPaperBrokerを同じSQLiteトランザクションへ束縛して注文登録と判断の登録済み状態を一括保存します。例外なら全登録をrollbackします。
注文時刻は判断のas_ofでなく**実際の登録時刻**です。API待ち時間中に既に観測された価格で遡及約定しないようにします。注文IDは判断ID・symbol・sideから決定論的に生成し、同一判断の並行登録も一度だけです。約定では既存PaperBrokerの後続バー/リスク検査が再度働きます。

## 永続化・Secret・Workflow

既存SQLiteへdecisionsとapi_usageを非破壊追加します。決定のRUNNING claim、API枠予約、保存結果、注文登録は短いトランザクションで、ネットワーク中はDBロックを保持しません。判断IDが既に処理中なら重複呼出しを拒否し、成功結果を再利用します。失敗・中断したIDを勝手に再送しません。
記録は入力時刻・ハッシュ/スナップショット、モデル、promptバージョン、応答・判断・計画、登録結果、使用量、エラー、状態です。SDKエラー本文はログに出さず、キーの反射出力もredactします。SDK由来トークン数が不明ならnullとし0と捏造しません。

手動Workflowは保護されたmainのSHAをcheckoutし、contents:readだけを与えます。実APIステップだけSecretを受け取り、mockや通常CIは受け取りません。2銘柄の実データ＋一時100万円口座でdry-run固定・実API最大1回・リトライ0。注文/約定/公開artifactは禁止し、Summaryだけ表示します。API枠・SQLite口座はActions実行間で継承されず、継続運用正本やグローバル利用量の証明にはしません。

## Phase 4へ

Supabaseで口座・判断・利用量を永続化してから継続運用を設計してください。定期実行、オルカン実比較、実資金注文、ニュース検索、AIによるコード自動変更はPhase 3に含めません。Webは準備状況だけを公開し、手動テスト結果を継続的な運用実績と混同しません。


# Phase 4: 永続化と公開境界

```text
Disposable market quotes → existing DecisionEngine / PaperBroker / Decimal ledger
                                         ↕ Session contract (unchanged accounting)
                      SQLiteRepository (development) / SupabaseRepository (persistent)
                                                               ↕
                             bot_read → buffered transaction → bot_commit CAS RPC
                                                               ↓
                     advisory lock + account version + ledger/positions/FK checks
                                                               ↓
                            PostgreSQL bot_private (source of truth)
                                                               ↓ explicit publish
                               allowlisted public DTO → bot_dashboard + SELECT RLS
                                                               ↓
                                    GitHub Pages read-only dashboard
```

SupabaseRepositoryは会計処理を複製しない。1回のread RPCで口座・全イベント・versionを一貫取得し、既存PythonロジックがSessionへ書き込む。commit RPCは全writer共通のadvisory lockと口座行ロックを取得し、versionを比較して1トランザクションで反映する。競合時は40001で全体を拒否し、途中のREST更新は一切ない。最終書込みだけを自動リトライせず、操作全体の明示的再試行を要求する。

RPC内で初期資金・作成日時の不変性、時刻逆行、状態遷移、追記専用イベント、現金台帳と残高、約定と保有株数/原価、注文/約定対応を検証する。NUMERIC列・Decimalを維持する。数量/リスク計算は既存PaperBrokerが同じtransaction Session上で行い、判断登録もBoundRepositoryにより同じcommitへ含める。ネットワーク中の不確定失敗では、最新状態を調べてから同じidempotency keyで操作を再試行する。

API予約は同じglobal advisory lockで**全口座共通**の日/月上限を確認し、UTCサーバー時刻を記録してから通信する。予約完了時もaccount versionを進める。実行間で共有し、失敗・中断で枠を返さず、削除で帳消しにしない。API使用量は不明値null、推定費用は設定した標準有料単価による参考換算額であり実請求額ではない。SDKの使用量不明時はnull。Data APIキーはDDL管理認証として利用しない。

NAVの公表・観測日時、適用日、出所を保存する。オルカン比較は運用開始日時一致、明示した翌営業日以降の適用NAV、1万口価格から整数口数と端数現金を計算する。基準価額の自動取得/再公開の許諾が未確認のため、手動CSVを採用し、公開許可がないNAVの派生比較値も非公開にする。各スナップショットは正確な基準時刻で一意、訂正前を別追記履歴へ保存する。

公開専用テーブルには許可した派生情報だけを出し、内部JSON・取引ID・ハッシュ・生NAV/株価・内部エラーは出さない。匿名/認証済み利用者の内部schema/RPC権限と公開テーブルの書込み権限を剥奪する。Secretを持つ処理だけが安全なDTOをversion付きで公開する。Pagesへ渡すのは環境変数由来のURL/Publishable Keyだけで、ブラウザに書込み機能はない。

小規模の現段階では全writer直列化・口座イベント一括読込を採用し、複雑な分散会計は導入しない。イベント増加時のページング/アーカイブ、NAV訂正・分配金、バックアップ運用、実データの権利確認は継続運用前の課題。cronは追加せず、Phase 4の通常継続Workflowはdry-run固定である。
