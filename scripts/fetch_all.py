"""검증용 대량 캔들 수집. 유동성 상위 KRW 마켓 × 다중 타임프레임."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ubbit.upbit import UpbitClient

OUT = "data/candles"
UNITS = [15, 60, 240]
BARS = {15: 6000, 60: 6000, 240: 3000}
TOP_N = int(sys.argv[1]) if len(sys.argv) > 1 else 20


def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    client = UpbitClient()
    markets = [m["market"] for m in client.markets(is_details=True) if m["market"].startswith("KRW-")]

    turnover = {}
    for i in range(0, len(markets), 100):
        for t in client.ticker(markets[i : i + 100]):
            turnover[t["market"]] = float(t.get("acc_trade_price_24h") or 0.0)
    top = sorted(turnover, key=lambda m: turnover[m], reverse=True)[:TOP_N]
    print("대상:", ", ".join(top), flush=True)

    for market in top:
        for unit in UNITS:
            path = os.path.join(OUT, f"{market}_{unit}m.json")
            want = BARS[unit]
            if os.path.exists(path):
                with open(path, encoding="utf-8") as fh:
                    if len(json.load(fh)) >= want * 0.9:
                        continue
            rows, to = [], None
            while len(rows) < want:
                batch = client.candles(market, unit=unit, count=min(200, want - len(rows)), to=to)
                if not batch:
                    break
                rows = batch + rows
                to = batch[0]["candle_date_time_utc"]
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(rows, fh, ensure_ascii=False)
            print(f"{market} {unit}m {len(rows)}봉 {rows[0]['candle_date_time_kst'][:10]}~{rows[-1]['candle_date_time_kst'][:10]}", flush=True)


if __name__ == "__main__":
    main()
