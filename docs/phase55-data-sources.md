# Phase 5.5 無料データ提供元調査

調査日：2026-10-17 JST。個人・非商用・非公開ペーパートレードを想定する。料金表上の「無料」「API対応」は、クラウド保存・自動判断・第三者AI送信・公開の許諾と同義ではない。登録・有料契約・APIキー発行・実データ取得は今回行っていない。

## 結論

**日米の新鮮な時間足＋USD/JPY＋分割・配当＋継続的な非公開保存を、月額0円で全て満たす構成は確認できなかった。** 特に日本株の現在の時間足と企業行動の組合せが不足する。存在しないと断定せず、調査した候補では要件達成を立証できないという判定である。利用者からも個別許諾なし・取得停止継続の指示を受けたため、実市場データ取得は無効のまま維持する。

## 比較

「保存未確認」は保存禁止と断定する意味ではなく、今回のSupabase保管・監査履歴保持まで可能と確認できないことを示す。価格は無料プランだけを比較し、有料プランへ自動移行しない。

| 提供元 | 無料枠・対象 | 時間足・更新遅延 | 分割・配当 | API／非公開保存／継続利用の判定 |
| --- | --- | --- | --- | --- |
| Yahoo Finance / yfinance | 公式の保証された無料API契約枠を確認できず。JP・US・FXを既存実装が参照 | 技術的な時間足取得実績と契約上の権利は別 | 技術的に取得できても許諾の代わりにならない | 規約2の自動収集制限で事前許可が必要。個別許諾なしのため停止。[規約](https://legal.yahoo.com/us/en/yahoo/terms/otos/index.html?ncid=mbr_idnedulnk00000001)、[yfinanceの注意書き](https://github.com/ranaroussi/yfinance#legal-stuff) |
| J-Quants Free | JP、5件/分。直近12週間を除く過去2年 | 無料は日足。分足・tickは無料に含まれない | 調整係数の用途と配当イベントは別。配当金データは無料に含まれない | 個人自身の利用とAPI契約を明記。無料契約は1年で自動終了、終了時に複製含む削除義務。Supabase・外部AI・原本を含む不変監査との適合は追加確認が必要。[料金・FAQ](https://jpx-jquants.com/ja)、[規約6〜8条](https://jpx-jquants.com/en/termsofservice)、[データ期間](https://jpx-jquants.com/en/spec/data-spec) |
| Twelve Data Basic | 8 API credits/分、800/日。US株・FX等。日本株が無料対象とは確認せず | API仕様に1h・5min等。BasicのUSデータを全市場統合と仮定しない。FXの時刻・バー確定を個別検証する | 当該2銘柄の分割・配当がBasicで継続取得可能か未確認 | Basicの内部non-display、規約2.2の内部処理・保存を確認。保存期間は契約・原提供元条件に従い、終了時削除も必要。外部AI送信は未確認。接続コードのみ準備し、未有効化。[料金](https://twelvedata.com/pricing)、[規約2・3・12・16](https://twelvedata.com/terms)、[API仕様](https://twelvedata.com/docs) |
| Alpaca Basic | US株、200リクエスト/分。Paper口座でもBasic無料 | IEXのリアルタイム、SIP履歴の直近15分制限。IEX出来高を全市場出来高と比較しない | 公式corporate actions機能あり。対象プラン・保管条件は要確認 | 個人非商用の規定はあるが、参照PDFには米国居住者向けの記述もあり、日本居住者の適用契約・クラウド保存範囲を確定できず。Provider未実装。[データ仕様](https://docs.alpaca.markets/us/docs/about-market-data-api)、[FAQ](https://docs.alpaca.markets/us/docs/market-data-faq)、[規約PDF](https://files.alpaca.markets/disclosures/library/TermsAndConditions.pdf)、[企業行動案内](https://alpaca.markets/support/what-are-corporate-action-announcements-and-how-can-i-access-them) |
| Massive Stocks Basic | US、5件/分、過去2年 | 無料はEOD配信。過去minute aggregatesはあっても、新鮮な時間足が無料という意味ではない | 料金表でCorporate Actionsを含む | 市場データ規約5(d)はnon-display・投資戦略等に別の権利確認を要求、8条は終了時データ削除。無料APIの存在だけでBotへ採用しない。[無料枠](https://massive.com/pricing?product=stocks)、[個人規約](https://massive.com/legal/individuals-terms-of-service)、[市場データ規約5・8条](https://massive.com/legal/market-data-terms-of-service) |
| Alpha Vantage | 通常25 API要求/日。US・FX等。対象JP銘柄の現行提供を未確認 | 株式intradayとFX_INTRADAYはPremium。無料キーを作れば時間足も使えるとは判断しない | 個別endpointの無料権限・網羅性は未確認 | 個人非商用の分析・研究・テストを明記。非公開クラウドの保持期間まで確認できず、今回の時間足要件も未達。[無料枠](https://www.alphavantage.co/support/)、[endpoint仕様](https://www.alphavantage.co/documentation/)、[規約](https://www.alphavantage.co/terms_of_service/) |
| ECB / ESCB | 参照FX統計、キー不要、無料再利用方針。EUR/USD・EUR/JPYからUSD/JPYを計算可能 | 営業日の日次参照値。通常16:00 CET頃公表。時間足・正確なquote timestampはない | 対象外 | 出所表示・正確性・加工表示を条件にした利用方針を確認。日次研究用コードのみ準備。取得日時を価格時刻にせず、売買用の鮮度1時間検証へ渡さない。[再利用方針](https://www.ecb.europa.eu/stats/ecb_statistics/governance_and_quality_framework/html/usage_policy.en.html)、[著作権](https://www.ecb.europa.eu/services/using-our-site/disclaimer/html/index.en.html)、[更新仕様](https://www.ecb.europa.eu/stats/policy_and_exchange_rates/euro_reference_exchange_rates/html/index.en.html) |
| 日本銀行 | 外為市況等の日次統計。公式統計APIのマニュアルあり | 連続する5分足・1時間足の代わりにはならない。対象系列の公表遅延・時刻も別検証が必要 | 対象外 | 非商用複製には出所表示等の条件、無断改変禁止。日銀本体と統計検索サイトの適用規約を一括と見なさず、今回の自動取得・クラウド保持・加工は未確認のまま。[著作権](https://www.boj.or.jp/about/copyright.htm)、[公式API仕様PDF](https://www.stat-search.boj.or.jp/info/api_manual.pdf) |

## 月額0円を優先した組合せ案

1. **過去データの研究候補**：日本株はJ-Quants Free、米株はTwelve Data Basic等の履歴権限確認、参照為替はECB。12週間遅延・日次の制限を明示する。これはPhase 5.5の実時間検証を完了する構成ではない。Massiveも無料だからバックテスト可能とは推定せず、non-display許諾が解決するまでは除外する。契約と保存期間の確認後にだけ利用する。
2. **米国だけの実時間検証候補**：US価格はTwelve Data BasicまたはAlpaca Basic、USD/JPYはTwelve Data Basic。分割・配当は別の無料APIの権限・網羅性と検知遅延を確認する必要がある。IEX等の限定市場を使う場合は、出来高の基準を全市場のものから黙って流用せず、別の検証済み設定が必要。
3. **日本を含む実時間検証**：日本の現在時間足・企業行動を0円で許諾付き提供する候補が未確定。日次／遅延への仕様変更か、有料データ予算の別判断が必要になる可能性がある。今回は費用0円と既存の鮮度条件を維持し、取得を停止する。

外部AIへの送信は、内部保存の許諾だけから推定しない。契約終了時の削除義務があるデータを、原本入りの不変世代・AI context・バックアップへ無期限保存する設計もそのまま採用しない。原本の保持範囲と、残せる非復元可能な監査情報を提供元に確認してから設計する。既存の口座・API履歴を今回削除することはない。

## 準備した接続範囲

`reference_fx.py` にTwelve Data FXとECB日次参照の選択ファクトリ・厳格なパーサを追加した。前者は確認できた内部API／保存権限の範囲を候補とし、契約・保持条件の最終確認後に使うための準備である。後者は研究用のdate精度モデルで、売買用FXProviderとして扱わない。いずれもネットワーク経路は停止ゲートで拒否し、取引runnerには接続していない。Yahooや未審査の名前をファクトリへ渡しても選択できない。

日本株・US株・企業行動の新Providerは、今回確認できなかった権利を推定して実装しない。将来の切替時は `PriceBar.source`、Featuresの出所、パーティションキー、バー時刻、取引所範囲、調整有無を一括で設計・検証する。Yahooのラベルのまま他社データを格納しない。
