"""Run a sweep against the LIVE production config.

The sweep's whole value is that it replays the engine AS DEPLOYED, so it has
to see the same TRADING_* values the droplet does. .env.production is not in
the repo (and the repo's .env is a decoy carrying a different budget), so the
values are pulled off the droplet and loaded here.

REFRESH trading.env BEFORE EVERY RUN. A snapshot taken once and reused goes
stale the moment anything is deployed, and a sweep against a stale snapshot
measures a configuration that no longer exists while printing a banner that
looks correct. That happened on 2026-09-05: the snapshot still carried
RIDE_TAKE_PROFIT=0 and no win cooldown hours after both were deployed, so
every sweep run that day measured the previous configuration.

    ssh root@<droplet> 'grep -E "^TRADING_" /opt/fastapi/.env.production'         > scratchpad/trading.env

INTRABAR STOPS DEFAULT OFF here, deliberately. See the note on
SWEEP_INTRABAR_STOPS in sweep.py: it models a resting stop order the engine
does not place, and turning it on made the harness stop out a trade the
engine rode to +478.
"""
import os
import runpy
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
for line in open(os.path.join(HERE, "trading.env"), encoding="utf-8"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ[k] = v.strip().strip('"')

os.environ.setdefault("SWEEP_CHAIN_PRICING", "true")
os.environ.setdefault("SWEEP_INTRABAR_STOPS", "false")
sys.path.insert(0, "C:/fastapi")
sys.argv = ["sweep.py"] + sys.argv[1:]
runpy.run_path("C:/fastapi/scripts/sweep.py", run_name="__main__")
