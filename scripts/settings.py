"""View and change the tunable trading settings from the command line.

Writes the same trading_overrides.env the /desk/settings page writes; the next
cron cycle (within a minute) trades on it. No container restart. See
trading_engine/settings_overrides.py for precedence and the whitelist, and
commands.txt for the droplet copy-paste versions.

    python scripts/settings.py list [--group weekly]
    python scripts/settings.py get TRADING_ORPHAN_STOP_PCT
    python scripts/settings.py set TRADING_ORPHAN_STOP_PCT=-30 TRADING_ORPHAN_STALL_MINUTES=20
    python scripts/settings.py unset TRADING_ORPHAN_STOP_PCT
    python scripts/settings.py reset          # drop every override
    python scripts/settings.py log            # audit trail
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trading_engine import settings_overrides as so  # noqa: E402


def _who() -> str:
    return f"cli:{os.getenv('SUDO_USER') or os.getenv('USER') or 'root'}"


def cmd_list(group: str | None) -> None:
    snap = so.snapshot()
    last = None
    for r in snap["settings"]:
        if group and group.lower() not in r["group"].lower():
            continue
        if r["group"] != last:
            print(f"\n== {r['group']} ==")
            last = r["group"]
        mark = {"override": "*", "env": " ", "default": "."}[r["source"]]
        eff = r["effective"] if r["effective"] != "" else "(blank)"
        unit = f" {r['unit']}" if r["unit"] else ""
        print(f" {mark} {r['key']:<42} {eff:>8}{unit:<7} {r['label']}")
    print("\n  * = override (this file)   blank = .env.production   . = code default")
    print(f"  file: {snap['path']}")
    for p in snap["problems"]:
        print(f"  WARNING {p}")


def cmd_get(key: str) -> None:
    for r in so.snapshot()["settings"]:
        if r["key"] == key:
            for k in ("label", "group", "help", "unit", "min", "max", "default",
                      "env_value", "override", "effective", "source"):
                print(f"{k:>10}: {r[k]}")
            return
    sys.exit(f"{key} is not a tunable setting (python scripts/settings.py list)")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    ls = sub.add_parser("list")
    ls.add_argument("--group", help="substring of the group name: 0dte, weekly, account, entries")
    sub.add_parser("get").add_argument("key")
    sub.add_parser("set").add_argument("pairs", nargs="+", metavar="KEY=VALUE")
    sub.add_parser("unset").add_argument("keys", nargs="+")
    sub.add_parser("reset")
    sub.add_parser("log")
    a = p.parse_args()

    try:
        if a.cmd == "list":
            cmd_list(a.group)
        elif a.cmd == "get":
            cmd_get(a.key)
        elif a.cmd == "set":
            values = {}
            for pair in a.pairs:
                if "=" not in pair:
                    sys.exit(f"expected KEY=VALUE, got {pair!r}")
                k, _, v = pair.partition("=")
                values[k.strip()] = v
            so.set_many(values, who=_who())
            for k in values:
                cmd_get(k.strip())
                print()
            print("applies from the next cron cycle (within a minute)")
        elif a.cmd == "unset":
            so.unset(a.keys, who=_who())
            print(f"removed: {' '.join(a.keys)} -- .env.production value back in force next cycle")
        elif a.cmd == "reset":
            current, _ = so.read_file()
            so.unset(list(current), who=_who())
            print(f"removed {len(current)} override(s)")
        elif a.cmd == "log":
            try:
                with open(so.AUDIT_PATH, encoding="utf-8") as f:
                    sys.stdout.write(f.read() or "(empty)\n")
            except FileNotFoundError:
                print("(no changes recorded yet)")
    except ValueError as e:
        sys.exit(f"refused: {e}")


if __name__ == "__main__":
    main()
