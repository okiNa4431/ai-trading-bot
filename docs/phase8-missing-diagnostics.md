# Phase 8：欠損診断と検査状態

診断のみの追加。100銘柄・221営業日・IEX/raw/asof=-、ページ・要求・時間上限、完全性検証、fail-closed条件は変更しない。実通信・保存・売買・AI送信は追加しない。

## 公開企業行動の参照

Marshの[2025年12月29日公式発表](https://www.marsh.com/en/corp/about/news/marsh-mclennan-to-change-nyse-symbol-to-mrsh.html)により、MMC→MRSHの適用日は2026年1月14日。同社の説明では普通株のNYSE上場とCUSIPは継続する。`research_diagnostics.py` にこの出所と適用日を管理する。これは発行体公表の変更情報であり、Alpacaがどのように履歴を対応付けるかの確認ではない。将来の情報を自動収集・承認する仕組みは追加しない。

## 集計フィールド

- `missing_pattern_symbols`：欠損のある銘柄の分類件数。`leading` は先頭だけ、`trailing` は末尾だけ、`interior_or_mixed` は途中または複合、`entire` は全期間。1銘柄を1分類に数える。
- `prefix_30_trailing_191_symbols`：221営業日のうち最初の30日だけが揃い、残り191日が欠ける銘柄数。単に30行あっただけでは一致しない。
- `known_change_boundary_matches`：ページ取得が完了したバッチ内のMMCについて、適用日前の全想定日だけが揃い、適用日以降の全想定日が欠ける場合に数える。新ティッカーの存在・配信品質を確認した意味ではない。
- `diagnostic_basis`：`complete_response` は終端ページまで処理済み、`partial_response` は途中停止、`unavailable` はバッチなし。途中停止時の分類は未受信部分も含むため、上場期間やIEX取引有無の結論には使わない。

銘柄名、個別の取引日、価格、OHLCV、認証情報は診断JSONに含めない。集計が特定の公表イベントと整合することは示すが、生の履歴は出さない。artifact/cache/DBには保存しない。

## 検査状態

`checks.paging` と `checks.completeness` は `[1回目, 2回目]` の配列。`checks.discontinuity`、`checks.repeat_consistency` は後続検査の状態。

- `PASSED`：該当検査を実行して通過。
- `FAILED`：該当処理を開始したが完了・通過できなかった。
- `NOT_RUN`：まだ実施していない。

`paging=FAILED` はページ処理が完了していない意味で、トークン異常だけでなくHTTP失敗やページ内の不正データも含む。`completeness` は想定営業日の網羅性検査。企業行動対応済み・戦略投入可能を意味しない。

例えば、初回で191日不足なら以下になる。これは合成テストの期待値であり、実市場での原因確定ではない。

```json
{
  "checks": {
    "paging": ["PASSED", "NOT_RUN"],
    "completeness": ["FAILED", "NOT_RUN"],
    "discontinuity": "NOT_RUN",
    "repeat_consistency": "NOT_RUN"
  },
  "discontinuities": null,
  "repeat_revisions": null
}
```

件数0は実施して該当なし、nullは未算定。後続検査は欠損解決後に別の問題で停止する可能性がある。既存ログの0を遡って「検査済み」とみなさない。

## ユーザーによる再実行

mainのCI成功後、既存の **Phase 8 Alpaca 100-stock read-only validation (manual)** をmainから `READ_ONLY_100_NO_STORAGE` で起動する。EnvironmentとSecrets・第2ゲートは既存どおり。Codexは起動しない。

欠損が再現した場合は、`diagnostic_basis`、末尾欠損の銘柄数、30/191パターン、変更日一致件数、検査状態を確認する。実行日が変われば221日窓も移動するので、30/191が0でも変更日一致が残る場合がある。失敗を成功に変更したり、MMCの削除・MRSHへの置換・履歴結合・バー補完をしたりしない。

## 将来の証券同一性設計案（今回未実装）

企業と証券と上場を分離し、内部の不変な `security_id` を株式クラス単位で付与する。発行体ID・取引所別listing IDは別に持つ。ティッカーはIDとして使わず、提供元・市場・通貨と有効期間 `[開始, 終了)` を持つ別名として管理する。CUSIP/ISIN等も単独の永続IDとみなさず、有効期間と出所・確認状態を持たせる。

変更イベントには、公表日・適用日・出所・承認者・版を記録する。旧新ティッカーの同一証券性は手動レビューを通し、期間重複・欠落・複数候補なら停止する。過去時点で知り得た情報の時刻も保存して先読みを防ぐ。分割・配当・分社化は別イベントとしてraw観測と調整系列を分離する。発行体の変更情報だけで提供元の履歴を勝手に結合しない。今回のAPI指定やDBスキーマは変更しない。
