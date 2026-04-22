# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Algorithmic stock trading system that combines economic data analysis, technical analysis, sentiment analysis, and automated trade execution through the Korean Investment Securities (한국투자증권/KIS) brokerage API. Comments and variable names are primarily in Korean.

## Running the Application

```bash
# Start the FastAPI server (main entry point)
python run.py
# Runs uvicorn on 0.0.0.0:8000 with reload enabled

# Standalone data collection
python stock.py

# ML prediction
python predict.py
```

No test suite exists yet (tests/ directory is empty).

## Architecture

### Layered Structure (app/)

- **api/routes/** - FastAPI route handlers (4 route groups: balance, economic, stock_recommendations, stocks)
- **api/api.py** - Central router that aggregates all routes
- **services/** - Business logic layer (largest files in the codebase)
  - `balance_service.py` - KIS brokerage API integration (orders, balances, token management)
  - `stock_recommendation_service.py` - Technical indicators (SMA, EMA, RSI, MACD, Golden Cross) and recommendation filtering
  - `economic_service.py` - FRED + Yahoo Finance + KIS data collection pipeline
  - `auth_service.py` - Token management
- **utils/scheduler.py** - APScheduler-based automated trading (auto-buy daily at midnight KST, auto-sell every 1 minute)
- **core/config.py** - Pydantic BaseSettings configuration
- **db/supabase.py** - Supabase client initialization
- **main.py** - FastAPI app with lifespan (starts schedulers + initial data collection on startup)

### Standalone Modules (root)

- `stock.py` - FRED API + Yahoo Finance + KIS data collection, merges into Supabase `economic_and_stock_data` table
- `predict.py` / `predict_real.py` - TensorFlow Transformer model for stock price prediction

### Data Flow

1. **Collection**: FRED economic indicators + Yahoo Finance prices + KIS domestic stocks → merged by date → stored in Supabase
2. **Analysis**: Historical data → technical indicators (SMA/EMA/RSI/MACD) → filtered by accuracy >= 80% and rise probability >= 3%
3. **Sentiment**: AlphaVantage news API → sentiment scores → stored in `ticker_sentiment_analysis`
4. **Execution**: Recommendations → auto-buy at midnight KST → auto-sell monitors every minute during US market hours

## Database (Supabase/PostgreSQL)

Key tables: `economic_and_stock_data`, `stock_analysis_results`, `stock_recommendations`, `ticker_sentiment_analysis`, `access_tokens`, `stocks`

## Environment Variables

Required in `.env`:
- `KIS_BASE_URL`, `KIS_REAL_URL` - KIS API endpoints (mock vs real)
- `KIS_APPKEY`, `KIS_APPSECRET` - KIS API credentials
- `KIS_CANO`, `KIS_ACNT_PRDT_CD` - KIS account info
- `KIS_USE_MOCK` - Toggle mock/real trading mode
- `SUPABASE_URL`, `SUPABASE_KEY` - Database credentials
- `ALPHA_VANTAGE_API_KEY` - Stock data API
- `FRED_API_KEY` - Federal Reserve economic data API
- `DEBUG` - Debug mode flag

## Key Implementation Details

- **Token caching**: KIS access tokens cached in memory + Supabase with thread-lock-protected 1-minute refresh throttling
- **Market hours**: Auto-sell detects US market hours (9:30 AM - 4:00 PM ET) with automatic daylight savings handling
- **Missing data**: Forward/backward fill strategy for gaps in economic/stock data
- **Async**: Background tasks and async scheduling via asyncio
- **Timezone**: All scheduling uses Korea Standard Time (Asia/Seoul via pytz)

## Dependencies

Core: fastapi, uvicorn, pydantic, pydantic-settings, python-dotenv
Database: supabase-py
Data: pandas, numpy, yfinance, requests
ML: tensorflow, keras, scikit-learn
Scheduling: schedule, APScheduler
