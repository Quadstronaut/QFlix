#!/usr/bin/env python3
"""Tautulli playback history audit + disk reconciliation."""
import json, os, subprocess, urllib.request, urllib.parse
from collections import Counter, defaultdict

KEY = open(os.path.expanduser("~/secrets/tautulli.key")).read().strip()
PORT = open(os.path.expanduser("~/secrets/tautulli.port")).read().strip()
BASE = "http://127.0.0.1:%s/tautulli/api/v2" % PORT

def taut(cmd, **params):
    params["apikey"] = KEY
    params["cmd"] = cmd
    url = BASE + "?" + urllib.parse.urlencode(params)
    return json.load(urllib.request.urlopen(url, timeout=60))

# Last 1000 plays
h = taut("get_history", length=1000, after="2026-04-19")
data = h.get("response", {}).get("data", {}) or {}
rows = data.get("data", []) or []
print("=== TAUTULLI HISTORY (last 30d, %d sessions) ===" % len(rows))

transcode_decision = Counter()
video_decision = Counter()
audio_decision = Counter()
sub_decision = Counter()
trans_reason_v = Counter()
trans_reason_a = Counter()
codec_played = Counter()
codec_streamed = Counter()
audio_played = Counter()
audio_streamed = Counter()
container_played = Counter()
client_platform = Counter()
client_player = Counter()
trans_size_bytes = 0
direct_size_bytes = 0

for r in rows:
    tdec = (r.get("transcode_decision") or "?").lower()
    vdec = (r.get("video_decision") or "?").lower()
    adec = (r.get("audio_decision") or "?").lower()
    sdec = (r.get("subtitle_decision") or "?").lower()
    transcode_decision[tdec] += 1
    video_decision[vdec] += 1
    audio_decision[adec] += 1
    sub_decision[sdec] += 1
    if vdec == "transcode":
        trans_reason_v[r.get("video_codec","?") + " -> " + r.get("stream_video_codec","?")] += 1
    if adec == "transcode":
        trans_reason_a[r.get("audio_codec","?") + "/" + str(r.get("audio_channels","?")) + " -> " + r.get("stream_audio_codec","?") + "/" + str(r.get("stream_audio_channels","?"))] += 1
    codec_played[(r.get("video_codec") or "?").lower()] += 1
    codec_streamed[(r.get("stream_video_codec") or "?").lower()] += 1
    audio_played[(r.get("audio_codec") or "?").lower()] += 1
    audio_streamed[(r.get("stream_audio_codec") or "?").lower()] += 1
    container_played[(r.get("container") or "?").lower()] += 1
    client_platform[(r.get("platform") or "?").lower()] += 1
    client_player[(r.get("player") or "?").lower()] += 1

print("\nTranscode decision overall:")
for d, n in transcode_decision.most_common():
    print("  %-15s %d (%.1f%%)" % (d, n, 100*n/max(len(rows),1)))
print("\nVideo decision: " + ", ".join("%s=%d" % (k,v) for k,v in video_decision.most_common()))
print("Audio decision: " + ", ".join("%s=%d" % (k,v) for k,v in audio_decision.most_common()))
print("Sub decision:   " + ", ".join("%s=%d" % (k,v) for k,v in sub_decision.most_common()))

print("\nVIDEO transcode reasons (src -> dest):")
for k, v in trans_reason_v.most_common():
    print("  %-30s %d" % (k, v))
print("\nAUDIO transcode reasons (src -> dest):")
for k, v in trans_reason_a.most_common():
    print("  %-30s %d" % (k, v))

print("\nWhat clients are playing (video codec source -> stream):")
print("  source codecs:  " + ", ".join("%s=%d" % (k,v) for k,v in codec_played.most_common()))
print("  stream codecs:  " + ", ".join("%s=%d" % (k,v) for k,v in codec_streamed.most_common()))
print("  source audio:   " + ", ".join("%s=%d" % (k,v) for k,v in audio_played.most_common()))
print("  stream audio:   " + ", ".join("%s=%d" % (k,v) for k,v in audio_streamed.most_common()))

print("\nTop player platforms (last 30d):")
for k, v in client_platform.most_common(8):
    print("  %-25s %d" % (k, v))
print("\nTop player apps:")
for k, v in client_player.most_common(8):
    print("  %-30s %d" % (k, v))

# Reconcile with on-disk usage
print("\n=== ON-DISK USAGE ===")
out = subprocess.check_output(["du", "-shc", os.path.expanduser("~/media")], text=True).strip()
print(out)
out2 = subprocess.check_output(["bash", "-c", "du -sh ~/media/*/ 2>/dev/null"], text=True).strip()
print(out2)
