import json
import re
import urllib.request
from html import unescape
from pathlib import Path
from collections import defaultdict

SOURCE_URL = "https://magnetic.website/MAD_TITAN_SPORTS/Keep_m3u_json/eplaylist.json"

OUTPUT_DIR = Path("playlists")
OUTPUT_DIR.mkdir(exist_ok=True)

SELECTED_DOMAINS = {
    "FREE3": "FREE3.m3u",
    "s.rocketdns.info:8080": "RocketDNS.m3u",
    "technologycloud.eu:80": "TechnologyCloud.m3u",
    "mainstreams.pro": "MainStreams.m3u",
    "blog.xyzstreams.shop": "XYZStreams.m3u",
}

def clean_title(title):
    title = re.sub(r"\[/?[A-Z0-9]+[^\]]*\]", "", title or "")
    title = unescape(title)
    title = re.sub(r"\s+", " ", title).strip()
    return title or "Unknown"

def safe_filename(name):
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
    return name.strip("_") or "unknown"

def get_items():
    req = urllib.request.Request(
        SOURCE_URL,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36",
            "Accept": "application/json,text/plain,*/*",
            "Referer": "https://magnetic.website/",
        },
    )
    with urllib.request.urlopen(req, timeout=45) as response:
        return json.loads(response.read().decode("utf-8")).get("items", [])

def write_m3u(items, file_path):
    lines = ["#EXTM3U"]
    seen_streams = set()

    for item in items:
        stream = item.get("stream", "").strip()
        if not stream or stream in seen_streams:
            continue

        seen_streams.add(stream)

        name = clean_title(item.get("title"))
        logo = item.get("thumbnail", "")
        group = item.get("group") or item.get("category") or item.get("domain1", "Other")
        tvg_id = item.get("tvg_id") or item.get("epg_id") or ""
        tvg_name = name

        lines.append(
            f'#EXTINF:-1 tvg-id="{tvg_id}" tvg-name="{tvg_name}" '
            f'tvg-logo="{logo}" group-title="{group}",{name}'
        )
        lines.append(stream)

    file_path.write_text("\n".join(lines), encoding="utf-8")

def write_domain_report(domain_map):
    lines = ["# Domain Report", "", "| Channels | Domain |", "|---:|---|"]

    for domain, items in sorted(domain_map.items(), key=lambda x: len(x[1]), reverse=True):
        lines.append(f"| {len(items)} | `{domain}` |")

    Path("domain-report.md").write_text("\n".join(lines), encoding="utf-8")

def main():
    items = get_items()

    domain_map = defaultdict(list)
    for item in items:
        domain = item.get("domain1") or item.get("domain") or "Unknown"
        domain_map[domain].append(item)

    write_domain_report(domain_map)

    combined_selected = []

    for domain, filename in SELECTED_DOMAINS.items():
        domain_items = domain_map.get(domain, [])
        if domain_items:
            write_m3u(domain_items, OUTPUT_DIR / filename)
            combined_selected.extend(domain_items)

    write_m3u(combined_selected, OUTPUT_DIR / "Combined_Selected.m3u")

    for domain, domain_items in domain_map.items():
        if len(domain_items) >= 10:
            filename = safe_filename(domain) + ".m3u"
            write_m3u(domain_items, OUTPUT_DIR / filename)

    write_m3u(items, OUTPUT_DIR / "Everything.m3u")

if __name__ == "__main__":
    main()
