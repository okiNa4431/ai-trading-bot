# JP-3 財務読取基盤（MOCK_INTERFACE_ONLY）

2026-10-21。実財務APIは未接続。株価・財務・NAVの実データ保存、Gemini送信、実データ売買、cronは有効化しない。[調査](edinetdb-free-review.md)を先に参照。

## 今できること

リポジトリ直下のWindows PowerShellで以下を実行できる。キー不要、環境変数の値を出力せず、HTTPクライアントも作らない。Windows以外のCIでも同じオフライン診断だけを検証する。

```powershell
uv run python scripts/edinetdb_local_probe.py --diagnose
uv run pytest tests/test_financials.py -v
```

status=MOCK_INTERFACE_READYは開発用インターフェースの準備完了だけを意味する。target_count=20、issuer_mapping=UNKNOWN、requests=0、live_adapter=NOT_IMPLEMENTED、external_ai=false、strategy_approved=falseを確認する。通常実行・--live等は拒否する。EDINETDB_LIVE_ENABLEDやJ-Quantsのキー/許可を設定しても通信できない。

`EdinetDBProvider.fetch()`はHTTP初期化前に常に拒否する。`FixtureFinancialProvider`は厳密にMockTransportだけを受け付け、fixture.invalidの内部SYNTHETIC契約だけを処理する。実EDINET DB API仕様の検証済み実装と混同しない。最大60要求・2ページ・各要求3試行・120秒の合成境界試験、401/403即停止、日次上限型429も再送せず停止。5xx/timeoutは最大2回再試行。本文・キー・数値をログへ出さない。実アカウント枠の永続的な管理は未実装。

## データモデルと先読み防止

Issuer/IssuerLink/IssuerNameで企業と証券・社名の有効期間を分離。複数証券→同一企業は許容するが、同一証券の期間重複や別企業への曖昧な対応は拒否する。JP-2の20証券は対象メタデータだけを再利用し、実企業のEDINETコードを推測して登録しない。対応根拠には既存ReviewFactの確認日時・対象期間・出所を再利用し、当時未確認ならUNKNOWN。

FinancialStatementは会計期間、実績/予想、連結/単体、会計基準、通貨、指標ごとの単位、文書・改訂ID、出所、公表日/時刻精度、配信時刻、取得時刻、改訂利用可能時刻を別々に保持する。日付だけの公表情報から時刻を捏造しない。NULL/未提供は欠損理由を持ち、0へ置換しない。内部成長率は式と入力証拠IDを持ち、企業・期間・単位・基準が異なる計算は拒否する。ROE等のprovider値と独自計算値を混同しない。

`known_at`は情報の利用可能性検査であって売買承認ではない。`select_as_of`は取得・公表・改訂時刻が判断以前に確認できる版だけを選び、訂正前の観測を保持する。同一版の内容衝突、訂正の分岐・循環・証拠不足は拒否またはUNKNOWN。2026年に初取得した系列を2025年の観測へ流用しない。過去版を復元できなければ過去AI成績検証は未承認。将来の判断ログを保存する段階では、別承認のもと、取得時点の不変スナップショット・採用版・候補選定時刻を記録する。

FinancialStatementは既存DiagnosticOnly型とpurposeを持ち、実財務のオブジェクト・入れ子・辞書化後もAI/注文/約定/評価/PrivateResearchStoreから拒否する。合成専用入力はsynthetic ID・出所・証拠・版・時刻・許可を検証する。合成スキーマJSONを実Geminiへ送信する機能も追加しない。

## 登録と次の承認

EDINET DBを使う選択をする場合、本人が[開発者ページ](https://edinetdb.jp/developers)でFree登録し、自分のダッシュボードからAPIキーを発行する。Freeはカード不要。キーをチャット・ソース・.env等へ貼る必要はない。今の診断にはキーの設定自体が不要で、発行を急ぐ必要もない。J-Quantsだけで検証する場合は新規EDINET DB登録不要。

実取得開始手順は、(1) provider/対象仕様・本人契約確認、(2) 実アダプタと独立した既定falseゲート・Windows排他・残量監視のレビュー実装、(3) ユーザーの実取得追加承認、(4) ユーザー自身のメモリ内読取検証、の順とする。今回は(2)以降を実施していない。隠れた実行許可や有料フォールバックはない。

JP-4の条件は、財務出所と証券対応・公表版・単位の検証、第三者AIへの必要最小限送信とGoogleの保持/学習条件の確認、JSON応答検証・予算・決定論的売買との分離のレビュー、明示承認。JP-2の価格不連続2件は未解決で、18証券も売買可能とは認定しない。実オルカン適用日・権利・比較公平性の確認もJP-5で必要。
