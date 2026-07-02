import os
import asyncio
import json
import time
import re
import copy
import subprocess
import traceback
import logging
import argparse
import sys
import atexit
from datetime import datetime
from openai import AsyncOpenAI
import tiktoken
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# --- MONKEY-PATCH MCP TO PREVENT CRASHES ON STDOUT POLLUTION ---
from mcp.types import JSONRPCMessage

_original_model_validate_json = JSONRPCMessage.model_validate_json

@classmethod
def robust_model_validate_json(cls, json_data: str | bytes, *args, **kwargs):
    if isinstance(json_data, bytes):
        line = json_data.decode('utf-8', errors='replace')
    else:
        line = json_data

    # Try validating directly first
    try:
        return _original_model_validate_json(json_data, *args, **kwargs)
    except Exception as original_exc:
        # Try to extract the JSON-RPC object if there is pollution around it
        try:
            start_idx = line.find('{')
            if start_idx != -1:
                end_idx = line.rfind('}')
                if end_idx != -1 and end_idx > start_idx:
                    cleaned_json = line[start_idx:end_idx + 1]
                    # Verify it's valid JSON
                    json.loads(cleaned_json)
                    return _original_model_validate_json(cleaned_json, *args, **kwargs)
        except Exception:
            pass
        
        # If it's completely non-JSON (like a print or system warning),
        # return a dummy notification to prevent the stdout_reader task from crashing.
        try:
            dummy_notification = '{"jsonrpc": "2.0", "method": "dummy/ignore"}'
            return _original_model_validate_json(dummy_notification, *args, **kwargs)
        except Exception:
            raise original_exc

JSONRPCMessage.model_validate_json = robust_model_validate_json
from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import ANSI
from contextlib import nullcontext

from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel

import config

# Can be run using arguments like this:
# pixi run python chat_overseer.py -p "tell me what tools do you have" -f formatted -s "Test_2" --brain 2 --coder 3 --summarizer 3 --adviser 3 -x

# --- CLI ARGUMENT PARSER ---
parser = argparse.ArgumentParser(description="AI-Forge Overseer")
parser.add_argument("-p", "--prompt", type=str, help="Initial user prompt")
parser.add_argument("-f", "--format", choices=["markdown", "text"], help="Console format override")
parser.add_argument("-v", "--verbosity", choices=["silent", "minimal", "standard", "detailed"], help="Console verbosity override")
parser.add_argument("-s", "--session", type=str, help="Session ID to load or create")
parser.add_argument("-x", "--exit", action="store_true", help="Auto-exit after finishing the CLI prompt")
# Add the LLM profile overrides back:
parser.add_argument("--brain", type=int, help="Brain LLM profile index")
parser.add_argument("--coder", type=int, help="Coder LLM profile index")
parser.add_argument("--summarizer", type=int, help="Summarizer LLM profile index")
parser.add_argument("--adviser", type=int, help="Adviser LLM profile index")
parser.add_argument("--analyst", type=int, help="Analyst LLM profile index")
parser.add_argument("--architect", type=int, help="Architect LLM profile index")

cli_args = parser.parse_args()

# Apply Overrides BEFORE any LLM clients initialize
if cli_args.format: config.FORMAT_MODE = cli_args.format
if cli_args.verbosity: config.VERBOSITY_MODE = cli_args.verbosity
if cli_args.session: config.SESSION_ID = cli_args.session
if cli_args.brain is not None: config.ACTIVE_BRAIN_PROFILE = cli_args.brain
if cli_args.coder is not None: config.ACTIVE_CODER_PROFILE = cli_args.coder
if cli_args.summarizer is not None: config.ACTIVE_SUMMARIZER_PROFILE = cli_args.summarizer
if cli_args.adviser is not None: config.ACTIVE_ADVISER_PROFILE = cli_args.adviser
if cli_args.analyst is not None: config.ACTIVE_ANALYST_PROFILE = cli_args.analyst
if cli_args.architect is not None: config.ACTIVE_ARCHITECT_PROFILE = cli_args.architect

# Capture Prompt (from -p flag OR piped STDIN)
cli_prompt = cli_args.prompt
if not cli_prompt and not sys.stdin.isatty():
    # This grabs piped text like: cat input.txt | python chat_overseer.py
    cli_prompt = sys.stdin.read().strip()
   
# Mute the MCP client's internal info logs
logging.getLogger("mcp").setLevel(logging.WARNING)
logging.getLogger("fastmcp").setLevel(logging.WARNING)

# Initialize the rich console
console = Console()

# --- CLI Colors ---
COLOR_RED = '\033[91m'
COLOR_BLUE = '\033[94m'
COLOR_YELLOW = '\033[93m'
COLOR_BRIGHT_GREEN = '\033[92m'
COLOR_DARK_GREEN = '\033[32m'
COLOR_ORANGE = '\033[38;5;208m' # ANSI 256-color orange
COLOR_DIM = '\033[2m'   # Dim text for "thinking"
COLOR_RESET = '\033[0m'
COLOR_CYAN = '\033[96m'

# --- LLM setups ---
tokenizer = tiktoken.get_encoding("cl100k_base")

brain_profile = config.LLM_PROFILES[config.ACTIVE_BRAIN_PROFILE]
if brain_profile.get("base_url"):
    brain_client = AsyncOpenAI(base_url=brain_profile["base_url"], api_key=brain_profile["api_key"], timeout=180.0)
else:
    brain_client = AsyncOpenAI(api_key=brain_profile["api_key"], timeout=180.0)

# --- SESSION & DIRECTORY SETUP ---
timestamp = datetime.now().strftime("%Y%m%d%H%M%S")

if config.SESSION_ID:
    active_session = f"Session_ID_{config.SESSION_ID}"
else:
    active_session = f"Session_ID_{timestamp}"

SESSION_DIR = os.path.abspath(f"./sessions/{active_session}")
is_resuming = os.path.exists(SESSION_DIR)

# Build the isolated folder structure
os.makedirs(f"{SESSION_DIR}/logs", exist_ok=True)
os.makedirs(f"{SESSION_DIR}/plugins", exist_ok=True)
os.makedirs(f"{SESSION_DIR}/histories", exist_ok=True) 
os.makedirs(f"{SESSION_DIR}/memories", exist_ok=True)
os.makedirs(f"{SESSION_DIR}/state", exist_ok=True)
os.makedirs(f"{SESSION_DIR}/sandbox", exist_ok=True)
os.makedirs(f"{SESSION_DIR}/outputs", exist_ok=True)
os.makedirs(f"{SESSION_DIR}/archive", exist_ok=True)

# Build the isolated folder structure for input/output folders
os.makedirs(config.HOST_INPUT_DIR, exist_ok=True)

# --- Pixi environment already baked into the podman container! ---

# Point the log file to this specific session
LOG_FILE = f"{SESSION_DIR}/logs/chat_log_{timestamp}.txt"
CURRENT_HISTORY_FILE = f"{SESSION_DIR}/state/current_history.json"

def build_skills_menu():
    """Scans the sandbox workspace for SKILL.md files and builds the menu."""
    skills_dir = os.path.join(SESSION_DIR, "skills")
    if not os.path.exists(skills_dir):
        return "AVAILABLE SKILLS MENU:\n- None currently installed. Use commission_architect to build some."

    menu_lines = ["AVAILABLE SKILLS MENU (Use `load_skill` to read full instructions):"]
    for item in sorted(os.listdir(skills_dir)):
        skill_path = os.path.join(skills_dir, item, "SKILL.md")
        if os.path.exists(skill_path):
            try:
                with open(skill_path, "r", encoding="utf-8") as f:
                    content = f.read()
                desc_match = re.search(r'description:\s*(.+)', content)
                description = desc_match.group(1).strip() if desc_match else "No description provided."
                menu_lines.append(f"- {item}: {description}")
            except Exception:
                pass
                
    if len(menu_lines) == 1:
        return "AVAILABLE SKILLS MENU:\n- None currently installed. Use commission_architect to build some."
    return "\n".join(menu_lines)


# --- STATE MANAGEMENT HELPERS ---
def load_history():
    """Loads the true state of the brain from the hard drive."""
    skills_menu = build_skills_menu()
    system_prompt = f"{config.SYSTEM_PROMPTS['brain']}\n\n{skills_menu}"
    
    if not os.path.exists(CURRENT_HISTORY_FILE):
        init_state = [{"role": "system", "content": system_prompt}]
        save_history(init_state)
        return init_state
        
    with open(CURRENT_HISTORY_FILE, "r") as f: 
        messages = json.load(f)
        
    if messages and messages[0]["role"] == "system":
        messages[0]["content"] = system_prompt
        
    return messages

def save_history(messages):
    """Saves the active history. Strips thinking tokens using atomic writes."""
    clean_messages = []
    for msg in messages:
        clean_msg = copy.deepcopy(msg)
        clean_msg.pop("reasoning_content", None)
        clean_messages.append(clean_msg)
        
    # 1. Write to a temporary file
    temp_path = f"{CURRENT_HISTORY_FILE}.{os.getpid()}.tmp"
    with open(temp_path, "w", encoding="utf-8") as f: 
        json.dump(clean_messages, f, indent=4)
        
    # 2. Atomically swap it
    os.replace(temp_path, CURRENT_HISTORY_FILE)
    
def estimate_tokens(messages):
    """Uses tiktoken for highly accurate estimation when the API receipt is voided."""
    total_text = ""
    for msg in messages:
        total_text += str(msg.get("content", ""))
        # Also count tool arguments if they exist
        if "tool_calls" in msg:
            for tc in msg["tool_calls"]:
                total_text += str(tc.get("function", {}).get("arguments", ""))
                
    # Returns the actual token count instead of a character guess
    return len(tokenizer.encode(total_text))


def detect_text_loop(text: str, min_loop_length: int = 120, required_repeats: int = 3) -> tuple[bool, int]:
    """Normalized loop detector. Strips structural formatting noise to catch 
    loops that contain minor spacing, newline, or character variations.
    Returns (True, loop_size) if a loop repeating more than required_repeats is found.
    """
    # Remove all spaces, newlines, and punctuation, forcing pure lowercase alphanumeric text
    clean_text = re.sub(r'[^a-zA-Z0-9]', '', text.lower())
    L = len(clean_text)
    
    if L < min_loop_length * (required_repeats + 1):
        return False, 0
        
    # Widened evaluation window to catch large multi-paragraph loops
    max_check_window = min(L // (required_repeats + 1), 3000)
    for size in range(min_loop_length, max_check_window + 1):
        chunk = clean_text[-size:]
        is_loop = True
        for r in range(1, required_repeats + 1):
            start_idx = - (r + 1) * size
            end_idx = - r * size  # ◄--- FIX: Clean, fixed negative indexing slices perfectly
            prev_chunk = clean_text[start_idx:end_idx]
            if prev_chunk != chunk:
                is_loop = False
                break
        if is_loop:
            return True, size
            
    return False, 0

def clean_tail_by_alphanumeric_count(raw_text: str, alpha_count_to_remove: int) -> str:
    """Removes characters from the trailing end of raw_text until exactly 
    alpha_count_to_remove alphanumeric characters have been stripped out.
    Guarantees perfect word boundaries without trimming trailing words in half.
    """
    removed_alpha = 0
    idx = len(raw_text) - 1
    while idx >= 0 and removed_alpha < alpha_count_to_remove:
        if raw_text[idx].isalnum():
            removed_alpha += 1
        idx -= 1
    return raw_text[:idx + 1]


def log_event(role, content, usage=None, thinking=None, text_color=None, hide_console=False):
    time_str = datetime.now().strftime("%H:%M:%S")
    
    # 1. Plain Text Log File (Always writes full details)
    file_msg = f"\n[{time_str}] === {role.upper()} ===\n"
    if thinking:
        file_msg += f"<thinking>\n{thinking}\n</thinking>\n\n"
    file_msg += f"{content}\n"
    if usage and usage.prompt_tokens is not None:
        file_msg += f"[Tokens: {usage.prompt_tokens} in | {usage.completion_tokens} out]\n"
        
    with open(LOG_FILE, "a", encoding="utf-8") as f: 
        f.write(file_msg)

    # 2. Console logging (Respects Verbosity)
    if config.VERBOSITY_MODE == "silent" or hide_console:
        return

    if role.upper() not in ["BRAIN"]:
        if role.upper().startswith("TOOL"):
            console_header = f"\n{COLOR_BRIGHT_GREEN}[{time_str}] === {role.upper()} ==={COLOR_RESET}"
            actual_color = text_color if text_color else COLOR_DARK_GREEN
            console_content = f"{actual_color}{content}{COLOR_RESET}"
        else:
            console_header = f"\n{COLOR_BLUE}[{time_str}] === {role.upper()} ==={COLOR_RESET}"
            console_content = f"{COLOR_RED}{content}{COLOR_RESET}" if role.upper() in ["USER", "YOU"] else content
            
        print(f"{console_header}\n{console_content}")
        if usage and usage.prompt_tokens is not None:
            print(f"{COLOR_YELLOW}[Tokens: {usage.prompt_tokens} in | {usage.completion_tokens} out]{COLOR_RESET}")
            

active_container_name = None

def cleanup_container():
    """Guarantees the container is killed when the python script exits."""
    if active_container_name:
        if config.VERBOSITY_MODE != "silent":
            print(f"\n{COLOR_CYAN}[SYSTEM] Tearing down container {active_container_name}...{COLOR_RESET}")
        subprocess.run(
            f"podman rm -f {active_container_name}", 
            shell=True, 
            stderr=subprocess.DEVNULL, 
            stdout=subprocess.DEVNULL
        )

# Register the cleanup function to run when the script dies
atexit.register(cleanup_container)

async def run_chat():
    state_dir = os.path.join(SESSION_DIR, "state")
    totals = config.get_token_totals(state_dir)
    grand = totals.get("_grand_total", {"total": 0, "prompt": 0, "completion": 0, "thinking": 0})
    banner = (
        f"Session: [{active_session}]\n"
        f"Brain:      {config.LLM_PROFILES[config.ACTIVE_BRAIN_PROFILE]['name']}\n"
        f"Coder:      {config.LLM_PROFILES[config.ACTIVE_CODER_PROFILE]['name']}\n"
        f"Summarizer: {config.LLM_PROFILES[config.ACTIVE_SUMMARIZER_PROFILE]['name']}\n"
        f"Adviser:    {config.LLM_PROFILES[config.ACTIVE_ADVISER_PROFILE]['name']}\n"
        f"Analyst:    {config.LLM_PROFILES[config.ACTIVE_ANALYST_PROFILE]['name']}\n"
        f"Architect:  {config.LLM_PROFILES[config.ACTIVE_ARCHITECT_PROFILE]['name']}\n"
        f"Log saved to: {LOG_FILE}\n"
        f"Accumulated Session Tokens: {grand['total']} ({grand['prompt']} in, {grand['completion']} out, {grand['thinking']} thinking)"
    )
    log_event("SYSTEM", banner)
    
    prompt_session = None
    quit_app = False
    last_known_tokens = 0 # State tracker for accurate token checking
    tool_schemas_overhead = 0 # Estimate of token cost for registered tool schemas
    
    help_text = "Commands: '/exit' or '/quit' to quit | UI: '/text', '/markdown' | Verbosity: '/silent', '/minimal', '/standard', '/detailed'"
    
    if is_resuming:
        log_event("SYSTEM", f"Successfully restored '{active_session}'. Your forged tools are loaded.\n{help_text}")
    else:
        log_event("SYSTEM", f"Started new workspace: '{active_session}'.\n{help_text}")
        
    skills_menu = build_skills_menu()
    system_prompt = f"{config.SYSTEM_PROMPTS['brain']}\n\n{skills_menu}"
    
    # THE SELF-HEALING CONNECTION LOOP
    while not quit_app:
        global active_container_name
        active_container_name = f"forge_sandbox_{active_session}_{os.getpid()}"
        # Clear stale Podman WSL state before every single boot/reboot ---
        if config.VERBOSITY_MODE != "silent":
            print(f"{COLOR_CYAN}[SYSTEM] Sweeping stale Podman state...{COLOR_RESET}")

        subprocess.run(
            f"podman rm -f --ignore {active_container_name}",
            shell=True, 
            stderr=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL
        )

        try:
            # --- 1. DYNAMICALLY BUILD THE TARGET COMMAND ---
            # Start with the base command
            god_tools_cmd = "pixi run --locked --manifest-path /app/pixi.toml -q python /app/god_tools.py"
            
            if cli_args.coder is not None: god_tools_cmd += f" --coder {cli_args.coder}"
            if cli_args.summarizer is not None: god_tools_cmd += f" --summarizer {cli_args.summarizer}"
            if cli_args.adviser is not None: god_tools_cmd += f" --adviser {cli_args.adviser}"
            if cli_args.analyst is not None: god_tools_cmd += f" --analyst {cli_args.analyst}"
            if cli_args.architect is not None: god_tools_cmd += f" --architect {cli_args.architect}"
                    
            server_params = StdioServerParameters(
                command="podman",
                args=[
                    "--log-level=error",
                    "run", "-i", "--rm",
                    "--init",
                    f"--name={active_container_name}", # True unique identifier
                    "--network=slirp4netns", # networking mode built specifically for rootless Podman
                    "--add-host=host.containers.internal:host-gateway",
                    # Core Security Protections
                    "--security-opt", "no-new-privileges=true",
                    # This safely hides vulnerable kernel symbols without crashing Podman!
                    "--security-opt", "mask=/proc/kallsyms",
                    "--security-opt", "mask=/proc/modules",
                    "--cap-drop=ALL",         # Drop all Linux capabilities
                    "--cpus=4.0",            # Limit to 4 CPU cores
                    "--memory=16g",           # Limit to 16 GB of RAM
                    "--pids-limit=1000",      # Neutralizes bash fork bombs
                    "--userns=keep-id",
                    "--device=nvidia.com/gpu=all", # GPU Passthrough!
#                    "--storage-opt", "size=10G", # Limits the container's scratch space, does not work on WSL2
                    "--env", "PYTHONSAFEPATH=1",
                    "--env", "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/app/.pixi/envs/default/bin:/home/agent/.cargo/bin",
                    # MANDATORY HARDENING: Blinds the container to unused high-risk SUID paths entirely
                    "-v", "/dev/null:/usr/bin/su:ro",
                    "-v", "/dev/null:/usr/bin/mount:ro",
                    "-v", "/dev/null:/usr/bin/passwd:ro",
                    "-v", "/dev/null:/usr/bin/gpasswd:ro",
                    
                    "-v", f"{SESSION_DIR}:/app/workspace:Z",
                    "-v", f"{os.path.abspath('./config.py')}:/app/config.py:ro,Z",
                    "-v", f"{os.path.abspath('./god_tools.py')}:/app/god_tools.py:ro,Z",
                    "-v", f"{os.path.abspath('./chat_overseer.py')}:/app/chat_overseer.py:ro,Z", # it can read own code
                    "-v", f"{config.HOST_INPUT_DIR}:/app/host_input:ro,Z", # same for all sessions, read only
                    
                    "ai-forge",
                    "bash", "-c", f"""
                    # 1. Initialize safe agent Git credentials quietly
                    git config --global user.name "AI-Forge-Agent"
                    git config --global user.email "agent@sandbox.local"

                    # FIX 1: Explicitly tell Git never to print diagnostic hints to stdout
                    git config --global advice.detachedHead false
                    git config --global advice.initBranch false

                    git config --global init.defaultBranch main

                    # 2. Ensure the persistent custom packages folder exists
                    mkdir -p /app/workspace/custom_packages

                    # 3. Setup or detect an active Git repository in the workspace
                    if [ ! -d "/app/workspace/.git" ]; then
                        cd /app/workspace
                        
                        # FIX 2: Mute git init output by redirecting it entirely to /dev/null
                        git init > /dev/null
                        
                        # Create a base ignore pattern so the agent doesn't track massive logs or databases
                        echo -e "logs/\nstate/\nsandbox/\ncustom_packages/\n*.db\n*.tmp\n*.bak" > .gitignore
                        
                        # FIX 3: Mute the initial commit tracking messages
                        git add .gitignore > /dev/null 2>&1
                        git commit -m "chore: initial sandbox tracking initialization" > /dev/null 2>&1
                    fi
                    
                    # 4. APPLICATION PROTECTION: Lock down standard library and user site-packages
                    mkdir -p /home/agent/.local/lib/python3.14/site-packages
                    chmod -R a-w /home/agent/.local/ 2>/dev/null
                    chmod -R a-w /app/__pycache__ 2>/dev/null
                    
                    # 5. HARDENING: Freeze the Pixi environment binary path to block PATH hijacking
                    chmod -R a-w /app/.pixi/envs/default/bin/ 2>/dev/null
                    
                    # 6. CORRECT ENVIRONMENT ALIGNMENT: Prepend framework paths while preserving Pixi site-packages
                    export PYTHONPATH=/app:/app/workspace/custom_packages:$PYTHONPATH
                    
                    # 7. Start the MCP server safely
                    cd /app/workspace
                    {god_tools_cmd}
                    """                   
                ]
            )
            
            async with stdio_client(server_params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    
                    mcp_tools = await session.list_tools()
                    openai_tools = [
                        {
                            "type": "function", 
                            "function": {
                                "name": t.name, 
                                "description": t.description, 
                                "parameters": t.inputSchema,
                                "strict": True
                            }
                        } 
                        for t in mcp_tools.tools
                    ]
                    
                    # Calculate token overhead of the registered tool schemas
                    tool_schemas_overhead = len(tokenizer.encode(json.dumps(openai_tools)))
                    
                    # Initialize tracker outside the loop
                    cli_prompt_consumed = False
                    tool_failure_streaks = {}
                    
                    while True:
                        try:
                            # 1. Check if we have a CLI or Piped prompt to use first
                            if cli_prompt and not cli_prompt_consumed:
                                user_input = cli_prompt
                                cli_prompt_consumed = True
                            else:
                                # 2. Auto-exit for silent mode after CLI prompt is done
                                if cli_prompt and (config.VERBOSITY_MODE == "silent" or cli_args.exit):
                                    quit_app = True
                                    break
                                    
                                # 3. Normal interactive mode
                                # Only initialize the prompter if we actually need human input!
                                if prompt_session is None:
                                    prompt_session = PromptSession()

                                prompt_text = ANSI(f"\n{COLOR_RED}YOU: {COLOR_RESET}")
                                user_input = await prompt_session.prompt_async(prompt_text)
                                

                        # Catch Ctrl+C and Ctrl+D gracefully
                        except KeyboardInterrupt:
                            continue
                        except EOFError:
                            quit_app = True
                            break

                        user_input = user_input.strip()
                        
                        # --- 1. EXIT COMMANDS ---
                        if user_input.lower() in ['/quit', '/exit']: 
                            quit_app = True
                            break
                            
                        # --- 2. DYNAMIC VERBOSITY COMMANDS ---
                        if user_input.lower() in ['/silent', '/minimal', '/standard', '/detailed']:
                            new_mode = user_input.lower()[1:] # Slice off the '/'
                            config.VERBOSITY_MODE = new_mode
                            
                            # Give the user immediate visual feedback
                            print(f"\n{COLOR_BRIGHT_GREEN}[SYSTEM: Console verbosity instantly changed to '{new_mode}']{COLOR_RESET}")
                            continue # Critical: Skip the rest of the loop so the LLM doesn't see this command!

                        # --- 3. DYNAMIC FORMAT COMMANDS ---
                        if user_input.lower() in ['/text', '/markdown']:
                            new_format = user_input.lower()[1:] 
                            config.FORMAT_MODE = new_format
                            
                            print(f"\n{COLOR_BRIGHT_GREEN}[SYSTEM: Console format instantly changed to '{new_format}']{COLOR_RESET}")
                            continue

                        # --- 4. EMPTY INPUT CHECK ---
                        if not user_input: continue
                            
                        log_event("USER", user_input)

                        
                        # Load and save state
                        messages = load_history()
                        messages.append({"role": "user", "content": user_input})
                        save_history(messages)

                        # --- ESCALATING LOOP DETECTION (INIT) ---
                        consecutive_tool_chains = 0
                        
                        while True:
                            try:
                                messages = load_history()
                                
                                # --- 1. TIME INJECTION (Sweep & Replace) ---
                                # Remove any previous system clocks to prevent bloat
                                messages = [msg for msg in messages if not (msg.get("role") == "user" and "[SYSTEM CLOCK:" in str(msg.get("content")))]
                                
                                # Inject the fresh clock
                                live_time = datetime.now().strftime("%A, %B %d, %Y %H:%M:%S")
                                messages.insert(1, {
                                    "role": "user", 
                                    "content": f"[SYSTEM CLOCK: It is currently {live_time}]"
                                })

                                # --- 2. TOKEN WARNING INJECTION ---
                                payload_tokens = estimate_tokens(messages)
                                # Estimate total official context window (messages payload + tool schemas + formatting overhead (4 tokens per message))
                                current_token_estimate = payload_tokens + tool_schemas_overhead + (len(messages) * 4)
                                pct = current_token_estimate / config.MAX_CONTEXT_TOKENS
                                
                                if pct >= 0.85:
                                    # Prevent appending multiple warnings in a row if the AI ignores it for a few turns
                                    messages = [msg for msg in messages if not (msg.get("role") == "user" and "[SYSTEM WARNING: Your context window is at" in str(msg.get("content")))]
                                    
                                    warn_msg = f"[SYSTEM WARNING: Your context window is at ~{pct*100:.0f}%. "
                                    if pct >= 0.95: warn_msg += "CRITICAL LIMIT REACHED. You MUST use the compress_and_store_context tool immediately.]"
                                    else: warn_msg += "Consider finishing your current task and using the compress_and_store_context tool soon.]"
                                    
                                    messages.append({"role": "user", "content": warn_msg})
                                    print(f"\n{COLOR_YELLOW}{warn_msg}{COLOR_RESET}")
                                    log_event("SYSTEM", warn_msg)

                                # --- 3. SAVE THE PURIST HISTORY BEFORE API CALL ---
                                save_history(messages)

                                # Setup API args using the true, saved messages
                                api_args = brain_profile["api_params"].copy()
                                api_args["model"] = brain_profile["model"]
                                api_args["messages"] = messages
                                api_args["tools"] = openai_tools
                                api_args["stream"] = True
                                api_args["stream_options"] = {"include_usage": True}
                                
                                if "seed" in api_args:
                                    api_args["seed"] = api_args.get("seed") or 42

                                if config.VERBOSITY_MODE != "silent":
                                    sys.stderr.write(f"\n{COLOR_YELLOW}[System: Brain payload is ~{payload_tokens} estimated tokens (Estimated total context window: ~{current_token_estimate})]{COLOR_RESET}\n")

                                response_stream = await brain_client.chat.completions.create(**api_args)
                                
                                if config.VERBOSITY_MODE != "silent":
                                    time_str = datetime.now().strftime("%H:%M:%S")
                                    print(f"\n{COLOR_BLUE}[{time_str}] === BRAIN ==={COLOR_RESET}")
                                
                                full_content = ""
                                full_thinking = ""
                                tool_calls_dict = {}
                                final_usage = None
                                loop_interrupted = False  # Track if a loop circuit breaker trips
                                
                                # --- MULTI-MODE CONTEXT ROUTING ---
                                if config.FORMAT_MODE == "markdown" and config.VERBOSITY_MODE != "silent":
                                    display_ctx = Live(console=console, refresh_per_second=4, transient=False)
                                else:
                                    display_ctx = nullcontext()

                                with display_ctx as live:
                                    async for chunk in response_stream:
                                        if chunk.usage:
                                            final_usage = chunk.usage

                                        if not chunk.choices or not chunk.choices[0].delta:
                                            continue
                                    
                                        if len(chunk.choices) > 0:
                                            delta = chunk.choices[0].delta
                                            
                                            chunk_thinking = getattr(delta, 'reasoning_content', None)
                                            if not chunk_thinking and hasattr(delta, "model_extra") and delta.model_extra:
                                                chunk_thinking = delta.model_extra.get("reasoning_content") or delta.model_extra.get("reasoning")
                                            
                                            if chunk_thinking: 
                                                full_thinking += chunk_thinking
                                                # Only check if we have enough content to warrant a loop check
                                                if len(full_thinking) > 500:
                                                    # Feed ONLY the last 10000 characters into the detector.
                                                    tail_buffer = full_thinking[-10000:]
                                                    is_loop, loop_size = detect_text_loop(tail_buffer, required_repeats=3)

                                                    if is_loop:
                                                        print(f"\n{COLOR_RED}[SYSTEM CIRCUIT BREAKER] Thinking repetition detected. Halting stream...{COLOR_RESET}")
                                                        full_thinking = clean_tail_by_alphanumeric_count(full_thinking, loop_size * 3) + "\n\n[SYSTEM NOTICE: Thinking loop halted due to repetition.]"
                                                        loop_interrupted = True
                                                        break

                                            if delta.content: 
                                                full_content += delta.content
                                                # Only check if we have enough content to warrant a loop check
                                                if len(full_content) > 500:
                                                    # Feed ONLY the last 10000 characters into the detector.
                                                    tail_buffer = full_content[-10000:]
                                                    is_loop, loop_size = detect_text_loop(tail_buffer, required_repeats=3)

                                                    if is_loop:
                                                        print(f"\n{COLOR_RED}[SYSTEM CIRCUIT BREAKER] Text output repetition detected. Halting stream...{COLOR_RESET}")
                                                        total_chars_to_purge = loop_size * 3
                                                        full_content = clean_tail_by_alphanumeric_count(full_content, total_chars_to_purge)
                                                        full_content += "\n\n[SYSTEM NOTICE: Generation halted due to a repetition loop.]"
                                                        loop_interrupted = True
                                                        break

                                            if delta.tool_calls:
                                                for tc in delta.tool_calls:
                                                    if tc.index not in tool_calls_dict:
                                                        tool_calls_dict[tc.index] = {
                                                            "id": tc.id, 
                                                            "type": "function", 
                                                            "function": {"name": tc.function.name, "arguments": ""}
                                                        }
                                                    if tc.function.arguments:
                                                        tool_calls_dict[tc.index]["function"]["arguments"] += tc.function.arguments

                                            # --- CONSOLE MODE DISPATCHER ---
                                            if config.VERBOSITY_MODE != "silent":
                                                if config.FORMAT_MODE == "text":
                                                    if chunk_thinking and config.VERBOSITY_MODE in ["standard", "detailed"]:
                                                        print(f"{COLOR_DIM}{chunk_thinking}{COLOR_RESET}", end="", flush=True)
                                                    if delta.content:
                                                        print(delta.content, end="", flush=True)
                                                        
                                                elif config.FORMAT_MODE == "markdown":
                                                    display_elements = []
                                                    if full_thinking and config.VERBOSITY_MODE in ["standard", "detailed"]:
                                                        
                                                        # --- Cap the display height to prevent terminal overflow ---
                                                        think_lines = full_thinking.splitlines()
                                                        max_lines = 15
                                                        
                                                        if len(think_lines) > max_lines:
                                                            display_thinking = "...\n" + "\n".join(think_lines[-max_lines:])
                                                        else:
                                                            display_thinking = full_thinking
                                                            
                                                        display_elements.append(Panel(
                                                            Markdown(display_thinking),
                                                            title="[bold yellow]BRAIN THOUGHTS[/bold yellow]", border_style="yellow", padding=(1, 2), subtitle="[dim white]Internal Logic Loop[/dim white]"
                                                        ))
                                                        
                                                    if full_content:
                                                        display_elements.append(Markdown(full_content))
                                                    
                                                    if not full_content and tool_calls_dict:
                                                        tool_names = [tc['function']['name'] for tc in tool_calls_dict.values()]
                                                        display_elements.append(Markdown(f"*Preparing tool calls: {', '.join(f'`{n}`' for n in tool_names)}...*"))
                                                        
                                                    if display_elements:
                                                        live.update(Group(*display_elements))

                                # If mode is "silent", we do nothing visually!
                                if config.VERBOSITY_MODE != "silent":
                                    print() # Drop a clean newline after the stream is fully finished
                                    
                                assistant_message = {"role": "assistant", "content": full_content}
                                
                                if full_thinking:
                                    assistant_message["reasoning_content"] = full_thinking

                                # Strictly omit any parsed tools if the stream was forcefully aborted
                                if tool_calls_dict and not loop_interrupted:
                                    assistant_message["tool_calls"] = list(tool_calls_dict.values())
                                    
                                messages = load_history()
                                messages.append(assistant_message)
                                
                                # Process state saving depending on loop status
                                if loop_interrupted:
                                    messages.append({
                                        "role": "user", 
                                        "content": (
                                            "[CRITICAL INTERVENTION: Your stream was automatically halted because a highly repetitive text sequence or looping thought-pattern was detected."
                                            "1. IF YOU ARE TRAPPED IN AN UNINTENDED ERROR/CODE LOOP: Stop immediately. Do NOT apologize. Change your strategy, use 'manage_plan' to pivot, or use 'consult_adviser' to analyze your project workspace files. "
                                            "2. IF YOU ARE INTENTIONALLY GENERATING REPETITIVE DATA FOR A USER TEST: Acknowledge that the system's streaming guardrail was tripped by the repetition. Do NOT keep trying to stream the exact same multi-paragraph payload out to the console. Instead, use 'write_file' to dump the requested repetitive dataset cleanly into an output file for the user, summarize what you did in a brief sentence, and wait for new instructions.]"
                                            )
                                    })
                                    save_history(messages)
                                    log_event("BRAIN", full_content, final_usage, full_thinking)
                                    
                                    # ◄--- Keeps session statistics completely accurate! ---
                                    if final_usage:
                                        reasoning_tokens = getattr(final_usage.completion_tokens_details, 'reasoning_tokens', 0) if hasattr(final_usage, 'completion_tokens_details') and final_usage.completion_tokens_details else 0
                                        if reasoning_tokens == 0 and full_thinking: 
                                            reasoning_tokens = len(full_thinking) // 4
                                        config.log_token_usage(os.path.join(SESSION_DIR, "state"), "brain", final_usage.prompt_tokens, final_usage.completion_tokens, reasoning_tokens)

                                    last_known_tokens = 0
                                    consecutive_tool_chains = 0
                                    continue
                                else:
                                    save_history(messages)
                                    log_event("BRAIN", full_content, final_usage, full_thinking)

                                # Update exact token count state for the next loop!
                                if final_usage:
                                    last_known_tokens = final_usage.prompt_tokens + final_usage.completion_tokens
                                    reasoning_tokens = 0
                                    if hasattr(final_usage, 'completion_tokens_details') and final_usage.completion_tokens_details:
                                        reasoning_tokens = getattr(final_usage.completion_tokens_details, 'reasoning_tokens', 0)
                                    
                                    if reasoning_tokens == 0 and full_thinking:
                                        reasoning_tokens = len(full_thinking) // 4
                                        
                                    config.log_token_usage(
                                        os.path.join(SESSION_DIR, "state"),
                                        "brain",
                                        final_usage.prompt_tokens,
                                        final_usage.completion_tokens,
                                        reasoning_tokens
                                    )
                                        
                                    if config.VERBOSITY_MODE != "silent":   
                                        if reasoning_tokens > 0:
                                            print(f"{COLOR_YELLOW}[Tokens: {final_usage.prompt_tokens} in | {final_usage.completion_tokens} out (~{reasoning_tokens} thinking)]{COLOR_RESET}")
                                        else:
                                            print(f"{COLOR_YELLOW}[Tokens: {final_usage.prompt_tokens} in | {final_usage.completion_tokens} out]{COLOR_RESET}")

                                if not tool_calls_dict:
                                    break

                                # --- Initialize the RAM cache for parallel tool outputs ---
                                executed_tool_outputs = {}

                                for tc_data in assistant_message["tool_calls"]:
                                    name = tc_data["function"]["name"]
                                    args_str = tc_data["function"]["arguments"]
                                    tc_id = tc_data["id"]
                                    
                                    # --- Intercept and self-heal bad JSON ---
                                    try:
                                        args = json.loads(args_str)
                                    except json.JSONDecodeError:
                                        error_msg = "SYSTEM ERROR: You provided invalid JSON arguments for this tool call. Please check your syntax (watch out for unescaped quotes or missing brackets) and try again."
                                        print(f"{COLOR_RED}Error decoding JSON. Intercepting and asking Brain to retry...{COLOR_RESET}")
                                        log_event("TOOL CALL", f"Requesting: {name}\nArgs: [MALFORMED JSON]\n{args_str}")
                                        log_event("TOOL RESULT (0.00s)", error_msg, text_color=COLOR_RED)
                                        
                                        # Cache the error so it survives compression!
                                        executed_tool_outputs[tc_id] = error_msg 
                                        
                                        messages = load_history()
                                        messages.append({
                                            "role": "tool",
                                            "tool_call_id": tc_id,
                                            "name": name,
                                            "content": error_msg
                                        })
                                        save_history(messages)
                                        last_known_tokens = 0
                                        continue 

                                    # Hide massive JSON strings from console if in minimal/standard
                                    hide_args = config.VERBOSITY_MODE in ["minimal", "standard"]
                                    log_event("TOOL CALL", f"Requesting: {name}\nArgs: {json.dumps(args, indent=2)}", hide_console=hide_args)

                                    if config.VERBOSITY_MODE != "silent":                                    
                                        if name == "forge_and_register_plugin":
                                            print(f"\n{COLOR_ORANGE}▶ Routing to Coder for plugin forging...{COLOR_RESET}")
                                        elif name == "compress_and_store_context":
                                            print(f"\n{COLOR_ORANGE}▶ Triggering Memory Manager Pipeline... Awaiting response...{COLOR_RESET}")
                                        elif name == "consult_adviser":
                                            print(f"\n{COLOR_ORANGE}▶ Consulting Senior Adviser... Awaiting strategic report...{COLOR_RESET}")
                                        elif name == "query_universal_llm":
                                            print(f"\n{COLOR_ORANGE}▶ Spawning Sub-Agent... Awaiting response...{COLOR_RESET}")
                                        elif name == "analyze_files":
                                            print(f"\n{COLOR_ORANGE}▶ Passing files to The Analyst... Awaiting report...{COLOR_RESET}")
                                        elif name == "commission_architect":
                                            print(f"\n{COLOR_ORANGE}▶ Waking up the Architect to draft skill...{COLOR_RESET}")
                                        elif hide_args:
                                            # If we hid the JSON args, print a clean 1-liner so the user knows it's doing something!
                                            print(f"{COLOR_ORANGE}▶ Running tool: {name}...{COLOR_RESET}")
                                            
                                    start = time.time()
                                    state_dir = os.path.join(SESSION_DIR, "state")
                                    totals_before = config.get_token_totals(state_dir)
                                    
                                    result = await session.call_tool(name, args)
                                    output = result.content[0].text
                                    
                                    totals_after = config.get_token_totals(state_dir)
                                    token_diff = config.get_totals_diff(totals_before, totals_after)
                                    if token_diff and config.VERBOSITY_MODE != "silent":
                                        diff_parts = []
                                        for agent, details in token_diff.items():
                                            part = f"{agent}: +{details['total']} ({details['prompt']} in, {details['completion']} out"
                                            if details['thinking'] > 0:
                                                part += f", {details['thinking']} thinking"
                                            part += ")"
                                            diff_parts.append(part)
                                        print(f"{COLOR_YELLOW}[Tokens used: {', '.join(diff_parts)}]{COLOR_RESET}")
                                    
                                    # --- NEW: Save the real output to our RAM dictionary immediately ---
                                    executed_tool_outputs[tc_id] = output

                                    # --- FAILURE STREAK TRACKER ---
                                    # Determine if the output looks like an error
                                    is_error = False
                                    output_lower = output.lower()
                                    if "system error:" in output_lower or "traceback (most recent" in output_lower or "error executing" in output_lower:
                                        is_error = True
                                    elif "exit code:" in output_lower and "exit code: 0" not in output_lower:
                                        is_error = True

                                    # Update the streak
                                    if is_error:
                                        tool_failure_streaks[name] = tool_failure_streaks.get(name, 0) + 1
                                    else:
                                        tool_failure_streaks[name] = 0 # Reset on success!

                                    # Trigger the intervention if stuck
                                    if tool_failure_streaks.get(name, 0) >= 5:
                                        intervention_msg = (
                                            f"[CRITICAL SYSTEM ALERT: You have failed to use the '{name}' tool {tool_failure_streaks[name]} times in a row. "
                                            f"YOU ARE STUCK IN A LOOP. You MUST STOP trying the exact same command. "
                                            f"Reflect on why this is failing. Consider using 'consult_adviser' for a new strategy, "
                                            f"or use 'fetch_webpage' to read the documentation for what you are trying to do.]"
                                        )
                                        # Append the warning directly to the output so the Brain reads it immediately
                                        output += f"\n\n{intervention_msg}"
                                        
                                        if config.VERBOSITY_MODE != "silent":
                                            print(f"\n{COLOR_RED}[SYSTEM: AI is stuck looping on {name}. Injecting forced intervention!]{COLOR_RESET}")
                                        
                                        log_event("SYSTEM", f"Forced intervention triggered for tool: {name}")

                                    coder_thoughts = ""
                                    coder_code = ""

                                    if "<___CODER_THOUGHTS___>" in output:
                                        match = re.search(r"<___CODER_THOUGHTS___>(.*?)</___CODER_THOUGHTS___>", output, re.DOTALL)
                                        if match: coder_thoughts = match.group(1).strip()
                                        output = re.sub(r"<___CODER_THOUGHTS___>.*?</___CODER_THOUGHTS___>", "", output, flags=re.DOTALL).strip()

                                    if "<___CODER_CODE___>" in output:
                                        match = re.search(r"<___CODER_CODE___>(.*?)</___CODER_CODE___>", output, re.DOTALL)
                                        if match: coder_code = match.group(1).strip()
                                        output = re.sub(r"<___CODER_CODE___>.*?</___CODER_CODE___>", "", output, flags=re.DOTALL).strip()


                                    if coder_thoughts or coder_code:
                                        time_str = datetime.now().strftime("%H:%M:%S")
                                        log_text = f"\n[{time_str}] === CODER (HIDDEN) ===\n"

                                        hide_coder = config.VERBOSITY_MODE in ["silent", "minimal", "standard"]

                                        if not hide_coder:
                                            print(f"\n{COLOR_ORANGE}[{time_str}] === CODER (HIDDEN) ==={COLOR_RESET}")

                                        if coder_thoughts:
                                            log_text += f"--- THOUGHTS ---\n{coder_thoughts}\n\n"
                                            if not hide_coder:
                                                print(f"{COLOR_DIM}--- THOUGHTS ---\n{coder_thoughts}\n{COLOR_RESET}")

                                        if coder_code:
                                            log_text += f"--- GENERATED CODE ---\n{coder_code}\n\n"
                                            if not hide_coder:
                                                print(f"--- GENERATED CODE ---\n{coder_code}\n")

                                        with open(LOG_FILE, "a", encoding="utf-8") as f:
                                            f.write(log_text)

                                    out_color = COLOR_ORANGE if name in ["forge_and_register_plugin", "compress_and_store_context", "commission_architect", "consult_adviser", "query_universal_llm", "analyze_files"] else COLOR_DARK_GREEN
                                    
                                    # Hide massive output dumps from console if in minimal/standard
                                    hide_output = config.VERBOSITY_MODE in ["minimal", "standard"]
                                    log_event(f"TOOL RESULT ({time.time() - start:.2f}s)", output, text_color=out_color, hide_console=hide_output)
                                    
                                    if hide_output and config.VERBOSITY_MODE != "silent":
                                        if name in ["commission_architect", "consult_adviser", "query_universal_llm", "analyze_files", "forge_and_register_plugin"]:
                                            print(f"{out_color}{output}{COLOR_RESET}")
                                        else:
                                            print(f"{COLOR_DARK_GREEN}✓ Tool '{name}' completed ({time.time() - start:.2f}s).{COLOR_RESET}")

                                    if name == "compress_and_store_context":
                                        print(f"\n{COLOR_ORANGE}[SYSTEM] Memory compression cycle complete. Waking up with pristine context...{COLOR_RESET}")
                                        last_known_tokens = 0 
                                        consecutive_tool_chains = 0
                                        break

                                    else:
                                        messages = load_history()
                                        messages.append({
                                            "role": "tool",
                                            "tool_call_id": tc_id,
                                            "name": name,
                                            "content": output
                                        })
                                        save_history(messages)
                                        last_known_tokens = 0

                                # --- ESCALATING LOOP DETECTION (CHECK) ---
                                consecutive_tool_chains += 1

                                if consecutive_tool_chains >= 1000:
                                    # Hard Stop: Protect the API limits
                                    halt_msg = f"[SYSTEM METRIC: You have executed {consecutive_tool_chains} consecutive tool chains. For safety and observability, you MUST STOP using tools now. Summarize your progress and ask the user for permission to continue.]"
                                    messages = load_history()
                                    messages.append({"role": "user", "content": halt_msg})
                                    save_history(messages)
                                    print(f"\n{COLOR_YELLOW}[SYSTEM: Hard pause triggered ({consecutive_tool_chains} chains). Forcing Brain to wait for user.]{COLOR_RESET}")
                                    log_event("SYSTEM", halt_msg)
                                    consecutive_tool_chains = 0
                                
                                elif consecutive_tool_chains > 0 and consecutive_tool_chains % 300 == 0:
                                    # Sweep old soft-pauses
                                    messages = [msg for msg in messages if not (msg.get("role") == "user" and "[SYSTEM METRIC: You have executed" in str(msg.get("content")))]
                                    # Soft Reflection: Ask the AI to evaluate itself
                                    eval_msg = f"[SYSTEM METRIC: You have executed {consecutive_tool_chains} consecutive tool chains. Please review your recent actions. Are you making steady progress, or are you stuck in an error loop? If you are stuck or repeatedly failing, STOP using tools and ask the user for input. If you are making legitimate progress, continue.]"
                                    messages = load_history()
                                    messages.append({"role": "user", "content": eval_msg})
                                    save_history(messages)
                                    print(f"\n{COLOR_YELLOW}[SYSTEM: Soft pause triggered ({consecutive_tool_chains} chains). Prompting Brain to self-evaluate.]{COLOR_RESET}")
                                    log_event("SYSTEM", eval_msg)
                              
                                    
                            except (KeyboardInterrupt, asyncio.CancelledError):
                                print(f"\n\n{COLOR_RED}[SYSTEM] 🛑 Process manually interrupted! Returning to prompt...{COLOR_RESET}")
                                log_event("SYSTEM", "Process manually interrupted by user.")
                                
                                messages = load_history()
                                messages.append({
                                     "role": "user", 
                                     "content": "[SYSTEM ALERT: The user pressed Ctrl+C to instantly abort the previous text generation or tool execution. Stop what you were doing, acknowledge the interruption, and await new instructions.]"
                                 })
                                save_history(messages)
                                break
                        if config.VERBOSITY_MODE != "silent":
                            state_dir = os.path.join(SESSION_DIR, "state")
                            totals = config.get_token_totals(state_dir)
                            if totals:
                                totals_parts = []
                                for agent, details in totals.items():
                                    if agent == "_grand_total":
                                        continue
                                    part = f"{agent}: {details['total']}"
                                    if details['thinking'] > 0:
                                        part += f" ({details['thinking']} thinking)"
                                    totals_parts.append(part)
                                grand = totals.get("_grand_total", {"total": 0, "thinking": 0})
                                grand_str = f"grand: {grand['total']}"
                                if grand['thinking'] > 0:
                                    grand_str += f" ({grand['thinking']} thinking)"
                                totals_parts.append(grand_str)
                                print(f"\n{COLOR_YELLOW}[Session Totals: {' | '.join(totals_parts)}]{COLOR_RESET}")
                                
        # 3. CATCH DEAD CONTAINERS AND RESTART
        except (KeyboardInterrupt, asyncio.CancelledError):
            print(f"\n{COLOR_YELLOW}[SYSTEM] Hard interrupt detected. Resetting sandbox...{COLOR_RESET}")
            await asyncio.sleep(1)
        except Exception as e:
            # Unwraps ExceptionGroups to print the real underlying API/network failures
            if type(e).__name__ in ["BaseExceptionGroup", "ExceptionGroup"] and hasattr(e, "exceptions"):
                print(f"\n{COLOR_RED}[CRASH DETECTED] Wrapped Exception Group Context Triggered:{COLOR_RESET}")
                for sub_exception in e.exceptions:
                    print(f"{COLOR_YELLOW}- Internal Error Type: {type(sub_exception).__name__}: {str(sub_exception)}{COLOR_RESET}")
                    print(f"{COLOR_DIM}--- SUB-TRACEBACK ---{COLOR_RESET}")
                    traceback.print_exception(type(sub_exception), sub_exception, sub_exception.__traceback__)
                    print(f"{COLOR_DIM}---------------------{COLOR_RESET}")
            else:
                print(f"\n{COLOR_RED}[CRASH DETECTED] {type(e).__name__}: {str(e)}{COLOR_RESET}")
                print(f"{COLOR_YELLOW}--- TRACEBACK ---{COLOR_RESET}")
                traceback.print_exc()
                print(f"{COLOR_YELLOW}-----------------{COLOR_RESET}")
                print(f"\n{COLOR_YELLOW}[SYSTEM] Sandbox connection dropped or API failed. Restarting loop...{COLOR_RESET}")
            await asyncio.sleep(1)

if __name__ == "__main__":
    try:
        asyncio.run(run_chat())
    except KeyboardInterrupt:
        print(f"\n{COLOR_RED}[SYSTEM] Forced shutdown. Goodbye!{COLOR_RESET}")