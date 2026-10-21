# JP-2：J-Quants Free読取20証券の操作・設計

[公式仕様・利用条件レビュー](jquants-free-review.md)を先に読む。2026-10-21現在、実通信0回、コードの通信フラグfalse。CI成功だけでは有効化されない。

## 実装

`src/trading_bot/jquants.py` はV2専用Provider。USのIEX/NYSE型や合成IDを流用しない。JPY/JP/日足の内部観測型でrawとadjustedを別管理し、取得条件・取得時刻・観測可能時刻を保持する。既存JPXセッションと診断専用型の拒否を再利用し、`PriceBar`や売買入力への変換APIを設けない。

20証券を1コードずつ、固定期間2025-01-06〜2025-12-30（JPX 243営業日、4,860行/回を期待）で2回読み取る。4桁codeで普通株のみを指定し、返却Codeを固定照合。期間外・休場日・未知コード・スキーマ不正・認証失敗・ページ破損はバッチ停止。欠損・重複・順序逆転・OHLCV不正・未確認企業行動・35%超不連続等は証券別隔離として集計し、他証券と分ける。隔離があっても、バッチ形式が安全なら同条件の2回目まで診断する。rawも調整済みも修正・補完しない。

調整済みを含む比較対象フィールドの内部指紋を2回の観測で比較する。指紋自体も出力しない。差があれば初回値を上書きせずSTOPPED。表現形式だけの差も保守的に差分扱いになる可能性がある。永続化しないため別Workflow間の訂正追跡は未対応。

公表時刻は未提供なので利用可能時刻=`fetched_at`、根拠=`observed_by`。当時の配信履歴や過去判断時点のデータを復元できたとは扱わない。品質異常がなくても歴史的同一性・企業行動レビューはUNKNOWN、ELIGIBLE=0、`strategy_approved=false`のまま。

## 通信予算

- GET・HTTPS・`api.jquants.com/v2/equities/bars/daily`だけ。redirect・proxy環境変数・HTTPTransport内部再試行は無効。
- 21秒以上の間隔（ローリング60秒で最大3回）。429/5xx/通信障害は各ページ最大3試行、バックオフ21/42秒。無制限の再試行やRetry-After待機は行わない。
- 1コード1回あたり最大2ページ、2取得で最大80正常ページ、再試行を含め最大100要求。通常1ページずつなら40要求・約13分39秒＋応答時間。公開仕様は固定ページサイズを保証していないため、2ページは実行上の安全上限であり完了の仮定ではない。3ページ目が必要なら停止し、勝手に上限を増やさない。
- 全体35分、HTTP timeout最大10秒、1ページ2MiB、Workflow40分。待機前・要求前後・本文読取り時に時間を確認。上限超過は停止。
- 公式Free上限5回/分はアカウント合算。他の端末/キー/ジョブを並列起動しない。同一Workflowはconcurrencyで直列化する。

## ユーザーが今準備できる設定

1. [公式クイックスタート](https://jpx-jquants.com/ja/spec/quickstart)から個人登録し、**Free**を購読する。有料プラン/アドオン/カード登録は不要。ダッシュボードでAPIキーを発行する。値をチャット・Issue・ソースへ貼らない。
2. GitHubリポジトリ → Settings → Environments → `jquants-research`を作成。Deployment branchesをmain限定にする。可能ならrequired reviewersと自己承認禁止も設定する。
3. Environment Secret **`JQUANTS_API_KEY`**を登録する。Repository全体やPages用の環境変数へ置かない。
4. Environment variables **`JQUANTS_PLAN=Free`**。**`JQUANTS_MARKET_DATA_LIVE_ENABLED=false`**のまま、**`JQUANTS_TERMS_CONFIRMED`**は未確認なら未設定/falseとする。

CodexはこれらのEnvironment/Secretsを設定していない。

## 将来の最初の手動実行（今回は起動しない）

まず、ユーザーが実通信を別途承認し、適用規約・Free購読と本人限定のActions利用を確認する。その後に限って、レビュー付き変更で `config/jquants-readonly-20.yaml` の `live_enabled` をtrueにする。現時点ではfalseのため、Environment変数を変えても通信しない。

承認後、Environment variablesを **`JQUANTS_TERMS_CONFIRMED=true`**、**`JQUANTS_MARKET_DATA_LIVE_ENABLED=true`**に設定し、Actions → **JP-2 J-Quants Free read-only 20 (manual)** → Run workflow → main → 承認文字列 **`READ_ONLY_JQUANTS_20_NO_STORAGE`** を完全一致で入力する。

main・workflow_dispatch・GitHub-hosted・専用Workflow ref・専用Environment・Free自己確認・契約確認・コードフラグ・期間/レビュー期限・APIキーがすべて必要。別Workflowや通常CLIへの解除経路はない。プラン種別を返すAPIは今回呼ばないため、実際の購読がFreeであることは本人確認事項であり自動認証済みとは表示しない。

設定変更後も将来のCIはモック試験だけ。Codexはこの実通信Workflowを起動しない。

## ログの読み方・復旧

`scope`、2取得それぞれの`paging`/`quality`、`repeat_consistency`はPASSED/FAILED/NOT_RUNを区別する。未実施の品質値や再取得差分はnull/空配列で、0件成功に置き換えない。

公開するのは対象数・営業日数・受信件数・欠損/重複/異常/Null件数・候補イベントの種類別件数・隔離/UNKNOWN数・HTTPステータス件数・要求/ページ/再試行数・時間など。価格、出来高、銘柄別の異常日付、本文、認証ヘッダー、内部指紋は出力しない。

- `READ_ONLY_COMPLETE`：2回の読取と定義済み品質検査が完了。売買・AI・保存の許可ではない。
- `STOPPED`：固定理由コードと集計を調査。認証不備や期限は設定/契約を確認する。429等では他端末を止め、API状態を確認してから手動判断する。
- 欠損/Null/企業行動/差分は銘柄削除・補完・調整で成功にしない。必要な公開情報や提供元仕様の調査は別レビューにする。自動再送はしない。

終了時にHTTP clientを閉じ、観測を外へ返さない。DB・artifact・cache・Pages・AI・注文経路なし。メモリ中の市場データはプロセス終了で破棄。既存Supabase/米国Workflow/JP-1は維持。

## 次の段階

実読取は未検証。別Workflowを跨ぐ保存・再観測の版管理は未実装。JP-3はEDINET DBの契約、財務データ公表/取得/訂正時刻、発行体と証券ID対応を別途確認する。JP-4の外部AI送信、JP-5の企業行動会計・実NAV比較・公開許諾、JP-6の定期運用はいずれも無効。
