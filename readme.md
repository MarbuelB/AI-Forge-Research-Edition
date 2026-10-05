# AI-Forge-Research-Edition: The Autonomous Self-Evolving AI Scientist Framework

A zero-trust AI architecture utilizing a multi-agent system to dynamically forge local tools, featuring persistent sessions, WSL2 isolation, GPU acceleration, and auto-healing debugging loops.

Rather than relying on a static set of pre-programmed tools, this framework empowers an AI to write, compile, debug, and execute its own multi-language tools (Python, JavaScript, TypeScript, Rust, C++) on the fly using the Model Context Protocol (MCP). By separating the "Brain" (logical reasoning) from the "Coder" (code generation), the "Summarizer" (memory management), and the "Adviser" (strategic correction), the system acts as a persistent, self-expanding AI operating system capable of acting as an autonomous researcher—scraping the web, downloading massive datasets, testing hypotheses, training machine learning models, and rendering publication-ready reports while safely bridging massive LLMs with your local hardware.

---

## 🧠 Core Concepts

### Step 0: The Host Sandbox (WSL2 Hardening)

For ultimate security, this framework should be run inside a dedicated WSL2 (Windows Subsystem for Linux) instance under a heavily restricted user account. The steps below create a hardened WSL environment where the AI is completely isolated from your Windows host system.

**1. Install a Dedicated WSL Instance**
Install a fresh Ubuntu instance specifically for this project (named `Agents`). You can install from a downloaded file:

    wsl --install --from-file "C:\path\to\your\download\ubuntu-24.04-wsl-amd64.wsl" --name Agents
    
**2. Create the Restricted User (`agent`)**
Log into your new instance as your initial setup user (who has administrative `sudo` rights). Then, create a restricted, non-sudo user named `agent`. This guarantees that the process running the AI has zero administrative rights to the underlying Linux system:

    wsl -d Agents -u <your-initial-sudo-user>
    sudo useradd -m -s /bin/bash agent
    sudo passwd agent
    
**3. Harden the WSL Configuration**
We must sever the default connections between WSL and your Windows host. Open the WSL configuration file:

    sudo nano /etc/wsl.conf
    
Paste the following configuration. This does three critical things: it sets `agent` as the default user, blocks WSL from automatically mounting your Windows hard drives (disabling `/mnt/c/`), and prevents WSL from executing Windows binaries (like `cmd.exe` or `powershell.exe`).

    [boot]
    systemd=true

    [user]
    default=agent
    
    [automount]
    enabled = false
    
    [interop]
    enabled = false
    appendWindowsPath = false
    
**4. Reboot and Initialize**
Save the file (Ctrl+O, Enter, Ctrl+X) and exit the terminal. Open a standard Windows Command Prompt or PowerShell and restart the WSL instance to apply the hardened settings:

    wsl --terminate Agents
    wsl -d Agents
    
You will now automatically be logged in as the restricted `agent` user, trapped inside a secure, air-gapped Linux bubble. You are now ready to proceed to Step 1!

### 1. The Multi-Agent Architecture
This project utilizes multiple distinct Large Language Models (LLMs) to maximize efficiency and accuracy:
* **The Brain (The Overseer):** A fast, high-parameter reasoning model. It communicates with the user, plans out the necessary steps, and orchestrates the execution of tools.
* **The Coder (The Hands):** A specialized multi-language engineering model operating inside the Forge sandbox. It operates invisibly in the background. When the Brain needs a new component, the Coder outputs the target source files, triggers automated syntax validation passes, and manages compilation schemas dynamically.
* **The Summarizer (The Memory Manager):** A background agent responsible for context compression using strict JSON schemas.
* **The Adviser (The Strategist):** A slow, extremely heavy reasoning model (e.g., DeepSeek-R1, Qwen 397B). It is invoked exclusively when the Brain encounters critical bottlenecks or requires strategic redirection.
* **The Analyst (The Eyes & Data Miner):** A specialized context-isolation and vision agent. When the Brain needs to read a massive 50,000-line error log, parse a raw data dump, or look at an image, it delegates the file to the Analyst. The Analyst processes the heavy payload and returns a highly concentrated summary, keeping the Brain's context window clean, fast, and immune to data-bloat.
* **The Architect (The Methodologist & Skill Builder):** A specialized agent dedicated to Standard Operating Procedure (SOP) codification. When the Brain solves a novel or complex engineering task, it calls `commission_architect`. The Architect synthesizes the full workflow (prerequisites, setup commands, code patterns, edge cases, troubleshooting table, and verification checks) into a permanent, reusable `SKILL.md` file saved to `/app/workspace/skills/<skill_name>/`. The Brain dynamically loads and follows these skills via `load_skill`.
* **Universal Sub-Agents:** The Brain has the ability to dynamically spawn its own temporary LLM sub-agents to delegate isolated tasks, test prompt engineering, or request second opinions using custom temperature and parameters.
* **Dynamic Payload Airlocks:** The framework utilizes a pre-flight token calculator (via `tiktoken`) for all sub-agent delegations. If the Brain attempts to send a 100,000-token log file to the Analyst or Adviser that exceeds 90% of their specific context window, the framework intercepts the request, blocks the API call, and bounces a `SYSTEM ERROR` back to the Brain instructing it to use `grep` or chunk the files first. This completely eliminates silent API truncation and OOM crashes.
* **Pointer-Based Context Delegation (Pass-by-Reference):** To prevent the Overseer from running out of working memory, the system uses an architectural pattern where large codebaselines, historical logs, or environment files are never read directly into the main chat window. Instead, the Overseer passes absolute string paths via the `context_filepaths` parameter available across the `Coder`, `Adviser`, `Architect`, and `Universal LLM` sub-agents. The background tool engine processes the files ephemerally, shielding the main orchestration loop from context bloat and avoiding unexpected memory wipeouts.

### 2. Polyglot Auto-Retry & Error Transparency
LLMs occasionally make syntax errors when generating tools. The framework isolates this through automated language-specific validation pipelines:
* **Python**: Validated via `py_compile`. Custom PyPI packages declared via `# REQUIRES: <package>` comments are automatically installed into the persistent `custom_packages/` directory before execution.
* **JavaScript / TypeScript**: Syntax-checked via `node --check` and `node --experimental-strip-types --check`.
* **Rust**: Verified in an isolated Cargo scratchpad (`cargo check -q`). Optional crate dependencies declared via `// REQUIRES: crate = "version"` comments are automatically parsed, added to `[dependencies]`, and propagated into the execution blueprint.
* **C++**: Pre-flight parsed using `g++ -fsyntax-only -std=c++17` before linking.
* **Compiler Error Transparency**: If code validation exhausts retries (`config.MAX_PLUGIN_RETRIES`), the exact compiler or linter `stderr` (`last_validation_error`) is returned directly to the Brain, eliminating blind debugging.
* **Pre-Flight Surgical Syntax Checks**: The `surgical_code_edit` tool supports sequential multi-block search/replace and tests syntax in-memory via `compile()` before writing to disk, preventing syntax corruption.

### 3. Rich Hierarchical Discovery (The Registries)
To prevent overwhelming the AI with hundreds of tools and memories, knowledge is split into compact JSON indexes and detailed physical files:
* **Tool Registry:** Plugins are categorized in `tool_registry.json`. The Brain navigates this in two steps: checking high-level categories, then diving into specific category descriptions to learn how to use its tools.
* **Memory Registry:** Long-term facts and summaries are indexed in `memory_registry.json` with short descriptions and timestamps. The exhaustive details are saved as individual Markdown files (`.md`) in the `memories/` folder. The Brain can browse the lightweight index and only spend tokens to read the full file when explicitly needed.
* **Skills Registry:** Standard operating procedures are organized under `/app/workspace/skills/<skill_name>/SKILL.md`. Available skills are dynamically cataloged in the Brain's prompt, and the Brain can load and follow them on demand via `load_skill` without bloating initial context.

### 4. Persistent Sessions & Isolated Environments
Workspaces are strictly isolated. The system dynamically generates unique `Session_ID` folders. 
* **State-Driven Restart:** The framework operates entirely on state files, not temporary RAM. When you resume a session, the Brain instantly re-loads its `current_history.json` and perfectly remembers exactly where it left off, alongside all its plugins, stored memories, and master plans. 
* **Layered Micro-Environments (Base + Delta):** The heavy, complex dependencies (like C++ libraries and Chromium) are locked safely in the container's base Pixi image. When the AI installs a new third-party library for a specific tool, it uses a background `pip --target` injection. This places the tiny Python files directly into a `custom_packages` folder inside that specific session's mapped workspace, keeping your hard drive completely free of 2GB redundant bloat while retaining perfect session persistence.
* **Bioconda Injection:** Every new workspace is automatically configured to pull from the `bioconda` conda channel, giving the AI immediate access to thousands of pre-compiled bioinformatics tools.

### 5. Fully Asynchronous Streaming
The system utilizes AsyncOpenAI for fully asynchronous streaming across all agents. Both the main Brain on the host and the background FastMCP tools (Coder, Summarizer, Adviser) use `async/await`. When a massive model takes up to 90 seconds to "think," the Python event loop remains unblocked. This ensures the background stdio connection to the Podman container never starves or drops, keeping the environment perfectly stable during long execution cycles.

### 6. Strict JSON Schema Pipelines
To guarantee system stability, background agents (like the Summarizer) do not rely on standard prompting. They use OpenAI's native Structured Outputs (`"strict": True`). This enforces mathematical compliance at the API level, ensuring the AI cannot hallucinate a broken comma or invalid schema that would corrupt the `current_history.json` or registry files.

### 7. The "AI Scientist" Super-Suite
To prevent the AI from reinventing the wheel with complex Python scripts, the sandbox natively includes powerful system-level capabilities:
* **Hardware Acceleration:** Native GPU passthrough (via NVIDIA CDI) allows the AI to dynamically install CUDA toolkits via Pixi and execute hardware-accelerated machine learning scripts.
* **Native Vector Database (Context-Bypass Architecture):** Equipped with `sqlite3` and the C-based `sqlite-vec` extension. The framework uses a strict Two-Table relational schema to link metadata directly to high-speed vector tables.
* *Zero Context Bloat:* The framework uses dedicated tools to shield the LLM from massive vector arrays. To ingest bulk data, the Brain uses the `batch_generate_embeddings` tool, which generates embeddings and natively injects them into the two-table schema in milliseconds. To query the database, it passes a `search_text_to_embed` parameter to the SQL tool. The massive floating-point arrays never touch the LLM's context window, drastically reducing token costs and preventing hallucination.
* **Zero-Cost Web Search & Stealth Scraping:** The framework is natively routing queries through a headless Chromium browser. By patching the browser with **Playwright-Stealth** and querying legacy HTML search portals, the AI visually scrapes live search results and reads articles as a spoofed human user, effortlessly bypassing Cloudflare, API shadow-bans, and WAF bot-protections. DuckDuckGo `uddg` redirect URLs are cleanly extracted, and academic papers/articles automatically have scholarly metadata (`citation_*`, `dc.*`, OpenGraph, DOI, authors) extracted and prepended as structured headers before layout stripping.
* **Native Multi-Modal Vision (VLM Support):** The framework isn't just limited to text. By pointing the Analyst profile to a Vision-Language Model, the AI framework can "see." The Brain can autonomously ask the Analyst to review generated charts, describe UI screenshots, or inspect visual data. The `analyze_files` pipeline includes pre-flight fast-failing for 0-byte log files (preventing wasted LLM calls) and evaluates API `finish_reason` to flag token-limit truncations explicitly.
* **Context-Isolation Pattern (Universal Ingestion):** Large source assets, configuration mappings, and logs are strictly airlocked away from the Overseer's active working memory. While the `analyze_files` tool routes data structures to the Analyst, the framework features native file extraction helpers (`gather_agent_context`) wired directly into tool triggers. When files are referenced via `context_filepaths`, the host reads up to 40,000 characters per asset, calculates the exact token footprint via `tiktoken` to guard against hardware boundaries, and prepends the codebase seamlessly into the sub-agent’s prompt under a specialized `=== ATTACHED AGENT CONTEXT BACKGROUND ENVIRONMENT ===` header.
* **Massive Data Processing:** Equipped with `aria2c` for high-speed parallel downloads and `pigz`/`zstd` for instantaneous decompression of massive genomic/data payloads.
* **Media & Documents:** Pre-loaded with `tesseract-ocr`, `poppler-utils` (for direct PDF extraction), and `ffmpeg`.
* **Scientific Rendering & Reporting:** Uses `graphviz` to render complex networks and `pandoc` to compile the AI's markdown findings directly into HTML, Word, or scientific PDF formats.

### 8. Master Planning & Self-Correction
To prevent the Brain from getting lost in the weeds of complex multi-step tasks, the framework utilizes persistent strategic states:
* **The Master Plan:** The Brain maintains a persistent Markdown file (`active_plan.md`) in its state folder to track overall objectives and checklists. It reads this plan upon waking up and seamlessly updates it when milestones are reached.
* **The Red Team Loop (Adviser):** If the Brain gets stuck on a coding error or logical dead end, it uses the `consult_adviser` tool. This packages the current plan, registries, and the encountered problem, and sends it to a Senior Adviser LLM. The Adviser generates a timestamped strategic advisory report saved to disk, enabling true autonomous self-correction without user hand-holding. If the report exceeds 12,000 characters, context guardrails automatically truncate the feedback preview in conversation memory while keeping the full document on disk.

### 9. Intelligent Loop Detection & Circuit Breakers
To prevent the AI from burning tokens in infinite error loops, the Overseer utilizes two distinct failsafes:
* **The Micro-Breaker (Failure Streaks):** The script tracks consecutive errors per tool. If the AI goes "blind" and fails to use the exact same tool 5 times in a row, the system forcefully intercepts the prompt with a [CRITICAL SYSTEM ALERT], snapping the AI out of its apology loop and commanding it to pivot its strategy.
* **The Macro-Breaker (Runaway Trains):** The script tracks uninterrupted tool chains. Every 300 tool calls, it triggers a "Soft Pause" by injecting a system prompt that forces the Brain to self-evaluate. If the AI hits 1,000 consecutive tool chains, the system executes a "Hard Stop," forcing the AI to await human intervention.
* **The Real-Time Stream Circuit Breaker (Anti-Trapping):** Large Language Models occasionally encounter attention loops where they repeat multi-paragraph blocks of text or internal logic sequences. To arrest this, the framework evaluates incoming data streams chunk-by-chunk across two independent channels: the standard text content channel (`full_content`) and the model's internal reasoning phase (`full_thinking`). 
  * *Alphanumeric Normalization:* The detector normalizes text in real time by stripping all whitespaces, newlines, and punctuation formatting noise (`re.sub(r'[^a-zA-Z0-9]', '', text)`), preventing the model from bypassing detection loops by altering paragraph layouts or spaces.
  * *Threshold & Backtracking:* The circuit breaker allows exactly 3 identical sequences to execute (original text plus 2 loops) and instantly trips on the first token of the 4th attempt. Rather than cutting text blindly by percentage slices, a dedicated helper (`clean_tail_by_alphanumeric_count`) backtracks from the end of the string, stripping out character bytes until it hits the exact boundary of the third iteration, preserving pristine trailing word boundaries without splitting words in half.
  * *Autonomous Self-Healing Retries:* The moment a loop is aborted, the system appends a hard `[CRITICAL ARCHITECTURAL DIRECTIVE]` warning statement directly into the history array, resets the local turn boundaries, and issues an asynchronous loop `continue`. This forces an immediate, automatic turn re-generation on the spot—intercepting the loop, injecting strategic course corrections, and executing a structural fallback tool (such as `consult_adviser` or `manage_plan`) without dropping the execution stream or stopping to wait for manual human keyboard inputs.
 
### 10. Perfect Temporal Awareness (The Live Clock)
Traditional agents lose track of time during long workflows. This framework utilizes a "Sweep & Replace" dynamic time injection. On every single API turn, the AI's context is injected with a Live System Clock down to the exact second. This allows the AI to accurately timestamp files and memories, without bloating the permanent `current_history.json` log with stale timestamps.

### 11. Self-Healing JSON Interception
Smaller or quantized models occasionally hallucinate malformed JSON when calling tools. The MCP server sits between the LLM and the tools, intercepting `JSONDecodeError` failures. Instead of crashing the script, it instantly returns a `SYSTEM ERROR` directly to the LLM, prompting it to fix its syntax and try again autonomously. Furthermore, if an active text stream is forcefully closed by the repetition circuit breaker, the system intercepts downstream execution blocks, omitting any incomplete tool structural formats from parsing queues to maintain pristine history tracking states.

### 12. On-Demand Observability & Container Logging
Standard agents crash silently when background dependencies or subprocesses fail. This framework features unified, multi-layered observability:
* **Host Interaction Logs:** Stored at `sessions/<Session_ID>/logs/chat_log_<timestamp>.txt`, capturing full conversation turns, streaming tokens, tool calls, execution latencies, and intercepted subagent thoughts.
* **Structured Container Debug Log:** Handled by a dedicated `ForgeContainer` logger (`level=logging.INFO`) writing directly to `/app/workspace/logs/container_debug.log`. All tool executions, subprocess commands, compilation checks, syntax validation errors, security enforcement events (destructive `rm` commands, path traversal, forbidden source file writes), search string mismatches, and database transactions are structured and timestamped.
* **Synchronized Clock Across Boundaries:** The Podman container runs with `--tz=local`, locking container log timestamps to the host system time to the second, making cross-boundary debugging instant.
* **Self-Debugging Pipeline:** If a tool fails unpredictably, the Brain delegates the log file to the Analyst sub-agent via `analyze_files(["/app/workspace/logs/container_debug.log"])`. The Analyst extracts the stack trace and pinpoint cause, enabling the Brain to patch the issue or install missing packages autonomously.

### 13. Dynamic UI & Verbosity Control
The Overseer natively decouples **Format** (how it looks) from **Verbosity** (how much it tells you). You can run it in `markdown` mode for a rich, color-coded terminal dashboard, or `text` mode for raw streaming. Furthermore, you have total runtime control: you can instantly change the UI by typing slash commands (`/silent`, `/minimal`, `/standard`, `/detailed`, `/text`, `/markdown`) directly into the chat prompt. This allows you to jump from a highly detailed matrix-style debugging view to a minimal, clean UI on the fly without ever restarting your container or dropping your session state!

---

## 🛡️ Sandboxing & Workspace Structure (Podman)

**CRITICAL WARNING:** This AI has the ability to execute bash commands and write files. To prevent it from modifying your host system or deleting crucial files, the MCP environment runs inside a heavily hardened, rootless **Podman Container**.

* **Non-Root Execution:** The AI runs as a restricted, non-root `agent` user inside the container. Podman handles UID mapping dynamically (via the `U` volume flag) to allow safe read/write access without root privileges.
* **Stripped Capabilities & Resource Limits:** The container drops all Linux capabilities (`--cap-drop=ALL`) and strictly limits the AI to 4 CPUs, 16GB RAM, and a hard limit of 1000 PIDs (`--pids-limit=1000`) to instantly neutralize bash fork bombs and resource-exhaustion attacks.
* **Anti-Zombie Process Hooks:** The container boots with the `--init` flag to perfectly manage PID 1 signals, and the host Python script uses `atexit` hooks. Even if you hard-crash the script via `Ctrl+D`, all nested bash processes, headless browsers, and stray daemons are instantly and cleanly slaughtered.
* **Stream Protection (Daemon Trapping):** The FastMCP server natively traps grandchild daemons (using `start_new_session=True` in subprocesses) and aggressively gags Python background logging. This ensures that stray warnings or detached background servers never corrupt the JSON-RPC communication streams.
* **The I/O Airlock (Surgical Mounts):** The container cannot see your host filesystem. It communicates via strict bind mounts:
  * `/app/host_input`: Mapped to your local input folder. **Strictly Read-Only (ro)**. The AI cannot delete or corrupt your source data. You must manually drop files (e.g., CSVs, scripts, or documents) into this host folder before asking the AI to analyze them. 
  * `/app/workspace`: The unified execution directory mapped to the active `Session_ID` folder.
* **Workspace Isolation:** The `/app/workspace` is split to protect the AI's "mind" from its "hands":
  * `/sandbox`: The scratchpad where the AI actually executes bash commands and tests tools.
  * `/outputs`: The dedicated folder where the AI saves finished artifacts and generated files.
  * `/state`: Contains the critical JSON registries, the master plan, adviser reports, and the live `current_history.json` file.
  * `/plugins`: Where the generated Python scripts live.
  * `/skills`: Where the Architect persists reusable SOP workflows (`SKILL.md`).
  * `/memories`: Where the detailed Markdown files live.
  * `/archive`: The dedicated "Soft-Delete" trash bin.
* **Zero-Trust Architecture:** The container contains absolutely NO sensitive data, API keys, or `.env` files. The AI uses hardcoded dummy keys (e.g., `sk-sandbox-fake-key`) and routes all requests to a local `host.containers.internal` gateway. Authentication and routing are securely handled by a LiteLLM proxy running safely on the Windows/WSL host, making credential theft mathematically impossible.
* **Context Preservation (I/O Truncation):** If the AI executes a bash command or scrapes a webpage that returns a massive payload (e.g., >10,000 characters), the system intercepts it. Instead of flooding the context window, the framework automatically saves the full output to a timestamped text file inside the sandbox. It then returns a tiny preview and a "Pointer" path back to the Brain, explicitly instructing it to delegate the heavy reading to the Analyst LLM.
* **Network Isolation:** The container runs on an isolated bridge network (`slirp4netns`). It communicates with local/tunneled LLMs strictly via the `host.containers.internal` gateway.
* **Anti-Deletion Protocol (Soft-Deletes):** The AI is strictly prompted against using destructive commands like `rm` or `DROP TABLE`. Instead, it is trained to use a robust "Soft-Delete" protocol. If it needs to rewrite a Python script or rebuild a database schema, it automatically uses the native `write_file` tool to create timestamped `.bak` copies, or uses bash `mv` to send old databases to the `/archive` folder. This provides a flawless audit trail and guarantees zero data loss during autonomous operations.
* **Strict Command Sanitization & Bash Runtime:** The `execute_bash` tool executes natively via `/bin/bash` (ensuring full compatibility with bash built-ins, process substitutions, and arrays). It employs strict regex to block destructive file deletion (`rm`, `rm -rf`), permits safe index untracking operations (`git rm --cached`), prevents command chaining via newline injection bypasses, and strictly limits output redirection (`>`, `>>`) to the `/sandbox/` and `/outputs/` directories, preventing the AI from hijacking its own `.bashrc` or staging payloads in `/tmp/`. Path traversal (`../`) is mathematically blocked using `os.path.commonpath`.

---

## ⚡ Quick-Start: Automated WSL2 & Environment Setup (Recommended)

To avoid manually running dozens of terminal commands and editing system files, use the included automated setup script [`setup_wsl2.sh`](file:///home/agent/ai_workspace/setup_wsl2.sh). It provisions any Debian/Ubuntu-based WSL2 environment from start to finish:

1. **System Provisioning (Phase 1 / Root):** Installs system dependencies (`podman`, `passt`, `slirp4netns`, `uidmap`, `git`, `jq`, `build-essential`), probes for NVIDIA GPU and configures CDI device passthrough, automates rootless `/etc/subuid` and `/etc/subgid` allocations for the execution user (creating dedicated `agent` user only if Option A is selected), enables `loginctl` user lingering, and configures user isolation.
2. **User-Space Provisioning (Phase 2):** Installs Pixi (`~/.pixi/bin`), installs workspace dependencies via `pixi install`, interactively prompts for your primary LLM endpoint (Name/Alias, Base URL, API Key, Model Name), generates LiteLLM proxy configuration (`~/litellm_proxy/config.yaml`), exports environment variables to `~/.bashrc`, updates [`llm_config.py`](file:///home/agent/ai_workspace/llm_config.py), auto-starts the LiteLLM proxy on port 4000, builds the rootless `ai-forge` Podman container, and runs an automated sandbox verification self-test.

### WSL2 Prerequisites
* **WSL2 with systemd enabled (Required):** Rootless Podman, D-Bus session bus, and user cgroups require systemd. Verify or add to `/etc/wsl.conf`:
  ```ini
  [boot]
  systemd=true
  ```
  > **Note:** If you modify `/etc/wsl.conf`, you must execute `wsl --shutdown` from Windows PowerShell or Command Prompt, then reopen your WSL2 distribution to activate systemd as PID 1.
* **Script Execution Permissions:** Ensure the setup script is marked executable before running:
  ```bash
  chmod +x setup_wsl2.sh
  ```

### Supported Setup Modes

The installer automatically detects your environment and offers two installation paths:
* **Option A: Dedicated Hardened Environment (Recommended):** Creates an isolated non-sudo `agent` user and hardens `/etc/wsl.conf` (disables Windows interop and automount) for maximum air-gapped sandboxing.
* **Option B: Existing User Account (Daily Driver):** Installs for your current user account (`ubuntu`, your custom username, etc.), configuring rootless Podman for your account while **preserving your existing `/etc/wsl.conf`, user login, and Windows `/mnt/c/` drives intact**.

### Running the Automated Setup:
Open your WSL2 terminal and run:

```bash
# Clone the repository and enter the directory
git clone <repo-url> ai_workspace
cd ai_workspace

# Ensure script is executable
chmod +x setup_wsl2.sh

# Run the complete automated setup (prompts for setup mode)
sudo bash setup_wsl2.sh
# (or: sudo ./setup_wsl2.sh)

# Or install explicitly for your existing user account without modifying wsl.conf:
sudo bash setup_wsl2.sh --user $(whoami)
```

### Script Commands & Diagnostics:
* `./setup_wsl2.sh --check`: Non-destructive diagnostics health check (reports live status for OS, WSL version, systemd PID 1, GPU/CDI, subuids, packages, Pixi, Podman image, and LiteLLM proxy port 4000).
* `sudo ./setup_wsl2.sh --system-only`: Runs only Phase 1 system provisioning (apt packages, GPU/CDI, subuids, user creation).
* `./setup_wsl2.sh --user-only`: Runs only Phase 2 user-space setup as current user (Pixi, dependencies, LiteLLM wizard, container build).
* `./setup_wsl2.sh --build-image`: Rebuilds the rootless Podman sandbox container (`ai-forge`).
* `sudo ./setup_wsl2.sh -y`: Unattended installation accepting default air-gapped settings.

---

## 🛠️ Manual Step-by-Step Setup (Reference)

If you prefer to configure your environment manually rather than using the automated installer, follow the steps below:

### Step 1: Install System Dependencies & GPU Drivers
Your freshly installed Ubuntu instance needs a few core tools before we can begin. Log into your hardened WSL terminal and run the following commands to install Podman, Pixi, and the NVIDIA toolkit for GPU passthrough:

    # Update the system and install Podman with rootless networking tools
    sudo apt update && sudo apt install -y curl podman passt slirp4netns uidmap
    
    # Add NVIDIA Container Toolkit repository and install (For GPU Passthrough)
    curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg \
      && curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \
        sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
        sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
    sudo apt-get update && sudo apt-get install -y nvidia-container-toolkit
    
    # Generate the CDI configuration so Podman can find the GPU
	sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml

    # Install Pixi
    curl -fsSL https://pixi.sh/install.sh | bash
    
    # Restart your terminal or source your profile to apply Pixi to your path
    source ~/.bashrc

### Step 2: Prepare Local Models
Ensure your local LLM server (like Ollama or vLLM) is running and you have pulled your designated Brain and Coder models. 
*(If using SSH tunnels to access remote GPUs, bind the tunnel to `0.0.0.0` so the Podman bridge can see it, e.g., `ssh -L 0.0.0.0:64100:127.0.0.1:8000 user@remote`).*

### Step 3: Initialize Host Pixi Environment (The Overseer)
Create a new directory for your project, initialize Pixi, and add the required libraries. Run these commands in your terminal:

    mkdir ai_workspace
    cd ai_workspace
    pixi init
    pixi add python openai mcp fastmcp prompt_toolkit rich tiktoken

### Step 4: Set Up the Host Proxy (Zero-Trust Routing)
To keep secrets completely out of the sandbox, we run a LiteLLM proxy on the host machine. The AI uses fake keys, and the proxy attaches the real ones.

Open a terminal on your host machine (outside the sandbox) and initialize a dedicated routing environment:

    cd ~
    mkdir litellm_proxy && cd litellm_proxy
    pixi init
    pixi add python=3.12 litellm pip
    pixi run pip install 'litellm[proxy]'

Create a `config.yaml` file in that folder to map your models to their actual endpoints (Ollama, vLLM, a remote HPC cluster) and pull your real API keys from the host's environment variables. 
Example (includes option to remove unsupported flags):

    litellm_settings:
      drop_params: true
    model_list:
      # [1] Local Model (Fast Brain / Coder)
      - model_name: Qwen/Qwen3.6-35B-A3B-FP8
        litellm_params:
          model: openai/Qwen/Qwen3.6-35B-A3B-FP8
          api_base: http://localhost:64100/v1
          api_key: local-vllm-key # vLLM just needs a dummy string

      # [2] Remote Model (Heavy Adviser / Strategist)
      - model_name: qwen35-397b-a17b-fp8
        litellm_params:
          model: openai/qwen35-397b-a17b-fp8
          # LiteLLM automatically pulls these from your ~/.bashrc exports!
          api_base: os.environ/LITELLM_API_BASE
          api_key: os.environ/LITELLM_API_KEY

Run this in your standard WSL2 terminal:

    nano ~/.bashrc

Add your secret variables like this:

    export LITELLM_API_KEY="<your-key>"
    export LITELLM_API_BASE="<your-address>"

Add this auto-start script to the bottom of the `.bashrc` file:

    # ==========================================
    # ZERO-TRUST AI PROXY AUTO-START
    # ==========================================
    if [ -d "$HOME/litellm_proxy" ] && [ -f "$HOME/litellm_proxy/config.yaml" ]; then
        if ! pgrep -f "litellm.*--port 4000" > /dev/null; then
            echo "🛡️ Starting Zero-Trust LiteLLM Proxy on port 4000..."
            (cd "$HOME/litellm_proxy" && nohup pixi run litellm --config config.yaml --port 4000 > proxy.log 2>&1 &)
        fi
    fi

Then, reload your profile by running this in your standard WSL2 terminal:

    source ~/.bashrc

### Step 5: Add the Project Files
Save the core scripts into the root of your `ai_workspace` folder:
1. `llm_config.py` (LLM endpoints, model parameters, and role assignments).
2. `config.py` (Core system prompts, memory limits, and runtime settings).
3. `god_tools.py` (The FastMCP server handling tool forging and execution).
4. `chat_overseer.py` (The main interactive async loop).

### Step 6: Build the Hardened Podman Container
Create a file named `Containerfile` in your workspace root. Notice that it contains no secrets, environment variables, or python files. 

*(Note: We deliberately do not `COPY` the Python scripts into this image. They are mapped dynamically via volumes at runtime. This allows you to tweak your prompts and Python code on the host machine endlessly without ever needing to rebuild the container cache!)*

	FROM ghcr.io/prefix-dev/pixi:latest

	# ROOT LEVEL: Install system-level scientific & media dependencies
	RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y \
		xvfb git git-lfs curl wget unzip aria2 file jq pigz zstd \
		poppler-utils tesseract-ocr ffmpeg imagemagick graphviz pandoc sqlite3 \
		build-essential cmake gfortran libgl1 libglib2.0-0 libxml2-dev libxslt-dev \
		procps ripgrep tree bzip2 \
		&& rm -rf /var/lib/apt/lists/*
		
	# USER SETUP
	RUN useradd -m -s /bin/bash agent
	WORKDIR /app

	# Disable the FastMCP ASCII Banner ---
	ENV FASTMCP_SHOW_SERVER_BANNER=0

	RUN mkdir /app/workspace && chown -R agent:agent /app

	# Switch to non-root user before installing Python tools
	USER agent

	# Explicitly provision Rust toolchains into the user path boundaries safely
	ENV RUSTUP_HOME=/home/agent/.rustup \
	    CARGO_HOME=/home/agent/.cargo \
	    PATH=/home/agent/.cargo/bin:$PATH

	# Install Rust toolchain natively for the non-root execution framework
	RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --default-toolchain stable --no-modify-path

	# We add conda-forge and bioconda to give the AI maximum scientific reach
	# Chained the Playwright install and cache cleanup into a single layer to minimize final image size.
	RUN pixi init && \
		pixi project channel add conda-forge && \
		pixi project channel add bioconda && \
		pixi add python pip openai mcp fastmcp \
		pandas numpy scipy matplotlib pyarrow \
		requests beautifulsoup4 lxml \
		pypdf2 python-docx pillow tiktoken \
		biopython rdkit sqlalchemy networkx \
		scikit-learn seaborn statsmodels openpyxl \
		duckdb sympy pyyaml h5py \
		nodejs && \
		pixi run npm install -g tsx && \
		pixi add --pypi sqlite-vec playwright playwright-stealth && \
		pixi run playwright install chromium && \
		rm -rf ~/.cache/rattler ~/.cache/pip
	

Build the rootless image by running:

    podman build -t ai-forge .
		
---

## 🚀 How to Run & Manage Sessions

### Starting a Chat & CLI Usage
The Overseer can be launched as a standard interactive chat or driven entirely via command-line arguments. To start a basic interactive session:

    pixi run python chat_overseer.py

**CLI Arguments & Headless Execution:**
You can override configurations, pipe in files, and trigger headless background tasks using the built-in CLI flags:
* `-p`, `--prompt`: Pass an initial prompt directly (e.g., `-p "Analyze the data"`).
* `-f`, `--format`: Override the visual format. Options: `markdown` (Rich Markdown panels), `text` (classic streaming text).
* `-v`, `--verbosity`: Override the console output level. Options: `silent` (pure background execution), `minimal` (hides massive JSON args and tool outputs), `standard` (shows AI thinking but hides JSON), `detailed` (shows all raw data).
* `-s`, `--session`: Override the `SESSION_ID` to load or create a specific workspace (e.g., `-s "Project_X"`).
* `-x`, `--exit`: Auto-exit back to your bash terminal when the AI finishes its tasks (perfect for single-shot scripts).
* `--brain`, `--coder`, `--summarizer`, `--adviser`, `--analyst`, `--architect`: Pass the index number of your LLM profiles to override the active models dynamically.

**Advanced Usage Examples:**
* **Rich Interactive Mode with Specific Models:**
  `pixi run python chat_overseer.py -f markdown -v standard -s "Test_2" --brain 2 --coder 3`
* **Headless Background Task (Auto-Exits when done):**
  `pixi run python chat_overseer.py -v silent -s "Research" -x -p "Scrape the latest papers on CRISPR and save to outputs/summary.md"`
* **Unix Pipeline (Pipe files directly into the AI):**
  `cat error_log.txt | pixi run python chat_overseer.py -f text -v minimal -x -p "Find the bug and write a python fix to outputs/fix.py"`

### Session Management (Config Fallback & Idempotent Resumption)
If you don't use the `-s` flag, the framework falls back to your `config.py`. **Always use strings for Session IDs**.
* `SESSION_ID = None` -> Starts a brand new, uniquely timestamped workspace (`Session_ID_<timestamp>`).
* `SESSION_ID = "my_project"` or `SESSION_ID = "Session_ID_my_project"` -> Idempotently loads or restores that specific session without duplicate prefixing, seamlessly recovering all plugins, plan checklists, and long-term state memory across separate process runs.

### Example Interaction Flow

Once the chat starts, try issuing a complex command:

> **YOU:** "Create a tool in the 'math' category that calculates the Fibonacci sequence up to a given number. Then use it to find the sequence up to 15."

**What happens behind the scenes:**
1. The **Brain** checks `view_tool_registry` and realizes the tool doesn't exist.
2. The Brain calls `forge_and_register_plugin` via the secure Podman MCP connection.
3. The **MCP Server** pings the **Coder LLM** through the `host.containers.internal` gateway to write `fibonacci.py`.
4. The system sanitizes the filename and runs a syntax check. If it passes, it updates `tool_registry.json`.
5. The Brain reads the usage instructions, then uses `execute_bash` to run it: `python /app/workspace/plugins/fibonacci.py`.
6. The result is streamed back to your color-coded console.

### Context Compression & Observability
If the session grows too long, the system will inject a dynamic warning prompting the Brain to use the `compress_and_store_context` tool.

1. The Background **Summarizer LLM** activates.
2. It executes a strict 2-step JSON schema pipeline.
3. **Step 1:** Extracts facts into the Memory Registry and saves detailed `.md` files.
4. **Step 2:** Compresses the chat history and injects an active to-do list.
5. The system backs up the bloated history to the `histories/` folder for your observation, and overwrites the active `current_history.json`.
6. The Brain reloads instantly with a clean context window, reads its next steps, and continues working automatically.

* **Infinite Context Scaling (Smart Rolling Summarization):** Standard agents suffer from permanent amnesia when their context window fills up. This framework uses a token-aware rolling summarizer. If the history exceeds 85% of the safe limit, the background manager dynamically calculates the exact token excess, bites off the oldest chunk of messages, and compresses them into a highly dense chronological bulleted list. It recursively splices these summaries back into the history until the context is safe, guaranteeing zero data loss even during multi-day autonomous runs.

---

### 🐝 Swarm Mode (Parallel Execution)
Because the framework strictly isolates state, memory, and Podman execution environments (dynamically generating container names via Session IDs and OS Process IDs), you can run multiple autonomous agents simultaneously on the same machine without them colliding.

You can use `tmux` to launch a multi-agent swarm in a 2x2 split-pane dashboard. Pre-configured benchmark test scripts (`./start_tests_analysis_tmux_1` and `./start_tests_analysis_tmux_2`) are included in the repository root to boot a 4-agent parallel benchmark suite with automated post-task self-analysis.

Alternatively, paste this chained command into your terminal to boot 4 independent researchers at once:

    tmux new-session -d -s forge_swarm \; \
      send-keys "pixi run python chat_overseer.py -f text -s 'Swarm_Text_1'" C-m \; \
      split-window -h \; \
      send-keys "pixi run python chat_overseer.py -f text -s 'Swarm_Text_2'" C-m \; \
      split-window -v \; \
      send-keys "pixi run python chat_overseer.py -f markdown -s 'Swarm_MD_2'" C-m \; \
      select-pane -t 0 \; \
      split-window -v \; \
      send-keys "pixi run python chat_overseer.py -f markdown -s 'Swarm_MD_1'" C-m \; \
      select-layout tiled \; \
      attach-session -t forge_swarm

**Pro-Tip for Swarm Management:** Enable mouse support in your tmux configuration (`echo "set -g mouse on" >> ~/.tmux.conf`) so you can simply click between your agents, drag the window borders to resize their panels, and use your mouse wheel to independently scroll through their individual reasoning logs!

## 📖 Monitoring & Logs

* **Interaction Logs:** Every interaction is mirrored to a timestamped log file inside your active session folder (e.g., `sessions/Session_ID_.../logs/chat_log_<timestamp>.txt`). This includes the Brain's reasoning tokens, tool payloads, and the Coder's intercepted internal thoughts (which are hidden from the Brain to save context space).
* **Token Usage Tracking:** Session-wide token consumption is logged atomically in `sessions/Session_ID_.../state/token_usage.json`. This tracks prompt, completion, and thinking tokens across all roles, including the Brain, Coder, Adviser, Analyst, and **Embedding generation** operations.
* **Context Warning Logic:** The system uses `tiktoken` to estimate the **total official context window** size dynamically, calibrated against real API usage receipts. This estimate accounts for the raw message payload, the size of all registered tool schemas, and chat wrapper template overhead. When the total context usage exceeds 80% of `config.MAX_CONTEXT_TOKENS` (120,000 tokens), the system alerts the Brain to trigger memory compression before hitting hard ceilings.

You can safely type `/exit` or `/quit` at any time to shut down the system and instantly destroy the temporary container.


### ⚠️ Disclaimer: Web Scraping & API Limits
The default web search tool in this framework utilizes Playwright to scrape DuckDuckGo's legacy HTML portal. This is provided for **educational and research purposes only**. Actively bypassing bot protections or aggressively scraping search engines may violate their Terms of Service. If you intend to use this framework for heavy, production-grade autonomous research, you have to replace the `search_web` tool with an official API like Serper.dev, Tavily, or the Google Custom Search API to ensure stability and compliance. The author assumes no liability for IP bans or ToS violations incurred by using this code.