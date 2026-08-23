# Breakout Scanner — Column Reference

## Identity

| Column | Field | What it is | How to read it |
|---|---|---|---|
| **Bar Date** | `bar_time` | Date of the bar that triggered the signal | If this is today and the market is open, the bar is INCOMPLETE and can repaint |
| **Direction** | `direction` | Bullish = broke resistance, bearish = broke support | — |
| **Mode** | `mode` | Standard, or Strict which adds an ATR-expansion test | Strict is a narrower net |
| **Symbol** | `symbol` | NSE trading symbol | — |
| **Timeframe** | `timeframe` | Bar size the breakout was detected on | 1H / 1D / 1W / 1M |

## Breakout

| Column | Field | What it is | How to read it |
|---|---|---|---|
| **ATR(14)** | `atr` | 14-bar average true range (strict mode only) | — |
| **Break %** | `breakout_pct` | How far past the level the close is | Your mentor avoids paying more than 2% above. Small = early, large = extended |
| **Break Level** | `level` | The Donchian level that was crossed | Resistance for bullish, support for bearish |
| **Close** | `close` | Closing price of the signal bar | — |
| **Lookback** | `lookback` | Donchian window in bars | 20 on 1D = a new one-month high |
| **Prior High** | `prior_high` | Highest high of the lookback window | — |
| **Prior Low** | `prior_low` | Lowest low of the lookback window | — |
| **Strong Close** | `strong_close` | Close finished in the top 60% of the bar's range | Weak closes are filtered out before you see them |
| **TR/ATR** | `tr_atr_ratio` | Bar range vs recent average (strict mode only) | Strict requires > 1.2. Higher = more decisive bar |
| **True Range** | `true_range` | Bar range including gaps (strict mode only) | — |

## Relative strength

| Column | Field | What it is | How to read it |
|---|---|---|---|
| **3m Return** | `ret_3m` | Stock return over 63 trading days | Raw return, not yet compared to anything |
| **Mansfield RS** | `mansfield_rs` | (stock/index ratio vs its own 200-bar average − 1) × 100 | ZERO is the line. Catches stocks TURNING outperformer, which a plain return misses. Weinstein's measure |
| **O'Neil Score** | `oneil_score` | 2×(3m) + 6m + 9m + 12m return — raw, before ranking | Input to RS Rating; not useful on its own |
| **RS Rank** | `rs_rank` | Percentile of RS vs NIFTY across the scanned universe | 0-100. Only meaningful on 50+ symbols |
| **RS Rating** | `rs_rating` | O'Neil score percentile-ranked 1-99 | O'Neil wanted 80+. Needs 252 bars, so NaN for recent listings |
| **RS vs NIFTY** | `rs_vs_nifty` | 3-month return minus NIFTY's, in percentage points | > 0 beats the index. In a flat index this is a LOW bar — NIFTY was +1.02% last quarter |

## Trend / Minervini

| Column | Field | What it is | How to read it |
|---|---|---|---|
| **150 DMA** | `dma150` | 150-day simple moving average | — |
| **200 DMA** | `dma200` | 200-day simple moving average | — |
| **200 DMA Rising** | `dma200_rising` | 200 DMA higher than a month ago | Minervini criterion 3 |
| **50 DMA** | `dma50` | 50-day simple moving average | — |
| **> 50 DMA** | `above_50dma` | Close is above the 50 DMA | Your mentor's criterion |
| **MV1 >150&200** | `mv1` | Price above both 150 and 200 DMA | — |
| **MV2 150>200** | `mv2` | 150 DMA above 200 DMA | — |
| **MV3 200 rising** | `mv3` | 200 DMA trending up ≥1 month | — |
| **MV4 50>150&200** | `mv4` | 50 DMA above both | — |
| **MV5 >50dma** | `mv5` | Price above 50 DMA | — |
| **MV6 +30% off low** | `mv6` | ≥30% above the 52-week low | — |
| **MV7 ≥75% of high** | `mv7` | Within 25% of the 52-week high | — |
| **MV8 RS≥70** | `mv8` | RS Rating at least 70 | — |
| **Minervini** | `minervini_score` | How many of the 8 Trend Template criteria pass | 0-8. 7/8 failing only on RS is very different from 3/8 |
| **Minervini 8/8** | `minervini_pass` | All eight criteria pass | Rare. A strict definition of a leader |

## Liquidity

| Column | Field | What it is | How to read it |
|---|---|---|---|
| **Avg Vol 10d** | `avg_vol_10d` | Mean volume of the last 10 COMPLETED sessions | ABSOLUTE floor — 'tradeable at all'. Mentor's threshold: ≥ 100,000. This is what kills the 50× artifacts |
| **Circuit?** | `circuit_suspect` | Bar looks locked at an upper/lower circuit | Heuristic: frozen bar, or pinned to a 5/10/20% band with almost no range |
| **Price** | `price` | Latest close | Mentor's floor: ≥ ₹100 |
| **Turnover 10d** | `turnover_10d` | avg_vol_10d × price, in rupees | ₹100 price + 100k volume implies ≥ ₹1 crore daily turnover |
| **Vol (full-day est.)** | `vol_today_extrapolated` | Partial-session volume scaled to a full day | Mid-session only. Mentor's method: 30k by 09:30 against a 1 lakh average is healthy |
| **Vol Ratio** | `volume_ratio` | Signal bar volume ÷ 20-bar average | RELATIVE surge — 'unusual for this stock'. A thin stock can show 50× on one block trade |

## Highs

| Column | Field | What it is | How to read it |
|---|---|---|---|
| **% of 52w High** | `pct_of_52w_high` | Close as a percentage of the 52-week high | Minervini wants ≥ 75%. A 20-day high at 45% of the 52w high is a bounce in a downtrend |
| **% of ATH** | `pct_of_ath` | Close as a percentage of the all-time high | 100% = at all-time highs |
| **% off 52w Low** | `pct_of_52w_low` | How far above the 52-week low | Minervini wants ≥ 30% |
| **52W High?** | `is_52w_high` | Close is at a new 52-week high | — |
| **ATH** | `ath` | Highest high in the full available history | — |
| **ATH History** | `ath_history_bars` | Bars of history the ATH was computed from | Small number = shallow history, treat the ATH with suspicion |
| **At ATH?** | `at_ath` | Within 0.1% of the all-time high | — |

## Listing

| Column | Field | What it is | How to read it |
|---|---|---|---|
| **Bars** | `bars_available` | Daily bars available for this symbol | Below 60 the scanner cannot fire at all |
| **IPO (<1yr)** | `is_recent_listing` | Between 60 and 252 bars of history | IPO scan. These have NaN for Mansfield / RS Rating / Minervini — combining an IPO scan with an RS filter returns zero |
| **Listing Age** | `approx_listing_days` | Approximate sessions since listing | Inferred from bar count — no listings feed needed |

## Machine learning

| Column | Field | What it is | How to read it |
|---|---|---|---|
| **ML Confidence** | `ml_confidence` | RandomForest probability that the setup reaches +3% before −2.5% within 10 bars | A RANKING, not a probability — the model is poorly calibrated (Brier 0.247). Out-of-sample AUC 0.530. Its value is the BOTTOM decile (32% win rate vs 38% base), not the top |
