"""One-shot underlying trigger, armed by the operator 2026-09-18 11:08 ET:
close SNDK 1605/1710 the moment SNDK prints below 1709.50. Fires once. Kill:
pkill -f sndk_trigger. Logs to /app/sndk_trigger.log (host: /opt/fastapi/)."""
import time, logging
from datetime import datetime
from trading_engine import orphans as o
from trading_engine.data_feed import fetch_spot
logging.basicConfig(filename='/app/sndk_trigger.log', level=logging.INFO, format='%(asctime)s %(message)s')
LEVEL = 1709.50
logging.info('ARMED: close SNDK 1605/1710 if SNDK < %.2f', LEVEL)
while True:
    try:
        px = fetch_spot('SNDK')
        logging.info('SNDK %.2f', px)
        if px is not None and px < LEVEL:
            sts = [s for s in (o.open_structures() or []) if s['root']=='SNDK' and s['long_strike']==1605.0 and s['short_strike']==1710.0]
            if not sts:
                logging.info('TRIGGERED at %.2f but position not found (already closed?) -- exiting', px); break
            st = sts[0]; m = o._mark(st)
            if not m:
                logging.warning('TRIGGERED at %.2f but no mark -- retrying', px); time.sleep(5); continue
            res = o._close(st, 'OPERATOR_UNDERLYING_TRIGGER', m[0])
            logging.info('TRIGGERED at %.2f: closed at %.2f (entry %.2f, books %+.0f) -> %s', px, m[0], abs(st['entry']), (m[0]-abs(st['entry']))*st['qty']*100, res)
            break
    except Exception:
        logging.exception('trigger loop error')
    time.sleep(15)
