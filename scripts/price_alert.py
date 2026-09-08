"""Email an alert when an underlying crosses a level. Fires ONCE per crossing.

    python scripts/price_alert.py --rules "SNDK<1800"
    python scripts/price_alert.py --rules "SNDK<1800,CRWV<101,NVDA>235"
    python scripts/price_alert.py --rules "SNDK<1800" --status
    python scripts/price_alert.py --rules "SNDK<1800" --force   # ignore the clock

DELIVERY. Two channels, either or both:

    ALERT_WEBHOOK_URL   an HTTPS webhook (Slack, Discord, Teams, anything that
                        accepts a JSON POST). THIS IS THE ONE THAT WORKS.
    ALERT_EMAIL / --to  SMTP through helpers.mailer.

SMTP IS BLOCKED ON THIS DROPLET AND CANNOT BE FIXED IN CODE. Measured
2026-09-08: ufw allows outgoing, but ports 587 and 465 both time out from the
host as well as the container, while 443 connects fine. That is the provider
blocking SMTP egress, which DigitalOcean does by default. The email path is
kept because it costs nothing and works the moment egress is opened or the
mailer is pointed at an HTTP mail API -- but a webhook is the channel to use
today.

Every firing is also appended to the state file regardless of channel, so an
alert is never lost just because delivery failed.

WHY THIS IS NOT A ONE-LINE CRON. Three things make a naive price check useless
in practice, and all three are the reason this file exists:

  1. IT WOULD SPAM. A cron that mails whenever SNDK < 1800 mails every minute
     for the rest of the session. State is kept per rule, so a crossing fires
     once and then goes quiet.

  2. IT WOULD SPAM ANYWAY, from noise. A level sitting inside the bid-ask, or a
     name oscillating around it, re-crosses constantly. A rule only RE-ARMS
     once price recovers past the level by REARM_PCT, so 1800.05 does not
     re-arm a 1800 alert -- 1804.50 does.

  3. IT WOULD FIRE AT 04:00. The quote feed answers outside regular hours with
     thin or stale prices. The clock and calendar are checked the same way
     run_cycle.py and news_watch.py check them, holidays included.

STATE IS A FILE, deliberately: the alert must work even when the database is
the thing that broke, and one JSON file needs no migration and no schema
decision made before anyone knows what alerting will need.

AN HONEST LIMITATION. This depends on the droplet, its cron and its network. A
hard price trigger you actually rely on belongs at the BROKER, where it runs on
their infrastructure and fires whether or not this box is up. Use this for
"tell me so I can look", not for anything that must not be missed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

NY = ZoneInfo("America/New_York")
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_PATH = os.getenv("PRICE_ALERT_STATE",
                       os.path.join(REPO_ROOT, "data", "price_alerts.json"))
# How far back past the level price must recover before the rule can fire
# again. 0.25% of the level -- about 4.50 on SNDK at 1800.
REARM_PCT = float(os.getenv("PRICE_ALERT_REARM_PCT", "0.25"))
WEBHOOK_URL = os.getenv("ALERT_WEBHOOK_URL", "")
OPEN_T, CLOSE_T = dtime(9, 30), dtime(16, 0)


def parse_rules(raw: str) -> list:
    """'SNDK<1800,CRWV>105' -> [(symbol, op, level), ...]"""
    out = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        for op in ("<=", ">=", "<", ">"):
            if op in part:
                sym, _, lvl = part.partition(op)
                out.append((sym.strip().upper(), op.strip(), float(lvl)))
                break
        else:
            raise ValueError(f"cannot parse rule {part!r}; use SNDK<1800")
    return out


def load_state() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
    os.replace(tmp, STATE_PATH)


def spot(symbols) -> dict:
    from trading_engine.tradier_orders import quotes

    q = quotes(list(symbols))
    out = {}
    for s in symbols:
        d = q.get(s) or {}
        px = d.get("last") or d.get("close")
        if px:
            out[s] = float(px)
    return out


def triggered(op: str, price: float, level: float) -> bool:
    return {"<": price < level, "<=": price <= level,
            ">": price > level, ">=": price >= level}[op]


def rearmed(op: str, price: float, level: float) -> bool:
    """Has price recovered far enough past the level to allow another alert?"""
    band = abs(level) * REARM_PCT / 100.0
    if op in ("<", "<="):
        return price > level + band
    return price < level - band


def post_webhook(subject: str, body: str) -> bool:
    """POST to ALERT_WEBHOOK_URL. Never raises.

    The payload carries `text` AND `content` because Slack reads the first and
    Discord the second, so one URL field works for either without the caller
    having to say which service it is. `subject` and `body` are included too
    for anything that takes arbitrary JSON.
    """
    if not WEBHOOK_URL:
        return False
    try:
        import httpx

        r = httpx.post(WEBHOOK_URL,
                       json={"text": body, "content": body,
                             "subject": subject, "body": body},
                       timeout=10.0)
        if r.status_code >= 400:
            print(f"webhook returned {r.status_code}: {r.text[:200]}")
            return False
        return True
    except Exception as exc:
        print(f"webhook failed: {exc}")
        return False


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rules", required=True,
                    help='comma separated, e.g. "SNDK<1800,CRWV<101"')
    ap.add_argument("--to", default=os.getenv("ALERT_EMAIL", ""))
    ap.add_argument("--force", action="store_true",
                    help="skip the market-hours and trading-day checks")
    ap.add_argument("--status", action="store_true",
                    help="print each rule and its armed state, send nothing")
    ap.add_argument("--reset", action="store_true",
                    help="re-arm every rule and exit")
    args = ap.parse_args()

    rules = parse_rules(args.rules)
    state = load_state()

    if args.reset:
        for sym, op, lvl in rules:
            state.pop(f"{sym}{op}{lvl}", None)
        save_state(state)
        print(f"re-armed {len(rules)} rule(s)")
        return

    now = datetime.now(NY)
    if not (args.force or args.status):
        from trading_engine import market_calendar

        if not market_calendar.is_trading_day(now.date()):
            print(f"{now:%Y-%m-%d %H:%M %Z} — not a trading day.")
            return
        if not (OPEN_T <= now.time() <= CLOSE_T):
            print(f"{now:%Y-%m-%d %H:%M %Z} — outside regular hours.")
            return

    prices = spot({r[0] for r in rules})
    if not prices:
        print("no quotes returned; nothing checked")
        return

    fired = []
    for sym, op, lvl in rules:
        key = f"{sym}{op}{lvl}"
        px = prices.get(sym)
        if px is None:
            print(f"{sym:6s} no quote")
            continue
        armed = key not in state  # "_history" is not a rule key and never collides
        hit = triggered(op, px, lvl)
        if args.status:
            print(f"{sym:6s} {px:10.2f}  rule {op}{lvl:<10.2f} "
                  f"{'ARMED' if armed else 'fired ' + state[key].get('at', '')}"
                  f"{'  [condition true now]' if hit else ''}")
            continue
        if hit and armed:
            fired.append((sym, op, lvl, px))
            state[key] = {"at": now.strftime("%Y-%m-%d %H:%M %Z"), "price": px}
        elif not armed and rearmed(op, px, lvl):
            # Recovered past the level by more than the band: allow it again.
            del state[key]
            print(f"{sym:6s} {px:10.2f}  re-armed {op}{lvl}")

    if args.status:
        return
    save_state(state)

    if not fired:
        print(f"{now:%H:%M %Z}  " + "  ".join(
            f"{s}={prices.get(s, float('nan')):.2f}" for s in sorted(prices)))
        return

    from helpers import mailer

    lines = [f"{s} {px:.2f} crossed {op}{lvl:g}" for s, op, lvl, px in fired]
    subject = "Price alert: " + "; ".join(lines)
    body = (subject + "\n\n"
            + f"as of {now:%Y-%m-%d %H:%M %Z}\n\n"
            + "\n".join(f"  {s:6s} {px:10.2f}   rule {op}{lvl:g}"
                        for s, op, lvl, px in fired)
            + "\n\nEvery rule above has now fired and will stay quiet until "
              f"price recovers past its level by {REARM_PCT}%.\n"
              "This alert runs on the trading droplet's cron. A trigger you "
              "cannot afford to miss belongs at the broker instead.\n")
    for line in lines:
        print("FIRED: " + line)

    # Record the firing before attempting delivery. A dropped webhook must not
    # also lose the fact that the level was crossed.
    hist = state.setdefault("_history", [])
    hist.append({"at": now.strftime("%Y-%m-%d %H:%M %Z"), "alerts": lines})
    del state["_history"]
    state["_history"] = hist[-200:]
    save_state(state)

    sent = []
    if post_webhook(subject, body):
        sent.append("webhook")
    if args.to:
        if mailer.send(args.to, subject, body):
            sent.append("email")
        else:
            print(f"email to {args.to} failed -- SMTP egress is blocked on "
                  f"this host (ports 587/465 time out); use ALERT_WEBHOOK_URL")
    if sent:
        print("delivered via " + ", ".join(sent))
    else:
        print("NOT DELIVERED anywhere. Set ALERT_WEBHOOK_URL. The firing is "
              f"recorded in {STATE_PATH} either way.")


if __name__ == "__main__":
    main()
