# BTC Polymarket 15m — 15-day guarded rollout

## Phase 1: shadow validation (days 1–2)

- Generate stable and value signals on the live 15-minute markets.
- Record the actual best ask, spread, top-five depth, VWAP, latency and outcome.
- Place no orders.
- Continue only if data freshness is reliable and executable-price slippage is within the stress-tested range.

## Phase 2: small live orders (days 3–15)

- Dedicated test wallet only; never reuse a main wallet.
- Fixed 10 USDC maximum per order; compounding disabled.
- Maximum one position per BTC 15-minute market.
- Maximum daily loss: 30 USDC.
- Pause after three consecutive losses.
- Maximum live spread: 0.03.
- Order size may not exceed 10% of top-five ask depth.
- Skip when market or BTC data is older than two seconds.
- Skip when volatility ratio > 1.8, 15-minute range ratio > 2.2, or shock z-score > 3.0.
- Skip when the selected price differs from 60-second VWAP by more than 0.12.
- Skip when complementary asks deviate from 1.00 by more than 0.10.
- Cancel an unfilled limit order rather than chase the price.

## Stop conditions

- API, clock or WebSocket health failure.
- Three consecutive losses.
- Daily loss reaches 30 USDC.
- Realized slippage exceeds 0.03 twice in one day.
- Rolling live win rate over 100 settled orders falls below 68% for stable or 62% for value.
- Any mismatch between expected and actual market settlement.

## Review metrics

- Signal count, order count and fill rate.
- Stable/value win rate separately.
- Quoted versus realized entry price.
- Net profit after fees.
- Maximum drawdown and longest loss streak.
- Number and reason of every blocked order.

Research and controlled testing only. No return or win rate is guaranteed.
