#!/bin/bash
set -e

#######################################
# Project Deploy Script (runs on DEV machine)
# Deploys a local project to the homeserver
# and optionally sets up a systemd service.
#
# Usage: ./scripts/deploy.sh <project-dir> [--service] [--host <name>]
#######################################

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Helper functions
info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

warn() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Change to script directory so relative paths work
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$SCRIPT_DIR/.."

# Defaults
SSH_HOST="homeserver"
INSTALL_SERVICE=false
PROJECT_DIR=""

# Parse arguments
while [[ "$#" -gt 0 ]]; do
    case $1 in
        --service|-s) INSTALL_SERVICE=true ;;
        --host|-h)
            shift
            SSH_HOST="$1"
            ;;
        --help)
            echo "Usage: ./scripts/deploy.sh <project-dir> [--service] [--host <name>]"
            echo ""
            echo "Arguments:"
            echo "  <project-dir>    Path to the local project directory to deploy"
            echo ""
            echo "Options:"
            echo "  --service, -s    Install and start systemd service (requires service template)"
            echo "  --host, -h       SSH host (default: homeserver, from ~/.ssh/config)"
            echo "  --help           Show this help message"
            exit 0
            ;;
        *)
            if [ -z "$PROJECT_DIR" ]; then
                PROJECT_DIR="$1"
            else
                error "Unknown parameter: $1"
                exit 1
            fi
            ;;
    esac
    shift
done

# Validate project directory
if [ -z "$PROJECT_DIR" ]; then
    error "No project directory specified"
    echo "Usage: ./scripts/deploy.sh <project-dir> [--service] [--host <name>]"
    exit 1
fi

# Resolve to absolute path
PROJECT_DIR="$(cd "$PROJECT_DIR" && pwd)"

if [ ! -d "$PROJECT_DIR" ]; then
    error "Project directory not found: $PROJECT_DIR"
    exit 1
fi

# Extract project name from directory
PROJECT_NAME="$(basename "$PROJECT_DIR")"
REMOTE_DIR="~/projects/${PROJECT_NAME}"

echo ""
echo -e "${BLUE}=== Project Deploy ===${NC}"
echo ""
info "Project:     ${PROJECT_NAME}"
info "Source:      ${PROJECT_DIR}"
info "Destination: ${SSH_HOST}:${REMOTE_DIR}"
info "Service:     ${INSTALL_SERVICE}"
echo ""

# Step 1: Ensure remote projects directory exists
info "Ensuring remote directory exists..."
ssh "$SSH_HOST" "mkdir -p ${REMOTE_DIR}"

# Step 2: Rsync project to server
info "Syncing project files..."
rsync -avz --delete \
    --exclude '.git' \
    --exclude '__pycache__' \
    --exclude '.venv' \
    --exclude 'node_modules' \
    --exclude '*.db' \
    --exclude 'data/' \
    "${PROJECT_DIR}/" "${SSH_HOST}:${REMOTE_DIR}/"

success "Files synced to server"

# Step 3: SSH to server and set up the project
info "Setting up project on server..."
ssh "$SSH_HOST" bash -s "$PROJECT_NAME" "$REMOTE_DIR" <<'SETUP_SCRIPT'
    set -e
    PROJECT_NAME="$1"
    REMOTE_DIR="$2"

    # Expand tilde
    REMOTE_DIR="${REMOTE_DIR/#\~/$HOME}"

    cd "$REMOTE_DIR"

    # Check if mise is installed
    if ! command -v mise &> /dev/null && [ ! -f "$HOME/.local/bin/mise" ]; then
        echo "[ERROR] mise is not installed on this server."
        echo ""
        echo "Install it with:"
        echo "  curl https://mise.run | sh"
        echo ""
        echo "Then add to your shell RC:"
        echo "  echo 'eval \"\$(~/.local/bin/mise activate bash)\"' >> ~/.bashrc"
        echo ""
        echo "Or run the homeserver bootstrap.sh which installs mise automatically."
        exit 1
    fi

    # Ensure mise is on PATH
    export PATH="$HOME/.local/bin:$PATH"

    # Run mise install to set up tool versions
    echo "[INFO] Running mise install..."
    mise install --yes 2>&1 || true

    # Run setup task if available, otherwise fall back to uv sync
    if mise task ls 2>/dev/null | grep -q "setup"; then
        echo "[INFO] Running mise run setup..."
        mise run setup
    elif [ -f "pyproject.toml" ]; then
        echo "[INFO] Running uv sync..."
        if command -v uv &> /dev/null || [ -f "$HOME/.local/bin/uv" ]; then
            export PATH="$HOME/.local/share/mise/shims:$PATH"
            uv sync
        else
            echo "[WARNING] uv not found, skipping dependency install"
        fi
    elif [ -f "package.json" ]; then
        echo "[INFO] Running npm install..."
        npm install
    fi

    echo "[SUCCESS] Project setup complete"
SETUP_SCRIPT

success "Project setup complete on server"

# Step 4: Install systemd service if requested
if [ "$INSTALL_SERVICE" = true ]; then
    echo ""
    SERVICE_FILE="${REPO_DIR}/services/projects/${PROJECT_NAME}.service"

    if [ ! -f "$SERVICE_FILE" ]; then
        error "Service template not found: ${SERVICE_FILE}"
        warn "Create a service file at services/projects/${PROJECT_NAME}.service first"
        exit 1
    fi

    info "Installing systemd service..."

    # Copy service file to server
    scp "$SERVICE_FILE" "${SSH_HOST}:/tmp/${PROJECT_NAME}.service"

    # Install and start service on server
    ssh "$SSH_HOST" bash -s "$PROJECT_NAME" <<'SERVICE_SCRIPT'
        set -e
        PROJECT_NAME="$1"

        sudo cp "/tmp/${PROJECT_NAME}.service" "/etc/systemd/system/${PROJECT_NAME}.service"
        rm "/tmp/${PROJECT_NAME}.service"

        sudo systemctl daemon-reload
        sudo systemctl enable "${PROJECT_NAME}"
        sudo systemctl restart "${PROJECT_NAME}"

        echo ""
        echo "[SUCCESS] Service ${PROJECT_NAME} installed and started"
SERVICE_SCRIPT

    success "Service ${PROJECT_NAME} installed and started"
fi

# Step 5: Print status and useful commands
echo ""
echo -e "${GREEN}=== Deploy Complete ===${NC}"
echo ""
info "Useful commands:"
echo "  ssh ${SSH_HOST} 'systemctl status ${PROJECT_NAME}'"
echo "  ssh ${SSH_HOST} 'journalctl -u ${PROJECT_NAME} -f'"
echo "  ssh ${SSH_HOST} 'sudo systemctl restart ${PROJECT_NAME}'"
echo ""
