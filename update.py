import json
import re
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict
from difflib import SequenceMatcher
from html import unescape
from pathlib import Path


# ---------------------------------------------------------------------
# Source files
# ---------------------------------------------------------------------

EPLAYLIST_URL = (
    "https://magnetic.website/"
    "MAD_TITAN_SPORTS/Keep_m3u_json/eplaylist.json"
)

EPG_URL = "https://magnetic.website/jet/epg/merged_epg.xml"

CACHE_FILE = Path("m3u8_cache.json")
NOTES_FILE = Path("provider-notes.json")


# ---------------------------------------------------------------------
# Output folders
# ---------------------------------------------------------------------

OUTPUT_DIR = Path("playlists")
PROVIDER_DIR = OUTPUT_DIR / "by-provider"

OUTPUT_DIR.mkdir(exist_ok=True)
PROVIDER_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------

# A provider must have at least this many channels before the script
# creates an individual playlist for it.
MIN_PROVIDER_CHANNELS = 5

# Provider order used in Priority_Clean.m3u.
# The first working provider that has a channel is preferred.
PROVIDER_PRIORITY = [
    "FREE3",
    "s.rocketdns.info:8080",
    "technologycloud.eu:80",
    "mainstreams.pro",
    "blog.xyzstreams.shop",
]


# ---------------------------------------------------------------------
# Download helpers
# ---------------------------------------------------------------------

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


# ---------------------------------------------------------------------
# Text-cleaning helpers
# ---------------------------------------------------------------------

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


# ---------------------------------------------------------------------
# Provider testing notes
# ---------------------------------------------------------------------

def load_provider_notes():
    if not NOTES_FILE.exists():
        return {}

    try:
        data = json.loads(
            NOTES_FILE.read_text(encoding="utf-8")
        )

        if not isinstance(data, dict):
            print(
                "Warning: provider-notes.json must contain "
                "a JSON object."
            )
            return {}

        return data

    except Exception as error:
        print(
            "Warning: provider-notes.json could not be read: "
            f"{error}"
        )
        return {}


def provider_status(domain, provider_notes):
    note = provider_notes.get(domain, {})

    status = str(
        note.get("status", "untested")
    ).strip().lower()

    valid_statuses = {
        "working",
        "partial",
        "dead",
        "untested",
    }

    if status not in valid_statuses:
        return "untested"

    return status


def provider_is_allowed(domain, provider_notes):
    return provider_status(
        domain,
        provider_notes,
    ) != "dead"


# ---------------------------------------------------------------------
# Load source playlist
# ---------------------------------------------------------------------

def load_eplaylist():
    data = json.loads(
        fetch(EPLAYLIST_URL).decode("utf-8")
    )

    return data.get("items", [])


# ---------------------------------------------------------------------
# Load Jet Guide channel metadata
# ---------------------------------------------------------------------

def load_cache_channels():
    if not CACHE_FILE.exists():
        print("Warning: m3u8_cache.json was not found.")
        return []

    data = json.loads(
        CACHE_FILE.read_text(encoding="utf-8")
    )

    channels = []

    for channel in data:
        name = channel.get("name", "")
        tvg_id = channel.get("tvg_id", "")

        if not name or not tvg_id:
            continue

        keys = {
            normalize(name),
            normalize(
                channel.get("normalized_name", "")
            ),
        }

        url = channel.get("url", "")

        match = re.search(
            r"/CHANNEL_GUIDE/([^/?]+)\.json",
            url,
        )

        if match:
            guide_name = match.group(1)

            keys.add(normalize(guide_name))
            keys.add(
                normalize(
                    guide_name.replace("_", " ")
                )
            )

        channels.append(
            {
                "name": name,
                "tvg_id": tvg_id,
                "logo": channel.get("logo", ""),
                "group": channel.get("group", ""),
                "keys": {
                    key for key in keys if key
                },
            }
        )

    return channels


# ---------------------------------------------------------------------
# Load channel IDs directly from the XMLTV guide
# ---------------------------------------------------------------------

def load_epg_channels():
    channels = []

    try:
        root = ET.fromstring(fetch(EPG_URL))

        for channel in root.findall("channel"):
            tvg_id = channel.attrib.get("id", "")

            display_names = [
                element.text.strip()
                for element in channel.findall(
                    "display-name"
                )
                if element.text
                and element.text.strip()
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
                        "keys": {
                            normalize(name)
                        },
                    }
                )

    except Exception as error:
        print(
            "Warning: EPG channel loading failed: "
            f"{error}"
        )

    return channels


# ---------------------------------------------------------------------
# Extract useful channel names from stream URLs
# ---------------------------------------------------------------------

def stream_based_names(item):
    names = []
    stream = item.get("stream", "")

    # Example:
    # http://23.237.104.106:8080/USA_CMT/index.m3u8
    match = re.search(
        r"/USA_([^/]+)/",
        stream,
        re.IGNORECASE,
    )

    if match:
        stream_name = match.group(1)

        names.append(
            stream_name.replace("_", " ")
        )

        names.append(stream_name)

    return names


# ---------------------------------------------------------------------
# Build EPG lookup tables
# ---------------------------------------------------------------------

def build_match_sources():
    sources = (
        load_cache_channels()
        + load_epg_channels()
    )

    exact_map = {}

    for source in sources:
        for key in source["keys"]:
            if key and key not in exact_map:
                exact_map[key] = source

    return exact_map, sources


# ---------------------------------------------------------------------
# Match a stream entry to an EPG channel
# ---------------------------------------------------------------------

def match_epg(item, exact_map, all_sources):
    possible_names = [
        clean_channel_name(item),
        clean_title(item.get("title", "")),
    ]

    possible_names.extend(
        stream_based_names(item)
    )

    raw_title = item.get("raw_title", "")

    if raw_title:
        possible_names.append(
            raw_title.split(" - ")[0]
        )

    possible_keys = []

    for name in possible_names:
        key = normalize(name)

        if key and key not in possible_keys:
            possible_keys.append(key)

    # Exact matches are preferred.
    for key in possible_keys:
        if key in exact_map:
            return exact_map[key]

    # Conservative fuzzy matching.
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


# ---------------------------------------------------------------------
# Add EPG, logo, group and domain information to each stream
# ---------------------------------------------------------------------

def enrich_item(item, exact_map, all_sources):
    epg = match_epg(
        item,
        exact_map,
        all_sources,
    )

    fallback_name = clean_channel_name(item)

    return {
        "name": (
            epg.get("name")
            or fallback_name
        ),
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
        "stream": item.get(
            "stream",
            "",
        ).strip(),
        "source": item,
    }


# ---------------------------------------------------------------------
# Write an M3U playlist
# ---------------------------------------------------------------------

def write_m3u(
    enriched_items,
    file_path,
    group_mode="jet",
):
    lines = ["#EXTM3U"]
    seen_streams = set()

    for entry in enriched_items:
        stream = entry["stream"]

        if not stream:
            continue

        if stream in seen_streams:
            continue

        seen_streams.add(stream)

        if group_mode == "domain":
            group = entry["domain"]
        else:
            group = entry["jet_group"]

        name_attribute = escape_attribute(
            entry["name"]
        )

        tvg_id = escape_attribute(
            entry["tvg_id"]
        )

        logo = escape_attribute(
            entry["logo"]
        )

        group = escape_attribute(group)

        lines.append(
            f'#EXTINF:-1 '
            f'tvg-id="{tvg_id}" '
            f'tvg-name="{name_attribute}" '
            f'tvg-logo="{logo}" '
            f'group-title="{group}",'
            f'{entry["name"]}'
        )

        lines.append(stream)

    file_path.write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------
# Determine whether two streams represent the same channel
# ---------------------------------------------------------------------

def channel_key(entry):
    if entry["tvg_id"]:
        return (
            "id:"
            + entry["tvg_id"].lower()
        )

    return (
        "name:"
        + normalize(entry["name"])
    )


# ---------------------------------------------------------------------
# Build provider-priority playlist
# ---------------------------------------------------------------------

def build_priority_playlist(
    enriched_items,
    provider_notes,
):
    provider_rank = {
        provider: rank
        for rank, provider
        in enumerate(PROVIDER_PRIORITY)
    }

    selected = {}

    for entry in enriched_items:
        provider = entry["domain"]

        if provider not in provider_rank:
            continue

        # Providers marked dead are excluded.
        if not provider_is_allowed(
            provider,
            provider_notes,
        ):
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


# ---------------------------------------------------------------------
# Generate provider test report
# ---------------------------------------------------------------------

def write_provider_report(
    domain_map,
    provider_notes,
):
    report_lines = [
        "# Provider Testing Report",
        "",
        (
            "Edit `provider-notes.json` after "
            "testing each provider in Kodi."
        ),
        "",
        (
            "| Channels | Provider | Playlist | "
            "Tested | Status | Notes |"
        ),
        (
            "|---:|---|---|:---:|---|---|"
        ),
    ]

    for domain, entries in sorted(
        domain_map.items(),
        key=lambda pair: (
            -len(pair[1]),
            pair[0].lower(),
        ),
    ):
        filename = (
            safe_filename(domain)
            + ".m3u"
        )

        if len(entries) >= MIN_PROVIDER_CHANNELS:
            playlist_link = (
                "[Open]"
                f"(playlists/by-provider/{filename})"
            )
        else:
            playlist_link = "Not generated"

        note = provider_notes.get(
            domain,
            {},
        )

        tested = (
            "✅"
            if note.get("tested")
            else "⬜"
        )

        status = provider_status(
            domain,
            provider_notes,
        )

        comments = str(
            note.get("notes", "")
        ).replace("|", "/")

        report_lines.append(
            f"| {len(entries)} | "
            f"`{domain}` | "
            f"{playlist_link} | "
            f"{tested} | "
            f"{status} | "
            f"{comments} |"
        )

    Path("provider-report.md").write_text(
        "\n".join(report_lines) + "\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------
# Remove old provider playlists
# ---------------------------------------------------------------------

def remove_old_provider_playlists():
    for path in PROVIDER_DIR.glob("*.m3u"):
        path.unlink()


# ---------------------------------------------------------------------
# Main program
# ---------------------------------------------------------------------

def main():
    print("Downloading source playlist...")

    source_items = load_eplaylist()

    print(
        f"Source entries: {len(source_items)}"
    )

    print("Loading provider notes...")

    provider_notes = load_provider_notes()

    print("Loading EPG matching data...")

    exact_map, all_sources = (
        build_match_sources()
    )

    enriched_items = [
        enrich_item(
            item,
            exact_map,
            all_sources,
        )
        for item in source_items
        if item.get("stream", "").strip()
    ]

    domain_map = defaultdict(list)

    for entry in enriched_items:
        domain_map[
            entry["domain"]
        ].append(entry)

    print(
        f"Unique domains: {len(domain_map)}"
    )

    remove_old_provider_playlists()

    generated_provider_count = 0

    # Generate one playlist per provider.
    for domain, entries in domain_map.items():
        if len(entries) < MIN_PROVIDER_CHANNELS:
            continue

        filename = (
            safe_filename(domain)
            + ".m3u"
        )

        sorted_entries = sorted(
            entries,
            key=lambda entry: (
                entry["name"].lower()
            ),
        )

        write_m3u(
            sorted_entries,
            PROVIDER_DIR / filename,
            group_mode="jet",
        )

        generated_provider_count += 1

    print(
        "Provider playlists generated: "
        f"{generated_provider_count}"
    )

    # All streams grouped like Jet Guide.
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

    # All streams grouped by provider.
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

    # All usable streams.
    write_m3u(
        enriched_items,
        OUTPUT_DIR / "Everything.m3u",
        group_mode="jet",
    )

    # All streams from the providers listed
    # in PROVIDER_PRIORITY.
    selected_entries = [
        entry
        for entry in enriched_items
        if (
            entry["domain"]
            in PROVIDER_PRIORITY
        )
        and provider_is_allowed(
            entry["domain"],
            provider_notes,
        )
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

    # Deduplicated playlist using provider priority.
    priority_entries = build_priority_playlist(
        enriched_items,
        provider_notes,
    )

    write_m3u(
        priority_entries,
        OUTPUT_DIR / "Priority_Clean.m3u",
        group_mode="jet",
    )

    write_provider_report(
        domain_map,
        provider_notes,
    )

    matched_count = sum(
        1
        for entry in enriched_items
        if entry["tvg_id"]
    )

    dead_providers = [
        domain
        for domain in domain_map
        if provider_status(
            domain,
            provider_notes,
        ) == "dead"
    ]

    print("")
    print("Update complete")
    print(
        f"Usable stream entries: "
        f"{len(enriched_items)}"
    )
    print(
        f"EPG-matched entries: "
        f"{matched_count}"
    )
    print(
        f"Unmatched entries: "
        f"{len(enriched_items) - matched_count}"
    )
    print(
        f"Priority playlist entries: "
        f"{len(priority_entries)}"
    )
    print(
        f"Dead providers excluded: "
        f"{len(dead_providers)}"
    )


if __name__ == "__main__":
    main()
