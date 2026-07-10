import json
import re
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict
from difflib import SequenceMatcher
from html import unescape
from pathlib import Path

EPLAYLIST_URL = (
    "https://magnetic.website/"
    "MAD_TITAN_SPORTS/Keep_m3u_json/eplaylist.json"
)

EPG_URL = "https://magnetic.website/jet/epg/merged_epg.xml"

CACHE_FILE = Path("m3u8_cache.json")
OUTPUT_DIR = Path("playlists")
PROVIDER_DIR = OUTPUT_DIR / "by-provider"

OUTPUT_DIR.mkdir(exist_ok=True)
PROVIDER_DIR.mkdir(parents=True, exist_ok=True)

# Only providers with at least this many entries get their own playlist.
MIN_PROVIDER_CHANNELS = 5

# Later, reorder this after testing providers.
PROVIDER_PRIORITY = [
    "FREE3",
    "s.rocketdns.info:8080",
    "technologycloud.eu:80",
    "mainstreams.pro",
    "blog.xyzstreams.shop",
]


def fetch(url):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/126.0 Safari/537.36"
            ),
            "Accept": "*/*",
            "Referer": "https://magnetic.website/",
        },
    )

    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read()


def clean_title(text):
    text = re.sub(r"\[/?[A-Z0-9]+[^\]]*\]", "", text or "")
    text = unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or "Unknown"


def clean_channel_name(item):
    title = clean_title(item.get("title", ""))

    for provider_text in [
        item.get("domain1", ""),
        item.get("domain", ""),
    ]:
        if provider_text:
            title = title.replace(provider_text, "")

    title = re.sub(r"\s+", " ", title).strip()
    return title or "Unknown"


def normalize(text):
    text = clean_title(text).lower()
    text = text.replace("&", "and")

    text = re.sub(
        r"\b("
        r"hd|sd|uhd|fhd|east|west|channel|network|"
        r"television|tv|us|usa|feed|stream"
        r")\b",
        "",
        text,
    )

    return re.sub(r"[^a-z0-9]+", "", text)


def safe_filename(name):
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
    name = name.strip("._-")
    return name or "unknown"


def escape_attribute(value):
    value = str(value or "")
    return (
        value.replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def load_eplaylist():
    data = json.loads(fetch(EPLAYLIST_URL).decode("utf-8"))
    return data.get("items", [])


def load_cache_channels():
    if not CACHE_FILE.exists():
        print("Warning: m3u8_cache.json was not found.")
        return []

    data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    channels = []

    for channel in data:
        name = channel.get("name", "")
        tvg_id = channel.get("tvg_id", "")

        if not name or not tvg_id:
            continue

        keys = {
            normalize(name),
            normalize(channel.get("normalized_name", "")),
        }

        url = channel.get("url", "")
        match = re.search(r"/CHANNEL_GUIDE/([^/?]+)\.json", url)

        if match:
            guide_name = match.group(1)
            keys.add(normalize(guide_name))
            keys.add(normalize(guide_name.replace("_", " ")))

        channels.append(
            {
                "name": name,
                "tvg_id": tvg_id,
                "logo": channel.get("logo", ""),
                "group": channel.get("group", ""),
                "keys": {key for key in keys if key},
            }
        )

    return channels


def load_epg_channels():
    channels = []

    try:
        root = ET.fromstring(fetch(EPG_URL))

        for channel in root.findall("channel"):
            tvg_id = channel.attrib.get("id", "")

            display_names = [
                element.text.strip()
                for element in channel.findall("display-name")
                if element.text and element.text.strip()
            ]

            icon = channel.find("icon")
            logo = ""

            if icon is not None:
                logo = icon.attrib.get("src", "")

            for name in display_names:
                channels.append(
                    {
                        "name": name,
                        "tvg_id": tvg_id,
                        "logo": logo,
                        "group": "",
                        "keys": {normalize(name)},
                    }
                )

    except Exception as error:
        print(f"Warning: EPG channel loading failed: {error}")

    return channels


def stream_based_names(item):
    names = []
    stream = item.get("stream", "")

    # FREE3 format:
    # /USA_CMT/index.m3u8
    match = re.search(r"/USA_([^/]+)/", stream, re.IGNORECASE)

    if match:
        stream_name = match.group(1)
        names.append(stream_name.replace("_", " "))
        names.append(stream_name)

    return names


def build_match_sources():
    sources = load_cache_channels() + load_epg_channels()
    exact_map = {}

    for source in sources:
        for key in source["keys"]:
            if key and key not in exact_map:
                exact_map[key] = source

    return exact_map, sources


def match_epg(item, exact_map, all_sources):
    possible_names = [
        clean_channel_name(item),
        clean_title(item.get("title", "")),
    ]

    possible_names.extend(stream_based_names(item))

    raw_title = item.get("raw_title", "")

    if raw_title:
        possible_names.append(raw_title.split(" - ")[0])

    possible_keys = []

    for name in possible_names:
        key = normalize(name)

        if key and key not in possible_keys:
            possible_keys.append(key)

    # Exact matches first.
    for key in possible_keys:
        if key in exact_map:
            return exact_map[key]

    # Then conservative fuzzy matching.
    best_match = None
    best_score = 0.0

    for key in possible_keys:
        for source in all_sources:
            for source_key in source["keys"]:
                score = SequenceMatcher(
                    None,
                    key,
                    source_key,
                ).ratio()

                if score > best_score:
                    best_score = score
                    best_match = source

    if best_match and best_score >= 0.88:
        return best_match

    return {}


def enrich_item(item, exact_map, all_sources):
    epg = match_epg(item, exact_map, all_sources)

    fallback_name = clean_channel_name(item)

    return {
        "name": epg.get("name") or fallback_name,
        "tvg_id": epg.get("tvg_id", ""),
        "logo": (
            epg.get("logo")
            or item.get("thumbnail", "")
        ),
        "jet_group": (
            epg.get("group")
            or item.get("group")
            or item.get("category")
            or "Other"
        ),
        "domain": (
            item.get("domain1")
            or item.get("domain")
            or "Unknown"
        ),
        "stream": item.get("stream", "").strip(),
        "source": item,
    }


def write_m3u(enriched_items, file_path, group_mode="jet"):
    lines = ["#EXTM3U"]
    seen_streams = set()

    for entry in enriched_items:
        stream = entry["stream"]

        if not stream or stream in seen_streams:
            continue

        seen_streams.add(stream)

        if group_mode == "domain":
            group = entry["domain"]
        else:
            group = entry["jet_group"]

        name = escape_attribute(entry["name"])
        tvg_id = escape_attribute(entry["tvg_id"])
        logo = escape_attribute(entry["logo"])
        group = escape_attribute(group)

        lines.append(
            f'#EXTINF:-1 '
            f'tvg-id="{tvg_id}" '
            f'tvg-name="{name}" '
            f'tvg-logo="{logo}" '
            f'group-title="{group}",'
            f'{entry["name"]}'
        )

        lines.append(stream)

    file_path.write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )


def channel_key(entry):
    if entry["tvg_id"]:
        return f'id:{entry["tvg_id"].lower()}'

    return f'name:{normalize(entry["name"])}'


def build_priority_playlist(enriched_items):
    provider_rank = {
        provider: rank
        for rank, provider in enumerate(PROVIDER_PRIORITY)
    }

    selected = {}

    for entry in enriched_items:
        provider = entry["domain"]

        if provider not in provider_rank:
            continue

        key = channel_key(entry)

        if not key or key == "name:":
            continue

        existing = selected.get(key)

        if existing is None:
            selected[key] = entry
            continue

        existing_rank = provider_rank.get(
            existing["domain"],
            9999,
        )

        new_rank = provider_rank.get(
            provider,
            9999,
        )

        if new_rank < existing_rank:
            selected[key] = entry

    return sorted(
        selected.values(),
        key=lambda entry: (
            entry["jet_group"].lower(),
            entry["name"].lower(),
        ),
    )


def write_provider_report(domain_map):
    old_notes = {}

    notes_file = Path("provider-notes.json")

    if notes_file.exists():
        try:
            old_notes = json.loads(
                notes_file.read_text(encoding="utf-8")
            )
        except Exception:
            old_notes = {}

    report_lines = [
        "# Provider Testing Report",
        "",
        (
            "Update `provider-notes.json` after testing. "
            "The generated report preserves those results."
        ),
        "",
        "| Channels | Provider | Playlist | Tested | Status | Notes |",
        "|---:|---|---|:---:|---|---|",
    ]

    for domain, entries in sorted(
        domain_map.items(),
        key=lambda pair: (-len(pair[1]), pair[0].lower()),
    ):
        filename = safe_filename(domain) + ".m3u"

        if len(entries) >= MIN_PROVIDER_CHANNELS:
            playlist_link = f"[Open](playlists/by-provider/{filename})"
        else:
            playlist_link = "Not generated"

        note = old_notes.get(domain, {})

        tested = "✅" if note.get("tested") else "⬜"
        status = note.get("status", "")
        comments = note.get("notes", "")

        report_lines.append(
            f"| {len(entries)} | `{domain}` | "
            f"{playlist_link} | {tested} | "
            f"{status} | {comments} |"
        )

    Path("provider-report.md").write_text(
        "\n".join(report_lines) + "\n",
        encoding="utf-8",
    )

    if not notes_file.exists():
        notes_file.write_text("{}\n", encoding="utf-8")


def remove_old_provider_playlists():
    for path in PROVIDER_DIR.glob("*.m3u"):
        path.unlink()


NOTES_FILE = Path("provider-notes.json")


def load_provider_notes():
    if not NOTES_FILE.exists():
        return {}

    try:
        return json.loads(
            NOTES_FILE.read_text(encoding="utf-8")
        )
    except Exception as error:
        print(f"Warning: provider-notes.json could not be read: {error}")
        return {}


def provider_is_allowed(domain, provider_notes):
    note = provider_notes.get(domain, {})
    status = str(note.get("status", "untested")).lower()

    return status != "dead"

def main():
    print("Downloading source playlist...")
    source_items = load_eplaylist()

    print(f"Source entries: {len(source_items)}")

    print("Loading EPG matching data...")
    exact_map, all_sources = build_match_sources()

    enriched_items = [
        enrich_item(item, exact_map, all_sources)
        for item in source_items
        if item.get("stream", "").strip()
    ]

    domain_map = defaultdict(list)

    for entry in enriched_items:
        domain_map[entry["domain"]].append(entry)

    print(f"Unique domains: {len(domain_map)}")

    remove_old_provider_playlists()

    generated_provider_count = 0

    for domain, entries in domain_map.items():
        if len(entries) < MIN_PROVIDER_CHANNELS:
            continue

        filename = safe_filename(domain) + ".m3u"

        write_m3u(
            sorted(
                entries,
                key=lambda entry: entry["name"].lower(),
            ),
            PROVIDER_DIR / filename,
            group_mode="jet",
        )

        generated_provider_count += 1

    print(
        f"Provider playlists generated: "
        f"{generated_provider_count}"
    )

    # Every stream, grouped like Jet Guide.
    write_m3u(
        sorted(
            enriched_items,
            key=lambda entry: (
                entry["jet_group"].lower(),
                entry["name"].lower(),
            ),
        ),
        OUTPUT_DIR / "Jet_Groups.m3u",
        group_mode="jet",
    )

    # Every stream, grouped by provider/domain.
    write_m3u(
        sorted(
            enriched_items,
            key=lambda entry: (
                entry["domain"].lower(),
                entry["name"].lower(),
            ),
        ),
        OUTPUT_DIR / "Domain_Groups.m3u",
        group_mode="domain",
    )

    # Every source entry.
    write_m3u(
        enriched_items,
        OUTPUT_DIR / "Everything.m3u",
        group_mode="jet",
    )

    selected_entries = [
        entry
        for entry in enriched_items
        if entry["domain"] in PROVIDER_PRIORITY
    ]

    write_m3u(
        sorted(
            selected_entries,
            key=lambda entry: (
                entry["jet_group"].lower(),
                entry["name"].lower(),
            ),
        ),
        OUTPUT_DIR / "Combined_Selected.m3u",
        group_mode="jet",
    )

    priority_entries = build_priority_playlist(
        enriched_items
    )

    write_m3u(
        priority_entries,
        OUTPUT_DIR / "Priority_Clean.m3u",
        group_mode="jet",
    )

    write_provider_report(domain_map)

    matched_count = sum(
        1 for entry in enriched_items if entry["tvg_id"]
    )

    print("")
    print("Update complete")
    print(f"Usable stream entries: {len(enriched_items)}")
    print(f"EPG-matched entries: {matched_count}")
    print(
        f"Unmatched entries: "
        f"{len(enriched_items) - matched_count}"
    )
    print(
        f"Priority playlist entries: "
        f"{len(priority_entries)}"
    )


if __name__ == "__main__":
    main()
