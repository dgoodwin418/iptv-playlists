import concurrent.futures
import json
import os
import random
import re
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

CONFIG_FILE = Path("sources.json")
HEALTH_FILE = Path("provider-health.json")
STREAM_HEALTH_FILE = Path("stream-health.json")
FAIL_FILE = Path("stream-failures.json")
REPORT_DIR = Path("reports")
REPORT_DIR.mkdir(exist_ok=True)
TIMEOUT = 10
WORKERS = 20
MAX_DUPLICATE_TESTS_PER_PROVIDER = 25
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


def normalize(text):
    text = (text or "").lower().replace("&", "and")
    text = re.sub(r"\b(hd|sd|uhd|fhd|east|west|channel|network|television|tv|us|usa|feed|stream)\b", "", text)
    return re.sub(r"[^a-z0-9]+", "", text)


def attrs_from_extinf(line):
    attrs = {k.lower(): v for k, v in re.findall(r'([\w-]+)="([^"]*)"', line)}
    name = line.split(",", 1)[1].strip() if "," in line else "Unknown"
    return attrs, name


def parse_items(text):
    out = []
    pending = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXTINF"):
            attrs, name = attrs_from_extinf(line)
            pending = {
                "name": name or attrs.get("tvg-name") or "Unknown",
                "tvg_id": attrs.get("tvg-id", ""),
                "tvg_name": attrs.get("tvg-name", "") or name,
            }
        elif not line.startswith("#"):
            meta = pending or {"name": "Unknown", "tvg_id": "", "tvg_name": "Unknown"}
            out.append({**meta, "url": line, "provider": provider(line)})
            pending = None
    return out


def channel_key(item):
    tid = (item.get("tvg_id") or "").strip().lower()
    if tid:
        return "id:" + tid
    n = normalize(item.get("tvg_name") or item.get("name"))
    return "name:" + n if n else "url:" + item.get("url", "")


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


def load_json(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def load_items():
    items = []
    failures = []
    for src in configured_sources():
        try:
            text = fetch_text(src["url"])
            vals = parse_items(text)
            for v in vals:
                v["source_id"] = src.get("id", "unknown")
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
    buckets = defaultdict(list)
    for x in items:
        scheme = urllib.parse.urlsplit(x["url"].split("|", 1)[0]).scheme.lower()
        if scheme in ("http", "https"):
            groups[x["provider"]].append(x)
            buckets[channel_key(x)].append(x)

    duplicate_urls = defaultdict(list)
    for vals in buckets.values():
        unique = {x["url"]: x for x in vals}
        if len(unique) > 1:
            for x in unique.values():
                duplicate_urls[x["provider"]].append(x)

    jobs_by_url = {}
    seed = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for p, vals in groups.items():
        rng = random.Random(seed + "|provider|" + p)
        for x in rng.sample(vals, min(sample_count(len(vals)), len(vals))):
            jobs_by_url[x["url"]] = (p, x)

        dupes = list({x["url"]: x for x in duplicate_urls.get(p, [])}.values())
        if dupes:
            rng2 = random.Random(seed + "|duplicates|" + p)
            for x in rng2.sample(dupes, min(MAX_DUPLICATE_TESTS_PER_PROVIDER, len(dupes))):
                jobs_by_url[x["url"]] = (p, x)

    jobs = list(jobs_by_url.values())
    results = defaultdict(list)
    all_results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(test_stream, x): p for p, x in jobs}
        for fut in concurrent.futures.as_completed(futs):
            r = fut.result()
            results[futs[fut]].append(r)
            all_results.append(r)

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

    stream_health = load_json(STREAM_HEALTH_FILE)
    current_urls = {x["url"] for x in items}
    for url in list(stream_health):
        if url not in current_urls:
            del stream_health[url]

    for r in all_results:
        old = stream_health.get(r["url"], {}) if isinstance(stream_health.get(r["url"], {}), dict) else {}
        previous_failures = int(old.get("consecutive_failures", 0) or 0)
        consecutive = 0 if r["ok"] else previous_failures + 1
        stream_health[r["url"]] = {
            "name": r.get("name", "Unknown"),
            "provider": r.get("provider", "Unknown"),
            "source_id": r.get("source_id", ""),
            "last_result": "working" if r["ok"] else "failed",
            "consecutive_failures": consecutive,
            "last_tested": now,
            "last_latency_seconds": r.get("latency"),
            "last_error": "" if r["ok"] else r.get("reason", ""),
        }

    HEALTH_FILE.write_text(json.dumps(health, indent=2) + "\n", encoding="utf-8")
    STREAM_HEALTH_FILE.write_text(json.dumps(stream_health, indent=2) + "\n", encoding="utf-8")
    FAIL_FILE.write_text(json.dumps(failures, indent=2) + "\n", encoding="utf-8")

    lines = ["# Provider Health", "", f"Updated: **{now}**", "",
             "| Provider | Channels | Tested | Passed | Health | Status | Latency |",
             "|---|---:|---:|---:|---:|---|---:|"]
    for p, h in sorted(health.items(), key=lambda kv: (-kv[1]["success_rate"], -kv[1]["channel_count"], kv[0].lower())):
        lat = "—" if h["average_latency_seconds"] is None else f"{h['average_latency_seconds']:.3f}s"
        lines.append(f"| `{p}` | {h['channel_count']} | {h['tested_channels']} | {h['passed']} | {h['success_rate']}% | {h['auto_status']} | {lat} |")
    (REPORT_DIR / "provider-health.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    dead_individual = sum(1 for v in stream_health.values() if isinstance(v, dict) and int(v.get("consecutive_failures", 0) or 0) >= 3)
    print(f"Tested {len(jobs)} streams across {len(groups)} providers")
    print(f"Tracked {len(stream_health):,} individual streams; {dead_individual:,} currently have 3+ consecutive failures")


if __name__ == "__main__":
    main()
