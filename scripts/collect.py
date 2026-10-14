"""Superstar Edge collector: records Kalshi 15-min XRP results by time slot.
Seeds from forecaster ledgers, then collects new windows every 15 min.
Data saved to repo data branch as results.jsonl + stats.json."""
import json, urllib.request, os, math
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

UA = {"User-Agent": "superstar-edge/1.0 (research)"}
PT = ZoneInfo("America/Los_Angeles")
ET = ZoneInfo("America/New_York")

def get(url, timeout=20):
    try:
        req = urllib.request.Request(url, headers=UA)
        return json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode())
    except Exception as e:
        print(f"  fetch failed: {str(e)[:80]}")
        return None

def seed_from_ledgers():
    """Pull scored windows from forecaster repos. Returns list of (ws_ts, y)."""
    import base64, sys
    sys.path.insert(0, '/opt/hatch/skills/skill-creator/bin')
    try:
        from dynamic_credentials import add_surrogate_to_request, read_json_response
    except ImportError:
        print("  (no github creds, skipping seed)")
        return []
    def gh(path):
        req = urllib.request.Request(f"https://api.github.com{path}", headers=UA)
        add_surrogate_to_request(req, "custom.github", allowed_hosts=["api.github.com"])
        return read_json_response(urllib.request.urlopen(req, timeout=30))
    results = []
    seen = set()
    for repo in ["MisoPrettyStacks/econ-forecaster",
                 "MisoPrettyStacks/how-to-make-frog-food-recipe",
                 "MisoPrettyStacks/TeacupChihuahua",
                 "MisoPrettyStacks/Orange-Kitty"]:
        try:
            d = gh(f"/repos/{repo}/contents/data/ledger.jsonl?ref=data")
            for line in base64.b64decode(d["content"]).decode().strip().split("\n"):
                if not line.strip():
                    continue
                r = json.loads(line)
                ws, y = r.get("ws"), r.get("y")
                if ws and y is not None and ws not in seen:
                    seen.add(ws)
                    results.append({"ws": ws, "y": int(y), "src": "seed"})
        except Exception as e:
            print(f"  seed {repo}: {str(e)[:60]}")
    print(f"  seeded {len(results)} unique windows")
    return results

def get_kalshi_target():
    d = get("https://api.elections.kalshi.com/trade-api/v2/markets?series_ticker=KXXRP15M&limit=20&status=open")
    if not d:
        return None, None
    now = datetime.now(timezone.utc)
    for m in d.get("markets", []):
        if m.get("status") not in ("active", "open"):
            continue
        ot = datetime.fromisoformat(m["open_time"].replace("Z", "+00:00"))
        ct = datetime.fromisoformat(m["close_time"].replace("Z", "+00:00"))
        if ot <= now < ct:
            return m.get("floor_strike"), m.get("ticker")
    return None, None

def get_close(ws_ts):
    try:
        url = f"https://api.exchange.coinbase.com/products/XRP-USD/candles?start={ws_ts}&end={ws_ts+900}&granularity=900"
        req = urllib.request.Request(url, headers=UA)
        bars = json.loads(urllib.request.urlopen(req, timeout=15).read().decode())
        if bars:
            return float(bars[0][4])
    except Exception:
        pass
    return None

def compute_stats(results):
    """Compute per-slot UP%, z-score, significance. Slots in ET (like reference)."""
    from collections import defaultdict
    slots = defaultdict(lambda: {"up": 0, "down": 0})
    for r in results:
        ws = datetime.fromtimestamp(r["ws"], timezone.utc)
        et = ws.astimezone(ET)
        slot = et.strftime("%H:%M")
        if r["y"] == 1:
            slots[slot]["up"] += 1
        else:
            slots[slot]["down"] += 1
    out = []
    for slot in sorted(slots.keys()):
        s = slots[slot]
        n = s["up"] + s["down"]
        up_pct = s["up"] / n * 100 if n else 50
        # z-score vs 50% null
        p = s["up"] / n if n else 0.5
        se = math.sqrt(0.25 / n) if n else 1
        z = (p - 0.5) / se if se else 0
        # stars: |z|>=2.58:3, >=1.96:2, >=1.65:1
        az = abs(z)
        stars = 3 if az >= 2.58 else (2 if az >= 1.96 else (1 if az >= 1.65 else 0))
        out.append({
            "slot_et": slot,
            "n": n, "up": s["up"], "down": s["down"],
            "up_pct": round(up_pct, 1),
            "z": round(z, 2), "stars": stars,
        })
    out.sort(key=lambda s: abs(s["z"]), reverse=True)
    return out

def main():
    out_dir = os.environ.get("FC_OUT", "data")
    os.makedirs(out_dir, exist_ok=True)
    
    # Load existing
    res_path = os.path.join(out_dir, "results.jsonl")
    results, seen = [], set()
    if os.path.exists(res_path):
        for line in open(res_path):
            line = line.strip()
            if line:
                try:
                    r = json.loads(line)
                    results.append(r)
                    seen.add(r["ws"])
                except:
                    pass
    print(f"Loaded {len(results)} existing results")
    
    # Seed on first run
    if not results:
        for r in seed_from_ledgers():
            if r["ws"] not in seen:
                results.append(r)
                seen.add(r["ws"])
    
    # Collect new: check last few windows
    now = datetime.now(timezone.utc)
    for i in range(1, 5):
        ws = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0) - timedelta(minutes=15*i)
        ws_ts = int(ws.timestamp())
        if ws_ts in seen:
            continue
        # Get Kalshi target for that window (use current as proxy if recent)
        k, _ = get_kalshi_target()
        close = get_close(ws_ts)
        if close is not None and k is not None:
            # Note: using current target as proxy for past windows is approximate
            # Only record if window is recent (< 2h) where target likely similar
            age_h = (now - ws).total_seconds() / 3600
            if age_h < 2:
                y = 1 if close > k else 0
                results.append({"ws": ws_ts, "y": y, "src": "live", "k": k})
                seen.add(ws_ts)
                print(f"  recorded {ws.strftime('%H:%M')}: {'UP' if y else 'DOWN'}")
    
    # Save results
    with open(res_path, "w") as f:
        for r in sorted(results, key=lambda x: x["ws"]):
            f.write(json.dumps(r) + "\n")
    
    # Compute and save stats
    stats = compute_stats(results)
    sig = [s for s in stats if s["stars"] >= 1]
    print(f"\nTotal: {len(results)} windows, {len(stats)} slots, {len(sig)} significant")
    
    with open(os.path.join(out_dir, "stats.json"), "w") as f:
        json.dump({
            "generated": now.isoformat(),
            "total_windows": len(results),
            "slots": stats,
            "significant": sig,
        }, f, indent=1)
    print("Done.")

if __name__ == "__main__":
    main()
