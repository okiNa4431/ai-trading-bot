# Phase 5 migrationの影響範囲

対象は `supabase/phase5-upgrade.sql`。既存Phase 4 DBに対する追加専用の、BEGIN/COMMIT付きSQL Editor用スクリプトである。新規DB向け全体bootstrapとは分ける。

追加内容：

- 非公開の実行履歴・停止制御・イベント・データ世代テーブル4個。test-phase5口座だけで利用する。
- 既存accounts/records/balances/positionsの適用前スナップショット4個。元テーブルを変更せず、private schema内に一度だけ複製し、RLSと権限剥奪で公開しない。同一DB内の確認用コピーであり、外部災害対策バックアップの代用ではない。
- サービスキー専用のclaim/step/fenced commit/共有API予約/データ保存/読取/照合RPC。匿名・通常ユーザーへ実行権限を与えない。
- 既存accounts/recordsへのガードtrigger。test-phase5-*の既存経路からの書込みも、有効なlease/fencingなしでは拒否する。paper-liveの注文・約定・両替・資金変更を拒否する。既存dry-run判断・共有API予約の仕組みは維持する。
- 適用前データが一致するかをboolだけで返す検証RPCと、PostgRESTのschema再読込通知。

元口座・元台帳・判断・API使用量に対するDELETE/TRUNCATE/初期化/書換えSQLは含めない。元のAPI枠関数と会計commit関数は置換しない。新しいwrapperの内部で元関数を呼ぶため、会計検証と共有枠を維持する。スケジュールや注文を作るSQLもない。

運用上の変更は、paper-liveの金融mutationと、test-phase5口座の無排他mutationが拒否されること。現在の目的である「継続口座は売買しない」に対応する。既存口座の金融操作を再開するときは別途検証・承認が必要。

DDL適用はDB所有者/SQL Editorの権限を要する。Data APIキーでは実行しない。適用中は他の手動売買Workflowを起動しない。所有者が必要な外部バックアップの状態も確認したうえで、対象プロジェクトのSQL Editorで全文を1回実行する。失敗した場合は同トランザクションをロールバックし、エラーを確認してから対応する。

適用後に最初に行うのはread-only healthcheckと非公開スナップショット比較である。実Geminiやpaper-liveの注文処理を起動しない。その後、test-phase5-fixture-jp/usだけで明示的な合成データの複数手動実行を行う。
