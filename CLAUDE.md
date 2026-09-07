# Project Guidelines: Options Trading Pipeline

## Core Architecture & Data Flow
1. Sentiment Engine: Process raw news/text feeds to generate a numerical sentiment score.
2. ML Feature Joining: Explicitly join this sentiment score timeline with historical price data. The sentiment score MUST be an active feature in the XGBoost training dataset.
3. XGBoost Probability Output: Train the XGBoost model to output concrete success probabilities (`predict_proba`) for target price boundaries.

## Mathematical Optimization Engine
- Treat 'Spread Width' as a dynamic variable array (e.g., looping through $0.50, $1.00, $2.50, $5.00, $10.00). Do not hardcode widths.
- For every available width, calculate Expected Value (EV):
  EV = (XGBoost Success Probability * Potential Profit) - (Failure Probability * Max Risk)
- Penalize wide/illiquid spreads by accounting for bid-ask slippage.
- The pipeline must automatically choose and return the exact combination of strike price and spread width yielding the highest absolute EV.

## Execution Requirements
- Implement two runtime execution modes:
  1. 0DTE Mode (evaluating tight widths, high gamma, same-day expiry).
  2. Weekly Mode (evaluating broader widths, 4-7 days to expiry).
- Include an automation hook/loop that triggers the entire data pipeline precisely at 9:30 AM EST every trading day.
- Keep code modular with clean Python type hinting. Do not break the data flow connection between sentiment and XGBoost features.
