"""Resumen del paper trading: python report.py"""
import sqlite3

db = sqlite3.connect("bot.db")
bal = db.execute("SELECT v FROM state WHERE k='balance'").fetchone()[0]
openv = db.execute("SELECT COALESCE(SUM(size_eur*remaining),0) FROM positions WHERE status='open'").fetchone()[0]
closed = db.execute("SELECT size_eur, realized_eur, exit_reason FROM positions WHERE status='closed'").fetchall()

print(f"Balance libre: {bal:.2f} EUR | En posiciones abiertas: {openv:.2f} EUR")
seen = db.execute("SELECT verdict, COUNT(*) FROM seen GROUP BY verdict").fetchall()
print("Tokens analizados:", dict(seen))

if closed:
    pnl = [r - s for s, r, _ in closed]
    wins = [p for p in pnl if p > 0]
    print(f"Operaciones cerradas: {len(closed)} | Ganadoras: {len(wins)} ({len(wins)/len(closed):.0%})")
    print(f"PnL total: {sum(pnl):+.2f} EUR | PnL medio: {sum(pnl)/len(pnl):+.2f} EUR")
    by_reason = {}
    for (s, r, reason), p in zip(closed, pnl):
        by_reason.setdefault(reason, []).append(p)
    for reason, ps in by_reason.items():
        print(f"  {reason}: {len(ps)} ops, {sum(ps):+.2f} EUR")
else:
    print("Aun no hay operaciones cerradas.")

print("\nMotivos de rechazo mas frecuentes:")
reasons = {}
for (r,) in db.execute("SELECT reasons FROM seen WHERE verdict='FAIL'"):
    for x in r.split("; "):
        key = x.split(" (")[0]
        reasons[key] = reasons.get(key, 0) + 1
for k, v in sorted(reasons.items(), key=lambda kv: -kv[1])[:8]:
    print(f"  {v:4d}  {k}")
