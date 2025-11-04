import requests
from fastapi import APIRouter

router = APIRouter()


@router.get("/social_trending")
def get_social_trending():
    """Fetch trending tickers from StockTwits, score sentiment, and filter under $10."""
    try:
        # 1️⃣ Get trending tickers
        resp = requests.get("https://api.stocktwits.com/api/2/trending/symbols.json")
        symbols = resp.json().get("symbols", [])

        trending = []
        for sym in symbols:
            symbol = sym["symbol"]
            name = sym.get("title", "")

            # 2️⃣ Get sentiment messages for each symbol
            sentiment_data = {"bullish": 0, "bearish": 0}
            try:
                msg_resp = requests.get(f"https://api.stocktwits.com/api/2/streams/symbol/{symbol}.json")
                messages = msg_resp.json().get("messages", [])
                for m in messages:
                    sentiment = m.get("entities", {}).get("sentiment", {})
                    if sentiment:
                        if sentiment.get("basic") == "Bullish":
                            sentiment_data["bullish"] += 1
                        elif sentiment.get("basic") == "Bearish":
                            sentiment_data["bearish"] += 1
            except Exception:
                pass

            # 3️⃣ Compute sentiment index (0 to 1)
            total = sentiment_data["bullish"] + sentiment_data["bearish"]
            sentiment_index = round(sentiment_data["bullish"] / total, 2) if total else None

            # 4️⃣ Filter by price (under $10)
            try:
                q = requests.get(f"https://query1.finance.yahoo.com/v7/finance/quote?symbols={symbol}")
                res = q.json()["quoteResponse"]["result"]
                price = res[0]["regularMarketPrice"] if res else None
            except Exception:
                price = None

            if price and price <= 10:
                trending.append({
                    "symbol": symbol,
                    "name": name,
                    "price": price,
                    "bullish": sentiment_data["bullish"],
                    "bearish": sentiment_data["bearish"],
                    "sentiment_index": sentiment_index
                })

        return {"count": len(trending), "trending": trending}

    except Exception as e:
        return {"error": str(e)}
