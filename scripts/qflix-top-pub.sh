#!/usr/bin/env bash
# qflix-top-pub — PUBLIC, stack-only view of how much CPU/RAM each QFlix
# component uses. The "you vs other tenants" comparison from qflix-top.sh is
# intentionally stripped: this variant shows ONLY our own stack's footprint
# (suitable for the public dashboard and the /api/usage feed). It never derives
# or implies other tenants' usage.
#
# Runs as an unprivileged user on an Ultra.cc shared box (hidepid=2): we can
# only see our own processes. /proc/stat and /proc/meminfo are box-wide and are
# used here ONLY to express our footprint in whole-core / GiB units and as a
# fraction of total machine capacity — never to break out anyone else's share.
#
# Grouping: processes are bucketed by their cgroup (docker container hash for
# UCC apps, systemd unit for native services), then given a friendly label by
# matching any member's cmdline. Anything unrecognized collapses into a single
# "Other" bucket — the public view never leaks raw cmdlines/paths.
#
# Usage:
#   ./qflix-top-pub.sh                 live view, 2s refresh (default: role)
#   ./qflix-top-pub.sh --view app      per-app | role | both
#   ./qflix-top-pub.sh --interval 3    refresh seconds
#   ./qflix-top-pub.sh --once          single snapshot, then exit (pipe-friendly)
#   ./qflix-top-pub.sh --json          single JSON snapshot for the dashboard
# Live keys: [a]pp  [r]ole  [b]oth views · [+]/[-] interval · [q]uit
set -u

VIEW=role
INTERVAL=2
ONCE=0
MODE=human
while [ $# -gt 0 ]; do
  case $1 in
    --view) VIEW=${2:-role}; shift 2;;
    --interval) INTERVAL=${2:-2}; shift 2;;
    --once) ONCE=1; shift;;
    --json) MODE=json; ONCE=1; shift;;
    -h|--help) sed -n '2,21p' "$0"; exit 0;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done

MYUID=$(id -u)
NCPU=$(nproc 2>/dev/null || echo 1)
HOSTN=$(hostname 2>/dev/null || echo seedbox)
USERN=$(id -un 2>/dev/null || echo me)
CAPTURED=$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo "")

# Map a cmdline to a component, setting CL_LABEL / CL_ROLE / CL_TIER (globals, so
# we avoid a subshell fork per process). CL_TIER ranks confidence so the right
# process wins its cgroup regardless of scan order:
#   2 = a real app · 1 = bundled infra (nginx/redis a UCC container ships with) ·
#   0 = unrecognized (init wrappers like sh/tini/dumb-init).
# Without this, the greedy *nginx* match would mislabel any app container that
# runs its own reverse proxy. Add new apps here — keep in sync with the Kuma
# monitor set (manifest/apps.yaml).
classify() {
  CL_TIER=2                       # default for the recognized apps below
  case $1 in
    *plexmediaserver*|*"Plex Media Server"*|*"Plex Transcoder"*) CL_LABEL=Plex; CL_ROLE=Media;;
    *qbittorrent-nox*)            CL_LABEL=qBittorrent; CL_ROLE=Download;;
    *SABnzbd.py*)                 CL_LABEL=SABnzbd; CL_ROLE=Download;;
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
    *qflix-dash/build*)           CL_LABEL="QFlix Dash"; CL_ROLE=Web;;
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

# total jiffies across all cores, and the idle (idle+iowait) portion. Used only
# to scale OUR cpu deltas into whole-core units — never to report box busyness.
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
    # other pids. A higher tier always overrides a lower one regardless of scan
    # order, so a bundled nginx never masks the real app.
    if [ "${G_TIER[$key]:-0}" -lt 2 ]; then
      mapfile -d '' -t parts < "/proc/$pid/cmdline" 2>/dev/null  # NUL-delimited, fork-free
      cmd="${parts[*]}"
      classify "$cmd"
      if [ -n "$CL_LABEL" ] && [ "${CL_TIER:-0}" -gt "${G_TIER[$key]:-0}" ]; then
        G_LABEL[$key]=$CL_LABEL; G_ROLE[$key]=$CL_ROLE; G_TIER[$key]=$CL_TIER
      elif [ -z "${G_LABEL[$key]:-}" ]; then   # nothing recognized in this cgroup yet
        # Public view: collapse to "Other" — never leak the raw cmdline/path.
        G_LABEL[$key]=Other; G_ROLE[$key]=Other; G_TIER[$key]=0
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
  local cols memtot memav total_d idle_d
  cols=$(tput cols 2>/dev/null || echo 100); (( cols > 160 )) && cols=160
  memtot=$(awk '/^MemTotal:/{print $2; exit}' /proc/meminfo)
  memav=$(awk '/^MemAvailable:/{print $2; exit}' /proc/meminfo)
  total_d=$(( ST_TOT - P_TOT )); (( total_d <= 0 )) && total_d=1
  idle_d=$(( ST_IDLE - P_IDLE )); (( idle_d < 0 )) && idle_d=0

  emit_groups | awk -F'\t' \
    -v cols="$cols" -v ncpu="$NCPU" -v view="$VIEW" -v mode="$MODE" \
    -v memtot="$memtot" -v memav="$memav" \
    -v minecpu="$MINE_CPU" -v minepss="$MINE_PSS" \
    -v totald="$total_d" -v interval="$INTERVAL" -v once="$ONCE" \
    -v host="$HOSTN" -v usern="$USERN" -v captured="$CAPTURED" '
    function clamp(x){ return x<0?0:(x>1?1:x) }
    function bar(frac,w,   n,full,rem,i,s){
      frac=clamp(frac); n=int(frac*w*8+0.5); full=int(n/8); rem=n%8
      s=""
      for(i=0;i<full;i++) s=s "\342\226\210"            # full block
      if(rem>0){ split("",E); E[1]="\342\226\217";E[2]="\342\226\216";E[3]="\342\226\215";E[4]="\342\226\214";E[5]="\342\226\213";E[6]="\342\226\212";E[7]="\342\226\211"; s=s E[rem]; full++ }
      for(i=full;i<w;i++) s=s "\342\226\221"            # light shade
      return s
    }
    function col(c,t){ return sprintf("\033[38;5;%dm%s\033[0m",c,t) }
    function gib(kb){ return sprintf("%.1f",kb/1048576) }
    function jesc(s){ gsub(/\\/,"\\\\",s); gsub(/"/,"\\\"",s); return s }
    BEGIN{
      ORANGE=215; CYAN=117; GOLD=178; DIM=240; GREEN=114
      pal[0]=215; pal[1]=117; pal[2]=178; pal[3]=114; pal[4]=211; pal[5]=180; pal[6]=109
      n=0
    }
    {
      cpu[n]=$1+0; pss[n]=$2+0; lab[n]=$3; role[n]=($4==""?"Other":$4); n++
    }
    END{
      # Split rows: recognized components vs the single collapsed "Other" bucket.
      other_c=0; other_p=0; m=0
      for(i=0;i<n;i++){
        if(role[i]=="Other"){ other_c+=cpu[i]; other_p+=pss[i] }
        else { L[m]=lab[i]; C[m]=cpu[i]; P[m]=pss[i]; R[m]=role[i]; m++ }
      }
      # de-dupe identical labels among recognized apps (sonarr/sonarr2 share a
      # cmdline) -> append index. Done before Other is appended so it stays "Other".
      for(i=0;i<m;i++){ seen[L[i]]++; if(seen[L[i]]>1) L[i]=L[i] " " seen[L[i]] }
      if(other_c>0 || other_p>0){ L[m]="Other"; C[m]=other_c; P[m]=other_p; R[m]="Other"; m++ }

      # Stack totals — all derived from raw deltas so figures reconcile.
      mine_cores = (minecpu*1.0/totald)*ncpu          # our usage in whole-core units
      mine_box   = (minecpu*100.0)/totald             # our % of ALL cores (capacity, not tenants)
      mine_gib   = minepss/1048576.0
      mine_mem_box = (minepss*100.0)/memtot

      if(mode=="json"){
        printf "{"
        printf "\"schema_version\":1,"
        printf "\"captured_at\":\"%s\",", jesc(captured)
        printf "\"host\":\"%s\",", jesc(host)
        printf "\"ncpu\":%d,", ncpu
        printf "\"interval_s\":%d,", interval
        printf "\"stack\":{\"cpu_cores\":%.3f,\"cpu_pct_of_box\":%.3f,\"ram_gib\":%.3f,\"ram_gib_total\":%.1f,\"ram_pct_of_box\":%.3f},",
          mine_cores, mine_box, mine_gib, memtot/1048576.0, mine_mem_box
        printf "\"components\":["
        first=1
        for(i=0;i<m;i++){
          if(!first) printf ","
          first=0
          cores=(C[i]*1.0*ncpu)/totald
          cf = minecpu>0 ? C[i]*100.0/minecpu : 0
          pf = minepss>0 ? P[i]*100.0/minepss : 0
          printf "{\"label\":\"%s\",\"role\":\"%s\",\"cpu_cores\":%.3f,\"cpu_pct_of_stack\":%.1f,\"ram_gib\":%.3f,\"ram_pct_of_stack\":%.1f}",
            jesc(L[i]), jesc(R[i]), cores, cf, P[i]/1048576.0, pf
        }
        printf "]}\n"
        exit
      }

      # ---- human header (stack-only; no tenant comparison) ----
      printf "\033[1m%s\033[0m  %s@%s\n", "qflix stack", col(ORANGE,usern), host
      printf " CPU  %s cores  ·  %d total  ·  %s of box capacity\n",
        col(GOLD,sprintf("%.2f",mine_cores)), ncpu, col(DIM,sprintf("%.2f%%",mine_box))
      printf " RAM  %s GiB  ·  %d total  ·  %s of box capacity\n",
        col(GOLD,gib(minepss)), int(memtot/1048576), col(DIM,sprintf("%.2f%%",mine_mem_box))
      print ""

      # ---- component breakdown (bars = share of OUR stack) ----
      bw=int((cols-58)/2); if(bw<10) bw=10; if(bw>26) bw=26
      printf "\033[1m %-16s %-*s   %-*s\033[0m\n"," component",bw+22,"CPU  (bar: share of stack · cores)",bw+10,"RAM (share of stack)"

      if(view=="role" || view=="both"){
        delete rcpu; delete rpss; delete rlist
        for(i=0;i<m;i++){ rcpu[R[i]]+=C[i]; rpss[R[i]]+=P[i] }
        nr=0; for(r in rcpu) rlist[nr++]=r
        for(a=0;a<nr;a++) for(b=a+1;b<nr;b++) if(rcpu[rlist[b]]>rcpu[rlist[a]]){tmp=rlist[a];rlist[a]=rlist[b];rlist[b]=tmp}
        for(a=0;a<nr;a++){ r=rlist[a]
          if(view=="role") row(r, rcpu[r], rpss[r], bw, pal[a%7])
          else {  # both: role header, then its apps (input already cpu-sorted)
            printf "\033[1m %s\033[0m\n", col(GOLD,r)
            for(i=0;i<m;i++) if(R[i]==r){ printf "  "; row(L[i],C[i],P[i],bw,pal[a%7]) }
          }
        }
      } else {
        for(i=0;i<m;i++) row(L[i],C[i],P[i],bw,pal[i%7])
      }

      if(!once){
        printf "\n \033[38;5;240m[a]pp [r]ole [b]oth · [+/-] %ds · [q]uit\033[0m\n", interval
      }
    }
    function row(name,c,p,bw,color,   cf,pf,cores){
      cf = minecpu>0 ? c/minecpu : 0          # share of OUR stack (bar)
      pf = minepss>0 ? p/minepss : 0
      cores = (c*1.0*ncpu)/totald
      printf " %-16.16s %s %s   %s %s\n",
        name,
        col(color,bar(cf,bw)),
        sprintf("%5.2f cores %3.0f%%", cores, cf*100),
        col(color,bar(pf,bw)),
        sprintf("%5s GiB %3.0f%%", gib(p), pf*100)
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
