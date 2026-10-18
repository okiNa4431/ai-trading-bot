# Alpaca Basic実市場接続の準備報告

調査・実装日：2026-10-18 JST。実市場API・実Gemini通信は0回。Supabase・既存口座・API履歴・公開ダッシュボードは変更していない。cronも未追加。以下の許諾判定は公開一次資料に基づく技術的整理で、登録アカウント固有の契約確認は未実施。

## 実装した範囲

`AlpacaDailyProvider`は既存MarketDataProviderのfetch契約を実装し、fetch_manyで最大10,000銘柄を100銘柄単位に分割する。固定の市場データホストへGETのみ。注文API・実証券ブローカー・Supabaseへの接続はない。既存CLIやスイング本番経路への自動接続は行わない。

- 1Day、feed=iex、adjustment=raw、currency=USD、sort=ascを必ず指定。asof=-で暗黙の旧名称マッピングを止める。改称前後の連結は企業行動の別検証が必要。
- limit=10,000は全銘柄合計の上限。少数行のページでもnext_page_tokenがあれば続行。重複／循環token、予算超過、返却対象外銘柄、重複timestamp、不正OHLCVは拒否する。
- 日付はNY現地午前0時をUTC化。DSTを保持。NYSEカレンダーの期待セッションごとに欠損を検出する。休日は欠損に数えない。NYSE以外に固有の停止・上場日・取引例外がある場合は保守的に停止する。
- **翌NY日付00:15まで当日足を使わない**。Basicの直近データ制限も考慮した保守的な実装方針であり、提供元の最終確定・改訂なし保証ではない。終値直後の判断は今回対象外。実測してから緩和を検討する。
- 実際の取得時刻fetched_atとsource=alpaca、report側のfeed・raw・mock/realを区別。過去データでもfetched_atを当時の観測時刻へ偽装しない。取得履歴を持たない過去バックテストはpoint-in-time証明にならない。
- require_completeで欠損または期待セッションなしを拒否。fetchは自動適用。fetch_manyの診断結果を使う側も保存／screening前に必ず呼ぶ。途中失敗は例外partialに正常取得分を残すが、自動保存・売買・再送はしない。
- 同一プロセスのProviderで共有する120要求/分の間隔制御。最大500要求/呼出し、100ページ/100銘柄、再試行最大2回。429・一時障害だけ再試行。401/403・redirectは停止し、別hostやSIPへ切り替えない。Retry-Afterは最大60秒。それ以上／不正な指定は停止。
- 複数runner全体のレート枠はこのプロセス内limiterでは共有できない。将来は既存のDB排他・GitHub concurrencyで単一取得を保証し、同じキーを使う別アプリの要求も含めて余裕を確認する。
- エラー本文・認証header・環境変数をログへ出さない。資格情報はrepr対象のモデルに保存しない。HTTP redirect・proxy環境変数を無効化。

通常の生成は既存require_live_market_accessで**キー読込とHTTP client生成より前に拒否**される。envやWorkflow入力で解除する仕組みは追加していない。テスト専用のhttpx.MockTransportだけを注入可能。実Transportをテスト用引数に渡すと拒否する。

## 月額0円・2,000銘柄の取得設計

[Basic仕様](https://docs.alpaca.markets/us/docs/about-market-data-api)は無料IEX、履歴200要求/分を案内する。[日足API](https://docs.alpaca.markets/us/reference/stockbars)は複数symbolsとページングに対応。IEXは全米統合OHLCVではない。流動性・出来高閾値をSIPと同等だと扱わない。

100銘柄バッチ・10,000行ページで221営業日を初期取得すると、2,000銘柄では理論最少60要求（442,000行）。2営業日を重複取得する差分は20要求/日が下限。小さなページ・再試行・企業行動・資産一覧を別に加算する。日足経路に対する技術的見込みで、実アカウントの権限・被覆率・実レイテンシは未検証。過去期間の全件を毎日再取得しない。差分は既存ParquetStoreのtimestamp upsertを使えるが、実データの保存先・保持は許諾確認後に決定する。

## 公開利用条件による用途別判定

許可・禁止・未確認を混ぜない。アカウント作成は[Paper Onlyを世界中から登録可能](https://docs.alpaca.markets/us/docs/paper-trading)。古い一般PDFのUS居住者文言で日本からのPaper登録全体を禁止と解釈しない。一方、Paper登録だけで今回の全用途が許諾済みとも扱わない。

| 用途 | 判定・根拠 | 今回の動作 |
| --- | --- | --- |
| 個人非商用の自動API取得・内部分析 | 一般許可の根拠あり。公式Basic APIと[標準TermsのPersonal and Non-Commercial Usage](https://files.alpaca.markets/disclosures/library/TermsAndConditions.pdf)。登録時の追加データ契約・non-professional区分に従う | 実通信はユーザー指示に従い停止 |
| 個人用の非公開保存 | 個人内部処理・[CSV/SQL保存の公式例](https://alpaca.markets/learn/collecting-market-data)はある。私的な委託処理サーバーであることだけで転載と断定しない。ただし今回の長期Supabase原本・特徴量・backupについて適用標準契約を未確認 | Supabaseへの実データ保存は未接続 |
| 保存期間 | 今回参照した公開一般規約から一律の許可日数を確定できない。30日・90日・無期限をライセンスとして捏造しない。契約終了・削除義務も登録時契約で確認する | 新たな実データ保持・削除処理は未起動。既存データを消さない |
| 最小派生特徴量を外部Geminiへ送信 | [公式MCP](https://docs.alpaca.markets/us/docs/trading-api-mcp)はAIクライアント利用を案内。AI利用全般は禁止としない。ただし無料Gemini側の学習・再利用に必要な権利は確認できない。比率・RSIへ変換しただけで許可になるとは断定しない | external_ai無効 |
| 市場データの一般公開・配布 | 標準TermsのMarket Dataに公開・配布等の書面同意条件あり。無許諾の転載は不可 | 原本・注文価格等は公開しない |
| 非復元的な集計運用成績だけの公開 | 公開一般規約では今回の具体的な派生指標の範囲を確定できない。価格を転載しないことだけで許可と推定しない | public_performance無効、公開projection未変更 |

[Googleの無料サービス規約](https://ai.google.dev/gemini-api/terms)は入力・応答を製品改善や機械学習に利用し、人手レビューも行うとする。有料サービスは別条件だが、課金を有効化することは今回の承認範囲外。無料枠のRPM/RPDが十分であることと、データを送る権利は別問題。

追加メールを必須とはしない。まず[契約一覧](https://alpaca.markets/disclosures)と登録時に表示されるアカウント固有の標準データ契約を確認し、適用文書名・版・日付を記録する。クラウド委託保管・外部サービスへの再利用許諾がそこで確定しない場合は、その用途を保留する。まず外部Geminiなしの私的な取得品質検証へ要件を限定すれば、Gemini送信権の問題を分離できる。問い合わせなしで全用途を満たすと断定はしない。

## 企業行動・ユニバース・FX

| 種別 | 取得先候補と残る条件 |
| --- | --- |
| 分割・配当・改称・合併等 | [Alpaca Corporate actions](https://docs.alpaca.markets/us/reference/corporateactions-1)。data_quality=allで未完成も検出し、next_page_tokenを追跡。検索日付はprocess_dateで、ex-dateと混同しない。公開遅延があり「空ならイベントなし」は保証しない。Basic権限・実遅延を確認し、失敗／古い観測／未対応イベントは全新規売買を停止。今回CA実取得・調整処理は未実装 |
| 米国普通株約2,000 | [GET /v2/assets](https://docs.alpaca.markets/us/reference/get-v2-assets-1)、[公式説明](https://docs.alpaca.markets/us/docs/working-with-assets)。active・us_equity・tradableだけでは普通株とETF/ADR/優先株を完全区別できず、sectorも別の根拠が必要。種類不明は採用しない。観測時刻付きのユニバースsnapshotを新たに蓄積し、現在一覧を過去に適用しない。実際の2,000銘柄一覧・分類は未取得 |
| USD/JPY | Alpaca株式APIからは調達しない。既存reference_fxの[ECB参照値](https://www.ecb.europa.eu/stats/policy_and_exchange_rates/euro_reference_exchange_rates/html/index.en.html)が無料日次候補。JPY/EUR÷USD/EURを同じ公表日の値で算出。[再利用条件](https://www.ecb.europa.eu/services/using-our-site/disclaimer/html/index.en.html)の出所・変更表示に従う。ECBは参照・情報目的で取引利用を推奨しない。実約定時の新鮮なFXの代替にせず、日次参考評価用途と明示。取引用FXの許諾・観測時刻・鮮度条件は未解決 |

無料の株価取得が可能でも、この3系統と市場indexの独立した時系列・品質が揃うまでは完全な仮想運用に接続しない。日足を架空の時間足へ展開せず、過去FXを現在観測で補完しない。

## ユーザーが今すぐ行う設定

1. AlpacaのPaper Only／Basicを選択し、無料プラン・non-professional用途と登録時の利用条件を確認する。実証券口座への資金投入・有料プラン登録は不要。
2. Paper環境のキーを発行する。チャット・issue・コード・スクリーンショットへキー値を貼らない。
3. GitHubリポジトリ Settings → Environments に将来の読取専用検証用 `alpaca-research` を用意し、利用可能ならRequired reviewers・prevent self-review・main限定を設定する。private/public・プランによる保護機能差を確認し、保護できなければ実データWorkflowを追加しない。
4. そのEnvironmentのSecretsへ **ALPACA_API_KEY_ID** と **ALPACA_API_SECRET_KEY** を登録する。[GitHub公式手順](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-secrets)を参照。既存Supabase/Gemini Secretsは変更不要。キー登録自体は取得を開始しない。
5. 登録時の適用データ契約の文書名・版・参照先と、Basic選択の確認結果を共有する。秘密キーの共有は不要。個別メールは現段階で不要。

今回はEnvironmentやSecretsを実際には作成・変更していない。新規Workflowはモック専用で、secretsを参照しない。実データ用Workflowを有効化するには、次の条件を満たす別の変更と明示的承認が必要。

## 実市場テスト開始までの残課題・手順

1. 取得・私的保存の適用契約を確定。保持・終了時処理・バックアップを含む範囲を定義する。Geminiと公開の許諾は別項目として保留可能。
2. 実データの読取専用手動Workflowをレビューして追加。Environment承認、concurrency=1、要求数上限、固定GET先、権利記録参照を必須にし、秘密情報をartifactに含めない。市場データ入りartifactは公開リポジトリへuploadしない。データ本文を標準出力へ出さない。
3. 明示的承認後のみ停止ゲートを限定変更し、まず20銘柄・終了済み201〜221セッションの取得品質を確認。実データとして観測時刻、欠損、IEXのOHLCV、raw、要求数・遅延を非公開記録する。今回はこの段階を実施しない。
4. 同じ範囲を次回手動実行し、2セッション重複の差分・改訂・冪等保存を検証。欠損を自動補正せず、イベントや分類の不明は停止。
5. 100→500→2,000へ拡大し、実被覆率・速度・無料枠・保管容量・転送を測定。今回のモック時間を実APIの速度としない。
6. CA、普通株分類、FX、市場benchmarkを揃え、権利のあるデータをpoint-in-timeのスクリーニング入力へ変換する。既存SwingDataの合成FX／時間足生成経路へ実データを渡す代用接続はしない。現在その実データbridgeは未接続。
7. 新規test口座でまずルール／mock HOLD。Gemini送信権が確定してから、別承認で実Geminiを最小回数検証。paper-live・実証券注文・cronはこの手順でも有効化しない。

## 再現可能なモック検証

追加35テスト成功。修正後の全Python回帰は500件成功・PostgreSQL専用42件スキップ（ローカルDB未構成）。Webビルド・13テスト、uv sync --locked、Ruff check・format（113ファイル）、mypy（60ソースファイル）も成功。PostgreSQL専用試験は既存CIの使い捨てDBで確認する。

検証中に、全期待行を受信した後の後続ページ障害を完全取得と誤認できる余地を修正した。行の被覆率だけでなく全ページの正常終了をrequire_completeの必須条件とし、部分結果を利用しない回帰テストを追加した。途中正常分は診断用に保持する。

ローカル実測（221セッション、ネットワーク待機・実レート制限待機を含まない）：

| 銘柄 | 行数 | HTTPモック要求 | 秒 |
| --- | --- | --- | --- |
| 20 | 4,420 | 1 | 0.161 |
| 100 | 22,100 | 3 | 0.436 |
| 500 | 110,500 | 15 | 2.780 |
| 2,000 | 442,000 | 60 | 14.639 |

全規模で欠損0。20銘柄の別結合テストは既存ParquetStoreへ全4,420行を保存・再保存・再読込する。規模試験の数値はHTTPモックJSON生成とProvider解析の実測で、既存のNumPyスクリーニング単体の時間や実APIの速度とは異なる。

`uv run pytest tests/test_alpaca.py -v` は20銘柄221営業日のHTTP→PriceBar→Parquet再読込・重複保存、20/100/500/2,000銘柄、DST、欠損、未確定、異常OHLCV、循環ページ、部分失敗、予算、429/503/timeout、ログ秘匿、実通信ゲートを検証する。

`uv run python scripts/alpaca_mock_probe.py` は全4規模で各221セッションのJSONレスポンスを生成し、同じ実装でページング・検証する。`Alpaca offline integration (manual only)` Workflowも同じ処理のみ。すべてmockと明示し、実相場の接続成功や運用利益として公開しない。
