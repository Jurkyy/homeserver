#!/bin/bash
set -e

#######################################
# Home Server Bootstrap Script
# Sets up a fresh home server with Docker,
# Tailscale, and essential tools.
#######################################

# Colors for output
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

# Check if running as root
check_root() {
    if [[ $EUID -ne 0 ]]; then
        error "This script must be run as root (or with sudo)"
        exit 1
    fi
}

# Detect distribution
detect_distro() {
    if [ -f /etc/os-release ]; then
        . /etc/os-release
        DISTRO=$ID
    elif [ -f /etc/debian_version ]; then
        DISTRO="debian"
    elif [ -f /etc/arch-release ]; then
        DISTRO="arch"
    else
        error "Unable to detect distribution"
        exit 1
    fi

    case $DISTRO in
        debian|ubuntu|linuxmint|pop)
            DISTRO_FAMILY="debian"
            PKG_MANAGER="apt"
            ;;
        arch|manjaro|endeavouros)
            DISTRO_FAMILY="arch"
            PKG_MANAGER="pacman"
            ;;
        *)
            error "Unsupported distribution: $DISTRO"
            exit 1
            ;;
    esac

    info "Detected distribution: $DISTRO (family: $DISTRO_FAMILY)"
}

# Install packages based on distro
install_packages() {
    local packages=("$@")

    if [[ $PKG_MANAGER == "apt" ]]; then
        apt-get update
        apt-get install -y "${packages[@]}"
    elif [[ $PKG_MANAGER == "pacman" ]]; then
        pacman -Sy --noconfirm "${packages[@]}"
    fi
}

# Install SSH server
install_ssh() {
    info "Installing and enabling SSH server..."

    if [[ $DISTRO_FAMILY == "debian" ]]; then
        install_packages openssh-server
    elif [[ $DISTRO_FAMILY == "arch" ]]; then
        install_packages openssh
    fi

    systemctl enable sshd
    systemctl start sshd
    success "SSH server installed and running"
}

# Install basic tools
install_basic_tools() {
    info "Installing basic tools..."

    if [[ $DISTRO_FAMILY == "debian" ]]; then
        install_packages \
            git curl wget \
            htop btop \
            neovim \
            bat fd-find ripgrep fzf \
            tmux \
            unzip jq tree

        # Create symlinks for tools with different names on Debian
        ln -sf /usr/bin/batcat /usr/local/bin/bat 2>/dev/null || true
        ln -sf /usr/bin/fdfind /usr/local/bin/fd 2>/dev/null || true

    elif [[ $DISTRO_FAMILY == "arch" ]]; then
        install_packages \
            git curl wget \
            htop btop \
            neovim \
            bat eza fd ripgrep fzf \
            tmux \
            unzip jq tree \
            zoxide
    fi

    success "Basic tools installed"
}

# Install Docker
install_docker() {
    info "Installing Docker..."

    if command -v docker &> /dev/null; then
        success "Docker already installed"
    else
        if [[ $DISTRO_FAMILY == "debian" ]]; then
            # Remove old versions
            apt-get remove -y docker docker-engine docker.io containerd runc 2>/dev/null || true

            # Install dependencies
            apt-get update
            apt-get install -y ca-certificates curl gnupg lsb-release

            # Add Docker's official GPG key
            install -m 0755 -d /etc/apt/keyrings
            if [ ! -f /etc/apt/keyrings/docker.gpg ]; then
                curl -fsSL https://download.docker.com/linux/$DISTRO/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
                chmod a+r /etc/apt/keyrings/docker.gpg
            fi

            # Set up the repository
            echo \
                "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/$DISTRO \
                $(lsb_release -cs) stable" | tee /etc/apt/sources.list.d/docker.list > /dev/null

            # Install Docker
            apt-get update
            apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

        elif [[ $DISTRO_FAMILY == "arch" ]]; then
            pacman -Sy --noconfirm docker docker-compose
        fi

        success "Docker installed"
    fi

    # Start and enable Docker service
    info "Enabling and starting Docker service..."
    systemctl enable docker
    systemctl start docker
    success "Docker service enabled and started"

    # Add current user to docker group
    if [ -n "$SUDO_USER" ]; then
        info "Adding user $SUDO_USER to docker group..."
        usermod -aG docker "$SUDO_USER"
        success "User $SUDO_USER added to docker group (re-login required)"
    fi
}

# Install Tailscale
install_tailscale() {
    info "Installing Tailscale..."

    if command -v tailscale &> /dev/null; then
        success "Tailscale already installed"
    else
        if [[ $DISTRO_FAMILY == "debian" ]]; then
            curl -fsSL https://tailscale.com/install.sh | sh
        elif [[ $DISTRO_FAMILY == "arch" ]]; then
            pacman -Sy --noconfirm tailscale
        fi
        success "Tailscale installed"
    fi

    # Enable and start Tailscale
    info "Enabling and starting Tailscale service..."
    systemctl enable tailscaled
    systemctl start tailscaled
    success "Tailscale service enabled and started"
}

# Check external drive mount
check_media_mount() {
    info "Checking external drive mount at /mnt/media..."

    if mountpoint -q /mnt/media 2>/dev/null; then
        success "External drive is mounted at /mnt/media"
    else
        warn "External drive is NOT mounted at /mnt/media"
        warn "Please mount your external drive before running services that require it"
    fi
}

# Setup environment file
setup_env_file() {
    local script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    local env_file="$script_dir/.env"
    local env_example="$script_dir/.env.example"

    info "Setting up environment file..."

    if [ -f "$env_file" ]; then
        success ".env file already exists, skipping copy"
    elif [ -f "$env_example" ]; then
        cp "$env_example" "$env_file"
        success "Copied .env.example to .env"
    else
        info "Creating new .env file..."
        touch "$env_file"
        success "Created new .env file"
    fi
}

# Prompt for API keys
prompt_api_keys() {
    local script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    local env_file="$script_dir/.env"

    info "Configuring API keys..."
    echo ""
    echo -e "${YELLOW}You will be prompted for API keys. Press Enter to skip any key.${NC}"
    echo ""

    # Function to update or add key to .env
    update_env_key() {
        local key=$1
        local value=$2

        if [ -z "$value" ]; then
            return
        fi

        if grep -q "^${key}=" "$env_file" 2>/dev/null; then
            # Update existing key
            sed -i "s|^${key}=.*|${key}=${value}|" "$env_file"
        else
            # Add new key
            echo "${key}=${value}" >> "$env_file"
        fi
    }

    # ANTHROPIC_API_KEY or OPENAI_API_KEY
    read -p "Enter ANTHROPIC_API_KEY (or press Enter to skip): " anthropic_key
    if [ -n "$anthropic_key" ]; then
        update_env_key "ANTHROPIC_API_KEY" "$anthropic_key"
        success "ANTHROPIC_API_KEY saved"
    else
        read -p "Enter OPENAI_API_KEY (or press Enter to skip): " openai_key
        if [ -n "$openai_key" ]; then
            update_env_key "OPENAI_API_KEY" "$openai_key"
            success "OPENAI_API_KEY saved"
        fi
    fi

    # DISCORD_BOT_TOKEN
    read -p "Enter DISCORD_BOT_TOKEN (or press Enter to skip): " discord_token
    if [ -n "$discord_token" ]; then
        update_env_key "DISCORD_BOT_TOKEN" "$discord_token"
        success "DISCORD_BOT_TOKEN saved"
    fi

    # DISCORD_CLIENT_ID
    read -p "Enter DISCORD_CLIENT_ID (or press Enter to skip): " discord_client_id
    if [ -n "$discord_client_id" ]; then
        update_env_key "DISCORD_CLIENT_ID" "$discord_client_id"
        success "DISCORD_CLIENT_ID saved"
    fi

    # TAILSCALE_AUTHKEY
    read -p "Enter TAILSCALE_AUTHKEY (or press Enter to skip): " tailscale_key
    if [ -n "$tailscale_key" ]; then
        update_env_key "TAILSCALE_AUTHKEY" "$tailscale_key"
        success "TAILSCALE_AUTHKEY saved"
    fi

    echo ""
    success "API key configuration complete"
}

# Main execution
main() {
    echo ""
    echo -e "${BLUE}========================================${NC}"
    echo -e "${BLUE}   Home Server Bootstrap Script${NC}"
    echo -e "${BLUE}========================================${NC}"
    echo ""

    check_root
    detect_distro

    echo ""
    info "Starting installation..."
    echo ""

    install_basic_tools
    install_ssh
    install_docker
    install_tailscale

    echo ""
    check_media_mount

    echo ""
    setup_env_file
    prompt_api_keys

    echo ""
    echo -e "${GREEN}========================================${NC}"
    echo -e "${GREEN}   Bootstrap Complete!${NC}"
    echo -e "${GREEN}========================================${NC}"
    echo ""

    if [ -n "$SUDO_USER" ]; then
        warn "Please log out and back in for docker group changes to take effect"
    fi

    info "Next steps:"
    echo "  1. Review and update .env file as needed"
    echo "  2. Connect Tailscale: sudo tailscale up"
    echo "  3. Mount external drive to /mnt/media if needed"
    echo "  4. Run docker compose to start services"
    echo ""
}

main "$@"
