#!/usr/bin/env python3
"""One-shot Plex library codec audit. Run on seedbox: python3 codec-audit.py"""
import json, os, urllib.request
from collections import Counter, defaultdict

PLEX = open(os.path.expanduser("~/secrets/plex.host")).read().strip()
PORT = open(os.path.expanduser("~/secrets/plex.port")).read().strip()
TOKEN = open(os.path.expanduser("~/secrets/plex.token")).read().strip()
BASE = "http://%s:%s" % (PLEX, PORT)

def plex_get(path):
    req = urllib.request.Request(BASE + path, headers={"Accept": "application/json", "X-Plex-Token": TOKEN})
    return json.load(urllib.request.urlopen(req, timeout=120))

GB = 1024**3

sections = plex_get("/library/sections")["MediaContainer"]["Directory"]
print("=== LIBRARY SECTIONS (%d) ===" % len(sections))
for s in sections:
    print("  [%s] %-25s type=%s" % (s["key"], s["title"], s["type"]))

g_codec_count = Counter()
g_codec_size = defaultdict(int)
g_total_files = 0
g_total_size = 0

for s in sections:
    if s["type"] not in ("movie", "show"):
        continue
    item_type = 1 if s["type"] == "movie" else 4
    try:
        data = plex_get("/library/sections/%s/all?type=%d" % (s["key"], item_type))
    except Exception as e:
        print("\n[skip] %s: %s" % (s["title"], e))
        continue
    items = data["MediaContainer"].get("Metadata", []) or []

    vcodec_count = Counter()
    vcodec_size = defaultdict(int)
    acodec_count = Counter()
    container_count = Counter()
    height_buckets = Counter()
    bitrate_buckets = Counter()
    total_size = 0
    total_files = 0

    for item in items:
        for media in item.get("Media", []):
            vcodec = (media.get("videoCodec") or "?").lower()
            acodec = (media.get("audioCodec") or "?").lower()
            container = (media.get("container") or "?").lower()
            height = media.get("height") or 0
            bitrate = media.get("bitrate") or 0
            if height >= 2160: hb = "4k+"
            elif height >= 1080: hb = "1080p"
            elif height >= 720:  hb = "720p"
            elif height >= 480:  hb = "480p"
            else: hb = "other"
            if bitrate >= 15000: bb = ">=15Mbps"
            elif bitrate >= 8000: bb = "8-15Mbps"
            elif bitrate >= 4000: bb = "4-8Mbps"
            elif bitrate >= 2000: bb = "2-4Mbps"
            else: bb = "<2Mbps"
            for part in media.get("Part", []):
                size = part.get("size") or 0
                total_files += 1
                total_size += size
                vcodec_count[vcodec] += 1
                vcodec_size[vcodec] += size
                acodec_count[acodec] += 1
                container_count[container] += 1
                height_buckets[hb] += 1
                bitrate_buckets[bb] += 1
                g_total_files += 1
                g_total_size += size
                g_codec_count[vcodec] += 1
                g_codec_size[vcodec] += size

    avg = (total_size / max(total_files, 1)) / GB
    print("\n=== %s (type=%s) ===" % (s["title"], s["type"]))
    print("  files: %d  total: %.1f GB  avg: %.2f GB/file" % (total_files, total_size / GB, avg))
    print("  Video codecs:")
    for c, n in vcodec_count.most_common():
        pct = 100 * vcodec_size[c] / max(total_size, 1)
        print("    %-10s %6d files  %8.1f GB  (%5.1f%% of size)" % (c, n, vcodec_size[c] / GB, pct))
    print("  Audio codecs: " + ", ".join("%s=%d" % (c, n) for c, n in acodec_count.most_common(8)))
    print("  Containers:   " + ", ".join("%s=%d" % (c, n) for c, n in container_count.most_common(6)))
    print("  Resolution:   " + ", ".join("%s=%d" % (h, n) for h, n in height_buckets.most_common()))
    print("  Bitrate:      " + ", ".join("%s=%d" % (b, n) for b, n in bitrate_buckets.most_common()))

print("\n=== GRAND TOTAL ===")
print("  files: %d  total: %.1f GB" % (g_total_files, g_total_size / GB))
print("  Codec distribution:")
for c, n in g_codec_count.most_common():
    pct = 100 * g_codec_size[c] / max(g_total_size, 1)
    print("    %-10s %6d files  %8.1f GB  (%5.1f%% of total)" % (c, n, g_codec_size[c] / GB, pct))

non_hevc_size = sum(v for c, v in g_codec_size.items() if c not in ("hevc", "h265"))
print("\n=== HEVC OPPORTUNITY ===")
print("  Non-HEVC volume: %.1f GB (%.1f%% of library)" % (non_hevc_size / GB, 100 * non_hevc_size / max(g_total_size, 1)))
print("  Projected savings at 30%% reduction: %.1f GB" % (non_hevc_size * 0.30 / GB))
print("  Projected savings at 40%% reduction: %.1f GB" % (non_hevc_size * 0.40 / GB))
print("  Projected savings at 50%% reduction: %.1f GB" % (non_hevc_size * 0.50 / GB))
