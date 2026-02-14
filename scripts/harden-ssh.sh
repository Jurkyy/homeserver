#!/bin/bash
set -e

#######################################
# SSH Hardening Script
# Run this on the SERVER after setting up
# remote access with setup-remote.sh
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

SSHD_CONFIG="/etc/ssh/sshd_config"

echo ""
echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}   SSH Hardening${NC}"
echo -e "${BLUE}========================================${NC}"
echo ""

# Check if running as root
if [[ $EUID -ne 0 ]]; then
    error "This script must be run as root (or with sudo)"
    exit 1
fi

# 1. Check that at least one authorized_keys entry exists
AUTHORIZED_KEYS=""
if [ -n "$SUDO_USER" ]; then
    USER_HOME=$(getent passwd "$SUDO_USER" | cut -d: -f6)
    AUTHORIZED_KEYS="$USER_HOME/.ssh/authorized_keys"
else
    AUTHORIZED_KEYS="$HOME/.ssh/authorized_keys"
fi

info "Checking for authorized SSH keys..."

if [ ! -f "$AUTHORIZED_KEYS" ] || [ ! -s "$AUTHORIZED_KEYS" ]; then
    error "No authorized_keys found at $AUTHORIZED_KEYS"
    echo ""
    echo "  You MUST set up SSH keys before hardening, or you will be locked out!"
    echo "  Run setup-remote.sh on your dev machine first."
    echo ""
    exit 1
fi

KEY_COUNT=$(wc -l < "$AUTHORIZED_KEYS")
success "Found $KEY_COUNT authorized key(s) in $AUTHORIZED_KEYS"

# 2. Back up sshd_config
info "Backing up sshd_config..."
BACKUP="${SSHD_CONFIG}.backup.$(date +%Y%m%d_%H%M%S)"
cp "$SSHD_CONFIG" "$BACKUP"
success "Backup saved to $BACKUP"

# 3. Set PasswordAuthentication no
info "Disabling password authentication..."
if grep -q "^PasswordAuthentication no" "$SSHD_CONFIG"; then
    success "PasswordAuthentication already set to no"
elif grep -q "^#\?PasswordAuthentication" "$SSHD_CONFIG"; then
    sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' "$SSHD_CONFIG"
    success "PasswordAuthentication set to no"
else
    echo "PasswordAuthentication no" >> "$SSHD_CONFIG"
    success "PasswordAuthentication no appended to config"
fi

# 4. Set PermitRootLogin no
info "Disabling root login..."
if grep -q "^PermitRootLogin no" "$SSHD_CONFIG"; then
    success "PermitRootLogin already set to no"
elif grep -q "^#\?PermitRootLogin" "$SSHD_CONFIG"; then
    sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin no/' "$SSHD_CONFIG"
    success "PermitRootLogin set to no"
else
    echo "PermitRootLogin no" >> "$SSHD_CONFIG"
    success "PermitRootLogin no appended to config"
fi

# 5. Restart sshd
info "Restarting SSH daemon..."
if systemctl is-active --quiet sshd; then
    systemctl restart sshd
    success "sshd restarted"
elif systemctl is-active --quiet ssh; then
    systemctl restart ssh
    success "ssh restarted"
else
    warn "Could not determine SSH service name, please restart manually"
fi

# 6. Warnings
echo ""
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}   SSH Hardening Complete!${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""
warn "Password authentication is now DISABLED"
warn "Root login is now DISABLED"
echo ""
info "Only key-based authentication will work from now on"
info "If you have a firewall (ufw/iptables), ensure port 22 is open:"
echo "  sudo ufw allow ssh"
echo ""
info "To revert, restore the backup:"
echo "  sudo cp $BACKUP $SSHD_CONFIG"
echo "  sudo systemctl restart sshd"
echo ""
