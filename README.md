# microcap-scout-bot  

An AI-driven tool for scouting micro cap stocks with penny stock characteristics. This bot scans market data, social sentiment, and technical signals to identify high potential micro cap opportunities.  

## Overview  

Micro cap stocks — companies with market capitalizations roughly between $50 million and $300 million — can offer outsized returns but also carry significant risk due to illiquidity, limited public information, and susceptibility to volatility ([Micro-Cap: Definition in Stock Investing, Risks Vs. Larger ...](https://www.investopedia.com/terms/m/microcapstock.asp#:~:text=Micro,What%20Is%20a%20Micro%20Cap)). Many micro caps are speculative and may run out of capital or issue dilutive shares ([Are there any people here that do deep dives into ...](https://www.reddit.com/r/ValueInvesting/comments/1m27vmw/are_there_any_people_here_that_do_deep_dives_into/#:~:text=Are%20there%20any%20people%20here,very%20good%20info%20on%20this)). This bot helps navigate that landscape by combining traditional indicators (like Relative Strength Index and Moving Average Convergence Divergence) with NLP based sentiment analysis drawn from public chatter on Reddit and X.  

## Features  

- **Sentiment Analysis:** Uses NLP to gauge positive/negative sentiment around tickers from Reddit, X, and news sources. Note that public sentiment is a noisy and manipulatable signal; treat it as a barometer, not a compass.  
- **Technical Indicators:** Calculates RSI and MACD to identify momentum shifts and potential entry/exit points ([Micro-Cap: Definition in Stock Investing, Risks Vs. Larger ...](https://www.investopedia.com/terms/m/microcapstock.asp#:~:text=3%20Advanced%20Technical%20Indicators%20for,Use%20for%20Trading%20Penny%20Stocks), [Low-Priced Stocks Can Spell Big Problems](https://www.finra.org/investors/insights/low-priced-stocks-big-problems#:~:text=Relative%20Strength%20Index%20,period%20timeframe)).  
- **Data Aggregation:** Fetches price and volume data from market APIs (e.g. Alpaca) and caches results to minimize latency.  
- **Alert System:** Sends alerts when a ticker meets combined sentiment and technical criteria.  
- **Webhook Ready:** Exposes endpoints for integration with external platforms.  

## Installation  

1. Clone this repository.  
2. Install dependencies via `poetry install` or `pip install -r requirements.txt`.  
3. Create a `.env` file with your API keys and webhook URLs.  
4. Run `python main.py` to start the bot.  

## Usage  

- Set your watchlist of micro cap tickers in `config.json`.  
- The bot fetches data periodically, computes metrics, and logs results.  
- Customize thresholds for RSI and sentiment in `settings.py`.  
- Optionally, schedule trades through supported brokerage APIs (e.g. Alpaca). **Use caution and comply with applicable securities regulations.**  

## Disclaimer  

This repository is for educational purposes only. Investing in micro cap and penny stocks carries high risk; you can lose all invested capital. Do your own due diligence and consult a financial advisor before making any trades. Past performance is not indicative of future results.  

## Contributing  

Pull requests are welcome. Please ensure:  
- Code is formatted (black) and linted (flake8).  
- New features come with tests (pytest).  
- Documentation is updated accordingly.  

## License  

MIT License. See `LICENSE` file for details.
