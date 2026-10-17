# 提供元への問い合わせ下書き（未送信）

2026-10-17。秘密キーや口座情報を含めず、利用条件の回答と適用規約へのリンクを求める。回答は実取得や無料利用の許諾確認に使い、拒否・未回答を許可と扱わない。Supabaseの実リージョンは送信前に確認して追記する。

## Twelve Data

公式連絡先は[サポート案内](https://support.twelvedata.com/en/articles/5609168-introduction-to-twelve-data)の support@twelvedata.com。契約・データライセンス担当への転送を依頼する。

Subject: Basic plan rights clarification: private US equity paper trading, cloud retention and Gemini free API

Hello Twelve Data licensing team,

I am an individual resident of Japan developing a personal, non-commercial simulated stock account. There are no customers, no real brokerage orders, and no public display or redistribution of market prices or derived performance. I need a strictly $0/month arrangement.

The proposed use is five US-listed stocks initially (possibly ten), regular-session 15-minute OHLCV retrieval, one daily historical refresh, and USD/JPY retrieval every 15 minutes. Software would review the portfolio once per hour. It runs in GitHub Actions and stores data privately in Supabase, with access restricted to me. No raw data would be committed to GitHub, uploaded to public artifacts or shown on Pages.

Please confirm in writing, with applicable license/third-party terms:

1. Does Basic permit this automated non-display research/paper-trading use for a Japan resident, including processing by GitHub Actions and private cloud storage/backups in Supabase? Are any exchange approvals, attribution or fees required?
2. What is the permitted retention period for intraday and daily prices, FX, snapshots, derived features and backups? I propose a maximum of 30 calendar days intraday and 90 days daily, subject to your terms. What must be deleted on termination, including backups, and which non-reconstructable audit metadata or my own simulated order records may remain? Please clarify the interaction of termination/deletion clauses and section 16.
3. May I send rounded/bucketed returns, SMA distance, volatility ratios and portfolio weights, without absolute prices, OHLCV series, cash, share quantities or raw FX rates, to Google's unpaid Gemini API for inference? Google's unpaid-service terms allow product improvement/model training and possible human review of inputs and outputs. Does that require redistribution/AI rights, or is it prohibited on Basic? I will not assume that derived features automatically qualify as unrestricted Derived Data.
4. Are /time_series 15min and 1h for these US stocks and USD/JPY included on Basic? Please confirm the feed's venue/volume coverage, bar start/timezone, session anchoring, adjustment defaults, publication delay and correction/finality policy. Is one credit per symbol correct, including multi-symbol requests, with eight credits/minute and 800/day reset at UTC midnight?
5. I understand /splits and /dividends require Grow or above. Is any complete, ongoing free entitlement available for split/dividend alerts for these stocks, beyond trial symbols? If not, may I use Alpaca corporate-action events alongside your price feed without creating a combined redistribution product?

Please distinguish permissions for private storage from third-party AI processing. If any requested use requires payment or is not permitted, please identify it explicitly; I will keep live collection disabled until this is resolved.

Thank you.

## Alpaca

[公式Support](https://alpaca.markets/support)からデータ・ライセンス担当へ。実注文の有効化や証券取引口座への入金を依頼する文面ではない。

Subject: Japan-resident Basic market-data-only use: private paper trading, corporate actions and external AI

Hello Alpaca market-data licensing team,

I am a Japan-resident individual seeking a strictly free, personal, non-commercial market-data-only arrangement. My own software performs simulated trading only; it will never send orders to Alpaca or any real broker. I initially monitor five, later up to ten, US-listed stocks. I would collect 15-minute bars and review decisions hourly during regular US sessions using GitHub Actions and private Supabase storage. No raw data or derived performance is publicly displayed or redistributed.

Please confirm:

1. Am I eligible for Basic market data and corporate-actions API without funding or enabling a live trading account? Which current agreement applies to Japan residents, including IEX/exchange and non-display terms? Some published terms refer to US residents; please provide the applicable terms.
2. Does Basic permit automated private paper trading, processing in GitHub Actions, and private Supabase storage/backups? What retention periods and termination/deletion obligations apply to bars, events, derived features and audit records? Proposed maxima are 30 days intraday and 90 days daily, only if permitted. Can my own simulated orders and non-reconstructable audit metadata be retained?
3. May minimal rounded/bucketed ratios/returns/volatility and portfolio weights be sent for inference to Google's unpaid Gemini API, which may use inputs/outputs for product improvement/model training and human review? No absolute prices, raw OHLCV, quantities, cash or raw FX would be sent. Please state whether separate AI/redistribution rights are required; I will not assume derived data is unrestricted.
4. Please confirm Basic feed=iex 15-minute/1-hour bar access, adjustment defaults, timestamps, session anchoring, latency/finality and revision policy, and the separate restrictions on recent SIP history. Is the published 200 historical requests/minute limit the applicable free allowance, including pagination?
5. Is GET data.alpaca.markets/v1/corporate-actions available continuously at $0 for my symbols, including data_quality=all, all pages, incomplete events, and split/dividend/spin-off/merger data? What rate limits, lookback/forward ranges, update timestamps and coverage/processing delays apply? Are there additional rights for privately storing these events and using them to halt a simulation?
6. May I combine your corporate-action alerts with Twelve Data US prices/USDJPY, or use IEX prices with Twelve Data FX, solely within this private application? Is any attribution required in private reports?

Please identify any paid requirement explicitly. If a request is not permitted, live collection will remain disabled. Please distinguish private storage rights from external AI processing rights.

Thank you.
