from signals.gemini_client import GeminiTradingClient
try:
    client = GeminiTradingClient()
    balance = client.get_balance()
    print(f"Gemini Balance: ${balance['balance'] / 100:.2f}")
except Exception as e:
    print(f"Error: {e}")
