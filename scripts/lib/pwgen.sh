#!/usr/bin/env bash
pwgen_24() { openssl rand -base64 24 | tr -d '+/=\n' | head -c 24; printf '\n'; }
