# Phase 6：Alpaca実市場の読取専用検証準備

確認日：2026-10-18 JST。実装とモック検証のみ。**実Alpaca Workflowは未実行・実API通信0回**。Supabase・paper-live・API履歴・公開Webのデータを変更していない。

## 現在の承認状態

ユーザー申告によりEnvironment `alpaca-research`、main限定、2 Secretsの登録完了を把握している。キーの値・アカウント画面・適用契約は取得していない。アカウント固有の標準契約が未確認のため、`config/alpaca-readonly.yaml` の `live_enabled: false` と空の `contract_reference` を維持した。Workflowを手動起動しても、このままでは**キー読込・HTTP client生成前に非0終了**する。

今回の依頼はWorkflow実装・モック・CI・pushまでである。Codexは実通信Workflowをdispatchしない。契約確認と初回実行許可を得た後、別のレビュー可能な変更で契約参照とこの専用経路だけのactivationを設定する。既存の `require_live_market_access()` は変更していない。

## 公式利用条件の再確認

| 用途 | 公開資料の結論 | 今回の扱い |
| --- | --- | --- |
| 個人非商用のAPI読取 | [Termsの個人非商用利用](https://files.alpaca.markets/disclosures/library/TermsAndConditions.pdf)と[Basic無料IEX](https://docs.alpaca.markets/us/docs/about-market-data-api)に一般的根拠あり | 今回アカウントの適用契約確認まで停止 |
| 自動取得・個人内部研究 | [Paper Only公式仕様](https://docs.alpaca.markets/us/docs/paper-trading)は世界中からの登録とIEXによるアルゴリズム試験を案内 | API権限があることと追加のデータ条件は区別 |
| GitHub Actionsでの一時処理 | 公開一般規約にGitHub-hosted runnerという製品名での明示許可は確認できない。私的な処理委託で、第三者への配布・再利用を行わない用途として整合する可能性はあるが、**これは解釈**。アカウント適用条件を未確認なので許可済みと断定しない | 使い捨てrunner・RAMのみ・集計のみ出力という最小構成を準備、未起動 |
| ローカル／クラウド保存・保持期間 | 公開一般規約から今回の原本・特徴量・バックアップの一律保持日数を確定できない | 実データは一切ファイルへ保存しない。既存データの削除もしない |
| 外部Gemini送信 | 公式AI連携があっても、無料Gemini側の二次利用を含む今回の権利は未確定 | 送信もGemini呼出しもなし |
| 派生成績の公開 | 一般規約の公開・配布制限と、非復元的成績だけの具体的範囲を分ける。今回の公開許可は未確認 | 成績生成・公開なし |

[現行契約一覧](https://alpaca.markets/disclosures)にTerms、NASDAQ OMX Subscriber、NYSE Market Data Display契約が掲載されることを確認した。今回、後者2文書のリンク先本文はWeb取得エラーで確認できていない。[公開Customer Agreement](https://files.alpaca.markets/disclosures/library/AcctAppMarginAndCustAgmt.pdf)にも非専門家区分と市場データの複製・配布等の制限があるが、Paper Onlyへの適用範囲はアカウントの同意文書で確認が必要。公開TermsのData PlansではBasicを無料とし、Pro選択時の追加契約も記載する。この記述を根拠に、すべてのPaper Only／Basic利用者へNASDAQ・NYSEの有料フィード条件が同じ形で適用されるとは断定しない。逆に、すべての追加条件が免除されるとも推定しない。公開PDFの米国居住者向け記述と、世界中のPaper Only登録を認める現行仕様の差もあり、実際に登録時に同意した標準条件を確認する。

確認方法は、Alpacaのアカウント設定・Market Data subscription／agreements・登録時の同意文書を利用者が確認し、文書名・版・日付・公式参照先、Basic／Paper Only・IEX・個人非商用であることを共有する。キー・口座番号・個人情報は不要。個別メールは最初から必須にしない。公開資料／適用標準契約で決着しない用途だけを保留する。

## Workflowと通信範囲

名称：**Phase 6 Alpaca read-only validation (manual)**。ファイル：`.github/workflows/alpaca-readonly.yml`。

- workflow_dispatchのみ、mainのみ、Environment `alpaca-research`。concurrencyは専用固定グループ、実行中の強制キャンセルなし。待機要求が増える場合は次の手動実行を控える。既存取得ゲートは他Workflowを遮断したまま。
- 承認入力は完全一致の `READ_ONLY_20_NO_STORAGE`。入力はenvで受け取り、shellへ展開しない。さらに契約記録とreview済みactivation、GitHub-hosted runner、repository、event、workflow_refをPythonで確認する。
- 上記は運用上のチェックであり、悪意あるPythonコードを書き換える攻撃に対するsandboxではない。mainのレビュー・Environment保護が境界。Required reviewers／self-review禁止が利用可能なら追加する。既存Environment・Secretsは今回変更しない。
- 通信は `https://data.alpaca.markets/v2/stocks/bars` のGETのみ。URL・method・port・path・query・symbol集合・空bodyを送信前に検証。注文、資産一覧、CA、FX等の別endpointも拒否する。redirect、proxy環境変数、HTTPTransport自体のretryを無効化。
- APIキーはEnvironment Secrets `ALPACA_API_KEY_ID`／`ALPACA_API_SECRET_KEY`を実行stepだけへ渡す。Paperキーであることは利用者が発行元で確認する。認証キーの文字列パターンからPaperを断定する仕組みは作らない。
- 既存Providerのraw IEX日足検証を再利用し、NYSEカレンダーの直近221営業日（設定可能範囲201〜221）を固定リスト最大20銘柄で取得する。今回は20銘柄からの変更をWorkflow入力で受け付けない。
- 120要求/分、最大12要求/取得pass、4ページ/pass、2回の再試行、2pass合計24要求で停止。20×221行なら通常1ページ/passが見込みだが、実測ではない。429・5xx・timeoutは有界retry、401/403/redirectは即停止。Retry-Afterは最大60秒。
- 同じ期間を2回取得してOHLCV等を比較。fetched_atだけを比較から除外する。改訂・欠損・異常値・未完了ページは非0終了し、勝手に補正しない。古い正常ファイル等を参照するfallbackはない。
- 日足の採用可能時刻は翌NY日付00:15 UTC換算。DST・休場・短縮日を反映する。これは保守的な採用基準で、提供元の最終公表・改訂なし保証ではない。取得時刻を過去の公表時刻に偽装しない。

## 出力・保持・品質の限界

生OHLCV・銘柄別履歴・価格hash・レスポンス本文・認証headerは出力しない。HTTPヘッダーまでの応答時間・status別件数・要求数、全体行数、欠損／成功率、期間・観測時刻・採用可能時刻、再取得の変更件数等の許可した集計だけを出力する。失敗時も本文やtracebackを表示せず、停止状態・要求／行数／不足銘柄数等だけを残す。通常ログ出力を抑止し、cleanupでも例外内容を出さない。

**artifact uploadなし・市場データのディスク書込みなし・Supabaseなし・Pagesなし・Geminiなし**。取得したデータはPythonプロセスのRAMだけで扱い、終了時に破棄される。完了した別runと価格履歴を照合する永続保存は今回対象外。2passの整合性確認は同一run内だけ。公開リポジトリのActionsログに残るのは集計であり、原本や運用利益ではない。runner自体やGitHub内部処理までの物理的なメモリ消去を保証するものではない。

固定リストはAAPL、MSFT、AMZN、GOOGL、META、NVDA、JPM、V、MA、WMT、COST、HD、PG、KO、PEP、JNJ、UNH、XOM、CVX、CAT。普通株の初期検証候補として選んだもので、現在の流動性・上場状態を実取得確認したという意味ではない。変更は設定diffでレビューする。

IEXのOHLCV・出来高は全米統合SIPと異なる。市場全体との出来高偏差をこの単一feed試験だけで定量比較できない。出来高0は単独では異常としないが集計する。NYSEカレンダーは銘柄固有の停止・IPO日を証明しないため、その場合の欠損は調査まで停止する。35%超の終値不連続は企業行動等の確認待ちで停止するが、この閾値だけで分割・配当を網羅検出できない。CA APIは今回呼ばないので `corporate_actions_verified=false`、常に `strategy_ready=false` を出す。

## 利用者による初回実行手順

**現在は実行しない。契約確認と明示的許可が先。**

1. 上記の適用標準契約を確認し、参照先・版と「この20銘柄の個人研究・GitHub-hosted上の一時処理のみ」の初回実行許可を伝える。秘密キーは送らない。
2. 別のレビュー済み変更で専用設定の契約参照とlive_enabledのみを更新する。一般市場ゲート・Gemini・売買・保存・cronは解除しない。
3. GitHub Actions → 本Workflow → Run workflow → branch `main`、approval `READ_ONLY_20_NO_STORAGE` を入力。Environmentの承認要求があれば、対象commit・設定・Secrets scopeを確認して承認する。Codexは今回dispatchしない。
4. 集計のstatus・期待4,420行/pass・成功率・欠損・HTTP件数・観測／採用時刻・再取得差分を確認。エラーや変更があれば停止したまま調査し、不明結果を自動再送しない。

## 次の規模・スイング接続へ進む条件

20銘柄の複数営業日で被覆率・時間・改訂・例外を確認し、無料枠・GitHub Actionsの割当時間も確認する。100→500→2,000は**今回の20上限を解除する別レビュー**が必要。銘柄分類とpoint-in-timeユニバース、CA品質・遅延、取引用USD/JPY、独立した市場benchmark、非公開保存・保持・backupの権利と容量を揃えてから段階拡張する。今回の一時RAM試験をSupabaseの保存許可へ拡張しない。

スイング入力への実データbridge、利用権のあるFXと企業行動、無料Geminiの二次利用を含む送信条件、新規隔離口座での手動検証は別段階。現在の合成時間足／FXで実データを埋め合わせない。paper-live・実証券注文・定期運用開始は対象外。

## 変更と回帰検証

- `.github/workflows/alpaca-readonly.yml`：専用Environment・main・手動限定、データexportなし。
- `config/alpaca-readonly.yaml`：20銘柄固定・221セッション・契約未確認の停止設定。
- `src/trading_bot/alpaca_research.py`：専用承認、送信前ガード、2pass品質検証、集計出力。
- `src/trading_bot/alpaca.py`：既存停止ゲートを保ち、client初期化だけを内部共通化。
- `scripts/alpaca_readonly.py`：専用entry point。一般CLIのProvider選択へは接続しない。
- `tests/test_alpaca_research.py`：新規45テスト。既存Providerと合わせた関連80件成功。
- `docs/phase6-readonly.md`・`README.md`：契約確認・操作・拡張条件と未実施範囲。

ローカル実行：Python全545件成功、PostgreSQL専用42件はローカルDB未構成のためスキップ。Webビルド・13テスト成功。uv sync --locked、Ruff check・format（117ファイル）、mypy（61ソースファイル）成功。PostgreSQL試験は既存GitHub CIの使い捨てDBで実行する。実通信Workflow自体の実行は今回の承認範囲外なので未実行。
