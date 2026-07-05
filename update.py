import json, re, urllib.request, xml.etree.ElementTree as ET
from html import unescape
from pathlib import Path
from collections import defaultdict
from difflib import SequenceMatcher

EPLAYLIST_URL = "https://magnetic.website/MAD_TITAN_SPORTS/Keep_m3u_json/eplaylist.json"
EPG_URL = "https://magnetic.website/jet/epg/merged_epg.xml"
CACHE_FILE = Path("m3u8_cache.json")
OUTPUT_DIR = Path("playlists")
OUTPUT_DIR.mkdir(exist_ok=True)

PROVIDER_PRIORITY = [
    "FREE3",
    "s.rocketdns.info:8080",
    "technologycloud.eu:80",
    "mainstreams.pro",
    "blog.xyzstreams.shop",
]

def fetch(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0",
        "Accept": "*/*",
        "Referer": "https://magnetic.website/",
    })
    with urllib.request.urlopen(req, timeout=90) as r:
        return r.read()

def clean_title(text):
    text = re.sub(r"\[/?[A-Z0-9]+[^\]]*\]", "", text or "")
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip() or "Unknown"

def clean_channel_name(item):
    title = clean_title(item.get("title", ""))
    for remove in [item.get("domain1", ""), item.get("domain", "")]:
        if remove:
            title = title.replace(remove, "")
    return re.sub(r"\s+", " ", title).strip() or "Unknown"

def normalize(text):
    text = clean_title(text).lower()
    text = text.replace("&", "and")
    text = re.sub(r"\b(hd|sd|east|west|channel|network|television|tv|us|usa)\b", "", text)
    return re.sub(r"[^a-z0-9]+", "", text)

def safe_filename(name):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_") or "unknown"

def load_eplaylist():
    return json.loads(fetch(EPLAYLIST_URL).decode("utf-8")).get("items", [])

def load_cache_channels():
    if not CACHE_FILE.exists():
        return []

    data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    channels = []

    for ch in data:
        name = ch.get("name", "")
        tvg_id = ch.get("tvg_id", "")
        if not name or not tvg_id:
            continue

        channels.append({
            "name": name,
            "tvg_id": tvg_id,
            "logo": ch.get("logo", ""),
            "group": ch.get("group", ""),
            "keys": {normalize(name), normalize(ch.get("normalized_name", ""))}
        })

        url = ch.get("url", "")
        m = re.search(r"/CHANNEL_GUIDE/([^/?]+)\.json", url)
        if m:
            guide_name = m.group(1)
            channels[-1]["keys"].add(normalize(guide_name))
            channels[-1]["keys"].add(normalize(guide_name.replace("_", " ")))

    return channels

def load_epg_channels():
    channels = []
    try:
        xml_bytes = fetch(EPG_URL)
        root = ET.fromstring(xml_bytes)

        for channel in root.findall("channel"):
            tvg_id = channel.attrib.get("id", "")
            names = [d.text for d in channel.findall("display-name") if d.text]

            for name in names:
                channels.append({
                    "name": name,
                    "tvg_id": tvg_id,
                    "logo": "",
                    "group": "",
                    "keys": {normalize(name)}
                })
    except Exception as e:
        print(f"EPG load failed: {e}")

    return channels

def stream_based_names(item):
    names = []
    stream = item.get("stream", "")

    m = re.search(r"/USA_([^/]+)/", stream)
    if m:
        names.append(m.group(1).replace("_", " "))

    return names

def build_match_sources():
    sources = load_cache_channels() + load_epg_channels()

    exact = {}
    all_sources = []

    for src in sources:
        all_sources.append(src)
        for key in src["keys"]:
            if key and key not in exact:
                exact[key] = src

    return exact, all_sources

def match_epg(item, exact_map, all_sources):
    names = [clean_channel_name(item), clean_title(item.get("title", ""))]
    names.extend(stream_based_names(item))

    raw = item.get("raw_title", "")
    if raw:
        names.append(raw.split(" - ")[0])

    keys = [normalize(n) for n in names if n]

    for key in keys:
        if key in exact_map:
            return exact_map[key]

    best = None
    best_score = 0

    for key in keys:
        if not key:
            continue

        for src in all_sources:
            for src_key in src["keys"]:
                score = SequenceMatcher(None, key, src_key).ratio()
                if score > best_score:
                    best_score = score
                    best = src

    if best and best_score >= 0.86:
        return best

    return {}

def write_m3u(items, file_path, exact_map, all_sources):
    lines = ["#EXTM3U"]
    seen = set()

    for item in items:
        stream = item.get("stream", "").strip()
        if not stream or stream in seen:
            continue

        seen.add(stream)
        epg = match_epg(item, exact_map, all_sources)

        name = epg.get("name") or clean_channel_name(item)
        tvg_id = epg.get("tvg_id", "")
        logo = epg.get("logo") or item.get("thumbnail", "")
        group = epg.get("group") or item.get("group") or item.get("category") or item.get("domain1", "Other")

        lines.append(f'#EXTINF:-1 tvg-id="{tvg_id}" tvg-name="{name}" tvg-logo="{logo}" group-title="{group}",{name}')
        lines.append(stream)

    file_path.write_text("\n".join(lines), encoding="utf-8")

def channel_key(item):
    epg_name = clean_channel_name(item)
    stream_names = stream_based_names(item)

    if stream_names:
        return normalize(stream_names[0])

    return normalize(epg_name)

def build_priority_playlist(items):
    selected = {}
    provider_rank = {p: i for i, p in enumerate(PROVIDER_PRIORITY)}

    for item in items:
        domain = item.get("domain1", "")
        if domain not in provider_rank:
            continue

        key = channel_key(item)
        if not key:
            continue

        if key not in selected:
            selected[key] = item
        else:
            current_rank = provider_rank.get(selected[key].get("domain1", ""), 999)
            new_rank = provider_rank.get(domain, 999)
            if new_rank < current_rank:
                selected[key] = item

    return list(selected.values())

def write_domain_report(items):
    domains = defaultdict(int)
    for item in items:
        domains[item.get("domain1") or item.get("domain") or "Unknown"] += 1

    lines = ["# Domain Report", "", "| Channels | Domain |", "|---:|---|"]
    for domain, count in sorted(domains.items(), key=lambda x: x[1], reverse=True):
        lines.append(f"| {count} | `{domain}` |")

    Path("domain-report.md").write_text("\n".join(lines), encoding="utf-8")

def main():
    items = load_eplaylist()
    exact_map, all_sources = build_match_sources()
    write_domain_report(items)

    selected_items = [i for i in items if i.get("domain1") in PROVIDER_PRIORITY]
    priority_items = build_priority_playlist(items)

    write_m3u(selected_items, OUTPUT_DIR / "Combined_Selected.m3u", exact_map, all_sources)
    write_m3u(priority_items, OUTPUT_DIR / "Priority_Clean.m3u", exact_map, all_sources)

    for provider in PROVIDER_PRIORITY:
        provider_items = [i for i in items if i.get("domain1") == provider]
        if provider_items:
            write_m3u(provider_items, OUTPUT_DIR / f"{safe_filename(provider)}.m3u", exact_map, all_sources)

if __name__ == "__main__":
    main()
