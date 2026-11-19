"""Task 1 connection test: open a quote context, snapshot US.QQQ, print, close.

Run from the project root:
    .venv\\Scripts\\python.exe scripts\\check_opend.py
No credentials are read, stored or passed here. Log in to OpenD by hand first.
"""
import sys

from moomoo import RET_OK, OpenQuoteContext

HOST, PORT = "127.0.0.1", 11111


def main() -> int:
    ctx = OpenQuoteContext(host=HOST, port=PORT, ai_type=1)
    try:
        ret, state = ctx.get_global_state()
        if ret != RET_OK:
            print("get_global_state failed:", state)
            return 1
        print("OpenD server version:", state.get("server_ver"))
        print("quote logged in:", state.get("qot_logined"), "| trade logged in:", state.get("trd_logined"))
        print("US market state:", state.get("market_us"))

        ret, snap = ctx.get_market_snapshot(["US.QQQ"])
        if ret != RET_OK:
            print("get_market_snapshot failed:", snap)
            return 1
        cols = [c for c in ("code", "name", "update_time", "last_price", "prev_close_price",
                            "high_price", "low_price", "volume", "pre_price", "after_price",
                            "overnight_price") if c in snap.columns]
        print(snap[cols].T.to_string(header=False))
        return 0
    finally:
        ctx.close()


if __name__ == "__main__":
    sys.exit(main())
