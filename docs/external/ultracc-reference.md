# Ultra.cc Seedbox Reference

Dense reference for `quadstronaut@seedbox.example.com` user-level operations.

## App Lifecycle

### Command Format
```
app-<name> {install|start|stop|restart|uninstall|backup|migrate|password|upgrade|version|help}
```

### Install
- Sets up application; prompts for strong password
- Flags: `--silent-install`, `--reuse-db` (app-specific)
- May include version selection during setup
- 5-10 minutes typical initialization time

### Start / Stop / Restart
- User-level systemd or Docker control
- Check status via Control Panel Apps tab
- Restart applies configuration changes

### Uninstall
- Removes app container; preserves config backups
- Home dir may contain residual config at `~/.apps/<app>/`

### Backup / Restore
- Accessible via Control Panel System menu
- Recommended before major version upgrades

### Migrate
- `app-<name> migrate` handles slot migration
- May show stale config from prior slot if not cleaned
- Run repair if issues persist post-migration

### Password
- `app-<name> password` resets/updates login password
- Used for web UI authentication

### Upgrade / Version
- `app-<name> upgrade` → latest
- `app-<name> version` → current version
- Beta/nightly versions selectable during install on some apps

### Help
- `app-<name> help` → available subcommands and flags

## Network Model

### Binding Patterns
- **User systemd**: Bound to `127.0.0.1:17xxx` (host-local only)
- **Docker containers**: Bound to `172.17.x.x` (docker0 bridge, visible to all tenants on host)

### Port Discovery
Actual upstream port found in:
```
~/.apps/nginx/proxy.d/<app>.conf
```
Look for `upstream` block port or Docker bridge IP.

### User Nginx
- Runs at `~/.apps/nginx/` 
- Reverse-proxies subpaths: `/sonarr/`, `/radarr/`, etc.
- Uses `~/.apps/nginx/proxy.d/<app>.conf` per app

### Public URL Pattern
```
https://<username>.<slot>.usbx.me/<app>/
```
Example: `https://quadstronaut.seedbox.example.com/sonarr/`

### BasePathOverride
*arr apps (*Sonarr*, *Radarr*, *Readarr*, etc.) require `BasePathOverride=/appname` in config to serve at `/appname/` subpath. Set via UI or edit config XML directly.

### Authentication
- Ultra.cc-managed nginx (external-facing)
- User-level htpasswd at `~/www/.htpasswd` (if custom auth needed)
- Most apps use local UI password, not htpasswd

## Per-App Documentation

### Sonarr (TV Management)
- **Install**: Via Control Panel Installers tab
- **Default port**: 8989 (internal), exposed at `/sonarr/`
- **Config**: `~/.apps/sonarr/config.xml`
- **API key**: Settings > General > Security
- **Setup**: Enable "Rename Episodes", use hardlinks, add Root Folder (`~/media`)
- **Download clients**: rTorrent, qBittorrent, Transmission, Deluge, SABnzbd, NZBGet
  - Host: `{username}.{servername}.usbx.me`, port 443, SSL enabled
  - Category: `tv-sonarr` (or `tv` for usenet)
- **Gotchas**: 502 errors from incorrect `config.xml` port; ensure port 8989 and SSL 9898

### Radarr (Movie Management)
- **Install**: Via Control Panel Installers tab
- **Default port**: 7878 (internal), SSL 9898
- **Config**: `~/.apps/radarr/config.xml`
- **API key**: Settings > General > Security
- **Setup**: Enable "Rename Movies", use hardlinks, add Root Folder (`~/media`)
- **Media servers**: Plex (token auth), Emby, Jellyfin (API keys)
  - Host: `servername-direct.usbx.me` (no SSL for local)
- **Download clients**: Same as Sonarr
- **Gotchas**: 502 errors from port misconfiguration; maintain 7878/9898 internally

### Prowlarr (Indexer Manager)
- **Install**: Via Control Panel Installers tab, set password
- **Setup**: Settings > Apps, enter Sonarr/Radarr/Readarr URLs and API keys
- **Download clients**: 7 clients supported (Deluge, rTorrent, Transmission, qBittorrent, SABnzbd, NZBGet)
  - All use SSL, port 443
  - Set category to `prowlarr`
- **URL base**: Can be configured for reverse proxy

### Bazarr (Subtitle Manager)
- **Install**: Via Control Panel Installers, password required
- **Prerequisites**: Radarr + Sonarr must be installed first
- **Setup**: Enable providers, select languages, create language profile
- **Connect Radarr/Sonarr**:
  - Host: `{username}.{servername}.usbx.me`
  - Port: 443, SSL enabled
  - Base URL: `sonarr` or `radarr`
- **Initialization**: ~5 minutes
- **Media location**: `~/media`
- **No `app-bazarr2` slot.** Ultra.cc provides `app-sonarr2` and `app-radarr2`, but not a second Bazarr. For anime *arr subtitle coverage, QFlix runs a parallel **bare-Python Bazarr 2** under `~/.apps/bazarr2/` (python3.11 venv, user systemd unit), version-pinned to bazarr-1 by `bazarr2-sync.timer`. See `scripts/install/06-bazarr2.sh` and `docs/anime-subs-deferred.md` (resolved).

### Plex Media Server
- **Requirements**: Plex account (plex.tv), non-Essential plan
- **Install**: Via Control Panel Installers
  - Visit `plex.tv/claim`, generate claim code, paste during install
- **Default folders**: `~/media/Movies/`, `~/media/TV Shows/`, `~/media/Music/`
- **Security**: Set "Secure connection" to at least "Preferred"
- **Plugins**: Install via SSH (Hama Bundle, Extended Personal Media Shows, Trakt.tv Scrobbler)
- **Troubleshooting**: Restart, wait 5 minutes; repair via Control Panel if container damaged
- **Gotchas**: Token expiration; reclaim via Plex.tv if needed

### Jellyfin (Open-source Media Server)
- **Minimum tier**: Metaliux or higher
- **Install**: Via Control Panel Installers, password required
- **Default folders**: `~/media/Movies/`, `~/media/TV Shows/`
- **Username**: Your Ultra.cc account username
- **Connection formats**:
  - Standard: `https://username.servername.usbx.me/jellyfin`
  - LG webOS TV: `servername-direct.usbx.me:{port}/jellyfin`
- **Troubleshooting**:
  - Slow streaming → disable transcoding (CPU-intensive)
  - WebUI issues → restart/repair via Control Panel
  - Check `migrations.xml` not empty, verify disk I/O

### Jellyseerr (Request Manager for Jellyfin/Plex/Emby)
- **Install**: Via Control Panel Installers
- **Prerequisites**: Media server (Jellyfin/Plex/Emby) + at least one of (Sonarr/Radarr/Lidarr)
- **Setup**:
  - Jellyfin/Emby: URL `<username>.<hostname>.usbx.me`, port 443, SSL enabled
  - Plex: Select `[local] [secure]` server, port 32400
- **Enable libraries**: Sync, select which to expose
- **Add download clients**: Input API keys from Radarr/Sonarr
- **Discord notifications**: Settings > Notifications > Discord, paste webhook
- **Direct HTTP access**: `http://{servername}-direct.usbx.me:{port}` (skips auth popup)

### Tautulli (Plex Monitoring)
- **Prerequisite**: Fully configured Plex Media Server
- **Install**: Via Control Panel Installers
- **Setup**: Settings > Plex Media Center, fetch auth token, enter Plex IP (typically `172.17.0.1`) and port
- **Custom scripts**: Support JBOPS repository automation
  - `killstream.py`: Prevent 4K transcoding
  - `limiterr.py`: Restrict nighttime viewing (22-01 UTC)
- **Script placement**: `~/.apps/tautulli/scripts/` (via SSH)

### Readarr (Ebook/Audiobook Management)
- **Install**: Via Control Panel Installers
- **Setup**: Enable book renaming, hardlinks; add Root Folder (`~/media`)
- **Download clients**: Deluge, qBittorrent, rTorrent, Transmission, SABnzbd, NZBGet
  - Host: `{username}.{servername}.usbx.me`, port 443, SSL enabled
- **Metadata**: Configurable via development settings
- **Integration**: Calibre Content Server support
- **Port**: 8787 (internal)

### Mylar3 (Comic Book Manager)
- **Prerequisites**: Torrent client + Jackett installed
- **Install**: Via Control Panel Installers, password required
- **Setup**: Enable authentication (Forms login), set username/password
- **Download clients**:
  - NZBGet/SABnzbd: `servername-direct.usbx.me`
  - Deluge: `servername.usbx.me` daemon port
  - rTorrent: `https://username.servername.usbx.me/RPC2`
- **Automation**: Enable post-processing with hardlink mode, folder monitoring, failed retry

### Komga (Digital Library Server)
- **Formats**: CBZ, CBR, EPUB, PDF
- **Install**: Via Control Panel Installers
- **Library setup**: Create library, point to folder with books
- **Folder structure**: `Library Root/Series Name/book files`
- **Update**: `app-komga upgrade` via SSH
- **OPDS feed**: `https://{username}.{servername}.usbx.me/komga/opds/v1.2/catalog`
- **Default login**: Ultra.cc username + installation password

### Kavita (Comics & Ebooks)
- **Install**: Via Control Panel Installers
- **Default library**: "Manga" at `~/media/Manga`
- **Library config**: Server Settings > Libraries > Add Library
- **User management**: Server Settings > Users > Invite
- **OPDS access**: `https://<username>.<hostname>.usbx.me/kavita` (UCP credentials)

### Calibre-Web (Ebook Interface)
- **Install**: Via Control Panel Installers
- **Features**: Browse, read (EPUB in browser), download ebooks
- **Database**: Uses Calibre database
- **OPDS integration**: Moon+ Reader: `https://username.hostname.usbx.me/calibre-web` + UCP credentials
- **Default login**: UCP username + installation password

### Audiobookshelf (Audiobook & Podcast Server)
- **Install**: Via Control Panel Installers, password required
- **Default libraries**: `~/media/Audiobooks/`, `~/media/Podcasts/`
- **URL format**: `https://audiobookshelf-username.hostname.usbx.me`
- **Directory structure**: Authors (top-level) → book titles → audio files + cover images
- **Mobile apps**: Android + iOS, configure via server address from Control Panel
- **User management**: Settings (gear icon), add users with type (Guest/User/Admin) + permissions

### FlareSolverr (Cloudflare Bypass)
- **Purpose**: Proxy to bypass Cloudflare for indexers
- **Install**: Via Control Panel Installers
- **Prowlarr integration**: Settings > Indexers, add proxy with host `http://172.17.0.1:xxxxx`, tag indexers
- **Jackett integration**: Input FlareSolverr API URL in configuration
- **Critical**: Requires regular updates; captcha tech evolves frequently

### Unpackerr (Archive Extractor)
- **Formats**: rar, tar, tgz, gz, zip, 7z, bz2, tbz2
- **Install**: Via Control Panel (not available on Essential tier)
- **Auto-detection**: Automatically detects Radarr/Sonarr/Lidarr/Readarr and configures them
- **Behavior**: Checks media app queues every 2 min, removes extracted data every 5 min
- **Watch directory**: `~/downloads/unpackerr/` (created on install)
- **Wait time**: ~10 min after import before removing torrents to prevent duplicates

### Homarr (Dashboard)
- **Install**: Via Control Panel Installers
- **Configuration**: Edit Mode → Move tiles → Add Tile → Apps
- **URL format**: No trailing slash; e.g., `https://username.hostname.usbx.me/appname`
- **Authentication**: Username/password or API keys per app (API keys in Settings > General for *arr apps)
- **Upgrade**: V1 version available with migration support

### Uptime Kuma (Monitoring)
- **Install**: Via Control Panel Installers, password required
- **Monitor type**: HTTP(s), TCP, ping, DNS
- **URL format**: `https://username.hostname.usbx.me/appname`
- **Notifications**: Discord via webhook; toggle "Default enabled" for all monitors
- **Testing**: Test notification before saving

### Jellystat (Jellyfin Analytics)
- **Prerequisites**: Jellyfin + PostgreSQL installed
- **Status**: Beta (stability not guaranteed)
- **Setup**: Generate Jellyfin API key (Dashboard > API Keys), enter into Jellystat setup
- **Password restriction**: Cannot contain symbols (`!`, `@`, `%`)
- **Access**: Apps section of Control Panel

### Maintainerr (Multi-app Dashboard)
- **Purpose**: Monitor & manage Plex, Overseerr, Jellyseerr, Radarr, Sonarr, Tautulli
- **Install**: Via Control Panel Installers
- **First step**: Authenticate Plex (auto-directed on login)
  - Use `[local] [secure]` server entry
- **Other apps**: Each needs hostname/port/base URL + API keys
- **Features**: System logs access, scheduled tasks monitoring via Jobs tab

### Autobrr (Torrent Automation)
- **Install**: Via Control Panel Installers
- **Clients**: qBittorrent, Deluge, rTorrent, Transmission
- **Prowlarr feed integration**:
  - Copy feed URL from Prowlarr indexer list
  - Extract base: `https://username.hostname.usbx.me/prowlarr/1/api`
  - Extract API key from URL parameters
  - Settings > Indexers > Generic Torznab
- **Logs**: `~/.apps/autobrr/log/autobrr.log` or via `tail -f` SSH

### qBittorrent (Torrent Client)
- **Install**: Via Control Panel Installers
- **Default paths**:
  - Downloads: `~/downloads/qbittorrent`
  - Config: `~/.config/qbittorrent`
  - Watch folder: `~/watch/qbittorrent`
- **Fair Usage Policy**: Rclone fuse mounts create extreme disk strain → 24-hour ban
- **HDD optimization**: Limit concurrent downloads to 1-3 to prevent I/O saturation
- **Features**: Magnet links, `.torrent` files, pooled IP visible in Control Panel
- **Troubleshooting**: Connection issues → repair function; 502 errors → restart app/webserver

## App Management Commands & Utilities

### app-stats
```
app-stats              # Shows usage (CPU, RAM, disk) when no args
```
Only displays when run with no arguments; outputs memory/disk used per app.

### app-list
Docs silent on this command; verify via `app-list help` on slot.

### app-ports
Docs silent on this command; verify via `app-ports help` on slot.

## Reverse Proxy & Auth

### Ultra.cc-Managed Nginx
- External-facing, handles HTTPS at `https://<user>.<slot>.usbx.me`
- Proxies to user-level nginx at `~/.apps/nginx/`

### User-Level Nginx
- Runs on `127.0.0.1`
- Config: `~/.apps/nginx/proxy.d/<app>.conf`
- Upstream block reveals Docker IP + port or systemd port

### Custom Auth
- `~/www/.htpasswd` can be used if custom HTTP Basic auth needed
- Most apps use local UI password instead

### SubPath Routing
Apps accessed at `/appname/` require:
1. Nginx reverse proxy configured at `~/.apps/nginx/proxy.d/<app>.conf`
2. App's `BasePathOverride` or equivalent setting = `/appname`
3. Typical format: `https://<user>.<slot>.usbx.me/appname/`

## Disk Quota & Storage

### Quota System
- **Upload quota**: Monthly limit per plan; counts data sent *to* slot
- **Download quota**: Unlimited (incoming traffic not counted)
- **Reset**: On day of sign-up each month
- **Exempt**: FTP, SSH, media server uploads (Plex, Emby, Jellyfin)

### Throttling
- Essential tier: 10 Mb/s after quota exhausted
- Non-Essential: 100 Mb/s after quota exhausted
- Downloads: Unaffected by quota

### Usage Reporting
- `app-stats` shows per-app disk usage (when run alone)
- `ncdu -x` shows folder-level disk usage via SSH
- 3 TB user quota typical (per plan details)

### Rclone Fuse Mounts
- Observed as `fuse.rclone` mount points (e.g., `/home34/melvin03/plex`)
- Allow cloud storage integration without local copy
- **Caution**: Direct cloud mounts on torrent downloads cause extreme I/O strain → banned

## Limitations & Out-of-Scope

- **TLS/SSL certs**: Ultra.cc-managed only; user cannot customize
- **System OS**: Debian; user cannot modify kernel or system packages
- **Port assignment**: Fixed; user cannot request specific ports
- **Root access**: Not available
- **Docker exec**: Not available for user-level operation
- **Shared IPs**: All users on shared IP pool
- **Data isolation**: Users jailed to home directory; cannot access other users

## Connection Details

### SSH
- Host: `{username}.{slot}.usbx.me`
- Port: 22
- Auth: SSH keys (public-key) or password
- UCP shows connection string

### FTP
- Host: `{username}.{slot}.usbx.me`
- Port: 21
- Username: UCP username
- Password: From UCP

### WebDAV
- Protocol for file access
- Integrated with slot

### HTTP Access
- Reverse proxy via nginx
- Subpath routing per app

### OpenVPN
- 3 VPN configs provided per service

## Source Pages Crawled

- https://docs.ultra.cc/ (main index)
- https://docs.ultra.cc/applications (app list)
- https://docs.ultra.cc/connection-details (network overview)
- https://docs.ultra.cc/connection-details/ssh (SSH guide)
- https://docs.ultra.cc/connection-details/ssh/your-ultra-shell-a-beginners-guide (shell & app- commands)
- https://docs.ultra.cc/applications/sonarr (TV management)
- https://docs.ultra.cc/applications/radarr (movie management)
- https://docs.ultra.cc/applications/prowlarr (indexer manager)
- https://docs.ultra.cc/applications/bazarr (subtitle manager)
- https://docs.ultra.cc/applications/plex (media server)
- https://docs.ultra.cc/applications/jellyfin (open-source media server)
- https://docs.ultra.cc/applications/jellyseerr (request manager)
- https://docs.ultra.cc/applications/tautulli (plex monitoring)
- https://docs.ultra.cc/applications/readarr (ebook/audiobook management)
- https://docs.ultra.cc/applications/mylar3 (comic books)
- https://docs.ultra.cc/applications/komga (digital library)
- https://docs.ultra.cc/applications/kavita (comics & ebooks)
- https://docs.ultra.cc/applications/calibre-web (ebook interface)
- https://docs.ultra.cc/applications/audiobookshelf (audiobook & podcast)
- https://docs.ultra.cc/applications/flaresolverr (cloudflare bypass)
- https://docs.ultra.cc/applications/unpackerr (archive extractor)
- https://docs.ultra.cc/applications/homarr (dashboard)
- https://docs.ultra.cc/applications/uptime-kuma (monitoring)
- https://docs.ultra.cc/applications/jellystat (jellyfin stats)
- https://docs.ultra.cc/applications/maintainerr (multi-app dashboard)
- https://docs.ultra.cc/applications/autobrr (torrent automation)
- https://docs.ultra.cc/applications/qbittorrent (torrent client)
- https://docs.ultra.cc/getting-started/faq (FAQ, quotas, service details)
- https://docs.ultra.cc/misc-guides (misc topics index)
- https://docs.ultra.cc/rclone (rclone overview)
