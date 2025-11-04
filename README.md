# microcap-scout-bot  

An AI-driven tool for scouting micro cap stocks with penny stock characteristics. This bot scans market data, social sentiment, and technical signals to identify high potential micro cap opportunities.  

## Overview  

Micro cap stocks — companies with market capitalizations roughly between $50 million and $300 million — can offer outsized returns but also carry significant risk due to illiquidity, limited public information, and susceptibility to volatility ([Micro-Cap: Definition in Stock Investing, Risks Vs. Larger ...](https://www.investopedia.com/terms/m/microcapstock.asp#:~:text=Micro,What%20Is%20a%20Micro%20Cap)).  

This bot focuses on a single momentum strategy: it scrapes Finviz’s insider trading screener for recent BUY filings, filters for stocks trading between $1 and $10, then places a bracket order through the Alpaca paper trading API with a 5 % take-profit and 2 % stop-loss.  

## Features  

- **Insider Trade Scan:** Scrapes the latest insider transactions from Finviz’s public screener and filters for qualifying BUY trades.  
- **Price Validation:** Confirms the current quote via stockdata.org and enforces the $1–$10 price band.  
- **Bracket Orders:** Submits Alpaca bracket market orders with configurable size, attaching both take-profit and stop-loss legs.  
- **Position Guard:** Skips symbols that are already present in the Alpaca account’s open positions.  

## Installation  

1. Clone this repository.  
2. Install dependencies via `pip install -r requirements.txt`.  
3. Set the following environment variables (e.g. in `.env` or your process manager):  
   - `APCA_API_KEY_ID`, `APCA_API_SECRET_KEY` (Alpaca paper trading credentials)  
   - `STOCKDATA_API_KEY` (stockdata.org API token)  
4. Run `python app.py` for the polling loop, or `python main.py` to serve the FastAPI app.  

## Usage  

- The script polls for insider trades every 15 minutes and attempts a bracket order for each qualifying ticker.  
- Adjust position sizing or polling frequency directly in `app.py` as needed.  
- Trade execution occurs on Alpaca’s paper environment by default; modify `TradingClient(..., paper=True)` if you intend to route to live trading (**highly discouraged without substantial safeguards**).  

## Disclaimer  

This repository is for educational purposes only. Investing in micro cap and penny stocks carries high risk; you can lose all invested capital. Do your own due diligence and consult a financial advisor before making any trades. Past performance is not indicative of future results.  

## Contributing  

Pull requests are welcome. Please ensure:  
- Code is formatted (black) and linted (flake8).  
- New features come with tests (pytest).  
- Documentation is updated accordingly.  

## License  

MIT License. See `LICENSE` file for details.
