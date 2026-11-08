"""
Bot de PAPER TRADING para Solana (no gasta dinero real).

Flujo:
  1. Descubre pares nuevos (DexScreener).
  2. Analiza riesgo (DexScreener + RugCheck).
  3. Si pasa los filtros, "compra" el 25% de la cartera simulada.
  4. Gestiona la salida: take profit parcial, stop loss, salida por tiempo.
  5. Registra todo en SQLite para analizar despues.

Uso:
    pip install -r requirements.txt
    python bot.py
"""
import sqlite3
import time
import logging
from dataclasses import dataclass

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")


# ----------------------------- CONFIGURACION -----------------------------
@dataclass
class Config:
    start_balance_eur: float = 10.0
    position_pct: float = 0.25          # 25% de la cartera por operacion
    max_open_positions: int = 2
    max_daily_loss_pct: float = 0.30    # parar si pierdes 30% en el dia

    # Costes simulados (ajustalos con datos reales cuando los midas)
    fee_pct: float = 0.01               # comision del DEX
    slippage_pct: float = 0.03          # slippage por lado
    fixed_cost_eur: float = 0.03        # prioridad + propinas, por operacion

    # Filtros de riesgo
    min_liquidity_usd: float = 8_000
    min_age_min: float = 5              # evita el caos de los primeros minutos
    max_age_min: float = 180
    min_txns_1h: int = 50
    max_top10_pct: float = 35.0         # concentracion top 10 holders
    max_creator_pct: float = 5.0        # % en wallet del creador
    max_single_holder_pct: float = 10.0
    max_insider_holders: int = 2

    # Gestion de salida
    take_profit_mult: float = 2.0       # vende la mitad al x2
    stop_loss_pct: float = 0.30
    max_hold_min: float = 90

    poll_seconds: int = 30


CFG = Config()

# Autoridades de pools conocidas (verifica con un reporte real si cambian).
KNOWN_POOL_AUTHORITIES = {
    "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1",  # Raydium AMM authority
    "GpMZbSM2GgvTKHJirzeGfMFoaZ8UR2X7F4v8vHTvxFbL",  # Raydium CPMM authority
    "39azUYFWPz3VHgKCf3VChUwbpURdCHRxjWVowf5jUJjg",  # pump.fun migration account
}
DEXSCREENER = "https://api.dexscreener.com"
RUGCHECK = "https://api.rugcheck.xyz/v1"
HTTP = requests.Session()
HTTP.headers["User-Agent"] = "paper-bot/0.1"


def get_json(url: str, timeout: int = 10):
    try:
        r = HTTP.get(url, timeout=timeout)
        if r.status_code == 429:
            log.warning("Rate limit en %s, esperando...", url)
            time.sleep(5)
            return None
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.debug("Error HTTP %s: %s", url, e)
        return None


# ------------------------------- BASE DE DATOS ---------------------------
def init_db(path: str = "bot.db") -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS state (k TEXT PRIMARY KEY, v REAL);
        CREATE TABLE IF NOT EXISTS seen (mint TEXT PRIMARY KEY, ts REAL, verdict TEXT, reasons TEXT);
        CREATE TABLE IF NOT EXISTS positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mint TEXT, symbol TEXT,
            entry_price REAL, entry_ts REAL, size_eur REAL,
            remaining REAL DEFAULT 1.0,      -- fraccion que queda abierta
            realized_eur REAL DEFAULT 0,
            status TEXT DEFAULT 'open', exit_reason TEXT, closed_ts REAL
        );
        """
    )
    cur = db.execute("SELECT v FROM state WHERE k='balance'")
    if cur.fetchone() is None:
        db.execute("INSERT INTO state VALUES ('balance', ?)", (CFG.start_balance_eur,))
        db.execute("INSERT INTO state VALUES ('day_start_balance', ?)", (CFG.start_balance_eur,))
        db.execute("INSERT INTO state VALUES ('day_start_ts', ?)", (time.time(),))
    db.commit()
    return db


def get_state(db, k):
    return db.execute("SELECT v FROM state WHERE k=?", (k,)).fetchone()[0]


def set_state(db, k, v):
    db.execute("INSERT OR REPLACE INTO state VALUES (?, ?)", (k, v))
    db.commit()


# ------------------------------- DESCUBRIMIENTO --------------------------
def discover_candidates() -> list[str]:
    """Mints de Solana con perfil reciente en DexScreener.
    Alternativa mas rapida: websocket de Helius sobre el programa de pump.fun."""
    mints = []
    for endpoint in ("/token-profiles/latest/v1", "/token-boosts/latest/v1"):
        data = get_json(DEXSCREENER + endpoint)
        if isinstance(data, list):
            mints += [t["tokenAddress"] for t in data if t.get("chainId") == "solana"]
    return list(dict.fromkeys(mints))  # sin duplicados, mantiene orden


def get_best_pair(mint: str):
    """Devuelve el par con mas liquidez para un mint."""
    pairs = get_json(f"{DEXSCREENER}/tokens/v1/solana/{mint}")
    if not isinstance(pairs, list) or not pairs:
        return None
    return max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd", 0) or 0)


# ------------------------------- FILTROS DE RIESGO -----------------------
def analyze(mint: str, pair: dict) -> tuple[bool, list[str]]:
    """Devuelve (pasa, motivos_de_rechazo)."""
    fails = []

    # --- Filtros de mercado (DexScreener) ---
    liq = (pair.get("liquidity") or {}).get("usd", 0) or 0
    if liq < CFG.min_liquidity_usd:
        fails.append(f"liquidez baja (${liq:,.0f})")

    created = pair.get("pairCreatedAt")
    if created:
        age_min = (time.time() * 1000 - created) / 60000
        if not CFG.min_age_min <= age_min <= CFG.max_age_min:
            fails.append(f"edad fuera de rango ({age_min:.0f} min)")
    else:
        fails.append("sin fecha de creacion")

    h1 = (pair.get("txns") or {}).get("h1", {})
    txns = (h1.get("buys", 0) or 0) + (h1.get("sells", 0) or 0)
    if txns < CFG.min_txns_1h:
        fails.append(f"poca actividad ({txns} txns/h)")
    if (h1.get("sells", 0) or 0) == 0 and txns > 10:
        fails.append("cero ventas: posible honeypot")

    # --- Filtros on-chain (RugCheck) ---
    rc = get_json(f"{RUGCHECK}/tokens/{mint}/report")
    if not rc:
        fails.append("RugCheck no disponible")
        return False, fails

    if rc.get("mintAuthority"):
        fails.append("mint authority NO revocada")
    if rc.get("freezeAuthority"):
        fails.append("freeze authority NO revocada")
    if rc.get("rugged"):
        fails.append("marcado como rugged")

    holders = rc.get("topHolders") or []

    # Excluir cuentas del propio pool / bonding curve: acumulan gran parte del
    # supply y NO son holders reales. Si RugCheck las incluye, el filtro
    # rechazaria casi todo.
    excluded = set(KNOWN_POOL_AUTHORITIES)
    for m in rc.get("markets") or []:
        for k in ("pubkey", "liquidityA", "liquidityB"):
            if m.get(k):
                excluded.add(m[k])
    holders = [h for h in holders
               if h.get("address") not in excluded and h.get("owner") not in excluded]

    def _top(n=3):
        return ", ".join(f"{h.get('pct', 0):.0f}% {str(h.get('owner') or h.get('address'))[:4]}"
                         for h in holders[:n])

    top10 = sum(h.get("pct", 0) for h in holders[:10])
    if top10 > CFG.max_top10_pct:
        fails.append(f"top10 concentrado ({top10:.1f}%; top: {_top()})")
    if holders and max(h.get("pct", 0) for h in holders) > CFG.max_single_holder_pct:
        fails.append(f"un holder supera el limite individual (top: {_top(2)})")
    if sum(1 for h in holders if h.get("insider")) > CFG.max_insider_holders:
        fails.append("demasiados holders 'insider' (cluster)")

    creator = rc.get("creator")
    if creator:
        creator_pct = sum(h.get("pct", 0) for h in holders if h.get("owner") == creator
                          or h.get("address") == creator)
        if creator_pct > CFG.max_creator_pct:
            fails.append(f"creador tiene {creator_pct:.1f}%")

    dangers = [r.get("name") for r in (rc.get("risks") or []) if r.get("level") == "danger"]
    if dangers:
        fails.append(f"riesgos graves RugCheck: {', '.join(dangers)}")

    return (len(fails) == 0), fails


# ------------------------------- PAPER TRADING ---------------------------
def cost_factor() -> float:
    return (1 - CFG.fee_pct) * (1 - CFG.slippage_pct)


def open_position(db, mint, symbol, price):
    balance = get_state(db, "balance")
    size = balance * CFG.position_pct
    if size <= CFG.fixed_cost_eur * 2:
        log.warning("Posicion demasiado pequena frente a los costes fijos")
        return
    set_state(db, "balance", balance - size)
    entry = price / cost_factor()  # precio efectivo peor por slippage+comision
    db.execute(
        "INSERT INTO positions (mint, symbol, entry_price, entry_ts, size_eur) VALUES (?,?,?,?,?)",
        (mint, symbol, entry, time.time(), size - CFG.fixed_cost_eur),
    )
    db.commit()
    log.info("COMPRA simulada %s (%s) %.2f EUR @ %.8f", symbol, mint[:6], size, entry)


def sell_fraction(db, pos, fraction, price, reason):
    """Vende 'fraction' de lo que queda de la posicion."""
    pid, mint, symbol, entry, _, size, remaining, realized = (
        pos["id"], pos["mint"], pos["symbol"], pos["entry_price"], pos["entry_ts"],
        pos["size_eur"], pos["remaining"], pos["realized_eur"])
    sold_part = remaining * fraction
    proceeds = size * sold_part * (price * cost_factor() / entry) - CFG.fixed_cost_eur
    set_state(db, "balance", get_state(db, "balance") + proceeds)
    new_remaining = remaining - sold_part
    closed = new_remaining < 0.01
    db.execute(
        "UPDATE positions SET remaining=?, realized_eur=?, status=?, exit_reason=?, closed_ts=? WHERE id=?",
        (new_remaining, realized + proceeds, "closed" if closed else "open",
         reason if closed else pos["exit_reason"], time.time() if closed else None, pid),
    )
    db.commit()
    log.info("VENTA simulada %s %.0f%% por %s -> +%.2f EUR", symbol, fraction * 100, reason, proceeds)


def manage_positions(db):
    db.row_factory = sqlite3.Row
    rows = db.execute("SELECT * FROM positions WHERE status='open'").fetchall()
    for pos in rows:
        pair = get_best_pair(pos["mint"])
        price = float(pair["priceUsd"]) if pair and pair.get("priceUsd") else None
        age_min = (time.time() - pos["entry_ts"]) / 60

        if price is None:
            # Sin precio: tratamos como rug si lleva tiempo sin datos
            if age_min > 10:
                sell_fraction(db, pos, 1.0, 1e-12, "sin_precio_posible_rug")
            continue

        mult = price / pos["entry_price"]
        took_profit = pos["realized_eur"] > 0  # ya vendimos la mitad

        if mult <= 1 - CFG.stop_loss_pct:
            sell_fraction(db, pos, 1.0, price, "stop_loss")
        elif mult >= CFG.take_profit_mult and not took_profit:
            sell_fraction(db, pos, 0.5, price, "take_profit_parcial")
        elif took_profit and mult <= 1.0:
            sell_fraction(db, pos, 1.0, price, "proteger_resto_en_breakeven")
        elif age_min > CFG.max_hold_min:
            sell_fraction(db, pos, 1.0, price, "tiempo_maximo")
    db.row_factory = None


def daily_loss_exceeded(db) -> bool:
    if time.time() - get_state(db, "day_start_ts") > 86400:
        set_state(db, "day_start_ts", time.time())
        set_state(db, "day_start_balance", get_state(db, "balance"))
    start = get_state(db, "day_start_balance")
    open_value = db.execute("SELECT COALESCE(SUM(size_eur*remaining),0) FROM positions WHERE status='open'").fetchone()[0]
    equity = get_state(db, "balance") + open_value
    return equity < start * (1 - CFG.max_daily_loss_pct)


# ------------------------------- BUCLE PRINCIPAL -------------------------
def run_cycle(db):
    """Un ciclo completo: gestionar salidas y buscar una entrada nueva."""
    manage_positions(db)

    n_open = db.execute("SELECT COUNT(*) FROM positions WHERE status='open'").fetchone()[0]
    if daily_loss_exceeded(db):
        log.warning("Limite de perdida diaria alcanzado: sin nuevas entradas")
        return
    if n_open >= CFG.max_open_positions:
        return

    for mint in discover_candidates():
        if db.execute("SELECT 1 FROM seen WHERE mint=?", (mint,)).fetchone():
            continue
        pair = get_best_pair(mint)
        if not pair:
            continue
        ok, fails = analyze(mint, pair)
        db.execute("INSERT INTO seen VALUES (?,?,?,?)",
                   (mint, time.time(), "PASS" if ok else "FAIL", "; ".join(fails)))
        db.commit()
        symbol = (pair.get("baseToken") or {}).get("symbol", "?")
        if ok:
            log.info("PASA filtros: %s (%s)", symbol, mint)
            open_position(db, mint, symbol, float(pair["priceUsd"]))
            return  # una entrada por ciclo
        log.info("rechazado %s: %s", symbol, "; ".join(fails[:3]))
        time.sleep(0.5)  # respeta rate limits


def main():
    import sys
    db = init_db()
    log.info("Bot en modo SIMULACION. Balance: %.2f EUR", get_state(db, "balance"))
    # Latido: deja constancia de cada ejecucion (para saber que esta vivo)
    row = db.execute("SELECT v FROM state WHERE k='runs'").fetchone()
    set_state(db, "runs", (row[0] if row else 0) + 1)
    set_state(db, "last_run", time.time())
    if "--once" in sys.argv:      # modo para GitHub Actions: un ciclo y salir
        run_cycle(db)
        return
    while True:
        try:
            run_cycle(db)
        except KeyboardInterrupt:
            log.info("Parando...")
            break
        except Exception:
            log.exception("Error en el ciclo")
        time.sleep(CFG.poll_seconds)


if __name__ == "__main__":
    main()
