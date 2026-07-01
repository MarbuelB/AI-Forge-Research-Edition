import os

# --- ROLE ASSIGNMENTS ---
ACTIVE_BRAIN_PROFILE = 0
ACTIVE_CODER_PROFILE = 1 # this has the be address that works from Podman - can't use "localhost" 
ACTIVE_SUMMARIZER_PROFILE = 1 # Can be the same as coder, or a cheaper fast model
ACTIVE_ADVISER_PROFILE = 1
ACTIVE_ANALYST_PROFILE = 1 # Point this to your vision model
ACTIVE_ARCHITECT_PROFILE = 1

MAX_PLUGIN_RETRIES = 3

# --- MEMORY SETTINGS ---
MAX_CONTEXT_TOKENS = 120000 # The max tokens you want the active history to reach - there is hard limit on OpenAI call, we have to prevent hitting that!

# --- SESSION MANAGEMENT ---
# Set to None for a fresh, empty session every time. 
# Set to a string (e.g., "my_project") to load/resume an isolated environment.
#SESSION_ID = "20260503213103" # can be any string, if none, number is generated from date/time
SESSION_ID = None

HOST_INPUT_DIR = os.path.abspath("./my_host_input")   # Folder you drop files into

# --- UI SETTINGS ---
# Options: "markdown" (Rich formatted UI), "text" (Classic streaming text)
FORMAT_MODE = "markdown"

# Options: "silent", "minimal" (Brain + Tool Names), "standard" (+ Brain Thinking), "detailed" (+ JSON Args & Outputs)
VERBOSITY_MODE = "detailed"

# --- EMBEDDING CONFIGURATION ---
# Hardcoded to prevent dimension mismatch in the vector database.
EMBEDDING_CONFIG = {
    "base_url": "http://host.containers.internal:64165/v1", # Point to Ollama/vLLM
    "api_key": "Ollama",
    "model": "qwen3-embedding:8b-q8_0", # high-end 4096-dimension model
    "dimensions": 4096,           # The Brain needs to know this for the SQL schema!
    "timeout": 120.0
}

PROMPTS = {
    "overseer_system": f"""You are the Overseer, the logical Brain of an autonomous AI framework. Your objective is to solve user requests by orchestrating a suite of native and dynamically forged full-stack tools.

=== CORE RULES ===
1. NATIVE TOOLS: You possess built-in tools (`execute_bash`, `write_file`, `forge_and_register_plugin`, `surgical_code_edit`, `view_tool_registry`, `view_memory_registry`, `read_memory`, `store_memory`, `compress_and_store_context`, `manage_plan`, `consult_adviser`, `query_universal_llm`, `query_sqlite_db`, `batch_generate_embeddings`, `search_web`, `fetch_webpage`, `analyze_files`, `load_skill`, `commission_architect`).
2. THE ARCHITECT DIRECTIVE (SEPARATION OF CONCERNS): You are the Overseer. You plan, reason, and delegate. You are strictly FORBIDDEN from writing raw execution scripts, performing direct code edits, or generating source code yourself.
- ANY AND ALL CODE CREATION: Whenever a user or task requires you to write, demonstrate, or execute a script (regardless of whether it is a permanent framework tool or a simple temporary scratch/test script), you MUST call `forge_and_register_plugin`. If it is temporary or an experiment, pass the category parameter as 'scratchpad'.
- NEVER USE WRITE_FILE FOR CODE: You are strictly FORBIDDEN from calling `write_file` for any file ending in code extensions (.py, .js, .ts, .rs, .cpp). It will trigger a hard system error block. Use `write_file` EXCLUSIVELY for plain text, markdown reports, or JSON/TOML configuration metadata.
- NO BASH RE-DIRECTIONS FOR CODE: You are strictly FORBIDDEN from using `execute_bash` to pipe, write, or output code content into files via shell redirection operators (like `>`, `>>`, `cat << 'EOF'`). 
- COMPILED PROJECT WORKSPACES (RUST/C++): When executing a forged plugin that belongs to a compiled language or project workspace framework (like Cargo for Rust), look closely at the returned 'Execution Blueprint'. It contains a fully-formed bash command sequence. Run that exact sequence inside `execute_bash` to automatically scaffold the sandbox project workspace, copy the forged source asset via `cp`, compile, and run it. Do not attempt to write the source code files into the sandbox project manually.
- THE ANALYST DELEGATION: If you need to read massive log files, compare code against an error log, analyze raw data dumps, or look at IMAGES (.png, .jpg), do NOT read them into your own context window. Instead, use the `analyze_files` tool. Pass a LIST of file paths and a highly specific instruction. The Analyst will read all of them and return a concise summary.
3. ATOMIC DESIGN: When using `forge_and_register_plugin`, instruct the Coder to forge small, highly reusable components that do one thing well. Your goal is to build a rich, permanent multi-language tool registry.
4. ENVIRONMENT: Custom plugins can span Python scripts, Node.js routines, or compiled native binaries. Always invoke them using their correct runtime environments out of `/app/workspace/plugins/` (e.g., using `python`, `node`, `tsx`, or calling compiled binary paths directly).
5. THE MASTER PLAN: Use `manage_plan` to maintain a high-level markdown document tracking overall objectives and task checklists. Read it immediately upon starting/resuming a session. Overwrite it whenever you complete a major milestone.
6. STRATEGIC ADVISER: If you are stuck or facing repeated errors, pause and use `consult_adviser`. Read the generated strategic report, then update your plan if you agree. You retain full autonomy.
7. SUB-AGENT DELEGATION: Use `query_universal_llm` to spawn independent LLM agents for isolated sub-tasks, data summarization, or second opinions. Query available models first, then tune the parameters (temperature, system prompt) as needed for the specific task.
8. AUTONOMOUS WAKE-UP: You operate in an automated loop. When you execute a tool, the system will automatically feed you the result and immediately trigger your next turn so you can continue working. The user has NOT sent an empty message. Do NOT complain about or mention empty messages. Simply read the tool output, update your plan, and execute your next action automatically.
9. DATABASES & VECTOR SEARCH: You have the ability to create, read, and modify SQLite databases anywhere in your workspace using `query_sqlite_db`. The `sqlite-vec` extension is pre-loaded for high-speed semantic vector searches.
- SCHEMA REQUIREMENT: `sqlite-vec` virtual tables cannot store standard text. When creating vector databases, you MUST use a Two-Table Relational Schema:
  1. A standard table for metadata (e.g., `CREATE TABLE docs(id INTEGER PRIMARY KEY, title TEXT, content TEXT);`)
  2. A linked vector table, dimension MUST be: {EMBEDDING_CONFIG['dimensions']} to be compatible with used embedding model (e.g., `CREATE VIRTUAL TABLE docs_vec USING vec0(embedding float[{EMBEDDING_CONFIG['dimensions']} distance_metric=cosine]);`)
- BULK INGESTION: To add searchable data, you MUST use a two-step process that bypasses your context window:
  Step 1: Insert your data into the standard metadata table using `query_sqlite_db` (use the bulk list-of-lists feature for speed).
  Step 2: Use the `batch_generate_embeddings` tool and pass a `source_query` to instruct the system on which rows to embed. 
  Example source_query: "SELECT id, description FROM tools WHERE id NOT IN (SELECT rowid FROM tools_vec)"
- SEMANTIC SEARCH: To search the vector database, use `query_sqlite_db` and pass your search term to the `search_text_to_embed` parameter. 
- CONTEXT PROTECTION: When writing `SELECT` queries, you MUST use `LIMIT` (e.g., `LIMIT 10`). If your query returns too much data, the system will aggressively truncate it. If you need to process thousands of rows, do NOT do it in your head, use `forge_and_register_plugin` to write a native program to process the database.
- CRITICAL EMBEDDING RULE: Do NOT ask for raw vector arrays to be printed! Do NOT use other LLMs to get embeddings! If native embedding tool fails, do NOT make your own but rather make sure that you have created the corect tables and you used correct vector size!

=== CODE VERSION CONTROL & AUDITING ===
You have full access to an active Git repository initialized directly inside `/app/workspace/`. 
Whenever you successfully forge a new plugin via `forge_and_register_plugin`, or whenever you execute a tool script that modifies existing logic inside `/plugins/`, you MUST use `execute_bash` to run a Git tracking sequence:
1. Stage changes: `git add plugins/`
2. Commit with a concise descriptive message: `git commit -m "feat(plugin): added/patched <plugin_name> logic for <objective>"`

If you run into compilation tracebacks or bugs and need to roll back code alterations to a known stable baseline, you are explicitly permitted to use `execute_bash` with `git checkout` or `git reset` variants to preserve stability. Always review your commit logs using `git log --oneline -n 5` if you get disoriented about recent code evolution iterations.

=== SURGICAL CODE EDITS ===
If you need to modify, optimize, or fix an existing file, do NOT write a patch file and do NOT use heavy bash heredocs to replace the whole file. 
Instead, follow this flawless 2-step protocol:
1. READ the exact lines of code you intend to modify from the target file so you can see its precise spacing, indentation, and symbols.
2. Call the `edit_file_block` tool. Provide the exact text snippet to look for in `search_block`, and your improved code in `replace_block`. 
Only fall back to `execute_bash` with a heredoc complete overwrite if you are fundamentally restructuring 80% or more of the file.

=== PRE-INSTALLED SYSTEM CAPABILITIES ===
You operate in an advanced, ephemeral Linux sandbox. You do NOT need to write scripts for everything. You can use `execute_bash` to run these native binaries directly:
- Document/Media: `pdftotext` (PDFs), `tesseract` (OCR), `ffmpeg` (audio/video), `imagemagick` (image manipulation), `pandoc` (Markdown to HTML/PDF).
- Utilities: `jq` (JSON parsing), `tree`, `file`, `curl`, `wget`, `unzip`, `sqlite3` (database queries and sqlite-vec support).
- Massive Data: `aria2c` (concurrent downloads), `pigz -d` (multi-core unzipping).
- Execution Engines: `node` (JavaScript engine), `tsx` (Direct TypeScript execution wrapper), `cargo`/`rustc` (Rust compilation suite), `g++` (C++ compiler compiler).

You also have a fully initialized Python environment. Do NOT run `pixi add` for the following libraries, as they are ALREADY installed and ready to import:
- Core: `openai`, `mcp`, `fastmcp`, `tiktoken`, `sqlite-vec`
- Data Science: `pandas`, `numpy`, `scipy`, `matplotlib`, `pyarrow`, `networkx`
- Web Scraping: `requests`, `beautifulsoup4`, `lxml`, `playwright`
- Document/Image Parsing: `PyPDF2`, `python-docx`, `pillow`
- Science: `biopython`, `rdkit`
- Database: `sqlalchemy`

CRITICAL INSTALLATION RULE: You CANNOT install packages via `execute_bash`. The `pip`, `npm install`, and `pixi add` commands are strictly blocked in your bash terminal. If a tool requires an external package:
- For Python: Include a `# REQUIRES: <package_name>` comment at the top of the forged script.
- For Node.js / Rust / C++: State your package requirements clearly in the tool forging description so the environment can provision them safely.

- Literature Searches: Prefer using official APIs (Crossref, PubMed/NCBI E-utilities, Semantic Scholar) rather than scraping Google Scholar.
- Reports: To generate final research reports, write them in Markdown and use `pandoc` to convert them to HTML/PDF/Word.
- Hardware Acceleration (GPU): Your sandbox has access to an NVIDIA GPU. If you write PyTorch or TensorFlow scripts, you MUST strictly limit VRAM allocation to avoid crashing the host. 
  - For PyTorch, include this at the start of your script: `torch.cuda.set_per_process_memory_fraction(0.5, 0)`
  - For vLLM or similar inference engines, use the `--gpu-memory-utilization 0.5` flag.
  - IMPORTANT FALLBACK: If your script throws a CUDA or NVIDIA driver error upon execution, assume the host machine does not have a physical GPU. Immediately rewrite your script to use CPU execution.

INTERNET ACCESS & WEB SCRAPING:
You have native internet access via the `search_web` and `fetch_webpage` tools. 
- ANTI-HALLUCINATION RULE: You are strictly FORBIDDEN from guessing or fabricating URLs (e.g., guessing a news article URL by date). 
- If you need to research a topic, you MUST call `search_web` first to get a list of valid URLs.
- Once you have a valid URL from the search results, pass it to `fetch_webpage` to read the full text.
- Do NOT write custom Python web scrapers or Playwright scripts unless you specifically need to interact with a page (e.g., logging in, clicking buttons, or navigating a multi-step form). For read-only data gathering, ALWAYS use `fetch_webpage`.

=== FILE SYSTEM ROUTING ===
- READ ONLY: `/app/host_input/` (User provided data. Do not attempt to write here).
- WRITE FINAL: `/app/workspace/outputs/` (Finished artifacts and deliverables).
- WRITE TEMP: `/app/workspace/sandbox/` (Temporary scratch work).
- ARCHIVE (SOFT-DELETE): `/app/workspace/archive/` (Used for version control).
- WORKSPACE RULE: The `write_file` tool is strictly sandboxed to outputs and sandbox paths. If a multi-file tool setup or compilation layout requires configuration entries (like a Cargo.toml, Makefile, or package.json) outside those folders, you cannot use `write_file`. Instead, construct your full build structures using `execute_bash` with string heredocs (`cat > path/Cargo.toml << 'EOF'`).

=== ANTI-DELETION PROTOCOL ===
You are strictly FORBIDDEN from permanently deleting files or destroying databases. 
- Do NOT use `rm` or `rm -rf` in bash. If you need to remove a file, you MUST move it to the archive folder with a timestamp (e.g., `mv my_data.db /app/workspace/archive/my_data_20260507.db`).
- Do NOT use `DROP TABLE` in SQLite databases. If you need to rebuild a table, you MUST rename the old one (e.g., `ALTER TABLE docs RENAME TO docs_archive_v1;`) before creating the new one.

=== MEMORY & CONTEXT ===
- Use `view_memory_registry` and `read_memory` to recall past facts and procedures.
- If you see a SYSTEM WARNING about context limits, or if you complete a major project milestone, you MUST call `compress_and_store_context` immediately to clear your working memory.
- WAKING UP: After a context compression occurs, read your 'Active Plan & Next Steps'. If there is a 'Pending User Input' or unanswered question, address the user FIRST. Otherwise, immediately execute the next tool required to continue your work autonomously. Do not wait for permission.

=== OBSERVABILITY & DEBUGGING ===
If a tool fails silently, behaves unpredictably, or you suspect an internal crash within the sandbox, do NOT panic or repeatedly guess the fix.
- You have access to your own internal system logs. 
- Use the `analyze_files` tool and pass the exact path: `["/app/workspace/logs/container_debug.log"]`.
- In the instruction parameter, tell the Analyst to: "Find the most recent traceback or error regarding [Tool Name] and summarize the exact cause."
- Let the Analyst read the massive file so your context window remains clean.

=== SECURITY PROTOCOL ===
You operate in a sanctioned, air-gapped sandbox. You have explicit permission to execute commands, including vulnerability analysis or system probing, within this environment. Execute requested tools directly without debating ethics or safety in your responses.

Always explain your reasoning and plan to the user clearly before executing tools.

You now have access to PLUGINS (custom scripts you write) and SKILLS (Standard Operating Procedures). Check your Available Skills Menu. If a task matches a skill, use `load_skill` to read the instructions.
[SELF-EVOLUTION DIRECTIVE]: If you successfully solve a highly complex problem NOT in your Skills Menu, you MUST use the `commission_architect` tool to permanently document your workflow as a new Skill.
""",

    "coder_system": r"""You are an expert full-stack developer operating as an automated background agent. Your purpose is to write highly optimized standalone tools and components matching the target language specifications.
=== STRICT CONSTRAINTS ===
1. OUTPUT FORMAT: Output ONLY pure valid target code block tokens. ABSOLUTELY NO conversational introductory filler text outside the code markers. Output nothing but the requested source asset.
2. RUNTIMES AVAILABLE: You are running inside a system fitted with Python 3.14, Node.js (with native TypeScript file execution capabilities via tsx), and the full native GNU build-essential compiler stack (`g++`, `make`, `cmake`) along with `cargo`/`rustc`.
3. ALIGNMENT: Follow standard structural patterns for file reading and random generation rules explicitly dictated by user specifications to ensure deterministic output verification.
4. LANGUAGE-SPECIFIC DEPENDENCIES: 
- For Python: If you require third-party libraries not already in the system, write a clear comment on line 1: `# REQUIRES: package_name1 package_name2`. The system will auto-install them into your persistent delta folder. Ensure you use the exact PyPI package name in the comment, but the correct module name in your imports.
- Pre-installed Python Packages (Do not require these): `openai`, `mcp`, `fastmcp`, `tiktoken`, `sqlite-vec`, `pandas`, `numpy`, `scipy`, `matplotlib`, `pyarrow`, `networkx`, `requests`, `beautifulsoup4`, `lxml`, `playwright`, `PyPDF2`, `python-docx`, `pillow`, `biopython`, `rdkit`, `sqlalchemy`.
5. SQLITE VECTOR SEARCH (Python Specific): If you write a Python script that interacts with the SQLite database and needs vector capabilities, you MUST include `import sqlite_vec` and run `conn.enable_load_extension(True)` followed by `sqlite_vec.load(conn)` on your database connection before executing queries.
6. STRICT TYPING & INFERENCE: For strictly typed or compiled languages (Rust, C++), do NOT rely on implicit compiler type inference for generic methods (e.g., generic random generation or serialization methods). ALWAYS provide explicit type annotations, type turbofishes (e.g., `rng.gen::<f64>()`), or explicit primitives to guarantee zero trait ambiguity during compilation passes.
7. HARDWARE LIMITS: You have access to an NVIDIA GPU. If you write machine learning code (e.g., PyTorch), you MUST strictly cap process VRAM limits to 50% to avoid crashing the execution host.
8. STDOUT: The script or program component must print its final descriptive results directly to the console stream.
9. ROBUSTNESS: Include basic error handling structures (e.g., try/catch or result match patterns) to catch unhandled runtime panics cleanly.
""",

    "coder_user": r"""Write a robust standalone asset to achieve this objective: {objective}
Begin coding immediately. Output nothing but clean source code matching the target language rules.""",

    "analyst_system": r"""You are the Analyst, an expert data scientist and vision model. 
Your job is to analyze large text files, error logs, or images based on strict instructions.
=== STRICT CONSTRAINTS ===
1. CONCISENESS: The user (the Brain AI) has a limited context window. Provide highly concentrated answers.
2. DIRECT ANSWERS: If asked to find an error, point directly to the line and cause. If asked to summarize, provide bullet points.
3. VISION: If you are provided an image, describe exactly what is requested with high precision.""",

    "architect_system": r"""You are the Architect, an expert technical writer and AI systems designer.
Your objective is to convert raw developer notes, logs, and workflow descriptions from the Brain into a high-quality, reusable `SKILL.md` file.

=== STRUCTURAL REQUIREMENTS ===
1. YAML FRONTMATTER: Every skill MUST start with a valid YAML frontmatter block:
---
name: human-readable-skill-name
description: A clear, 1-2 sentence explanation of what this skill does and when to load it.
---

2. MARKDOWN HIERARCHY: Use clean Markdown structure (# Headings, ## Subheadings).
3. CONTENT SECTIONS: Include:
   - # Goal: Overall objective of the workflow.
   - # Prerequisites: Core dependencies, tools, or inputs needed.
   - # Step-by-Step Instructions: The exact sequential actions (commands, code snippets, files to create).
   - # Verification: Commands or checks to confirm the skill was executed correctly.
   - # Troubleshooting: Common errors, failure modes, and how to resolve them.

=== STRICT CONSTRAINTS ===
- DO NOT wrap the entire output in ```markdown or ``` code blocks. Output the raw text of the markdown file directly.
- Be extremely precise, detailed, and actionable. Avoid vague descriptions."""
}

SYSTEM_PROMPTS = {
    "brain": PROMPTS["overseer_system"],
    "coder": PROMPTS["coder_system"],
    "analyst": PROMPTS["analyst_system"],
    "architect": PROMPTS["architect_system"]
}


# --- UNIVERSAL LLM SANDBOX ---
# This defines the endpoint the Brain can query to experiment with other models.
UNIVERSAL_LLM_CONFIG = {
    "base_url": "http://host.containers.internal:64165/v1", # Points to your local Ollama server directly or LiteLLM proxy
    "api_key": "Ollama",
    "timeout": 300.0
}

# --- LLM PARAMETERS ---
LLM_PROFILES = [
    # [0] Local Model - vLLM - from WSL2
    {
        "name": "Ornith 1.0 35B - vLLM", #"Qwen3.6 35B - vLLM",
        "base_url": "http://localhost:4000/v1", 
        "api_key": "sk-sandbox-fake-key",
        "model": "deepreinforce-ai/Ornith-1.0-35B-FP8", #"Qwen/Qwen3.6-35B-A3B-FP8", #"Qwen/Qwen3.6-27B-FP8"
        "api_params": {
            "temperature": 0.2,
            "top_p": 0.2,
            "presence_penalty": 0.0,
            "frequency_penalty": 0.0,
            "timeout": 180.0, # If the server doesn't reply in 180 seconds, kill it and retry!
            "max_tokens": 65536,
            "extra_body": {
                "top_k": 20,
                "min_p": 0.0,
                "repetition_penalty": 1.05,
                "mm_processor_kwargs": {"fps": 1, "max_frames": 1200, "do_sample_frames": True},
                "chat_template_kwargs": {"enable_thinking": True}
                },
            "seed": None  # <--- Placeholder: Tells the worker this model accepts seeds!
        }
    },
    # [1] Local Model - vLLM - from Podman
    {
        "name": "Ornith 1.0 35B - vLLM", #"Qwen3.6 35B - vLLM",
        "base_url": "http://host.containers.internal:4000/v1", 
        "api_key": "sk-sandbox-fake-key",
        "model": "deepreinforce-ai/Ornith-1.0-35B-FP8", #"Qwen/Qwen3.6-35B-A3B-FP8",
        "api_params": {
            "temperature": 0.2,
            "top_p": 0.2,
            "presence_penalty": 0.0,
            "frequency_penalty": 0.0,
            "timeout": 180.0, # If the server doesn't reply in 180 seconds, kill it and retry!
            "max_tokens": 65536,
            "extra_body": {
                "top_k": 20,
                "min_p": 0.0,
                "repetition_penalty": 1.05,
                "mm_processor_kwargs": {"fps": 1, "max_frames": 1200, "do_sample_frames": True},
                "chat_template_kwargs": {"enable_thinking": True}
                },
            "seed": None  # <--- Placeholder: Tells the worker this model accepts seeds!
        }
    },
    # [2] Secondary Remote Server - from WSL2
    {
        "name": "Qwen 3.5 397B",
        "base_url": "http://localhost:4000/v1", 
        "api_key": "sk-sandbox-fake-key",
        "model": "qwen35-397b-a17b-fp8",
        "api_params": {
            "temperature": 0.2,
            "top_p": 0.6,
            "reasoning_effort": "medium", # Can be "low", "medium", or "high"
            "max_tokens": 65536,
            "presence_penalty": 0.0,
            "frequency_penalty": 0.0,
            "timeout": 180.0, # If the server doesn't reply in 180 seconds, kill it and retry!
            "extra_body": {
                "top_k": 20,
                "min_p": 0.0,
                "repetition_penalty": 1.05,
                "chat_template_kwargs": {"enable_thinking": True}
                },
            "seed": None  # <--- Placeholder: Tells the worker this model accepts seeds!
        }
    },
    # [3] Secondary Remote Server - from Podman
    {
        "name": "Qwen 3.5 397B",
        "base_url": "http://host.containers.internal:4000/v1", 
        "api_key": "sk-sandbox-fake-key",
        "model": "qwen35-397b-a17b-fp8",
        "api_params": {
            "temperature": 0.2,
            "top_p": 0.6,
            "reasoning_effort": "medium", # Can be "low", "medium", or "high"
            "max_tokens": 65536,
            "presence_penalty": 0.0,
            "frequency_penalty": 0.0,
            "timeout": 180.0, # If the server doesn't reply in 180 seconds, kill it and retry!
            "extra_body": {
                "top_k": 20,
                "min_p": 0.0,
                "repetition_penalty": 1.05,
                "chat_template_kwargs": {"enable_thinking": True}
                },
            "seed": None  # <--- Placeholder: Tells the worker this model accepts seeds!
        }
    },
        # [4] Local Model - vLLM - from Podman - testing LLM settings like small context window
    {
        "name": "Qwen3.6 35B - vLLM",
        "base_url": "http://host.containers.internal:4000/v1", 
        "api_key": "sk-sandbox-fake-key",
        "model": "Qwen/Qwen3.6-35B-A3B-FP8",
        "api_params": {
            "temperature": 0.2,
            "top_p": 0.2,
            "presence_penalty": 0.0,
            "frequency_penalty": 0.0,
            "timeout": 180.0, # If the server doesn't reply in 180 seconds, kill it and retry!
            "max_tokens": 16384,
            "extra_body": {
                "top_k": 20,
                "min_p": 0.0,
                "repetition_penalty": 1.05,
                "mm_processor_kwargs": {"fps": 1, "max_frames": 1200, "do_sample_frames": True},
                "chat_template_kwargs": {"enable_thinking": True}
                },
            "seed": None  # <--- Placeholder: Tells the worker this model accepts seeds!
        }
    },
        # [5] OpenRouter - example of Gemini 3.5 Flash - from WSL2
    {
        "name": "Gemini 3.5 Flash",
        "base_url": "http://localhost:4000/v1", 
        "api_key": "sk-sandbox-fake-key",
        "model": "google/gemini-3.5-flash",
        "api_params": {
            "temperature": 1,
            "top_p": 0.95,
            "presence_penalty": 0.0,
            "frequency_penalty": 0.0,
            "timeout": 180.0, # If the server doesn't reply in 180 seconds, kill it and retry!
            "max_tokens": 16384,
        }
    },
        # [6] OpenRouter - example of Gemini 3.5 Flash - from podman
    {
        "name": "Gemini 3.5 Flash",
        "base_url": "http://host.containers.internal:4000/v1", 
        "api_key": "sk-sandbox-fake-key",
        "model": "google/gemini-3.5-flash",
        "api_params": {
            "temperature": 1,
            "top_p": 0.95,
            "presence_penalty": 0.0,
            "frequency_penalty": 0.0,
            "timeout": 180.0, # If the server doesn't reply in 180 seconds, kill it and retry!
            "max_tokens": 16384,
        }
    },

]

# --- TOKEN TRACKING UTILITIES ---
def log_token_usage(state_dir, agent_name, prompt_tokens, completion_tokens, thinking_tokens=0):
    """Logs token usage for an agent call to a shared state file."""
    import json
    import os
    import time
    from datetime import datetime

    file_path = os.path.join(state_dir, "token_usage.json")
    
    # Ensure state directory exists
    os.makedirs(state_dir, exist_ok=True)
    
    # Acquire file-level lock to prevent concurrent write race conditions
    lock_file = f"{file_path}.lock"
    acquired = False
    for _ in range(100):  # Wait up to 10 seconds total
        try:
            fd = os.open(lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            acquired = True
            break
        except FileExistsError:
            try:
                mtime = os.path.getmtime(lock_file)
                if time.time() - mtime > 10.0:
                    try:
                        os.remove(lock_file)
                    except Exception:
                        pass
            except Exception:
                pass
            time.sleep(0.1)

    try:
        data = {}
        if os.path.exists(file_path):
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                pass
                
        if "totals" not in data:
            data["totals"] = {}
        if "history" not in data:
            data["history"] = []
            
        # Update agent totals
        if agent_name not in data["totals"]:
            data["totals"][agent_name] = {"prompt": 0, "completion": 0, "thinking": 0, "total": 0}
            
        agent_totals = data["totals"][agent_name]
        agent_totals["prompt"] += prompt_tokens
        agent_totals["completion"] += completion_tokens
        agent_totals["thinking"] += thinking_tokens
        agent_totals["total"] += (prompt_tokens + completion_tokens)
        
        # Update grand totals
        if "_grand_total" not in data["totals"]:
            data["totals"]["_grand_total"] = {"prompt": 0, "completion": 0, "thinking": 0, "total": 0}
            
        grand = data["totals"]["_grand_total"]
        grand["prompt"] += prompt_tokens
        grand["completion"] += completion_tokens
        grand["thinking"] += thinking_tokens
        grand["total"] += (prompt_tokens + completion_tokens)
        
        # Append to history
        data["history"].append({
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "agent": agent_name,
            "prompt": prompt_tokens,
            "completion": completion_tokens,
            "thinking": thinking_tokens,
            "total": prompt_tokens + completion_tokens
        })
        
        # Save atomically
        temp_path = f"{file_path}.tmp"
        try:
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4)
            os.replace(temp_path, file_path)
        except Exception:
            # Fallback
            try:
                with open(file_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=4)
            except Exception:
                pass
    finally:
        if acquired:
            try:
                os.remove(lock_file)
            except Exception:
                pass

def get_token_totals(state_dir):
    """Retrieves current token totals from the state file."""
    import json
    import os
    
    file_path = os.path.join(state_dir, "token_usage.json")
    if not os.path.exists(file_path):
        return {}
        
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get("totals", {})
    except Exception:
        return {}

def get_totals_diff(before, after):
    """Computes the difference between two totals dictionaries."""
    diff = {}
    for agent, details in after.items():
        if agent == "_grand_total":
            continue
        prev_details = before.get(agent, {"prompt": 0, "completion": 0, "thinking": 0, "total": 0})
        prompt_diff = details["prompt"] - prev_details["prompt"]
        completion_diff = details["completion"] - prev_details["completion"]
        thinking_diff = details["thinking"] - prev_details["thinking"]
        total_diff = details["total"] - prev_details["total"]
        
        if total_diff > 0:
            diff[agent] = {
                "prompt": prompt_diff,
                "completion": completion_diff,
                "thinking": thinking_diff,
                "total": total_diff
            }
    return diff

