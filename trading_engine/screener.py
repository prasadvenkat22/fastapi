"""Import shim so the HTTP layer and the CLI screener share one implementation.

WHY A SHIM AND NOT A MOVE. The vertical screener lives in scripts/weekly_pick.py
and is ~500 lines of measured, corrected maths -- the three probability engines,
log-return demeaning, the chain-priced fills, the news overlay whose sign bug
section 122 caught. Copying any of it here to serve an endpoint would create a
second copy to keep in step, and the first divergence would be silent: the API
would answer a slightly different question from the command line and nobody
would notice until the two were compared.

So the logic stays where it is and this module adds scripts/ to sys.path and
re-exports it. The ugliness is one documented import, in one file, instead of a
duplicated model.

If weekly_pick ever grows a third caller, move the maths INTO this module and
make the script import from here -- that is the right shape, and it is not
worth the risk of moving 500 working lines today.
"""

from __future__ import annotations

import os
import sys

_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

from weekly_pick import (  # noqa: E402
    SORT_KEY,
    SORT_LABEL,
    evaluate,
    flow_read,
    rank,
)

__all__ = ["SORT_KEY", "SORT_LABEL", "evaluate", "flow_read", "rank", "flow_table"]


def flow_table(symbols, day: str = "", interval: str = "5min") -> list:
    """Today's tape for a list of symbols: the cross-section flow.py prints.

    Separate from rank(): flow.py ranks NAMES and weekly_pick ranks STRUCTURES
    within a name, and a UI wants both. Returns one dict per symbol, symbols
    that fail omitted rather than raising -- a screen over six names should
    return the five that answered.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from flow import analyse

    day = day or datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    out = []
    for sym in [str(s).strip().upper() for s in symbols if str(s).strip()]:
        try:
            r = analyse(sym, day, interval, False)
        except Exception:
            continue
        if not r or not r.get("vwap"):
            continue
        tot = r["up"] + r["dn"]
        up_pct = (r["up"] / tot * 100.0) if tot else 50.0
        vs = (r["last"] / r["vwap"] - 1.0) * 100.0
        if vs > 0 and up_pct > 55:
            label = "BUY"
        elif vs < 0 and up_pct < 45:
            label = "SELL"
        else:
            label = "MIXED"
        out.append({
            "symbol": sym, "vwap": round(r["vwap"], 4),
            "last": round(r["last"], 4), "vs_vwap_pct": round(vs, 4),
            "vwap_slope_pct": round(r.get("slope") or 0.0, 4),
            "bars_above_vwap_pct": round(r.get("above_pct") or 0.0, 2),
            "up_volume_pct": round(up_pct, 2),
            "net_signed_volume": int(r["net"]),
            "volume": int(r["vol"]),
            "volume_vs_adv": (round(r["vs_adv"], 4) if r.get("vs_adv") else None),
            "bars": r.get("bars"),
            "label": label,
        })
    return out
