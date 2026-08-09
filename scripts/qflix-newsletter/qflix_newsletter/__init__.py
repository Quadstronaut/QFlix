"""qflix-newsletter — weekly Plex digest, sent via Listmonk.

Replaces Conjurr (Gemini AI rec engine) and Newsletterr (Tautulli digest).
Path B per docs/superpowers/specs: standalone Python script, scheduled via
systemd timer Mon 08:00, reads ~/secrets/* at runtime.
"""

__version__ = "0.1.0"
