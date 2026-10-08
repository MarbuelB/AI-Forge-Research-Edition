import asyncio
import signal
import os
import json
import subprocess
import traceback
import logging
import re
import sys
import shutil
import shlex
import argparse
import sqlite3
import sqlite_vec
import array
import base64
import urllib.parse
import mimetypes
import socket
import ipaddress
from typing import Any
from datetime import datetime
from openai import AsyncOpenAI
from playwright.async_api import async_playwright
from playwright_stealth import Stealth
from bs4 import BeautifulSoup

import tiktoken
tokenizer = tiktoken.get_encoding("cl100k_base")

import config

# --- HIDE FASTMCP INTERNAL LOGS BEFORE IMPORT ---
# We MUST set this before importing FastMCP, otherwise it reads the default settings!
os.environ["FASTMCP_LOG_ENABLED"] = "false"
from fastmcp import FastMCP


# --- CATCH CLI ARGUMENTS FROM PODMAN ---
parser = argparse.ArgumentParser(description="Forge God Tools (Containerized)")
parser.add_argument("--coder", type=int, help="Coder LLM profile index")
parser.add_argument("--summarizer", type=int, help="Summarizer LLM profile index")
parser.add_argument("--adviser", type=int, help="Adviser LLM profile index")
parser.add_argument("--analyst", type=int, help="Analyst LLM profile index")
parser.add_argument("--architect", type=int, help="Architect LLM profile index")
args, unknown = parser.parse_known_args() # Ignore other arguments Podman might pass
# --- HIDE ARGUMENTS FROM FASTMCP ---
sys.argv = [sys.argv[0]] + unknown

# --- OVERRIDE CONFIG IN MEMORY ---
if args.coder is not None: config.ACTIVE_CODER_PROFILE = args.coder
if args.summarizer is not None: config.ACTIVE_SUMMARIZER_PROFILE = args.summarizer
if args.adviser is not None: config.ACTIVE_ADVISER_PROFILE = args.adviser
if args.analyst is not None: config.ACTIVE_ANALYST_PROFILE = args.analyst
if args.architect is not None: config.ACTIVE_ARCHITECT_PROFILE = args.architect

## Start the MCP server
mcp = FastMCP("TheForge")


# --- MCP STREAM PROTECTION & LOGGING ---
# Suppress stdout logging to protect FastMCP, but route logs to a file for debugging
log_file_path = "/app/workspace/logs/container_debug.log"
os.makedirs(os.path.dirname(log_file_path), exist_ok=True)

logging.basicConfig(
    filename=log_file_path,
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
# Forcefully clear any existing console handlers that might corrupt the JSON-RPC stream
logging.getLogger().handlers = [h for h in logging.getLogger().handlers if isinstance(h, logging.FileHandler)]
logger = logging.getLogger("ForgeContainer")


WORKSPACE_DIR = "/app/workspace"
STATE_DIR = os.path.join(WORKSPACE_DIR, "state")
SANDBOX_DIR = os.path.join(WORKSPACE_DIR, "sandbox")
PLUGINS_DIR = os.path.join(WORKSPACE_DIR, "plugins")
_LOADED_SKILLS = set()
MEMORIES_DIR = os.path.join(WORKSPACE_DIR, "memories")
HISTORIES_DIR = os.path.join(WORKSPACE_DIR, "histories")
TOOL_REGISTRY_FILE = os.path.join(STATE_DIR, "tool_registry.json")
MEMORY_REGISTRY_FILE = os.path.join(STATE_DIR, "memory_registry.json")
CURRENT_HISTORY_FILE = os.path.join(STATE_DIR, "current_history.json")

coder_profile = config.LLM_PROFILES[config.ACTIVE_CODER_PROFILE]
summarizer_profile = config.LLM_PROFILES[config.ACTIVE_SUMMARIZER_PROFILE]
adviser_profile = config.LLM_PROFILES[config.ACTIVE_ADVISER_PROFILE]

if coder_profile.get("base_url"):
    coder_client = AsyncOpenAI(base_url=coder_profile["base_url"], api_key=coder_profile["api_key"], timeout=120.0)
else:
    coder_client = AsyncOpenAI(api_key=coder_profile["api_key"], timeout=120.0)
    
if summarizer_profile.get("base_url"):
    summarizer_client = AsyncOpenAI(base_url=summarizer_profile["base_url"], api_key=summarizer_profile["api_key"], timeout=180.0)
else:
    summarizer_client = AsyncOpenAI(api_key=summarizer_profile["api_key"], timeout=180.0)

if adviser_profile.get("base_url"):
    adviser_client = AsyncOpenAI(base_url=adviser_profile["base_url"], api_key=adviser_profile["api_key"], timeout=300.0)
else:
    adviser_client = AsyncOpenAI(api_key=adviser_profile["api_key"], timeout=300.0)

analyst_profile = config.LLM_PROFILES[config.ACTIVE_ANALYST_PROFILE]

if analyst_profile.get("base_url"):
    analyst_client = AsyncOpenAI(base_url=analyst_profile["base_url"], api_key=analyst_profile["api_key"], timeout=300.0)
else:
    analyst_client = AsyncOpenAI(api_key=analyst_profile["api_key"], timeout=300.0)

architect_profile = config.LLM_PROFILES[config.ACTIVE_ARCHITECT_PROFILE]

if architect_profile.get("base_url"):
    architect_client = AsyncOpenAI(base_url=architect_profile["base_url"], api_key=architect_profile["api_key"], timeout=180.0)
else:
    architect_client = AsyncOpenAI(api_key=architect_profile["api_key"], timeout=180.0)
    
# --- Initialize Universal Client ---
uni_config = config.UNIVERSAL_LLM_CONFIG
universal_client = AsyncOpenAI(
    base_url=uni_config["base_url"], 
    api_key=uni_config["api_key"], 
    timeout=uni_config["timeout"]
)

# --- Initialize Embedding Client ---
emb_config = config.EMBEDDING_CONFIG
embedding_client = AsyncOpenAI(
    base_url=emb_config["base_url"], 
    api_key=emb_config["api_key"], 
    timeout=emb_config["timeout"]
)

# --- HELPER FUNCTIONS ---
def extract_thinking_and_content(message) -> tuple[str, str]:
    """Extracts thinking/reasoning content and main text content from an OpenAI message object."""
    thinking = getattr(message, 'reasoning_content', None)
    if not thinking and hasattr(message, 'model_extra') and message.model_extra:
        thinking = message.model_extra.get('reasoning_content') or message.model_extra.get('reasoning')
    
    content = message.content or ""
    if not thinking and "<think>" in content:
        think_match = re.search(r"<think>(.*?)</think>", content, re.DOTALL)
        if think_match:
            thinking = think_match.group(1).strip()
            content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
            
    return (thinking or "").strip(), (content or "").strip()

def load_json(filepath):
    if not os.path.exists(filepath): return {}
    with open(filepath, "r") as f: return json.load(f)

def save_json(filepath, data):
    """Saves JSON using an atomic write to prevent corruption on crash."""
    temp_path = f"{filepath}.tmp"
    
    # 1. Write to a temporary file first
    with open(temp_path, "w", encoding="utf-8") as f: 
        json.dump(data, f, indent=4)
        
    # 2. Atomically swap it with the real file
    # If the system crashes during step 1, the original file is untouched!
    os.replace(temp_path, filepath)

def save_failure_trace(subagent_name: str, payload: dict) -> str:
    """Saves a failure trace / post-mortem diagnostic artifact to state/ for full observability."""
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"failure_{subagent_name}_{timestamp}.json"
        filepath = os.path.join(STATE_DIR, filename)
        save_json(filepath, payload)
        return filepath
    except Exception as e:
        logger.error(f"[save_failure_trace] Failed to save trace for {subagent_name}: {e}")
        return ""

def sqlite_authorizer(action, arg1, arg2, dbname, source):
    # 9 = SQLITE_DELETE, 11 = SQLITE_DROP_TABLE
    if action in (sqlite3.SQLITE_DELETE, sqlite3.SQLITE_DROP_TABLE):
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK

def get_payload_tokens(messages):
    """Accurately counts text tokens and adds a safe mathematical buffer for images."""
    total_text = ""
    image_count = 0
    
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, list):  # Handle vision/multi-part arrays
            for part in content:
                if part.get("type") == "text":
                    total_text += part.get("text", "")
                elif part.get("type") == "image_url":
                    image_count += 1
        else:  # Handle standard string content
            total_text += str(content)
            
        # Count tool arguments if they exist
        if "tool_calls" in msg and msg["tool_calls"]:
            for tc in msg["tool_calls"]:
                total_text += str(tc.get("function", {}).get("arguments", ""))
            
    # Measure exact text tokens
    text_tokens = len(tokenizer.encode(total_text))
    
    # Add a safe buffer for images (Most vision models charge ~1000 tokens per image)
    image_tokens = image_count * 1000 
    
    return text_tokens + image_tokens


def read_file_slice(
    filepath: str,
    start_line: int = 1,
    max_lines: int = None,
    tail_mode: bool = False,
    max_chars: int = 50000
) -> tuple[str, int, int, int, bool]:
    """Reads a bounded slice of a text file, reporting exact line numbers and coverage.
    Returns: (content, line_start_idx, line_end_idx, total_lines, is_partial)
    """
    file_size = os.path.getsize(filepath)
    if file_size == 0:
        return "", 0, 0, 0, False

    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
        all_lines = f.readlines()

    total_lines = len(all_lines)
    if total_lines == 0:
        return "", 0, 0, 0, False

    if tail_mode:
        candidate_lines = all_lines[-max_lines:] if (max_lines and max_lines > 0) else all_lines
        selected = []
        chars = 0
        for line in reversed(candidate_lines):
            if chars + len(line) > max_chars and selected:
                break
            selected.append(line)
            chars += len(line)
        selected.reverse()
        start_idx = total_lines - len(selected) + 1
        end_idx = total_lines
        content = "".join(selected)
    elif start_line > 1 or (max_lines is not None and max_lines > 0):
        s_idx = max(0, start_line - 1)
        e_idx = s_idx + max_lines if (max_lines and max_lines > 0) else total_lines
        candidate_lines = all_lines[s_idx:e_idx]
        selected = []
        chars = 0
        for line in candidate_lines:
            if chars + len(line) > max_chars and selected:
                break
            selected.append(line)
            chars += len(line)
        start_idx = s_idx + 1
        end_idx = s_idx + len(selected)
        content = "".join(selected)
    else:
        selected = []
        chars = 0
        for line in all_lines:
            if chars + len(line) > max_chars and selected:
                break
            selected.append(line)
            chars += len(line)
        start_idx = 1
        end_idx = len(selected)
        content = "".join(selected)

    is_partial = (len(content) < file_size)
    return content, start_idx, end_idx, total_lines, is_partial


def gather_agent_context(filepaths: list[str] = None, max_chars_per_file: int = 40000) -> str:
    """Natively extracts and strings together absolute file contents inside the sandbox environment, 
    allowing sub-agents to read project code states without blowing out the main Overseer memory bank.
    """
    if not filepaths:
        return ""
        
    context_str = "\n\n=== ATTACHED AGENT CONTEXT BACKGROUND ENVIRONMENT ==="
    for path in filepaths:
        resolved_path = os.path.realpath(path)
        if os.path.exists(resolved_path):
            try:
                if os.path.commonpath([resolved_path, "/app/workspace"]) != "/app/workspace":
                    context_str += f"\n\n[ACCESS DENIED: Path '{os.path.basename(resolved_path)}' falls outside safe workspace boundaries.]"
                    continue
                    
                # Check raw file size before reading to protect system memory
                file_size_bytes = os.path.getsize(resolved_path)
                if file_size_bytes > 5_000_000: # 5MB limit safety cutoff
                    context_str += f"\n\n--- REFERENCE FILE STATE: {os.path.basename(resolved_path)} ---\n[SYSTEM NOTICE: This file is too massive ({file_size_bytes / 1024 / 1024:.2f} MB) to read directly. Use dedicated grep or chunk analysis tools.]"
                    continue

                content, start_idx, end_idx, total_lines, is_partial = read_file_slice(
                    resolved_path, max_chars=max_chars_per_file
                )
                est_tokens = len(tokenizer.encode(content))
                
                if is_partial:
                    pct = max(1, int((len(content) / file_size_bytes) * 100)) if file_size_bytes > 0 else 100
                    context_str += (
                        f"\n\n--- REFERENCE FILE STATE: {os.path.basename(resolved_path)} (~{est_tokens} tokens) "
                        f"[PARTIALLY INGESTED: lines {start_idx}–{end_idx} of {total_lines:,} ({pct}% coverage)] ---\n"
                        f"[COVERAGE BOUNDARY: Read first {len(content):,} of {file_size_bytes:,} chars (lines {start_idx} to {end_idx}). "
                        f"The remaining {total_lines - end_idx:,} lines were omitted to protect sub-agent context budget. "
                        f"ANTI-HALLUCINATION DIRECTIVE: Do NOT assume or guess function signatures, logic, or variables in unread lines {end_idx + 1}–{total_lines:,}.]\n"
                        f"{content}\n"
                        f"... [COVERAGE WARNING: TRUNCATED NATIVELY — Read lines {start_idx}–{end_idx} of {total_lines:,} ({pct}%). "
                        f"Lines {end_idx + 1} to {total_lines:,} omitted. To inspect downstream lines, slice with bash 'sed -n \\'{end_idx + 1},{end_idx + 500}p\\' <path>' or 'grep'.] ..."
                    )
                else:
                    context_str += f"\n\n--- REFERENCE FILE STATE: {os.path.basename(resolved_path)} (~{est_tokens} tokens, {total_lines:,} lines) [FULL CONTENT] ---\n{content}"
            except Exception as e:
                context_str += f"\n\n[READ FAULT: {os.path.basename(resolved_path)} - Error: {str(e)}]"
        else:
            context_str += f"\n\n[FILE TARGET NOT FOUND: {path}]"
            
    return context_str + "\n=======================================================\n\n"


# --- MCP TOOLS ---
@mcp.tool()
def view_tool_registry(category: str = None) -> str:
    """Views the custom forged tools registry. Pass NO arguments to see top-level categories. Pass a category string to see detailed tools inside it."""
    registry = load_json(TOOL_REGISTRY_FILE)
    if not registry: return "Tool Registry is empty."
    
    if not category:
        summary = {cat: data.get("category_description", "") for cat, data in registry.items()}
        return json.dumps({"categories": summary}, indent=2) + "\n\nCall this tool again with a specific category name to see its tools."
    else:
        if category in registry:
            return json.dumps({category: registry[category]["tools"]}, indent=2)
        else:
            return f"Category '{category}' not found. Available categories are: {list(registry.keys())}"


@mcp.tool()
def manage_plan(action: str, content: str = None) -> str:
    """Reads or completely overwrites the Master Project Plan.
    'action' must be 'read' or 'write'.
    If action is 'write', you MUST provide the full, updated markdown text in 'content'.
    """
    plan_path = os.path.join(STATE_DIR, "active_plan.md")
    normalized_action = action.strip().rstrip(">").rstrip(":").strip().lower() if action else ""
    
    if normalized_action == "read":
        if os.path.exists(plan_path):
            with open(plan_path, "r", encoding="utf-8") as f:
                return f"--- CURRENT MASTER PLAN ---\n{f.read()}"
        else:
            return "No active plan exists yet. Please initialize one using the 'write' action."
            
    elif normalized_action == "write":
        if not content:
            return "Error: You must provide the full markdown string in the 'content' argument to write."
        if os.path.exists(plan_path):
            try:
                with open(plan_path, "r", encoding="utf-8") as f_cur:
                    current_plan = f_cur.read()
                if current_plan.strip() == content.strip():
                    return "SUCCESS: Master Plan is already up to date (no changes detected)."
            except Exception:
                pass
        with open(plan_path, "w", encoding="utf-8") as f:
            f.write(content)
        return "SUCCESS: Master Plan has been updated and saved to disk."
        
    else:
        return f"Error: Invalid action '{action}'. Valid actions are 'read' and 'write'."

@mcp.tool()
async def consult_adviser(current_plan: str, encountered_problems: str, context_filepaths: list[str] = None) -> str:
    """Consults the Senior Adviser AI for strategic guidance.
    Pass your current plan and a detailed description of the problems or bottlenecks you are facing.
    The Adviser will review your available tools and memories and return a strategic document.
    'context_filepaths' provides explicit absolute paths to failing scripts, profiles, or log segments to audit.
    """
    tool_registry = load_json(TOOL_REGISTRY_FILE)
    memory_registry = load_json(MEMORY_REGISTRY_FILE)
    
    # ◄--- Gather background file targets ---
    file_environmental_context = gather_agent_context(context_filepaths)
    
    user_prompt = (
        f"--- CURRENT TOOL REGISTRY ---\n{json.dumps(tool_registry, indent=2)}\n\n"
        f"--- CURRENT MEMORY REGISTRY ---\n{json.dumps(memory_registry, indent=2)}\n\n"
        f"--- ATTACHED ENVIRONMENT CODE ANALYSIS EVIDENCE ---\n{file_environmental_context}\n"
        f"--- CURRENT PLAN ---\n{current_plan}\n\n"
        f"--- ENCOUNTERED PROBLEMS & REQUEST FOR ADVICE ---\n{encountered_problems}"
    )

    api_args = adviser_profile["api_params"].copy()
    api_args["model"] = adviser_profile["model"]
    api_args["messages"] = [
        {"role": "system", "content": config.SYSTEM_PROMPTS["adviser"]},
        {"role": "user", "content": user_prompt}
    ]
    
    # --- DYNAMIC PAYLOAD CHECKER ---
    max_context = adviser_profile.get("context_window", adviser_profile.get("max_context_tokens", config.MAX_CONTEXT_TOKENS))
    safe_budget = int(max_context * 0.90)
    
    payload_tokens = get_payload_tokens(api_args["messages"])
    
    if payload_tokens > safe_budget:
        return (f"SYSTEM ERROR: The data you sent to the Adviser is too massive! "
                f"Payload is {payload_tokens} tokens, but the safety limit is {safe_budget}. "
                f"Please drastically shorten your 'current_plan' and 'encountered_problems' before consulting the Adviser.")
    
    if config.VERBOSITY_MODE != "silent":
        sys.stderr.write(f"\n\033[93m[System: Sending prompt to Adviser (~{payload_tokens} estimated tokens)...\033[0m\n")
        
    try:
        # 4. Await the LLM response using the dedicated ADVISER client
        response = await adviser_client.chat.completions.create(**api_args)
        adviser_thinking, advice_text = extract_thinking_and_content(response.choices[0].message)
        
        # Log token usage with fallback
        tokens_in = response.usage.prompt_tokens if (response.usage and response.usage.prompt_tokens) else get_payload_tokens(api_args["messages"])
        tokens_out = response.usage.completion_tokens if (response.usage and response.usage.completion_tokens) else (len(tokenizer.encode(advice_text)) if tokenizer else len(advice_text) // 4)
        thinking_tokens = getattr(response.usage.completion_tokens_details, 'reasoning_tokens', 0) if response.usage and hasattr(response.usage, 'completion_tokens_details') and response.usage.completion_tokens_details else 0
        if thinking_tokens == 0 and adviser_thinking:
            thinking_tokens = len(adviser_thinking) // 4
        config.log_token_usage(STATE_DIR, "adviser", tokens_in, tokens_out, thinking_tokens)
        
        # 5. Format the filename to start with the date (e.g., 20260503_204530_advice.md)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{timestamp}_advice.md"
        filepath = os.path.join(STATE_DIR, filename)
        
        # 6. Save the physical document to the state folder (with collapsible thinking for auditability)
        file_body = advice_text
        if adviser_thinking:
            file_body = f"<details>\n<summary>Adviser Reasoning & Strategy</summary>\n\n{adviser_thinking}\n\n</details>\n\n{advice_text}"
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(file_body)
            
        # Context guardrail: truncate preview for context window if advice exceeds 8,000 characters
        if len(advice_text) > 8000:
            preview = advice_text[:3000]
            display_feedback = (
                f"{preview}\n\n... [SYSTEM CONTEXT GUARDRAIL: The full Adviser report ({len(advice_text):,} chars) was saved to '{filepath}'. "
                f"A preview of the first 3,000 characters is shown above to protect your context window.\n"
                f"HOW TO GET AROUND THIS TRUNCATION: Inspect specific sections of the saved report using analyze_files(['{filepath}'], ...) "
                f"or bash tools: execute_bash('sed -n \\'100,200p\\' {filepath}') or 'grep'.]"
            )
        else:
            display_feedback = advice_text

        # 7. Return the text back to the Brain's context window with hidden sentinel tags
        result_msg = f"Adviser report successfully saved to disk as '{filename}'.\n\n--- ADVISER FEEDBACK ---\n{display_feedback}"
        if adviser_thinking:
            result_msg += f"\n<___ADVISER_THOUGHTS___>\n{adviser_thinking}\n</___ADVISER_THOUGHTS___>"
        result_msg += f"\n<___ADVISER_REPORT___>\n{advice_text}\n</___ADVISER_REPORT___>"
        return result_msg
        
    except Exception as e:
        trace_file = save_failure_trace("adviser", {
            "subagent": "adviser",
            "error": str(e),
            "traceback": traceback.format_exc(),
            "messages": api_args.get("messages", []) if 'api_args' in locals() else []
        })
        trace_note = f"\n[SYSTEM: Failure trace saved to '{trace_file}']" if trace_file else ""
        return f"Failed to consult the adviser. Error: {str(e)}{trace_note}"


@mcp.tool()
async def query_universal_llm(
    action: str,
    model: str = None,
    system_prompt: str = "You are a helpful AI assistant.",
    user_prompt: str = "",
    temperature: float = 0.7,
    top_p: float = 1.0,
    max_tokens: int = 32768,
    reasoning_effort: str = None,
    context_filepaths: list[str] = None  # ◄--- Added parameter
) -> str:
    """Queries an available LLM endpoint to run experiments or delegate sub-tasks.
    'action' must be either 'list_models' or 'chat'.
    If action is 'list_models', it returns the names of all available models. No other arguments are needed.
    If action is 'chat', you MUST provide 'model' and 'user_prompt'. 
    You may optionally tune 'system_prompt', 'temperature' (0.0 - 2.0), 'top_p', 'max_tokens', and 'reasoning_effort' ('low', 'medium', 'high' for supported reasoning models).
    Use this only if the Analyst or the Adviser fail to answer questions or run out of ideas.
    'context_filepaths' allows passing a list of absolute paths to files this sub-agent should read before executing instructions.
    """
    
    if action == "list_models":
        try:
            models_response = await universal_client.models.list()
            model_names = [m.id for m in models_response.data]
            return "--- AVAILABLE MODELS ---\n" + "\n".join(model_names)
        except Exception as e:
            return f"Failed to fetch model list. Error: {str(e)}"
            
    elif action == "chat":
        if not model or not user_prompt:
            return "Error: You must provide a 'model' name and a 'user_prompt' to use the chat action."
            
        # ◄--- Extract background context files quietly ---
        file_environmental_context = gather_agent_context(context_filepaths)
        final_user_payload = f"{file_environmental_context}{user_prompt}"

        api_args = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": final_user_payload}
            ],
            "temperature": temperature,
            "top_p": top_p,
            "max_tokens": max_tokens
        }
        
        safe_budget = int(config.MAX_CONTEXT_TOKENS * 0.90) 
        payload_tokens = get_payload_tokens(api_args["messages"])
        
        if payload_tokens > safe_budget:
            return (f"SYSTEM ERROR: The prompt and attached context files sent to the Universal Sub-Agent are too massive! "
                    f"Your payload is {payload_tokens} tokens, but the safety limit is {safe_budget}. "
                    f"Please refine your 'context_filepaths' selection.")
                    
        if reasoning_effort:
            api_args["extra_body"] = {"reasoning_effort": reasoning_effort}
            
        if config.VERBOSITY_MODE != "silent":
            sys.stderr.write(f"\n\033[93m[System: Sending prompt to Universal Sub-Agent ({model}) (~{payload_tokens} estimated tokens)...\033[0m\n")
            
        try:
            response = await universal_client.chat.completions.create(**api_args)
            thinking, content = extract_thinking_and_content(response.choices[0].message)
            
            # Log token usage with fallback
            tokens_in = response.usage.prompt_tokens if (response.usage and response.usage.prompt_tokens) else get_payload_tokens(api_args["messages"])
            tokens_out = response.usage.completion_tokens if (response.usage and response.usage.completion_tokens) else (len(tokenizer.encode(content)) if tokenizer else len(content) // 4)
            thinking_tokens = getattr(response.usage.completion_tokens_details, 'reasoning_tokens', 0) if response.usage and hasattr(response.usage, 'completion_tokens_details') and response.usage.completion_tokens_details else 0
            if thinking_tokens == 0 and thinking:
                thinking_tokens = len(thinking) // 4
            config.log_token_usage(STATE_DIR, "universal", tokens_in, tokens_out, thinking_tokens)
            
            finish_reason = response.choices[0].finish_reason or "unknown"
            
            result_str = f"--- RESPONSE FROM {model} ---\n"
            if thinking:
                result_str += f"<thinking>\n{thinking}\n</thinking>\n\n"
            result_str += content
            
            if not content.strip() and not thinking:
                result_str += f"\n[SYSTEM WARNING: Empty response. Finish Reason: '{finish_reason}']"
            
            if thinking:
                result_str += f"\n<___UNIVERSAL_THOUGHTS___>\n{thinking}\n</___UNIVERSAL_THOUGHTS___>"
            if content:
                result_str += f"\n<___UNIVERSAL_OUTPUT___ model=\"{model}\">\n{content}\n</___UNIVERSAL_OUTPUT___>"
            
            return result_str
            
        except Exception as e:
            trace_file = save_failure_trace("universal", {
                "subagent": "universal",
                "model": model,
                "error": str(e),
                "traceback": traceback.format_exc(),
                "messages": api_args.get("messages", []) if 'api_args' in locals() else []
            })
            trace_note = f"\n[SYSTEM: Failure trace saved to '{trace_file}']" if trace_file else ""
            return f"LLM Query Failed. Error: {str(e)}{trace_note}"
                        
    else:
        return "Error: Invalid action. Must be 'list_models' or 'chat'."

@mcp.tool()
async def execute_bash(command: str, timeout_seconds: int = 180) -> str:
    """Executes a bash command STRICTLY inside the sandbox directory. 
    'timeout_seconds' defaults to 180. Increase it up to 600 if you expect a long-running process like a massive download or large database search.
    Do NOT use destructive commands! Move files to the archive folder instead."""
    
    # 1. Catch empty inputs or incorrect types immediately
    if not command or not isinstance(command, str):
        return 'SYSTEM ERROR: The "command" parameter must be a non-empty string. Example: command="ls -la"'

    # 2. Block Destructive Commands (rm as an executed command, not an argument inside quotes/grep)
    if re.search(r'\bgit\s+rm\s+--cached\b', command.lower()):
        pass  # Explicitly permit non-destructive git index untracking
    elif re.search(r'(?:^|[;&|\n]|\bxargs\b)\s*rm\s+', command.strip().lower()):
        first_token = command.strip().split()[0].lower() if command.strip().split() else ""
        if first_token in ("grep", "egrep", "fgrep", "rg", "echo", "printf", "sed", "awk"):
            pass
        else:
            logger.warning(f"[execute_bash] Security block: destructive rm command rejected: {command[:200]}")
            return "SYSTEM ERROR: Destructive commands (rm) are blocked. Use the archive folder instead."

    try:
        if not command.strip():
            logger.warning("[execute_bash] Empty command payload rejected")
            return "SYSTEM ERROR: Empty command payload."

        current_env = os.environ.copy()
        current_env["PYTHONPATH"] = f"/app/workspace/custom_packages:{current_env.get('PYTHONPATH', '')}"

        # Utilizes an asynchronous shell context to permit multi-language execution pipelines (&&, |, >) safely within container borders
        process = await asyncio.create_subprocess_shell(
            command,
            cwd=SANDBOX_DIR,
            env=current_env,
            executable="/bin/bash",
            stdout=asyncio.subprocess.PIPE,     
            stderr=asyncio.subprocess.STDOUT,   
            stdin=asyncio.subprocess.DEVNULL,   
            start_new_session=True              
        )

        try:
            # Await the completion with an async timeout
            stdout_data, _ = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
            
        except (asyncio.TimeoutError, asyncio.CancelledError):
            # If it times out or is cancelled, aggressively kill the entire process group
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass
            try:
                await process.wait()
            except Exception:
                pass
                
            logger.error(f"[execute_bash] Command timed out after {timeout_seconds}s: {command[:200]}")
            return f"SYSTEM ERROR: Command timed out after {timeout_seconds} seconds and was forcefully terminated."

        # Decode the byte stream safely
        output = stdout_data.decode('utf-8', errors='replace') if stdout_data else ""

        if process.returncode != 0:
            logger.warning(f"[execute_bash] Non-zero exit ({process.returncode}) for: {command[:200]} | output: {output[:300]}")
            if "no such module: vec0" in output.lower() or "no such function: vec_" in output.lower():
                output += "\n[SYSTEM HINT: The sqlite3 CLI binary does not load the sqlite-vec extension. Use the native 'query_sqlite_db' tool instead, which pre-loads sqlite-vec.]"
        else:
            logger.info(f"[execute_bash] Success (exit 0): {command[:200]}")

        # --- THE POINTER APPROACH (Context Protection) ---
        if len(output) > 10000:
            # Generate a clean timestamped filename
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            temp_file_name = f"cmd_output_{timestamp}.txt"
            os.makedirs(SANDBOX_DIR, exist_ok=True)
            temp_file_path = os.path.join(SANDBOX_DIR, temp_file_name)
            
            # Save the full massive output safely to the sandbox
            with open(temp_file_path, "w", encoding="utf-8") as f:
                f.write(output)
                
            total_lines = output.count('\n') + 1
            head_preview = output[:1500]
            tail_preview = output[-1500:]
            omitted_chars = len(output) - len(head_preview) - len(tail_preview)
            head_lines = head_preview.count('\n') + 1
            tail_lines = tail_preview.count('\n') + 1
            
            return (f"Exit Code: {process.returncode}\n"
                    f"Output Preview (Head: first 1,500 chars / {head_lines} lines | Tail: last 1,500 chars / {tail_lines} lines of {len(output):,} chars total):\n"
                    f"--- [HEAD PREVIEW (First 1,500 chars)] ---\n{head_preview}\n\n"
                    f"... [SNIP: {omitted_chars:,} characters omitted — complete {len(output):,} chars ({total_lines:,} lines) saved to '/app/workspace/sandbox/{temp_file_name}'] ...\n\n"
                    f"--- [TAIL PREVIEW (Last 1,500 chars)] ---\n{tail_preview}\n\n"
                    f"[SYSTEM CONTEXT GUARDRAIL: Output exceeded 10,000 characters. Both head (headers/start) and tail (final results/errors) are shown above.\n"
                    f"HOW TO GET AROUND THIS TRUNCATION:\n"
                    f"- To inspect additional tail lines: execute_bash('tail -n 100 /app/workspace/sandbox/{temp_file_name}')\n"
                    f"- To inspect a specific line range: execute_bash('sed -n \\'100,200p\\' /app/workspace/sandbox/{temp_file_name}')\n"
                    f"- To search for specific keywords: execute_bash('grep -n -C 3 \"ERROR\\|Exception\" /app/workspace/sandbox/{temp_file_name}')\n"
                    f"- To delegate analysis of the entire file: analyze_files(['/app/workspace/sandbox/{temp_file_name}'], 'Find errors ...')]")

        return f"Exit Code: {process.returncode}\nOutput:\n{output}"
        
    except Exception as e:
        logger.error(f"[execute_bash] Exception on '{command[:200]}': {str(e)}")
        return f"Error executing command: {str(e)}"
        

@mcp.tool()
def write_file(filepath: str, content: str, mode: str = "w") -> str:
    """Writes text content to a file in 'outputs' or 'sandbox' directory.
    - mode: 'w' (default) to overwrite, or 'a' to append content to an existing file.
    When mode='w' and the file already exists, an archive backup is automatically created.
    Use this instead of bash 'echo' or 'cat' to write markdown, or text files safely.
    You can only write to 'outputs' or 'sandbox' directories.
    IMPORTANT: For any source code files (Python, JS, TS, Rust, C++), you MUST use forge_and_register_plugin!
    """
    
    # Enforces code containment by routing all source code modifications through the syntax checking pipeline
    forbidden_extensions = (".py", ".js", ".ts", ".rs", ".cpp", ".c", ".hpp", ".h", ".cc", ".cxx", ".mjs", ".cjs")
    if filepath.strip().lower().endswith(forbidden_extensions):
        logger.warning(f"[write_file] Blocked direct write to forbidden source extension: '{filepath}'")
        return f"SYSTEM ERROR: You are strictly FORBIDDEN from using write_file to create source code scripts directly. You MUST use 'forge_and_register_plugin' with the appropriate 'language' parameter so the Coder agent can generate and validate it safely."

    # 1. Resolve the absolute path
    resolved_path = os.path.realpath(filepath)
    
    # 2. Define safe zones
    allowed_dirs = [
        os.path.realpath("/app/workspace/outputs"),
        os.path.realpath("/app/workspace/sandbox")
    ]
    
    # 3. Check if the resolved path starts with any allowed directory
    is_safe = any(os.path.commonpath([resolved_path, safe_dir]) == safe_dir for safe_dir in allowed_dirs)
    
    if not is_safe:
        logger.warning(f"[write_file] Blocked path traversal attempt to '{filepath}' (resolved: '{resolved_path}')")
        return f"SYSTEM ERROR: Path Traversal Blocked. You are only allowed to write files to {allowed_dirs}."

    try:
        # Ensure parent directory exists for nested paths
        os.makedirs(os.path.dirname(resolved_path), exist_ok=True)

        # Check existing file status
        file_exists = os.path.exists(resolved_path)
        write_mode = "a" if mode.lower().strip() == "a" else "w"

        if file_exists and write_mode == "w":
            existing_size = os.path.getsize(resolved_path)
            # Size-shrink guard: prevent accidental clobbering if new content is drastically smaller (< 40%)
            if existing_size > 1000 and len(content) < int(existing_size * 0.4):
                return (f"SYSTEM WARNING: Overwrite blocked to protect data integrity. "
                        f"Existing file is {existing_size} bytes, but incoming content is only {len(content)} bytes ({int(len(content)/existing_size*100)}%). "
                        f"If you intended to append, call write_file with mode='a'. If you intentionally wish to replace the file with this shorter content, write via execute_bash redirection.")

            timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
            filename = os.path.basename(resolved_path)
            backup_dir = "/app/workspace/archive"
            os.makedirs(backup_dir, exist_ok=True)
            backup_path = os.path.join(backup_dir, f"{filename}.{timestamp}.bak")
            shutil.copy2(resolved_path, backup_path)
            backup_msg = f"(Old version backed up to archive/{os.path.basename(backup_path)})"
        elif file_exists and write_mode == "a":
            backup_msg = f"(Content appended to existing file)"
        else:
            backup_msg = "(New file created)"
            
        with open(resolved_path, write_mode, encoding="utf-8") as f:
            f.write(content)
            
        action_verb = "appended to" if write_mode == "a" else "saved to"
        return f"SUCCESS: File {action_verb} {resolved_path} {backup_msg}"
    except Exception as e:
        return f"Error writing file: {str(e)}"


# --- MEMORY TOOLS ---
@mcp.tool()
def view_memory_registry(category: str = None) -> str:
    """Views the long-term Memory Registry. Pass NO arguments to see memory categories. Pass a category to see specific memory titles, short descriptions, and timestamps."""
    memories = load_json(MEMORY_REGISTRY_FILE)
    if not memories: return "Memory Registry is empty."
    
    if not category:
        summary = {cat: data.get("category_description", "No description provided.") for cat, data in memories.items()}
        return json.dumps({"memory_categories": summary}, indent=2) + "\n\nCall this tool again with a category name to see available memories."
    
    if category in memories:
        cat_mems = memories[category].get("memories", {})
        summary = {}
        for title, data in cat_mems.items():
            summary[title] = {"description": data["description"], "timestamp": data["timestamp"]}
        return f"Memories in '{category}':\n{json.dumps(summary, indent=2)}\n\nUse read_memory with the exact title to read the full text."
    return f"Category not found. Available: {list(memories.keys())}"


@mcp.tool()
def read_memory(category: str, memory_title: str) -> str:
    """Reads the full markdown text of a specific memory from the registry."""
    memories = load_json(MEMORY_REGISTRY_FILE)
    try:
        mem_data = memories[category]["memories"][memory_title]
        filepath = os.path.join(WORKSPACE_DIR, mem_data["file"])
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
        return f"--- MEMORY: {memory_title} ({mem_data['timestamp']}) ---\n{content}"
    except KeyError:
        return "Error: Memory or Category not found. Use view_memory_registry to see available options."
    except Exception as e:
        return f"Error reading memory file: {str(e)}"


@mcp.tool()
def store_memory(category: str, category_description: str, title: str, short_description: str, detailed_markdown: str) -> str:
    """Proactively stores an important fact, completed objective, or context into the long-term Memory Registry."""
    memories = load_json(MEMORY_REGISTRY_FILE)
    
    if category not in memories:
        memories[category] = {"category_description": category_description, "memories": {}}
    else:
        memories[category]["category_description"] = category_description
        
    safe_title = re.sub(r'[^a-zA-Z0-9_]', '', title) or "memory"
    timestamp_prefix = datetime.now().strftime("%Y%m%d%H%M%S")
    filename = f"{timestamp_prefix}_{safe_title}.md"
    filepath = os.path.join(MEMORIES_DIR, filename)
    
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(detailed_markdown)
        
    memories[category]["memories"][title] = {
        "description": short_description,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "file": f"memories/{filename}"
    }
    
    save_json(MEMORY_REGISTRY_FILE, memories)
    return f"SUCCESS: Memory '{title}' saved to category '{category}'."



@mcp.tool()
async def compress_and_store_context() -> str:
    """Triggers the background Memory Manager to sequence a memory extraction followed by a targeted handoff history compression."""
    current_history = load_json(CURRENT_HISTORY_FILE)
    current_memories = load_json(MEMORY_REGISTRY_FILE)
    
    # 1. Extract the initial user objective from the first user turn
    initial_user_prompt = ""
    for msg in current_history:
        if msg.get("role") == "user":
            content = str(msg.get("content", ""))
            if not content.startswith("[SYSTEM CLOCK") and not content.startswith("[SYSTEM WARNING"):
                initial_user_prompt = content
                break
    if not initial_user_prompt:
        initial_user_prompt = "No initial user prompt identified."

    # 2. Read the Master Plan (active_plan.md)
    plan_path = os.path.join(STATE_DIR, "active_plan.md")
    active_plan_text = "No active plan found on disk."
    if os.path.exists(plan_path):
        try:
            with open(plan_path, "r", encoding="utf-8") as f:
                c = f.read().strip()
                if c:
                    active_plan_text = c
        except Exception:
            pass

    # 3. Inventory verified deliverables in /app/workspace/outputs/
    deliverables = []
    outputs_dir = os.path.join(WORKSPACE_DIR, "outputs")
    if os.path.exists(outputs_dir):
        try:
            for root, _, files in os.walk(outputs_dir):
                for file in files:
                    rel = os.path.relpath(os.path.join(root, file), WORKSPACE_DIR)
                    deliverables.append(f"/app/workspace/{rel}")
        except Exception:
            pass
    deliverables_summary = "\n".join(f"- {d}" for d in deliverables) if deliverables else "None identified yet in /app/workspace/outputs/."

    # 4. Extract recent execution window (last 20 messages)
    recent_turns = current_history[-20:] if len(current_history) > 20 else current_history
    recent_context_text = json.dumps(recent_turns, indent=2)

    # Accumulators for subagent telemetry
    summarizer_thoughts = []

    # ==========================================
    # STEP 1: EXTRACT MEMORIES (Strict Schema)
    # ==========================================
    memory_schema = {
        "type": "json_schema",
        "json_schema": {
            "name": "memory_extraction",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "extracted_memories": {
                        "type": "array",
                        "description": "A list of important facts, tool creations, or context to remember.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "category": {"type": "string", "description": "Broad category, e.g., 'Tool Concepts', 'User Preferences'"},
                                "category_description": {"type": "string", "description": "1-2 sentences explaining what types of memories belong in this category."},
                                "title": {"type": "string", "description": "Short, unique title"},
                                "short_description": {"type": "string", "description": "1-2 sentences summarizing the memory for the registry overview."},
                                "detailed_markdown": {"type": "string", "description": "The full, exhaustive details, code snippets, and explanations."}
                            },
                            "required": ["category", "category_description", "title", "short_description", "detailed_markdown"],
                            "additionalProperties": False
                        }
                    }
                },
                "required": ["extracted_memories"],
                "additionalProperties": False
            }
        }
    }

    mem_sys_prompt = (
        "You are a data extractor. Analyze the mission objective, active plan, verified deliverables, and recent turns. "
        "Extract NEW crucial long-term facts, completed objectives, database paths, or forged tools into the memory schema. "
        "Write highly detailed markdown files for the 'detailed_markdown' field. "
        "CRITICAL: Cross-reference the provided CURRENT MEMORY REGISTRY. Do NOT extract or duplicate facts already saved in the registry!"
    )
    
    mem_user_prompt = (
        f"CURRENT MEMORY REGISTRY (DO NOT DUPLICATE THESE):\n{json.dumps(current_memories, indent=2)}\n\n"
        f"ORIGINAL GOAL:\n{initial_user_prompt}\n\n"
        f"ACTIVE MASTER PLAN:\n{active_plan_text}\n\n"
        f"VERIFIED DELIVERABLES ON DISK:\n{deliverables_summary}\n\n"
        f"RECENT EXECUTION TURNS:\n{recent_context_text}"
    )

    mem_api_args = summarizer_profile["api_params"].copy()
    mem_api_args["model"] = summarizer_profile["model"]
    mem_api_args["messages"] = [
        {"role": "system", "content": mem_sys_prompt},
        {"role": "user", "content": mem_user_prompt}
    ]
    mem_api_args["response_format"] = memory_schema

    if config.VERBOSITY_MODE != "silent":
        payload_tokens = get_payload_tokens(mem_api_args["messages"])
        sys.stderr.write(f"\n\033[93m[System: Summarizer memory extraction payload is ~{payload_tokens} estimated tokens]\033[0m\n")

    added_titles = []
    try:
        mem_response = await summarizer_client.chat.completions.create(**mem_api_args)
        mem_thinking, mem_raw_content = extract_thinking_and_content(mem_response.choices[0].message)
        if mem_thinking:
            summarizer_thoughts.append(f"--- MEMORY EXTRACTION THINKING ---\n{mem_thinking}")

        # Log token usage with fallback
        tokens_in = mem_response.usage.prompt_tokens if (mem_response.usage and mem_response.usage.prompt_tokens) else get_payload_tokens(mem_api_args["messages"])
        tokens_out = mem_response.usage.completion_tokens if (mem_response.usage and mem_response.usage.completion_tokens) else (len(tokenizer.encode(mem_raw_content)) if tokenizer else len(mem_raw_content) // 4)
        thinking_tokens = getattr(mem_response.usage.completion_tokens_details, 'reasoning_tokens', 0) if mem_response.usage and hasattr(mem_response.usage, 'completion_tokens_details') and mem_response.usage.completion_tokens_details else 0
        if thinking_tokens == 0 and mem_thinking:
            thinking_tokens = len(mem_thinking) // 4
        config.log_token_usage(STATE_DIR, "summarizer", tokens_in, tokens_out, thinking_tokens)

        mem_data = json.loads(mem_raw_content)
        for memory in mem_data.get("extracted_memories", []):
            cat = memory["category"]
            cat_desc = memory["category_description"]
            title = memory["title"]
            short_desc = memory["short_description"]
            full_text = memory["detailed_markdown"]

            if cat not in current_memories:
                current_memories[cat] = {"category_description": cat_desc, "memories": {}}
            else:
                current_memories[cat]["category_description"] = cat_desc

            safe_title = re.sub(r'[^a-zA-Z0-9_]', '', title)
            timestamp_prefix = datetime.now().strftime("%Y%m%d%H%M%S")
            filename = f"{timestamp_prefix}_{safe_title}.md"
            filepath = os.path.join(MEMORIES_DIR, filename)

            with open(filepath, "w", encoding="utf-8") as f:
                f.write(full_text)

            current_memories[cat]["memories"][title] = {
                "description": short_desc,
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "file": f"memories/{filename}"
            }
            added_titles.append(f"{{'category': '{cat}', 'title': '{title}'}}")

        save_json(MEMORY_REGISTRY_FILE, current_memories)

    except Exception as e:
        trace_file = save_failure_trace("summarizer_memory", {
            "subagent": "summarizer",
            "phase": "memory_extraction",
            "error": str(e),
            "traceback": traceback.format_exc(),
            "messages": mem_api_args.get("messages", [])
        })
        trace_note = f"\n[SYSTEM: Failure trace saved to '{trace_file}']" if trace_file else ""
        return f"FAILED during Memory Extraction Phase. Error: {str(e)}{trace_note}"

    # ==========================================
    # STEP 2: TARGETED HANDOFF SYNTHESIS
    # ==========================================
    handoff_schema = {
        "type": "json_schema",
        "json_schema": {
            "name": "targeted_handoff",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "handoff_markdown": {
                        "type": "string",
                        "description": "The concise 3-section transition handoff report in markdown."
                    }
                },
                "required": ["handoff_markdown"],
                "additionalProperties": False
            }
        }
    }

    handoff_sys_prompt = (
        "You are an operational handoff synthesizer. The agent's working context is being compacted. "
        "Synthesize a concise, high-signal Transition Handoff Report for the incoming Brain turn.\n\n"
        "CRITICAL REQUIREMENTS:\n"
        "1. Anchor strictly on the Original User Goal, the Master Plan, verified workspace deliverables, and recent actions.\n"
        "2. Format your response into exactly three structured sections:\n"
        "   ### 1. Accomplished Deliverables & Workspace State\n"
        "   - List exact verified filepaths created on disk, tools forged, and biological/scientific findings established.\n"
        "   ### 2. Current Plan Status\n"
        "   - Specific checklist items completed vs. still pending in active_plan.md.\n"
        "   ### 3. Immediate Next Steps\n"
        "   - The precise next action or tool call the Brain should execute to continue progress toward the original goal.\n"
        "3. Be technical, precise, and completely free of conversational filler."
    )

    handoff_user_prompt = (
        f"=== ORIGINAL USER GOAL ===\n{initial_user_prompt}\n\n"
        f"=== ACTIVE MASTER PLAN (active_plan.md) ===\n{active_plan_text}\n\n"
        f"=== VERIFIED WORKSPACE DELIVERABLES ===\n{deliverables_summary}\n\n"
        f"=== RECENT EXECUTION TURNS ===\n{recent_context_text}"
    )

    handoff_api_args = summarizer_profile["api_params"].copy()
    handoff_api_args["model"] = summarizer_profile["model"]
    handoff_api_args["messages"] = [
        {"role": "system", "content": handoff_sys_prompt},
        {"role": "user", "content": handoff_user_prompt}
    ]
    handoff_api_args["response_format"] = handoff_schema

    if config.VERBOSITY_MODE != "silent":
        payload_tokens = get_payload_tokens(handoff_api_args["messages"])
        sys.stderr.write(f"\n\033[93m[System: Summarizer targeted handoff payload is ~{payload_tokens} estimated tokens]\033[0m\n")

    try:
        handoff_resp = await summarizer_client.chat.completions.create(**handoff_api_args)
        handoff_thinking, handoff_raw_content = extract_thinking_and_content(handoff_resp.choices[0].message)
        if handoff_thinking:
            summarizer_thoughts.append(f"--- TARGETED HANDOFF THINKING ---\n{handoff_thinking}")

        # Log token usage with fallback
        tokens_in = handoff_resp.usage.prompt_tokens if (handoff_resp.usage and handoff_resp.usage.prompt_tokens) else get_payload_tokens(handoff_api_args["messages"])
        tokens_out = handoff_resp.usage.completion_tokens if (handoff_resp.usage and handoff_resp.usage.completion_tokens) else (len(tokenizer.encode(handoff_raw_content)) if tokenizer else len(handoff_raw_content) // 4)
        thinking_tokens = getattr(handoff_resp.usage.completion_tokens_details, 'reasoning_tokens', 0) if handoff_resp.usage and hasattr(handoff_resp.usage, 'completion_tokens_details') and handoff_resp.usage.completion_tokens_details else 0
        if thinking_tokens == 0 and handoff_thinking:
            thinking_tokens = len(handoff_thinking) // 4
        config.log_token_usage(STATE_DIR, "summarizer", tokens_in, tokens_out, thinking_tokens)

        handoff_data = json.loads(handoff_raw_content)
        handoff_markdown = handoff_data.get("handoff_markdown", "")

        # Backup the old bloated history before overwriting
        backup_file = os.path.join(HISTORIES_DIR, f"backup_history_{datetime.now().strftime('%Y%m%d%H%M%S')}.json")
        save_json(backup_file, current_history)

        handoff_directive = config.PROMPTS.get(
            "handoff_wake_up_directive",
            "[SYSTEM TRANSITION HANDOFF: Context has been compacted and archived to disk. "
            "Memory compression is COMPLETE (do NOT call compress_and_store_context again). "
            "Revisit your plan: "
            "- If any tasks still need to be done, proceed with them. "
            "- If everything is already finished, output your final summary to conclude.]"
        )

        handoff_full_text = (
            f"=== SESSION TRANSITION HANDOFF ===\n\n"
            f"**Original Goal:**\n{initial_user_prompt}\n\n"
            f"{handoff_markdown}\n\n"
            f"===================================\n\n"
            f"{handoff_directive}"
        )

        # Overwrite the active working memory with pristine context
        new_history = [
            {"role": "system", "content": config.SYSTEM_PROMPTS["brain"]},
            {"role": "user", "content": handoff_full_text}
        ]
        save_json(CURRENT_HISTORY_FILE, new_history)
        _LOADED_SKILLS.clear()

        summarizer_output = f"Extracted Memories: {added_titles}\nCompressed History: 2 messages (Pristine Handoff)"
        result_msg = (
            f"SUCCESS: Context compressed and old history moved to {os.path.basename(backup_file)}.\n"
            f"New detailed memories extracted to disk: {added_titles}.\n"
            f"{handoff_directive}"
        )

        if summarizer_thoughts:
            result_msg += f"\n<___SUMMARIZER_THOUGHTS___>\n" + "\n\n".join(summarizer_thoughts) + "\n</___SUMMARIZER_THOUGHTS___>"
        result_msg += f"\n<___SUMMARIZER_OUTPUT___>\n{summarizer_output}\n</___SUMMARIZER_OUTPUT___>"

        return result_msg

    except Exception as e:
        trace_file = save_failure_trace("summarizer_handoff", {
            "subagent": "summarizer",
            "phase": "targeted_handoff_synthesis",
            "error": str(e),
            "traceback": traceback.format_exc(),
            "messages": handoff_api_args.get("messages", []) if 'handoff_api_args' in locals() else []
        })
        trace_note = f"\n[SYSTEM: Failure trace saved to '{trace_file}']" if trace_file else ""
        return f"FAILED during Targeted Handoff Synthesis Phase. Error: {str(e)}{trace_note}"


@mcp.tool()
async def forge_and_register_plugin(
    category: str, 
    category_description: str, 
    plugin_name: str, 
    plugin_description: str, 
    objective: str, 
    language: str = "python",
    context_filepaths: list[str] = None  # ◄--- Universal Context Entrypoint
) -> str:
    """Delegates code writing to the Coder LLM, validates compilation, and registers the plugin.
    CRITICAL: Set 'language' explicitly to match your objective!
    - 'language': Target programming language: 'python' (default), 'rust', 'cpp', 'javascript', or 'typescript'.
      If your objective asks for Rust (cargo/rustc), you MUST pass language='rust'.
      If your objective asks for C++, you MUST pass language='cpp'.
      If your objective asks for TypeScript, you MUST pass language='typescript'.
    - 'plugin_name': Base filename for the tool (without file extension, e.g. 'kmer_counter').
    - 'category': Grouping tag (e.g. 'bioinformatics', 'database', 'parsing').
    - 'category_description': Brief description of the category.
    - 'plugin_description': Summary of what this tool does.
    - 'objective': Detailed functional requirements, CLI arguments, I/O formats, and logic for the Coder.
    - 'context_filepaths': Optional list of absolute file paths for the Coder to read to adapt to existing schemas/code.
    """
    lang_map = {
        "python": {"ext": ".py", "block": "python"},
        "javascript": {"ext": ".js", "block": "javascript"},
        "typescript": {"ext": ".ts", "block": "typescript"},
        "rust": {"ext": ".rs", "block": "rust"},
        "cpp": {"ext": ".cpp", "block": "cpp"}
    }
    target_lang = language.lower() if language.lower() in lang_map else "python"
    ext = lang_map[target_lang]["ext"]
    md_block = lang_map[target_lang]["block"]

    safe_name = os.path.basename(plugin_name)
    for k_ext in [".py", ".js", ".ts", ".rs", ".cpp"]:
        if safe_name.endswith(k_ext): safe_name = safe_name[:-len(k_ext)]
    safe_name = re.sub(r'[^a-zA-Z0-9_-]', '', safe_name)
    if not safe_name: safe_name = "default_plugin_name"

    filename = f"{safe_name}{ext}"
    file_path = os.path.join(PLUGINS_DIR, filename)
    
    coder_sys = config.SYSTEM_PROMPTS["coder"]
    if target_lang != "python":
        coder_sys = re.sub(r"write robust, standalone Python scripts", f"write robust standalone {language} assets", coder_sys)
        coder_sys = re.sub(r"Output ONLY valid, executable Python code", f"Output ONLY valid executable {language} source code", coder_sys)
        coder_sys = re.sub(r"```python", f"```{md_block}", coder_sys)

    # ◄--- Gather background context files seamlessly ---
    file_environmental_context = gather_agent_context(context_filepaths)

    messages = [
        {"role": "system", "content": coder_sys},
        {"role": "user", "content": f"Language Selection: {target_lang}\n{file_environmental_context}\n\n" + config.PROMPTS["coder_user"].format(objective=objective)}
    ]
    
    last_validation_error = "Unknown error (validation failed without output)"
    for attempt in range(config.MAX_PLUGIN_RETRIES):
        try:
            api_args = coder_profile["api_params"].copy()
            api_args["model"] = coder_profile["model"]
            api_args["messages"] = messages
            if "seed" in api_args:
                api_args["seed"] = (api_args.get("seed") or 1000) + attempt
            
            if config.VERBOSITY_MODE != "silent":
                sys.stderr.write(f"\n\033[93m[System: Coder ({target_lang}) payload is ~{get_payload_tokens(messages)} estimated tokens]\033[0m\n")
            
            response = await coder_client.chat.completions.create(**api_args)
            coder_thinking, raw_content = extract_thinking_and_content(response.choices[0].message)
            
            match = re.search(rf"```{md_block}[ \t]*\r?\n(.*?)\r?\n```", raw_content, re.DOTALL)
            code = match.group(1).strip() if match else raw_content.replace(f"```{md_block}", "").replace("```", "").strip()
    
            # Log token usage with fallback
            tokens_in = response.usage.prompt_tokens if (response.usage and response.usage.prompt_tokens) else get_payload_tokens(api_args["messages"])
            tokens_out = response.usage.completion_tokens if (response.usage and response.usage.completion_tokens) else (len(tokenizer.encode(raw_content)) if tokenizer else len(raw_content) // 4)
            thinking_tokens = getattr(response.usage.completion_tokens_details, 'reasoning_tokens', 0) if response.usage and hasattr(response.usage, 'completion_tokens_details') and response.usage.completion_tokens_details else 0
            if thinking_tokens == 0 and coder_thinking:
                thinking_tokens = len(coder_thinking) // 4
            config.log_token_usage(STATE_DIR, "coder", tokens_in, tokens_out, thinking_tokens)
            
            if os.path.exists(file_path):
                shutil.copy2(file_path, os.path.join(WORKSPACE_DIR, "archive", f"{safe_name}_{datetime.now().strftime('%Y%m%d%H%M%S')}.bak{ext}"))
            
            with open(file_path, "w", encoding="utf-8") as f: 
                f.write(code)

            deps_report = ""
            if target_lang == "python":
                requires_match = re.search(r"# REQUIRES:\s*(.*)", code, re.IGNORECASE)
                if requires_match:
                    raw_reqs = requires_match.group(1).replace("pip install", "").replace("pixi add", "").replace(",", " ").strip()
                    cleaned_reqs = re.sub(r'\(.*?\)', '', raw_reqs).strip()
                    words = [w.strip() for w in cleaned_reqs.split() if w.strip()]
                    ignored_words = {"none", "null", "nil", "standard", "stdlib", "library", "only", "built-in", "builtin", "no", "external", "dependencies", "deps", "-"}
                    safe_deps_list = [w for w in words if re.match(r'^[A-Za-z0-9_]+[A-Za-z0-9_.-]*(?:\[.*\])?(?:[<>=!~].*)?$', w) and w.lower() not in ignored_words]
                    if safe_deps_list:
                        os.makedirs("/app/workspace/custom_packages", exist_ok=True)
                        proc_install = await asyncio.create_subprocess_exec(
                            sys.executable, "-m", "pip", "install", "--target", "/app/workspace/custom_packages", *safe_deps_list,
                            cwd=WORKSPACE_DIR, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                        )
                        _, stderr_install = await proc_install.communicate()
                        deps_report = f"\n[SYSTEM: Installed custom dependencies]" if proc_install.returncode == 0 else f"\n[SYSTEM WARNING: Dependency fault: {stderr_install.decode('utf-8', errors='replace')}]"

            # Dynamic platform syntax compile evaluations across all supported execution architectures
            is_valid = True
            err_msg = ""
            if target_lang == "python":
                check = await asyncio.to_thread(subprocess.run, [sys.executable, "-m", "py_compile", filename], cwd=PLUGINS_DIR, capture_output=True, text=True)
                if check.returncode != 0: is_valid = False; err_msg = check.stderr
            elif target_lang == "javascript":
                check = await asyncio.to_thread(subprocess.run, ["node", "--check", filename], cwd=PLUGINS_DIR, capture_output=True, text=True)
                if check.returncode != 0: is_valid = False; err_msg = check.stderr
            elif target_lang == "typescript":
                # Leverages Node's native type stripping to safely perform syntax parsing on TypeScript files
                check = await asyncio.to_thread(subprocess.run, ["node", "--experimental-strip-types", "--check", filename], cwd=PLUGINS_DIR, capture_output=True, text=True)
                if check.returncode != 0: is_valid = False; err_msg = check.stderr
            elif target_lang == "rust":
                # Flawless validation: Provisions an isolated cargo scratchpad project to resolve external crates automatically
                scratch_dir = "/app/workspace/sandbox/.check_rust"
                os.makedirs(os.path.join(scratch_dir, "src"), exist_ok=True)
                
                # Parse optional crate dependencies from comments (e.g., // REQUIRES: serde_json = "1.0")
                extra_deps = []
                for line in code.splitlines()[:15]:
                    stripped = line.strip()
                    if stripped.startswith(("// REQUIRES:", "// CRATES:", "// DEPENDENCIES:")):
                        raw_crates = stripped.split(":", 1)[1].strip()
                        for c in raw_crates.split(","):
                            c = c.strip()
                            if c:
                                extra_deps.append(c if "=" in c else f'{c} = "*"')
                                
                deps_lines = extra_deps
                deps_block = "\n".join(deps_lines) + ("\n" if deps_lines else "")
                
                cargo_toml_content = f"""[package]
name = "check_rust"
version = "0.1.0"
edition = "2021"

[dependencies]
{deps_block}"""
                with open(os.path.join(scratch_dir, "Cargo.toml"), "w", encoding="utf-8") as f:
                    f.write(cargo_toml_content)
                    
                # Copies the forged target source file into the scratchpad context entrypoint
                shutil.copy2(file_path, os.path.join(scratch_dir, "src", "main.rs"))
                
                # Executes cargo check - verifies language tokens and syncs library assets simultaneously
                check = await asyncio.to_thread(subprocess.run, ["cargo", "check", "-q"], cwd=scratch_dir, capture_output=True, text=True)
                if check.returncode != 0: 
                    is_valid = False
                    err_msg = check.stderr

            elif target_lang == "cpp":
                # GNU compiler syntax check mode - parses headers and code templates without linking output files
                check = await asyncio.to_thread(subprocess.run, ["g++", "-fsyntax-only", "-std=c++17", filename], cwd=PLUGINS_DIR, capture_output=True, text=True)
                if check.returncode != 0: is_valid = False; err_msg = check.stderr

            # --- Behavioral Pre-Registration Smoke Test ---
            if is_valid and target_lang in ["python", "javascript", "typescript"]:
                smoke_env = os.environ.copy()
                smoke_env["PYTHONPATH"] = f"/app/workspace/custom_packages:{PLUGINS_DIR}:{smoke_env.get('PYTHONPATH', '')}"
                smoke_cmd = None
                if target_lang == "python":
                    smoke_cmd = [sys.executable, filename, "--help"]
                elif target_lang == "javascript":
                    smoke_cmd = ["node", filename, "--help"]
                elif target_lang == "typescript":
                    smoke_cmd = ["tsx", filename, "--help"]

                if smoke_cmd:
                    try:
                        smoke_res = await asyncio.to_thread(
                            subprocess.run, smoke_cmd, cwd=PLUGINS_DIR, env=smoke_env,
                            capture_output=True, text=True, timeout=4
                        )
                        # Check if running generated an unhandled traceback or runtime crash
                        combined_err = (smoke_res.stderr + smoke_res.stdout).lower()
                        if smoke_res.returncode != 0 and ("traceback (most recent call last)" in combined_err or "referenceerror:" in combined_err or "typeerror:" in combined_err or "syntaxerror:" in combined_err):
                            is_valid = False
                            err_msg = f"Behavioral smoke test crashed during startup:\n{smoke_res.stderr or smoke_res.stdout}"
                            logger.warning(f"[forge_and_register_plugin] Smoke test failed for '{plugin_name}': {err_msg[:200]}")
                    except subprocess.TimeoutExpired:
                        pass
                    except Exception:
                        pass

            if is_valid:
                registry = load_json(TOOL_REGISTRY_FILE)
                if category not in registry: registry[category] = {"category_description": category_description, "tools": {}}
                registry[category]["tools"][plugin_name] = {"path": file_path, "description": plugin_description, "language": target_lang, "usage_objective": objective}
                save_json(TOOL_REGISTRY_FILE, registry)
                
                if target_lang == "python":
                    run_hint = f"execute_bash('python {file_path}')"
                elif target_lang == "javascript":
                    run_hint = f"execute_bash('node {file_path}')"
                elif target_lang == "typescript":
                    run_hint = f"execute_bash('tsx {file_path}')"
                elif target_lang == "cpp":
                    run_hint = f"execute_bash('g++ -std=c++17 {file_path} -o {file_path}_bin && {file_path}_bin')"
                elif target_lang == "rust":
                    cargo_deps_escaped = deps_block.replace('"', '\\"').replace('\n', '\\n')
                    run_hint = (
                        f"execute_bash('mkdir -p /app/workspace/sandbox/{safe_name}_project/src && "
                        f"printf \"[package]\\nname = \\\"{safe_name}\\\"\\nversion = \\\"0.1.0\\\"\\nedition = \\\"2021\\\"\\n\\n[dependencies]\\n{cargo_deps_escaped}\" > /app/workspace/sandbox/{safe_name}_project/Cargo.toml && "
                        f"cp {file_path} /app/workspace/sandbox/{safe_name}_project/src/main.rs && "
                        f"cd /app/workspace/sandbox/{safe_name}_project && cargo run')"
                    )

                code_lines = len(code.splitlines())
                report = f"SUCCESS (Attempt {attempt+1}): {language.upper()} Asset '{plugin_name}' saved to registry.\n"
                report += f"[File: {file_path} | Lines: {code_lines} | Size: {len(code):,} bytes]\n"
                report += f"[Tokens: {tokens_in} in | {tokens_out} out]\n{deps_report}Execution Blueprint: {run_hint}"
                if coder_thinking: report += f"\n<___CODER_THOUGHTS___>\n{coder_thinking}\n</___CODER_THOUGHTS___>"
                return report
            else:
                last_validation_error = err_msg.strip() if err_msg else "Syntax validation failed"
                logger.warning(f"[forge_and_register_plugin] Attempt {attempt+1} validation failed for '{plugin_name}': {last_validation_error[:200]}")
                if os.path.exists(file_path): os.remove(file_path)
                save_failure_trace("coder_forge", {
                    "subagent": "coder",
                    "action": "forge_and_register_plugin",
                    "plugin_name": plugin_name,
                    "target_language": target_lang,
                    "attempt": attempt + 1,
                    "validation_error": last_validation_error,
                    "generated_code": code,
                    "coder_thinking": coder_thinking,
                    "prompt_messages": messages
                })
                messages.append({"role": "assistant", "content": code})
                messages.append({"role": "user", "content": f"Code validation failed. Error:\n{err_msg}\nPlease patch the syntax rules and return the raw block."})
                
        except Exception as e:
            logger.error(f"[forge_and_register_plugin] Exception on attempt {attempt+1} for '{plugin_name}': {str(e)}")
            trace_file = save_failure_trace("coder_forge_exception", {
                "subagent": "coder",
                "action": "forge_and_register_plugin",
                "plugin_name": plugin_name,
                "attempt": attempt + 1,
                "error": str(e),
                "traceback": traceback.format_exc(),
                "messages": messages
            })
            trace_note = f"\n[SYSTEM: Failure trace saved to '{trace_file}']" if trace_file else ""
            return f"Fatal Forging Exception on attempt {attempt+1}: {str(e)}{trace_note}"
            
    logger.error(f"[forge_and_register_plugin] Forging failed after {config.MAX_PLUGIN_RETRIES} attempts for '{plugin_name}': {last_validation_error[:200]}")
    trace_file = save_failure_trace("coder_forge_final", {
        "subagent": "coder",
        "action": "forge_and_register_plugin",
        "plugin_name": plugin_name,
        "target_language": target_lang,
        "attempts": config.MAX_PLUGIN_RETRIES,
        "last_validation_error": last_validation_error
    })
    fail_msg = f"FAILED: Coder could not validate artifact constraints after {config.MAX_PLUGIN_RETRIES} runs.\nLast Validation Error:\n{last_validation_error}"
    if trace_file:
        fail_msg += f"\n[SYSTEM: Failure trace saved to '{trace_file}']"
    if 'coder_thinking' in locals() and coder_thinking:
        fail_msg += f"\n<___CODER_THOUGHTS___>\n{coder_thinking}\n</___CODER_THOUGHTS___>"
    if 'code' in locals() and code:
        fail_msg += f"\n<___CODER_CODE___>\n{code}\n</___CODER_CODE___>"
    return fail_msg


@mcp.tool()
async def surgical_code_edit(filepath: str, edit_objective: str) -> str:
    """Delegates a targeted code modification task directly to the Coder agent.
    The Coder will natively read the existing file from disk, isolate the target lines, 
    and output ONLY the precise text replacement blocks required to satisfy the objective.
    Use this for modifying massive scripts to save context space.
    
    Parameters:
    - filepath: Absolute path to the file to modify.
    - edit_objective: Clear instruction on what needs to be changed, added, or fixed.
    """
    real_target = os.path.realpath(filepath)
    if not os.path.exists(real_target):
        return f"SYSTEM ERROR: Target file '{filepath}' does not exist."
    if (os.path.commonpath([real_target, "/app/workspace"]) != "/app/workspace" or
        os.path.commonpath([real_target, "/app/workspace/state"]) == "/app/workspace/state" or
        os.path.commonpath([real_target, "/app/workspace/.git"]) == "/app/workspace/.git"):
        return f"SYSTEM ERROR: Target file '{filepath}' is protected or falls outside workspace boundaries."

    # 1. Natively read the file content from disk so the Coder can see it
    with open(real_target, "r", encoding="utf-8") as f:
        current_code = f.read()

    # 2. Spawn a specialized system prompt forcing the Coder to emit structured search/replace format
    coder_sys = (
        "You are an expert full-stack developer operating as a surgical script-patching agent.\n"
        "Your objective is to modify an existing script without changing unneeded blocks.\n"
        "Analyze the provided source code, identify the precise snippet(s) needing correction, "
        "and return a clean JSON object structure containing an 'edits' array of search/replace objects.\n"
        "Each item in 'edits' must have 'search_string' and 'replace_string'.\n"
        "CRITICAL RULES:\n"
        "1. UNIQUE & EXACT MATCH: Each 'search_string' MUST exist inside the source code word-for-word, down to the exact spacing and newlines. Include enough surrounding lines so each 'search_string' matches uniquely.\n"
        "2. TARGET INTEGRITY: If the edit objective instructs you to modify, replace, or delete specific lines, functions, or symbols that DO NOT exist in the provided source code, you MUST NOT hallucinate, invent, or append them. Do NOT substitute an unrelated search block. Set 'status' to 'target_not_found', explain what was missing in 'explanation', and return an empty 'edits' array (`\"edits\": []`).\n"
        "3. MINIMAL SCOPE: Change ONLY the lines necessary to satisfy the objective. Do not alter surrounding formatting or unrelated logic.\n"
        "4. ORDER OF EDITS: If multiple separate locations need changes (e.g. imports at top, flags in main), provide multiple discrete edit items in 'edits' in the order they appear in the file. Do NOT lump unrelated sections into one giant search block.\n"
        "Output ONLY a valid, single JSON block wrapped inside a standard markdown json code block token. No conversational filler text."
    )

    coder_user = (
        f"--- TARGET FILE LOCATION ---\n{filepath}\n\n"
        f"--- EDIT OBJECTIVE ---\n{edit_objective}\n\n"
        f"--- CURRENT ON-DISK SOURCE CODE ---\n{current_code}\n\n"
        "Perform your analysis and return the json code block immediately. "
        "If any target code specified to be modified in the objective does not exist in the source code above, "
        "set status to 'target_not_found' and edits to []."
    )

    messages = [
        {"role": "system", "content": coder_sys},
        {"role": "user", "content": coder_user}
    ]

    try:
        api_args = coder_profile["api_params"].copy()
        api_args["model"] = coder_profile["model"]
        api_args["messages"] = messages
        api_args["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": "surgical_edit_schema",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {
                        "status": {
                            "type": "string",
                            "enum": ["success", "target_not_found", "error"],
                            "description": "'success' if all targets exist and can be safely patched, or 'target_not_found'/'error' if target code does not exist in source."
                        },
                        "explanation": {
                            "type": "string",
                            "description": "Clear explanation of changes made or why the target code was not found."
                        },
                        "edits": {
                            "type": "array",
                            "description": "Sequential search/replace blocks applied in file order. Empty if target_not_found.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "search_string": {"type": "string", "description": "The exact block of code to search for."},
                                    "replace_string": {"type": "string", "description": "The new block of code to swap in."}
                                },
                                "required": ["search_string", "replace_string"],
                                "additionalProperties": False
                            }
                        }
                    },
                    "required": ["status", "explanation", "edits"],
                    "additionalProperties": False
                }
            }
        }

        response = await coder_client.chat.completions.create(**api_args)
        coder_thinking, raw_json_str = extract_thinking_and_content(response.choices[0].message)
        
        # Log Coder token usage with fallback
        tokens_in = response.usage.prompt_tokens if (response.usage and response.usage.prompt_tokens) else get_payload_tokens(api_args["messages"])
        tokens_out = response.usage.completion_tokens if (response.usage and response.usage.completion_tokens) else (len(tokenizer.encode(raw_json_str)) if tokenizer else len(raw_json_str) // 4)
        thinking_tokens = getattr(response.usage.completion_tokens_details, 'reasoning_tokens', 0) if response.usage and hasattr(response.usage, 'completion_tokens_details') and response.usage.completion_tokens_details else 0
        if thinking_tokens == 0 and coder_thinking:
            thinking_tokens = len(coder_thinking) // 4
        config.log_token_usage(STATE_DIR, "coder", tokens_in, tokens_out, thinking_tokens)

        if "```" in raw_json_str:
            match = re.search(r"```(?:json)?\s*(.*?)\s*```", raw_json_str, re.DOTALL)
            if match:
                raw_json_str = match.group(1).strip()

        # Parse structural change instructions safely
        try:
            edit_data = json.loads(raw_json_str)
        except json.JSONDecodeError as e:
            trace_file = save_failure_trace("coder_surgical_json", {
                "subagent": "coder",
                "action": "surgical_code_edit",
                "filepath": filepath,
                "objective": edit_objective,
                "error": f"JSON decode error: {str(e)}",
                "raw_response": raw_json_str,
                "coder_thinking": coder_thinking
            })
            fail_msg = f"SYSTEM ERROR: Coder returned invalid JSON for surgical edit: {str(e)}"
            if trace_file: fail_msg += f"\n[SYSTEM: Failure trace saved to '{trace_file}']"
            if coder_thinking: fail_msg += f"\n<___CODER_THOUGHTS___>\n{coder_thinking}\n</___CODER_THOUGHTS___>"
            fail_msg += f"\n<___CODER_CODE___>\n{raw_json_str}\n</___CODER_CODE___>"
            return fail_msg

        status = str(edit_data.get("status", "success")).lower()
        explanation = edit_data.get("explanation", "")
        edits_list = edit_data.get("edits", [])
        if not edits_list and "search_string" in edit_data:
            edits_list = [{"search_string": edit_data["search_string"], "replace_string": edit_data.get("replace_string", "")}]

        if status in ("target_not_found", "error") or not edits_list:
            reason = explanation or ("Target code specified in objective not found in file." if status == "target_not_found" else "The Coder returned an empty edits list. No modifications applied.")
            trace_file = save_failure_trace("coder_surgical_target_missing", {
                "subagent": "coder",
                "action": "surgical_code_edit",
                "filepath": filepath,
                "objective": edit_objective,
                "status": status,
                "explanation": explanation,
                "raw_response": raw_json_str,
                "coder_thinking": coder_thinking
            })
            fail_msg = f"SYSTEM ERROR: The Coder could not apply surgical edit ({status}): {reason}"
            if trace_file: fail_msg += f"\n[SYSTEM: Failure trace saved to '{trace_file}']"
            if coder_thinking: fail_msg += f"\n<___CODER_THOUGHTS___>\n{coder_thinking}\n</___CODER_THOUGHTS___>"
            fail_msg += f"\n<___CODER_CODE___>\n{raw_json_str}\n</___CODER_CODE___>"
            return fail_msg

        # Validate that all search blocks exist before applying any modification
        working_code = current_code
        applied_blocks = []
        for idx, edit_item in enumerate(edits_list, 1):
            s_block = edit_item.get("search_string", "")
            r_block = edit_item.get("replace_string", "")
            if not s_block:
                logger.warning(f"[surgical_code_edit] Edit #{idx} in '{filepath}' provided empty search_string")
                return f"SYSTEM ERROR: Edit #{idx} provided an empty search_string. Aborting modifications for safety."
            if s_block not in working_code:
                logger.warning(f"[surgical_code_edit] Search string mismatch in '{filepath}' on edit #{idx}")
                # Provide diagnostic near-match context so the Coder can self-correct indentation/spacing immediately
                file_lines = working_code.splitlines()
                search_lines = s_block.splitlines()
                first_search_line = search_lines[0].strip() if search_lines else ""
                near_lines = []
                for l_idx, line in enumerate(file_lines):
                    if first_search_line and first_search_line in line:
                        start_ctx = max(0, l_idx - 1)
                        end_ctx = min(len(file_lines), l_idx + len(search_lines) + 2)
                        near_lines = file_lines[start_ctx:end_ctx]
                        break
                diff_hint = ""
                if near_lines:
                    diff_hint = f"\nClosest matching lines in file:\n```\n" + "\n".join(near_lines) + "\n```\n"
                trace_file = save_failure_trace("coder_surgical_mismatch", {
                    "subagent": "coder",
                    "action": "surgical_code_edit",
                    "filepath": filepath,
                    "objective": edit_objective,
                    "edit_index": idx,
                    "search_string": s_block,
                    "replace_string": r_block,
                    "coder_thinking": coder_thinking,
                    "closest_lines": near_lines
                })
                fail_msg = (
                    f"SYSTEM ERROR: The Coder generated a 'search_string' in edit #{idx} that does not match the actual file lines exactly. "
                    f"Aborting all modifications for safety.{diff_hint}"
                    f"Ensure exact match of leading whitespace, indentation, and newlines."
                )
                if trace_file: fail_msg += f"\n[SYSTEM: Failure trace saved to '{trace_file}']"
                if coder_thinking: fail_msg += f"\n<___CODER_THOUGHTS___>\n{coder_thinking}\n</___CODER_THOUGHTS___>"
                fail_msg += f"\n<___CODER_CODE___>\n{raw_json_str}\n</___CODER_CODE___>"
                return fail_msg
            working_code = working_code.replace(s_block, r_block, 1)
            applied_blocks.append(f"--- EDIT #{idx} ---\nSearch Block:\n{s_block}\n\nReplace Block:\n{r_block}")

        updated_code = working_code

        # Pre-flight syntax validation before modifying disk
        ext = os.path.splitext(filepath)[1].lower()
        if ext == ".py":
            try:
                compile(updated_code, filepath, 'exec')
            except SyntaxError as e:
                bad_line = f" ({e.text.strip()})" if e.text else ""
                logger.error(f"[surgical_code_edit] Python syntax error in '{filepath}' on line {e.lineno}: {e.msg}{bad_line}")
                trace_file = save_failure_trace("coder_surgical_syntax", {
                    "subagent": "coder",
                    "action": "surgical_code_edit",
                    "filepath": filepath,
                    "objective": edit_objective,
                    "error": f"SyntaxError line {e.lineno}: {e.msg}{bad_line}",
                    "coder_thinking": coder_thinking,
                    "raw_json_str": raw_json_str
                })
                fail_msg = f"SYSTEM ERROR: Surgical edit aborted because it introduces a Python syntax error on line {e.lineno}{bad_line}: {e.msg}. File on disk was NOT modified."
                if trace_file: fail_msg += f"\n[SYSTEM: Failure trace saved to '{trace_file}']"
                if coder_thinking: fail_msg += f"\n<___CODER_THOUGHTS___>\n{coder_thinking}\n</___CODER_THOUGHTS___>"
                fail_msg += f"\n<___CODER_CODE___>\n{raw_json_str}\n</___CODER_CODE___>"
                return fail_msg

        # Archive backup snapshot
        filename = os.path.basename(real_target)
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        backup_path = f"/app/workspace/archive/{filename}.{timestamp}.surgical.bak"
        with open(backup_path, "w", encoding="utf-8") as f:
            f.write(current_code)

        # Write applied changes back onto disk safely
        with open(real_target, "w", encoding="utf-8") as f:
            f.write(updated_code)

        logger.info(f"[surgical_code_edit] Successfully applied {len(edits_list)} edit(s) to '{real_target}'")

        # Dynamic Git Commit Tracking Checkpoint
        if os.path.exists("/app/workspace/.git"):
            try:
                proc_add = await asyncio.create_subprocess_exec("git", "add", real_target, cwd=WORKSPACE_DIR, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                await proc_add.communicate()
                proc_diff = await asyncio.create_subprocess_exec("git", "diff", "--cached", "--quiet", cwd=WORKSPACE_DIR)
                diff_rc = await proc_diff.wait()
                if diff_rc != 0:
                    clean_obj = re.sub(r'[\r\n\x00-\x1f]+', ' ', edit_objective).strip()[:40]
                    proc_commit = await asyncio.create_subprocess_exec("git", "commit", "-m", f"feat(coder): surgical patch applied ({len(edits_list)} edits) for {clean_obj}", cwd=WORKSPACE_DIR, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                    await proc_commit.communicate()
            except Exception:
                pass

        registry = load_json(TOOL_REGISTRY_FILE)
        blueprint_hint = ""
        for category_data in registry.values():
            for name, t_data in category_data.get("tools", {}).items():
                if t_data.get("path") == filepath:
                    lang = t_data.get("language", "python")
                    if lang == "rust":
                        blueprint_hint = "\nREMINDER: This is a Rust asset. You MUST copy this updated file from plugins into your sandbox Cargo project structure (src/main.rs) and re-run compilation."
                    elif lang == "cpp":
                        blueprint_hint = "\nREMINDER: This is a C++ asset. Ensure you re-compile the source file using g++ before executing the binary path."

        edit_summary_block = "\n\n".join(applied_blocks)
        explanation_note = f" Note: {explanation}" if explanation else ""
        res = f"SUCCESS: Coder surgically applied {len(edits_list)} edit(s) to '{filepath}' to achieve the objective.{explanation_note} Backup generated: {backup_path}.{blueprint_hint}"
        if coder_thinking:
            res += f"\n<___CODER_THOUGHTS___>\n{coder_thinking}\n</___CODER_THOUGHTS___>"
        res += f"\n<___CODER_CODE___>\n{edit_summary_block}\n</___CODER_CODE___>"
        return res

    except Exception as e:
        logger.error(f"[surgical_code_edit] Exception on '{filepath}': {str(e)}")
        trace_file = save_failure_trace("coder_surgical_exception", {
            "subagent": "coder",
            "action": "surgical_code_edit",
            "filepath": filepath,
            "objective": edit_objective,
            "error": str(e),
            "traceback": traceback.format_exc()
        })
        trace_note = f"\n[SYSTEM: Failure trace saved to '{trace_file}']" if trace_file else ""
        return f"SYSTEM ERROR: Surgical Coder sequence aborted. Error: {str(e)}{trace_note}"

db_tool_desc = f"""Executes a SQL query against a specified SQLite database.
'db_path' MUST be an absolute path (e.g., '/app/workspace/state/my_db.db').

CRITICAL RULES:
- CONTEXT: Always use LIMIT in your SELECT queries (e.g., LIMIT 10) to protect your context window!
- MULTIPLE STATEMENTS: To execute multiple schema creation commands at once, you may chain them with semicolons.
- BULK INSERTS: To insert multiple rows efficiently, write a single parameterized INSERT statement and pass a LIST OF LISTS to 'parameters'. The system will automatically use batch execution (executemany).

SEMANTIC SEARCH: If you provide a string in 'search_text_to_embed', the system will automatically generate its vector and append it to the end of your 'parameters' list. 
Search Syntax Example:
  query="SELECT m.*, v.distance FROM (SELECT rowid, distance FROM docs_vec WHERE embedding MATCH ? ORDER BY distance LIMIT 5) v JOIN docs m ON v.rowid = m.id"
  parameters=[]
  search_text_to_embed="My search query"
"""

@mcp.tool(description=db_tool_desc)
async def query_sqlite_db(db_path: str, query: str, parameters: list = None, search_text_to_embed: str = None) -> str:

    # Validate database path is inside workspace
    resolved_db = os.path.abspath(db_path)
    if not (resolved_db == "/app/workspace" or resolved_db.startswith("/app/workspace/")):
        return "SYSTEM ERROR: Database path must be inside /app/workspace"

    conn = None

    try:
        # --- Handle Search Embedding Injection ---
        if search_text_to_embed:
            response = await embedding_client.embeddings.create(
                model=config.EMBEDDING_CONFIG["model"],
                input=search_text_to_embed
            )
            
            # Log embedding token usage with fallback
            tokens_in = response.usage.prompt_tokens if (response.usage and response.usage.prompt_tokens) else len(tokenizer.encode(search_text_to_embed))
            config.log_token_usage(STATE_DIR, "embedding", tokens_in, 0, 0)
            
            embedding_vector = response.data[0].embedding
            vector_blob = array.array('f', embedding_vector).tobytes()
            
            if parameters is None:
                parameters = []
            if len(parameters) > 0 and isinstance(parameters[0], list):
                return "SYSTEM ERROR: You cannot use 'search_text_to_embed' simultaneously with bulk (list of lists) parameters."
            parameters.append(vector_blob)
        
        # --- Database Execution ---
        db_dir = os.path.dirname(db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
            
        conn = sqlite3.connect(db_path)
        conn.set_authorizer(sqlite_authorizer)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        is_bulk_operation = False
        
        try:
            if parameters:
                # Detect Bulk Insert (List of Lists)
                if len(parameters) > 0 and isinstance(parameters[0], list):
                    cursor.executemany(query, parameters)
                    is_bulk_operation = True
                else:
                    cursor.execute(query, parameters)
            else:
                try:
                    cursor.execute(query)
                except Exception as e:
                    # Catch multi-statement attempts and cleanly route them to executescript
                    if "one statement at a time" in str(e).lower() or "can only execute one statement" in str(e).lower():
                        # Manually check for forbidden destructive actions since executescript bypasses the authorizer
                        forbidden_patterns = [r'\bdrop\s+table\b', r'\bdelete\s+from\b', r'\btruncate\b']
                        if any(re.search(pat, query.lower()) for pat in forbidden_patterns):
                            return "SYSTEM ERROR: Destructive database modifications (DROP/DELETE) are blocked in multi-statement queries."
                        
                        cursor.executescript(query)
                        conn.commit()
                        return f"Multi-statement script executed successfully. Rows affected: {cursor.rowcount}"
                    else:
                        raise e

        except Exception as db_exec_error:
            # Force structural rollbacks immediately if transaction validation fails to avoid frozen journal locks
            if conn:
                conn.rollback()
            raise db_exec_error
                        
        # --- Output Formatting & CONTEXT PROTECTION ---
        if is_bulk_operation or cursor.description is None:
            # executescript and executemany don't return fetchable rows
            rowcount = cursor.rowcount
            result_str = f"Operation executed successfully. Rows affected/processed: {rowcount}"
        else:
            rows = cursor.fetchall()
            if not rows:
                result_str = "Query executed successfully. 0 rows returned."
            else:
                MAX_ROWS = 50
                warning_msg = ""
                if len(rows) > MAX_ROWS:
                    warning_msg = f"\n\n... [SYSTEM WARNING: The query returned {len(rows)} rows, but only the first {MAX_ROWS} are shown to protect context. Use 'LIMIT' to paginate.]"
                    rows = rows[:MAX_ROWS]
                
                data = []
                for row in rows:
                    row_dict = {}
                    for k in row.keys():
                        v = row[k]
                        if isinstance(v, (bytes, bytearray)) and len(v) > 64:
                            row_dict[k] = f"<BLOB {len(v)} bytes>"
                        else:
                            row_dict[k] = v
                    data.append(row_dict)

                result_str = json.dumps(data, indent=2, default=str)
                if len(result_str) > 20000 and len(data) > 1:
                    while len(result_str) > 20000 and len(data) > 1:
                        data = data[:max(1, len(data) // 2)]
                        result_str = json.dumps(data, indent=2, default=str)
                    result_str += f"\n\n... [SYSTEM WARNING: Output was truncated to {len(data)} rows to stay within 20,000 characters. Refine your query or use LIMIT.]"
                else:
                    result_str += warning_msg
                    
        conn.commit()
        logger.info(f"[query_sqlite_db] Success on '{os.path.basename(db_path)}' ({query[:80].strip()})")
        return result_str

    except Exception as e:
        logger.error(f"[query_sqlite_db] Error on '{os.path.basename(db_path)}': {str(e)}")
        return f"SYSTEM ERROR: Database Exception: {str(e)}"
    finally:
        if conn:
            conn.close() # GUARANTEES the lock is released!


@mcp.tool()
async def batch_generate_embeddings(db_path: str, vec_table: str, source_query: str) -> str:
    """
    Generates vector embeddings in bulk natively from the database and inserts them into a vec0 virtual table.
    This completely bypasses the context window!
    'source_query' MUST be a SELECT statement returning EXACTLY two columns:
    1. The integer ID (which maps to the vec_table's rowid).
    2. The text string to be embedded.
    Example: "SELECT id, description FROM tools WHERE id NOT IN (SELECT rowid FROM tools_vec)"
    """
    # HARDENING: Validate table names to prevent SQL injection compilation tricks
    if not re.match(r'^[a-zA-Z0-9_]+$', vec_table):
        return "SYSTEM ERROR: Invalid characters detected in table name parameters."
    
    # Validate database path is inside workspace
    resolved_db = os.path.abspath(db_path)
    if not (resolved_db == "/app/workspace" or resolved_db.startswith("/app/workspace/")):
        return "SYSTEM ERROR: Database path must be inside /app/workspace"

    if not source_query.strip().upper().startswith("SELECT"):
        return "SYSTEM ERROR: 'source_query' must be a SELECT statement."
        
    conn = None # Initialize empty so the finally block doesn't crash

    try:
        # 1. Connect and LOAD THE VECTOR EXTENSION IMMEDIATELY
        conn = sqlite3.connect(db_path)
        conn.set_authorizer(sqlite_authorizer)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        cursor = conn.cursor()
        
        # Now it is safe to execute queries that reference virtual vec0 tables!
        cursor.execute(source_query)
        rows = cursor.fetchall()
        
        if not rows:
            return "SUCCESS: source_query returned 0 rows. Nothing to embed."
            
        rowids = []
        texts_to_embed = []
        for row in rows:
            if len(row) != 2:
                return f"SYSTEM ERROR: source_query must return exactly 2 columns (id, text). Yours returned {len(row)}."
            rowids.append(row[0])
            texts_to_embed.append(str(row[1]))
            
        # 2. Generate Embeddings via API in safe chunks of 128
        BATCH_SIZE = 128
        inserted_count = 0
        total_tokens_in = 0

        for chunk_start in range(0, len(texts_to_embed), BATCH_SIZE):
            chunk_texts = texts_to_embed[chunk_start:chunk_start + BATCH_SIZE]
            chunk_rowids = rowids[chunk_start:chunk_start + BATCH_SIZE]

            response = await embedding_client.embeddings.create(
                model=config.EMBEDDING_CONFIG["model"],
                input=chunk_texts
            )

            if response.usage and response.usage.prompt_tokens:
                total_tokens_in += response.usage.prompt_tokens
            else:
                total_tokens_in += sum(len(tokenizer.encode(t)) for t in chunk_texts)

            # Insert batch into vec table
            for i, rowid in enumerate(chunk_rowids):
                embedding_vector = response.data[i].embedding
                vector_blob = array.array('f', embedding_vector).tobytes()
                cursor.execute(f"INSERT OR REPLACE INTO {vec_table}(rowid, embedding) VALUES (?, ?)", (rowid, vector_blob))
                inserted_count += 1

        config.log_token_usage(STATE_DIR, "embedding", total_tokens_in, 0, 0)
        conn.commit()
        return f"SUCCESS: Natively extracted and embedded {inserted_count} rows into '{vec_table}'."
        
    except Exception as e:
        return f"SYSTEM ERROR: Batch Embedding Failed. {str(e)}"
    finally:
        if conn:
            conn.close() # GUARANTEES the lock is released!

@mcp.tool()
async def search_web(query: str, max_results: int = 5) -> str:
    """Searches the web using a stealth browser to find relevant URLs and snippets.
    ALWAYS use this tool first to find factual URLs before using fetch_webpage.
    Do NOT guess or fabricate URLs.
    """
    # We use DuckDuckGo's legacy HTML page because it lacks advanced JS bot-detection
    url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}"
    
    try:
        # Spin up the exact same stealth browser you use for fetch_webpage
        async with Stealth().use_async(async_playwright()) as p:
            browser = await p.chromium.launch(
                headless=True, 
                args=[
                    '--no-sandbox', 
                    '--disable-setuid-sandbox', 
                    '--disable-dev-shm-usage',
                    '--disable-blink-features=AutomationControlled' 
                ]
            )
            try:
                context = await browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    viewport={"width": 1920, "height": 1080},
                    locale="en-US"
                )
                
                page = await context.new_page()
                
                # Navigate to the search page
                await page.goto(url, wait_until="domcontentloaded", timeout=15000)
                html_content = await page.content()
            finally:
                await browser.close()
            
        # Parse the raw HTML using BeautifulSoup
        soup = BeautifulSoup(html_content, 'html.parser')
        results = soup.find_all('div', class_='result')
        
        if not results:
            return f"SYSTEM ERROR: No results found for '{query}'. The stealth browser may have been served a Captcha."
            
        formatted_results = f"--- SEARCH RESULTS FOR '{query}' ---\n\n"
        count = 0
        
        for row in results:
            if count >= max_results:
                break
                
            title_tag = row.find('a', class_='result__a')
            snippet_tag = row.find('a', class_='result__snippet')
            url_tag = row.find('a', class_='result__url')
            
            if title_tag and snippet_tag and url_tag:
                title = title_tag.text.strip()
                snippet = snippet_tag.text.strip()
                
                # Extract the real destination URL from the redirect link if available
                actual_url = ""
                raw_href = title_tag.get('href', '')
                if "uddg=" in raw_href:
                    try:
                        qs = urllib.parse.parse_qs(urllib.parse.urlparse(raw_href).query)
                        if "uddg" in qs and qs["uddg"]:
                            actual_url = qs["uddg"][0]
                    except Exception:
                        pass
                        
                if not actual_url:
                    actual_url = url_tag.text.strip()
                    # Clean up breadcrumbs or extra spaces if present
                    actual_url = actual_url.split()[0]
                    if not actual_url.startswith("http"):
                        actual_url = "https://" + actual_url
                    
                formatted_results += f"{count+1}. {title}\nURL: {actual_url}\nSnippet: {snippet}\n\n"
                count += 1
                
        if count == 0:
            return f"SYSTEM ERROR: Could not extract valid links from the search page."
            
        return formatted_results

    except Exception as e:
        return f"SYSTEM ERROR: Stealth search failed. {str(e)}"


def is_safe_web_url(url: str) -> tuple[bool, str]:
    """Validates that a URL is safe to fetch and does not probe local/host networks."""
    try:
        parsed = urllib.parse.urlparse(url.strip())
        if parsed.scheme.lower() not in ("http", "https"):
            return False, f"Scheme '{parsed.scheme}' is blocked. Only http and https are permitted."
        
        hostname = parsed.hostname
        if not hostname:
            return False, "Missing hostname in URL."
            
        lower_host = hostname.lower()
        if lower_host in ("localhost", "host.containers.internal", "host.docker.internal"):
            return False, f"Host alias '{hostname}' is restricted."

        try:
            addr_info = socket.getaddrinfo(hostname, None)
        except socket.gaierror as e:
            return False, f"DNS resolution failed for '{hostname}': {str(e)}"

        for _, _, _, _, sockaddr in addr_info:
            ip = ipaddress.ip_address(sockaddr[0])
            if (
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_reserved
                or ip.is_multicast
            ):
                return False, f"Target resolves to restricted private/internal IP ({sockaddr[0]})."

        return True, ""
    except Exception as e:
        return False, f"Invalid URL: {str(e)}"


@mcp.tool()
async def fetch_webpage(url: str) -> str:
    """Fetches a webpage using a headless Chromium browser to render JavaScript, 
    and returns ONLY the clean, readable text. 
    Use this to read documentation, articles, or search results without writing a custom scraper.
    """
    safe, reason = is_safe_web_url(url)
    if not safe:
        return f"SYSTEM ERROR: URL blocked by security policy. {reason}"

    try:
        async with Stealth().use_async(async_playwright()) as p:
            # Launch chromium natively in headless mode with container-safe flags
            browser = await p.chromium.launch(
                headless=True, 
                args=[
                    '--no-sandbox', 
                    '--disable-setuid-sandbox', 
                    '--disable-dev-shm-usage',
                    '--disable-blink-features=AutomationControlled' 
                ]
            )
            try:
                # Spoof a realistic Windows Chrome browser
                context = await browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    viewport={"width": 1920, "height": 1080},
                    locale="en-US",
                    timezone_id="America/New_York"
                )
                
                page = await context.new_page()
                
                # Protect against redirect SSRF to internal/host IPs
                async def filter_navigation(route):
                    if route.request.is_navigation_request():
                        req_safe, _ = is_safe_web_url(route.request.url)
                        if not req_safe:
                            await route.abort()
                            return
                    await route.continue_()

                await page.route("**/*", filter_navigation)
                            
                # Navigate and wait for the page to finish loading its network requests (JS rendering)
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                            
                # Extract the raw HTML after JS has executed
                html_content = await page.content()
            finally:
                await browser.close()
            
            # Use BeautifulSoup to aggressively strip out layout garbage
            soup = BeautifulSoup(html_content, 'html.parser')
            
            # Extract scholarly and document metadata before decomposing tags
            scholarly_meta = []
            for meta_tag in soup.find_all('meta'):
                name = meta_tag.get('name', '').lower()
                prop = meta_tag.get('property', '').lower()
                content = meta_tag.get('content', '').strip()
                if content and (name.startswith('citation_') or name.startswith('dc.') or prop.startswith('og:') or name == 'description'):
                    meta_key = name if name else prop
                    scholarly_meta.append(f"{meta_key}: {content}")

            for tag in soup(['script', 'style', 'nav', 'footer', 'aside', 'header', 'meta', 'noscript', 'svg']):
                tag.decompose()
                
            # Extract just the readable text
            text = soup.get_text(separator='\n\n')
            
            # Prepend extracted metadata if present
            if scholarly_meta:
                meta_block = "--- SCHOLARLY / PAGE METADATA ---\n" + "\n".join(scholarly_meta[:25]) + "\n\n"
                text = meta_block + text

            # Clean up excessive newlines to protect the context window
            clean_text = re.sub(r'\n\s*\n', '\n\n', text).strip()
            
            # --- THE POINTER APPROACH (Context Protection) ---
            if len(clean_text) > 20000:
                # Create a safe filename based on the domain name
                parsed_url = urllib.parse.urlparse(url)
                safe_domain = re.sub(r'[^a-zA-Z0-9]', '_', parsed_url.hostname or "webpage")
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                temp_file_name = f"web_{safe_domain}_{timestamp}.txt"
                os.makedirs(SANDBOX_DIR, exist_ok=True)
                temp_file_path = os.path.join(SANDBOX_DIR, temp_file_name)
                
                # Save the full scraped text
                with open(temp_file_path, "w", encoding="utf-8") as f:
                    f.write(f"--- FULL CONTENT FROM {url} ---\n\n{clean_text}")
                    
                preview = clean_text[:6000]
                
                return (f"--- PREVIEW FROM {url} (First 6,000 of {len(clean_text):,} chars) ---\n\n{preview}\n\n"
                        f"... [SYSTEM CONTEXT GUARDRAIL: The webpage was {len(clean_text):,} characters long. To protect your context window, "
                        f"the full text was saved to '/app/workspace/sandbox/{temp_file_name}'.\n"
                        f"HOW TO GET AROUND THIS TRUNCATION: Inspect the full text using analyze_files(['/app/workspace/sandbox/{temp_file_name}'], ...) "
                        f"or bash tools: execute_bash('grep -n -C 3 <pattern> /app/workspace/sandbox/{temp_file_name}') or 'sed'.]")
                
            return f"--- CONTENT FROM {url} ---\n\n{clean_text}"

    except Exception as e:
        return f"Failed to fetch {url}. Error: {str(e)}"


@mcp.tool()
async def analyze_files(
    filepaths: list[str], 
    instruction: str,
    start_line: int = 1,
    max_lines: int = None,
    tail_mode: bool = False
) -> str:
    """Delegates the analysis of multiple massive text files, logs, or images to the Analyst LLM.
    Use this to prevent large files from blowing out your context window, or to compare multiple files.
    'filepaths' must be a list of absolute paths to the files.
    'instruction' must be a specific question or command (e.g., "Compare these logs", "Find the error between this code and this log").
    'start_line': 1-indexed line number to start reading from (defaults to 1). Use this to read a specific slice of a large file.
    'max_lines': Maximum number of lines to read (capped at 50,000 characters per file to protect the Analyst context window).
    'tail_mode': If True, reads from the end (tail) of the file up to 50,000 characters. Perfect for inspecting recent errors in large log files!
    """
    api_args = analyst_profile["api_params"].copy()
    api_args["model"] = analyst_profile["model"]
    
    # We build a multi-part message array
    user_content = [{"type": "text", "text": f"Instruction: {instruction}\n\n"}]
    truncated_files_info = []
    
    try:
        # Pre-flight check: if all requested existing files are empty (0 bytes), return immediately
        existing_files = [f for f in filepaths if os.path.exists(f) and not os.path.isdir(f)]
        if existing_files and all(os.path.getsize(f) == 0 for f in existing_files):
            file_names = ", ".join([os.path.basename(f) for f in existing_files])
            return f"SYSTEM NOTICE: Target file(s) [{file_names}] are currently 0 bytes (empty). No content or tracebacks exist to analyze."

        for filepath in filepaths:
            if not os.path.exists(filepath):
                user_content.append({"type": "text", "text": f"\n[ERROR: File '{filepath}' does not exist.]"})
                continue

            if os.path.isdir(filepath):
                user_content.append({"type": "text", "text": f"\n[ERROR: Path '{filepath}' is a directory, not a file. Use bash tools (ls, tree) to inspect directories.]"})
                continue

            mime_type, _ = mimetypes.guess_type(filepath)
            is_image = mime_type and mime_type.startswith('image/')
            
            filename = os.path.basename(filepath)
            
            if is_image:
                # --- VISION PIPELINE ---
                file_size = os.path.getsize(filepath)
                if file_size > 10 * 1024 * 1024:
                    user_content.append({"type": "text", "text": f"\n[ERROR: Image '{filename}' is too massive ({file_size / (1024*1024):.1f} MB) for the vision API. Max size is 10 MB.]"})
                    continue
                with open(filepath, "rb") as image_file:
                    base64_image = base64.b64encode(image_file.read()).decode('utf-8')
                user_content.append({"type": "text", "text": f"\n--- IMAGE: {filename} ---"})
                user_content.append({"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{base64_image}"}})
            else:
                # --- TEXT PIPELINE ---
                file_size = os.path.getsize(filepath)
                if file_size == 0:
                    user_content.append({"type": "text", "text": f"\n--- TEXT FILE: {filename} (EMPTY - 0 bytes) ---\n[This file is currently 0 bytes / empty. No data has been written to it.]\n"})
                    continue

                content, start_idx, end_idx, total_lines, is_partial = read_file_slice(
                    filepath, start_line=start_line, max_lines=max_lines, tail_mode=tail_mode, max_chars=50000
                )

                if is_partial:
                    pct = max(1, int((len(content) / file_size) * 100)) if file_size > 0 else 100
                    truncated_files_info.append(f"'{filename}' (read lines {start_idx}–{end_idx} of {total_lines:,}, {len(content):,}/{file_size:,} chars, {pct}%)")
                    
                    coverage_header = (
                        f"[COVERAGE BOUNDARY: Read lines {start_idx} to {end_idx} of {total_lines:,} total lines "
                        f"({len(content):,} of {file_size:,} chars, {pct}% coverage). "
                        f"Lines outside this window were omitted to protect your context window. "
                        f"MANDATORY: State this coverage explicitly in your report. Do NOT guess or extrapolate events or errors outside this ingested window.]\n"
                    )
                    content = coverage_header + content + f"\n... [END OF INGESTED SLICE (lines {start_idx}–{end_idx} of {total_lines:,})] ..."

                user_content.append({"type": "text", "text": f"\n--- TEXT FILE: {filename} ---\n{content}\n"})

        api_args["messages"] = [
            {"role": "system", "content": config.PROMPTS["analyst_system"]},
            {"role": "user", "content": user_content}
        ]
        
        # --- DYNAMIC PAYLOAD CHECKER ---
        # 1. Look up the context limit for the Analyst profile
        max_context = analyst_profile.get("context_window", analyst_profile.get("max_context_tokens", config.MAX_CONTEXT_TOKENS))
        safe_budget = int(max_context * 0.90) # Leave 10% for the response!
        
        # 2. Accurately measure what we are about to send
        payload_tokens = get_payload_tokens(api_args["messages"])
        
        # 3. Bounce the request back to the Brain if it's too massive
        if payload_tokens > safe_budget:
            return (f"SYSTEM ERROR: The files you asked the Analyst to read are too massive! "
                    f"Your payload is {payload_tokens} tokens, but the safety limit is {safe_budget} tokens. "
                    f"Please run 'analyze_files' on fewer files at a time, or use bash tools like 'head', 'tail', or 'grep' to narrow down the data first.")
                    
        # --- 1. FORCE THE JSON SCHEMA ---
        api_args["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": "analyst_report_schema",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {
                        "executive_summary": {
                            "type": "string", 
                            "description": "A 1-3 sentence definitive answer or core conclusion."
                        },
                        "full_report": {
                            "type": "string", 
                            "description": "The exhaustive, detailed analysis and breakdown."
                        }
                    },
                    "required": ["executive_summary", "full_report"],
                    "additionalProperties": False
                }
            }
        }

        if config.VERBOSITY_MODE != "silent":
            sys.stderr.write(f"\n\033[93m[System: Analyst payload is ~{payload_tokens} estimated tokens]\033[0m\n")

        # Call the Analyst Model
        logger.info(f"[analyze_files] Delegating analysis of {[os.path.basename(f) for f in filepaths]}")
        response = await analyst_client.chat.completions.create(**api_args)
        analyst_thinking, raw_analyst_content = extract_thinking_and_content(response.choices[0].message)
        finish_reason = getattr(response.choices[0], 'finish_reason', None) if response.choices else None
        if finish_reason == "length":
            logger.warning(f"[analyze_files] Analyst generation was truncated by max_tokens limit")
        
        # Log token usage with fallback
        tokens_in = response.usage.prompt_tokens if (response.usage and response.usage.prompt_tokens) else get_payload_tokens(api_args["messages"])
        tokens_out = response.usage.completion_tokens if (response.usage and response.usage.completion_tokens) else (len(tokenizer.encode(raw_analyst_content)) if tokenizer else len(raw_analyst_content) // 4)
        thinking_tokens = getattr(response.usage.completion_tokens_details, 'reasoning_tokens', 0) if response.usage and hasattr(response.usage, 'completion_tokens_details') and response.usage.completion_tokens_details else 0
        if thinking_tokens == 0 and analyst_thinking:
            thinking_tokens = len(analyst_thinking) // 4
        config.log_token_usage(STATE_DIR, "analyst", tokens_in, tokens_out, thinking_tokens)
        
        # --- 2. PARSE THE JSON ---
        try:
            clean_json = raw_analyst_content.strip()
            if "```" in clean_json:
                match = re.search(r"```(?:json)?\s*(.*?)\s*```", clean_json, re.DOTALL)
                if match:
                    clean_json = match.group(1).strip()
            report_data = json.loads(clean_json)
            ex_summ = report_data.get("executive_summary", "")
            full_rep = report_data.get("full_report", "")
        except json.JSONDecodeError:
            # Fallback just in case the JSON breaks
            ex_summ = "Failed to parse JSON."
            full_rep = raw_analyst_content
            
        file_list = ", ".join([os.path.basename(f) for f in filepaths])
        combined_text = f"--- EXECUTIVE SUMMARY ---\n{ex_summ}\n\n--- DETAILED REPORT ---\n{full_rep}"
        if finish_reason == "length":
            combined_text += "\n\n[SYSTEM NOTICE: Analyst generation was cut short because it reached the max_tokens limit. Consider requesting a more concise format or auditing specific sections.]"
        
        # --- 3. AUTO-SAVE THE FULL COMBINED REPORT (pure report output) ---
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{timestamp}_analyst_report.md"
        filepath = os.path.join(STATE_DIR, filename)
        
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(combined_text)
            
        # --- 4. YOUR DYNAMIC ROUTING LOGIC ---
        coverage_banner = ""
        if truncated_files_info:
            coverage_banner = (
                f"[COVERAGE WARNING: Partial read for: {', '.join(truncated_files_info)}.\n"
                f"The Analyst ONLY analyzed the ingested lines. Do NOT assume facts about unread lines.\n"
                f"HOW TO GET AROUND THIS TRUNCATION:\n"
                f"- To inspect a subsequent slice: analyze_files(filepaths=[...], instruction='...', start_line=<N>)\n"
                f"- To inspect the tail of a log file: analyze_files(filepaths=[...], instruction='...', tail_mode=True)\n"
                f"- Or use execute_bash with: 'tail -n 200 <path>', 'sed -n \\'<start>,<end>p\\' <path>', or 'grep -n <pattern> <path>'.]\n\n"
            )

        if len(combined_text) > 20000:
            # Return ONLY the summary
            result_msg = (f"{coverage_banner}--- ANALYST EXECUTIVE SUMMARY FOR [{file_list}] ---\n{ex_summ}\n\n"
                    f"... [SYSTEM ALERT: The detailed report was {len(combined_text):,} chars long. To protect your context window, "
                    f"the full analysis was saved to '/app/workspace/state/{filename}'.\n"
                    f"HOW TO GET AROUND THIS TRUNCATION: Inspect specific sections using 'analyze_files' or bash 'sed/grep' on '/app/workspace/state/{filename}'.]")
        else:
            # Return BOTH
            result_msg = f"{coverage_banner}--- ANALYST REPORT FOR [{file_list}] ---\n{combined_text}\n\n[SYSTEM: A backup of this report was saved to '/app/workspace/state/{filename}']"

        if analyst_thinking:
            result_msg += f"\n<___ANALYST_THOUGHTS___>\n{analyst_thinking}\n</___ANALYST_THOUGHTS___>"
        result_msg += f"\n<___ANALYST_REPORT___>\n{combined_text}\n</___ANALYST_REPORT___>"
        return result_msg

    except Exception as e:
        logger.error(f"[analyze_files] Failed to process files: {str(e)}")
        trace_file = save_failure_trace("analyst", {
            "subagent": "analyst",
            "filepaths": filepaths,
            "instruction": instruction,
            "error": str(e),
            "traceback": traceback.format_exc(),
            "messages": api_args.get("messages", []) if 'api_args' in locals() else []
        })
        trace_note = f"\n[SYSTEM: Failure trace saved to '{trace_file}']" if trace_file else ""
        return f"Analyst failed to process files. Error: {str(e)}{trace_note}"
        
@mcp.tool()
def load_skill(skill_name: str = "") -> str:
    """Loads the full instructional blueprint for a given skill.
    If called without a skill_name (or empty string), it lists all available skills and their descriptions.
    Use this immediately when your current task matches a skill in your system prompt."""
    global _LOADED_SKILLS
    skills_root = os.path.realpath("/app/workspace/skills")
    
    # Helper to gather all available skills
    available_skills = {}
    if os.path.exists(skills_root):
        for item in sorted(os.listdir(skills_root)):
            s_path = os.path.join(skills_root, item, "SKILL.md")
            if os.path.exists(s_path):
                try:
                    with open(s_path, "r", encoding="utf-8") as f:
                        c = f.read()
                    d_match = re.search(r'description:\s*(.+)', c)
                    desc = d_match.group(1).strip() if d_match else "No description provided."
                    available_skills[item] = re.sub(r'[\r\n\x00-\x1f]+', ' ', desc)[:200]
                except Exception:
                    available_skills[item] = "Available (failed to parse description)."

    # If no skill name provided, return the catalog
    if not skill_name or not skill_name.strip():
        if not available_skills:
            return "AVAILABLE SKILLS: None currently installed in /app/workspace/skills. Use commission_architect to build some."
        summary_lines = ["AVAILABLE SKILLS REGISTRY (Call load_skill(skill_name) to activate):"]
        for s_name, s_desc in available_skills.items():
            summary_lines.append(f"- {s_name}: {s_desc}")
        return "\n".join(summary_lines)

    safe_name = os.path.basename(skill_name.strip())
    skill_path = os.path.realpath(os.path.join(skills_root, safe_name, "SKILL.md"))
    try:
        if not os.path.commonpath([skill_path, skills_root]) == skills_root:
            return f"[SYSTEM ERROR: Invalid skill name '{skill_name}'.]"
    except Exception:
        return f"[SYSTEM ERROR: Invalid skill name '{skill_name}'.]"

    if not os.path.exists(skill_path):
        avail_list = ", ".join(available_skills.keys()) if available_skills else "None"
        return f"[SYSTEM ERROR: Skill '{safe_name}' not found. Available skills: {avail_list}]"
        
    with open(skill_path, "r", encoding="utf-8") as f:
        content = f.read()
        
    is_reload = safe_name in _LOADED_SKILLS
    _LOADED_SKILLS.add(safe_name)
    status_label = "RE-ACTIVATED" if is_reload else "ACTIVATED"
    return f"--- SKILL {status_label}: {safe_name} ---\n{content}\n\n[SYSTEM: You must now strictly follow these instructions.]"


@mcp.tool()
async def commission_architect(skill_name: str, objective: str, brain_notes: str, context_filepaths: list[str] = None) -> str:
    """Use this immediately after solving a complex problem to permanently document it as a system skill.
    Passes raw notes to the Architect agent, who formats and saves it as a new Skill.
    'context_filepaths' can take target code implementations or terminal output histories to extract instructions from.
    """
    safe_name = re.sub(r'[^a-zA-Z0-9_-]', '', os.path.basename(skill_name.strip()))
    if not safe_name:
        return "[ARCHITECT ERROR] Invalid skill name provided."

    # ◄--- Gather background reference metrics ---
    file_environmental_context = gather_agent_context(context_filepaths)

    architect_user = f"{file_environmental_context}Skill Name: {safe_name}\nObjective: {objective}\nBrain's Notes:\n{brain_notes}"
    
    try:
        messages = [
            {"role": "system", "content": config.SYSTEM_PROMPTS["architect"]},
            {"role": "user", "content": architect_user}
        ]
        
        if config.VERBOSITY_MODE != "silent":
            payload_tokens = get_payload_tokens(messages)
            sys.stderr.write(f"\n\033[93m[System: Architect payload is ~{payload_tokens} estimated tokens]\033[0m\n")

        api_args = architect_profile.get("api_params", {}).copy()
        api_args["model"] = architect_profile["model"]
        api_args["messages"] = messages

        response = await architect_client.chat.completions.create(**api_args)
        architect_thinking, formatted_skill_md = extract_thinking_and_content(response.choices[0].message)
        
        # Log token usage with fallback
        tokens_in = response.usage.prompt_tokens if (response.usage and response.usage.prompt_tokens) else get_payload_tokens(api_args["messages"])
        tokens_out = response.usage.completion_tokens if (response.usage and response.usage.completion_tokens) else (len(tokenizer.encode(formatted_skill_md)) if tokenizer else len(formatted_skill_md) // 4)
        thinking_tokens = getattr(response.usage.completion_tokens_details, 'reasoning_tokens', 0) if response.usage and hasattr(response.usage, 'completion_tokens_details') and response.usage.completion_tokens_details else 0
        if thinking_tokens == 0 and architect_thinking:
            thinking_tokens = len(architect_thinking) // 4
        config.log_token_usage(STATE_DIR, "architect", tokens_in, tokens_out, thinking_tokens)
        
        clean_skill_md = formatted_skill_md.strip()
        # Only unwrap if the entire output was enclosed in top-level markdown fences
        if clean_skill_md.startswith("```"):
            clean_skill_md = re.sub(r"^```(?:markdown|md)?\s*\r?\n", "", clean_skill_md)
            clean_skill_md = re.sub(r"\r?\n```\s*$", "", clean_skill_md).strip()

        skill_dir = f"/app/workspace/skills/{safe_name}"
        os.makedirs(skill_dir, exist_ok=True)
        
        skill_path = os.path.join(skill_dir, "SKILL.md")
        with open(skill_path, "w", encoding="utf-8") as f:
            f.write(clean_skill_md)
            
        result_msg = f"[SUCCESS] The Architect has drafted and saved '{safe_name}' to {skill_path}. It is now immediately available in your Available Skills Menu and can be loaded via load_skill('{safe_name}')."
        if architect_thinking:
            result_msg += f"\n<___ARCHITECT_THOUGHTS___>\n{architect_thinking}\n</___ARCHITECT_THOUGHTS___>"
        result_msg += f"\n<___ARCHITECT_SKILL___>\n{clean_skill_md}\n</___ARCHITECT_SKILL___>"
        return result_msg
    except Exception as e:
        trace_file = save_failure_trace("architect", {
            "subagent": "architect",
            "skill_name": skill_name,
            "objective": objective,
            "brain_notes": brain_notes,
            "error": str(e),
            "traceback": traceback.format_exc(),
            "messages": api_args.get("messages", []) if 'api_args' in locals() else []
        })
        trace_note = f"\n[SYSTEM: Failure trace saved to '{trace_file}']" if trace_file else ""
        return f"[ARCHITECT ERROR] Failed to generate skill: {str(e)}{trace_note}"


if __name__ == "__main__":
    mcp.run()