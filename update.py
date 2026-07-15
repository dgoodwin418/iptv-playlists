import json
import re
import urllib.request
import xml.etree.ElementTree as ET

from collections import defaultdict
from datetime import datetime, timezone
from difflib import SequenceMatcher
from html import unescape
from pathlib import Path


# ---------------------------------------------------------------------
# Remote sources
# ---------------------------------------------------------------------

EPLAYLIST_URL = (
    "https://magnetic.website/"
    "MAD_TITAN_SPORTS/Keep_m3u_json/eplaylist.json"
)

EPG_URL = "https://magnetic.website/jet/epg/merged_epg.xml"


# ---------------------------------------------------------------------
# Local files and output folders
# ---------------------------------------------------------------------

CACHE_FILE = Path("m3u8_cache.json")
NOTES_FILE = Path("provider-notes.json")

OUTPUT_DIR = Path("playlists")
PROVIDER_DIR = OUTPUT_DIR / "by-provider"
REPORTS_DIR = Path("reports")

OUTPUT_DIR.mkdir(exist_ok=True)
PROVIDER_DIR.mkdir(parents=True, exist_ok=True)
REPORTS_DIR.mkdir(exist_ok=True)


# ---------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------

# Providers with fewer than this number of streams remain in the
# provider reports, but do not receive an individual provider playlist.
MIN_PROVIDER_CHANNELS = 5

# These providers are used for Combined_Selected.m3u and
# Priority_Clean.m3u.
#
# You can edit and reorder this list later after more provider testing.
PROVIDER_PRIORITY = [
    "FREE3",
    "s.rocketdns.info:8080",
    "technologycloud.eu:80",
    "mainstreams.pro",
    "blog.xyzstreams.shop",
]

VALID_STATUSES = {
    "working",
    "partial",
    "untested",
    "dead",
}

STATUS_ORDER = {
    "working": 0,
    "partial": 1,
    "untested": 2,
    "dead": 3,
}


# ---------------------------------------------------------------------
# Download helper
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

    with urllib.request.urlopen(
        request,
        timeout=120,
    ) as response:
        return response.read()


# ---------------------------------------------------------------------
# Text-cleaning helpers
# ---------------------------------------------------------------------

def clean_title(text):
    text = re.sub(
        r"\[/?[A-Z0-9]+[^\]]*\]",
        "",
        text or "",
    )

    text = unescape(text)
    text = re.sub(r"\s+", " ", text).strip()

    return text or "Unknown"


def clean_channel_name(item):
    title = clean_title(
        item.get("title", "")
    )

    provider_values = [
        item.get("domain1", ""),
        item.get("domain", ""),
    ]

    for provider_text in provider_values:
        if provider_text:
            title = title.replace(
                provider_text,
                "",
            )

    title = re.sub(
        r"\s+",
        " ",
        title,
    ).strip()

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

    return re.sub(
        r"[^a-z0-9]+",
        "",
        text,
    )


def safe_filename(name):
    name = re.sub(
        r"[^A-Za-z0-9._-]+",
        "_",
        name,
    )

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
# Provider status and notes
# ---------------------------------------------------------------------

def load_provider_notes():
    if not NOTES_FILE.exists():
        return {}

    try:
        data = json.loads(
            NOTES_FILE.read_text(
                encoding="utf-8"
            )
        )

        if not isinstance(data, dict):
            print(
                "Warning: provider-notes.json must "
                "contain a JSON object."
            )
            return {}

        return data

    except Exception as error:
        print(
            "Warning: provider-notes.json could "
            f"not be read: {error}"
        )
        return {}


def provider_status(
    domain,
    provider_notes,
):
    note = provider_notes.get(
        domain,
        {},
    )

    status = str(
        note.get(
            "status",
            "untested",
        )
    ).strip().lower()

    if status not in VALID_STATUSES:
        return "untested"

    return status


def provider_is_allowed(
    domain,
    provider_notes,
):
    return provider_status(
        domain,
        provider_notes,
    ) != "dead"


def update_provider_notes(
    domain_map,
    existing_notes,
):
    """
    Adds every current provider to provider-notes.json while preserving
    statuses, tested values and notes already entered by the user.

    Providers are sorted by:
      1. Working
      2. Partial
      3. Untested
      4. Dead

    Within each status, providers with the most channels appear first.
    """

    providers = list(
        domain_map.keys()
    )

    def provider_sort_key(domain):
        status = provider_status(
            domain,
            existing_notes,
        )

        channel_count = len(
            domain_map.get(
                domain,
                [],
            )
        )

        return (
            STATUS_ORDER.get(
                status,
                2,
            ),
            -channel_count,
            domain.lower(),
        )

    providers.sort(
        key=provider_sort_key
    )

    updated_notes = {}

    for domain in providers:
        existing = existing_notes.get(
            domain,
            {},
        )

        status = str(
            existing.get(
                "status",
                "untested",
            )
        ).strip().lower()

        if status not in VALID_STATUSES:
            status = "untested"

        updated_notes[domain] = {
            "tested": bool(
                existing.get(
                    "tested",
                    False,
                )
            ),
            "status": status,
            "notes": str(
                existing.get(
                    "notes",
                    "",
                )
            ),
        }

    NOTES_FILE.write_text(
        json.dumps(
            updated_notes,
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    return updated_notes


# ---------------------------------------------------------------------
# Load source playlist
# ---------------------------------------------------------------------

def load_eplaylist():
    data = json.loads(
        fetch(
            EPLAYLIST_URL
        ).decode("utf-8")
    )

    items = data.get(
        "items",
        [],
    )

    if not isinstance(
        items,
        list,
    ):
        raise ValueError(
            "The source JSON does not contain "
            "a valid items list."
        )

    return items


# ---------------------------------------------------------------------
# Load Jet Guide channel metadata
# ---------------------------------------------------------------------

def load_cache_channels():
    if not CACHE_FILE.exists():
        print(
            "Warning: m3u8_cache.json "
            "was not found."
        )
        return []

    data = json.loads(
        CACHE_FILE.read_text(
            encoding="utf-8"
        )
    )

    channels = []

    for channel in data:
        name = channel.get(
            "name",
            "",
        )

        tvg_id = channel.get(
            "tvg_id",
            "",
        )

        if not name or not tvg_id:
            continue

        keys = {
            normalize(name),
            normalize(
                channel.get(
                    "normalized_name",
                    "",
                )
            ),
        }

        url = channel.get(
            "url",
            "",
        )

        match = re.search(
            r"/CHANNEL_GUIDE/([^/?]+)\.json",
            url,
        )

        if match:
            guide_name = match.group(1)

            keys.add(
                normalize(
                    guide_name
                )
            )

            keys.add(
                normalize(
                    guide_name.replace(
                        "_",
                        " ",
                    )
                )
            )

        channels.append(
            {
                "name": name,
                "tvg_id": tvg_id,
                "logo": channel.get(
                    "logo",
                    "",
                ),
                "group": channel.get(
                    "group",
                    "",
                ),
                "keys": {
                    key
                    for key in keys
                    if key
                },
            }
        )

    return channels


# ---------------------------------------------------------------------
# Load channel information from the XMLTV EPG
# ---------------------------------------------------------------------

def load_epg_channels():
    channels = []

    try:
        root = ET.fromstring(
            fetch(
                EPG_URL
            )
        )

        for channel in root.findall(
            "channel"
        ):
            tvg_id = channel.attrib.get(
                "id",
                "",
            )

            display_names = [
                element.text.strip()
                for element
                in channel.findall(
                    "display-name"
                )
                if element.text
                and element.text.strip()
            ]

            icon_element = channel.find(
                "icon"
            )

            logo = ""

            if icon_element is not None:
                logo = (
                    icon_element.attrib.get(
                        "src",
                        "",
                    )
                )

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
            "Warning: EPG loading failed: "
            f"{error}"
        )

    return channels


# ---------------------------------------------------------------------
# Extract possible channel names from stream URLs
# ---------------------------------------------------------------------

def stream_based_names(item):
    names = []

    stream = item.get(
        "stream",
        "",
    )

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
            stream_name.replace(
                "_",
                " ",
            )
        )

        names.append(
            stream_name
        )

    return names


# ---------------------------------------------------------------------
# EPG matching
# ---------------------------------------------------------------------

def build_match_sources():
    sources = (
        load_cache_channels()
        + load_epg_channels()
    )

    exact_map = {}

    for source in sources:
        for key in source["keys"]:
            if (
                key
                and key not in exact_map
            ):
                exact_map[key] = source

    return exact_map, sources


def match_epg(
    item,
    exact_map,
    all_sources,
):
    possible_names = [
        clean_channel_name(item),
        clean_title(
            item.get(
                "title",
                "",
            )
        ),
    ]

    possible_names.extend(
        stream_based_names(item)
    )

    raw_title = item.get(
        "raw_title",
        "",
    )

    if raw_title:
        possible_names.append(
            raw_title.split(
                " - "
            )[0]
        )

    possible_keys = []

    for name in possible_names:
        key = normalize(name)

        if (
            key
            and key not in possible_keys
        ):
            possible_keys.append(key)

    # Prefer exact matches.
    for key in possible_keys:
        if key in exact_map:
            return exact_map[key]

    # Use conservative fuzzy matching when no exact match exists.
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

    if (
        best_match
        and best_score >= 0.88
    ):
        return best_match

    return {}


# ---------------------------------------------------------------------
# Add EPG, logo, group and provider metadata to streams
# ---------------------------------------------------------------------

def enrich_item(
    item,
    exact_map,
    all_sources,
):
    epg = match_epg(
        item,
        exact_map,
        all_sources,
    )

    fallback_name = (
        clean_channel_name(item)
    )

    return {
        "name": (
            epg.get("name")
            or fallback_name
        ),
        "tvg_id": epg.get(
            "tvg_id",
            "",
        ),
        "logo": (
            epg.get("logo")
            or item.get(
                "thumbnail",
                "",
            )
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
    lines = [
        "#EXTM3U"
    ]

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

        group_attribute = escape_attribute(
            group
        )

        lines.append(
            f'#EXTINF:-1 '
            f'tvg-id="{tvg_id}" '
            f'tvg-name="{name_attribute}" '
            f'tvg-logo="{logo}" '
            f'group-title="{group_attribute}",'
            f'{entry["name"]}'
        )

        lines.append(stream)

    file_path.write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------
# Filter entries by provider status
# ---------------------------------------------------------------------

def filter_entries_by_status(
    enriched_items,
    provider_notes,
    allowed_statuses,
):
    allowed_statuses = {
        str(status).strip().lower()
        for status in allowed_statuses
    }

    return [
        entry
        for entry in enriched_items
        if provider_status(
            entry["domain"],
            provider_notes,
        )
        in allowed_statuses
    ]


# ---------------------------------------------------------------------
# Channel deduplication and provider priority
# ---------------------------------------------------------------------

def channel_key(entry):
    if entry["tvg_id"]:
        return (
            "id:"
            + entry["tvg_id"].lower()
        )

    return (
        "name:"
        + normalize(
            entry["name"]
        )
    )


def build_priority_playlist(
    enriched_items,
    provider_notes,
):
    provider_rank = {
        provider: rank
        for rank, provider
        in enumerate(
            PROVIDER_PRIORITY
        )
    }

    selected = {}

    for entry in enriched_items:
        provider = entry["domain"]

        if provider not in provider_rank:
            continue

        if not provider_is_allowed(
            provider,
            provider_notes,
        ):
            continue

        key = channel_key(entry)

        if (
            not key
            or key == "name:"
        ):
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
# Report helpers
# ---------------------------------------------------------------------

def get_status_counts(
    domain_map,
    provider_notes,
):
    counts = {
        "working": 0,
        "partial": 0,
        "untested": 0,
        "dead": 0,
    }

    for domain in domain_map:
        status = provider_status(
            domain,
            provider_notes,
        )

        counts[status] += 1

    return counts


def write_provider_report(
    domain_map,
    provider_notes,
    generated_provider_count,
):
    status_counts = get_status_counts(
        domain_map,
        provider_notes,
    )

    updated_time = datetime.now(
        timezone.utc
    ).strftime(
        "%Y-%m-%d %H:%M UTC"
    )

    lines = [
        "# Provider Testing Report",
        "",
        f"**Last updated:** {updated_time}",
        "",
        f"- Providers found: **{len(domain_map)}**",
        (
            "- Provider playlists generated: "
            f"**{generated_provider_count}**"
        ),
        (
            "- Working: "
            f"**{status_counts['working']}**"
        ),
        (
            "- Partial: "
            f"**{status_counts['partial']}**"
        ),
        (
            "- Untested: "
            f"**{status_counts['untested']}**"
        ),
        (
            "- Dead: "
            f"**{status_counts['dead']}**"
        ),
        "",
        (
            "Edit `provider-notes.json` after "
            "testing each provider."
        ),
        "",
        (
            "| Channels | Provider | Playlist | "
            "Tested | Status | Notes |"
        ),
        "|---:|---|---|:---:|---|---|",
    ]

    sorted_domains = sorted(
        domain_map,
        key=lambda domain: (
            STATUS_ORDER.get(
                provider_status(
                    domain,
                    provider_notes,
                ),
                2,
            ),
            -len(
                domain_map[domain]
            ),
            domain.lower(),
        ),
    )

    for domain in sorted_domains:
        entries = domain_map[domain]

        filename = (
            safe_filename(domain)
            + ".m3u"
        )

        if (
            len(entries)
            >= MIN_PROVIDER_CHANNELS
        ):
            playlist_link = (
                "[Open]"
                f"(playlists/by-provider/{filename})"
            )
        else:
            playlist_link = (
                "Not generated"
            )

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
            note.get(
                "notes",
                "",
            )
        ).replace(
            "|",
            "/",
        )

        lines.append(
            f"| {len(entries)} | "
            f"`{domain}` | "
            f"{playlist_link} | "
            f"{tested} | "
            f"{status} | "
            f"{comments} |"
        )

    Path(
        "provider-report.md"
    ).write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )


def write_status_report(
    filename,
    title,
    status,
    domain_map,
    provider_notes,
):
    lines = [
        f"# {title}",
        "",
        "| Channels | Provider | Notes |",
        "|---:|---|---|",
    ]

    matching_domains = [
        domain
        for domain in domain_map
        if provider_status(
            domain,
            provider_notes,
        )
        == status
    ]

    matching_domains.sort(
        key=lambda domain: (
            -len(
                domain_map[domain]
            ),
            domain.lower(),
        )
    )

    for domain in matching_domains:
        note = provider_notes.get(
            domain,
            {},
        )

        comments = str(
            note.get(
                "notes",
                "",
            )
        ).replace(
            "|",
            "/",
        )

        lines.append(
            f"| {len(domain_map[domain])} | "
            f"`{domain}` | "
            f"{comments} |"
        )

    (
        REPORTS_DIR
        / filename
    ).write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )


def write_epg_report(
    enriched_items,
):
    matched = [
        entry
        for entry in enriched_items
        if entry["tvg_id"]
    ]

    unmatched = [
        entry
        for entry in enriched_items
        if not entry["tvg_id"]
    ]

    lines = [
        "# EPG Matching Report",
        "",
        (
            "- Total streams: "
            f"**{len(enriched_items)}**"
        ),
        (
            "- Matched: "
            f"**{len(matched)}**"
        ),
        (
            "- Unmatched: "
            f"**{len(unmatched)}**"
        ),
        "",
        "## Unmatched channels",
        "",
        "| Channel | Provider |",
        "|---|---|",
    ]

    for entry in sorted(
        unmatched,
        key=lambda item: (
            item["name"].lower(),
            item["domain"].lower(),
        ),
    ):
        lines.append(
            f"| {entry['name']} | "
            f"`{entry['domain']}` |"
        )

    (
        REPORTS_DIR
        / "epg-report.md"
    ).write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------
# Remove old individual provider playlists
# ---------------------------------------------------------------------

def remove_old_provider_playlists():
    for path in PROVIDER_DIR.glob(
        "*.m3u"
    ):
        path.unlink()


# ---------------------------------------------------------------------
# Main program
# ---------------------------------------------------------------------

def main():
    print(
        "Downloading source playlist..."
    )

    source_items = load_eplaylist()

    print(
        f"Source entries: "
        f"{len(source_items)}"
    )

    print(
        "Loading EPG matching data..."
    )

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
        if item.get(
            "stream",
            "",
        ).strip()
    ]

    domain_map = defaultdict(list)

    for entry in enriched_items:
        domain_map[
            entry["domain"]
        ].append(entry)

    print(
        f"Unique domains: "
        f"{len(domain_map)}"
    )

    existing_notes = (
        load_provider_notes()
    )

    provider_notes = (
        update_provider_notes(
            domain_map,
            existing_notes,
        )
    )

    remove_old_provider_playlists()

    generated_provider_count = 0

    # -------------------------------------------------------------
    # Create one playlist for each provider with enough streams
    # -------------------------------------------------------------

    for domain, entries in domain_map.items():
        if (
            len(entries)
            < MIN_PROVIDER_CHANNELS
        ):
            continue

        filename = (
            safe_filename(domain)
            + ".m3u"
        )

        write_m3u(
            sorted(
                entries,
                key=lambda entry: (
                    entry["name"].lower()
                ),
            ),
            PROVIDER_DIR / filename,
            group_mode="jet",
        )

        generated_provider_count += 1

    print(
        "Provider playlists generated: "
        f"{generated_provider_count}"
    )

    # -------------------------------------------------------------
    # Complete source playlists
    # -------------------------------------------------------------

    # Every stream, using Jet Guide-style groups.
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

    # Every usable stream in source order.
    write_m3u(
        enriched_items,
        OUTPUT_DIR / "Everything.m3u",
        group_mode="jet",
    )

    # -------------------------------------------------------------
    # Verified playlists based on provider-notes.json
    # -------------------------------------------------------------

    working_entries = (
        filter_entries_by_status(
            enriched_items,
            provider_notes,
            {"working"},
        )
    )

    working_and_partial_entries = (
        filter_entries_by_status(
            enriched_items,
            provider_notes,
            {
                "working",
                "partial",
            },
        )
    )

    # Only providers marked working, grouped like Jet Guide.
    write_m3u(
        sorted(
            working_entries,
            key=lambda entry: (
                entry["jet_group"].lower(),
                entry["name"].lower(),
            ),
        ),
        OUTPUT_DIR / "Verified_Working.m3u",
        group_mode="jet",
    )

    # Working and partial providers, grouped like Jet Guide.
    write_m3u(
        sorted(
            working_and_partial_entries,
            key=lambda entry: (
                entry["jet_group"].lower(),
                entry["name"].lower(),
            ),
        ),
        (
            OUTPUT_DIR
            / "Verified_Working_and_Partial.m3u"
        ),
        group_mode="jet",
    )

    # Working and partial providers, grouped by provider/domain.
    write_m3u(
        sorted(
            working_and_partial_entries,
            key=lambda entry: (
                entry["domain"].lower(),
                entry["name"].lower(),
            ),
        ),
        OUTPUT_DIR / "Verified_By_Domain.m3u",
        group_mode="domain",
    )

    # -------------------------------------------------------------
    # Existing selected and priority playlists
    # -------------------------------------------------------------

    selected_entries = [
        entry
        for entry in enriched_items
        if (
            entry["domain"]
            in PROVIDER_PRIORITY
            and provider_is_allowed(
                entry["domain"],
                provider_notes,
            )
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

    priority_entries = (
        build_priority_playlist(
            enriched_items,
            provider_notes,
        )
    )

    write_m3u(
        priority_entries,
        OUTPUT_DIR / "Priority_Clean.m3u",
        group_mode="jet",
    )

    # -------------------------------------------------------------
    # Reports
    # -------------------------------------------------------------

    write_provider_report(
        domain_map,
        provider_notes,
        generated_provider_count,
    )

    write_status_report(
        "working-providers.md",
        "Working Providers",
        "working",
        domain_map,
        provider_notes,
    )

    write_status_report(
        "partial-providers.md",
        "Partially Working Providers",
        "partial",
        domain_map,
        provider_notes,
    )

    write_status_report(
        "untested-providers.md",
        "Untested Providers",
        "untested",
        domain_map,
        provider_notes,
    )

    write_status_report(
        "dead-providers.md",
        "Dead Providers",
        "dead",
        domain_map,
        provider_notes,
    )

    write_epg_report(
        enriched_items
    )

    # -------------------------------------------------------------
    # Action log summary
    # -------------------------------------------------------------

    matched_count = sum(
        1
        for entry in enriched_items
        if entry["tvg_id"]
    )

    status_counts = get_status_counts(
        domain_map,
        provider_notes,
    )

    print("")
    print("Update complete")

    print(
        f"Usable streams: "
        f"{len(enriched_items)}"
    )

    print(
        f"EPG matched: "
        f"{matched_count}"
    )

    print(
        f"EPG unmatched: "
        f"{len(enriched_items) - matched_count}"
    )

    print(
        f"Verified working streams: "
        f"{len(working_entries)}"
    )

    print(
        "Verified working/partial streams: "
        f"{len(working_and_partial_entries)}"
    )

    print(
        f"Priority playlist entries: "
        f"{len(priority_entries)}"
    )

    print(
        f"Working providers: "
        f"{status_counts['working']}"
    )

    print(
        f"Partial providers: "
        f"{status_counts['partial']}"
    )

    print(
        f"Untested providers: "
        f"{status_counts['untested']}"
    )

    print(
        f"Dead providers: "
        f"{status_counts['dead']}"
    )


if __name__ == "__main__":
    main()
