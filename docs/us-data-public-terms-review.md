# 公開規約だけによるUSデータ利用条件の再調査

調査日：2026-10-17 JST。対象：個人・非商用の自作ペーパートレード、US株5銘柄の15分足、USD/JPY、毎時Gemini、非公開Supabase保存、継続利用、可能なら運用成績公開。公開されている公式規約・料金・FAQ・API仕様のみを調べた。問い合わせ、登録、キー取得、市場データAPI取得、Gemini呼出、DB操作、定期起動は行っていない。

## 結論と前回報告の修正

**全要件を月額0円・追加個別許諾なしで満たす構成は、今回確認した提供元からは確定できなかった。** サービスが存在しない、全ての利用が禁止、メールが必ず必要、という結論ではない。主な未解決点は、無料Gemini側の二次利用を伴う情報送信と、データ・派生結果の保存／公開範囲である。

一方で、通常のAPI取得や内部処理まで一律に個別メール必須とする根拠もない。以下を修正する。

- Twelve DataはBasicの内部non-displayと、規約上の内部処理・保存を明示。一般的な内部保存許可があるのに「Supabaseという製品名が書かれていない」ことだけで個別許諾必須とはしない。
- Alpacaは公式に世界中からPaper Only Accountの作成を認め、無料IEXによる試験を案内する。古い一般PDFのUS resident記述だけを根拠に、日本居住者のPaper Only登録まで未確認とするのは不十分だった。登録の一般許諾と、今回のクラウド保管・外部AI利用の範囲は別。
- 両社とも公式AI連携がある。AI利用そのものを一律禁止とは判定しない。ただし、全プランで第三者の学習・再利用まで許諾する契約とは限らない。
- 標準のアカウント規約・市場データ契約への同意は、追加の個別メール交渉と区別する。メールは現時点で必須手順としない。

## 判定方法

○＝該当する許可・提供の根拠あり（明示条件を守る）。×＝追加権利なしでは明示的に禁止、または無料プランでは提供しない。△＝今回の具体的用途に適用できるか公開文書から確定できない。×の「提供なし」と「契約上禁止」は理由を分ける。料金表はAPI権限を示すが、再利用権の全てを示すものではない。

## 主要候補

| 提供元・無料プラン | 自動取得・15分足 | 非公開クラウド保存・継続 | 無料Geminiへ最小情報送信 | 派生運用成績の一般公開 | 判定 |
| --- | --- | --- | --- | --- | --- |
| Twelve Data Basic | ○ US株・FXのAPI、8 credits/分・800/日。15minはAPI仕様にある | ○内部保存の一般許可。△個別の保持上限・原提供元条件。契約終了時削除が必要 | ○AI連携を公式案内。△Basic＋Googleの学習等への再利用を含む権利 | ×無許可の原本・再配布用派生物。△具体的な非復元可能な仮想損益の公開範囲 | 最有力。全要件の利用確認済みではない |
| Alpaca Basic / Paper Only | ○世界中から無料Paper Only、IEX、履歴200要求/分。USD/JPYは別系統が必要 | ○個人非商用の一般利用。△自作Botの長期Supabase保管・終了後保持の具体条件 | ○Gemini CLI等のMCPを公式案内。△無料Gemini側の二次利用を含む権利 | ×許可なしの市場データ公開・配布。△自作Botの集計損益だけの公開 | 第二候補。標準契約を確認し、未確定用途を切り離す |
| Tiingo Starter / Trial | APIによるIEX等は公式に提供。今回の全銘柄の無料権限・freshnessは未検証 | **×原本もDerived Productsも永続保存不可**。揮発メモリ／一時キャッシュは処理終了前に除去 | △外部サービスへの送信・保持を含む用途。少なくとも無料Geminiを永続保存禁止の回避策にしない | 無料で保存・履歴追跡を含む今回の方式は×。有料向けの派生物許可を無料へ流用しない | Supabase継続履歴の条件に不適合 |
| Finnhub Personal | APIあり。現行の価格ページは取得した本文に表がなく、必要な無料15分足権限は確定せず | 個人利用○。△クラウド保管・保持期間 | **×データまたはderived resultsの第三者共有は書面承認が必要** | **×同じ共有制限** | 「特徴量だけ」では解決しないため除外 |
| Massive Stocks Basic | ○API。**×無料はEOD配信**。Minute Aggregatesの記載は新鮮な15分足の保証ではない | △今回のnon-display投資戦略用途の権利を確認できず | ×追加ライセンスなしの第三者へのDerived Works送信等 | ×同じ配布制限 | 料金表の無料CAだけを流用しない |
| Alpha Vantage Free | ○個人研究用API。**×株式intraday・FX_INTRADAYはPremium** | ○個人研究等の一般許可。△保存の具体条件 | ○公式MCPあり。△今回の無料外部AIの再利用条件 | △個人研究の一般許可から公開まで推定しない | 無料の時間足要件を満たさない |
| FMP Basic | ○250 calls/日。**×無料はEOD等、Intraday Chartsは有料** | △個人契約にcopy/downloadの書面承認条項があり、今回の永続保存を許可済みとは扱えない | ×第三者へのデータ・派生情報提供は一般個人契約の許可範囲外 | ×一般個人契約の共有・表示制限 | APIの無料枠だけでは採用できない |
| Marketstack Free | ○100要求/月・EOD。**×15分足は有料Basic以上** | 非商用プランだが今回の保持条件は未確定 | △今回の外部AI条件は確定せず | △今回の派生結果公開は確定せず | 無料15分足を提供しないため不採用 |
| EODHD Free | ○20 API calls/日・過去1年EOD。一般銘柄の新鮮な15分足は無料の包括権限を確認できず | 個人用途の案内あり。△今回の保持・再利用条件 | △今回の権利は確定せず | △今回の権利は確定せず | demoの限定銘柄・welcome bonusを5銘柄の継続無料利用と見なさない |

比較の根拠と制約は以下の提供元別記録を参照。別プランの許可を混ぜない。OSSライブラリのMIT等を取得データのライセンスと見なさず、非公式スクレイピングや転載サイトも代替権利の根拠としない。

## Twelve Data：一般許可を使える範囲

[個人料金表](https://twelvedata.com/pricing)は個人・内部・非商用向けBasicにUS株・FX・内部non-displayを含む。[規約](https://twelvedata.com/terms)2.2(a)(c)(d)は内部の取得・処理・保存と非復元的Derived Data作成を許可し、6.2は適合するDerived Dataの顧客の権利を認める。したがって、通常のAPIによる個人内部研究まで追加メールが必要とは判断しない。

同規約のAuthorized Userには契約者が承認したcontractors・computerized systemsが含まれる。**本調査の解釈**として、利用者だけがアクセスでき、委託先が独立した用途に再利用しないSupabaseを内部保存先にすることは、内部処理・保存許可に沿う候補となる。Supabaseの製品名の記載欠如だけを不許可の根拠にしない。一方、2.3(g)、3、16の保持・原提供元条件は必要で、原本入り世代・decision context・backupを無期限保存できるとはいえない。12.5は終了時の全Data削除、16.2は30日以内削除を規定。30日を自由な継続利用猶予と扱わず、保守的に終了時利用停止・削除可能な設計を選ぶ。30日／90日という既存設計案は提供元が保証した保持期間ではない。

[公式AI案内](https://twelvedata.com/artificial-intelligence)はMCP、LLM学習、推論、agentic trading等を掲げ、[2026年6月更新](https://twelvedata.com/news/june-2026-update)はClaude・ChatGPT等への接続を案内する。これはAI用途が一般に想定される根拠であり、Basicデータを無料Geminiの製品改善・学習へ使わせる包括許可までは記載されていない。

2.3(b)の無権利転送、17.1(g)の再配布用derived works制限もある。6.2の権利保持だけを根拠に、連続リターン・注文価格・口数・評価額・公開ベンチマークの全てを一般公開可能とはしない。非復元的な集計損益の具体例がどこまで適合するかは△とし、公開は保留する。

[US feed説明](https://support.twelvedata.com/en/articles/9935903-us-equities-market-data)では既定の実時間データは全米出来高の約5%。全市場のOHLCVと同一ではない。[遅延FAQ](https://support.twelvedata.com/en/articles/5203307-data-delays)はRESTのバー終了後の典型遅延0.3〜2分を説明するが、確定・改訂なしの保証ではない。銘柄と時刻を実測する前に品質確認済みとしない。

分割・配当の[API仕様](https://twelvedata.com/docs)は/splits、/dividendsを各20 credits/銘柄、Grow以上とする。Basicだけでは既存の企業行動安全条件まで揃わない。APIの取得回数は、5株式＋FXを15分ごと＋追加2回＋日足更新5回で173 credits/日、予備100込み273という前回試算のまま。枠の余裕と利用権の不足は別問題。

## Alpaca：Paper Onlyと自作Botを分ける

[Paper Trading仕様](https://docs.alpaca.markets/us/docs/paper-trading)は世界中からのPaper Only登録とIEX利用を明示する。[Basic仕様](https://docs.alpaca.markets/us/docs/about-market-data-api)は無料・IEX・履歴200要求/分、[FAQ](https://docs.alpaca.markets/us/docs/market-data-faq)は最新SIPの別権限を説明する。「全世界のPaper登録に個別許諾が必要」とはしない。

[公式MCP](https://docs.alpaca.markets/us/docs/trading-api-mcp)にはGemini CLIを含むAIクライアントとmarket-dataの接続がある。[公式保存チュートリアル](https://alpaca.markets/learn/collecting-market-data)もCSV／SQL等のキャッシュを扱う。よって、アルゴリズム利用・保存・AI利用が全て禁止とする根拠はない。ただしチュートリアルは2018年の記事で、現在の契約の保持期間を代替しない。

[公開規約PDF](https://files.alpaca.markets/disclosures/library/TermsAndConditions.pdf)は個人非商用利用を認め、公開・配布等を制限する一方、US residentsの文言も残る。[契約一覧](https://alpaca.markets/disclosures)から辿れるPDFと現行Paper仕様の違いを隠さず、登録時の適用契約を確認する。標準契約の取得・確認はメール交渉とは別。日本からのPaper登録許可を、全データの無期限保存・外部学習への転送・一般公開へ拡張しない。

企業行動の[Market Data API](https://docs.alpaca.markets/us/reference/corporateactions-1)は未完成イベントを含むdata_quality=allとページングを提供するが、取得・処理の遅延を明記する。今回のアカウントの無料権限を実取得で確認したわけではない。Alpaca自身のPaperも配当を完全には再現しないため、採用しても自作Botの企業行動停止を省略しない。USD/JPYは別提供元が必要。

## 他の株価候補を外した根拠

- **Tiingo**：[公式規約1.6(a)](https://app.tiingo.com/tos/)はStarter/Trialの原本・派生結果の永続保存を禁止し、処理終了前の揮発メモリ除去を要求する。有料向け1.6(c)に集計損益等の条件付き例示はあるが、無料の保存禁止は優先する。SupabaseをローカルCSVへ変えても解決しない。
- **Finnhub**：[公式規約Redistribution Rights and Personal Use](https://finnhub.io/terms-of-service)は原本だけでなく派生結果の第三者共有にも書面承認を要求。Googleへ送る情報を特徴量に変えるだけでは追加承認を避けられない。価格ページの表は今回本文を取得できず、未確認の無料枠や15分足を記載しない。
- **Massive**：[料金](https://massive.com/pricing?product=stocks)は無料をEODとし、[市場データ規約5(c)(d)](https://massive.com/legal/market-data-terms-of-service)は派生物の第三者転送、non-display・投資戦略等に追加権利を要求。無料CAを別系統へ組み合わせれば権利条件を無視できるわけではない。
- **Alpha Vantage**：[API仕様](https://www.alphavantage.co/documentation/)は株式intradayとFX_INTRADAYをPremiumとする。[個人規約](https://www.alphavantage.co/terms_of_service/)の研究・試験の許可と、必要データが無料かは別。公式MCPがあってもPremiumのデータ権限は得られない。
- **FMP**：[無料料金](https://site.financialmodelingprep.com/developer/docs/pricing)は250 calls/日・EOD等。[個人契約2.2.1・2.6.1](https://site.financialmodelingprep.com/terms-of-service)のコピー・第三者提供制限を回避しない。Intraday Chartsも有料で、今回採用しない。
- **Marketstack**：[公式料金・FAQ](https://marketstack.com/pricing)は無料100要求/月・EOD、intradayは有料。細かな権利を解決する前に無料の機能条件で不足する。[標準規約](https://www.ideracorp.com/legal/APILayer)から今回の外部Gemini等を許可済みとまでは判断しない。
- **EODHD**：[公式無料案内](https://eodhd.com/financial-apis/spreadsheets-for-stocks-etfs-forex-markets)は20 calls/日・過去1年EOD。[呼出制限](https://eodhd.com/financial-apis/api-limits)も無料枠の限定性を説明する。demo対象にUS株があることを、任意5株式・USD/JPYの恒久的な実運用権限と混同しない。

## USD/JPYを分離すれば解決できる部分

### ECBの日次参照値：追加問い合わせ不要の候補

[ESCB再利用方針](https://www.ecb.europa.eu/stats/ecb_statistics/governance_and_quality_framework/html/usage_policy.en.html)は、公表された自らの統計の無料利用・再利用を商用／非商用に関係なく認める。[著作権条件](https://www.ecb.europa.eu/services/using-our-site/disclaimer/html/index.en.html)に従い、正確に出所を示し、加工を明示する。EUR/USDとEUR/JPYの原系列を保持し、USD/JPY=EUR/JPY÷EUR/USDを「ECB統計から計算した日次参照値」と表示できる。原提供元が別の第三者データはこの一般許可の対象外。

この条件内での内部保存・再利用・計算結果の配布に、個別メールを要求する条項は確認されない。明確な再利用許可を根拠に、出所・加工説明を伴う必要最小限のAI入力も候補にできる。ただしGoogleの別条件にも従い、事実に反したECB公認等の表示はしない。

**日次参照値は取引用のリアルタイムUSD/JPYではない。** 日付しかない値に取得時刻をquote時刻として付けず、既存3600秒の鮮度検証へ投入しない。採用するなら「USD建て売買＋日次の参考JPY比較」へ用途を変更し、既存取引FXとはモデル・保存領域を分ける必要がある。今回、この変更も有効化も行っていない。

### Frankfurter：公式FAQだけでデータ権を一括判断しない

[FAQ](https://frankfurter.dev/)は無料API・DBキャッシュ・blended ratesの無料利用・AI向けMCPを案内する。一方、[License & Terms](https://frankfurter.dev/license/)はソフトウェアのMITとデータ権を区別し、原提供元条件が適用されると明記する。FAQの無料記述だけで全104提供元のデータのAI学習・転載を許可済みとしない。問い合わせなしを優先するなら、混合feedの全権利を推測せず、ECB等の再利用条件が明確な原提供元を固定して確認する。[公式MCP](https://frankfurter.dev/mcp/)の存在も無制限データライセンスの代わりではない。更新は基本日次で、株価15分足の代わりにもならない。

## Gemini側との整合

[Google追加規約](https://ai.google.dev/gemini-api/terms)のUnpaid Servicesは入力・出力の製品改善・機械学習利用と人手確認を規定する。日本居住の今回、EEA等の例外を適用したとは扱わない。無料枠の残量や「推論だけを依頼した」事実では、Googleの二次利用条項は無効にならない。開発者向けAPIの用途条件・機密情報を送らない条件にも従う。

同規約のPaid Servicesは学習利用しない等の別条件を設けるが、APIのbillingを有効化しただけで永久に0円を保証するものではない。[モデル料金](https://ai.google.dev/gemini-api/docs/pricing)の標準単価を使うと、仮に1要求入力6000・出力2000 tokens、月138要求ならgemini-3.5-flash-liteは約$0.94/月相当（税・変動・追加利用等除外）。これは有料化の提案を自動適用するものではなく、0円制約を緩める際の比較値である。学習なしにしても提供元の第三者処理許可が自動的に成立するとはしない。

比率・丸め・匿名化・ラベル化だけで非復元性を証明したことにはならない。公知価格をアンカーにした連続リターン等は復元可能になり得る。現行のDecisionContext全体送信は、この用途の許可済み最小payloadではない。FeatureProposalもmock専用で、規約適合の証明ではない。

利用者確認済みの表示枠15 RPM・250,000 TPM・500 RPD、Botの内部8/日・200/月は維持する。通常6要求/日・23日で138要求/月の頻度は技術上の候補であり、今回の問題はその回数自体ではない。個別APIキーを増やして枠を回避しない。

「継続利用」は、標準契約と無料プランが有効な間に反復利用できることを意味し、無料価格・API提供・保持権限が永久に変わらないという保証ではない。trial銘柄や一度限りのボーナスでは継続要件を満たさない。無料プランでも規約・endpoint権限の変更時は停止し、別提供元へ黙って切り替えない。

## 問い合わせなしで実現できる範囲と緩和案

| 案 | 維持できるもの | 緩める条件／残る確認 | 今回の判断 |
| --- | --- | --- | --- |
| 現状の全条件を維持 | 0円・5株15分・毎時無料Gemini・Supabase・継続・可能なら公開 | 無料Gemini側の二次利用、保持条件、公開範囲、企業行動を全て公開根拠で確定する必要 | 確認できず。停止維持 |
| 非公開の内部研究へ限定 | Twelve Basicの一般許可に基づくUS/FX API・内部解析・条件内の保存 | 市場由来の入力は外部Geminiへ送らず、ローカル処理／mockにする。成績公開なし、無期限保存なし。企業行動安全条件は別途必要 | 個別メールを一律の前提としない候補。毎時Geminiの市場判断という要件は満たさない |
| Alpaca公式Paperの標準機能だけ | 世界中から無料登録・IEX・アルゴリズム試験 | 自作Supabase履歴・外部AI送信・JPY比較・公開を外す。Alpacaの仕様上の配当等の限界も受け入れる | 公式に案内された限定用途。今回の自作Bot全要件を代替しない |
| FXのみ日次参照へ変更 | ECBの無料再利用・保存・出所付き加工・日次比較 | リアルタイムFX・毎時JPY評価を外す。株価・外部Geminiの権利は別に残る | FX部分は個別メールなしの明確な候補 |
| 株価をEOD／過去研究へ変更 | 無料データ候補の機能範囲が増える | 毎時新しい15分価格の判断を諦める。保存・外部AI・公開の権利は自動解決しない | 頻度を重視する今回の第一候補にはしない |
| Googleの学習利用なしのサービスへ変更 | 毎時判断の構造 | 0円の永久保証を外す可能性。提供元の第三者処理・保持条件の確認は依然必要 | 費用を許容しても全権利を確認済みとはしない |
| 合成株価＋自作特徴量＋再利用可能な日次FX | mock検証、毎時の構成、0円優先、外部送信用の自作情報 | 実株価による市場検証を外す。実Geminiは無料／課金確認と別の起動許可後のみ | 今すぐ継続できる開発範囲。実市場検証の完了とは報告しない |

「公開だけをやめる」「生データを特徴量にする」「データを15分遅らせる」「クラウドをローカルCSVへ変える」の一つだけで、全ての未確定事項が解決するとはいえない。企業行動を無視したりFX鮮度を黙って緩めたりして運用可能としない。

個別連絡を選ぶのは、**未確定の用途をどうしても維持し、適用できる公開規約・標準契約に根拠が見つからない場合**に限る。Finnhubのように書面承認を明記する提供元は、当該用途を維持するなら承認が必要。Twelve/Alpacaの△は「メールが法律上必ず必要」という意味ではなく、今回の根拠不足である。公開FAQへの明確化、登録時に提示される適合した標準ライセンス、許諾が明確な他の提供元への切替でも解決し得る。現状では、全条件の達成を確認済みとして起動できるとは報告しない。

## 次の手順と今回の変更

1. メール送信を必須タスクから外す。本報告を先に判断材料とする。
2. 0円・毎時Geminiを最優先する間は、合成データの構成試験を継続する。許諾不明の実価格特徴量は送信しない。
3. 実価格利用を優先する場合は、外部Gemini・成績公開・無期限保持を外した限定用途にするか、公開されている適用標準契約で残りの権利を確認する。条件変更は利用者の判断後で、取得停止の解除とは別。
4. 新提供元の許諾が確定してからProvider・15分足・保持制御・AI payloadを実装し、手動隔離検証を行う。

今回変更したのは本調査書と関連ドキュメントの案内だけ。コード・AI上限・Workflow・DB・Secrets・公開ダッシュボードは変更しない。実データ・Gemini通信0回、メール未送信、cronなし、paper-liveおよび既存口座・API履歴の操作なし。調査書の更新に対して売買サイクルや全テストを再実行したとは主張しない。
