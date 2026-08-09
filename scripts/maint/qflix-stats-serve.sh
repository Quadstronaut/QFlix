#!/bin/bash
# Forced-command endpoint for the invite page's hourly stats pull.
# Ignores SSH_ORIGINAL_COMMAND entirely by design.
exec /usr/bin/python3 "$HOME/scripts/maint/qflix-stats.py"
