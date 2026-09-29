"""PostgreSQL compatibility of the startup migration.

A deployment on Render failed to boot with

    psycopg.errors.UndefinedObject: type "datetime" does not exist

because the migration added trades.frozen_at as `DATETIME`. DATETIME is a MySQL
type. PostgreSQL has no such type, and neither does the SQL standard. SQLite
accepts it only because its type system is advisory -- it will take almost any
name and file the value away without complaint -- which is exactly why the bug
could sit unnoticed through a long stretch of local testing: the only database in
the loop was the one that cannot complain.

The fix is to resolve the declaration per dialect (app/main.py::_timestamp_decl)
so a column type is never written as a literal that the current backend might
not have.

This file checks the two paths that can create the column, because they are
different code and only one of them used to be wrong:

  1. FRESH DATABASE -- the model is rendered to DDL. Never emitted DATETIME,
     because SQLAlchemy maps DateTime to a real timestamp type.
  2. EXISTING DATABASE -- _migrate() ALTERs each missing column. This is the
     path that broke, and it is the one a live Render database takes.

It also pins the type to TIMESTAMP *WITHOUT* time zone on purpose, and checks
that against the type of the neighbouring timestamp columns rather than merely
asserting a string. See _timestamp_decl's docstring for why WITH TIME ZONE would
be the wrong fix here: it would make trades.frozen_at the only column in the
table that hands the ORM a timezone-aware datetime, while every comparison in
the trading service is against a naive datetime.utcnow().

Phase 1 (static + dialect compile) always runs and needs no server.
Phase 2 (live) runs the real startup migration against a real PostgreSQL and is
opt-in via PG_TEST_ADMIN_URL, e.g.

    PG_TEST_ADMIN_URL=postgresql://postgres:pw@127.0.0.1:5432/postgres \\
        python smoke_migration_postgres.py

Phase 2 creates and drops its own throwaway database and touches nothing else.
"""

import os
import re
import sys
import uuid

FAILS = []


def check(label, got, want):
    if got != want:
        FAILS.append(f"{label}: got {got!r}, want {want!r}")
        print(f"  FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"  ok   {label} = {got!r}")


def truthy(label, cond):
    check(label, bool(cond), True)


MAIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app", "main.py")

print("=" * 74)
print("PHASE 1 -- static and dialect-level (no database server required)")
print("=" * 74)

src = open(MAIN, encoding="utf-8").read()

# ---------------------------------------------------------------------------
# 1a. No raw DDL may hand PostgreSQL a type it does not have.
# ---------------------------------------------------------------------------
# Only the string literals actually passed to _add_column() count here: those
# are dialect-INDEPENDENT, so a DATETIME among them would be a statement
# PostgreSQL runs verbatim. The dialect-aware helper is checked separately in
# (1d), by resolving it against the real PostgreSQL dialect, because its MySQL
# branch returning DATETIME is correct and is never seen by PostgreSQL.
add_column_calls = re.findall(r'_add_column\(\s*"[^"]+"\s*,\s*"[^"]+"\s*,\s*"([^"]+)"', src)
helper_branches = re.findall(r'return\s+"([A-Z][A-Z ]+)"', src)

print("\n[1a] every column type the migration hands to a backend:")
for decl in dict.fromkeys(add_column_calls):
    print(f"      literal   {decl}")
for decl in dict.fromkeys(helper_branches):
    print(f"      helper    {decl}")

check("no dialect-INDEPENDENT declaration is DATETIME", "DATETIME" in add_column_calls, False)
truthy(
    "DATETIME survives only inside the dialect-aware helper, not as a literal",
    "DATETIME" not in add_column_calls and len(helper_branches) > 0,
)

# ---------------------------------------------------------------------------
# 1b. The frozen_at column is declared through the dialect, not as a literal.
# ---------------------------------------------------------------------------
# frozen_at is the last entry in that list, so anchor to the end of the line.
# A lazy capture alone would stop at the ')' of `_timestamp_decl(` and cut the
# expression in half.
frozen_line = re.search(r'\("trades",\s*"frozen_at",\s*(.+?)\s*\)\s*,?\s*$', src, re.M)
truthy("frozen_at is still in the migration list", frozen_line is not None)
decl_expr = frozen_line.group(1).strip() if frozen_line else ""
check("frozen_at is declared via the dialect helper", decl_expr, "_timestamp_decl()")
truthy("...and it is NOT a bare DATETIME literal anymore", decl_expr != '"DATETIME"')

# The three frozen columns must all still be there, with the same purpose.
for col in ("frozen_exit_price", "frozen_profit"):
    truthy(f'{col} is still declared as "FLOAT"', f'("trades", "{col}", "FLOAT")' in src)
truthy(
    "frozen_at is still nullable-by-default (no NOT NULL was added)",
    "NOT NULL" not in decl_expr,
)

# ---------------------------------------------------------------------------
# 1c. The already-correct profit_moved fix must be untouched by this change.
# ---------------------------------------------------------------------------
truthy(
    "profit_moved is still added as BOOLEAN DEFAULT FALSE NOT NULL",
    '_add_column("trades", "profit_moved", "BOOLEAN DEFAULT FALSE NOT NULL")' in src,
)

# ---------------------------------------------------------------------------
# 1d. Resolve the helper exactly as PostgreSQL would, and compile the fresh-DB
#     DDL through the real PostgreSQL dialect.
# ---------------------------------------------------------------------------
from sqlalchemy.dialects import postgresql  # noqa: E402
from sqlalchemy.schema import CreateTable  # noqa: E402
import app.models as models  # noqa: E402

PG_DIALECT = postgresql.dialect()
PG_NAME = PG_DIALECT.name

# Run the real helper body against the real PostgreSQL dialect rather than
# re-implementing its logic, so this test breaks if the helper ever regresses.
ts_decl = re.search(r'def _timestamp_decl\(\):(.*?)(?=\n\ndef |\n\n# |\Z)', src, re.S)
truthy("_timestamp_decl is defined in app/main.py", ts_decl is not None)
helper_ns = {"engine": type("E", (), {"dialect": PG_DIALECT})()}
exec("def _timestamp_decl():" + ts_decl.group(1), helper_ns)  # noqa: S102
pg_decl = helper_ns["_timestamp_decl"]()

print(f"\n[1d] _timestamp_decl() on dialect {PG_NAME!r} -> {pg_decl!r}")
truthy("PostgreSQL gets a TIMESTAMP", "TIMESTAMP" in pg_decl.upper())
truthy("PostgreSQL never gets DATETIME", "DATETIME" not in pg_decl.upper())
truthy(
    "PostgreSQL gets TIMESTAMP WITHOUT TIME ZONE, matching SQLAlchemy DateTime",
    pg_decl.strip().upper() == "TIMESTAMP WITHOUT TIME ZONE",
)

fresh = str(CreateTable(models.Trade.__table__).compile(dialect=PG_DIALECT))
print("\n      trades DDL as PostgreSQL receives it:")
for line in fresh.splitlines():
    if any(k in line for k in ("frozen", "profit_moved", "closes_at", "opened_at", "settled_at")):
        print(f"        {line.strip()}")

check("fresh-DB DDL contains no DATETIME", "DATETIME" in fresh.upper(), False)
truthy("fresh-DB DDL contains TIMESTAMP", "TIMESTAMP" in fresh.upper())

def _pg_type_of(ddl, column):
    """The bare TYPE of a column in compiled DDL.

    Strips the modifiers (NOT NULL) and the trailing comma, because neither is
    part of the type and the neighbours legitimately differ on both.
    """
    m = re.search(rf"^\s*{column}\s+([A-Z]+(?: [A-Z]+)*?)(?:\s+NOT NULL)?\s*,",
                  ddl, re.M | re.I)
    return " ".join(m.group(1).upper().split()) if m else ""


fresh_type = _pg_type_of(fresh, "frozen_at")
check("fresh-DB frozen_at type matches the ALTER declaration", fresh_type, pg_decl)

# The type is only right if it also agrees with its neighbours: the app stores
# naive UTC from datetime.utcnow() everywhere, so a timestamptz here would be
# the odd column out and would raise on the first naive comparison.
check("frozen_at agrees with the existing closes_at column",
      fresh_type, _pg_type_of(fresh, "closes_at"))
check("frozen_at agrees with settled_at too", fresh_type, _pg_type_of(fresh, "settled_at"))

pm_line = re.search(r"^\s*profit_moved\s+.*?,?$", fresh, re.M)
check("fresh-DB profit_moved is still BOOLEAN DEFAULT false NOT NULL",
      (pm_line.group(0).strip().rstrip(",") if pm_line else ""),
      "profit_moved BOOLEAN DEFAULT false NOT NULL")

# ---------------------------------------------------------------------------
# 1e. The helper must be portable: the other backends must still be valid.
# ---------------------------------------------------------------------------
sqlite_dialect = __import__("sqlalchemy.dialects.sqlite", fromlist=["dialect"]).dialect()
ns2 = {"engine": type("E", (), {"dialect": sqlite_dialect})()}
exec("def _timestamp_decl():" + ts_decl.group(1), ns2)  # noqa: S102
check("SQLite still gets a declaration it accepts", "TIMESTAMP" in ns2["_timestamp_decl"]().upper(), True)
truthy("SQLite does not get DATETIME either", "DATETIME" not in ns2["_timestamp_decl"]().upper())

# ===========================================================================
# PHASE 2 -- live PostgreSQL (opt-in)
# ===========================================================================
print()
print("=" * 74)
print("PHASE 2 -- live startup migration against a real PostgreSQL")
print("=" * 74)

admin_url = os.environ.get("PG_TEST_ADMIN_URL", "").strip()
if not admin_url:
    print("  skipped: set PG_TEST_ADMIN_URL to a postgres:// admin URL to run this.")
    print("           (this machine has a PostgreSQL on 127.0.0.1:5432 but no")
    print("            credentials were provided, so the live run is not possible)")
    print()
else:
    from sqlalchemy import create_engine, text, inspect  # noqa: E402

    scratch = "migchk_" + uuid.uuid4().hex[:10]
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{scratch}"'))

    target = re.sub(r"/[^/?]+(\?|$)", f"/{scratch}\\1", admin_url)
    if "://" in target and "@" in target:
        head, tail = target.split("@", 1)
        if "?" in tail:
            userinfo, q = tail.split("?", 1)
            target = f"{head}@{userinfo.rsplit('/', 1)[0]}/{scratch}?{q}"
        else:
            target = f"{head}@{tail.rsplit('/', 1)[0]}/{scratch}"

    try:
        # This is the real thing: importing app.main IS the Render startup, and
        # it calls _migrate() at module scope. The crash being fixed happened
        # exactly here.
        os.environ["DATABASE_URL"] = target
        print(f"  running the real startup migration against {scratch} ...")
        import app.main  # noqa: F401

        eng = create_engine(target)
        insp = inspect(eng)
        cols = {c["name"]: c for c in insp.get_columns("trades")}

        truthy("trades.frozen_at exists", "frozen_at" in cols)
        check("trades.frozen_at is a real PostgreSQL timestamp",
              str(cols["frozen_at"]["type"]).upper().startswith("TIMESTAMP"), True)
        check("trades.frozen_at is nullable", cols["frozen_at"]["nullable"], True)
        check("trades.frozen_profit exists", "frozen_profit" in cols, True)
        check("trades.frozen_exit_price exists", "frozen_exit_price" in cols, True)
        check("trades.profit_moved is boolean",
              str(cols["profit_moved"]["type"]).upper().split("(")[0].strip(), "BOOLEAN")
        check("trades.profit_moved is NOT NULL", cols["profit_moved"]["nullable"], False)

        with eng.begin() as c2:
            pm = c2.execute(text("SELECT column_default FROM information_schema.columns "
                                 "WHERE table_name='trades' AND column_name='profit_moved'")).scalar()
        truthy(f"trades.profit_moved default is false ({pm!r})", str(pm).strip() in ("false", "FALSE", "f", "0"))

        # Idempotence: booting twice must be a no-op, which is what actually
        # happens on every redeploy.
        print("  re-running the migration (a redeploy) ...")
        import app.main  # noqa: F811
        app.main._migrate()
        app.main._migrate()
        check("re-running the migration is a no-op", len(inspect(eng).get_columns("trades")) == len(cols), True)

        with eng.begin() as c3:
            c3.execute(text("INSERT INTO trades (id, symbol, direction, amount, entry_price, "
                            "opened_at, closes_at) VALUES "
                            "('chk1','GOLF','UP',10,0.02,now(),now())"))
            row = c3.execute(text("SELECT frozen_at, frozen_profit, profit_moved FROM trades "
                                  "WHERE id='chk1'")).one()
        check("a pre-existing row defaults frozen_at to NULL", row[0], None)
        check("...frozen_profit to NULL", row[1], None)
        check("...and profit_moved to false", bool(row[2]), False)
        with eng.begin() as c4:
            c4.execute(text("UPDATE trades SET frozen_at=now() WHERE id='chk1'"))
        with eng.begin() as c5:
            stored = c5.execute(text("SELECT frozen_at FROM trades WHERE id='chk1'")).scalar()
        truthy(f"frozen_at accepts a real timestamp ({stored!r})", stored is not None)
        with eng.begin() as c6:
            aware = c6.execute(text("SELECT frozen_at FROM trades WHERE id='chk1'")).scalar()
        truthy("frozen_at comes back NAIVE, like every other timestamp",
               getattr(aware, "tzinfo", None) is None)
        eng.dispose()
        print("  ok   live PostgreSQL migration passed")
    finally:
        try:
            import app.main  # noqa: F811
            from app.database import engine as app_engine
            app_engine.dispose()
        except Exception:
            pass
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{scratch}" WITH (FORCE)'))
        admin.dispose()
        os.environ.pop("DATABASE_URL", None)

print()
if FAILS:
    print(f"FAILED ({len(FAILS)}):")
    for f in FAILS:
        print("  - " + f)
    sys.exit(1)
print("All PostgreSQL migration-compatibility checks passed.")
