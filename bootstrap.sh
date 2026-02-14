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

# Update system and sync package database
update_system() {
    info "Updating system and syncing package database..."

    if [[ $PKG_MANAGER == "apt" ]]; then
        apt-get update
    elif [[ $PKG_MANAGER == "pacman" ]]; then
        pacman -Syu --noconfirm
    fi

    success "System updated"
}

# Install packages based on distro
install_packages() {
    local packages=("$@")

    if [[ $PKG_MANAGER == "apt" ]]; then
        apt-get install -y "${packages[@]}"
    elif [[ $PKG_MANAGER == "pacman" ]]; then
        pacman -S --needed --noconfirm "${packages[@]}"
    fi
}

# Install SSH server
install_ssh() {
    info "Installing and enabling SSH server..."

    if [[ $DISTRO_FAMILY == "debian" ]]; then
        install_packages openssh-server
        systemctl enable ssh
        systemctl start ssh
    elif [[ $DISTRO_FAMILY == "arch" ]]; then
        install_packages openssh
        systemctl enable sshd
        systemctl start sshd
    fi

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
            unzip jq tree \
            zsh

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
            zoxide \
            zsh
    fi

    success "Basic tools installed"
}

# Install mise (version manager)
install_mise() {
    info "Installing mise..."

    local user_home=$(getent passwd "$SUDO_USER" | cut -d: -f6)

    if [ -f "$user_home/.local/bin/mise" ]; then
        success "mise already installed"
    else
        # Install mise as the regular user
        if [ -n "$SUDO_USER" ]; then
            sudo -u "$SUDO_USER" bash -c 'curl https://mise.run | sh'
        else
            curl https://mise.run | sh
        fi
        success "mise installed"
    fi

    # Add mise activation to shell RC
    local user_shell=$(getent passwd "$SUDO_USER" | cut -d: -f7)
    local shell_rc=""

    if [[ "$user_shell" == *"zsh"* ]]; then
        shell_rc="$user_home/.zshrc"
    else
        shell_rc="$user_home/.bashrc"
    fi

    if [ -f "$shell_rc" ] && ! grep -q "mise activate" "$shell_rc" 2>/dev/null; then
        echo '' >> "$shell_rc"
        echo '# mise (version manager)' >> "$shell_rc"
        if [[ "$shell_rc" == *".zshrc"* ]]; then
            echo 'eval "$($HOME/.local/bin/mise activate zsh)"' >> "$shell_rc"
        else
            echo 'eval "$($HOME/.local/bin/mise activate bash)"' >> "$shell_rc"
        fi
        chown "$SUDO_USER:$SUDO_USER" "$shell_rc"
        success "Added mise activation to $shell_rc"
    fi

    # Install Python and uv via mise
    info "Installing Python 3.12 and uv via mise..."
    if [ -n "$SUDO_USER" ]; then
        sudo -u "$SUDO_USER" bash -c '$HOME/.local/bin/mise use -g python@3.12 uv@latest'
    else
        "$user_home/.local/bin/mise" use -g python@3.12 uv@latest
    fi
    success "Python 3.12 and uv installed via mise"
}

# Setup projects directory
setup_projects_dir() {
    if [ -z "$SUDO_USER" ]; then
        return
    fi

    local user_home=$(getent passwd "$SUDO_USER" | cut -d: -f6)
    local projects_dir="$user_home/projects"

    info "Setting up projects directory..."

    if [ ! -d "$projects_dir" ]; then
        mkdir -p "$projects_dir"
        chown "$SUDO_USER:$SUDO_USER" "$projects_dir"
        success "Created $projects_dir"
    else
        success "Projects directory already exists at $projects_dir"
    fi
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
            install_packages docker docker-compose
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
        success "User $SUDO_USER added to docker group"
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
            install_packages tailscale
        fi
        success "Tailscale installed"
    fi

    # Enable and start Tailscale
    info "Enabling and starting Tailscale service..."
    systemctl enable tailscaled
    systemctl start tailscaled
    success "Tailscale service enabled and started"
}

# Set Tailscale hostname
setup_server_hostname() {
    info "Setting Tailscale hostname to 'homeserver'..."
    tailscale set --hostname=homeserver
    success "Tailscale hostname set to 'homeserver'"
}

# Check external drive mount
check_media_mount() {
    info "Checking external drive mount at /mnt/media..."

    if mountpoint -q /mnt/media 2>/dev/null; then
        success "External drive is mounted at /mnt/media"
    else
        warn "External drive is NOT mounted at /mnt/media"
        warn "Jellyfin won't work until you mount your media drive"
    fi
}

# Setup environment file
setup_env_file() {
    local script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    local env_file="$script_dir/.env"
    local env_example="$script_dir/.env.example"

    info "Setting up environment file..."

    if [ -f "$env_file" ]; then
        success ".env file already exists"
    elif [ -f "$env_example" ]; then
        cp "$env_example" "$env_file"
        success "Created .env from template"
    else
        touch "$env_file"
        success "Created empty .env file"
    fi
}

# Prompt for API keys
prompt_api_keys() {
    local script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    local env_file="$script_dir/.env"

    echo ""
    echo -e "${YELLOW}Configure API keys (press Enter to skip any):${NC}"
    echo ""

    # Function to update or add key to .env
    update_env_key() {
        local key=$1
        local value=$2

        if [ -z "$value" ]; then
            return
        fi

        if grep -q "^${key}=" "$env_file" 2>/dev/null; then
            sed -i "s|^${key}=.*|${key}=${value}|" "$env_file"
        else
            echo "${key}=${value}" >> "$env_file"
        fi
    }

    read -p "ANTHROPIC_API_KEY: " anthropic_key
    if [ -n "$anthropic_key" ]; then
        update_env_key "ANTHROPIC_API_KEY" "$anthropic_key"
        success "Saved"
    fi

    read -p "DISCORD_BOT_TOKEN: " discord_token
    if [ -n "$discord_token" ]; then
        update_env_key "DISCORD_BOT_TOKEN" "$discord_token"
        success "Saved"
    fi

    read -p "DISCORD_CLIENT_ID: " discord_client_id
    if [ -n "$discord_client_id" ]; then
        update_env_key "DISCORD_CLIENT_ID" "$discord_client_id"
        success "Saved"
    fi

    read -p "TAILSCALE_AUTHKEY: " tailscale_key
    if [ -n "$tailscale_key" ]; then
        update_env_key "TAILSCALE_AUTHKEY" "$tailscale_key"
        success "Saved"
    fi
}

# Setup shell config for the user
setup_shell_config() {
    if [ -z "$SUDO_USER" ]; then
        return
    fi

    local script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    local user_home=$(getent passwd "$SUDO_USER" | cut -d: -f6)
    local aliases_file="$user_home/.aliases"
    local shell_rc=""

    # Detect user's shell
    local user_shell=$(getent passwd "$SUDO_USER" | cut -d: -f7)
    if [[ "$user_shell" == *"zsh"* ]]; then
        shell_rc="$user_home/.zshrc"
    else
        shell_rc="$user_home/.bashrc"
    fi

    info "Setting up shell config for $SUDO_USER..."

    # Copy aliases file
    if [ -f "$script_dir/dotfiles/aliases" ]; then
        cp "$script_dir/dotfiles/aliases" "$aliases_file"
        chown "$SUDO_USER:$SUDO_USER" "$aliases_file"
        success "Installed aliases to ~/.aliases"
    fi

    # Create shell rc if it doesn't exist
    if [ ! -f "$shell_rc" ]; then
        touch "$shell_rc"
    fi

    # Add zoxide init if not present (Arch only)
    if [[ $DISTRO_FAMILY == "arch" ]] && ! grep -q "zoxide init" "$shell_rc" 2>/dev/null; then
        echo '' >> "$shell_rc"
        echo '# Zoxide (smart cd)' >> "$shell_rc"
        if [[ "$shell_rc" == *".zshrc"* ]]; then
            echo 'eval "$(zoxide init zsh)"' >> "$shell_rc"
        else
            echo 'eval "$(zoxide init bash)"' >> "$shell_rc"
        fi
        echo 'alias cd="z"' >> "$shell_rc"
    fi

    # Source aliases file if not already
    if ! grep -q "source.*\.aliases" "$shell_rc" 2>/dev/null; then
        echo '' >> "$shell_rc"
        echo '# Load aliases' >> "$shell_rc"
        echo '[ -f ~/.aliases ] && source ~/.aliases' >> "$shell_rc"
    fi

    chown "$SUDO_USER:$SUDO_USER" "$shell_rc"
    success "Shell config updated"
}

# Connect Tailscale
connect_tailscale() {
    echo ""
    read -p "Connect to Tailscale now? [Y/n]: " connect_ts
    if [[ "$connect_ts" != "n" && "$connect_ts" != "N" ]]; then
        info "Starting Tailscale authentication..."
        tailscale up
        success "Tailscale connected"
    fi
}

# Start services
start_services() {
    local script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

    echo ""
    read -p "Start Docker services now? [Y/n]: " start_docker
    if [[ "$start_docker" != "n" && "$start_docker" != "N" ]]; then
        info "Starting services..."
        cd "$script_dir"

        # Need to run as the regular user for docker group
        if [ -n "$SUDO_USER" ]; then
            # Use newgrp trick or just run docker directly since we're root
            docker compose up -d
        else
            docker compose up -d
        fi

        success "Services started"
        echo ""
        docker compose ps
    fi
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

    update_system
    install_basic_tools
    install_mise
    install_ssh
    install_docker
    install_tailscale
    setup_server_hostname

    echo ""
    check_media_mount

    echo ""
    setup_env_file
    prompt_api_keys

    echo ""
    setup_shell_config
    setup_projects_dir

    connect_tailscale
    start_services

    echo ""
    echo -e "${GREEN}========================================${NC}"
    echo -e "${GREEN}   Bootstrap Complete!${NC}"
    echo -e "${GREEN}========================================${NC}"
    echo ""

    info "Services:"
    echo "  - OpenClaw:       http://localhost:18789"
    echo "  - Home Assistant: http://localhost:8123"
    echo "  - Jellyfin:       http://localhost:8096"
    echo ""

    if [ -n "$SUDO_USER" ]; then
        warn "Log out and back in to use docker without sudo"
    fi

    echo ""
}

main "$@"
