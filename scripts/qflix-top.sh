#!/usr/bin/env bash
# qflix-top — htop-style view of how much CPU/RAM each QFlix component uses,
# in ratio to each other AND to the rest of the shared seedbox.
#
# Runs as an unprivileged user on an Ultra.cc shared box (hidepid=2): we can
# only see our own processes, but /proc/stat and /proc/meminfo are box-wide.
# So "everyone else" is derived as (box total - mine), never by snooping other
# users' processes.
#
# Grouping: processes are bucketed by their cgroup (docker container hash for
# UCC apps, systemd unit for native services), then given a friendly label by
# matching any member's cmdline. This bundles Plex's server+transcoder+plugins
# into one "Plex" line and keeps sonarr/sonarr2 (separate containers) distinct.
#
# Usage:
#   ./qflix-top.sh                 live view, 2s refresh (default: role)
#   ./qflix-top.sh --view app      per-app | role | both
#   ./qflix-top.sh --interval 3    refresh seconds
#   ./qflix-top.sh --once          single snapshot, then exit (pipe-friendly)
# Live keys: [a]pp  [r]ole  [b]oth views · [+]/[-] interval · [q]uit
set -u

VIEW=role
INTERVAL=2
ONCE=0
while [ $# -gt 0 ]; do
  case $1 in
    --view) VIEW=${2:-role}; shift 2;;
    --interval) INTERVAL=${2:-2}; shift 2;;
    --once) ONCE=1; shift;;
    -h|--help) sed -n '2,18p' "$0"; exit 0;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done

MYUID=$(id -u)
NCPU=$(nproc 2>/dev/null || echo 1)
HOSTN=$(hostname 2>/dev/null || echo seedbox)
USERN=$(id -un 2>/dev/null || echo me)

# Map a cmdline to a component, setting CL_LABEL / CL_ROLE / CL_TIER (globals, so
# we avoid a subshell fork per process). CL_TIER ranks confidence so the right
# process wins its cgroup regardless of scan order:
#   2 = a real app · 1 = bundled infra (nginx/redis a UCC container ships with) ·
#   0 = unrecognized (init wrappers like sh/tini/dumb-init).
# Without this, the greedy *nginx* match would mislabel any app container that
# runs its own reverse proxy (Maintainerr did exactly this). Add new apps here.
classify() {
  CL_TIER=2                       # default for the recognized apps below
  case $1 in
    *plexmediaserver*|*"Plex Media Server"*|*"Plex Transcoder"*) CL_LABEL=Plex; CL_ROLE=Media;;
    *qbittorrent-nox*)            CL_LABEL=qBittorrent; CL_ROLE=Download;;
    *unpackerr*)                  CL_LABEL=unpackerr; CL_ROLE=Download;;
    */app/sonarr/bin/Sonarr*)     CL_LABEL=Sonarr; CL_ROLE=Arr;;
    */app/radarr/bin/Radarr*)     CL_LABEL=Radarr; CL_ROLE=Arr;;
    */app/prowlarr/bin/Prowlarr*) CL_LABEL=Prowlarr; CL_ROLE=Arr;;
    *bazarr2*)                    CL_LABEL=Bazarr2; CL_ROLE=Arr;;
    *bazarr*)                     CL_LABEL=Bazarr; CL_ROLE=Arr;;
    *flaresolverr*)               CL_LABEL=FlareSolverr; CL_ROLE=Arr;;
    *Tautulli.py*)                CL_LABEL=Tautulli; CL_ROLE=Stats;;
    *Tdarr_Server*)               CL_LABEL="Tdarr Server"; CL_ROLE=Transcode;;
    *Tdarr_Node*)                 CL_LABEL="Tdarr Node"; CL_ROLE=Transcode;;
    *application.jar*)            CL_LABEL=Komga; CL_ROLE=Books;;
    */app/kavita/Kavita*|*/Kavita) CL_LABEL=Kavita; CL_ROLE=Books;;
    *calibre-web*|*cps.py*)       CL_LABEL=Calibre-Web; CL_ROLE=Books;;
    *victoria-logs*)              CL_LABEL=VictoriaLogs; CL_ROLE=Stats;;
    *uptime-kuma*)                CL_LABEL="Uptime Kuma"; CL_ROLE=Stats;;
    *qflix_newsletter*|*qflix-newsletter*) CL_LABEL=Newsletter; CL_ROLE=Comms;;
    *listmonk*)                   CL_LABEL=Listmonk; CL_ROLE=Comms;;
    *manitoba-maint*)             CL_LABEL=manitoba-maint; CL_ROLE=Maint;;
    *wssServer.cjs*|*apps/nextjs*) CL_LABEL=Maintainerr; CL_ROLE=Retention;;
    *dist/index.js*)              CL_LABEL=Seerr; CL_ROLE=Requests;;
    *postgres*)                   CL_LABEL=Postgres; CL_ROLE=Data;;
    *"node index.js"*)            CL_LABEL=Audiobookshelf; CL_ROLE=Books;;  # entry=index.js, CONFIG_PATH=/config
    # --- tier 1: infra a UCC app container bundles; a real app in the same cgroup outranks it ---
    *redis-server*)               CL_LABEL=Redis; CL_ROLE=Data; CL_TIER=1;;
    *nginx*)                      CL_LABEL=nginx; CL_ROLE=Web;  CL_TIER=1;;
    */lib/systemd/systemd*)       CL_LABEL="systemd (user)"; CL_ROLE=System; CL_TIER=1;;
    *dbus-daemon*)                CL_LABEL=dbus; CL_ROLE=System; CL_TIER=1;;
    *) CL_LABEL=''; CL_ROLE=''; CL_TIER=0;;
  esac
}

# Some apps are only distinguishable by working directory, not cmdline (e.g.
# Maintainerr's API backend runs a bare `node dist/main`). Resolve via cwd —
# only called for node/npm processes the cmdline pass couldn't identify, so the
# extra readlink fork stays rare. Sets CL_LABEL/CL_ROLE on a hit.
classify_by_cwd() {
  local cwd; cwd=$(readlink "/proc/$1/cwd" 2>/dev/null)
  case $cwd in
    /opt/app|*/opt/app/apps/server*) CL_LABEL="Maintainerr api"; CL_ROLE=Retention; CL_TIER=2;;
  esac
}

# Group key from a pid's cgroup: docker container short-hash, or the .service
# unit, or "misc". Uses the unified (0::) line, falling back to name=systemd.
cgkey() {
  local pid=$1 line path
  while IFS= read -r line; do
    case $line in
      0::*) path=${line#0::} ;;
      1:name=systemd:*) [ -z "${path:-}" ] && path=${line#1:name=systemd:} ;;
    esac
  done < "/proc/$pid/cgroup" 2>/dev/null
  path=${path:-/}
  case $path in
    */docker/*) local h=${path##*/docker/}; h=${h%%/*}; echo "dk:${h:0:12}";;
    *.service)  echo "u:$(basename "$path")";;
    *)          echo "misc";;
  esac
}

# utime+stime (jiffies) from /proc/<pid>/stat. comm field can contain spaces
# and ')', so split on the LAST ')' and index the remainder.
cpu_jiffies() {
  local raw rest
  read -r raw 2>/dev/null < "/proc/$1/stat" || return 1
  rest=${raw##*) }            # drop "pid (comm) "
  # shellcheck disable=SC2086
  set -- $rest                # rest[1]=state(f3); utime=f14 -> $12, stime=f15 -> $13
  echo $(( ${12:-0} + ${13:-0} ))
}

# Proportional set size (kB) — shared pages counted once, unlike RSS.
pss_kb() {
  local k v
  while read -r k v _; do
    [ "$k" = "Pss:" ] && { echo "$v"; return; }
  done < "/proc/$1/smaps_rollup" 2>/dev/null
  # fallback: RSS pages from statm * 4 kB
  local rss
  read -r _ rss _ < "/proc/$1/statm" 2>/dev/null && echo $(( rss * 4 )) || echo 0
}

# total jiffies across all cores, and the idle (idle+iowait) portion.
# guest/guest_nice are NOT added: the kernel already folds them into user/nice,
# so summing them again would double-count. total = busy + idle, which over one
# interval equals ncpu * interval * CLK_TCK (verified against /proc/stat).
read_stat() {  # echoes "total idle"
  local _ u n s idle iowait irq softirq steal
  read -r _ u n s idle iowait irq softirq steal _ < /proc/stat
  local idleall=$(( idle + iowait ))
  local busy=$(( u + n + s + irq + softirq + steal ))
  echo "$(( busy + idleall )) $idleall"
}

declare -A PREV          # pid -> cpu jiffies at previous sample
declare -A G_CPU G_PSS G_LABEL G_ROLE G_TIER   # G_TIER = best label confidence seen (see classify)

# One sample: fill PREV/G_* deltas against the previous reading.
# Returns the per-group table on stdout via the global REPORT/TOTALS.
sample() {
  local pids pid key d cpu1 p prev cmd parts
  read -r ST_TOT ST_IDLE < <(read_stat)
  G_CPU=(); G_PSS=(); G_LABEL=(); G_ROLE=(); G_TIER=()
  MINE_CPU=0; MINE_PSS=0
  pids=$(ps -u "$MYUID" -o pid= 2>/dev/null)
  for pid in $pids; do
    cpu1=$(cpu_jiffies "$pid") || continue
    prev=${PREV[$pid]:-}
    PREV[$pid]=$cpu1
    if [ -n "$prev" ]; then d=$(( cpu1 - prev )); else d=0; fi  # new pid: no delta yet
    (( d < 0 )) && d=0
    p=$(pss_kb "$pid")
    key=$(cgkey "$pid")
    # Label the group by the highest-confidence process seen in it. Tier 2 (a
    # real app) is the ceiling, so once reached we stop classifying the group's
    # other pids (skips Plex's swarm). A higher tier always overrides a lower one
    # regardless of scan order, so a bundled nginx never masks the real app.
    if [ "${G_TIER[$key]:-0}" -lt 2 ]; then
      mapfile -d '' -t parts < "/proc/$pid/cmdline" 2>/dev/null  # NUL-delimited, fork-free
      cmd="${parts[*]}"
      classify "$cmd"
      if [ -z "$CL_LABEL" ] && { [[ $cmd == *node* ]] || [[ $cmd == *npm* ]]; }; then
        classify_by_cwd "$pid"
      fi
      if [ -n "$CL_LABEL" ] && [ "${CL_TIER:-0}" -gt "${G_TIER[$key]:-0}" ]; then
        G_LABEL[$key]=$CL_LABEL; G_ROLE[$key]=$CL_ROLE; G_TIER[$key]=$CL_TIER
      elif [ -z "${G_LABEL[$key]:-}" ]; then   # only an init wrapper seen so far
        G_LABEL[$key]=${cmd:0:20}; [ -z "${G_LABEL[$key]}" ] && G_LABEL[$key]=$key
        G_ROLE[$key]=Other; G_TIER[$key]=0
      fi
    fi
    G_CPU[$key]=$(( ${G_CPU[$key]:-0} + d ))
    G_PSS[$key]=$(( ${G_PSS[$key]:-0} + p ))
    MINE_CPU=$(( MINE_CPU + d )); MINE_PSS=$(( MINE_PSS + p ))
  done
}

emit_groups() {  # cpu_d \t pss_kb \t label \t role  (sorted by cpu desc)
  local k
  for k in "${!G_LABEL[@]}"; do
    printf '%s\t%s\t%s\t%s\n' "${G_CPU[$k]:-0}" "${G_PSS[$k]:-0}" "${G_LABEL[$k]}" "${G_ROLE[$k]}"
  done | sort -t$'\t' -k1 -nr
}

render() {
  local cols memtot memav total_d idle_d busy_d
  cols=$(tput cols 2>/dev/null || echo 100); (( cols > 160 )) && cols=160
  memtot=$(awk '/^MemTotal:/{print $2; exit}' /proc/meminfo)
  memav=$(awk '/^MemAvailable:/{print $2; exit}' /proc/meminfo)
  total_d=$(( ST_TOT - P_TOT )); (( total_d <= 0 )) && total_d=1
  idle_d=$(( ST_IDLE - P_IDLE )); (( idle_d < 0 )) && idle_d=0
  busy_d=$(( total_d - idle_d )); (( busy_d < 0 )) && busy_d=0

  emit_groups | awk -F'\t' \
    -v cols="$cols" -v ncpu="$NCPU" -v view="$VIEW" \
    -v memtot="$memtot" -v memav="$memav" \
    -v minecpu="$MINE_CPU" -v minepss="$MINE_PSS" \
    -v totald="$total_d" -v idled="$idle_d" -v busyd="$busy_d" \
    -v host="$HOSTN" -v usern="$USERN" -v interval="$INTERVAL" -v once="$ONCE" '
    function clamp(x){ return x<0?0:(x>1?1:x) }
    function bar(frac,w,   n,full,rem,i,s,p,np){
      frac=clamp(frac); n=int(frac*w*8+0.5); full=int(n/8); rem=n%8
      s=""
      for(i=0;i<full;i++) s=s "\342\226\210"            # full block
      # partial eighth blocks ▏▎▍▌▋▊▉ for the fractional remainder
      if(rem>0){ split("",E); E[1]="\342\226\217";E[2]="\342\226\216";E[3]="\342\226\215";E[4]="\342\226\214";E[5]="\342\226\213";E[6]="\342\226\212";E[7]="\342\226\211"; s=s E[rem]; full++ }
      for(i=full;i<w;i++) s=s "\342\226\221"            # light shade
      return s
    }
    function col(c,t){ return sprintf("\033[38;5;%dm%s\033[0m",c,t) }
    function gib(kb){ return sprintf("%.1f",kb/1048576) }
    BEGIN{
      ORANGE=215; CYAN=117; GOLD=178; DIM=240; GREEN=114
      pal[0]=215; pal[1]=117; pal[2]=178; pal[3]=114; pal[4]=211; pal[5]=180; pal[6]=109
      n=0
    }
    {
      cpu[n]=$1+0; pss[n]=$2+0; lab[n]=$3; role[n]=($4==""?"Other":$4); n++
    }
    END{
      # de-dupe identical labels (sonarr/sonarr2 share a label) -> append index
      for(i=0;i<n;i++){ seen[lab[i]]++; if(seen[lab[i]]>1) lab[i]=lab[i] " " seen[lab[i]] }

      # All figures below derive from these raw values so they always reconcile.
      mine_box   = (minecpu*100.0)/totald          # your % of ALL cores
      mine_cores = (minecpu*1.0/totald)*ncpu        # your usage in whole-core units
      idle_box   = (idled*100.0)/totald
      box_busy   = (busyd*100.0)/totald             # whole box, all tenants
      share_busy = busyd>0 ? (minecpu*100.0)/busyd : 0   # your share of in-use CPU
      mine_inuse_frac = busyd>0 ? minecpu/busyd : 0

      used_kb     = memtot - memav
      mine_mem_box= (minepss*100.0)/memtot
      mine_used   = used_kb>0 ? (minepss*100.0)/used_kb : 0
      mine_used_frac = used_kb>0 ? minepss*1.0/used_kb : 0

      # ---- header ----
      printf "\033[1m%s\033[0m  %s@%s\n", "qflix-top", col(ORANGE,usern), host

      sw = cols-34; if(sw<16) sw=16; if(sw>72) sw=72
      # The bar shows IN-USE CPU split into you vs other tenants, so your slice
      # stays visible even though it is a sliver of the 128-core whole. Idle is text.
      me = int(sw*clamp(mine_inuse_frac)+0.5); if(me<1 && minecpu>0) me=1
      cb=""; for(i=0;i<me;i++) cb=cb "\342\226\210"; cb=col(ORANGE,cb)
      t="";  for(i=me;i<sw;i++) t=t "\342\226\210"; cb=cb col(CYAN,t)
      printf " CPU  %d cores · box %s busy · %s idle\n",
        ncpu, col(GOLD,sprintf("%.1f%%",box_busy)), col(DIM,sprintf("%.0f%%",idle_box))
      printf "      %s  you %s of in-use   (\342\211\210%.2f of %d cores · %.2f%% of total)\n",
        cb, col(ORANGE,sprintf("%.1f%%",share_busy)), mine_cores, ncpu, mine_box

      me = int(sw*clamp(mine_used_frac)+0.5); if(me<1 && minepss>0) me=1
      mb=""; for(i=0;i<me;i++) mb=mb "\342\226\210"; mb=col(ORANGE,mb)
      t="";  for(i=me;i<sw;i++) t=t "\342\226\210"; mb=mb col(CYAN,t)
      printf " RAM  %s GiB · %s used · %s free\n",
        gib(memtot), col(GOLD,gib(used_kb)), col(DIM,gib(memav))
      printf "      %s  you %s of used    (%s GiB · %.2f%% of total)\n",
        mb, col(ORANGE,sprintf("%.1f%%",mine_used)), gib(minepss), mine_mem_box
      print ""
      print col(DIM,"      bar: \033[38;5;215myou\033[38;5;240m vs \033[38;5;117mother tenants\033[38;5;240m (of the in-use portion)")
      print ""

      # ---- component breakdown ----
      bw=int((cols-58)/2); if(bw<10) bw=10; if(bw>26) bw=26
      printf "\033[1m %-16s %-*s   %-*s\033[0m\n"," component",bw+24,"CPU  (bar: share of you · 100% core = 1 full core)",bw+10,"RAM (share of you)"

      if(view=="role" || view=="both"){
        # Aggregate per role, keyed by the role NAME (not a slot index) so a
        # missing/duplicate index can never spawn a phantom empty header.
        delete rcpu; delete rpss; delete rlist
        for(i=0;i<n;i++){ rcpu[role[i]]+=cpu[i]; rpss[role[i]]+=pss[i] }
        nr=0; for(r in rcpu) rlist[nr++]=r
        for(a=0;a<nr;a++) for(b=a+1;b<nr;b++) if(rcpu[rlist[b]]>rcpu[rlist[a]]){tmp=rlist[a];rlist[a]=rlist[b];rlist[b]=tmp}
        for(a=0;a<nr;a++){ r=rlist[a]
          if(view=="role") row(r, rcpu[r], rpss[r], bw, pal[a%7])
          else {  # both: role header, then its apps (input is already cpu-sorted)
            printf "\033[1m %s\033[0m\n", col(GOLD,r)
            for(i=0;i<n;i++) if(role[i]==r){ printf "  "; row(lab[i],cpu[i],pss[i],bw,pal[a%7]) }
          }
        }
      } else {
        shown=0
        for(i=0;i<n && shown<22;i++){ row(lab[i],cpu[i],pss[i],bw,pal[i%7]); shown++ }
        if(n>shown) printf " \033[38;5;240m… %d more\033[0m\n", n-shown
      }

      if(!once){
        printf "\n \033[38;5;240m[a]pp [r]ole [b]oth · [+/-] %ds · [q]uit\033[0m\n", interval
      }
    }
    function row(name,c,p,bw,color,   cf,pf,corepct){
      cf = minecpu>0 ? c/minecpu : 0          # share of YOUR total (bar)
      pf = minepss>0 ? p/minepss : 0
      corepct = (c*100.0*ncpu)/totald          # top-style: 100% = one full core
      printf " %-16.16s %s %s   %s %s\n",
        name,
        col(color,bar(cf,bw)),
        sprintf("%3.0f%% yours · %4.0f%% core", cf*100, corepct),
        col(color,bar(pf,bw)),
        sprintf("%5s GiB", gib(p))
    }
    '
}

# ---- main ----
P_TOT=0; P_IDLE=0
prime() { read -r P_TOT P_IDLE < <(read_stat); sample; P_TOT=$ST_TOT; P_IDLE=$ST_IDLE; }

if [ "$ONCE" = 1 ]; then
  prime
  sleep "$INTERVAL"
  sample
  render
  exit 0
fi

cleanup(){ tput rmcup 2>/dev/null; tput cnorm 2>/dev/null; }
trap 'cleanup; exit 0' INT TERM
tput smcup 2>/dev/null; tput civis 2>/dev/null
prime
while :; do
  key=""
  IFS= read -rsn1 -t "$INTERVAL" key || true   # this read IS the sample interval
  case $key in
    q|Q) break;;
    a|A) VIEW=app;;
    r|R) VIEW=role;;
    b|B) VIEW=both;;
    +)   INTERVAL=$(( INTERVAL + 1 ));;
    -)   (( INTERVAL > 1 )) && INTERVAL=$(( INTERVAL - 1 ));;
  esac
  P_TOT=$ST_TOT; P_IDLE=$ST_IDLE      # carry previous /proc/stat reading
  sample
  out=$(render)
  tput clear 2>/dev/null
  printf '%s\n' "$out"
done
cleanup
