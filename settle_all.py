import time
from datetime import datetime, timezone
from db import Database, Environment
from ingestion.api.client import KalshiClient
from risk_engine.settler import Settler

client = KalshiClient(base_url="https://api.elections.kalshi.com/trade-api/v2")

targets = [
    ("trading_bot_sell_3n_prod_sl.db", Environment.PRODUCTION),
    ("trading_bot_last_9_demo_sl.db", Environment.DEMO),
    ("trading_bot_random_demo_sl.db", Environment.DEMO),
]

print("Starting settlement monitor for 18:15 EST markets...")

for attempt in range(30):  # Up to 5 minutes
    now_utc = datetime.now(timezone.utc)
    now_iso = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    total_unsettled = 0

    for db_path, env in targets:
        db = Database(db_path, environment=env)
        to_settle = db.get_positions_to_settle(now_iso)
        if to_settle:
            total_unsettled += len(to_settle)
            settler = Settler(client=client, db=db)
            settled = settler.settle(now_iso)
            if settled:
                print(f"[{now_iso}] {db_path} settled {len(settled)} positions:")
                for s in settled:
                    print(f"  {s['ticker']} ({s['side']}): result={s['result']}, PnL={s['pnl_cents']}c")

    if total_unsettled == 0 and now_utc.minute >= 15:
        print("All positions settled!")
        break

    time.sleep(10)
