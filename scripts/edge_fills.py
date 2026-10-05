"""How do our 2-leg vertical orders fill relative to the mid and the natural? Read-only.

Inputs (pulled from the droplet read-only into scratch/edge_fills/):
  qqq.log        zcat of /var/log/qqq-trading.log* (engine + orphan ladder), 2026-09-21 ->
  dte0.log       /var/log/dte0-trade.log   (stock rotation, 0DTE)
  weekly.log     /var/log/weekly-trade.log (stock rotation, weekly)
  orders.jsonl   broker GET /accounts/{id}/orders/{id} for every "Order submitted" id in the logs
  history.jsonl  broker GET /accounts/{id}/history?type=trade (leg fills, date only, no order id)

Sources: (a) engine = ids in "Order [PLAYBOOK] OPEN/CLOSE"; (b) orphan = other qqq.log ids,
split into ASK-ladder rests and rule closes (reason from the status line before it);
(c) rotation = ORDER SENT in dte0/weekly logs; (d) manual = history leg fills not explained
by any filled order from a-c (window 2026-09-21..2026-10-02, when all logs exist).

    python scripts/edge_fills.py
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

D = Path(__file__).resolve().parent.parent / "scratch" / "edge_fills"
TS = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),(\d{3})")
SUB = re.compile(r"Order submitted: \{'id': (\d+)")
ENG = re.compile(r"Order \[([^\]]+)\] (OPEN|CLOSE): \{'id': (\d+)")
ASK = re.compile(r"ORPHAN ASK: closing (\S+) ([\d.]+)/([\d.]+) x\d+ — \{'id': (\d+)")
STAT = re.compile(r"ORPHAN (\S+) ([CP]) ([\d.]+)/([\d.]+) x(\d+) (debit|credit): entry ([\d.]+) "
                  r"value ([-\d.]+) .*?— ([A-Z_]+)")
CHAIN = re.compile(r"Chain vs model \[(entry|open) [^\]]+\] \w+ ([\d.]+)/([\d.]+): .*market mid "
                   r"([\d.]+) .*natural ([\d.]+)/([\d.]+)")
PLAN = re.compile(r"^\S+ \S+ (\w+)\s+(CALL|PUT)\s+([\d.]+)/([\d.]+) w([\d.]+) x(\d+) @ ([\d.]+)")
QPCT = re.compile(r"^\S+ \S+ (\w+)\s+quote ([\d.]+)% of mid")


def ts(line: str) -> "datetime | None":
    m = TS.match(line)
    if not m:
        return None
    return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(
        microsecond=int(m.group(2)) * 1000, tzinfo=timezone.utc)


def bts(s: str) -> datetime:
    return datetime.strptime(s.replace("Z", ""), "%Y-%m-%dT%H:%M:%S.%f").replace(tzinfo=timezone.utc)


def parse_qqq() -> dict:
    out, last_stat, last_chain = {}, None, None
    lines = (D / "qqq.log").read_text(encoding="utf-8", errors="replace").splitlines()
    for ln in lines:
        t = ts(ln)
        if t is None:
            continue
        if (m := STAT.search(ln)) and m.group(9) != "holding":
            last_stat = (t, m)
        if m := CHAIN.search(ln):
            last_chain = (t, m)
        if m := SUB.search(ln):
            out[int(m.group(1))] = {"src": "b_close", "t": t}
            st = last_stat
            if st and (t - st[0]).total_seconds() < 30:
                s = st[1]
                out[int(m.group(1))].update(root=s.group(1), k=f"{s.group(3)}/{s.group(4)}",
                                            reason=s.group(9), natural=float(s.group(8)),
                                            struct=s.group(6))
            last_stat = None
        if m := ENG.search(ln):
            o = out.setdefault(int(m.group(3)), {"t": t})
            o.update(src="a_engine", reason=m.group(2))
            if last_chain and (t - last_chain[0]).total_seconds() < 15:
                c = last_chain[1]
                o.update(mid=float(c.group(4)), bid=float(c.group(5)), ask=float(c.group(6)))
        if m := ASK.search(ln):
            out[int(m.group(4))].update(src="b_ask", root=m.group(1),
                                        k=f"{m.group(2)}/{m.group(3)}", reason="ASK")
    return out


def parse_rot(name: str) -> dict:
    out, plan, qp = {}, None, {}
    for ln in (D / name).read_text(encoding="utf-8", errors="replace").splitlines():
        if m := QPCT.match(ln):
            qp[m.group(1)] = float(m.group(2))
        if m := PLAN.match(ln):
            plan = m
        if "ORDER SENT" in ln and plan:
            oid = int(re.search(r"'id': (\d+)", ln).group(1))
            out[oid] = {"src": "c_rot_" + name.split(".")[0], "t": ts(ln), "root": plan.group(1),
                        "k": f"{plan.group(3)}/{plan.group(4)}", "w": float(plan.group(5)),
                        "reason": "OPEN", "natural": float(plan.group(7)),
                        "qpct": qp.get(plan.group(1))}
            plan = None
    return out


def legs(o: dict) -> list:
    lg = o.get("leg") or []
    return [lg] if isinstance(lg, dict) else lg


def build() -> pd.DataFrame:
    meta = parse_qqq()
    meta.update(parse_rot("dte0.log"))
    meta.update(parse_rot("weekly.log"))
    rows = []
    for ln in (D / "orders.jsonl").read_text().splitlines():
        o = json.loads(ln)
        m = meta.get(o["id"], {"src": "?"})
        sell = o["type"] == "credit"            # order collects -> higher is better
        price = float(o.get("price") or 0)
        fill = abs(float(o.get("avg_fill_price") or 0)) if o["status"] == "filled" else None
        nat = m.get("natural")
        if m["src"] == "a_engine":
            nat = m["bid"] if sell else m["ask"]
        mid = m.get("mid")
        at_mid = False
        if m["src"] == "b_close" and nat is not None:
            # a profit close at the mid sends a price better than the natural
            if (price > nat + 0.005) if sell else (price < nat - 0.005):
                mid, at_mid = price, True
        lp = legs(o)
        if m["src"].startswith("c_rot") and m.get("qpct") and lp and o["status"] == "filled":
            # ESTIMATE: leg half-spread = qpct/2 x leg price (median near-ATM quote of that name)
            lsum = sum(float(x.get("avg_fill_price") or 0) for x in lp)
            mid = nat - m["qpct"] / 100 / 2 * lsum
        half = abs(nat - mid) if (nat is not None and mid is not None) else None
        slip = None
        if fill is not None and mid is not None:
            slip = (mid - fill) if sell else (fill - mid)      # + = paid away vs mid
        ttf = (bts(o["transaction_date"]) - bts(o["create_date"])).total_seconds() \
            if o.get("transaction_date") else None
        rows.append(dict(id=o["id"], src=m["src"], reason=m.get("reason"), root=o["symbol"],
                         k=m.get("k"), t=bts(o["create_date"]), status=o["status"],
                         qty=float(o.get("quantity") or 0), sell=sell, price=price, fill=fill,
                         natural=nat, mid=mid, at_mid=at_mid, half=half, slip=slip,
                         pos=(slip / half if slip is not None and half else None),
                         ttf=ttf, quoted_rt=(2 * half * 100 if half is not None else None),
                         nlegs=len(lp)))
    return pd.DataFrame(rows).sort_values("t")


def mid_episodes(df: pd.DataFrame) -> pd.DataFrame:
    """Consecutive mid-priced closes on one structure = one episode until fill or a 3-min gap."""
    mm = df[df.at_mid].sort_values("t")
    eps = []
    for (root, k), g in mm.groupby(["root", "k"]):
        cur = None
        for r in g.itertuples():
            if cur and (r.t - cur["last"]).total_seconds() <= 180 and not cur["filled"]:
                cur["n"] += 1
            else:
                cur = {"root": root, "k": k, "t0": r.t, "n": 1, "filled": False, "last": r.t,
                       "fill_after": None}
                eps.append(cur)
            cur["last"] = r.t
            if r.status == "filled":
                cur["filled"] = True
                cur["fill_after"] = (r.t - cur["t0"]).total_seconds() + (r.ttf or 0)
    return pd.DataFrame(eps)


def manual(df: pd.DataFrame) -> pd.DataFrame:
    """History leg fills not explained by any known filled order, per (day, symbol, side).

    History events carry signed quantity (+ bought, - sold) and a date, no order id and no
    time, so matching is by (day, option symbol, buy/sell) contract counts.
    """
    known = defaultdict(float)
    for l in (D / "orders.jsonl").read_text().splitlines():
        o = json.loads(l)
        if o["status"] != "filled":
            continue
        for lg in legs(o):
            q = float(lg.get("exec_quantity") or 0)
            sd = "buy" if lg["side"].startswith("buy") else "sell"
            known[(lg["transaction_date"][:10], lg["option_symbol"], sd)] += q
    hist = defaultdict(lambda: [0.0, 0, 0.0])
    for l in (D / "history.jsonl").read_text().splitlines():
        e = json.loads(l)
        tr = e.get("trade") or {}
        if tr.get("trade_type") != "option":
            continue
        q = float(tr["quantity"])
        key = (e["date"][:10], tr["symbol"], "buy" if q > 0 else "sell")
        hist[key][0] += abs(q)
        hist[key][1] += 1
        hist[key][2] += abs(q) * float(tr["price"])
    rows = []
    for key, (q, n, notional) in hist.items():
        rows.append(dict(day=key[0], sym=key[1], side=key[2], hist_qty=q, events=n,
                         px=notional / q if q else None, known_qty=known.get(key, 0.0),
                         unexplained=q - known.get(key, 0.0)))
    return pd.DataFrame(rows)


def main():
    pd.set_option("display.width", 220)
    df = build()
    df.to_csv(D / "fills.csv", index=False)
    print("window:", df.t.min(), "->", df.t.max())
    print(df.groupby(["src", "status"]).size().unstack(fill_value=0))
    print("\nby src/reason/status:")
    print(df.groupby(["src", "reason", "status"]).size().to_string())
    f = df[df.status == "filled"]
    print("\nfilled, time to fill (s) by src:")
    print(f.groupby("src").ttf.describe().round(1))
    print("\nfilled with a known mid: position 0=mid 1=natural, $/contract paid vs mid:")
    k = f.dropna(subset=["pos"])
    print(k.groupby(["src", "at_mid"]).agg(n=("pos", "size"), pos_med=("pos", "median"),
                                            pos_mean=("pos", "mean"),
                                            usd_vs_mid_med=("slip", lambda s: s.median() * 100),
                                            usd_vs_mid_mean=("slip", lambda s: s.mean() * 100),
                                            half_med=("half", lambda s: s.median() * 100)).round(3))
    print("\nfills vs limit ($/contract better than the limit):")
    f2 = f.assign(impr=lambda x: ((x.fill - x.price).where(x.sell, x.price - x.fill)) * 100)
    print(f2.groupby("src").impr.describe().round(2))
    print("\nmid-priced orders (b_close at_mid):")
    am = df[df.at_mid]
    print(am[["t", "root", "k", "reason", "status", "price", "natural", "half", "fill", "ttf"]]
          .to_string())
    ep = mid_episodes(df)
    if len(ep):
        print(f"\nmid episodes: {len(ep)}, filled {ep.filled.sum()}")
        for lim in (60, 120, 300):
            print(f"  filled within {lim}s of first mid post: "
                  f"{(ep.fill_after.fillna(1e9) <= lim).sum()}/{len(ep)}")
        print(ep[["root", "k", "t0", "n", "filled", "fill_after"]].to_string())
    print("\nquoted round-trip (2 x half-spread) $/contract at submission, by src:")
    q = df.dropna(subset=["quoted_rt"]).drop_duplicates(["root", "k", "src"])
    print(q.groupby(["src"]).quoted_rt.describe().round(2))
    print(q[["src", "root", "k", "natural", "mid", "quoted_rt"]].to_string())
    mn = manual(df)
    print(f"\nhistory: {mn.day.min()} .. {mn.day.max()}, {int(mn.events.sum())} option fill events, "
          f"{mn.hist_qty.sum():.0f} contracts")
    for lo, hi in (("2026-08-01", "2026-09-13"), ("2026-09-14", "2026-09-20"),
                   ("2026-09-21", "2026-10-02")):
        w = mn[(mn.day >= lo) & (mn.day <= hi)]
        un = w[w.unexplained > 0.01]
        print(f"  {lo}..{hi}: {w.hist_qty.sum():.0f} contracts, explained by logged orders "
              f"{w[['hist_qty', 'known_qty']].min(axis=1).sum():.0f}, unexplained "
              f"{un.unexplained.sum():.0f} in {int(un.events.sum())} events over {un.day.nunique()} "
              f"days; roots {sorted({re.match(r'[A-Z]+', x).group(0) for x in un.sym})}")


if __name__ == "__main__":
    main()
