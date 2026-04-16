h"""
Flyagonal Command Center - Render Backend
"""

import os, json, time, logging, base64
from datetime import datetime, timedelta, timezone
from flask import Flask, request, jsonify
from flask_cors import CORS
import requests as http_req

SCHWAB_CLIENT_ID = os.environ.get("SCHWAB_CLIENT_ID", "")
SCHWAB_CLIENT_SECRET = os.environ.get("SCHWAB_CLIENT_SECRET", "")
SCHWAB_CALLBACK_URL = os.environ.get("SCHWAB_CALLBACK_URL", "https://127.0.0.1")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
API_SECRET = os.environ.get("API_SECRET_KEY", "flyagonal-default-key")

app = Flask(__name__)
CORS(app, origins=["https://lotustemplar.github.io", "http://localhost:3000"])
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

schwab_tokens = {"access_token": None, "refresh_token": None, "expires_at": 0}
trades = {}
CPI_2026 = ["2026-01-14","2026-02-12","2026-03-11","2026-04-10","2026-05-13","2026-06-10","2026-07-14","2026-08-12","2026-09-11","2026-10-13","2026-11-12","2026-12-10"]

def send_telegram(msg, pm="HTML"):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID: return False
    try:
        http_req.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": pm}, timeout=10)
        return True
    except: return False

def get_auth_header():
    return base64.b64encode(f"{SCHWAB_CLIENT_ID}:{SCHWAB_CLIENT_SECRET}".encode()).decode()

def refresh_schwab_token():
    if not schwab_tokens["refresh_token"]: return False
    try:
        r = http_req.post("https://api.schwabapi.com/v1/oauth/token", headers={"Authorization": f"Basic {get_auth_header()}", "Content-Type": "application/x-www-form-urlencoded"}, data={"grant_type": "refresh_token", "refresh_token": schwab_tokens["refresh_token"]}, timeout=15)
        r.raise_for_status(); d = r.json()
        schwab_tokens["access_token"] = d["access_token"]
        schwab_tokens["refresh_token"] = d.get("refresh_token", schwab_tokens["refresh_token"])
        schwab_tokens["expires_at"] = time.time() + d.get("expires_in", 1800)
        return True
    except: return False

def get_schwab_headers():
    if time.time() > schwab_tokens["expires_at"] - 60: refresh_schwab_token()
    if not schwab_tokens["access_token"]: return None
    return {"Authorization": f"Bearer {schwab_tokens['access_token']}"}

@app.route("/schwab/auth")
def schwab_auth():
    return jsonify({"auth_url": f"https://api.schwabapi.com/v1/oauth/authorize?client_id={SCHWAB_CLIENT_ID}&redirect_uri={SCHWAB_CALLBACK_URL}&response_type=code"})

@app.route("/schwab/callback", methods=["POST"])
def schwab_callback():
    import urllib.parse
    data = request.get_json(force=True)
    if data.get("secret") != API_SECRET: return jsonify({"error": "unauthorized"}), 401
    code = urllib.parse.parse_qs(urllib.parse.urlparse(data.get("redirect_url", "")).query).get("code", [None])[0]
    if not code: return jsonify({"error": "no code"}), 400
    try:
        r = http_req.post("https://api.schwabapi.com/v1/oauth/token", headers={"Authorization": f"Basic {get_auth_header()}", "Content-Type": "application/x-www-form-urlencoded"}, data={"grant_type": "authorization_code", "code": code, "redirect_uri": SCHWAB_CALLBACK_URL}, timeout=15)
        r.raise_for_status(); d = r.json()
        schwab_tokens["access_token"] = d["access_token"]
        schwab_tokens["refresh_token"] = d["refresh_token"]
        schwab_tokens["expires_at"] = time.time() + d.get("expires_in", 1800)
        return jsonify({"status": "authenticated"})
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route("/api/chain")
def get_chain():
    h = get_schwab_headers()
    if not h: return jsonify({"error": "not_authenticated"}), 503
    try:
        r = http_req.get("https://api.schwabapi.com/marketdata/v1/chains", headers=h, params={"symbol": "$SPX", "contractType": "ALL", "strikeCount": 40, "includeUnderlyingQuote": "true", "daysToExpiration": request.args.get("dte", 8)}, timeout=15)
        r.raise_for_status(); d = r.json()
        price = d.get("underlyingPrice", 0)
        best, best_d = None, 999
        for exp, strikes in d.get("callExpDateMap", {}).items():
            for strike, opts in strikes.items():
                for o in opts:
                    diff = abs(abs(o.get("delta", 0)) - 0.08)
                    if diff < best_d: best_d = diff; best = {"strike": o["strikePrice"], "delta": o.get("delta", 0)}
        if not best: return jsonify({"error": "no_8_delta"}), 404
        b = best["strike"]
        return jsonify({"underlying_price": price, "call_body": b, "delta": best["delta"], "upper_wing": b + 30, "lower_wing": b - 30, "put_strike": round(price * 0.981 / 25) * 25, "source": "schwab_live"})
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route("/api/vix")
def get_vix():
    h = get_schwab_headers()
    if not h: return jsonify({"error": "not_authenticated"}), 503
    try:
        r = http_req.get("https://api.schwabapi.com/marketdata/v1/quotes", headers=h, params={"symbols": "$VIX.X,$VIX9D.X", "fields": "quote"}, timeout=10)
        r.raise_for_status(); d = r.json()
        v = d.get("$VIX.X", {}).get("quote", {}).get("lastPrice", 0)
        v9 = d.get("$VIX9D.X", {}).get("quote", {}).get("lastPrice", 0)
        ratio = v9 / v if v > 0 else 0
        return jsonify({"vix": v, "vix9d": v9, "ratio": round(ratio, 4), "ratio_pass": 0.85 <= ratio <= 0.97, "source": "schwab_live"})
    except Exception as e: return jsonify({"error": str(e)}), 500

def calc_dte(exp):
    try: return (datetime.strptime(exp, "%Y-%m-%d").date() - datetime.now(timezone.utc).date()).days
    except: return None

def evaluate_alerts(t):
    hwm, cur, pt = t["high_water_mark"], t["current_pnl_pct"], t["profit_target_pct"] * 100
    dte = calc_dte(t["expiration_date"])
    a = t.get("alerts_sent", set())
    if dte is None: return
    tid = t["trade_id"]
    if cur >= pt and "pt_hit" not in a:
        send_telegram(f"\ud83c\udfaf <b>PT HIT â CLOSE NOW</b>\nTrade: {tid}\nCurrent: +{cur:.1f}%\nDTE: {dte}")
        a.add("pt_hit"); t["status"] = "pt_hit"
    if hwm >= 15 and "hit_15" not in a:
        send_telegram(f"\u2705 <b>Crossed 15% profit</b>\nTrade: {tid}\nHWM: {hwm:.1f}%\nDTE: {dte}")
        a.add("hit_15")
    if dte <= 4 and hwm < 15 and "checkpoint" not in a:
        send_telegram(f"\u26a0\ufe0f <b>4 DTE CHECKPOINT â SCALE OUT HALF</b>\nTrade: {tid}\nHWM: {hwm:.1f}%\nDTE: {dte}")
        a.add("checkpoint"); t["status"] = "checkpoint_warn"
    if dte <= 2 and hwm < 20 and "bail" not in a:
        send_telegram(f"\ud83d\udea8 <b>2 DTE BAIL â CLOSE EVERYTHING</b>\nTrade: {tid}\nHWM: {hwm:.1f}%\nDTE: {dte}")
        a.add("bail"); t["status"] = "bail"
    t["alerts_sent"] = a

@app.route("/api/trade/update", methods=["POST"])
def update_trade():
    d = request.get_json(force=True)
    if d.get("secret") != API_SECRET: return jsonify({"error": "unauthorized"}), 401
    tid = d.get("trade_id")
    if not tid: return jsonify({"error": "trade_id required"}), 400
    pnl = float(d.get("current_pnl_pct", 0))
    if tid in trades:
        t = trades[tid]; t["current_pnl_pct"] = pnl; t["high_water_mark"] = max(t["high_water_mark"], pnl)
        t["last_update"] = datetime.now(timezone.utc).isoformat()
        for k in ["net_debit", "profit_target_pct", "contracts", "expiration_date"]:
            if k in d: t[k] = d[k]
    else:
        t = {"trade_id": tid, "entry_date": d.get("entry_date", datetime.now(timezone.utc).date().isoformat()), "expiration_date": d.get("expiration_date", ""), "net_debit": float(d.get("net_debit", 0)), "profit_target_pct": float(d.get("profit_target_pct", 0.30)), "contracts": int(d.get("contracts", 1)), "current_pnl_pct": pnl, "high_water_mark": max(pnl, 0), "last_update": datetime.now(timezone.utc).isoformat(), "status": "active", "alerts_sent": set()}
        trades[tid] = t
        send_telegram(f"\ud83d\udcca <b>New Trade</b>\nTrade: {tid}\nExp: {t['expiration_date']}\nPT: {t['profit_target_pct']*100:.0f}%\nContracts: {t['contracts']}")
    evaluate_alerts(t)
    return jsonify({"status": "ok", "trade_id": tid, "high_water_mark": t["high_water_mark"], "trade_status": t["status"], "dte": calc_dte(t["expiration_date"])})

@app.route("/api/trade/status")
def trade_status():
    return jsonify({"trades": [{"trade_id": t["trade_id"], "status": t["status"], "current_pnl_pct": t["current_pnl_pct"], "high_water_mark": t["high_water_mark"], "dte": calc_dte(t["expiration_date"]), "alerts_sent": list(t.get("alerts_sent", []))} for t in trades.values()]})

@app.route("/health")
def health():
    return jsonify({"status": "ok", "schwab": schwab_tokens["access_token"] is not None, "trades": len(trades)})

@app.route("/telegram", methods=["POST"])
def telegram_webhook():
    d = request.get_json(force=True)
    txt = d.get("message", {}).get("text", "").strip()
    if str(d.get("message", {}).get("chat", {}).get("id", "")) != TELEGRAM_CHAT_ID: return jsonify({"ok": True})
    if txt == "/status":
        if not trades: send_telegram("No active trades.")
        for t in trades.values(): send_telegram(f"\ud83d\udcca {t['trade_id']}\nStatus: {t['status']}\nP/L: {t['current_pnl_pct']:+.1f}%\nHWM: {t['high_water_mark']:.1f}%")
    elif txt == "/help": send_telegram("<b>Commands</b>\n/status - Show trades\n/help - This")
    return jsonify({"ok": True})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
