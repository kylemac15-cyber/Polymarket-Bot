#!/usr/bin/env python3
"""
Polymarket Spike Scanner  v1.1
================================
Monitors Polymarket for sudden price/volume spikes on economic/political
contracts that are closing within 12–24 hours.

Usage:
    pip install requests colorama
    python polymarket_scanner.py
"""

import requests
import time
import json
import sys
from datetime import datetime, timezone
from collections import defaultdict, deque
from colorama import Fore, Back, Style, init

init(autoreset=True)

# ─────────────────────── DISCORD CONFIG ───────────────────────────────────────

DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/1480935317535195187/WQFY0OMge_aKBel2glIVeSVU1xfqeeMYe37cYrNkVAkZUkxYa5DnFY7tzZGIB40F5R_M"

# ─────────────────────────── CONFIG ───────────────────────────────────────────

SPIKE_THRESHOLD_PCT    = 10.0   # % price change to trigger alert
VOLUME_SPIKE_MULT      = 3.0    # volume must be X times the rolling avg
ROLLING_WINDOW         = 10     # data points for rolling average
SCAN_INTERVAL_SEC      = 15     # seconds between full market scans
MAX_MARKETS_PER_SCAN   = 100    # cap to avoid hammering the API

# ── Closing window filter ──────────────────────────────────────────────────────
CLOSING_WINDOW_MIN_HRS = 0      # only alert if contract closes within...
CLOSING_WINDOW_MAX_HRS = 20     # ...this many hours from now

# ── High-bids filter ───────────────────────────────────────────────────────────
HIGH_BID_MIN_COUNT     = 3      # min number of bids to qualify as "multiple"
HIGH_BID_MIN_SIZE      = 500    # minimum $ size per bid to be considered "high"

# Keywords that classify a market as economic or political
ECON_POLITICAL_KEYWORDS = [
    # Geopolitical / conflict
    "war", "invasion", "attack", "missile", "strike", "military",
    "conflict", "troops", "nato", "nuclear", "sanctions",
    # Political / elections
    "election", "president", "senate", "congress", "vote", "poll",
    "impeach", "resign", "cabinet", "parliament", "prime minister",
    "referendum", "ballot", "candidate",
    # Economic / markets
    "fed", "interest rate", "inflation", "gdp", "recession", "tariff",
    "trade", "deficit", "unemployment", "debt ceiling", "treasury",
    "oil", "gas", "energy", "crypto", "bitcoin", "stock", "market crash",
    # Country / region triggers
    "iran", "russia", "china", "ukraine", "israel", "taiwan",
    "north korea", "middle east", "opec", "g7", "g20",
]

POLYMARKET_GAMMA_API = "https://gamma-api.polymarket.com"
POLYMARKET_CLOB_API  = "https://clob.polymarket.com"
CLOB_BASE            = POLYMARKET_CLOB_API

# ──────────────────────────────────────────────────────────────────────────────


def banner():
    print(f"""
{Fore.CYAN}{Style.BRIGHT}╔══════════════════════════════════════════════════════╗
║        POLYMARKET SPIKE SCANNER  v1.2                ║
║   Economic & Political  |  Closing-Window Filter     ║
╚══════════════════════════════════════════════════════╝{Style.RESET_ALL}
{Fore.YELLOW}  Spike threshold   : {SPIKE_THRESHOLD_PCT}%
  Volume multiplier : {VOLUME_SPIKE_MULT}x rolling avg
  Closing window    : 0h - {CLOSING_WINDOW_MAX_HRS}h from now
  Urgency flags     : 🔴 <6h  🟡 6-12h  🟢 12-20h
  High bids flag    : {HIGH_BID_MIN_COUNT}+ bids >= ${HIGH_BID_MIN_SIZE} each
  Scan interval     : {SCAN_INTERVAL_SEC}s
  Max markets/scan  : {MAX_MARKETS_PER_SCAN}
{Style.RESET_ALL}""")


# ─────────────────────── CLOSING-WINDOW FILTER ────────────────────────────────

def parse_end_date(market: dict):
    """
    Try every field Polymarket uses to store the resolution/end date.
    Returns a timezone-aware datetime or None.
    """
    for field in ("endDate", "end_date", "endDateIso", "resolutionDate",
                  "resolution_date", "closeTime", "close_time"):
        raw = market.get(field)
        if not raw:
            continue
        try:
            if isinstance(raw, (int, float)):
                return datetime.fromtimestamp(raw, tz=timezone.utc)
            raw = str(raw).rstrip("Z")
            for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
                try:
                    return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
                except ValueError:
                    continue
        except Exception:
            continue
    return None


def hours_until_close(market: dict):
    """Return float hours until contract closes, or None if unknown."""
    end_dt = parse_end_date(market)
    if end_dt is None:
        return None
    now = datetime.now(tz=timezone.utc)
    return (end_dt - now).total_seconds() / 3600


def is_in_closing_window(market: dict):
    """
    Returns (passes_filter: bool, hours_remaining: float|None).
    passes_filter is True only when contract closes within configured window.
    """
    hrs = hours_until_close(market)
    if hrs is None:
        return False, None
    return CLOSING_WINDOW_MIN_HRS <= hrs <= CLOSING_WINDOW_MAX_HRS, hrs


# ─────────────────────── KEYWORD FILTER ───────────────────────────────────────

def is_econ_political(question: str, tags: list = None) -> bool:
    text = question.lower()
    if tags:
        text += " " + " ".join(t.lower() for t in tags)
    return any(kw in text for kw in ECON_POLITICAL_KEYWORDS)


# ─────────────────────── API HELPERS ──────────────────────────────────────────

def fetch_markets(limit: int = 100) -> list:
    """Fetch active markets from Gamma API, sorted by 24h volume."""
    try:
        resp = requests.get(
            f"{POLYMARKET_GAMMA_API}/markets",
            params={
                "active": "true",
                "closed": "false",
                "limit": limit,
                "order": "volume24hr",
                "ascending": "false",
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else data.get("markets", [])
    except Exception as e:
        print(f"{Fore.RED}[ERROR] fetch_markets: {e}{Style.RESET_ALL}")
        return []


def extract_best_price(market: dict):
    """Pull the YES-outcome price (0-1) from market data."""
    try:
        prices = market.get("outcomePrices")
        if isinstance(prices, str):
            prices = json.loads(prices)
        if isinstance(prices, list) and len(prices) > 0:
            return float(prices[0])
    except Exception:
        pass
    tokens = market.get("tokens", [])
    for t in tokens:
        if str(t.get("outcome", "")).upper() == "YES":
            return float(t.get("price", 0))
    return None


def extract_volume(market: dict) -> float:
    try:
        return float(market.get("volume24hr") or market.get("volumeClob") or 0)
    except Exception:
        return 0.0


# ─────────────────────── HIGH-BIDS DETECTOR ───────────────────────────────────

def fetch_order_book(token_id: str) -> dict:
    """Fetch the order book for a token from the CLOB API."""
    try:
        resp = requests.get(
            f"{CLOB_BASE}/book",
            params={"token_id": token_id},
            timeout=8,
        )
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return {}


def check_high_bids(market: dict) -> dict | None:
    """
    Returns a HIGH_BIDS alert dict if the market has multiple large bids,
    otherwise None.
    Checks both YES and NO token order books.
    """
    tokens = market.get("tokens", [])
    if not tokens:
        # Try conditionId as fallback token id
        cid = market.get("conditionId") or market.get("id")
        if cid:
            tokens = [{"token_id": cid, "outcome": "YES"}]

    all_high_bids = []

    for token in tokens:
        token_id = token.get("token_id") or token.get("tokenId")
        if not token_id:
            continue

        book = fetch_order_book(token_id)
        bids = book.get("bids", [])

        for bid in bids:
            try:
                size  = float(bid.get("size", 0))
                price = float(bid.get("price", 0))
                notional = size * price  # approximate $ value
                if notional >= HIGH_BID_MIN_SIZE:
                    all_high_bids.append({
                        "outcome":  token.get("outcome", "?"),
                        "price":    price,
                        "size":     size,
                        "notional": notional,
                    })
            except Exception:
                continue

    if len(all_high_bids) >= HIGH_BID_MIN_COUNT:
        # Sort by notional descending
        all_high_bids.sort(key=lambda b: b["notional"], reverse=True)
        return {
            "type":      "HIGH_BIDS",
            "market":    market,
            "bids":      all_high_bids,
            "bid_count": len(all_high_bids),
            "top_bid":   all_high_bids[0],
        }
    return None


# ─────────────────────── SPIKE TRACKER ────────────────────────────────────────

class SpikeTracker:
    def __init__(self):
        self.price_history:  dict = defaultdict(lambda: deque(maxlen=ROLLING_WINDOW))
        self.volume_history: dict = defaultdict(lambda: deque(maxlen=ROLLING_WINDOW))
        self.alerts_fired:   dict = {}
        self.high_bids_fired: dict = {}   # market_id -> last high-bids alert time

    def update(self, market_id: str, price: float, volume: float, market: dict) -> list:
        now    = time.time()
        alerts = []
        ph     = self.price_history[market_id]
        vh     = self.volume_history[market_id]

        # ── Price spike ────────────────────────────────────────────────────────
        if ph:
            prev_price = ph[-1][1]
            if prev_price > 0:
                pct_change = ((price - prev_price) / prev_price) * 100
                if abs(pct_change) >= SPIKE_THRESHOLD_PCT:
                    last = self.alerts_fired.get(market_id + "_price", 0)
                    if now - last > SCAN_INTERVAL_SEC * 2:
                        alerts.append({
                            "type":       "PRICE_SPIKE",
                            "market":     market,
                            "pct_change": pct_change,
                            "old_price":  prev_price,
                            "new_price":  price,
                            "volume":     volume,
                        })
                        self.alerts_fired[market_id + "_price"] = now

        # ── Volume spike ───────────────────────────────────────────────────────
        if len(vh) >= 3:
            avg_vol = sum(vh) / len(vh)
            if avg_vol > 0 and volume >= avg_vol * VOLUME_SPIKE_MULT:
                last = self.alerts_fired.get(market_id + "_vol", 0)
                if now - last > SCAN_INTERVAL_SEC * 2:
                    alerts.append({
                        "type":       "VOLUME_SPIKE",
                        "market":     market,
                        "pct_change": None,
                        "old_price":  ph[-1][1] if ph else price,
                        "new_price":  price,
                        "volume":     volume,
                        "avg_volume": avg_vol,
                    })
                    self.alerts_fired[market_id + "_vol"] = now

        ph.append((now, price))
        vh.append(volume)
        return alerts


# ─────────────────────── DISCORD ALERTS ──────────────────────────────────────

def send_discord_alert(alert: dict, hours_left: float):
    m        = alert["market"]
    question = m.get("question", "Unknown")
    url      = f"https://polymarket.com/event/{m.get('slug', m.get('id', ''))}"
    time_str = format_hours(hours_left)

    if hours_left < 6:
        urgency = "🔴 URGENT"
        color   = 0xFF0000
    elif hours_left < 12:
        urgency = "🟡 WATCH"
        color   = 0xFFAA00
    else:
        urgency = "🟢 MONITOR"
        color   = 0x00CC44

    if alert["type"] == "PRICE_SPIKE":
        pct   = alert["pct_change"]
        arrow = "📈" if pct > 0 else "📉"
        title = f"{arrow} PRICE SPIKE — {'+' if pct > 0 else ''}{pct:.1f}%"
        desc  = f"YES price moved **{alert['old_price']:.3f} → {alert['new_price']:.3f}**"
    elif alert["type"] == "VOLUME_SPIKE":
        avg  = alert.get("avg_volume", 0)
        mult = alert["volume"] / avg if avg else 0
        title = f"📊 VOLUME SPIKE — {mult:.1f}x normal"
        desc  = (f"Volume: **{alert['volume']:,.0f}** vs avg **{avg:,.0f}**\n"
                 f"YES price: **{alert['new_price']:.3f}**")
    else:  # HIGH_BIDS
        top   = alert["top_bid"]
        title = f"💰 HIGH BIDS — {alert['bid_count']} large orders on the book"
        lines = [f"`#{i+1}` **{b['outcome']}**  ${b['notional']:,.0f}  @  {b['price']:.3f}"
                 for i, b in enumerate(alert["bids"][:5])]
        desc  = f"Top bid: **${top['notional']:,.0f}** @ {top['price']:.3f} ({top['outcome']})\n" + "\n".join(lines)

    embed = {
        "title":       title,
        "description": f"{desc}\n\n{urgency} — closes in **{time_str}**",
        "url":         url,
        "color":       color,
        "fields": [
            {"name": "Market",  "value": question,                              "inline": False},
            {"name": "24h Vol", "value": f"${alert.get('volume', 0):,.0f}",     "inline": True},
            {"name": "Link",    "value": f"[Open on Polymarket]({url})",        "inline": True},
        ],
        "footer":    {"text": "Polymarket Spike Scanner v1.2"},
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
    }

    try:
        resp = requests.post(
            DISCORD_WEBHOOK_URL,
            json={"embeds": [embed]},
            timeout=5,
        )
        if resp.status_code not in (200, 204):
            print(f"{Fore.RED}[Discord] HTTP {resp.status_code}: {resp.text}{Style.RESET_ALL}")
    except Exception as e:
        print(f"{Fore.RED}[Discord error] {e}{Style.RESET_ALL}")


# ─────────────────────── DISPLAY ──────────────────────────────────────────────

def format_hours(hrs: float) -> str:
    h = int(hrs)
    m = int((hrs - h) * 60)
    return f"{h}h {m:02d}m"


def print_high_bids_alert(alert: dict, hours_left: float):
    m        = alert["market"]
    question = m.get("question", "Unknown market")
    url      = f"https://polymarket.com/event/{m.get('slug', m.get('id', ''))}"
    ts       = datetime.now().strftime("%H:%M:%S")

    if hours_left < 6:
        urgency_color = Fore.RED;    urgency_icon = "🔴"
    elif hours_left < 12:
        urgency_color = Fore.YELLOW; urgency_icon = "🟡"
    else:
        urgency_color = Fore.GREEN;  urgency_icon = "🟢"

    time_str = format_hours(hours_left)

    print(f"\n{Back.MAGENTA}{Fore.WHITE} 💰 HIGH BIDS DETECTED  [{ts}] {Style.RESET_ALL}")
    print(f"{Fore.MAGENTA}{Style.BRIGHT}  {alert['bid_count']} large bids on the book  |  "
          f"Top bid: ${alert['top_bid']['notional']:,.0f} @ {alert['top_bid']['price']:.3f} "
          f"({alert['top_bid']['outcome']}){Style.RESET_ALL}")

    # Show top 5 bids
    for i, bid in enumerate(alert["bids"][:5], 1):
        print(f"  {Fore.MAGENTA}  #{i}  {bid['outcome']:<4}  "
              f"${bid['notional']:>10,.0f}  @  {bid['price']:.3f}  "
              f"(size: {bid['size']:,.0f}){Style.RESET_ALL}")

    print(f"  {urgency_color}{urgency_icon} Closes in {time_str}{Style.RESET_ALL}")
    print(f"  {Fore.CYAN}{question}{Style.RESET_ALL}")
    print(f"  {Fore.WHITE}{url}{Style.RESET_ALL}")
    print(f"  {'-'*60}")


def print_alert(alert: dict, hours_left: float):
    m        = alert["market"]
    question = m.get("question", "Unknown market")
    url      = f"https://polymarket.com/event/{m.get('slug', m.get('id', ''))}"
    ts       = datetime.now().strftime("%H:%M:%S")

    # Urgency colour based on time remaining
    if hours_left < 6:
        urgency_color = Fore.RED
        urgency_icon  = "🔴"
    elif hours_left < 12:
        urgency_color = Fore.YELLOW
        urgency_icon  = "🟡"
    else:
        urgency_color = Fore.GREEN
        urgency_icon  = "🟢"

    time_str = format_hours(hours_left)

    if alert["type"] == "PRICE_SPIKE":
        pct       = alert["pct_change"]
        direction = "UP" if pct > 0 else "DOWN"
        arrow     = "▲" if pct > 0 else "▼"
        color     = Fore.GREEN if pct > 0 else Fore.RED
        print(f"\n{Back.WHITE}{Fore.BLACK} PRICE SPIKE [{ts}] {Style.RESET_ALL}")
        print(f"{color}{Style.BRIGHT}  {arrow} {direction}  {abs(pct):.1f}%  |  YES: {alert['old_price']:.3f} -> {alert['new_price']:.3f}{Style.RESET_ALL}")
    else:
        avg  = alert.get("avg_volume", 0)
        mult = alert["volume"] / avg if avg else 0
        print(f"\n{Back.YELLOW}{Fore.BLACK} VOLUME SPIKE [{ts}] {Style.RESET_ALL}")
        print(f"{Fore.YELLOW}{Style.BRIGHT}  Volume: {alert['volume']:,.0f}  |  Avg: {avg:,.0f}  |  {mult:.1f}x normal{Style.RESET_ALL}")
        print(f"  Current YES price: {alert['new_price']:.3f}")

    print(f"  {urgency_color}{urgency_icon} Closes in {time_str}{Style.RESET_ALL}")
    print(f"  {Fore.CYAN}{question}{Style.RESET_ALL}")
    print(f"  {Fore.WHITE}{url}{Style.RESET_ALL}")
    vol_str = f"{alert['volume']:,.0f}" if alert["volume"] else "N/A"
    print(f"  24h Volume: {vol_str}")
    print(f"  {'-'*60}")


def print_status(scan_num: int, total: int, ep: int, window: int, alerts_total: int):
    ts = datetime.now().strftime("%H:%M:%S")
    print(
        f"\r{Fore.WHITE}[{ts}] Scan #{scan_num:04d} | "
        f"Total: {total}  Econ/Pol: {ep}  In window: {Fore.CYAN}{window}{Style.RESET_ALL}  | "
        f"Alerts: {Fore.YELLOW}{alerts_total}{Style.RESET_ALL}   ",
        end="",
        flush=True,
    )


# ─────────────────────── MAIN LOOP ────────────────────────────────────────────

def run():
    banner()
    tracker      = SpikeTracker()
    scan_num     = 0
    total_alerts = 0

    print(f"{Fore.GREEN}Scanning for contracts closing within {CLOSING_WINDOW_MAX_HRS} hours...{Style.RESET_ALL}")

    # Test Discord connection on startup
    try:
        resp = requests.post(
            DISCORD_WEBHOOK_URL,
            json={"content": "✅ **Polymarket Spike Scanner is live!** Watching for economic & political contract spikes..."},
            timeout=5,
        )
        if resp.status_code in (200, 204):
            print(f"{Fore.GREEN}Discord webhook connected — test ping sent.{Style.RESET_ALL}\n")
        else:
            print(f"{Fore.RED}Discord webhook error: HTTP {resp.status_code}{Style.RESET_ALL}\n")
    except Exception as e:
        print(f"{Fore.RED}Discord connection failed: {e}{Style.RESET_ALL}\n")

    while True:
        scan_num    += 1
        markets_raw  = fetch_markets(limit=MAX_MARKETS_PER_SCAN)

        # Step 1: keyword filter → economic / political only
        ep_markets = [
            m for m in markets_raw
            if is_econ_political(
                m.get("question", ""),
                m.get("tags", []) + [m.get("category", "")]
            )
        ]

        # Step 2: closing-window filter → 12–24 h remaining
        window_markets = []
        for m in ep_markets:
            in_window, hrs = is_in_closing_window(m)
            if in_window:
                window_markets.append((m, hrs))

        # Step 3: spike detection + high-bids check on qualifying markets only
        for m, hrs in window_markets:
            market_id = m.get("id") or m.get("conditionId") or m.get("slug")
            if not market_id:
                continue
            price  = extract_best_price(m)
            volume = extract_volume(m)
            if price is None:
                continue

            # Spike alerts
            alerts = tracker.update(str(market_id), price, volume, m)
            for a in alerts:
                print_alert(a, hrs)
                send_discord_alert(a, hrs)
                total_alerts += 1

            # High-bids alert (checked every scan but with cooldown)
            last_hb = tracker.high_bids_fired.get(str(market_id), 0)
            if time.time() - last_hb > SCAN_INTERVAL_SEC * 4:
                hb_alert = check_high_bids(m)
                if hb_alert:
                    print_high_bids_alert(hb_alert, hrs)
                    send_discord_alert(hb_alert, hrs)
                    total_alerts += 1
                    tracker.high_bids_fired[str(market_id)] = time.time()

        print_status(scan_num, len(markets_raw), len(ep_markets),
                     len(window_markets), total_alerts)

        time.sleep(SCAN_INTERVAL_SEC)


# ─────────────────────── ENTRY POINT ──────────────────────────────────────────

if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        print(f"\n\n{Fore.CYAN}Scanner stopped.{Style.RESET_ALL}")
        sys.exit(0)
