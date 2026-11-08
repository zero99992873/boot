"""Test de la logica con datos simulados (sin internet): python test_offline.py"""
import os, time, sqlite3
import bot

if os.path.exists("test.db"):
    os.remove("test.db")

NOW_MS = time.time() * 1000
PRICE = {"GOOD": 1.0, "RUG": 1.0}


def fake_pair(mint):
    return {
        "priceUsd": str(PRICE[mint]),
        "baseToken": {"symbol": mint, "address": mint},
        "pairCreatedAt": NOW_MS - 30 * 60000,
        "liquidity": {"usd": 50_000},
        "txns": {"h1": {"buys": 80, "sells": 60}},
    }


def fake_get_json(url, timeout=10):
    if "token-profiles" in url:
        return [{"chainId": "solana", "tokenAddress": "GOOD"},
                {"chainId": "solana", "tokenAddress": "RUG"},
                {"chainId": "ethereum", "tokenAddress": "ETHTOKEN"}]
    if "token-boosts" in url:
        return []
    if "/tokens/v1/solana/" in url:
        mint = url.rsplit("/", 1)[1]
        return [fake_pair(mint)]
    if "rugcheck" in url:
        if "/GOOD/" in url:
            return {"mintAuthority": None, "freezeAuthority": None, "rugged": False,
                    "creator": "dev1", "risks": [],
                    "topHolders": [{"pct": 3, "owner": "a"}] * 10 + [{"pct": 1, "owner": "dev1"}]}
        return {"mintAuthority": "dev", "freezeAuthority": None, "rugged": False,
                "risks": [{"name": "Mutable metadata", "level": "danger"}],
                "topHolders": [{"pct": 20, "owner": "x"}] * 10}
    return None


bot.get_json = fake_get_json
db = bot.init_db("test.db")

# Ciclo 1: debe aceptar GOOD y rechazar RUG (y filtrar ethereum)
bot.run_cycle(db)
bot.run_cycle(db)  # el 2o ciclo analiza RUG (una entrada por ciclo)
seen = dict(db.execute("SELECT mint, verdict FROM seen").fetchall())
assert seen == {"GOOD": "PASS", "RUG": "FAIL"}, seen
assert db.execute("SELECT COUNT(*) FROM positions WHERE status='open'").fetchone()[0] == 1
bal = bot.get_state(db, "balance")
assert abs(bal - 7.5) < 1e-6, bal
print("OK ciclo 1: GOOD comprado (25% = 2.50), RUG rechazado. Balance libre:", bal)

# Ciclo 2: sube x2.5 -> take profit parcial
PRICE["GOOD"] = 2.5
bot.manage_positions(db)
row = db.execute("SELECT remaining, realized_eur, status FROM positions").fetchone()
assert abs(row[0] - 0.5) < 1e-6 and row[1] > 0 and row[2] == "open", row
print("OK take profit parcial:", row)

# Ciclo 3: cae por debajo del precio de entrada -> cierra el resto
PRICE["GOOD"] = 0.9
bot.manage_positions(db)
row = db.execute("SELECT status, exit_reason FROM positions").fetchone()
assert row[0] == "closed", row
print("OK salida del resto:", row)

# Stop loss en una posicion nueva
db.execute("DELETE FROM seen WHERE mint='GOOD'"); db.commit()
PRICE["GOOD"] = 1.0
bot.run_cycle(db)
PRICE["GOOD"] = 0.5
bot.manage_positions(db)
reason = db.execute("SELECT exit_reason FROM positions ORDER BY id DESC LIMIT 1").fetchone()[0]
assert reason == "stop_loss", reason
print("OK stop loss:", reason)

final = bot.get_state(db, "balance")
print(f"\nTodos los tests pasan. Balance final simulado: {final:.2f} EUR")
os.remove("test.db")
