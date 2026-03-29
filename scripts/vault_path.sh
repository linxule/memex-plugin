#!/bin/bash
# Print the memex vault path. No dependencies — reads ~/.memex/config.json directly.
# Usage: VAULT=$(~/.memex/vault_path.sh)  or  VAULT=$(path/to/vault_path.sh)
python3 -c "import json; print(json.load(open('$HOME/.memex/config.json'))['memex_path'])" 2>/dev/null || echo ""
