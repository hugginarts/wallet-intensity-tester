#!/usr/bin/env python3
"""Wallet Intensity - Analizador histórico de wallets públicas de Solana.

Solo librería estándar de Python (funciona en Termux). Solo lectura: nunca pide
claves privadas ni firma nada.

Uso:
    python wallet_intensity_v2.py                      # panel en http://127.0.0.1:8765
    python wallet_intensity_v2.py WALLET --limit 500   # resultado en texto
    SOLANA_RPC_URL="https://mainnet.helius-rpc.com/?api-key=..." python wallet_intensity_v2.py

Cómo funciona: descarga las transacciones de la wallet por RPC estándar, detecta
swaps SOL/WSOL o USDC/USDT contra un token, agrupa compras y ventas por moneda de
cotización (FIFO) y estima una copia hipotética. El slippage es manual; no mide
el retraso ni la liquidez real al copiar.
"""
import argparse
import json
import math
import os
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import Counter, defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

DEFAULT_RPC = "https://api.mainnet-beta.solana.com"
WSOL = "So11111111111111111111111111111111111111112"
STABLES = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
}
LAMPORTS = 1_000_000_000
TOKEN_RENT = 0.00203928          # renta de una cuenta de token (se devuelve al cerrarla)
ABANDON_HOURS = 24               # posición abierta sin movimiento > 24 h = abandonada
MAX_SUPPORTED_TRANSACTION_VERSION = 1
B58 = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")


# --------------------------------------------------------------------------- RPC
class RpcError(Exception):
    pass


class Limiter:
    """Espacia las consultas para no chocar con el límite del RPC."""

    def __init__(self, interval):
        self.interval, self.lock, self.next = interval, threading.Lock(), 0.0

    def wait(self):
        with self.lock:
            now = time.monotonic()
            slot = max(now, self.next)
            self.next = slot + self.interval
        if slot > now:
            time.sleep(slot - now)


def rpc(url, method, params, limiter=None, retries=6, timeout=30):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    delay, last = 1.0, "sin respuesta"
    for _ in range(retries):
        if limiter:
            limiter.wait()
        try:
            req = urllib.request.Request(url, data=body, headers={
                "Content-Type": "application/json", "User-Agent": "WalletIntensity/2.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 500, 502, 503, 504):
                last = f"HTTP {exc.code} (límite del RPC)" if exc.code == 429 else f"HTTP {exc.code}"
                time.sleep(delay)
                delay = min(delay * 2, 12)
                continue
            raise RpcError(f"HTTP {exc.code}: el RPC rechazó la consulta (¿clave inválida o RPC público bloqueado?)")
        except (urllib.error.URLError, OSError, ValueError) as exc:
            last = f"conexión: {type(exc).__name__}"
            time.sleep(delay)
            delay = min(delay * 2, 12)
            continue
        err = data.get("error") if isinstance(data, dict) else None
        if err:
            code = err.get("code") if isinstance(err, dict) else None
            msg = str(err.get("message", err) if isinstance(err, dict) else err)
            if code == -32015 or ("version" in msg.lower() and "not supported" in msg.lower()):
                raise RpcError("El RPC rechazó esta versión de transacción. Wallet Intensity solicita soporte hasta v1; verifica que el proveedor admita maxSupportedTransactionVersion=1.")
            if code in (429, -32005) or "rate" in msg.lower() or "too many" in msg.lower():
                last = "límite de consultas del RPC"
                time.sleep(delay)
                delay = min(delay * 2, 12)
                continue
            raise RpcError(f"RPC {code}: {msg[:160]}")
        return data.get("result")
    raise RpcError(last)


# ------------------------------------------------------------------ Parseo de txs
def _raw(row):
    amount = row.get("uiTokenAmount") or {}
    try:
        raw = int(amount.get("amount") or 0)
    except (TypeError, ValueError):
        raw = 0
    return raw, int(amount.get("decimals") or 0)


def parse_tx(tx, wallet):
    """Devuelve (tipo, datos). tipo == 'swap' si es un swap SOL<->token de la wallet."""
    if not tx:
        return "missing", None
    meta = tx.get("meta") or {}
    if meta.get("err"):
        return "failed", None
    msg = (tx.get("transaction") or {}).get("message") or {}
    keys = [k.get("pubkey") if isinstance(k, dict) else k for k in msg.get("accountKeys") or []]
    try:
        idx = keys.index(wallet)
    except ValueError:
        return "absent", None
    pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
    if idx >= len(pre) or idx >= len(post):
        return "absent", None
    # SOL nativo (incluye comisión y propinas pagadas: son un coste real de operar).
    native = (post[idx] - pre[idx]) / LAMPORTS

    pre_acc, post_acc = {}, {}
    for row in meta.get("preTokenBalances") or []:
        if row.get("owner") == wallet:
            raw, dec = _raw(row)
            pre_acc[row.get("accountIndex")] = (row.get("mint"), raw, dec)
    for row in meta.get("postTokenBalances") or []:
        if row.get("owner") == wallet:
            raw, dec = _raw(row)
            post_acc[row.get("accountIndex")] = (row.get("mint"), raw, dec)
    delta, decimals = defaultdict(int), {}
    for mint, raw, dec in pre_acc.values():
        delta[mint] -= raw
        decimals[mint] = dec
    for mint, raw, dec in post_acc.values():
        delta[mint] += raw
        decimals[mint] = dec
    wsol = delta.pop(WSOL, 0) / LAMPORTS
    # Renta de cuentas de token creadas/cerradas: no es parte del precio del swap.
    created = sum(1 for i, (m, _, _) in post_acc.items() if i not in pre_acc and m != WSOL)
    closed = sum(1 for i, (m, _, _) in pre_acc.items() if i not in post_acc and m != WSOL)
    sol = native + wsol + (created - closed) * TOKEN_RENT

    changes = {m: d / (10 ** decimals[m]) for m, d in delta.items() if d != 0}
    stable_changes = {m: d for m, d in changes.items() if m in STABLES}
    stable_moved = bool(stable_changes)
    tokens = {m: d for m, d in changes.items() if m not in STABLES}
    if not tokens:
        return ("stable" if stable_moved else "no_token"), None
    if len(tokens) > 1:
        return "multi_token", None
    mint, qty = next(iter(tokens.items()))
    if stable_moved:
        # USDC/USDT are treated as approximately USD 1. This lets the tool
        # evaluate stable-quoted swaps without mixing USD PnL with SOL PnL.
        cash, quote = sum(stable_changes.values()), "USD"
    else:
        cash, quote = sol, "SOL"
    if abs(cash) < (1e-4 if quote == "SOL" else 1e-6) or qty * cash >= 0:
        return "transfer", None
    return "swap", {"mint": mint, "q": qty, "sol": cash, "quote": quote,
                    "t": tx.get("blockTime") or 0, "slot": tx.get("slot") or 0}


def fetch_signatures(url, wallet, want, limiter, progress):
    """Firmas exitosas más recientes (las fallidas se cuentan pero no se descargan)."""
    ok, failed, seen, before = [], 0, 0, None
    max_seen = min(want * 8, 12000)
    while len(ok) < want and seen < max_seen:
        opts = {"limit": 1000}
        if before:
            opts["before"] = before
        page = rpc(url, "getSignaturesForAddress", [wallet, opts], limiter) or []
        if not page:
            break
        for row in page:
            seen += 1
            if row.get("err"):
                failed += 1
            else:
                ok.append(row)
                if len(ok) >= want:
                    break
        before = page[-1]["signature"]
        progress(f"Buscando firmas… {len(ok)} válidas, {failed} fallidas", 3)
        if len(page) < 1000:
            break
    return ok, failed


def fetch_transaction(url, signature, limiter):
    """Lee transacciones Solana legacy, v0 y v1."""
    return rpc(url, "getTransaction", [signature, {
        "encoding": "jsonParsed",
        "maxSupportedTransactionVersion": MAX_SUPPORTED_TRANSACTION_VERSION,
        "commitment": "confirmed",
    }], limiter)


def collect(wallet, rpc_url, limit, progress):
    host = (urlparse(rpc_url).hostname or "").lower()
    public = host.endswith("solana.com")
    if "helius" in host and "api-key" not in rpc_url and "api_key" not in rpc_url:
        raise RuntimeError("La URL de Helius está incompleta: pega la URL completa con ?api-key=…")
    try:
        rps = float(os.getenv("WI_RPS", "0")) or (3.0 if public else 8.0)
    except ValueError:
        rps = 3.0 if public else 8.0
    limiter = Limiter(1.0 / rps)
    workers = 2 if public else 8

    sigs, failed = fetch_signatures(rpc_url, wallet, limit, limiter, progress)
    if not sigs:
        raise RuntimeError("El RPC no devolvió transacciones exitosas para esa dirección. Revisa que sea una wallet (no un token).")

    diag = defaultdict(int)
    trades, errors, error_sample = [], 0, ""
    stop = threading.Event()
    total, done = len(sigs), 0

    def work(row):
        if stop.is_set():
            return "skipped", None, None
        try:
            tx = fetch_transaction(rpc_url, row["signature"], limiter)
            kind, data = parse_tx(tx, wallet)
            return kind, data, None
        except RpcError as exc:
            return "error", None, str(exc)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for kind, data, err in pool.map(work, sigs):
            done += 1
            if kind == "skipped":
                continue
            if kind == "error":
                errors += 1
                error_sample = error_sample or err
                if errors >= 25 and errors > 0.5 * done:
                    stop.set()
            else:
                diag[kind] += 1
                if data:
                    trades.append(data)
            if done % 10 == 0 or done == total:
                progress(f"Analizando {done}/{total} · swaps detectados: {len(trades)}",
                         5 + 95 * done / total)
    if stop.is_set():
        raise RuntimeError(f"El RPC falla demasiado ({error_sample}). Revisa la URL, los límites del proveedor y su soporte de historial Solana.")

    times = [r.get("blockTime") for r in sigs if r.get("blockTime")]
    meta = {"wallet": wallet, "requested": limit, "reviewed": total, "failed_sigs": failed,
            "t_start": min(times) if times else None, "t_end": max(times) if times else None,
            "diag": dict(diag), "errors": errors, "error_sample": error_sample,
            "source": "RPC público" if public else "RPC propio"}
    return meta, trades


# ------------------------------------------------------------------- Análisis
def analyze(trades, t_end):
    """Agrupa swaps en posiciones (FIFO). Una posición se cierra cuando se vende todo."""
    state, closed, orphans, last_price = {}, [], 0, {}
    for tr in sorted(trades, key=lambda x: (x["slot"], x["t"])):
        mint, qty, cash, t = tr["mint"], tr["q"], tr.get("sol", tr.get("cash", 0)), tr["t"]
        quote = tr.get("quote", "SOL")
        key = (mint, quote)
        st = state.get(key)
        if qty > 0:                                   # compra
            cost = -cash
            if st is None:
                st = state[key] = {"mint": mint, "quote": quote, "lots": deque(),
                                    "proceeds": 0.0, "matched_cost": 0.0,
                                    "buys": 0, "sells": 0, "qty_in": 0.0, "open_t": t,
                                    "hold_w": 0.0, "hold_q": 0.0, "last_t": t}
            st["lots"].append([qty, cost, t])
            st["buys"] += 1
            st["qty_in"] += qty
            st["last_t"] = t
            last_price[key] = cost / qty
            continue
        sell_qty = -qty                               # venta
        last_price[key] = cash / sell_qty
        if st is None or not st["lots"]:
            orphans += 1                              # se compró antes de la ventana
            continue
        remaining, matched, matched_cost, hold_w = sell_qty, 0.0, 0.0, 0.0
        while remaining > 1e-12 and st["lots"]:
            lot = st["lots"][0]
            take = min(remaining, lot[0])
            part_cost = lot[1] * take / lot[0]
            matched += take
            matched_cost += part_cost
            hold_w += max(0, t - lot[2]) * take
            lot[0] -= take
            lot[1] -= part_cost
            remaining -= take
            if lot[0] <= 1e-12:
                st["lots"].popleft()
        st["proceeds"] += cash * (matched / sell_qty)
        st["matched_cost"] += matched_cost
        st["sells"] += 1
        st["hold_w"] += hold_w
        st["hold_q"] += matched
        st["last_t"] = t
        left = sum(l[0] for l in st["lots"])
        if left <= max(1e-12, 0.005 * st["qty_in"]):  # vendida (el polvo restante cuenta como pérdida)
            dropped = sum(l[1] for l in st["lots"])
            cost_total = st["matched_cost"] + dropped
            closed.append({"mint": mint, "quote": quote, "cost": cost_total, "proceeds": st["proceeds"],
                           "pnl": st["proceeds"] - cost_total, "open_t": st["open_t"],
                           "close_t": t, "legs": st["buys"] + st["sells"],
                           "hold": st["hold_w"] / st["hold_q"] if st["hold_q"] else 0.0})
            del state[key]
    opens = []
    for (_, quote), st in state.items():
        mint = st["mint"]
        cost = sum(l[1] for l in st["lots"])
        age_h = (t_end - st["last_t"]) / 3600 if t_end else 0
        opens.append({"mint": mint, "quote": quote, "cost": cost, "age_h": age_h,
                      "abandoned": age_h > ABANDON_HOURS})
    return closed, opens, orphans


def wilson_lower(wins, n, z=1.96):
    if not n:
        return 0.0
    p = wins / n
    den = 1 + z * z / n
    center = p + z * z / (2 * n)
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)
    return max(0.0, (center - margin) / den)


def copy_pnls(closed, size, s_in, s_out, fee):
    """PnL que habrías tenido copiando: entras s_in más caro, sales s_out más barato, pagas fee por swap."""
    out = []
    for c in closed:
        r = c["proceeds"] / c["cost"] if c["cost"] > 1e-12 else 0.0
        out.append(size * (r * (1 - s_out) / (1 + s_in) - 1) - fee * c["legs"])
    return out


def breakeven_slippage(closed, size, fee):
    """Mayor slippage por lado (entrada y salida) con el que la copia aún no pierde."""
    def total(s):
        return sum(copy_pnls(closed, size, s, s, fee))
    if total(0.0) <= 0:
        return 0.0
    if total(0.6) > 0:
        return 0.6
    lo, hi = 0.0, 0.6
    for _ in range(40):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if total(mid) > 0 else (lo, mid)
    return lo


def build_report(meta, trades, size=0.1, slip=0.05, fee=0.003):
    quote_counts = Counter(tr.get("quote", "SOL") for tr in trades)
    quote = quote_counts.most_common(1)[0][0] if quote_counts else "SOL"
    unit = "SOL" if quote == "SOL" else "USD"
    all_swaps = len(trades)
    ignored_quotes = sum(n for q, n in quote_counts.items() if q != quote)
    # Never add dollar-denominated PnL to SOL-denominated PnL. If both occur,
    # the report uses the most common quote and discloses the excluded swaps.
    trades = [tr for tr in trades if tr.get("quote", "SOL") == quote]
    closed, opens, orphans = analyze(trades, meta.get("t_end"))
    closed.sort(key=lambda c: c["close_t"])
    n = len(closed)
    pnls = [c["pnl"] for c in closed]
    copy = copy_pnls(closed, size, slip, slip, fee)
    wins = sum(p > 0 for p in pnls)
    gross_win = sum(p for p in pnls if p > 0)
    gross_loss = -sum(p for p in pnls if p < 0)
    pf = 99.0 if (gross_loss <= 1e-12 and gross_win > 0) else (gross_win / gross_loss if gross_loss else 0.0)
    pf = min(pf, 99.0)
    net = sum(pnls)
    copy_total = sum(copy)
    t0, t1 = meta.get("t_start"), meta.get("t_end")
    window_days = (t1 - t0) / 86400 if t0 and t1 else 0.0
    per_day_div = max(window_days, 1 / 24)
    cushion = breakeven_slippage(closed, size, fee) if n else 0.0
    wilson = wilson_lower(wins, n)
    holds = [c["hold"] for c in closed]
    median_hold = statistics.median(holds) if holds else 0.0
    best = max(pnls) if pnls else 0.0
    net_wo_best = net - best if n else 0.0

    cum, peak, max_dd = 0.0, 0.0, 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
    recovery = 99.0 if (max_dd <= 1e-12 and net > 0) else (max(0.0, net / max_dd) if max_dd else 0.0)
    recovery = min(recovery, 99.0)

    recent_n = max(1, n // 2) if n else 0
    recent_copy = sum(copy[-recent_n:]) if n else 0.0
    abandoned = [o for o in opens if o["abandoned"]]
    abandoned_cost = sum(o["cost"] for o in abandoned)
    invested = sum(c["cost"] for c in closed) + sum(o["cost"] for o in opens)
    abandoned_ratio = abandoned_cost / invested if invested > 0 else 0.0
    copy_conservative = copy_total - len(abandoned) * (size + fee)
    swaps = len(trades)
    reviewed = meta.get("reviewed", 0)
    fail_total = meta.get("failed_sigs", 0) + reviewed
    fail_rate = meta.get("failed_sigs", 0) / fail_total if fail_total else 0.0

    checks = []

    def chk(label, ok, value, hard):
        checks.append({"label": label, "ok": bool(ok), "value": value, "hard": hard})

    chk("Gana dinero (PnL realizado de la wallet)", net > 0, f"{net:+.3f} {unit}", True)
    chk("Ganarías TÚ copiándola (con tu slippage y comisiones)", copy_total > 0,
        f"{copy_total:+.3f} {unit} con {size:g} {unit}/operación", True)
    chk("Profit factor ≥ 1.3", pf >= 1.3, f"{pf:.2f}" if pf < 99 else "sin pérdidas", True)
    chk("Aguanta al menos 5% de slippage por lado", cushion >= 0.05, f"aguanta {cushion*100:.1f}%", True)
    chk("Tenencia mediana ≥ 20 s (se puede copiar a tiempo)", median_hold >= 20, _fmt_time(median_hold), True)
    chk("No depende de una sola operación afortunada", net_wo_best > 0, f"sin la mejor: {net_wo_best:+.3f} {unit}", True)
    chk("Muestra suficiente: ≥ 30 operaciones cerradas", n >= 30, f"{n} cerradas", False)
    chk("El tramo reciente también da PnL simulado positivo", recent_copy > 0, f"{recent_copy:+.3f} {unit}", False)
    chk("Win rate fiable (límite inferior Wilson 95% ≥ 30%)", wilson >= 0.30, f"{wilson*100:.0f}%", False)
    chk("Se recupera de las caídas (PnL / drawdown ≥ 1)", recovery >= 1, f"{recovery:.1f}x" if recovery < 99 else "sin caídas", False)
    chk("Pocas posiciones abandonadas (≤ 20% del capital)", abandoned_ratio <= 0.20,
        f"{abandoned_ratio*100:.0f}% · {len(abandoned)} abiertas >24 h", False)
    chk("Sigue ganando si lo abandonado se pierde del todo", copy_conservative > 0, f"{copy_conservative:+.3f} {unit}", False)
    chk("Ventana analizada ≥ 12 h", window_days >= 0.5, f"{window_days:.1f} días", False)
    chk("Pocas transacciones fallidas (≤ 60%)", fail_rate <= 0.60, f"{fail_rate*100:.0f}% fallidas", False)

    hard_fail = [c for c in checks if c["hard"] and not c["ok"]]
    soft_fail = [c for c in checks if not c["hard"] and not c["ok"]]
    weight = sum(2 if c["hard"] else 1 for c in checks)
    score = round(100 * sum((2 if c["hard"] else 1) for c in checks if c["ok"]) / weight)

    if n == 0 and swaps == 0:
        verdict, title = "insufficient", "SIN OPERACIONES COMPATIBLES"
        sub = ("No se encontraron swaps SOL↔token ni USDC/USDT↔token reconocibles. "
               "Los ceros no indican pérdidas; abre Cobertura del análisis para ver qué movimientos se descartaron.")
    elif n == 0:
        verdict, title = "insufficient", "SIN POSICIONES CERRADAS"
        sub = (f"Se detectaron {swaps} swaps cotizados en {unit}, pero no se pudo emparejar una compra y una venta "
               "dentro de la ventana analizada. Los indicadores de rendimiento no están disponibles todavía.")
    elif n < 20:
        verdict, title = "insufficient", "DATOS INSUFICIENTES"
        sub = (f"Solo {n} operaciones cerradas en la ventana. Sube las transacciones a revisar "
               "(500–2000) para poder decidir.")
    elif hard_fail:
        verdict, title = "no", "NO CONVIENE COPIARLA"
        sub = "Falla en: " + "; ".join(c["label"].split(" (")[0] for c in hard_fail[:3]) + "."
    elif n >= 30 and len(soft_fail) <= 1:
        verdict, title = "yes", "CANDIDATA PARA VALIDAR EN PAPER"
        sub = (f"El historial supera los filtros configurados: {copy_total:+.2f} {unit} simulados en "
               f"{n} cierres. Es una lectura retrospectiva; no confirma resultados futuros.")
    else:
        verdict, title = "caution", "RESULTADO INCONCLUSO"
        reasons = "; ".join(c["label"].split(" (")[0] for c in soft_fail[:3])
        sub = ("La evidencia histórica todavía no pasa suficientes filtros" +
               (": " + reasons if reasons else ". Revisa los parámetros y la cobertura del historial."))

    # Curva de capital (se reduce a ~200 puntos).
    cr, cc, curve = 0.0, 0.0, []
    for p, q in zip(pnls, copy):
        cr += p
        cc += q
        curve.append([round(cr, 4), round(cc, 4)])
    if len(curve) > 200:
        step = len(curve) / 200
        curve = [curve[min(len(curve) - 1, int(i * step))] for i in range(200)] + [curve[-1]]

    by_token = defaultdict(lambda: {"n": 0, "pnl": 0.0})
    for c in closed:
        by_token[c["mint"]]["n"] += 1
        by_token[c["mint"]]["pnl"] += c["pnl"]
    tokens = [{"mint": m, "n": v["n"], "pnl": round(v["pnl"], 4)}
              for m, v in sorted(by_token.items(), key=lambda kv: -abs(kv[1]["pnl"]))[:8]]

    diag = meta.get("diag", {})
    avg_cost = statistics.mean(c["cost"] for c in closed) if closed else 0.0
    return {
        "wallet": meta["wallet"], "verdict": verdict, "title": title, "sub": sub, "score": score,
        "quote": quote, "unit": unit, "ignored_quote_swaps": ignored_quotes,
        "checks": checks,
        "params": {"size": size, "slip": slip, "fee": fee},
        "n_closed": n, "swaps": swaps, "winrate": round(100 * wins / n, 1) if n else 0.0,
        "wilson": round(wilson * 100, 1), "profit_factor": round(pf, 2), "pf_inf": pf >= 99,
        "net": round(net, 4), "copy_total": round(copy_total, 4),
        "copy_per_day": round(copy_total / per_day_div, 3), "net_per_day": round(net / per_day_div, 3),
        "swaps_per_day": round(swaps / per_day_div, 1), "closed_per_day": round(n / per_day_div, 1),
        "window_days": round(window_days, 2), "median_hold": _fmt_time(median_hold),
        "cushion": round(cushion * 100, 1), "max_dd": round(max_dd, 3), "avg_size": round(avg_cost, 3),
        "best": round(best, 3), "open_positions": len(opens), "abandoned": len(abandoned),
        "curve": curve, "tokens": tokens, "short_window": window_days < 0.5,
        "coverage": {
            "reviewed": reviewed, "failed_sigs": meta.get("failed_sigs", 0), "swaps": all_swaps,
            "sol_swaps": quote_counts.get("SOL", 0), "usd_swaps": quote_counts.get("USD", 0),
            "ignored_quote_swaps": ignored_quotes,
            "multi_token": diag.get("multi_token", 0), "no_token": diag.get("no_token", 0),
            "stable": diag.get("stable", 0), "transfer": diag.get("transfer", 0),
            "absent": diag.get("absent", 0) + diag.get("missing", 0), "errors": meta.get("errors", 0),
            "error_sample": meta.get("error_sample", ""), "orphan_sells": orphans,
            "source": meta.get("source", ""),
        },
    }


def _fmt_time(seconds):
    s = int(max(0, seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60}s"
    if s < 86400:
        return f"{s // 3600}h {(s % 3600) // 60}m"
    return f"{s // 86400}d {(s % 86400) // 3600}h"


# ------------------------------------------------------------------ Panel web
PAGE = r'''<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#0b0711">
<meta name="description" content="Analiza el historial de una wallet pública de Solana y simula los costos de copiar sus operaciones."><title>Wallet Intensity · Solana wallet research</title>
<style>
:root{--ink:#0b0711;--panel:#15101d;--line:#352743;--text:#f4effa;--mute:#a99db5;--go:#c084fc;--warn:#f5bd57;--stop:#fb7185;--info:#9b7bff}
*{box-sizing:border-box}body{margin:0;background:var(--ink);color:var(--text);font:16px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:760px;margin:auto;padding:22px 16px 40px}h1{font-size:28px;margin:6px 0 2px;letter-spacing:-.4px}
.eyebrow{display:inline-block;color:#d8b4fe;background:#26143b;border:1px solid #4b2c67;border-radius:99px;padding:5px 10px;font-size:11px;letter-spacing:1.3px;font-weight:750}
.sub{color:var(--mute);margin:0 0 16px;font-size:14px}
.box{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:16px;margin:12px 0}
label{display:block;color:var(--mute);font-size:13px;margin:10px 0 5px}
input,select{width:100%;background:var(--ink);color:var(--text);border:1px solid var(--line);border-radius:10px;padding:12px;font-size:15px}
input:focus,select:focus,button:focus-visible,summary:focus-visible{outline:2px solid var(--info);outline-offset:1px}
.row{display:grid;grid-template-columns:1fr 1fr;gap:10px}.row3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px}
button{width:100%;margin-top:14px;border:0;border-radius:10px;background:linear-gradient(110deg,#a855f7,#c084fc);color:#170b24;font-weight:800;font-size:16px;padding:14px;box-shadow:0 8px 24px #8b5cf633}
button:disabled{opacity:.55}
details{margin-top:10px}summary{color:var(--mute);font-size:14px;cursor:pointer;padding:6px 0}
.hint{color:var(--mute);font-size:12px;margin:6px 0 0}
.prog{height:6px;background:var(--line);border-radius:6px;margin-top:12px;overflow:hidden}.prog i{display:block;height:100%;width:0;background:var(--info);transition:width .4s}
#status{color:var(--mute);font-size:14px;margin-top:8px}#status.err{color:var(--stop)}
.hero{border-radius:16px;padding:20px 18px;margin:12px 0;border:2px solid var(--c);background:color-mix(in srgb,var(--c) 10%,var(--panel))}
.hero.yes{--c:var(--go)}.hero.caution{--c:var(--warn)}.hero.no{--c:var(--stop)}.hero.insufficient{--c:var(--info)}
.vt{font-size:27px;font-weight:800;color:var(--c);line-height:1.15}.vs{margin-top:8px;font-size:15px}
.bar{height:8px;border-radius:8px;background:var(--line);margin-top:14px;overflow:hidden}.bar i{display:block;height:100%;background:var(--c)}
.sc{font-size:13px;color:var(--mute);margin-top:6px}
h2{font-size:17px;margin:0 0 10px}ul.ck{list-style:none;margin:0;padding:0}
.ck li{display:flex;gap:10px;padding:9px 0;border-bottom:1px solid var(--line)}.ck li:last-child{border:0}
.ck b{width:22px;flex:none;text-align:center}.ck .ok b{color:var(--go)}.ck .bad b{color:var(--stop)}
.ck small{display:block;color:var(--mute);font-size:13px}.ck em{font-style:normal;font-size:11px;color:var(--warn);border:1px solid var(--warn);border-radius:6px;padding:0 5px;margin-left:6px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}.m{background:var(--ink);border:1px solid var(--line);border-radius:12px;padding:12px}
.m .n{font-size:21px;font-weight:700;font-variant-numeric:tabular-nums}.m .l{font-size:12.5px;color:var(--mute)}
.pos{color:#d8b4fe}.neg{color:var(--stop)}
table{width:100%;border-collapse:collapse;font-size:14px;font-variant-numeric:tabular-nums}td,th{text-align:left;padding:8px 4px;border-bottom:1px solid var(--line)}th{color:var(--mute);font-weight:500}
.leg{font-size:12.5px;color:var(--mute);margin-top:6px}.leg span{margin-right:12px}
.small{color:var(--mute);font-size:12.5px;line-height:1.5}
@media(prefers-reduced-motion:reduce){*{transition:none!important}}
</style></head><body><main class="wrap">
<div class="eyebrow">SOLANA · ON-CHAIN RESEARCH · SOLO LECTURA</div>
<h1>Wallet Intensity</h1>
<p class="sub">Analiza swaps históricos cotizados en SOL o stablecoins y simula resultados con tus supuestos de slippage y costos.</p>
<form id="form" class="box">
<label for="wallet">Wallet pública</label><input id="wallet" placeholder="Dirección de la wallet" required autocomplete="off" autocapitalize="off" spellcheck="false">
<div class="row"><div><label for="limit">Transacciones a revisar</label><select id="limit"><option>250</option><option selected>500</option><option>1000</option><option>2000</option></select></div>
<div><label for="slip">Slippage supuesto</label><select id="slip"><option value="2">Optimista · 2% por lado</option><option value="5" selected>Moderado · 5% por lado</option><option value="10">Alto · 10% por lado</option><option value="15">Extremo · 15% por lado</option></select></div></div>
<div class="row"><div><label id="sizeLabel" for="size">Tamaño por operación</label><input id="size" type="number" step="any" min="0.000001" value="0.1" inputmode="decimal"></div>
<div><label id="feeLabel" for="fee">Costo estimado por swap</label><input id="fee" type="number" step="any" min="0" value="0.003" inputmode="decimal"></div></div>
<label for="rpc">RPC propio (opcional)</label><input id="rpc" type="password" placeholder="URL HTTPS completa del proveedor RPC" autocomplete="off">
<p class="hint">Si el RPC público limita el historial, pega aquí tu URL HTTPS completa. El panel no la conserva en el navegador; no la incluyas en capturas ni la compartas.</p>
<button id="go">Analizar wallet</button><div class="prog" id="prog" hidden><i></i></div><div id="status"></div></form>
<section id="out" hidden></section>
<p class="small">Solo lectura: no pide claves privadas, no conecta wallets y no envía órdenes. El índice resume filtros retrospectivos; no es una probabilidad de ganancias futuras.</p>
</main><script>
const $=s=>document.querySelector(s);
const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt=(n,d=2)=>Number(n).toLocaleString('en-US',{minimumFractionDigits:d,maximumFractionDigits:d});
const sgn=(n,d=3)=>(n>0?'+':'')+fmt(n,d);
const cls=n=>n>0?'pos':(n<0?'neg':'');
let jobId=null;
let adaptedUsdDefaults=false;
const qs=()=>'size='+encodeURIComponent($('#size').value||0.1)+'&slip='+encodeURIComponent($('#slip').value)+'&fee='+encodeURIComponent($('#fee').value||0);
function setStatus(t,err){const s=$('#status');s.textContent=t;s.className=err?'err':''}
async function localFetch(path,options){let last;const tries=(!options||!options.method||options.method.toUpperCase()==='GET')?4:1;for(let attempt=0;attempt<tries;attempt++){try{return await fetch(path,options)}catch(e){last=e;if(attempt+1<tries)await new Promise(r=>setTimeout(r,600*(attempt+1)))}}throw new Error('Falló la petición al panel local'+(tries>1?' tras '+tries+' intentos':'')+'. Termux puede seguir respondiendo a otras consultas; espera unos segundos y vuelve a probar. ('+(last?.message||'error de conexión')+')')}
$('#form').onsubmit=async e=>{e.preventDefault();const btn=$('#go');btn.disabled=true;btn.textContent='Analizando…';$('#out').hidden=true;
const prog=$('#prog');prog.hidden=false;prog.firstChild.style.width='2%';setStatus('Conectando…');
try{const rpc=$('#rpc').value.trim();
const r=await localFetch('/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({wallet:$('#wallet').value.trim(),rpc:rpc,limit:Number($('#limit').value),size:Number($('#size').value)||0.1,slip:Number($('#slip').value),fee:Number($('#fee').value)||0})});
const j=await r.json();if(!r.ok)throw Error(j.error||'No se pudo iniciar');jobId=j.id;
for(;;){await new Promise(r=>setTimeout(r,1200));const s=await (await localFetch('/status?id='+jobId)).json();
if(s.state==='error')throw Error(s.error||'Falló el análisis');
prog.firstChild.style.width=(s.pct||5)+'%';setStatus(s.message||'Analizando…');
if(s.state==='done'){render(s.result);setStatus('Listo');break}}}
catch(err){setStatus(err.message,true)}finally{btn.disabled=false;btn.textContent='Analizar wallet';$('#prog').hidden=true}};
async function recalc(){if(!jobId||$('#out').hidden)return;try{const r=await localFetch('/recalc?id='+jobId+'&'+qs());const j=await r.json();if(r.ok)render(j)}catch(e){}}
['#slip','#size','#fee'].forEach(s=>$(s).addEventListener('change',recalc));
function chart(curve){if(curve.length<2)return'';const W=320,H=130,P=6;const all=curve.flat().concat([0]);const lo=Math.min(...all),hi=Math.max(...all),rg=(hi-lo)||1;
const X=i=>P+(W-2*P)*i/(curve.length-1),Y=v=>H-P-(H-2*P)*(v-lo)/rg;
const line=(k,c)=>'<polyline fill="none" stroke="'+c+'" stroke-width="2" stroke-linejoin="round" points="'+curve.map((p,i)=>X(i).toFixed(1)+','+Y(p[k]).toFixed(1)).join(' ')+'"/>';
return '<svg viewBox="0 0 '+W+' '+H+'" width="100%" role="img" aria-label="Curva de ganancias acumuladas"><line x1="0" x2="'+W+'" y1="'+Y(0)+'" y2="'+Y(0)+'" stroke="#273241" stroke-dasharray="4 4"/>'+line(0,'#8d9bad')+line(1,'#3ddc97')+'</svg><div class="leg"><span style="color:#8d9bad">━ La wallet</span><span style="color:#3ddc97">━ Tú copiando</span></div>'}
function render(d){const o=$('#out');o.hidden=false;const p=d.params,unit=d.unit||'SOL',hasClosed=d.n_closed>0;
if(d.quote==='USD'&&!adaptedUsdDefaults&&Number($('#size').value)===0.1&&Number($('#fee').value)===0.003){adaptedUsdDefaults=true;$('#size').value='10';$('#fee').value='0.01';setTimeout(recalc,0)}
$('#sizeLabel').textContent='Tamaño por operación ('+unit+')';$('#feeLabel').textContent='Costo estimado por swap ('+unit+')';
const m=(l,v,c)=>'<div class="m"><div class="n '+(c||'')+'">'+v+'</div><div class="l">'+l+'</div></div>';
const cv=d.coverage;
o.innerHTML='<div class="hero '+d.verdict+'"><div class="vt">'+esc(d.title)+'</div><div class="vs">'+esc(d.sub)+'</div><div class="bar"><i style="width:'+d.score+'%"></i></div><div class="sc">Filtros históricos superados: '+d.score+'/100 · no es una probabilidad</div></div>'
+(d.n_closed===0?'<div class="box small">No hay operaciones cerradas para calcular rentabilidad. Los guiones indican datos no disponibles, no un resultado de cero. Revisa la cobertura del análisis.</div>':'')
+(d.ignored_quote_swaps?'<div class="box small">La wallet usa varias monedas de cotización. Para no mezclar USD con SOL, este informe usa '+d.quote+' e ignora '+d.ignored_quote_swaps+' swaps cotizados en otra moneda.</div>':'')
+(d.short_window?'<div class="box small">La ventana analizada es muy corta ('+d.window_days+' días). Esta wallet opera tanto que 500 transacciones cubren pocas horas: sube a 1000–2000 para más fiabilidad.</div>':'')
+'<div class="box"><h2>Resultados clave</h2><div class="grid">'
+m('Win rate',hasClosed?fmt(d.winrate,1)+'%':'—')
+m('Profit factor',hasClosed?(d.pf_inf?'∞':fmt(d.profit_factor)):'—')
+m('PnL de la wallet',hasClosed?sgn(d.net)+' '+unit:'—',cls(d.net))
+m('Tú copiando',hasClosed?sgn(d.copy_total)+' '+unit:'—',cls(d.copy_total))
+m('Swaps por día',fmt(d.swaps_per_day,1))
+m('Operaciones cerradas/día',fmt(d.closed_per_day,1))
+m('Ganarías por día (copiando)',hasClosed?sgn(d.copy_per_day,2)+' '+unit:'—',cls(d.copy_per_day))
+m('Tenencia mediana',hasClosed?esc(d.median_hold):'—')
+m('Slippage que aguanta',hasClosed?fmt(d.cushion,1)+'%':'—',d.cushion>=5?'pos':'neg')
+m('Caída máxima',hasClosed?fmt(d.max_dd,2)+' '+unit:'—')
+m('Tamaño medio de posición',hasClosed?fmt(d.avg_size,2)+' '+unit:'—')
+m('Operaciones cerradas',d.n_closed)
+'</div><p class="hint">Simulado con '+p.size+' '+unit+' por operación, '+(p.slip*100)+'% de slippage por lado y '+p.fee+' '+unit+' por swap.</p>'+(d.quote==='USD'?'<p class="hint">USDC/USDT se aproximan a USD 1. El PnL en USD no convierte la comisión de red pagada en SOL; inclúyela en el costo supuesto por swap.</p>':'')+'</div>'
+'<div class="box"><h2>Por qué este veredicto</h2><ul class="ck">'+d.checks.map(c=>'<li class="'+(c.ok?'ok':'bad')+'"><b>'+(c.ok?'✓':'✕')+'</b><span>'+esc(c.label)+(c.hard?'<em>clave</em>':'')+'<small>'+esc(c.value)+'</small></span></li>').join('')+'</ul></div>'
+'<div class="box"><h2>Ganancia acumulada ('+unit+')</h2>'+chart(d.curve)+'</div>'
+'<div class="box"><h2>Tokens con más impacto</h2>'+(d.tokens.length?'<table><tr><th>Token</th><th>Posiciones</th><th>PnL '+unit+'</th></tr>'+d.tokens.map(t=>'<tr><td>'+esc(t.mint.slice(0,5)+'…'+t.mint.slice(-4))+'</td><td>'+t.n+'</td><td class="'+cls(t.pnl)+'">'+sgn(t.pnl)+'</td></tr>').join('')+'</table>':'<p class="small">No hay posiciones cerradas todavía.</p>')+'</div>'
+'<details class="box" open><summary>Cobertura del análisis ('+esc(cv.source)+')</summary><p class="small">Se revisaron '+cv.reviewed+' transacciones exitosas ('+cv.failed_sigs+' fallidas aparte). Swaps detectados: <b>'+cv.swaps+'</b> ('+cv.sol_swaps+' SOL, '+cv.usd_swaps+' USDC/USDT). Descartadas: '+cv.multi_token+' con varios tokens a la vez, '+cv.stable+' con movimientos de stablecoin sin token asociado, '+cv.transfer+' transferencias, '+cv.no_token+' sin tokens, '+cv.absent+' sin datos de la wallet. Ventas sin compra dentro de la ventana: '+cv.orphan_sells+'. Errores de RPC: '+cv.errors+(cv.error_sample?' ('+esc(cv.error_sample)+')':'')+'. Posiciones aún abiertas: '+d.open_positions+' ('+d.abandoned+' abandonadas).</p></details>';
o.scrollIntoView({behavior:'smooth',block:'start'})}
</script></body></html>'''


JOBS, CACHE, LOCK = {}, {}, threading.Lock()


def _validate(data):
    wallet = str(data.get("wallet", "")).strip()
    if not 32 <= len(wallet) <= 44 or any(ch not in B58 for ch in wallet):
        raise ValueError("La dirección no es válida. Debe tener 32–44 caracteres (letras y números, sin espacios).")
    try:
        size = float(data.get("size", 0.1))
        slip = float(data.get("slip", 5)) / 100
        fee = float(data.get("fee", 0.003))
    except (TypeError, ValueError):
        raise ValueError("Tamaño, slippage o comisión no son números válidos.")
    if not (0.0001 <= size <= 100000 and 0 <= slip <= 0.6 and 0 <= fee <= 1):
        raise ValueError("Tamaño, slippage o comisión fuera de rango.")
    return wallet, size, slip, fee


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[panel] {self.address_string()} {fmt % args}", flush=True)

    def send_json(self, data, status=200):
        raw = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        url = urlparse(self.path)
        query = parse_qs(url.query)
        if url.path == "/":
            raw = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        elif url.path == "/status":
            with LOCK:
                job = JOBS.get(query.get("id", [""])[0])
            self.send_json(job or {"state": "error", "error": "Consulta no encontrada."}, 200 if job else 404)
        elif url.path == "/health":
            self.send_json({"ok": True, "service": "Wallet Intensity"})
        elif url.path == "/recalc":
            job_id = query.get("id", [""])[0]
            with LOCK:
                cached = CACHE.get(job_id)
            if not cached:
                return self.send_json({"error": "Análisis no encontrado. Vuelve a analizar la wallet."}, 404)
            try:
                _, size, slip, fee = _validate({"wallet": cached[0]["wallet"], "size": query.get("size", ["0.1"])[0],
                                                "slip": query.get("slip", ["5"])[0], "fee": query.get("fee", ["0.003"])[0]})
                self.send_json(build_report(cached[0], cached[1], size, slip, fee))
            except ValueError as exc:
                self.send_json({"error": str(exc)}, 400)
        else:
            self.send_json({"error": "No encontrado."}, 404)

    def do_POST(self):
        if self.path != "/start":
            return self.send_json({"error": "No encontrado."}, 404)
        try:
            size_b = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(min(size_b, 10000)))
            wallet, size, slip, fee = _validate(data)
            rpc_url = str(data.get("rpc", "")).strip() or os.getenv("SOLANA_RPC_URL", DEFAULT_RPC)
            if not rpc_url.startswith(("https://", "http://")):
                raise ValueError("La URL RPC debe empezar con https://")
            limit = max(50, min(int(data.get("limit", 500)), 2000))
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            return self.send_json({"error": str(exc) if isinstance(exc, ValueError) and not isinstance(exc, json.JSONDecodeError) else "Solicitud inválida."}, 400)
        job_id = uuid.uuid4().hex
        with LOCK:
            JOBS[job_id] = {"state": "running", "message": "Iniciando…", "pct": 2}

        def worker():
            def update(msg, pct):
                with LOCK:
                    JOBS[job_id] = {"state": "running", "message": msg, "pct": round(pct)}
            try:
                meta, trades = collect(wallet, rpc_url, limit, update)
                report = build_report(meta, trades, size, slip, fee)
                with LOCK:
                    CACHE[job_id] = (meta, trades)
                    while len(CACHE) > 6:
                        CACHE.pop(next(iter(CACHE)))
                    JOBS[job_id] = {"state": "done", "message": "Listo", "pct": 100, "result": report}
            except Exception as exc:  # el panel muestra el motivo en vez de quedarse colgado
                with LOCK:
                    JOBS[job_id] = {"state": "error", "error": str(exc)[:300]}
        threading.Thread(target=worker, daemon=True).start()
        self.send_json({"id": job_id})


def run_dashboard(port, host="127.0.0.1"):
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Panel listo: http://127.0.0.1:{port}  (ábrelo en Chrome; Ctrl+C para cerrar)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nCerrando…")
    finally:
        server.server_close()


def main():
    ap = argparse.ArgumentParser(description="Analiza el historial de una wallet pública de Solana (solo lectura)")
    ap.add_argument("wallet", nargs="?", help="Dirección pública (si la omites, abre el panel)")
    ap.add_argument("--limit", type=int, default=500, help="Transacciones a revisar (50–2000)")
    ap.add_argument("--rpc", default=os.getenv("SOLANA_RPC_URL", DEFAULT_RPC))
    ap.add_argument("--size", type=float, default=0.1, help="Tamaño por operación en la moneda de cotización")
    ap.add_argument("--slip", type=float, default=5.0, help="Slippage supuesto por lado en %%")
    ap.add_argument("--fee", type=float, default=0.003, help="Costo estimado por swap en la moneda de cotización")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--dashboard", action="store_true")
    args = ap.parse_args()
    if args.dashboard or not args.wallet:
        return run_dashboard(args.port)
    try:
        wallet, size, slip, fee = _validate({"wallet": args.wallet, "size": args.size, "slip": args.slip, "fee": args.fee})
        meta, trades = collect(wallet, args.rpc, max(50, min(args.limit, 2000)),
                               lambda msg, pct: print(f"\r{msg[:70]:<70}", end="", flush=True))
    except (ValueError, RuntimeError, RpcError) as exc:
        sys.exit(f"Error: {exc}")
    r = build_report(meta, trades, size, slip, fee)
    print(f"\n\n=== {r['title']} ({r['score']}/100) ===\n{r['sub']}\n")
    print(f"Operaciones cerradas: {r['n_closed']} | Win rate: {r['winrate']}% | Profit factor: {r['profit_factor']}")
    print(f"PnL wallet: {r['net']:+.3f} SOL | Copiando: {r['copy_total']:+.3f} SOL | Swaps/día: {r['swaps_per_day']}")
    for c in r["checks"]:
        print(f"  [{'OK' if c['ok'] else '--'}] {c['label']}: {c['value']}")


if __name__ == "__main__":
    main()
