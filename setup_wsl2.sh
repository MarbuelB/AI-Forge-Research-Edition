#!/usr/bin/env bash
# ==============================================================================
# AI-Forge-Research-Edition: Automated WSL2 & Environment Setup Script
# ==============================================================================
# This script automates the complete provisioning of a fresh WSL2 instance:
#   Phase 1 (System/Root): Packages, NVIDIA GPU CDI, Agent user, subuid/gid, wsl.conf
#   Phase 2 (User/Agent):  Pixi, Workspace env, LiteLLM Proxy, Podman build, Self-test
#
# Usage:
#   sudo ./setup_wsl2.sh           # Full setup from fresh WSL2 (runs Phase 1 -> Phase 2)
#   ./setup_wsl2.sh --user-only    # User-space only (Pixi, LiteLLM, Container build)
#   ./setup_wsl2.sh --check        # Non-destructive environment diagnostics
#   ./setup_wsl2.sh --build-image  # Rebuild the rootless Podman container image
#   ./setup_wsl2.sh --help         # Show all available options
# ==============================================================================

set -eo pipefail

# --- Color formatting ---
BOLD="\033[1m"
GREEN="\033[0;32m"
YELLOW="\033[0;33m"
RED="\033[0;31m"
BLUE="\033[0;34m"
CYAN="\033[0;36m"
RESET="\033[0m"

log_info()  { echo -e "${BLUE}[INFO]${RESET} $1"; }
log_step()  { echo -e "\n${BOLD}${CYAN}==>${RESET} ${BOLD}$1${RESET}"; }
log_done()  { echo -e "${GREEN}[✓]${RESET} $1"; }
log_warn()  { echo -e "${YELLOW}[WARN]${RESET} $1"; }
log_err()   { echo -e "${RED}[ERROR]${RESET} $1" >&2; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_USER="agent"
CUSTOM_USER=""
IMAGE_NAME="ai-forge"
AUTO_CONFIRM=false

# Detect calling user if run with sudo
if [ -n "$SUDO_USER" ] && [ "$SUDO_USER" != "root" ]; then
    CALLING_USER="$SUDO_USER"
else
    CALLING_USER="$(whoami)"
fi

# --- Parse Arguments ---
MODE="auto"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --check)
            MODE="check"
            shift
            ;;
        --system-only|--phase1)
            MODE="system"
            shift
            ;;
        --user-only|--phase2)
            MODE="user"
            shift
            ;;
        --build-image)
            MODE="build-image"
            shift
            ;;
        --user)
            CUSTOM_USER="$2"
            shift 2
            ;;
        -y|--yes)
            AUTO_CONFIRM=true
            shift
            ;;
        -h|--help)
            echo -e "${BOLD}AI-Forge WSL2 Automated Installer${RESET}"
            echo ""
            echo "Usage: $0 [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  (no args)           Auto-detect: runs system setup if root, user setup if normal user"
            echo "  --system-only       Run Phase 1 (system packages, agent user, subuids, wsl.conf)"
            echo "  --user-only         Run Phase 2 (Pixi, dependencies, LiteLLM proxy, Podman build)"
            echo "  --build-image       Rebuild the rootless Podman container image only"
            echo "  --check             Run non-destructive diagnostics and report status"
            echo "  --user <username>   Set target non-root user (default: 'agent', or current user)"
            echo "  -y, --yes           Auto-confirm prompts with default answers"
            echo "  -h, --help          Show this help message"
            echo ""
            echo "Examples:"
            echo "  sudo ./setup_wsl2.sh                    # Full setup (offers isolated 'agent' user or current user)"
            echo "  sudo ./setup_wsl2.sh --user ubuntu      # Install for existing 'ubuntu' account"
            echo "  ./setup_wsl2.sh --user-only             # Run user-space setup as current user"
            echo "  ./setup_wsl2.sh --check                 # Non-destructive environment health check"
            exit 0
            ;;
        *)
            log_err "Unknown argument: $1"
            echo "Run '$0 --help' for usage."
            exit 1
            ;;
    esac
done

if [ -n "$CUSTOM_USER" ]; then
    TARGET_USER="$CUSTOM_USER"
fi

get_user_home() {
    local u="$1"
    if [ "$u" = "root" ]; then
        echo "/root"
    else
        eval echo "~$u"
    fi
}

PROXY_DIR="$(get_user_home "$TARGET_USER")/litellm_proxy"

prompt_yn() {
    local prompt_msg="$1"
    local default_ans="$2"
    if [ "$AUTO_CONFIRM" = true ]; then
        return 0
    fi
    local answer
    read -r -p "$prompt_msg [$default_ans]: " answer
    answer="${answer:-$default_ans}"
    [[ "$answer" =~ ^[Yy]$ ]]
}

# ==============================================================================
# DIAGNOSTICS & HEALTH CHECK
# ==============================================================================
run_diagnostics() {
    log_step "AI-Forge Environment Diagnostics"
    echo "--------------------------------------------------------"

    # 1. OS & Architecture
    if [ -f /etc/os-release ]; then
        . /etc/os-release
        echo -e "OS Distribution:     ${GREEN}${PRETTY_NAME}${RESET}"
    else
        echo -e "OS Distribution:     ${RED}Unknown${RESET}"
    fi
    echo -e "WSL2 Detection:      $(grep -qi microsoft /proc/version 2>/dev/null && echo -e "${GREEN}Detected${RESET}" || echo -e "${YELLOW}Standard Linux (Not WSL2)${RESET}")"

    # 2. User & Sudo
    echo -e "Current User:        ${CYAN}$(whoami)${RESET} (UID: $(id -u))"
    if id "$TARGET_USER" &>/dev/null; then
        echo -e "Agent User:          ${GREEN}Configured${RESET} ($(id -u "$TARGET_USER"))"
    else
        echo -e "Agent User:          ${RED}Missing ('$TARGET_USER' does not exist)${RESET}"
    fi

    # 3. Subuid / Subgid
    local has_subuid=false
    local has_subgid=false
    grep -q "^${TARGET_USER}:" /etc/subuid 2>/dev/null && has_subuid=true || true
    grep -q "^${TARGET_USER}:" /etc/subgid 2>/dev/null && has_subgid=true || true
    echo -e "Rootless SubUID:     $($has_subuid && echo -e "${GREEN}Configured${RESET}" || echo -e "${RED}Missing in /etc/subuid${RESET}")"
    echo -e "Rootless SubGID:     $($has_subgid && echo -e "${GREEN}Configured${RESET}" || echo -e "${RED}Missing in /etc/subgid${RESET}")"

    # 4. Core System Packages
    for pkg in podman slirp4netns newuidmap curl git jq; do
        if command -v "$pkg" &>/dev/null; then
            echo -e "Package [${pkg}]:       ${GREEN}Installed${RESET} ($(command -v "$pkg"))"
        else
            echo -e "Package [${pkg}]:       ${RED}Not Found${RESET}"
        fi
    done

    # 5. GPU & NVIDIA CDI
    if command -v nvidia-smi &>/dev/null; then
        echo -e "NVIDIA Driver:       ${GREEN}Available${RESET} ($(nvidia-smi --query-gpu=gpu_name --format=csv,noheader 2>/dev/null | head -n1 || echo 'Detected'))"
    elif [ -e /dev/dxg ]; then
        echo -e "WSL GPU Bus (/dev/dxg): ${GREEN}Available${RESET}"
    else
        echo -e "GPU Passthrough:     ${YELLOW}No NVIDIA GPU detected (CPU mode)${RESET}"
    fi
    if [ -f /etc/cdi/nvidia.yaml ]; then
        echo -e "NVIDIA CDI Spec:     ${GREEN}Configured (/etc/cdi/nvidia.yaml)${RESET}"
    else
        echo -e "NVIDIA CDI Spec:     ${YELLOW}Not generated${RESET}"
    fi

    # 6. Pixi Package Manager
    if command -v pixi &>/dev/null || [ -f "$HOME/.pixi/bin/pixi" ] || [ -f "/home/${TARGET_USER}/.pixi/bin/pixi" ]; then
        echo -e "Pixi Manager:        ${GREEN}Installed${RESET}"
    else
        echo -e "Pixi Manager:        ${RED}Not Installed${RESET}"
    fi

    # 7. Podman Image
    if podman image exists "${IMAGE_NAME}" 2>/dev/null; then
        echo -e "Sandbox Container:   ${GREEN}Image '${IMAGE_NAME}' Ready${RESET}"
    else
        echo -e "Sandbox Container:   ${RED}Image '${IMAGE_NAME}' not built${RESET}"
    fi

    # 8. LiteLLM Proxy
    if [ -d "$PROXY_DIR" ] && [ -f "$PROXY_DIR/config.yaml" ]; then
        echo -e "LiteLLM Proxy:       ${GREEN}Configured ($PROXY_DIR)${RESET}"
    else
        echo -e "LiteLLM Proxy:       ${YELLOW}Not Configured${RESET}"
    fi
    if pgrep -f "litellm.*--port 4000" &>/dev/null || ss -tulpn 2>/dev/null | grep -q ":4000\b"; then
        echo -e "Proxy Port 4000:     ${GREEN}Running (Port 4000 Listening)${RESET}"
    else
        echo -e "Proxy Port 4000:     ${YELLOW}Not currently active${RESET}"
    fi
    echo "--------------------------------------------------------"
}

# ==============================================================================
# PHASE 1: SYSTEM SETUP (ROOT / SUDO)
# ==============================================================================
run_phase1_system() {
    log_step "Phase 1: System-Level Hardening & Package Installation (Root/Sudo)"

    if [ "$EUID" -ne 0 ]; then
        log_err "Phase 1 requires administrative privileges. Please run with sudo:"
        echo "    sudo $0"
        exit 1
    fi

    # Check package manager (Debian/Ubuntu)
    if ! command -v apt-get &>/dev/null; then
        log_err "Unsupported package manager. Automated installation currently targets Debian/Ubuntu-based WSL2 instances (apt-get)."
        echo "For Fedora, Arch, or Alpine WSL2, please consult the manual installation guide in readme.md."
        exit 1
    fi

    # Check for legacy WSL1
    if grep -qi microsoft /proc/version 2>/dev/null; then
        if ! uname -r | grep -qi "WSL2" && [ ! -e /dev/dxg ]; then
            log_err "Legacy WSL1 detected! Rootless Podman and GPU passthrough require WSL2."
            echo "Please upgrade your WSL instance to version 2 from Windows PowerShell:"
            echo "    wsl --set-version <distro-name> 2"
            exit 1
        fi
    fi

    # User isolation selection if not explicitly set via --user
    if [ -z "$CUSTOM_USER" ] && [ -n "$CALLING_USER" ] && [ "$CALLING_USER" != "root" ] && [ "$CALLING_USER" != "agent" ]; then
        echo ""
        log_info "Detected calling user: '${CALLING_USER}'."
        if [ "$AUTO_CONFIRM" != true ]; then
            echo -e "You can choose your setup mode:"
            echo -e "  [1] Create a dedicated restricted '${GREEN}agent${RESET}' user (Recommended for maximum sandbox security)"
            echo -e "  [2] Install AI-Forge for your existing account ('${CYAN}${CALLING_USER}${RESET}')"
            if prompt_yn "Create and use dedicated 'agent' user?" "Y"; then
                TARGET_USER="agent"
            else
                TARGET_USER="$CALLING_USER"
            fi
        else
            TARGET_USER="agent"
        fi
        PROXY_DIR="$(get_user_home "$TARGET_USER")/litellm_proxy"
    fi

    # 1. Update and install core system utilities
    log_info "Updating apt package index..."
    apt-get update -y

    log_info "Installing core dependencies (podman, slirp4netns, uidmap, git, curl, jq, build-essential)..."
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        curl \
        wget \
        podman \
        slirp4netns \
        uidmap \
        git \
        jq \
        ca-certificates \
        gnupg \
        build-essential

    log_done "Core system packages installed."

    # 2. NVIDIA GPU Passthrough & CDI Setup (if GPU present)
    log_step "Probing for NVIDIA GPU hardware..."
    local has_gpu=false
    if command -v nvidia-smi &>/dev/null || [ -e /dev/dxg ] || lspci 2>/dev/null | grep -qi nvidia; then
        has_gpu=true
    fi

    if [ "$has_gpu" = true ]; then
        log_info "NVIDIA hardware detected. Configuring NVIDIA Container Toolkit..."
        if ! command -v nvidia-ctk &>/dev/null; then
            mkdir -p /etc/apt/keyrings
            curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | gpg --dearmor -o /etc/apt/keyrings/nvidia-container-toolkit-keyring.gpg --yes
            curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \
                sed 's#deb https://#deb [signed-by=/etc/apt/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
                tee /etc/apt/sources.list.d/nvidia-container-toolkit.list >/dev/null
            apt-get update -y
            DEBIAN_FRONTEND=noninteractive apt-get install -y nvidia-container-toolkit
        fi

        log_info "Generating NVIDIA Container Device Interface (CDI) specification..."
        mkdir -p /etc/cdi
        nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml || log_warn "CDI generation warning; container will still boot."
        log_done "NVIDIA Container Toolkit & CDI configured."
    else
        log_info "No NVIDIA GPU detected; skipping NVIDIA Container Toolkit (CPU mode ready)."
    fi

    # 3. Create non-privileged agent user
    log_step "Configuring restricted non-root '${TARGET_USER}' user..."
    if ! id "$TARGET_USER" &>/dev/null; then
        useradd -m -s /bin/bash "$TARGET_USER"
        log_done "Created user '${TARGET_USER}'."
    else
        log_info "User '${TARGET_USER}' already exists."
    fi

    # 4. Configure subuid and subgid for rootless Podman
    log_info "Verifying rootless Podman subuid/subgid mapping..."
    if ! grep -q "^${TARGET_USER}:" /etc/subuid 2>/dev/null; then
        # Find next available UID range
        local next_subuid=165536
        if [ -s /etc/subuid ]; then
            local max_subuid
            max_subuid=$(awk -F: '{print $2+$3}' /etc/subuid | sort -nr | head -n1)
            [ -n "$max_subuid" ] && next_subuid="$max_subuid"
        fi
        echo "${TARGET_USER}:${next_subuid}:65536" >> /etc/subuid
        log_done "Added ${TARGET_USER} to /etc/subuid (${next_subuid}:65536)."
    else
        log_info "/etc/subuid already configured for ${TARGET_USER}."
    fi

    if ! grep -q "^${TARGET_USER}:" /etc/subgid 2>/dev/null; then
        local next_subgid=165536
        if [ -s /etc/subgid ]; then
            local max_subgid
            max_subgid=$(awk -F: '{print $2+$3}' /etc/subgid | sort -nr | head -n1)
            [ -n "$max_subgid" ] && next_subgid="$max_subgid"
        fi
        echo "${TARGET_USER}:${next_subgid}:65536" >> /etc/subgid
        log_done "Added ${TARGET_USER} to /etc/subgid (${next_subgid}:65536)."
    else
        log_info "/etc/subgid already configured for ${TARGET_USER}."
    fi

    # Enable lingering so user processes / Podman rootless daemons persist
    if command -v loginctl &>/dev/null; then
        loginctl enable-linger "$TARGET_USER" 2>/dev/null || true
    fi

    # 5. WSL2 Hardening (/etc/wsl.conf)
    if grep -qi microsoft /proc/version 2>/dev/null; then
        if [ "$TARGET_USER" = "agent" ]; then
            log_step "WSL2 Configuration Hardening (/etc/wsl.conf)..."
            local apply_wsl=true
            if [ "$AUTO_CONFIRM" != true ]; then
                if ! prompt_yn "Configure /etc/wsl.conf (default user: ${TARGET_USER}, disable automount/interop for security)?" "Y"; then
                    apply_wsl=false
                fi
            fi

            if [ "$apply_wsl" = true ]; then
                if [ -f /etc/wsl.conf ]; then
                    cp /etc/wsl.conf "/etc/wsl.conf.bak.$(date +%Y%m%d%H%M%S)"
                fi
                cat <<EOF > /etc/wsl.conf
[user]
default=${TARGET_USER}

[automount]
enabled = false

[interop]
enabled = false
appendWindowsPath = false
EOF
                log_done "Wrote hardened /etc/wsl.conf (backup saved if previous file existed)."
            fi
        else
            log_info "Installing for standard account '${TARGET_USER}'. Skipping /etc/wsl.conf hardening to preserve your existing user login and Windows mounts."
        fi
    fi

    # 6. Ensure proper workspace ownership
    log_info "Ensuring workspace permissions for ${TARGET_USER}..."
    chown -R "${TARGET_USER}:${TARGET_USER}" "$SCRIPT_DIR" 2>/dev/null || true

    log_done "Phase 1 (System Configuration) completed successfully!"
}

# ==============================================================================
# PHASE 2: USER-SPACE SETUP (PIXI, LITELLM, CONTAINER IMAGE)
# ==============================================================================
run_phase2_user() {
    log_step "Phase 2: User-Space Setup & Container Build"

    if [ "$EUID" -eq 0 ]; then
        log_warn "Phase 2 is running as root. It is recommended to run user setup as '${TARGET_USER}'."
        if [ "$AUTO_CONFIRM" != true ]; then
            if prompt_yn "Switch to user '${TARGET_USER}' to complete setup?" "Y"; then
                su - "${TARGET_USER}" -c "bash '$SCRIPT_DIR/setup_wsl2.sh' --user-only"
                return
            fi
        fi
    fi

    local USER_HOME="$HOME"

    # 1. Install Pixi package manager
    log_step "Checking Pixi package manager installation..."
    local PIXI_BIN="$USER_HOME/.pixi/bin/pixi"
    if ! command -v pixi &>/dev/null && [ ! -x "$PIXI_BIN" ]; then
        log_info "Downloading and installing Pixi for $(whoami)..."
        curl -fsSL https://pixi.sh/install.sh | bash
        export PATH="$USER_HOME/.pixi/bin:$PATH"
    else
        log_done "Pixi is already installed."
    fi

    # Ensure PATH is exported in ~/.bashrc
    if [ -f "$USER_HOME/.bashrc" ] && ! grep -q 'pixi/bin' "$USER_HOME/.bashrc"; then
        echo 'export PATH="$HOME/.pixi/bin:$PATH"' >> "$USER_HOME/.bashrc"
        log_done "Added Pixi to ~/.bashrc PATH."
    fi
    export PATH="$USER_HOME/.pixi/bin:$PATH"

    # 2. Install workspace dependencies
    log_step "Installing AI-Forge workspace dependencies with Pixi..."
    cd "$SCRIPT_DIR"
    if [ -f "pixi.toml" ]; then
        "$PIXI_BIN" install
        log_done "Workspace Pixi environment installed."
    else
        log_warn "pixi.toml not found in $SCRIPT_DIR; skipping workspace pixi install."
    fi

    # 3. Setup Zero-Trust LiteLLM Proxy
    log_step "Configuring Zero-Trust LiteLLM Proxy in ${PROXY_DIR}..."
    mkdir -p "$PROXY_DIR"
    cd "$PROXY_DIR"

    if [ ! -f "pixi.toml" ]; then
        cat <<'EOF' > pixi.toml
[workspace]
channels = ["conda-forge"]
name = "litellm_proxy"
platforms = ["linux-64"]
version = "0.1.0"

[tasks]

[dependencies]
python = "3.12.*"
litellm = ">=1.83.14,<2"
pip = ">=26.0.1,<27"
EOF
        log_done "Created $PROXY_DIR/pixi.toml."
    fi

    log_info "Installing LiteLLM environment dependencies..."
    "$PIXI_BIN" install
    "$PIXI_BIN" run pip install 'litellm[proxy]' 2>/dev/null || true

    # Create template config.yaml if missing
    if [ ! -f "config.yaml" ]; then
        cat <<'EOF' > config.yaml
litellm_settings:
  drop_params: true  # Strips unsupported model parameters dynamically

model_list:
  # ============================================================================
  # [1] Local Model Examples (Ollama or vLLM running on host or remote machine)
  # ============================================================================
  - model_name: Qwen3.8-Flash-Next-FP8
    litellm_params:
      model: openai/Qwen3.8-Flash-Next-FP8
      api_base: http://localhost:64100/v1/
      api_key: local-vllm-key

  - model_name: Qwen/Qwen3.8-27B-FP8
    litellm_params:
      model: openai/Qwen/Qwen3.8-27B-FP8
      api_base: http://localhost:64100/v1/
      api_key: local-vllm-key

  # ============================================================================
  # [2] Cloud Model Examples (Requires setting your API keys in ~/.bashrc)
  # ============================================================================
  # - model_name: claude-3-7-sonnet
  #   litellm_params:
  #     model: anthropic/claude-3-7-sonnet-20250219
  #     api_key: os.environ/ANTHROPIC_API_KEY
  #
  # - model_name: gemini-2.5-pro
  #   litellm_params:
  #     model: gemini/gemini-2.5-pro
  #     api_key: os.environ/GEMINI_API_KEY
  #
  # - model_name: openrouter/deepseek-r1
  #   litellm_params:
  #     model: openrouter/deepseek/deepseek-r1
  #     api_key: os.environ/OPENROUTER_API_KEY
EOF
        log_done "Generated template $PROXY_DIR/config.yaml."
    else
        log_info "$PROXY_DIR/config.yaml already exists; keeping existing configuration."
    fi

    # LiteLLM Proxy Auto-Start helper in ~/.bashrc
    if [ -f "$USER_HOME/.bashrc" ] && ! grep -q "ZERO-TRUST AI PROXY AUTO-START" "$USER_HOME/.bashrc"; then
        cat <<'EOF' >> "$USER_HOME/.bashrc"

# ==========================================
# ZERO-TRUST AI PROXY AUTO-START
# ==========================================
if [ -d "$HOME/litellm_proxy" ] && [ -f "$HOME/litellm_proxy/config.yaml" ]; then
    if ! pgrep -f "litellm.*--port 4000" > /dev/null; then
        echo "🛡️ Starting Zero-Trust LiteLLM Proxy on port 4000..."
        (cd "$HOME/litellm_proxy" && nohup "$HOME/.pixi/bin/pixi" run litellm --config config.yaml --port 4000 > proxy.log 2>&1 &)
    fi
fi
EOF
        log_done "Configured LiteLLM background auto-start in ~/.bashrc."
    fi

    # 4. Build Rootless Podman Sandbox Container
    log_step "Building rootless Podman image '${IMAGE_NAME}' from Containerfile..."
    cd "$SCRIPT_DIR"
    if [ -f "Containerfile" ]; then
        podman build -t "${IMAGE_NAME}" -f Containerfile .
        log_done "Podman image '${IMAGE_NAME}' built successfully."
    else
        log_err "Containerfile not found in $SCRIPT_DIR!"
        exit 1
    fi

    # 5. Pre-create required workspace folders
    log_step "Creating necessary session and IO folders..."
    mkdir -p "$SCRIPT_DIR/my_host_input"
    mkdir -p "$SCRIPT_DIR/sessions"
    mkdir -p "$SCRIPT_DIR/plugins"
    log_done "Folders initialized."

    # 6. Self-Test / Verification
    log_step "Running sandbox container self-verification test..."
    if podman run --rm "${IMAGE_NAME}" python -c "import fastmcp, sqlite_vec; print('Sandbox verification passed!')" 2>&1; then
        log_done "Rootless Podman sandbox self-test passed!"
    else
        log_warn "Sandbox verification test returned non-zero exit; please inspect Podman logs."
    fi

    log_done "Phase 2 (User-Space Setup) completed successfully!"
}

# ==============================================================================
# MAIN ENTRYPOINT
# ==============================================================================

echo -e "${BOLD}${CYAN}"
cat <<'EOF'
    ___    ____   ______                       
   /   |  /  _/  / ____/___  _________ ____    
  / /| |  / /   / /_  / __ \/ ___/ __ `/ _ \   
 / ___ |_/ /   / __/ / /_/ / /  / /_/ /  __/   
/_/  |_/___/  /_/    \____/_/   \__, /\___/    
                               /____/          
  Autonomous AI Scientist Framework - Setup
EOF
echo -e "${RESET}"

case "$MODE" in
    check)
        run_diagnostics
        exit 0
        ;;
    system)
        run_phase1_system
        exit 0
        ;;
    user)
        run_phase2_user
        exit 0
        ;;
    build-image)
        log_step "Rebuilding container image '${IMAGE_NAME}'..."
        cd "$SCRIPT_DIR"
        podman build -t "${IMAGE_NAME}" -f Containerfile .
        log_done "Image build complete."
        exit 0
        ;;
    auto)
        if [ "$EUID" -eq 0 ]; then
            log_info "Detected root/sudo privileges. Executing full setup (Phase 1 -> Phase 2)..."
            run_phase1_system
            echo ""
            log_step "Handing off to user '${TARGET_USER}' for Phase 2..."
            su - "${TARGET_USER}" -c "bash '$SCRIPT_DIR/setup_wsl2.sh' --user-only --user '${TARGET_USER}'"
        else
            log_info "Running as standard user ($(whoami)). Checking if system packages are installed..."
            if ! command -v podman &>/dev/null || ! command -v slirp4netns &>/dev/null; then
                log_warn "System packages (podman, slirp4netns) are missing. Administrative privileges required."
                echo "Please run Phase 1 with sudo first:"
                echo "    sudo $0"
                exit 1
            fi
            run_phase2_user
        fi
        ;;
esac

# ==============================================================================
# INSTRUCTIONS & NEXT STEPS
# ==============================================================================
echo ""
echo -e "${BOLD}${GREEN}======================================================================${RESET}"
echo -e "${BOLD}${GREEN}           🎉 AI-FORGE ENVIRONMENT SETUP COMPLETE!                    ${RESET}"
echo -e "${BOLD}${GREEN}======================================================================${RESET}"
echo ""
echo -e "${BOLD}Next Steps:${RESET}"
echo ""
echo -e "  ${BOLD}1. Configure your LLM endpoints & API Keys:${RESET}"
echo -e "     Edit the proxy configuration:"
echo -e "       ${CYAN}nano ~/litellm_proxy/config.yaml${RESET}"
echo -e "     Add your API keys to your environment (if using external providers):"
echo -e "       ${CYAN}echo 'export OPENAI_API_KEY=\"your-key\"' >> ~/.bashrc${RESET}"
echo ""
echo -e "  ${BOLD}2. Start or verify the LiteLLM Proxy:${RESET}"
echo -e "     (The proxy will automatically start when you open a new bash terminal)"
echo -e "     To start it manually in the background right now:"
echo -e "       ${CYAN}cd ~/litellm_proxy && nohup pixi run litellm --config config.yaml --port 4000 > proxy.log 2>&1 &${RESET}"
echo ""
echo -e "  ${BOLD}3. Launch the AI Overseer:${RESET}"
echo -e "     Interactive Rich UI:"
echo -e "       ${CYAN}cd $SCRIPT_DIR && pixi run python chat_overseer.py${RESET}"
echo ""
echo -e "     Run a single headless task and auto-exit:"
echo -e "       ${CYAN}cd $SCRIPT_DIR && pixi run python chat_overseer.py -x -p \"Check environment and view tool registry\"${RESET}"
echo ""
echo -e "  ${BOLD}4. Swarm Dashboard (Multi-Agent Tmux Grid):${RESET}"
echo -e "       ${CYAN}./start_tests_analysis_tmux_1${RESET}"
echo ""
echo -e "${BOLD}Diagnostics command anytime:${RESET}"
echo -e "  ${CYAN}./setup_wsl2.sh --check${RESET}"
echo -e "${BOLD}${GREEN}======================================================================${RESET}"
EOF
