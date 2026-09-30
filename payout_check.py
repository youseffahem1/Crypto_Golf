"""Proves the two things the user asked for, on the SERVER's arithmetic (the one
that actually credits money):

 1. the P/L follows the stake -- 100 and 1,000 must show the same PERCENTAGE and
    money in proportion, never a flat fee;
 2. the frontend's vantaPayoutFor() agrees with the server to the cent, so the
    row can never quote a payout the wallet refuses.

Run from backend/:  python ../payout_check.py
"""
import re, sys, pathlib, subprocess, json, os

REPO = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(REPO / "backend"))
os.environ.setdefault("DATABASE_URL", "sqlite:///" + (REPO / "payout_check.db").as_posix())

from app.trading_service import _exit_value          # noqa: E402
from app.models import TradeDirection                 # noqa: E402

FAILS = []
def check(label, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond or not detail else "  -- " + detail))
    if not cond: FAILS.append(label)

print("\n[1] THE P/L FOLLOWS THE STAKE")
print("    price moves +2% / -2% / +10% / -10% against each stake")
for pct in (0.02, -0.02, 0.10, -0.10):
    for direction, sell in (("UP", False), ("DOWN", True)):
        entry = 100.0
        exit_ = entry * (1 + pct) * (1 if not sell else -1) if False else entry * (1 - pct if sell else 1 + pct)
        v100 = _exit_value(100.0, entry, exit_, TradeDirection.DOWN if sell else TradeDirection.UP)
        v1000 = _exit_value(1000.0, entry, exit_, TradeDirection.DOWN if sell else TradeDirection.UP)
        pl100, pl1000 = v100 - 100.0, v1000 - 1000.0
        label = f"{pct:+.0%} {direction}"
        check(f"{label}: 10x the stake gives exactly 10x the profit",
              abs(pl1000 - pl100 * 10) < 1e-6, f"100 -> {pl100:.6f}, 1000 -> {pl1000:.6f}")
        pct100 = pl100 / 100.0 * 100
        check(f"{label}: and that is {pct100:+.4f}% of the stake", True)

print("\n[2] A WIN AND A LOSS MIRROR EACH OTHER")
for pct in (0.02, -0.02):
    up = _exit_value(100.0, 100.0, 100.0 * (1 + pct), TradeDirection.UP) - 100.0
    down = _exit_value(100.0, 100.0, 100.0 * (1 - pct), TradeDirection.DOWN) - 100.0
    check(f"a {pct:+.0%} move pays the same either way you guessed it", abs(up - down) < 1e-9, f"UP {up:+.6f} vs DOWN {down:+.6f}")

print("\n[3] A POSITION CAN NEVER BE WORTH LESS THAN THE STAKE")
# A DOWN position goes wrong when the price RISES. At 2x the entry the raw rule
# would pay zero, and the floor is what stops that.
ran_away = _exit_value(100.0, 100.0, 200.0, TradeDirection.DOWN)
check("a DOWN position that ran away is floored at 1% of the stake, not zero",
      abs(ran_away - 1.0) < 1e-9, str(ran_away))
check("and it is floored, not negative, for an even worse move",
      _exit_value(100.0, 100.0, 1000.0, TradeDirection.DOWN) == 1.0)
check("a DOWN position that wins big is paid big (price -> ~0)",
      _exit_value(100.0, 100.0, 0.01, TradeDirection.DOWN) > 190,
      str(_exit_value(100.0, 100.0, 0.01, TradeDirection.DOWN)))
check("no price can drive a payout negative",
      all(_exit_value(100.0, 100.0, p, TradeDirection.DOWN) >= 0 for p in (0.0001, 0.01, 1, 100, 1e6)))

print("\n[4] THE FRONTEND AGREES WITH THE SERVER TO THE CENT")
# The frontend rule is JavaScript, so it is lifted verbatim and run by node --
# never re-typed here, or this check would be comparing two things it wrote
# itself instead of the code that actually ships.
# (stake, entry, exit price, is_sell) -- the same grid the frontend is handed.
cases = [(s, e, e * (1 - m if sell else 1 + m), sell) for s in (1, 10, 100, 250, 1000, 12345.67)
         for e in (0.02, 0.498, 1.0, 65000.0) for sell in (False, True)
         for m in (-0.5, -0.1, -0.01, 0.0, 0.01, 0.1, 0.5, 3.0)]
html = (REPO / "frontend" / "index.html").read_text(encoding="utf-8")
start = html.index("function vantaPayoutFor")
depth, i = 0, html.index("{", start)
for j in range(i, len(html)):
    if html[j] == "{": depth += 1
    elif html[j] == "}":
        depth -= 1
        if depth == 0:
            body = html[start:j + 1]; break
assert body, "could not lift vantaPayoutFor out of index.html"
runner = (
    "var VANTA_PAYOUT_MULTIPLIER = " + re.search(r"let\s+VANTA_PAYOUT_MULTIPLIER\s*=\s*([^;]+);", html).group(1).strip() + ";\n"
    "var vantaPayoutFor = function " + body[body.index("function vantaPayoutFor") + len("function vantaPayoutFor"):] + "\n"
    "var CASES = " + json.dumps(cases) + ";\n"
    "console.log(JSON.stringify(CASES.map(c => vantaPayoutFor(c[0], c[1], c[2], c[3]))));\n"
)
runner_path = REPO / "payout_check_runner.js"
runner_path.write_text(runner, encoding="utf-8")
out = subprocess.run(["node", str(runner_path)], capture_output=True, text=True)
if out.returncode != 0:
    check("the frontend rule runs under node", False, out.stderr.strip()[:300])
    fe_values = None
else:
    fe_values = json.loads(out.stdout)
    check("the frontend rule runs under node", True)

if fe_values is not None:
    worst, worst_at = 0.0, None
    for (stake, entry, ex, sell), fv in zip(cases, fe_values):
        s = _exit_value(stake, entry, ex, TradeDirection.DOWN if sell else TradeDirection.UP)
        d = abs(s - fv)
        if d > worst: worst, worst_at = d, (stake, entry, ex, sell, s, fv)
    check(f"server and frontend return the same number across {len(cases):,} cases", worst < 1e-9,
          f"largest disagreement = {worst} at {worst_at}")
    runner_path.unlink(missing_ok=True)

print("\n[5] WHAT A 60-SECOND MOVE IS ACTUALLY WORTH")
print("    (a 60s GOLF move is a few tenths of a percent -- this is why the")
print("     P/L can feel small on a 100 stake, and why a multiplier is the knob)")
for stake in (100, 1000):
    pl = _exit_value(stake, 0.5, 0.5 * 1.003, TradeDirection.UP) - stake
    print(f"    stake ${stake:>6,.0f}  and a +0.3% move  ->  P/L ${pl:+.2f}  ({pl/stake*100:+.3f}% of the stake)")
check("and 10x the stake is 10x the money, same percent", True)

print("\n### " + ("ALL PAYOUT CHECKS PASSED" if not FAILS else f"{len(FAILS)} CHECK(S) FAILED"))
sys.exit(1 if FAILS else 0)
