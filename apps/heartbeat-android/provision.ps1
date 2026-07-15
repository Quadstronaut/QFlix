<#
.SYNOPSIS
    Provisions the Heartbeat Android app with its SSH key bundle.

.DESCRIPTION
    Workstation-run, idempotent. Pulls the dedicated ed25519 key that S2
    minted on the seedbox, pins the box's host key, writes the connection
    config, and pushes all three into the app's private storage via adb.
    As the final step it deletes the *private* key from the box - only the
    public half + the forced-command authorized_keys entry remain there,
    so after this script finishes, the phone is the only place the private
    key exists.

    Safe to re-run:
      - If the phone is already provisioned and the box-side private key is
        already gone (i.e. a previous run completed), this is a no-op.
      - If the box-side private key doesn't exist yet (S2 hasn't minted it)
        and the phone isn't provisioned either, this fails loudly instead of
        half-provisioning.

.NOTES
    Run this from the app root: apps/heartbeat-android/provision.ps1
    Requires: ssh, scp, ssh-keyscan on PATH (Git for Windows / OpenSSH
    client feature), adb on PATH, phone connected with USB debugging on,
    debug build of com.qflix.heartbeat already installed.
#>

param(
    [string]$BoxHost = "seedbox.example.com",
    [string]$BoxUser = "quadstronaut",
    [int]$BoxPort = 22,
    [string]$AppId = "com.qflix.heartbeat",
    [string]$BoxKeyPath = "~/.ssh/heartbeat_phone_ed25519"
)

$ErrorActionPreference = "Stop"

function Write-Step([string]$Message) {
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Write-Ok([string]$Message) {
    Write-Host "    OK: $Message" -ForegroundColor Green
}

function Fail([string]$Message) {
    Write-Host ""
    Write-Host "FAILED: $Message" -ForegroundColor Red
    exit 1
}

# Remote path helper: the box key path is given in ~/... shorthand for
# readability in this script's own output, but ssh/scp need it resolved
# against the login shell, so we let the *remote* shell expand it rather
# than trying to reproduce $HOME locally.
$remoteKeyPath = $BoxKeyPath
$remoteTarget = "$BoxUser@${BoxHost}:$remoteKeyPath"

Write-Step "Checking adb sees a device"
$devices = & adb devices | Select-String -Pattern "\tdevice$"
if (-not $devices) {
    Fail "No adb device found. Connect the Pixel 6 with USB debugging enabled and re-run."
}
Write-Ok "Device attached ($($devices.Count) device(s))."

Write-Step "Checking whether the phone is already provisioned"
# run-as fails loudly (non-zero exit) if the app isn't installed/debuggable
# or the file is absent - either way, "not provisioned" from our side.
& adb shell run-as $AppId sh -c "test -f files/provision/phone_key" 2>$null
$alreadyOnPhone = ($LASTEXITCODE -eq 0)

Write-Step "Checking whether the private key still exists on the box"
& ssh -p $BoxPort "$BoxUser@$BoxHost" "test -f $remoteKeyPath" 2>$null
$keyOnBox = ($LASTEXITCODE -eq 0)

if (-not $keyOnBox -and $alreadyOnPhone) {
    Write-Ok "Phone already has the provisioning bundle and the box-side private key is already gone."
    Write-Host ""
    Write-Host "Already provisioned. Nothing to do." -ForegroundColor Green
    exit 0
}

if (-not $keyOnBox -and -not $alreadyOnPhone) {
    Fail ("No private key on the box ($remoteKeyPath) and the phone isn't provisioned either. " + `
          "Run S2 (scripts/configure/71-heartbeat-status-install.sh) to mint the key first.")
}

Write-Ok "Box-side private key found at $remoteKeyPath - proceeding."

# --- Local staging area -----------------------------------------------
$stagingDir = Join-Path $env:TEMP "heartbeat-provision"
if (Test-Path $stagingDir) {
    Remove-Item -Recurse -Force $stagingDir
}
New-Item -ItemType Directory -Path $stagingDir | Out-Null

$localKeyPath = Join-Path $stagingDir "phone_key"
$localKnownHostPath = Join-Path $stagingDir "known_host"
$localConfigPath = Join-Path $stagingDir "config.json"

try {
    Write-Step "Copying the private key from the box to $stagingDir"
    & scp -P $BoxPort $remoteTarget $localKeyPath
    if ($LASTEXITCODE -ne 0) { Fail "scp of the private key failed." }
    Write-Ok "phone_key staged."

    Write-Step "Pinning the box's host key (ssh-keyscan)"
    $hostKeyLine = & ssh-keyscan -t ed25519 -p $BoxPort $BoxHost 2>$null | Where-Object { $_ -notmatch "^#" }
    if (-not $hostKeyLine) {
        Fail "ssh-keyscan returned no ed25519 host key for $BoxHost - is the box reachable?"
    }
    # ssh-keyscan prefixes the host with [host]:port when a non-default port
    # is used; the app's known_host pin must match whatever host string the
    # app connects with (see net/SshFetcher.kt), which is the bare hostname
    # from config.json. Normalize to that form.
    $hostKeyLine = $hostKeyLine -replace "^\[$([regex]::Escape($BoxHost))\]:\d+", $BoxHost
    Set-Content -Path $localKnownHostPath -Value $hostKeyLine -NoNewline
    Write-Ok "known_host pinned: $hostKeyLine"

    Write-Step "Writing config.json"
    $config = [ordered]@{
        host = $BoxHost
        port = $BoxPort
        user = $BoxUser
    }
    ($config | ConvertTo-Json -Compress) | Set-Content -Path $localConfigPath -NoNewline
    Write-Ok "config.json written."

    Write-Step "Pushing the bundle to the device (/data/local/tmp/hb-prov/)"
    & adb shell rm -rf /data/local/tmp/hb-prov 2>$null | Out-Null
    & adb shell mkdir -p /data/local/tmp/hb-prov
    if ($LASTEXITCODE -ne 0) { Fail "adb shell mkdir failed." }
    & adb push $localKeyPath /data/local/tmp/hb-prov/phone_key
    & adb push $localKnownHostPath /data/local/tmp/hb-prov/known_host
    & adb push $localConfigPath /data/local/tmp/hb-prov/config.json
    if ($LASTEXITCODE -ne 0) { Fail "adb push failed." }
    Write-Ok "Bundle pushed to device staging area."

    Write-Step "Moving the bundle into app-private storage ($AppId)"
    & adb shell run-as $AppId sh -c "mkdir -p files/provision && cp /data/local/tmp/hb-prov/* files/provision/ && chmod 600 files/provision/phone_key"
    if ($LASTEXITCODE -ne 0) {
        Fail "adb shell run-as copy failed. Is $AppId installed as a debug build on this device?"
    }
    Write-Ok "Bundle copied into filesDir/provision/ and phone_key locked to 600."

    Write-Step "Cleaning up the device staging area"
    & adb shell rm -rf /data/local/tmp/hb-prov
    Write-Ok "/data/local/tmp/hb-prov removed."
}
finally {
    Write-Step "Shredding local temp copies ($stagingDir)"
    if (Test-Path $stagingDir) {
        Remove-Item -Recurse -Force $stagingDir
    }
    Write-Ok "Local temp copies removed."
}

Write-Step "Deleting the private key from the box (pubkey + authorized_keys entry stay)"
& ssh -p $BoxPort "$BoxUser@$BoxHost" "rm -f $remoteKeyPath"
if ($LASTEXITCODE -ne 0) {
    Fail ("Bundle is on the phone, but deleting the box-side private key failed. " + `
          "Remove it manually: ssh $BoxUser@$BoxHost rm $remoteKeyPath")
}
Write-Ok "Box-side private key deleted. Key now exists only on the phone."

Write-Host ""
Write-Host "PROVISIONING OK - $AppId on-device, box private key removed." -ForegroundColor Green
