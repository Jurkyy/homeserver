#!/bin/bash
set -e

#######################################
# Remote Access Setup Script
# Run this on your DEV MACHINE (not the server)
# Sets up SSH access to homeserver via Tailscale
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

echo ""
echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}   Remote Access Setup (Dev Machine)${NC}"
echo -e "${BLUE}========================================${NC}"
echo ""

# 1. Check that Tailscale is installed and connected
info "Checking Tailscale status..."

if ! command -v tailscale &> /dev/null; then
    error "Tailscale is not installed on this machine"
    echo "  Install it from: https://tailscale.com/download"
    exit 1
fi

if ! tailscale status &> /dev/null; then
    error "Tailscale is not connected"
    echo "  Run: tailscale up"
    exit 1
fi

success "Tailscale is installed and connected"

# 2. Ask for the server's Tailscale hostname
echo ""
read -p "Server's Tailscale hostname [homeserver]: " TS_HOSTNAME
TS_HOSTNAME="${TS_HOSTNAME:-homeserver}"

info "Using hostname: $TS_HOSTNAME"

# 3. Generate SSH key pair if it doesn't exist
SSH_KEY="$HOME/.ssh/homeserver"

if [ -f "$SSH_KEY" ]; then
    success "SSH key already exists at $SSH_KEY"
else
    info "Generating SSH key pair..."
    mkdir -p "$HOME/.ssh"
    chmod 700 "$HOME/.ssh"
    ssh-keygen -t ed25519 -f "$SSH_KEY" -C "$(whoami)@$(hostname) -> homeserver"
    success "SSH key generated at $SSH_KEY"
fi

# 4. Add SSH config entry
SSH_CONFIG="$HOME/.ssh/config"

if [ -f "$SSH_CONFIG" ] && grep -q "^Host ${TS_HOSTNAME}$" "$SSH_CONFIG"; then
    warn "SSH config entry for '${TS_HOSTNAME}' already exists, skipping"
else
    info "Adding SSH config entry..."
    mkdir -p "$HOME/.ssh"

    # Add a newline if the file exists and doesn't end with one
    if [ -f "$SSH_CONFIG" ] && [ -s "$SSH_CONFIG" ]; then
        # Ensure there's a blank line before the new entry
        echo "" >> "$SSH_CONFIG"
    fi

    cat >> "$SSH_CONFIG" <<EOF
Host ${TS_HOSTNAME}
    HostName ${TS_HOSTNAME}
    User wolf
    IdentityFile ~/.ssh/homeserver
    ForwardAgent yes
    ServerAliveInterval 60
    ServerAliveCountMax 3
EOF

    chmod 600 "$SSH_CONFIG"
    success "SSH config entry added for '${TS_HOSTNAME}'"
fi

# 5. Copy public key to the server
echo ""
info "Copying public key to server..."
warn "You may be prompted for the server password (this is the last time)"
echo ""

ssh-copy-id -i "$SSH_KEY.pub" "wolf@${TS_HOSTNAME}"

success "Public key copied to server"

# 6. Test the connection
echo ""
info "Testing SSH connection..."

if ssh "$TS_HOSTNAME" "echo 'Connection successful!'"; then
    success "SSH connection to ${TS_HOSTNAME} works!"
else
    error "SSH connection test failed"
    exit 1
fi

# 7. Print next steps
echo ""
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}   Remote Access Setup Complete!${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""
info "You can now connect with:"
echo "  ssh ${TS_HOSTNAME}"
echo ""
info "Next steps:"
echo "  1. Harden SSH on the server (disables password auth):"
echo "     ssh ${TS_HOSTNAME} 'cd ~/homeserver && sudo ./scripts/harden-ssh.sh'"
echo ""
echo "  2. Add these aliases to your dev machine's shell config:"
echo "     alias hss='ssh ${TS_HOSTNAME}'"
echo "     alias hssync='rsync -avz --exclude .git --exclude __pycache__ --exclude .venv --exclude node_modules'"
echo ""
