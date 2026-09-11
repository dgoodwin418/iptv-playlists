import concurrent.futures
import json
import os
import random
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

CONFIG_FILE = Path("sources.json")
HEALTH_FILE = Path("provider-health.json")
FAIL_FILE = Path("stream-failures.json")
REPORT_DIR = Path("reports")
REPORT_DIR.mkdir(exist_ok=True)
TIMEOUT = 10
WORKERS = 20
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36"


def fetch_text(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def configured_sources():
    cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    out = [s for s in cfg.get("playlist_sources", []) if s.get("enabled", True) and s.get("url")]
    for env_name, sid, label in (
        ("IPTV_PRIVATE_SOURCE_107", "private-107", "Private source 107"),
        ("IPTV_PRIVATE_SOURCE_109", "private-109", "Private source 109"),
    ):
        url = os.environ.get(env_name, "").strip()
        if url:
            out.append({"id": sid, "name": label, "url": url})
    return out


def parse_items(text):
    out, name = [], "Unknown"
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXTINF"):
            name = line.split(",", 1)[1].strip() if "," in line else "Unknown"
        elif not line.startswith("#"):
            out.append({"name": name, "url": line, "provider": provider(line)})
            name = "Unknown"
    return out


def provider(url):
    base = (url or "").split("|", 1)[0]
    p = urllib.parse.urlsplit(base)
    return p.netloc or p.scheme or "Unknown"


def parse_url(raw):
    parts = raw.split("|", 1)
    url = parts[0].strip()
    headers = {}
    if len(parts) > 1:
        for pair in parts[1].split("&"):
            if "=" not in pair:
                continue
            k, v = pair.split("=", 1)
            k = urllib.parse.unquote_plus(k).strip().lower()
            v = urllib.parse.unquote_plus(v)
            mapping = {"referer": "Referer", "referrer": "Referer", "origin": "Origin", "user-agent": "User-Agent", "useragent": "User-Agent", "cookie": "Cookie", "authorization": "Authorization"}
            headers[mapping.get(k, k)] = v
    headers.setdefault("User-Agent", USER_AGENT)
    return url, headers


def request_bytes(url, headers, timeout=TIMEOUT, limit=262144):
    req = urllib.request.Request(url, headers=headers)
    t = time.monotonic()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read(limit)
        final = r.geturl()
    return data, final, time.monotonic() - t


def test_stream(item):
    raw = item["url"]
    base, headers = parse_url(raw)
    t0 = time.monotonic()
    try:
        scheme = urllib.parse.urlsplit(base).scheme.lower()
        if scheme not in ("http", "https"):
            return {"ok": False, "reason": f"unsupported scheme {scheme}", "latency": 0, **item}
        body, final, _ = request_bytes(base, headers)
        if not body:
            raise ValueError("empty response")
        text = body.decode("utf-8", "ignore")
        if "#EXTM3U" in text:
            refs = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
            if refs:
                target = urllib.parse.urljoin(final, refs[0])
                b2, f2, _ = request_bytes(target, headers)
                if b2.startswith(b"#EXTM3U"):
                    tx = b2.decode("utf-8", "ignore")
                    refs2 = [ln.strip() for ln in tx.splitlines() if ln.strip() and not ln.startswith("#")]
                    if refs2:
                        request_bytes(urllib.parse.urljoin(f2, refs2[0]), headers, limit=65536)
                elif not b2:
                    raise ValueError("empty media segment")
        return {"ok": True, "reason": "", "latency": round(time.monotonic() - t0, 3), **item}
    except Exception as exc:
        return {"ok": False, "reason": str(exc)[:300], "latency": round(time.monotonic() - t0, 3), **item}


def sample_count(n):
    if n <= 5: return n
    if n <= 25: return 5
    if n <= 75: return 8
    return 10


def load_items():
    items = []
    failures = []
    for src in configured_sources():
        try:
            text = fetch_text(src["url"])
            vals = parse_items(text)
            items.extend(vals)
            print(f"Loaded {len(vals):,} test candidates from {src.get('name', src['id'])}")
        except Exception as exc:
            failures.append({"source": src.get("name", src["id"]), "reason": str(exc)[:300]})
            print(f"WARNING: source skipped during testing: {src.get('name', src['id'])}: {exc}")
    if not items:
        raise RuntimeError("No source playlists could be downloaded for testing")
    return items, failures


def main():
    items, source_failures = load_items()
    groups = defaultdict(list)
    for x in items:
        scheme = urllib.parse.urlsplit(x["url"].split("|", 1)[0]).scheme.lower()
        if scheme in ("http", "https"):
            groups[x["provider"]].append(x)

    jobs = []
    seed = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for p, vals in groups.items():
        rng = random.Random(seed + "|" + p)
        jobs.extend((p, x) for x in rng.sample(vals, min(sample_count(len(vals)), len(vals))))

    results = defaultdict(list)
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(test_stream, x): p for p, x in jobs}
        for fut in concurrent.futures.as_completed(futs):
            results[futs[fut]].append(fut.result())

    health, failures = {}, list(source_failures)
    now = datetime.now(timezone.utc).isoformat()
    for p, vals in groups.items():
        rr = results.get(p, [])
        passed = sum(1 for r in rr if r["ok"])
        total = len(rr)
        rate = round(100 * passed / total, 1) if total else 0
        avg = round(sum(r["latency"] for r in rr if r["ok"]) / passed, 3) if passed else None
        status = "working" if rate >= 80 else "partial" if rate >= 20 else "dead"
        health[p] = {"channel_count": len(vals), "tested_channels": total, "passed": passed,
                     "failed": total - passed, "success_rate": rate, "average_latency_seconds": avg,
                     "auto_status": status, "last_tested": now}
        for r in rr:
            if not r["ok"]:
                failures.append({"provider": p, **r})

    HEALTH_FILE.write_text(json.dumps(health, indent=2) + "\n", encoding="utf-8")
    FAIL_FILE.write_text(json.dumps(failures, indent=2) + "\n", encoding="utf-8")

    lines = ["# Provider Health", "", f"Updated: **{now}**", "",
             "| Provider | Channels | Tested | Passed | Health | Status | Latency |",
             "|---|---:|---:|---:|---:|---|---:|"]
    for p, h in sorted(health.items(), key=lambda kv: (-kv[1]["success_rate"], -kv[1]["channel_count"], kv[0].lower())):
        lat = "—" if h["average_latency_seconds"] is None else f"{h['average_latency_seconds']:.3f}s"
        lines.append(f"| `{p}` | {h['channel_count']} | {h['tested_channels']} | {h['passed']} | {h['success_rate']}% | {h['auto_status']} | {lat} |")
    (REPORT_DIR / "provider-health.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Tested {len(jobs)} sample streams across {len(groups)} providers")


if __name__ == "__main__":
    main()
