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
    Requires: ssh, scp, ssh-keygen on PATH (Git for Windows / OpenSSH
    client feature - NOT ssh-keyscan; the box's host key is pinned via an
    authenticated ssh channel, not unauthenticated TOFU), adb on PATH,
    phone connected with USB debugging on, debug build of
    com.qflix.heartbeat already installed.
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
# or any file is absent - either way, "not provisioned" from our side. All
# three files must be present, matching Provisioning.kt's isProvisioned()
# exactly (net/Provisioning.kt: keyFile && configFile && knownHostFile) -
# checking phone_key alone would report "already provisioned" on a partial
# bundle (e.g. an earlier run whose adb push of known_host or config.json
# failed) and skip re-pushing, leaving the app permanently stuck on
# "Device not provisioned" with no automated recovery path.
& adb shell run-as $AppId sh -c "test -f files/provision/phone_key -a -f files/provision/known_host -a -f files/provision/config.json" 2>$null
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

    Write-Step "Pinning the box's host key (authenticated fetch, NOT ssh-keyscan)"
    # ssh-keyscan is unauthenticated TOFU - it accepts whatever key answers
    # on the wire with no cross-check against anything the operator already
    # trusts. This pin becomes the phone's PERMANENT, no-prompt-no-fallback
    # trust anchor (see net/SshFetcher.kt's OpenSSHKnownHosts strict-pin
    # docstring), so a MITM during this one-time provisioning step could
    # otherwise plant a forged key that every future connection silently
    # trusts. Instead, read the box's own host key file over the same
    # authenticated ssh channel already used for the scp/ssh calls above and
    # below (operator's own known_hosts enforces THAT channel's trust).
    $hostKeyLine = $null

    $pubKeyRaw = & ssh -p $BoxPort "$BoxUser@$BoxHost" "cat /etc/ssh/ssh_host_ed25519_key.pub" 2>$null
    if ($LASTEXITCODE -eq 0 -and $pubKeyRaw) {
        # ssh_host_ed25519_key.pub is "<type> <base64> [comment]"; the
        # known_host format sshj's OpenSSHKnownHosts expects is
        # "<host> <type> <base64>" - drop any comment field.
        $firstLine = @($pubKeyRaw)[0]
        $parts = $firstLine -split '\s+'
        if ($parts.Count -ge 2) {
            $hostKeyLine = "$BoxHost $($parts[0]) $($parts[1])"
        }
    }

    if (-not $hostKeyLine) {
        # Read-only diagnostic (still the authenticated channel) to tell a
        # genuinely-missing file apart from some other 'cat' failure, so the
        # operator gets a precise reason rather than a silent fallback.
        & ssh -p $BoxPort "$BoxUser@$BoxHost" "ls /etc/ssh/ssh_host_ed25519_key.pub" 2>$null | Out-Null
        $pathExists = ($LASTEXITCODE -eq 0)
        if ($pathExists) {
            Fail ("/etc/ssh/ssh_host_ed25519_key.pub exists on the box but 'cat' of it over SSH did not return a " + `
                  "usable key line - investigate manually (refusing to fall back silently past a readable-but-unparseable key).")
        }
        Write-Host "    /etc/ssh/ssh_host_ed25519_key.pub not found on the box - falling back to the operator's own known_hosts (ssh-keygen -F)." -ForegroundColor Yellow

        # Fallback: the fingerprint the operator's own OpenSSH client already
        # trusts for this host, from prior interactive/manual use - still
        # the operator's own established trust, never a fresh unauthenticated
        # probe of the wire.
        $fallbackLines = & ssh-keygen -F $BoxHost
        if ($LASTEXITCODE -eq 0 -and $fallbackLines) {
            foreach ($line in @($fallbackLines)) {
                if ($line -match '^#') { continue }
                $fields = $line -split '\s+'
                # ssh-keygen -F prints "<host> <type> <base64> [comment]"
                # (host may be hashed if HashKnownHosts is on - doesn't
                # matter, we rewrite it to the bare hostname below anyway).
                if ($fields.Count -ge 3 -and $fields[1] -eq "ssh-ed25519") {
                    $hostKeyLine = "$BoxHost $($fields[1]) $($fields[2])"
                    break
                }
            }
        }
    }

    if (-not $hostKeyLine) {
        Fail ("Could not obtain the box's ed25519 host key via either the authenticated " + `
              "'cat /etc/ssh/ssh_host_ed25519_key.pub' channel or the operator's own known_hosts " + `
              "(ssh-keygen -F $BoxHost). Refusing to fall back to unauthenticated ssh-keyscan - verify the " + `
              "box's host key manually, fix whichever path is broken, then re-run.")
    }

    Set-Content -Path $localKnownHostPath -Value $hostKeyLine -NoNewline
    Write-Ok "known_host pinned (authenticated): $hostKeyLine"

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
    # adb push reports success on stderr; under PS5.1 strict mode that becomes a
    # terminating NativeCommandError. Merge streams inside cmd.exe instead.
    foreach ($pair in @(
        @($localKeyPath,       "phone_key"),
        @($localKnownHostPath, "known_host"),
        @($localConfigPath,    "config.json"))) {
        cmd /c "adb push `"$($pair[0])`" /data/local/tmp/hb-prov/$($pair[1]) 2>&1" | Out-Null
        if ($LASTEXITCODE -ne 0) { Fail "adb push $($pair[1]) failed." }
    }
    Write-Ok "Bundle pushed to device staging area."

    Write-Step "Moving the bundle into app-private storage ($AppId)"
    # Whole remote command as ONE argument, sh -c payload single-quoted, so the
    # && chain executes inside run-as (not in the outer adb shell).
    & adb shell "run-as $AppId sh -c 'mkdir -p files/provision && cp /data/local/tmp/hb-prov/* files/provision/ && chmod 600 files/provision/phone_key'"
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
