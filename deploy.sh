#!/usr/bin/env bash
# deploy.sh — package nari, upload to the OCI VM, rebuild & restart the container, wait for health.
#
# Usage:  ./deploy.sh [--skip-tests] [--push-env] [--package-only] [-h]
#   --skip-tests    don't run the local pytest suite first
#   --push-env      upload local .env even if the server already has one (overwrites it)
#   --package-only  build the tarball locally and list it; no SSH (for checking)
#
# Target: NARI_REMOTE (e.g. opc@<server-ip>) from the environment or a git-ignored .deploy.env file.
# Other overrides (env vars): NARI_REMOTE_DIR (~/nari on the server),
#                       NARI_SSH_PORT (22), NARI_SSH_KEY (path to private key, optional)
#
# Adapted from zcare/deploy.sh. Works with macOS's bash 3.2. The image builds ON the server,
# so it matches the VM's CPU arch (aarch64).
set -euo pipefail

# Keep the server address out of the repo: read it from .deploy.env (git-ignored) if not exported.
if [ -z "${NARI_REMOTE:-}" ] && [ -f "$(dirname "${BASH_SOURCE[0]}")/.deploy.env" ]; then
  # shellcheck disable=SC1091
  . "$(dirname "${BASH_SOURCE[0]}")/.deploy.env"
fi
REMOTE="${NARI_REMOTE:-}"
REMOTE_DIR="${NARI_REMOTE_DIR:-nari}"          # relative = under the remote user's home
SSH_PORT="${NARI_SSH_PORT:-22}"
SSH_KEY="${NARI_SSH_KEY:-}"

SKIP_TESTS=0
PUSH_ENV=0
PACKAGE_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --skip-tests)   SKIP_TESTS=1 ;;
    --push-env)     PUSH_ENV=1 ;;
    --package-only) PACKAGE_ONLY=1 ;;
    -h|--help)      sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "Unknown option: $arg (see --help)" >&2; exit 2 ;;
  esac
done

# ---------------------------------------------------------------- output helpers
if [ -t 1 ]; then B=$'\033[1m'; G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; N=$'\033[0m'; else B=; G=; Y=; R=; N=; fi
step() { printf '\n%s==> %s%s\n' "$B" "$*" "$N"; }
ok()   { printf '%s✔ %s%s\n' "$G" "$*" "$N"; }
warn() { printf '%s! %s%s\n' "$Y" "$*" "$N"; }
die()  { printf '%s✘ %s%s\n' "$R" "$*" "$N" >&2; exit 1; }

[ -n "$REMOTE" ] || [ "$PACKAGE_ONLY" -eq 1 ] || {
  echo "Set the server first:  echo 'NARI_REMOTE=opc@<server-ip>' > .deploy.env" >&2; exit 2; }
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"
[ -f app/main.py ] && [ -f requirements.txt ] || die "Run from the nari project (app/main.py not found in $PROJECT_DIR)"
for cmd in tar ssh scp; do command -v "$cmd" >/dev/null || die "'$cmd' is required"; done

STAMP="$(date +%Y%m%d-%H%M%S)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/nari-deploy.XXXXXX")"
TARBALL="$WORK/nari-$STAMP.tar.gz"
CONTROL_PATH="$WORK/ssh-ctl"
cleanup() {
  [ -n "$REMOTE" ] && ssh -o ControlPath="$CONTROL_PATH" -O exit "$REMOTE" >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT

# ---------------------------------------------------------------- 1. local checks
step "1/4 Local checks & packaging"
if pgrep -f "app\.main" >/dev/null 2>&1; then
  warn "a local nari bot is running (pid $(pgrep -f 'app\.main' | tr '\n' ' ')) — it will fight the server for Telegram updates. Stop it."
fi
if [ "$SKIP_TESTS" -eq 1 ]; then
  warn "skipping tests (--skip-tests)"
elif [ -x .venv/bin/python ] && .venv/bin/python -c "import pytest" 2>/dev/null; then
  .venv/bin/python -m pytest -q tests >"$WORK/pytest.log" 2>&1 || { tail -30 "$WORK/pytest.log"; die "tests failed — fix them or use --skip-tests"; }
  ok "tests passed ($(tail -1 "$WORK/pytest.log"))"
else
  warn "no .venv with pytest found; skipping tests (create one: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt pytest)"
fi

EXCLUDES=(
  --exclude=.venv --exclude=.git --exclude=.env --exclude=.claude --exclude=.idea --exclude=.vscode
  --exclude=__pycache__ --exclude='*.pyc' --exclude=.pytest_cache --exclude=.DS_Store
  --exclude=./data --exclude=./tests --exclude=./samples --exclude='*.log' --exclude='*.tar.gz'
  --exclude=./deploy.sh --exclude=.deploy.env --exclude='.env.*' --exclude=./spec.md
)
TAR_FLAGS=()
if tar --version 2>/dev/null | grep -qi bsdtar; then
  export COPYFILE_DISABLE=1                      # no macOS ._AppleDouble files
  TAR_FLAGS=(--no-mac-metadata --no-xattrs)      # avoid "unknown extended header" warnings on Linux
fi
tar ${TAR_FLAGS[@]+"${TAR_FLAGS[@]}"} "${EXCLUDES[@]}" -czf "$TARBALL" .
ok "packaged $(tar -tzf "$TARBALL" | grep -vc '/$') files ($(du -h "$TARBALL" | cut -f1 | tr -d ' '))"

if [ "$PACKAGE_ONLY" -eq 1 ]; then
  tar -tzf "$TARBALL" | grep -v '/$' | sed 's#^\./#  #'
  cp "$TARBALL" "$PROJECT_DIR/"
  ok "--package-only: wrote $(basename "$TARBALL"); nothing was uploaded"
  exit 0
fi

# ---------------------------------------------------------------- 2. SSH
SSH_OPTS=(-p "$SSH_PORT" -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new -o LogLevel=ERROR
          -o ControlMaster=auto -o ControlPath="$CONTROL_PATH" -o ControlPersist=120)
SCP_OPTS=(-P "$SSH_PORT" -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new -o LogLevel=ERROR
          -o ControlPath="$CONTROL_PATH")
if [ -n "$SSH_KEY" ]; then SSH_OPTS+=(-i "$SSH_KEY" -o IdentitiesOnly=yes); SCP_OPTS+=(-i "$SSH_KEY" -o IdentitiesOnly=yes); fi

step "2/4 Connecting to $REMOTE"
ssh "${SSH_OPTS[@]}" -o BatchMode=yes "$REMOTE" true 2>"$WORK/ssh.err" || {
  cat "$WORK/ssh.err" >&2
  if grep -qiE "timed out|refused|no route|could not resolve" "$WORK/ssh.err"; then
    die "cannot reach $REMOTE:$SSH_PORT — check the VM is running and its security list allows SSH"
  fi
  die "key-based SSH failed. Set NARI_SSH_KEY in .deploy.env to the private key the VM accepts."
}
ok "key-based SSH works"
remote() { ssh "${SSH_OPTS[@]}" "$REMOTE" "$@"; }

remote 'command -v docker >/dev/null && docker compose version >/dev/null 2>&1' || die "Docker + Compose v2 not found on the server.
  Oracle Linux 9:
    sudo dnf config-manager --add-repo https://download.docker.com/linux/rhel/docker-ce.repo
    sudo dnf install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    sudo systemctl enable --now docker && sudo usermod -aG docker \$USER"
ok "docker compose available on server"

REMOTE_HAS_ENV="$(remote "test -f $REMOTE_DIR/.env && echo yes || echo no")"
UPLOAD_ENV=0
if [ "$REMOTE_HAS_ENV" = "no" ]; then
  [ -f .env ] || die "no .env on the server and none locally — create one from .env.example first"
  UPLOAD_ENV=1; warn "server has no .env — will upload your local .env"
elif [ "$PUSH_ENV" -eq 1 ]; then
  [ -f .env ] || die "--push-env given but no local .env"
  UPLOAD_ENV=1; warn "--push-env: server .env will be replaced by your local one"
else
  ok "keeping the server's existing .env (use --push-env to replace it)"
fi

# ---------------------------------------------------------------- 3. upload
step "3/4 Uploading"
REMOTE_TAR="/tmp/nari-$STAMP.tar.gz"
REMOTE_ENV="/tmp/nari-$STAMP.env"
scp -q "${SCP_OPTS[@]}" "$TARBALL" "$REMOTE:$REMOTE_TAR"
if [ "$UPLOAD_ENV" -eq 1 ]; then
  remote "umask 077 && : > '$REMOTE_ENV'"                 # create 0600 before secrets land in it
  scp -q "${SCP_OPTS[@]}" .env "$REMOTE:$REMOTE_ENV"
fi
ok "uploaded to $REMOTE:$REMOTE_TAR"

# ---------------------------------------------------------------- 4. remote build & start
step "4/4 Building & starting on server (first build takes a few minutes on 1 OCPU)"
remote "bash -s -- '$REMOTE_DIR' '$REMOTE_TAR' '$REMOTE_ENV' '$UPLOAD_ENV'" <<'REMOTE_SCRIPT'
set -euo pipefail
cd ~
dir="$1"; tarball="$2"; envfile="$3"; upload_env="$4"
release="${dir}.new"; previous="${dir}.prev"

rm -rf "$release"; mkdir -p "$release"
tar -xzf "$tarball" -C "$release"; rm -f "$tarball"
if [ "$upload_env" = "1" ]; then
  mv "$envfile" "$release/.env"
elif [ -f "$dir/.env" ]; then
  cp -p "$dir/.env" "$release/.env"
fi
chmod 600 "$release/.env"

# Swap releases; keep one previous copy for rollback. The compose project name is fixed ("nari"),
# so the container and the nari-data volume stay the same across deploys.
rm -rf "$previous"
[ -d "$dir" ] && mv "$dir" "$previous"
mv "$release" "$dir"
cd "$dir"

docker compose build

# The data volume is external (survives `docker compose down`); create it on first deploy.
if ! docker volume inspect nari-data >/dev/null 2>&1; then
  docker volume create nari-data >/dev/null
  # One-time migration from the old layout (bind-mounted ./data) into the volume.
  if [ -f "$HOME/$previous/data/nari.db" ]; then
    echo "Migrating $previous/data/nari.db into volume nari-data ..."
    docker stop nari >/dev/null 2>&1 || true          # consistent copy: old bot must not be writing
    docker run --rm --user 0 -v nari-data:/app/data -v "$HOME/$previous/data:/old:ro" --entrypoint sh nari:latest \
      -c 'cp -a /old/nari.db /app/data/ && chown -R 1000:1000 /app/data'
  fi
fi

# --force-recreate: config/ is bind-mounted from the release dir, which was just swapped; a container
# left running would keep reading ~/nari.prev/config (deleted on the next deploy).
if ! docker compose up -d --force-recreate --remove-orphans; then
  echo "START FAILED. Previous release kept at ~/$previous"
  echo "Rollback: cd ~/$previous && docker compose up -d --build"
  exit 1
fi

echo "Waiting for the bot heartbeat (Telegram connected + scheduler running) ..."
healthy=0
for _ in $(seq 1 45); do
  if docker compose exec -T nari python -c "import os,sys,time; sys.exit(0 if time.time()-os.path.getmtime('/tmp/nari-heartbeat')<120 else 1)" </dev/null >/dev/null 2>&1; then  # </dev/null: don't eat this script from stdin
    healthy=1; break
  fi
  sleep 2
done
docker compose ps
echo "----- last log lines -----"
docker compose logs --tail 15 nari 2>&1 | grep -v "apscheduler\|httpx" | sed -E 's/bot[0-9]+:[A-Za-z0-9_-]+/bot<TOKEN>/g'
if docker compose logs --since 2m nari 2>&1 | grep -q "Conflict: terminated by other getUpdates"; then
  echo "WARNING: Telegram reports another bot instance polling with this token (local copy still running?)."
fi
if [ "$healthy" != "1" ]; then
  echo "Bot did not become healthy within 90s (see logs above)."
  echo "Rollback: cd ~/$previous && docker compose up -d --build   (same project name: nari)"
  exit 1
fi
docker image prune -f >/dev/null 2>&1 || true
echo "DEPLOY_OK"
REMOTE_SCRIPT

ok "deployed to $REMOTE:~/$REMOTE_DIR"
printf '\nUseful:\n  ssh %s "cd %s && docker compose logs -f nari"\n  ssh %s "cd %s && docker compose restart"   # after editing config on the server\n' \
  "$REMOTE" "$REMOTE_DIR" "$REMOTE" "$REMOTE_DIR"
