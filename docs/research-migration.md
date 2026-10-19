# 非公開研究保存migration：影響と手順

追加SQLは `supabase/migrations/20261019000100_private_research.sql`。今回は本番未適用。ユーザーの明示的承認を受けた所有者だけが対象プロジェクトのSQL Editorで全文実行する。

追加するのは `bot_research_private` schema のaccounts（最新ステージ状態）、observations（原本の内容別vintage）、runs（実行結果）とservice_role専用RPC 3個だけ。既存口座・注文・API枠・dashboard・RPCは変更しない。account名は新規 `test-integration-*` のみでpaper-liveを拒否する。

全テーブルにRLSとFORCE RLS。anon/authenticatedにはschema・table・RPC権限を与えずpolicyも追加しない。Data APIにschemaを公開しない。新RPCの実行はservice_roleのみ。service keyはGitHub Environment Secret等で管理し、公開Webへ渡さない。今回はSecretsを追加・変更していない。

run IDとrequest hashで再実行を照合し、account versionのCASとtransaction advisory lockで古いrunner・競合commitを拒否する。原本upsert、口座・注文・約定・指標・内部入力の保存は1 RPCの同一トランザクション。途中失敗は全rollback。ネットワーク応答を失った場合は自動再送せず、同じrun IDをbot_research_runで照合する。不明な場合は停止を維持する。

同じOHLCV等の内容は再取得時刻が変わっても重複保存せず、最初のfetched_atを保持する。価格が改訂された場合は別vintageとして保存し、過去指標を上書きしない。削除・保持期限・backupの運用は明示的な別承認が必要。50MB/commitの安全上限があり、無料500MB DBへ2,000銘柄の長期保管が収まる保証はしない。20銘柄の私的検証から容量・転送を測定する。

承認後の手順：既存の非公開バックアップと対象projectを確認→SQL全文を1回適用→anon/authenticated拒否とRPCだけのサービス接続を確認→新規隔離口座でread→明示的書込許可後に1 cycle。既存口座のリセット、既存schemaのdrop、公開projectionへの同期は行わない。ロールバックは新機能の呼出し停止を先に行い、保存済み研究データを勝手に削除しない。
