# Phase 5.5 手動検証と運用前チェック

全て手動。実データ取得は利用者の指示で停止中。`phase55-validation.yml` のprepareは新規隔離口座の初期化だけであり、HOLD判断の成功ではない。holdは金融mutation・認証・データ取得の前に拒否する。取得停止を解除する入力・環境変数は用意しない。

## 許諾確認後の段取り

`test-phase5-real-jp` と `test-phase5-real-us` を使用する。既存fixtureとpaper-liveは金融操作しない。各100万円、USD資金0で開始する。初回prepareは現在時刻で一度だけ行い、繰返しても資金・作成日時をリセットしない。

検証戦略案は既存のcontrolled mockを継承する。最初はHOLD、次の営業日に1銘柄の目標比率5%のBUY、後続実行で約定、保有継続を記録した後の営業日にSELL、さらに別実行で約定。既存OrderPlannerの上限・単元・手数料・スリッページを使い、1単元が買えなければ注文なしをそのまま記録する。USの仮想両替は明示的10万円上限、初回のみ、費用0.25%と台帳を維持する。この戦略をGemini判断と表示しない。実行前には許諾付きデータ一式・監査出力・管理者承認保護の実装確認が必要で、現段階で注文を承認するものではない。

許諾の条件が解決した場合の最短の候補日（現時点では予約・起動しない）：

| 段階 | JP | US（JST） |
| --- | --- | --- |
| mock HOLD | 2026-10-19 16:45〜17:15 | 2026-10-20 06:15〜06:45（NY 10/19） |
| 翌営業日のBUY登録 | 10/20 16:45〜17:15 | 10/21 06:15〜06:45（NY 10/20） |
| 後続の最初の確定足を検査・BUY約定 | 10/21 10:15以降 | 10/21 23:45以降（NY 10:45。15分遅延データの場合の取得猶予を含む） |
| 持越し・評価・SELL登録 | 10/21 16:45〜17:15 | 10/22 06:15〜06:45（NY 10/21） |
| 後続のSELL約定 | 10/22 10:15以降 | 10/22 23:45以降 |

データが間に合わない場合は停止し、未来価格で穴埋めしない。時刻を過去へ偽装して遅れた実行を通さない。上記は提供元の配信仕様を確認した後に再計算する候補である。`scripts/phase55.py --mode calendar --market JP` / `US` は現在時点から実カレンダーで次の候補窓を計算する。DST・短縮日でUTC時刻が変わる。

## 容量・転送量・API使用量

`phase55-monitor.yml` は独立した手動observer。自動復旧・外部通知・売買はしない。

- DB総容量・全世代容量と全口座共通のUTC日／月API予約数は、所有者がSQL Editorで `supabase/phase55-monitoring.sql` を実行して確認する。**SELECTだけ、DDLなし**。アプリData APIからprivateテーブルを直接公開して測定しない。
- `latest_scope_json_bytes` は読取った最新世代JSONのアプリ上の大きさであり、DB総容量・インデックス・過去世代・請求対象egressとは別。監査は未測定の総容量とegressをnull・未確認と表示する。
- Supabase DashboardのOrganization Usageで実際のプラン・請求期間・Database Size・Database Egress・Storage Egress等を確認し、確認日時と数値を非公開運用台帳へ記録する。[公式usage説明](https://supabase.com/docs/guides/platform/manage-your-usage/egress)。今回Dashboard権限・Management API tokenは利用していない。
- Freeの公開基準はDB500MB、egress5GB等だが、実プロジェクトの契約枠を推定しない。[公式料金](https://supabase.com/pricing)。運用開始前に実枠を確認し、利用率70%で警告・80%で新規収集停止を承認済み監視へ実装する。現実装がこの組織総量閾値を自動強制できるとは主張しない。
- 予約中・失敗したGemini通信も内部上限の日5・月100に含める。スコープ内監視と全口座共通SQLを区別する。予約RPCによる実行時の全口座共通制限は既存のまま。実請求／提供元の無料枠はAI Studioで別確認する。今回は実Gemini通信0件。

## 実行欠落・異常終了

`config/phase55-expectations.json` に、承認済みの各手動実行を `account, run_id, deadline`（timezone付き）の配列で登録すると、期限経過後に該当DB実行の欠落・非成功を検出する。現在は空配列であり、`NOT_CONFIGURED` と報告する。起動予定のないスロットを欠落と呼ばない。

GitHub APIで最新30件のPhase 5.5 Workflowのfailure/cancelled/timed_out/action_requiredを照合し、非0終了する。検査が成功しても、承認保護・容量・egress・backup未確認ならreadinessはNOT_READYのまま。停止ゲートを意図的に試すholdもfailureとして検知するため、報告書で意図した拒否と実障害を区別する。未解決DB実行を勝手に完了・再送しない。

手動observerは呼び出された時だけ検出する。GitHub全体の停止や、observer自身の未起動は同じActionsでは検出できない。定期運用前に独立監視の契約・通知先・動作を別途確認する。今回は通知・cron・外部サービスの定期監視を作成しない。

## 外部バックアップ・復旧

[Supabase公式](https://supabase.com/docs/guides/platform/backups)はFreeの定期的なCLI dumpとオフサイト保管を勧めている。同じDB内のPhase 5コピーは外部バックアップではない。

所有者が管理する非公開端末で、CLIの対応バージョンと `supabase db dump --help` を確認し、schema・roles・dataを別々に取得する。接続URIは環境変数等で渡し、公開コマンド履歴・Git・Actions artifactに置かない。データの例：

```powershell
supabase db dump --db-url $env:SUPABASE_DB_URL --role-only -f $env:PRIVATE_BACKUP_ROLES
supabase db dump --db-url $env:SUPABASE_DB_URL -f $env:PRIVATE_BACKUP_SCHEMA
supabase db dump --db-url $env:SUPABASE_DB_URL --data-only --use-copy -f $env:PRIVATE_BACKUP_DATA
```

ファイルは管理者が暗号化し、別障害領域へ保管する。checksum・取得時刻・対象DB・CLI版・暗号化鍵管理・保存期限を記録する。取込データのライセンスが定める削除義務はバックアップにも及ぶ可能性があるため、保管許諾を事前確認する。

通常のCLI dumpはSupabase管理下のauth・storage等を除外する。これだけでプロジェクト全体の復元を保証しない。アプリのbot_private/publicの復元対象を明示し、AuthやStorage実体・設定・鍵の別途復旧は[CLI公式仕様](https://supabase.com/docs/reference/cli/supabase-db-dump)に従って確認する。別DBへ復元するときのdefault privilegesも検査し、匿名・認証ユーザーへprivateデータが漏れないことを実測する。

復旧訓練は**別の使い捨てSupabase／PostgreSQL**で行う。対応するroles/schema/dataを公式手順に従って戻し、初期資金・口座作成日時・注文／約定ID・API予約数・データhash・台帳照合・RLS・古いtoken拒否を比較する。復元データから外部通信・注文を自動再開しない。検証できるまで元DBは変更しない。今回DB管理接続URI・保管先・暗号鍵がないため、実DBのdumpと外部復元訓練は未実施。CIのrollback/fencing試験を外部復元成功として数えない。

## GitHub Environment・企業行動

取引や復旧を許可する前に `phase55-validation` / `phase5-recovery` のrequired reviewersと保護branchを所有者が設定・確認する。Environment名の存在やbranch policyだけを承認保護と見なさない。現在のprepare/auditは金融操作のない手動準備で、取引jobは実装・有効化していない。未設定を勝手に設定済みとして扱わない。

株式分割・配当・併合等は、通信失敗を「イベントなし」にせず、未解決イベントを取得窓の外でも保持する既存検出を維持する。Providerを変更してもイベント通貨・権利落日・観測時刻・対象市場を検証し、保有／注文に影響する未対応イベントで安全停止する。自動の数量調整・配当入金は今回実装しない。
