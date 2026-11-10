# Multi-Agent BTC Prediction Market Trader

## Goal

Build a system that trades **5-minute BTC prediction markets** using multiple AI agents.

Each agent independently analyzes the market and decides:

- 📈 UP
- 📉 DOWN
- ⏸️ NO TRADE

A trade is only executed when the consensus rules allow it.

Every agent also acts as a **Shadow Trader**, tracking what would have happened if its decisions were followed independently.

---

# System Architecture

```text
                 Binance WebSocket
                        │
                        ▼
              Live Price Engine
                        │
        ┌───────────────┼───────────────┐
        ▼               ▼               ▼
    Agent A         Agent B         Agent C
        │               │               │
        └───────────────┼───────────────┘
                        ▼
          Consensus & Shadow Traders
                        │
                        ▼
          Paper Trade / Live Dashboard
```

The Binance WebSocket is the **single source of truth**.

Instead of relying on external chart updates, the system builds its own market candles from every trade received.

---

# Data Pipeline

## Live Price Source

- Binance WebSocket
- Stream: `btcusdt@trade`

Every trade updates the latest BTC price in real time.

## Candle Builder

Generate rolling **5-minute candles** from the trade stream.

Each candle contains:

- Open
- High
- Low
- Close
- Volume

When a candle closes:

1. Freeze the candle.
2. Send identical market data to every AI agent.
3. Collect predictions before the next market begins.

---

# AI Agents

Each agent should use a different strategy instead of different prompts.

| Agent | Strategy |
|--------|----------|
| Momentum | Follows strong trends |
| Mean Reversion | Trades price pullbacks |
| Volatility | Trades only during high movement |

Each agent outputs:

- Direction
- Confidence
- Reasoning
- No-trade option

Example:

```json
{
  "decision": "UP",
  "confidence": 0.82,
  "reason": "Strong breakout above resistance."
}
```

---

# Consensus Engine

Example execution rules:

- **3/3 agree** → Execute trade.
- **2/3 agree** → Execute only if average confidence exceeds a threshold.
- Otherwise → No trade.

The consensus system should be modular so new voting systems can be tested later.

---

# Shadow Traders

Each AI keeps its own independent paper-trading record.

Track:

- Win rate
- Total Profit/Loss
- ROI
- Number of trades
- Consecutive wins
- Consecutive losses

This allows comparison between:

- Individual agents
- Consensus performance

without risking real money.

---

# Dashboard

Live dashboard should display:

## Market

- Live BTC price
- Current 5-minute candle
- Time remaining until market closes

## Agent Votes

| Agent | Vote | Confidence |
|--------|------|------------|
| Momentum | UP | 82% |
| Mean Reversion | DOWN | 64% |
| Volatility | NO TRADE | — |

## Consensus

- Final decision
- Average confidence
- Trade status

## Shadow Trader Stats

Each agent should have:

- Win rate
- P/L
- ROI
- Active streak

---

# Research Tasks

## Market Mechanics

- [ ] Understand Polymarket 5-minute BTC market settlement.
- [ ] Recreate payout calculations.
- [ ] Understand leverage mechanics.

## Data

- [x] Choose Binance WebSocket.
- [ ] Build live BTC price collector.
- [ ] Build real-time 5-minute candle generator.

## Timing

- [ ] Determine exactly when each market opens.
- [ ] Determine exactly when each market closes.
- [ ] Synchronize candle closes with market resets.

## AI

- [ ] Design Momentum strategy.
- [ ] Design Mean Reversion strategy.
- [ ] Design Volatility strategy.
- [ ] Standardize agent outputs.

## Trading Engine

- [ ] Build consensus engine.
- [ ] Implement Shadow Traders.
- [ ] Create paper-trading simulator.
- [ ] Add optional real trade execution later.

---

# Build Order (MVP)

## Phase 1 — Market Data

- [ ] Connect to Binance WebSocket.
- [ ] Receive live BTC trades.
- [ ] Keep one persistent connection.
- [ ] Auto-reconnect on disconnect.

## Phase 2 — Candle Builder

- [ ] Generate live OHLCV candles.
- [ ] Close candles every 5 minutes.

## Phase 3 — AI

- [ ] Implement one agent.
- [ ] Add two more agents.
- [ ] Standardize voting format.

## Phase 4 — Simulation

- [ ] Shadow Trader tracking.
- [ ] Consensus engine.
- [ ] Paper trading.

## Phase 5 — Dashboard

- [ ] Live price
- [ ] Agent votes
- [ ] Consensus
- [ ] Statistics

## Phase 6 — Optional Live Trading

- [ ] Real execution
- [ ] Risk controls
- [ ] Logging
- [ ] Safety checks

---

# Tech Stack

| Component | Choice |
|-----------|--------|
| Language | Python |
| Live Data | Binance WebSocket |
| Async | asyncio |
| WebSocket Library | websockets |
| Dashboard | FastAPI + HTML/JS (later) |
| Storage | SQLite |
| AI | Local or Cloud LLMs |
| Deployment | Docker |

---

# Core Principles

- Binance is the single source of truth.
- Build candles from raw trades instead of polling.
- Every agent receives identical market data.
- Keep one persistent WebSocket connection.
- Track every agent independently through Shadow Traders.
- Never execute trades without explicit consensus rules.
- Design everything to be modular so strategies and voting systems can be swapped later.
