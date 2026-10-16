#!/usr/bin/env bash
# Installs the git pre-commit hook that runs scripts/privacy_check.py on every commit.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
cat > .git/hooks/pre-commit <<'HOOK'
#!/usr/bin/env bash
exec python3 scripts/privacy_check.py
HOOK
chmod +x .git/hooks/pre-commit
echo "pre-commit hook installed (privacy check)"
[ -f .privacy-patterns ] || echo "note: create .privacy-patterns (git-ignored) with one regex per line"
