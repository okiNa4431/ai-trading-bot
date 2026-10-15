# Phase 3 実装・検証報告

検証日: 2026-10-15（Asia/Tokyo）。Phase 2の `2174574` を基点とする。

## 1. 作成・変更ファイル

| ファイル | 内容 |
| --- | --- |
| `screening/{__init__,models,indicators,screener}.py` | UTC確定・取得済みデータの選別、指標、重み付き候補抽出 |
| `strategy/{__init__,base,models,prompt,mock,gemini,security}.py` | AI契約、厳密JSON検証、プロンプト、公式SDK、Secret除去 |
| `orchestration/{__init__,engine,order_planner}.py` | 評価・判断・数量計画・明示的注文登録 |
| `persistence/decision_repository.py` | 判断履歴・API使用量・利用枠予約 |
| `persistence/sqlite_repository.py` | 既存DBへ非破壊的にテーブル追加、同一トランザクションで保存 |
| `ai_cli.py`, `cli.py` | screen / ai decide / ai history / ai usage |
| `config/strategy.yaml`, `config/ai.yaml` | 指標重み・鮮度・モデル・通信上限 |
| `.env.example`, `pyproject.toml`, `uv.lock` | 空のキー例、公式SDK・dotenvと依存関係固定 |
| `scripts/test_gemini.py`, `.github/workflows/test-gemini.yml` | 手動限定・使い捨て口座・最大1通信のdry-run |
| `tests/test_{indicators,screener,gemini_strategy,order_planner,ai_engine}.py` | Phase 3回帰テスト |
| `tests/ai_helpers.py`, `tests/__init__.py`, `tests/conftest.py` | テストデータと実SDK通信の禁止 |
| `README.md`, `docs/architecture.md`, `docs/phase3-report.md` | 操作・計算式・設計・検証記録 |
| `web/src/main.tsx`, `web/tests/site.spec.ts` | Phase 3表示と既存公開ページの検証 |

Pythonの表中パスは `src/trading_bot/` 基準（tests等の明記したものを除く）。

## 2. スクリーニング

最低21確定日足・2確定時間足を使い、1時間/1日リターン、SMA5/20、Wilder RSI14/ATR14、相対出来高、20日年率ボラティリティ、平均売買代金を算出する。数式・正規化式は `architecture.md` に記載した。

トレンド・モメンタム・出来高・流動性・ボラティリティ・品質の6要因を設定可能な重みで合成する。各市場上位5件（設定可能範囲5〜10）へ同じ市場の保有銘柄を上限外でも追加する。他市場の保有資産もJPY総資産評価に含める。重みは研究上の仮説で、収益性の証明ではない。

評価時刻より未来、取得時刻が未来、取得時点の未確定バーは利用しない。未来データの異常値も過去の候補選定へ影響させない。時間足が連続していなければ1時間変化を捏造しない。NaN等、履歴不足、未調整価格の35%超急変は補正せず除外する。保有銘柄の指標不足はHOLD専用として理由付きで残す。

## 3. Geminiのモデル・接続

初期候補 `gemini-2.5-flash` は実接続でHTTP 404となった。[Google公式の利用制限](https://ai.google.dev/gemini-api/docs/deprecations)では2.5系を過去の利用者に制限し、新規向けに3.5 Flash-Lite等を案内している。404の原因本文はSecret保護のため出力しておらず、アカウント側の詳細理由までは断定しない。

[公式モデル情報](https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash-lite)と[公式料金表](https://ai.google.dev/gemini-api/docs/pricing)でモデルID・無料枠・構造化出力を確認し、設定を明示的に `gemini-3.5-flash-lite` へ変更した。thinking levelはminimal。実行時の自動モデル切り替えはない。

公式 `google-genai` のDeveloper APIを使用する。応答はBUY / SELL / HOLDと売買後目標比率だけを受け取り、数量はPythonで計算する。未知/重複/欠落銘柄、余分な項目、範囲外比率、売買方向との矛盾、不正JSON・JSON重複キーを拒否する。Secretは実APIステップだけへ渡し、SDKエラー本文を表示しない。

## 4. 実APIの判断例

2026-10-15 14:51:51 UTCの評価。JPの実データ2銘柄に対し、実Geminiが次の構造化回答を返した（下記実API runのSummary）。

| 銘柄 | 判断 | 目標比率 | 実際の理由文 |
| --- | --- | --- | --- |
| 6758.T | HOLD | 0.0 | 現金比率を維持するため、今回は新規保有を見送りホールドとします。 |
| 7203.T | HOLD | 0.0 | 市場の動向を慎重に見極めるため、新規のポジション構築は行わずホールドします。 |

市場コメントは「日本市場の候補銘柄はテクニカル指標において中立的な推移を示しており、現時点では慎重に現金比率を維持する方針をとります。」。これはAIの参考説明であり、事実や収益予測を保証するものではない。

注文計画は空、両銘柄のスキップ理由は `HOLD: no order`、登録注文0件。売買を強制しない仕様どおりである。実APIでBUY/SELLを引き出す追加通信はせず、それぞれの構造化検証・数量計算・登録はモックでテストした。

## 5. OrderPlanner

目標比率×JPY総資産と現在評価額との差から、JP100株単位・US整数株へ切り下げる。手数料・スリッページ・丸め費用を含め、1銘柄10%・株式80%・現金20%を買付後の値で検証する。

既存PENDING BUYと同時BUYの必要資金を累積予約する。同一銘柄の未約定注文があれば計画しない。SELL予定代金を買付余力へ算入せず、通貨別残高を守り、自動両替しない。SELLも小さな超過を単元へ切り上げて過剰売却しない。HOLDと未保有SELLは注文しない。

## 6. PaperBroker連携

既定はmock・dry-run。実Gemini利用と注文登録は別々の明示オプションである。登録時はDB保存済み計画を正本として、口座・未約定注文・ルール・価格/FX鮮度を再確認する。既存PaperBrokerを同じSQLiteトランザクションへ束縛し、一部失敗なら全注文登録をrollbackする。

判断ID由来の注文IDにより二重登録を防ぐ。登録時刻は実際の時刻とし、API待機中の価格で遡及約定させない。登録後はPENDINGで、executeを呼ばない。これらは単体・統合・並行登録テストで確認した。実証券会社への接続はない。

## 7. ローカル品質検証

| 検証 | 結果 |
| --- | --- |
| Python 3.12 / uv sync --locked | PASS、63パッケージ |
| pytest -v | PASS、195件（既存127件＋新規68件） |
| Ruff check / format --check | PASS、54ファイル整形済み |
| mypy src | PASS、36ソースファイル |
| Web型チェック・本番ビルド | PASS |
| Webブラウザテスト | PASS、4件（360 / 390 / 768 / 1440px） |
| 公開ページ実ブラウザ | PASS、HTTP 200、PHASE 03・準備中表示 |

全自動テストは実Geminiを呼ばない。実APIは手動Workflowだけで実施した。NumPyと市場カレンダー依存のDeprecationWarningがある（最終ローカル実行1608件）。テスト失敗ではないが、将来の依存更新時に再確認が必要。

## 8. Windows・Ubuntu CI

[CI run 38754977481](https://github.com/okiNa4431/ai-trading-bot/actions/runs/38754977481): SUCCESS。`7c56154` のWindows・Ubuntuで195件すべて成功。Ruff・整形・mypyとWeb型チェック・ビルド・4件のブラウザテストも成功。

## 9. GitHub Actions mock

[mock run 38755023854](https://github.com/okiNa4431/ai-trading-bot/actions/runs/38755023854): SUCCESS。JP2銘柄の実データを使い、HOLD2件、API通信0回、注文登録0件。Secretなしステップで実行した。

## 10. GitHub Actions実Gemini

- [初回2.5 Flash run 38754182388](https://github.com/okiNa4431/ai-trading-bot/actions/runs/38754182388): FAILED、HTTP 404、実通信1回、注文0件。失敗として保存・表示し、自動再送や代替モデル呼出しはしていない。
- [3.5 Flash-Lite run 38755152284](https://github.com/okiNa4431/ai-trading-bot/actions/runs/38755152284): SUCCESS。実通信1回、JP2銘柄、HOLD2件、注文0件、口座状態変更なし。実行コードは `7c56154`。

手動限定、保護されたmainのSHA固定、contents:read、実API最大1回、リトライ0、dry-run固定。一時口座100万円・実データ2銘柄だけを使い、口座・注文・株価を公開artifactへ保存しない。

## 11. トークン使用量

初回404はSDKからトークン情報が返らず、input/output/total/thoughtはすべてnull（不明）。mockはAPI使用量レコード0件。

成功した3.5 Flash-LiteのSDK返却値は入力1801・出力191・合計1992トークン。thought_tokensはnull（不明）であり0とは記録しない。API使用量レコード1件が保存され、Summaryへ表示された。

## 12. 無料枠・利用制限

既定のローカル制限は入力16000トークン相当バイト、出力2048トークン、45秒、日5回/月100回/判断1回、リトライ0。失敗・タイムアウトも枠を消費し、429で停止する。count_tokens等の追加API、検索・ツール・有料フォールバックはない。

無料枠の実際の割当・課金設定はGoogleのプロジェクトに依存する。アプリが無料プランを強制したり、課金設定を読み取る機能はない。ActionsのDBは実行ごとに破棄するため、日/月の使用量は実行間で共有されない。継続運用の利用枠管理はPhase 4の永続化後に設計する。

## 13. GitHub Pages

[公開サイト](https://okina4431.github.io/ai-trading-bot/)はHTTP 200で表示確認済み。[デプロイ run 38753780384](https://github.com/okiNa4431/ai-trading-bot/actions/runs/38753780384)はSUCCESS。Phase 3表示へ更新し、実際の運用成績は未公開・準備中の表示を維持した。APIキー、株価ファイル、口座DBをサイトへ含めない。

## 14. 修正内容・残存課題

- 候補モデルの404: 公式資料で現状を確認し、無料枠対応モデルへ明示的に設定変更。最終実API結果は上記参照。
- 未来の異常データによる過去の候補除外: 時刻による利用可能性判定を値検証より先に実施。回帰テストを追加し195件成功。
- AI待ち時間による遡及注文、古い候補での登録、改変された呼出元計画、並行二重登録、一括登録途中の失敗は、実登録時刻・再検証・DB正本・トランザクションで防ぎ、回帰テストを実行した。
- 株式分割・配当調整は未対応。急変は除外するが、すべての企業行動を検出する保証はない。
- 過去時点のfetched_atを持たない後日取得データを、過去に利用可能だったデータとして使えない。履歴不足・古い価格・非連続時間足では候補が減る。
- 100万円・JP100株単位・10%上限では最低単元を買えない銘柄がある。注文しない正当な結果として理由を表示する。
- Phase 4以降の定期売買、Supabase、オルカン実比較、実売買、継続的なWeb成績公開は未実装。

## 15. 最終判定

**PASS：Phase 4に進める。**

既存127件を含む195件、静的解析、Windows・Ubuntu CI、Web、GitHub Actions mock→実Geminiの順序検証、Pages公開が成功した。判断・数量・注文登録・約定を分離し、dry-runで口座を変更しないことを確認した。

これはPhase 3機能の完了判定であり、収益性や継続無人運用の検証完了ではない。Phase 4では口座・判断・API使用量を永続化し、実行間の予算管理を整えてから定期実行を設計する。
