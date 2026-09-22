"""Read-only connection check. Never prints credentials, tokens or balances."""
import json
import os
import requests
from .upbit import UpbitClient, UpbitError


def main():
    result = {"orders_submitted": 0}
    try:
        result["outbound_ip"] = requests.get("https://api4.ipify.org", timeout=8).text.strip()
    except requests.RequestException:
        result["outbound_ip"] = None
    client = UpbitClient(os.environ["UPBIT_ACCESS_KEY"], os.environ["UPBIT_SECRET_KEY"], max_retries=1, timeout=8)
    for name, call in (("accounts", client.accounts), ("orders_chance", lambda: client.orders_chance("KRW-BTC"))):
        try:
            data = call()
            result[name] = {"ok": isinstance(data, list if name == "accounts" else dict)}
        except UpbitError as exc:
            allowed = {"no_authorization_ip", "out_of_scope", "expired_access_key", "jwt_verification", "invalid_access_key"}
            result[name] = {"ok": False, "status": exc.status, "reason": exc.name if exc.name in allowed else "upbit_auth_or_connection_error"}
        except Exception:
            result[name] = {"ok": False, "reason": "connection_error"}
    print(json.dumps(result))


if __name__ == "__main__":
    main()
