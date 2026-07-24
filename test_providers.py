import hashlib
import json
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

EPLAYLIST_URL = (
    "https://magnetic.website/"
    "MAD_TITAN_SPORTS/Keep_m3u_json/eplaylist.json"
)

NOTES_FILE = Path("provider-notes.json")
HEALTH_FILE = Path("provider-health.json")
FAILURES_FILE = Path("stream-failures.json")
REPORT_FILE = Path("reports/provider-health.md")

REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 Chrome/126.0 Safari/537.36"
)
REQUEST_TIMEOUT = 12
MAX_WORKERS = 20
MAX_READ_BYTES = 64 * 1024
VALID_STATUSES = {"working", "partial", "untested", "dead"}


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def fetch_bytes(url, headers=None, timeout=REQUEST_TIMEOUT, max_bytes=None):
    request_headers = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Referer": "https://magnetic.website/",
    }
    if headers:
        request_headers.update(headers)

    request = urllib.request.Request(url, headers=request_headers)
    started = time.monotonic()
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if max_bytes:
            body = response.read(max_bytes)
        else:
            body = response.read()
        elapsed = time.monotonic() - started
        return body, response.headers, response.geturl(), elapsed


def load_source_items():
    body, _, _, _ = fetch_bytes(EPLAYLIST_URL, timeout=60)
    payload = json.loads(body.decode("utf-8"))
    items = payload.get("items", [])
    if not isinstance(items, list):
        raise ValueError("eplaylist.json does not contain a valid items list")
    return [item for item in items if str(item.get("stream", "")).strip()]


def load_notes():
    if not NOTES_FILE.exists():
        return {}
    try:
        data = json.loads(NOTES_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as error:
        print(f"Warning: could not read provider-notes.json: {error}")
        return {}


def split_kodi_url(raw_url):
    """Split URL and Kodi-style pipe headers, such as |Referer=...&User-Agent=..."""
    raw_url = str(raw_url or "").strip()
    if "|" not in raw_url:
        return raw_url, {}

    url, option_text = raw_url.split("|", 1)
    headers = {}
    for key, value in urllib.parse.parse_qsl(option_text, keep_blank_values=True):
        normalized = key.strip().lower().replace("_", "-")
        header_name = {
            "user-agent": "User-Agent",
            "referer": "Referer",
            "referrer": "Referer",
            "origin": "Origin",
            "cookie": "Cookie",
            "authorization": "Authorization",
        }.get(normalized, key.strip())
        headers[header_name] = value
    return url.strip(), headers


def looks_like_hls(url, headers, body):
    content_type = str(headers.get("Content-Type", "")).lower()
    prefix = body[:1024].lstrip()
    return (
        ".m3u8" in urllib.parse.urlsplit(url).path.lower()
        or "mpegurl" in content_type
        or prefix.startswith(b"#EXTM3U")
    )


def parse_playlist_lines(body):
    text = body.decode("utf-8", errors="replace")
    return [line.strip() for line in text.splitlines() if line.strip()]


def choose_playlist_target(lines):
    candidates = [line for line in lines if not line.startswith("#")]
    if not candidates:
        return None
    return candidates[0]


def validate_hls(url, request_headers, body, depth=0):
    lines = parse_playlist_lines(body)
    if not lines or not lines[0].startswith("#EXTM3U"):
        raise ValueError("response is not a valid HLS playlist")

    target = choose_playlist_target(lines)
    if not target:
        raise ValueError("HLS playlist contains no playable target")

    target_url = urllib.parse.urljoin(url, target)
    target_body, target_headers, final_url, elapsed = fetch_bytes(
        target_url,
        headers=request_headers,
        max_bytes=MAX_READ_BYTES,
    )

    if looks_like_hls(final_url, target_headers, target_body) and depth < 2:
        nested_elapsed = validate_hls(
            final_url,
            request_headers,
            target_body,
            depth=depth + 1,
        )
        return elapsed + nested_elapsed

    if not target_body:
        raise ValueError("media segment returned no data")

    return elapsed


def test_stream(provider, item):
    raw_url = str(item.get("stream", "")).strip()
    title = str(item.get("title", "Unknown")).strip() or "Unknown"
    started = time.monotonic()

    try:
        url, headers = split_kodi_url(raw_url)
        if not re.match(r"^https?://", url, re.IGNORECASE):
            raise ValueError("unsupported or missing HTTP URL")

        body, response_headers, final_url, manifest_elapsed = fetch_bytes(
            url,
            headers=headers,
            max_bytes=MAX_READ_BYTES,
        )

        if not body:
            raise ValueError("stream returned no data")

        segment_elapsed = 0.0
        test_type = "direct"
        if looks_like_hls(final_url, response_headers, body):
            test_type = "hls"
            segment_elapsed = validate_hls(final_url, headers, body)

        total_elapsed = time.monotonic() - started
        return {
            "provider": provider,
            "title": title,
            "stream": raw_url,
            "passed": True,
            "test_type": test_type,
            "latency_seconds": round(total_elapsed, 3),
            "manifest_seconds": round(manifest_elapsed, 3),
            "segment_seconds": round(segment_elapsed, 3),
            "error": "",
        }

    except urllib.error.HTTPError as error:
        message = f"HTTP {error.code}: {error.reason}"
    except urllib.error.URLError as error:
        message = f"Network error: {error.reason}"
    except Exception as error:
        message = str(error)

    return {
        "provider": provider,
        "title": title,
        "stream": raw_url,
        "passed": False,
        "test_type": "unknown",
        "latency_seconds": round(time.monotonic() - started, 3),
        "manifest_seconds": None,
        "segment_seconds": None,
        "error": message[:500],
    }


def sample_count(channel_count):
    if channel_count <= 5:
        return channel_count
    if channel_count <= 25:
        return 5
    if channel_count <= 75:
        return 8
    return 10


def rotating_sample(provider, entries, count):
    if count >= len(entries):
        return list(entries)

    # Changes daily but remains reproducible during a workflow run.
    day_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    digest = hashlib.sha256(f"{provider}|{day_key}".encode("utf-8")).hexdigest()
    rng = random.Random(int(digest[:16], 16))
    return rng.sample(entries, count)


def classify(success_rate, passed, tested):
    if tested == 0:
        return "untested"
    if passed == 0 or success_rate < 20:
        return "dead"
    if success_rate >= 80:
        return "working"
    return "partial"


def legacy_manual_status(note):
    manual = str(note.get("manual_status", "")).strip().lower()
    if manual in VALID_STATUSES and manual != "untested":
        return manual

    legacy = str(note.get("status", "")).strip().lower()
    if note.get("tested") and legacy in VALID_STATUSES and legacy != "untested":
        return legacy

    return ""


def effective_status(manual_status, auto_status):
    if manual_status in VALID_STATUSES and manual_status != "untested":
        return manual_status
    if auto_status in VALID_STATUSES:
        return auto_status
    return "untested"


def health_bar(percent):
    filled = max(0, min(10, round(percent / 10)))
    return "█" * filled + "░" * (10 - filled)


def main():
    print("Downloading source playlist for automated provider testing...")
    items = load_source_items()

    provider_map = defaultdict(list)
    for item in items:
        provider = str(item.get("domain1") or item.get("domain") or "Unknown").strip()
        provider_map[provider].append(item)

    print(f"Providers found: {len(provider_map)}")

    jobs = []
    for provider, entries in provider_map.items():
        selected = rotating_sample(provider, entries, sample_count(len(entries)))
        for item in selected:
            jobs.append((provider, item))

    print(f"Streams selected for testing: {len(jobs)}")

    results = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_map = {
            executor.submit(test_stream, provider, item): (provider, item)
            for provider, item in jobs
        }
        for index, future in enumerate(as_completed(future_map), start=1):
            result = future.result()
            results.append(result)
            state = "PASS" if result["passed"] else "FAIL"
            print(f"[{index}/{len(jobs)}] {state} {result['provider']} - {result['title']}")

    by_provider_results = defaultdict(list)
    for result in results:
        by_provider_results[result["provider"]].append(result)

    tested_at = utc_now()
    health = {}
    failures = []

    for provider in sorted(provider_map, key=str.lower):
        provider_results = by_provider_results.get(provider, [])
        tested = len(provider_results)
        passed = sum(1 for result in provider_results if result["passed"])
        failed = tested - passed
        success_rate = round((passed / tested) * 100, 1) if tested else 0.0
        passed_latencies = [
            result["latency_seconds"]
            for result in provider_results
            if result["passed"]
        ]
        average_latency = (
            round(sum(passed_latencies) / len(passed_latencies), 3)
            if passed_latencies
            else None
        )
        auto_status = classify(success_rate, passed, tested)

        health[provider] = {
            "channel_count": len(provider_map[provider]),
            "tested_channels": tested,
            "passed": passed,
            "failed": failed,
            "success_rate": success_rate,
            "average_latency_seconds": average_latency,
            "auto_status": auto_status,
            "last_tested": tested_at,
        }

        failures.extend(result for result in provider_results if not result["passed"])

    existing_notes = load_notes()
    updated_notes = {}

    for provider in sorted(provider_map, key=str.lower):
        existing = existing_notes.get(provider, {})
        manual_status = legacy_manual_status(existing)
        auto_status = health[provider]["auto_status"]
        status = effective_status(manual_status, auto_status)

        updated = dict(existing)
        updated.update(
            {
                "tested": bool(manual_status) or health[provider]["tested_channels"] > 0,
                "status": status,
                "manual_status": manual_status,
                "auto_status": auto_status,
                "health": health[provider]["success_rate"],
                "tested_channels": health[provider]["tested_channels"],
                "passed": health[provider]["passed"],
                "failed": health[provider]["failed"],
                "latency": health[provider]["average_latency_seconds"],
                "last_tested": tested_at,
                "notes": str(existing.get("notes", "")),
            }
        )
        updated_notes[provider] = updated

    NOTES_FILE.write_text(json.dumps(updated_notes, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    HEALTH_FILE.write_text(json.dumps(health, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    FAILURES_FILE.write_text(json.dumps(failures, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    report_lines = [
        "# Automated Provider Health Report",
        "",
        f"**Last tested:** {tested_at}",
        "",
        "Manual status remains authoritative when present. Otherwise, playlists use the automated status.",
        "",
        "| Provider | Channels | Tested | Passed | Health | Auto status | Latency |",
        "|---|---:|---:|---:|---|---|---:|",
    ]

    sorted_providers = sorted(
        health,
        key=lambda provider: (
            {"working": 0, "partial": 1, "untested": 2, "dead": 3}.get(health[provider]["auto_status"], 2),
            -health[provider]["success_rate"],
            provider.lower(),
        ),
    )

    for provider in sorted_providers:
        record = health[provider]
        latency = (
            f"{record['average_latency_seconds']:.3f}s"
            if record["average_latency_seconds"] is not None
            else "—"
        )
        report_lines.append(
            f"| `{provider}` | {record['channel_count']} | {record['tested_channels']} | "
            f"{record['passed']} | {health_bar(record['success_rate'])} {record['success_rate']:.1f}% | "
            f"{record['auto_status']} | {latency} |"
        )

    REPORT_FILE.write_text("\n".join(report_lines) + "\n", encoding="utf-8")

    working = sum(1 for record in health.values() if record["auto_status"] == "working")
    partial = sum(1 for record in health.values() if record["auto_status"] == "partial")
    dead = sum(1 for record in health.values() if record["auto_status"] == "dead")

    print("")
    print("Automated provider scan complete")
    print(f"Working providers: {working}")
    print(f"Partial providers: {partial}")
    print(f"Dead providers: {dead}")
    print(f"Failed sampled streams: {len(failures)}")


if __name__ == "__main__":
    main()
