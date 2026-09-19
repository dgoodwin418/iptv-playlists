import json
import os
import re
import shutil
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

CONFIG_FILE = Path("sources.json")
OUTPUT_DIR = Path("playlists")
BY_SOURCE_DIR = OUTPUT_DIR / "by-source"
BY_PROVIDER_DIR = OUTPUT_DIR / "by-provider"
REPORTS_DIR = Path("reports")
NOTES_FILE = Path("provider-notes.json")
HEALTH_FILE = Path("provider-health.json")

VALID_STATUSES = {"working", "partial", "untested", "dead"}
STATUS_SCORE = {"working": 3, "partial": 2, "untested": 1, "dead": 0}
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36"
TIMEOUT = 30

for p in (OUTPUT_DIR, BY_SOURCE_DIR, BY_PROVIDER_DIR, REPORTS_DIR):
    p.mkdir(parents=True, exist_ok=True)


def fetch_text(url, timeout=TIMEOUT):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    for enc in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    return raw.decode("utf-8", "replace")


def normalize(text):
    text = (text or "").lower().replace("&", "and")
    text = re.sub(r"\b(hd|sd|uhd|fhd|east|west|channel|network|television|tv|us|usa|feed|stream)\b", "", text)
    return re.sub(r"[^a-z0-9]+", "", text)


def esc(value):
    return (str(value or "").replace("&", "&amp;").replace('"', "&quot;")
            .replace("<", "&lt;").replace(">", "&gt;"))


def safe_filename(value):
    out = re.sub(r"[^A-Za-z0-9._-]+", "_", value or "").strip("._-")
    return out or "unknown"


def provider_from_url(url):
    base = (url or "").split("|", 1)[0].strip()
    try:
        p = urllib.parse.urlsplit(base)
        return p.netloc or p.scheme or "Unknown"
    except Exception:
        return "Unknown"


def attrs_from_extinf(line):
    attrs = {}
    for key, value in re.findall(r'([\w-]+)="([^"]*)"', line):
        attrs[key.lower()] = value
    name = line.split(",", 1)[1].strip() if "," in line else "Unknown"
    return attrs, name


def parse_m3u(text, source):
    entries = []
    pending = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXTINF"):
            attrs, name = attrs_from_extinf(line)
            pending = {
                "name": name or attrs.get("tvg-name") or "Unknown",
                "logo": attrs.get("tvg-logo", ""),
                "group": attrs.get("group-title", "Other") or "Other",
                "tvg_id": attrs.get("tvg-id", ""),
                "tvg_name": attrs.get("tvg-name", "") or name,
            }
            continue
        if line.startswith("#"):
            continue
        if pending is None:
            pending = {"name": line, "logo": "", "group": "Other", "tvg_id": "", "tvg_name": line}
        url = line
        item = dict(pending)
        item.update({
            "url": url,
            "source_id": source["id"],
            "source_name": source.get("name", source["id"]),
            "source_priority": int(source.get("priority", 9999)),
            "provider": provider_from_url(url),
        })
        entries.append(item)
        pending = None
    return entries


def load_config():
    if not CONFIG_FILE.exists():
        raise FileNotFoundError("sources.json is missing")
    cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    if not isinstance(cfg, dict):
        raise ValueError("sources.json must contain a JSON object")
    return cfg


def configured_playlist_sources(cfg):
    sources = [s for s in cfg.get("playlist_sources", []) if s.get("enabled", True) and s.get("url")]
    # Optional private/candidate feeds can be kept in GitHub Secrets rather than committed.
    for env_name, sid, label, priority in (
        ("IPTV_PRIVATE_SOURCE_107", "private-107", "Private source 107", 30),
        ("IPTV_PRIVATE_SOURCE_109", "private-109", "Private source 109", 40),
    ):
        url = os.environ.get(env_name, "").strip()
        if url:
            sources.append({"id": sid, "name": label, "url": url, "enabled": True, "priority": priority})
    return sources


def load_channels(cfg):
    entries = []
    statuses = []
    for src in configured_playlist_sources(cfg):
        try:
            text = fetch_text(src["url"])
            if "#EXTM3U" not in text[:10000]:
                raise ValueError("response is not an M3U playlist")
            vals = parse_m3u(text, src)
            entries.extend(vals)
            statuses.append({"id": src["id"], "name": src.get("name", src["id"]), "ok": True, "channels": len(vals), "error": ""})
            print(f"Loaded {len(vals):,} channels from {src.get('name', src['id'])}")
        except Exception as exc:
            statuses.append({"id": src["id"], "name": src.get("name", src["id"]), "ok": False, "channels": 0, "error": str(exc)[:300]})
            print(f"WARNING: skipped {src.get('name', src['id'])}: {exc}")
    if not entries:
        raise RuntimeError("All configured playlist sources failed; refusing to replace tv.m3u with an empty playlist")
    return entries, statuses


def load_json(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def effective_status(provider, notes, health):
    note = notes.get(provider, {}) if isinstance(notes.get(provider, {}), dict) else {}
    manual = str(note.get("manual_status", "")).strip().lower()
    if manual in VALID_STATUSES and manual != "untested":
        return manual
    old = str(note.get("status", "")).strip().lower()
    if note.get("tested") and old in VALID_STATUSES and old != "untested":
        return old
    auto = str((health.get(provider, {}) or {}).get("auto_status", "")).strip().lower()
    if auto in VALID_STATUSES:
        return auto
    return old if old in VALID_STATUSES else "untested"


def channel_key(entry):
    tid = (entry.get("tvg_id") or "").strip().lower()
    if tid:
        return "id:" + tid
    n = normalize(entry.get("tvg_name") or entry.get("name"))
    return "name:" + n if n else "url:" + entry.get("url", "")


def choose_best(candidates, notes, health):
    def score(e):
        status = effective_status(e["provider"], notes, health)
        h = health.get(e["provider"], {}) or {}
        success = float(h.get("success_rate", 0) or 0)
        latency = float(h.get("average_latency_seconds", 9999) or 9999)
        scheme = urllib.parse.urlsplit(e["url"].split("|", 1)[0]).scheme.lower()
        playable = 1 if scheme in ("http", "https") else 0
        # Lower source priority number is better.
        return (playable, STATUS_SCORE.get(status, 1), success, -latency, -int(e.get("source_priority", 9999)))
    return max(candidates, key=score)


def dedupe(entries, notes, health):
    buckets = defaultdict(list)
    for e in entries:
        buckets[channel_key(e)].append(e)
    selected = [choose_best(v, notes, health) for v in buckets.values()]
    return sorted(selected, key=lambda e: ((e.get("group") or "").lower(), (e.get("name") or "").lower()))


def write_m3u(entries, path, group_mode="original"):
    lines = ["#EXTM3U"]
    seen = set()
    for e in entries:
        url = e["url"]
        if not url or url in seen:
            continue
        seen.add(url)
        group = e["provider"] if group_mode == "provider" else e.get("group", "Other")
        lines.append(
            f'#EXTINF:-1 tvg-id="{esc(e.get("tvg_id"))}" tvg-name="{esc(e.get("tvg_name"))}" '
            f'tvg-logo="{esc(e.get("logo"))}" group-title="{esc(group)}",{e.get("name", "Unknown")}'
        )
        lines.append(url)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def generate_secondary_playlists(entries):
    by_source, by_provider = defaultdict(list), defaultdict(list)
    for e in entries:
        by_source[e["source_id"]].append(e)
        by_provider[e["provider"]].append(e)
    for sid, vals in by_source.items():
        write_m3u(vals, BY_SOURCE_DIR / f"{safe_filename(sid)}.m3u")
    # Generate a diagnostic playlist for every provider, including one-stream providers.
    for provider, vals in by_provider.items():
        write_m3u(vals, BY_PROVIDER_DIR / (safe_filename(provider) + ".m3u"))
    return by_source, by_provider



def write_provider_report(by_provider, notes, health):
    """Generate a provider-management table with direct playlist links."""
    now = datetime.now(timezone.utc).isoformat()
    rows = []
    for provider, vals in by_provider.items():
        note = notes.get(provider, {}) if isinstance(notes.get(provider, {}), dict) else {}
        manual = str(note.get("manual_status", "")).strip().lower()
        if manual not in VALID_STATUSES or manual == "untested":
            manual = "—"

        h = health.get(provider, {}) if isinstance(health.get(provider, {}), dict) else {}
        auto = str(h.get("auto_status", "")).strip().lower()
        if auto not in VALID_STATUSES:
            auto = "untested"

        effective = effective_status(provider, notes, health)
        rate = h.get("success_rate")
        health_text = "—" if rate is None else f"{float(rate):.1f}%"
        latency = h.get("average_latency_seconds")
        latency_text = "—" if latency is None else f"{float(latency):.3f}s"

        filename = safe_filename(provider) + ".m3u"
        playlist_link = "../playlists/by-provider/" + urllib.parse.quote(filename)

        rows.append((provider, len(vals), auto, manual, effective,
                     health_text, latency_text, playlist_link))

    rows.sort(key=lambda r: (-r[1], r[0].lower()))
    lines = [
        "# Provider Management", "",
        f"Updated: **{now}**", "",
        "Use this table to review every provider currently found in the source playlists. "
        "The **View Playlist** link opens that provider's generated M3U.", "",
        "**Manual Status** comes from `provider-notes.json`. "
        "**Effective Status** is the status used when building the production playlist.", "",
        "| Provider | Streams | Auto Status | Manual Status | Effective Status | Health | Latency | Playlist |",
        "|---|---:|---|---|---|---:|---:|---|",
    ]
    for provider, count, auto, manual, effective, health_text, latency_text, link in rows:
        provider_md = provider.replace("|", r"\|")
        lines.append(
            f"| `{provider_md}` | {count} | {auto} | {manual} | {effective} | "
            f"{health_text} | {latency_text} | [View Playlist]({link}) |"
        )
    (REPORTS_DIR / "provider-table.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

def merge_remote_epg(cfg, allowed_ids):
    channels, programmes = {}, {}
    statuses = []
    for src in cfg.get("epg_sources", []):
        if not src.get("enabled", True) or not src.get("url"):
            continue
        try:
            xml_text = fetch_text(src["url"])
            root = ET.fromstring(xml_text)
            ch_count = pr_count = 0
            for ch in root.findall("channel"):
                cid = ch.attrib.get("id", "")
                if cid and (not allowed_ids or cid in allowed_ids) and cid not in channels:
                    channels[cid] = ch
                    ch_count += 1
            for pr in root.findall("programme"):
                cid = pr.attrib.get("channel", "")
                if not cid or (allowed_ids and cid not in allowed_ids):
                    continue
                key = (cid, pr.attrib.get("start", ""), pr.attrib.get("stop", ""), pr.findtext("title", ""))
                if key not in programmes:
                    programmes[key] = pr
                    pr_count += 1
            statuses.append({"id": src.get("id", "epg"), "ok": True, "channels": ch_count, "programmes": pr_count, "error": ""})
        except Exception as exc:
            statuses.append({"id": src.get("id", "epg"), "ok": False, "channels": 0, "programmes": 0, "error": str(exc)[:300]})
            print(f"WARNING: EPG source {src.get('name', src.get('id', 'epg'))} failed: {exc}")
    if channels or programmes:
        out = ET.Element("tv")
        for cid in sorted(channels):
            out.append(channels[cid])
        for key in sorted(programmes, key=lambda k: (k[1], k[0], k[3])):
            out.append(programmes[key])
        ET.ElementTree(out).write("guide.xml", encoding="utf-8", xml_declaration=True)
    elif not Path("guide.xml").exists():
        ET.ElementTree(ET.Element("tv")).write("guide.xml", encoding="utf-8", xml_declaration=True)
    return len(channels), len(programmes), statuses


def main():
    cfg = load_config()
    notes = load_json(NOTES_FILE)
    health = load_json(HEALTH_FILE)
    entries, source_statuses = load_channels(cfg)
    by_source, by_provider = generate_secondary_playlists(entries)
    write_provider_report(by_provider, notes, health)
    write_m3u(entries, OUTPUT_DIR / "All_Sources.m3u")

    selected = dedupe(entries, notes, health)
    write_m3u(selected, OUTPUT_DIR / "Clean_Deduped.m3u")

    production = [
        e for e in selected
        if urllib.parse.urlsplit(e["url"].split("|", 1)[0]).scheme.lower() in ("http", "https")
        and "radio" not in (e.get("group") or "").lower()
        and effective_status(e["provider"], notes, health) != "dead"
    ]
    if not production:
        raise RuntimeError("Filtering produced an empty playlist; refusing to overwrite tv.m3u")

    write_m3u(production, OUTPUT_DIR / "Verified_Working_and_Partial.m3u")
    shutil.copyfile(OUTPUT_DIR / "Verified_Working_and_Partial.m3u", "tv.m3u")

    allowed_ids = {e["tvg_id"] for e in production if e.get("tvg_id")}
    epg_channels, epg_programmes, epg_statuses = merge_remote_epg(cfg, allowed_ids)

    duplicate_alternatives = len(entries) - len({channel_key(e) for e in entries})
    report = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "source_records": len(entries),
        "unique_channels": len(selected),
        "production_channels": len(production),
        "duplicate_alternatives_removed": duplicate_alternatives,
        "providers": len(by_provider),
        "sources": source_statuses,
        "epg_sources": epg_statuses,
        "epg_channels": epg_channels,
        "epg_programmes": epg_programmes,
    }
    Path("dashboard-data.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    (REPORTS_DIR / "source-status.json").write_text(json.dumps(source_statuses, indent=2) + "\n", encoding="utf-8")

    md = ["# Remote Source Report", "", f"Updated: **{report['generated_utc']}**", "",
          f"- Downloaded records: **{len(entries):,}**",
          f"- Production channels: **{len(production):,}**",
          f"- Duplicate alternatives removed: **{duplicate_alternatives:,}**", "",
          "| Source | Result | Channels |", "|---|---|---:|"]
    for s in source_statuses:
        result = "OK" if s["ok"] else f"FAILED: {s['error']}"
        md.append(f"| {s['name']} | {result} | {s['channels']:,} |")
    (REPORTS_DIR / "source-report.md").write_text("\n".join(md) + "\n", encoding="utf-8")

    print(f"Production tv.m3u: {len(production):,} channels")
    print(f"guide.xml: {epg_channels:,} channels / {epg_programmes:,} programmes")


if __name__ == "__main__":
    main()
