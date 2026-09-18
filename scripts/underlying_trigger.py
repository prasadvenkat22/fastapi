"""One-shot operator trigger on the UNDERLYING, not the spread mark.

    python scripts/underlying_trigger.py SYMBOL LONG_STRIKE SHORT_STRIKE LEVEL

Closes the named debit spread the moment the share price prints below LEVEL.
Fires once, through orphans._close (so the holdings clamp applies), then
exits. Does NOT re-arm on a recovery. Kill: pkill -f underlying_trigger.
Log: /app/underlying_trigger_SYMBOL.log (host: /opt/fastapi/).

WHY THE UNDERLYING. The spread mark on an illiquid name is a lagging, noisy
read of the share price through two option quotes -- on 2026-09-18 SNDK's mark
sat 16 points under intrinsic while the stock was pinned above the short
strike. The share price against the short strike is the one number that
decides whether the pin holds, so that is the number this watches.
"""
import sys, time, logging
from trading_engine import orphans as o
from trading_engine.data_feed import fetch_spot
sym, lo, hi, level = sys.argv[1].upper(), float(sys.argv[2]), float(sys.argv[3]), float(sys.argv[4])
logging.basicConfig(filename=f'/app/underlying_trigger_{sym}.log', level=logging.INFO, format='%(asctime)s %(message)s')
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.info('ARMED: close %s %g/%g if %s < %.2f', sym, lo, hi, sym, level)
while True:
    try:
        px = fetch_spot(sym)
        logging.info('%s %.2f', sym, px)
        if px is not None and px < level:
            sts = [s for s in (o.open_structures() or []) if s['root']==sym and s['long_strike']==lo and s['short_strike']==hi]
            if not sts:
                logging.info('TRIGGERED at %.2f but %s %g/%g not found (already closed?) -- exiting', px, sym, lo, hi); break
            st = sts[0]; m = o._mark(st)
            if not m:
                logging.warning('TRIGGERED at %.2f but no mark -- retrying', px); time.sleep(5); continue
            res = o._close(st, 'OPERATOR_UNDERLYING_TRIGGER', m[0])
            e = abs(st['entry'])
            logging.info('TRIGGERED at %.2f: closed %s %g/%g x%d at %.2f (entry %.2f, books %+.0f) -> %s', px, sym, lo, hi, st['qty'], m[0], e, (m[0]-e)*st['qty']*100, res)
            break
    except Exception:
        logging.exception('trigger loop error')
    time.sleep(15)
