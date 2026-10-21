# JP-2.1 Windowsローカル読取準備

確認日: 2026-10-21。**読取実装済み・実データ検証待ち**。今回、実API通信は実施していません。実市場の利益実績ではありません。JP-1の比較は引き続きSYNTHETICです。

## 事前準備と承認

[J-Quants公式クイックスタート](https://jpx-jquants.com/ja/spec/quickstart)から本人の個人アカウントを登録し、Freeを選択、ダッシュボードでV2 APIキーを発行します。有料プランは不要です。本人の契約、[規約](https://jpx-jquants.com/ja/termsofservice)、[利用FAQ](https://jpx-jquants.com/ja/help/usage)を確認してください。キーをCodex・チャット・Issuesへ貼らないでください。

今回は準備のみです。`config/jquants-local-probe.yaml` の `local_live_enabled` は **false** のままです。初回実通信の追加承認を得た後、ローカル専用設定をtrueにする変更をレビューしてから実行します。既存GitHub Actions設定の `live_enabled: false` は変更しません。環境変数だけでは現在の停止ゲートは解除されません。

固定対象は既存20普通株、2025-01-06〜2025-12-30、243 JPX営業日（期待4,860行／回）です。対象や日付を自動変更しません。基礎設定全体の正規化SHA-256をローカル設定と照合し、意図しない変更を拒否します。基礎設定とローカル設定の確認期限は2026-11-20。期限切れ時は公式仕様・Free期間を再確認してレビュー更新が必要です。ハッシュだけを書き換えて検査を迂回しないでください。

## Windows PowerShellでの操作（追加承認後）

本人の信頼できるWindows PCの対話コンソールだけを使用します。CI、リモート共有サービス、GitHub Actionsからは実行できません。同一アカウントの他PC・ノートブック・ツール・キーによる取得を停止してください。APIでは他クライアントの使用量を照会できないため、この確認は本人が行います。

リポジトリのルートで依存関係を準備します。

```powershell
uv sync --locked
```

キーを履歴に残す直接代入や`.env`保存を避け、非表示入力を使用します。環境変数一覧を表示しないでください。

```powershell
$jqSecure = Read-Host 'J-Quants API key（非表示）' -AsSecureString
$jqPtr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($jqSecure)
try {
    $env:JQUANTS_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($jqPtr)
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($jqPtr)
    $jqSecure.Dispose()
    Remove-Variable jqSecure,jqPtr -ErrorAction SilentlyContinue
}
$env:JQUANTS_PLAN = 'Free'
$env:JQUANTS_TERMS_CONFIRMED = 'true'
$env:JQUANTS_ACCOUNT_IDLE_CONFIRMED = 'true'
$env:JQUANTS_LOCAL_LIVE_ENABLED = 'true'
Remove-Item Env:JQUANTS_MARKET_DATA_LIVE_ENABLED -ErrorAction SilentlyContinue
uv run python scripts/jquants_local_probe.py
```

非表示の承認プロンプトに `READ_ONLY_JQUANTS_LOCAL_20_NO_STORAGE` を入力します。GitHub Actions用の文字列は拒否します。承認文字列をパイプやコマンド引数から渡す方式は用意していません。端末のトランスクリプトやデバッグ記録も無効にしてください。プロセス環境変数はHTTP認証に必要な平文を保持するため、共有PCは使用しません。

## 制限・所要時間

共有Providerを使い、HTTPS GETの固定J-Quants日足エンドポイントだけに接続します。認証・対象期間・コード・ページング・品質検査・2回比較は既存JP-2と同じです。20銘柄を同条件で2回取得。最初に61秒待機、その後リクエスト間隔21秒以上（Freeの5回/分より保守的）。ページ当たり最大3試行、各銘柄各取得最大2ページ、全体最大100リクエスト、35分の上限を維持します。通常40要求なら約15分＋応答時間。再試行やページ追加時は長くなり、上限で停止します。

WindowsのGlobal名前付きmutexで同一PC内のこのプローブを多重起動できなくします。ロック権限エラー時も停止し、弱いロックへ切り替えません。異常終了後の再起動も初回61秒待機します。ただし他デバイスや別ツールは制御できません。アカウント全体の同時利用を止める自己確認は必須です。

生データはプロセスメモリのみ。ファイル・DB・Supabase・artifact・cache・Pagesへ保存せず、Geminiその他AI、EDINET、売買・約定・評価にも接続しません。診断専用型の下流拒否を維持しています。終了でプロセスメモリを解放します（OSのスワップやクラッシュダンプまで安全消去する保証ではありません）。

## 結果と障害対応

標準出力は集計JSONのみです。件数、欠損・重複・不正値、企業行動候補、隔離・UNKNOWN、要求数、HTTPステータス件数、再試行、2回比較、各検査のPASSED/FAILED/NOT_RUNを確認します。個別OHLCV、履歴、内部指紋、レスポンス本文、キーは表示しません。

`READ_ONLY_COMPLETE` でも戦略承認はfalseです。再取得一致は再現性だけを示し、過去の企業行動会計や当時利用可能だったデータの正しさは証明しません。UNKNOWNは未確認のまま保持します。`STOPPED`、認証失敗、欠損、不連続、調整係数の問題では売買へ進まず、集計のみで原因を調査してください。欠損補完、銘柄除外、検査緩和はしません。自動再送はなく、再実行は契約・期間・他クライアント停止を再確認して本人が承認します。Ctrl+C時も集計STOPPEDとなり、通信済み要求は取り消せません。

終了後は親PowerShellの環境変数を削除し、ウィンドウを閉じます。子Pythonから親のキーは消せません。

```powershell
'JQUANTS_API_KEY','JQUANTS_PLAN','JQUANTS_TERMS_CONFIRMED',
'JQUANTS_ACCOUNT_IDLE_CONFIRMED','JQUANTS_LOCAL_LIVE_ENABLED' |
    ForEach-Object { Remove-Item "Env:$_" -ErrorAction SilentlyContinue }
```

漏えいが疑われる場合はダッシュボードでキーを失効・再発行してください。キー値を含むログを送らないでください。仕様・料金・契約は変更され得ます。保存や公開、外部AI送信の許諾へ今回の本人研究を一般化せず、将来の用途は別途レビュー・承認します。公式仕様と未解決事項は `jquants-free-review.md`、固定銘柄と既存診断は `jquants-readonly.md` を参照してください。
