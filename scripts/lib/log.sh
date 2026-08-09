#!/usr/bin/env bash
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[0;33m'; NC='\033[0m'
log_info()  { echo -e "${GREEN}[+]${NC} $*" >&2; }
log_warn()  { echo -e "${YELLOW}[!]${NC} $*" >&2; }
log_error() { echo -e "${RED}[x]${NC} $*" >&2; }
die() { log_error "$*"; exit 1; }
