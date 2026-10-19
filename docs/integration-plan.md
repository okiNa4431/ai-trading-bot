# 実市場統合：実装計画と承認境界

2026-10-19 JST。Phase 6（bc3713d）を基点とする。

再利用：Alpaca raw/IEXの日足検証、日次50→15選別、OrderPlanner、PaperBroker、Supabaseの口座CAS取引、NAV適用日・公表日検証。変更が必要なのは観測時刻付き実データbridge、非公開研究保存とCAS排他、外部AIへの送信禁止境界、内部入力契約、隔離口座サイクルの接続である。

独立したコミットに分ける。

1. AI送信境界：市場由来／不明の入力はSDK・キー読込・API枠予約より前に拒否。実SDK経路を停止し、合成データ＋明示的なSDK mockだけで既存応答検証を回帰する。
2. 非公開研究保存：追加専用migration、RLS・service_role限定RPC、run ID・account CAS・原子的snapshot保存。実本番への適用はしない。
3. 観測時刻付きbridgeと内部JSON：実価格・独立市場系列・FX・企業行動検証記録を明示入力し、欠損や未来データを拒否。JSON Schemaを生成・検証し、外部送信機能は付けない。
4. 隔離仮想口座の手動サイクル：既存broker/plannerを使い、注文と後続バー約定を分離。保存は非公開RPCで原子的に行い、mock/ルール比較とNAV比較を維持。fixture結合、全Python・PostgreSQL・Web、CIを検証する。

ユーザーが共有した提供元サポート回答は、個人・非商用・IEX・再配布なし・私的研究という限定的な利用根拠として扱う。回答本文・担当者情報・非公開証跡はコミットしない。一般向けの無制限許諾や、外部AI送信許可へ拡張しない。

今回実施するのはコード、mock/fixture、使い捨てPostgreSQL、CI、pushのみ。実Alpaca初回通信、実Supabase migration／書込、cron、実Gemini、実証券注文は実施しない。既存paper-live・API履歴・公開Webデータを変更しない。新規Workflowを自動起動する解除設定も加えない。
