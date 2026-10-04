# Small local tool-calling CLI

## Start here (five-minute version)

This program lets a local Ollama model inspect files, run commands, create output,
pause safely, and resume later. Its private history is stored in `.local-agent`
inside the selected workspace.

### 1. Start Ollama

```powershell
ollama serve
ollama list
```

### 2. Start the agent

```powershell
cd D:\LLR_Gen_Full_Tool\code\code
python main.py --workspace "D:\testing_llr\code" --instructions PLAN.md --instructions SKILL.md
```

On the first interactive launch, OAT asks you to choose an installed Ollama
model, default workspace, context size, timeout, and compact/detailed interface.
Preferences are stored outside the repository in `%LOCALAPPDATA%\OAT\settings.json`
on Windows. Run `python main.py --setup` or enter `/setup` to change them later.

### 3. Enter a request

```text
Execute the complete workflow in PLAN.md using SKILL.md.
```

While it works, `/plan` shows progress and `/stop` pauses safely. Later, `/resume`
continues saved work. Normal use does not require editing JSON or remembering IDs.

### Essential commands

| Command | What it does |
| --- | --- |
| `/help` | Shows available commands |
| `/menu` | Shows the compact interactive command menu |
| `/settings` | Shows model, workspace, context and timeout |
| `/setup` | Reruns the setup wizard and saves new defaults |
| `/ui compact` | Shows concise task progress without model-step noise |
| `/ui detailed` | Shows detailed runtime events |
| `/models` | Lists every model installed in local Ollama and marks the current one |
| `/model NUMBER` | Switches to a model by its `/models` number |
| `/model NAME` | Switches to an installed model by its exact tag |
| `/context 32768` | Changes context size for this session |
| `/timeout 1800` | Changes Ollama timeout for this session |
| `/plan` | Shows the active saved task |
| `/stop` | Pauses active work safely |
| `/history` | Lists earlier requests |
| `/history completed` | Filters history by completion status |
| `/history failed` | Shows blocked or incomplete work |
| `/history search WORDS` | Searches original requests |
| `/history show 2` | Shows complete metadata for history item 2 |
| `/history artifacts 2` | Shows files and commands recorded for item 2 |
| `/history delete 2` | Deletes a saved task record |
| `/review 2` | Reviews files, hashes, verification, and commands for item 2 |
| `/resume 1` | Continues history item 1 |
| `/rerun 1` | Runs history item 1 again from the beginning |
| `/workspace D:\project` | Switches to another existing folder |
| `/clear logs` | Deletes old logs, keeping the active log |
| `/clear tasks` | Deletes old tasks, keeping the active task |
| `/clear all` | Deletes old logs and tasks, keeping active records |
| `/trace off` | Hides large model/tool trace blocks |
| `/tokens` | Shows prompt, generated, cached, schema, system, and tool-result usage |
| `/profiles` | Shows persisted Ollama capability profiles |
| `/profiles test-all` | Tests every installed model that has not been profiled |
| `/profiles refresh MODEL` | Re-tests one installed model |
| `/retry` | Retries the latest incomplete task from durable state |
| `/retry --short-context` | Halves context and retries saved work |
| `/retry --model NAME` | Switches models and resumes the latest task |
| `/exit` | Exits the program |

`/workspace` changes the working folder, but it is not an operating-system
security sandbox. Commands still run with your Windows account's permissions.

## Context and timeout on local hardware

Bundled defaults:

- context: `32768` tokens
- maximum reply: `4096` tokens
- Ollama timeout: `1800` seconds (30 minutes)

A model showing 131K context is advertising a maximum supported ceiling. It does
not mean 131K will run comfortably on every machine. Context consumes additional
RAM/VRAM and long prompts take longer to evaluate. For a 20B model on a 16 GB
computer, 32K is a much safer default. If it is stable, try 64K; if Ollama swaps,
exits, or becomes extremely slow, lower it again. A longer timeout helps a slow but
healthy model, but it cannot solve an out-of-memory condition.

Relevant `config.json` settings:

```json
"timeout_seconds": 1800,
"max_context_chars": 100000,
"options": {"temperature": 0, "num_ctx": 32768, "num_predict": 4096}
```

Long tasks checkpoint before approaching the token budget. Each ordinary prompt
starts with clean conversational context so a previous failed task does not make
the next one slower. `/resume` reconstructs work from the persisted plan and
evidence.

## Lightweight prompt architecture

OAT minimizes repeated local inference work:

- Only tools relevant to the current workflow action are sent to Ollama. Direct
  requests use intent-based filtering with a safe fallback for ambiguous prompts.
- The complete planning prompt is sent only while a PLAN workflow is being
  compiled. Execution uses a short stable prompt plus routed instruction sections.
- Full tool results remain in local evidence files. Ollama receives compact file,
  command, and task-state results with explicit pagination when content is large.
- Completed workflow actions checkpoint immediately. Long direct tasks compact
  automatically as context pressure grows and recover older results by evidence ID.
- `/tokens` exposes the actual token totals and the accumulated character cost of
  tool schemas, system instructions, and tool results so improvements are measurable.

## Automatic model profiles

The first interactive use of an installed model runs a small capability probe and
saves the result beside user preferences. Each model is tested once unless you
explicitly refresh it. Profiles record native-tool reliability, JSON and thinking
support, tested context, recommended context/timeout, generation speed, and probe
errors. Selecting an untested model profiles it before switching. Use
`/profiles test-all` to proactively test every installed model.

Profiles let OAT avoid unsupported thinking, prefer JSON for unreliable native
tools, and choose conservative local settings. Probes are deliberately short and
are never run during a noninteractive one-shot command.

## Progress and recovery

Workflow progress reports completed/total actions, elapsed time, the current
action, and an estimate range once enough completed work exists. Direct tasks show
recorded action count and elapsed time without inventing a completion percentage.

Failures are classified as context, transport, timeout, model availability,
resource pressure, or server failure. One transient transport retry is automatic.
Context overflow triggers one durable checkpoint retry. Unsupported thinking is
disabled automatically. If recovery still needs user action, completed evidence
is preserved for `/retry`, `/retry --short-context`, or `/retry --model NAME`.

Python + Ollama + a local model. Type a task; the model reads files, calls tools,
examines results and continues until it can answer. There is no hard-coded Ada,
C, LLR, revision-history or other domain workflow. Your prompt and optional
instruction files define the work.

The implementation uses **Python's standard library only**. No pip dependencies,
cloud API keys, automatic model downloads, or external agent framework.

## Run on Windows

Requires Python 3.11+ and a running Ollama server with the model installed.

```powershell
cd D:\LLR_Gen_Full_Tool\code\code
python main.py --doctor
python main.py
```

The default model is `gpt-oss:20b`. Check `ollama list` to see installed tags.
Start `ollama serve` if the server is not running. Model installation is separate
from this application; on an online provisioning machine the command would be
`ollama pull gpt-oss:20b`.

Example CLI prompts:

```text
Find design.md under D:\Documents\Project and summarize it.
Read D:\Work\module.c and explain its control flow.
Create D:\Notes\result.txt containing: Review completed.
In D:\Notes\result.txt replace Review completed with Review pending.
Run this PowerShell command and show its output: Get-Date
Run this Bash command and show its output: printf 'hello\n'
Run python tools/check.py, read its output, and summarize any failures.
Read PLAN.md and SKILL.md and carry out the workflow they describe.
```

The workspace defaults to the directory you launch the CLI from (`workspace: null`
in config.json). Launching from the repository root therefore resolves `README.md`
to the root `README.md`. Use `--workspace D:\AnotherProject` to choose a
different base explicitly; a non-null workspace in a custom config also overrides
the launch-directory default.

Full terminal tracing is off by default to keep normal use readable. When enabled,
before each network request you see the
complete prompt/conversation, model settings and tool schemas sent to Ollama.
After the response arrives you see the model response, followed by tool arguments
and results/errors. The next request shows precisely how that result was sent back.
Multiline text and Windows paths are displayed without JSON escaping. The JSONL
log also retains the actual request payload. Nothing is shortened in full trace.

Use `python main.py --no-trace`, `trace: false` in config.json, or `/trace off` in
the session for compact output; `/trace on` enables it again. Ollama replies are
currently displayed when each HTTP response completes, not streamed token by token.
The main thread remains interruptible while the network worker waits, so Ctrl+C
can exit without waiting for Ollama to finish. Exiting does not stop the Ollama server.

Each ordinary prompt starts a clean task conversation. While a task is running on
Windows, type `/stop` and Enter to pause it safely, or `/plan` to inspect
the persisted progress. `/stop` ignores the outstanding Ollama reply and prevents
the next tool action; an already-running atomic command is allowed to finish so its
outcome can be recorded. Continue with `/resume` or start a different prompt.
`/tools` lists enabled tools; `/reset` clears in-memory conversation state;
`/help` shows help; `/exit` quits.

One-shot and multiline prompts:

```powershell
python main.py -p "Find README.md and summarize it"
python main.py --prompt-file D:\Tasks\request.txt
python main.py --workspace D:\AnotherProject -p "Read main.py and explain it"
```

## Eight basic tools

| Tool | Capability |
| --- | --- |
| `list_files` | List a directory |
| `find_files` | Find filenames/wildcards, optionally recursively |
| `glob_files` | Scan the sandbox recursively with patterns such as `**/*.py` |
| `read_file` | Read UTF-8 text/code in chunks |
| `search_text` | Locate literal text and return line numbers |
| `write_file` | Create a text file or explicitly replace it |
| `edit_file` | Replace exact matching text, rejecting ambiguous matches |
| `run_command` | Execute PowerShell, cmd or Bash and return stdout/stderr/exit code |

Files can be `.md`, `.txt`, `.c`, `.h`, `.py`, `.ada`, `.json` or other UTF-8 text.
No PDF/Word parsing is included. There are no document-format dependencies.
Large reads use model-visible chunks (`read_chunk_chars`, 12,000 characters by
default) and return `next_offset` rather than pretending the whole file was read.
For planned full-file work, OAT enforces that exact continuation offset and will
not accept gaps, overlaps, or a truncated final read as completion evidence.
Glob results are sorted workspace-relative paths and expose `next_offset` for
large sandboxes. `glob_files` is always confined to the active workspace: it
rejects absolute/traversal paths, skips symlinks, and never exposes `.git` or
`.local-agent`. Hidden files are included by default and can be excluded.

Repository-wide requests automatically use a durable plan instead of the fast
single-action path. When a request names root files and another directory such
as `src`, OAT inventories those levels separately and writes one visible plan
row per discovered file. Each row is locked to its named file, truncated reads
cannot complete it, and the next file remains unavailable until the current
summary is saved. Tools hidden from the current action are rejected before
execution even if a model hallucinates their names.
Text edits preserve existing CRLF line endings. Writes are atomic replacements.
Non-UTF-8 files produce an encoding error; convert them or use an explicitly
requested command with the appropriate encoding.

Relative file paths and command working directories use the selected workspace.
Absolute paths are supported, including outside that workspace. Existing files
require an explicit overwrite flag in the model's tool call; partial edits use
an exact-match tool. The agent is instructed to overwrite only as part of the
user's task. There is no undo stack or automatic file backup.

## Automatic runtime task tracking

Every request gets its own saved task record. Ordinary prompts use **direct
mode**: the model calls normal file/command tools immediately while the host
automatically journals successful actions and file hashes. This avoids making a
small local model serialize a second, nested planning language before it can do
simple work.

When selected PLAN-style instructions define numbered workflow requirements, the
runtime uses **workflow mode** instead. The model proposes one-time actions and a
single level of repeated action templates, and the host rejects missing coverage.
For example: setup once, discover files, read/write/check each file, then finalize
once. This is local task tracking, not Git pull requests.

The host controls completion and stores the record at
`<workspace>/.local-agent/tasks/<task-id>/plan.md` (readable checklist),
`plan.json` (state), and `evidence/` (actual tool results). Its management tools
are `plan_task`, `task_status`, `task_evidence`, `expand_task`,
`complete_task_step`, and `report_blocker`, alongside the seven execution tools.
Planning controls are hidden from the model in direct mode.

- Repeated items are expanded from actual file/command results, not a fabricated list.
- Foreach expansion preflights item-dependent read paths, rejecting command-output
  banners or status lines before invalid children are persisted.
- Steps close in order with recorded evidence; file checks inspect actual content
  and hashes. Failed commands cannot count as successful completion.
- A plain-text document is not a file write. A premature final answer is returned
  to the model for correction while planned work remains.
- Identical successful mutations/commands within a step are not blindly replayed.
- Repeated errors, lack of step progress, or execution limits stop the task as
  **incomplete**, with its progress saved. No infinite retry loop.
- Longer conversations checkpoint to persisted state; earlier tool results remain
  retrievable by evidence ID. This is structured task memory, not private reasoning.

Use `/status` to inspect progress. `/history` lists prior requests newest-first with
a number, model, artifact count, and full task ID, so forgotten work can be
rediscovered. `/history show 2` shows that entry's complete original prompt,
status, timestamp, and saved plan; `/history search revision report` filters
requests containing those words. `/review 2` reconstructs its files, verification
hashes, and command exit codes from durable evidence. `/resume 2`
(or `/resume TASK_ID`) continues that task
without repeating completed steps. `/rerun 2` starts its original request again
as a new task with a fresh plan and evidence, which is appropriate for completed
tasks or when inputs have changed.
From a new terminal, keep the same workspace and instruction files:

```powershell
python main.py --workspace "D:\testing_llr\code" --instructions PLAN.md --instructions SKILL.md
python main.py --workspace "D:\testing_llr\code" --instructions PLAN.md --instructions SKILL.md --resume
```

`--resume` chooses the latest task; `--resume TASK_ID` selects one explicitly.
Completed steps remain closed. If an interrupted command/write has an unknown
outcome, automatic resume is refused: inspect the evidence and real files first,
then start a new task describing the observed state. Ctrl+C does not undo writes.
If a file created by `write_file` is missing when an incomplete task resumes, the
runtime restores its exact content from the persisted evidence and reports it in
`recovered_artifacts`. Existing or externally modified files are never overwritten.

These safeguards verify the **declared plan**, not perfect understanding of an
arbitrary request. The model can still omit a requirement or produce incorrect
content. Review important plans and outputs. Plans cannot be replaced to silently
discard unfinished work; unexpected requirements may require a new task.

## Change the task through instructions

PLAN.md and SKILL.md are ordinary instruction files; their names and contents
have no special engine behavior. Load whichever files your task needs:

```powershell
python main.py --instructions PLAN.md --instructions SKILL.md
```

For workflows where omission would be costly, use this lightweight contract
format in the selected plan file:

```markdown
## Workflow
### 1. One-time setup
...
### 2. Select an item
...
### 3. Process the item
...
Return to Step 2.
```

Numbered `###` headings become required coverage IDs in the saved runtime plan.
`Return to Step N` marks that step and every later numbered step as repeated, so
the host requires them inside a `foreach`. Markdown headings inside an output
template do not interfere. This is deliberately small: the prose remains useful
to humans while the runtime gains a deterministic completeness gate.
Exact workflow titles also infer their coverage IDs automatically. Volatile
document content accidentally included in plan checks is removed, while the real
execution call and resulting file remain evidence-checked.

Or set this in config.json:

```json
"instruction_files": ["PLAN.md", "SKILL.md"]
```

Alternatively, simply tell the CLI to read and follow a file. The default
`instruction_files` is empty so unrelated tasks do not inherit a workflow.
An optional workspace `agent.md` or `AGENTS.md` is discovered automatically;
`agent.md` takes precedence. `--agent-file PATH` selects another file.
`agent.example.md` is a starting example. No agent file is required.

Configured instructions are loaded when the CLI starts. Restart after editing
them, or explicitly tell the current session to read the updated instruction
file. No Python changes are required when you switch language or task. Scripts
mentioned in a workflow can be executed using `run_command`, just like other
requested PowerShell/Python commands.

Explicit relative paths inside config.json resolve from the config's directory for
workspace/system_prompt, and from the workspace for instruction files/agent_file.
Relative `--workspace` paths resolve from the current terminal directory.

## Models and robust tool-call parsing

```powershell
python main.py --model gpt-oss:20b
python main.py --model devstral:latest --think default --tool-mode json
```

Use the exact model tag from your local `ollama list`. The Devstral tag above is
an example. The bundled config uses GPT-OSS `think: low`; `--think default`
omits this model-specific option for models that do not support it.

Tool modes:

- `auto` (default): native Ollama tool calls. Malformed native calls receive
  bounded native correction attempts; only a genuine unsupported-tools server
  response switches to JSON mode.
- `native`: native API tool calling without that API-error fallback.
- `json`: tool schemas in the prompt plus Ollama `format: "json"`; calls are returned as JSON. Useful
  when a model's installed Ollama template cannot emit native tool calls.
  Final answers use an `{"answer":"..."}` envelope, which the CLI unwraps.

Server-side tool parsing errors also receive bounded correction attempts in JSON
mode (and in explicitly selected native mode). Unsupported-tool errors trigger
the auto-mode fallback once; unrelated errors such as missing models or exhausted
memory are not blindly retried. No tool executes from an HTTP error response.

Every mode can normalize plain JSON, native tool calls, backtick JSON fences,
triple-apostrophe JSON fences (`'''json ... '''`), JSON-string arguments and
nested JSON strings. Malformed/ambiguous calls, unknown tools, missing/extra
arguments and wrong types are rejected before execution. The model receives
the error and can correct the call, up to `max_repairs`.

Known `tool.NAME` and `functions.NAME` namespace prefixes are normalized before
registry validation. The observed `tool.run_command` dispatcher is unwrapped
only when its arguments contain exactly `name` and `arguments`; the inner call
still must name an enabled tool and satisfy its schema. This never converts
arbitrary model text into a shell command. Raw replies and normalized calls both
remain in the log.

The parser never uses eval, never executes the model's thinking, and never
scrapes JSON examples embedded in prose. It does not guess missing arguments or
repair arbitrary broken syntax. This addresses known formatting failures without
claiming every model response can be recovered. Raw responses and errors remain
available in the run log for diagnosing new formats.

See the official [Ollama chat API](https://docs.ollama.com/api/chat) and
[tool-calling documentation](https://docs.ollama.com/capabilities/tool-calling).

## Commands and access

PowerShell is the default on Windows, using `pwsh` if available or Windows
PowerShell otherwise. Bash is optional: Git Bash is discovered in common install
paths or PATH. Set `bash_executable` if necessary:

```json
"bash_executable": "C:/Program Files/Git/bin/bash.exe"
```

Commands are noninteractive and run in the specified working directory. The tool
returns both output streams, the shell exit code, timeout status, and whether
captured output was truncated. Timeouts and Ctrl+C attempt to stop the command
process tree. Output drives subsequent tool decisions. There is no automatic
transport retry that silently reruns a shell command.

The CLI runs with your account's permissions. Shell commands can affect files
or networks your account can access; this is not an OS sandbox. To remove shell
execution, remove `run_command` from `enabled_tools`. `file_access: "workspace"`
restricts file tools to the workspace, but does not restrict a shell. Keep
`file_access: "any"` for the requested absolute-path behavior.

## Configuration, logs and offline setup

`config.json` controls model/server, workspace, instructions, enabled tools and
limits. Use `--config another.json` for a different setup. Relevant limits:
`max_steps`, `max_repairs`, `max_stalled_steps`, `max_file_chars`, `max_context_chars`,
`timeout_seconds`, `command_timeout_seconds`, and Ollama `options` such as
`num_ctx` and `num_predict`. Increase context/generation limits for larger jobs
or split the work into smaller tasks. More context can require more memory.

JSONL logs are written under `<workspace>/.local-agent/`. They contain loaded
instructions, prompts, raw replies (including thinking when returned), tool
arguments/results, corrections and errors. Logs can contain your source and
document contents. There is no telemetry; the client contacts only the configured
Ollama URL and ignores HTTP proxy environment variables. Requested commands can
make their own network calls.

Raw conversation history is in memory and the run log. Task plans and evidence
are persisted independently; resume reconstructs context from them. For a large
batch, increase `--max-steps` (model turns, not files) as appropriate. The default
is 80 turns; reaching it saves an incomplete task, not a successful result.

For air-gapped Windows, copy this folder and use your approved offline process
to install Python, Ollama and the model. Git/Git Bash are needed only for tasks
that use them. Set the workspace and model tag, then run `--doctor`. No pip
installation or internet connection is required by the application.

## Modular structure

```text
main.py                 Entry point
config.json             Runtime settings
prompts/                Replaceable system prompt
local_agent/
  cli.py                Interactive CLI, configuration and instruction loading
  config.py             Typed configuration
  agent.py              Generic model → tools → results loop
  tasks.py              Runtime plans, repeated steps, evidence and resume
  task_schema.py        Explicit nested plan/check schemas supplied to the model
  ollama.py             HTTP transport only
  protocol.py           Tool-call normalization only
  registry.py           Tool schemas, validation and dispatch
  file_tools.py         Text/code file operations
  command_tools.py      Shell execution
  workspace.py          Workspace and atomic writes
  storage.py            Logs and single-session workspace lock
  trace.py              Readable full request/response and tool tracing
tests/                  Offline tests and opt-in model integration test
```

For a new capability, implement a handler in its own module, register its schema
in the CLI, and add its name to `enabled_tools`. The agent loop does not need
task-specific branches. The registry validates top-level types, required/extra
arguments and enums; handlers validate detailed semantics. New nested argument
structures should have their contents validated in their handler.

## Troubleshooting in plain language

### “Ollama did not respond within … seconds”

The HTTP connection worked, but Ollama did not finish in time. Run `/settings`.
Try `/timeout 3600` if the model is simply slow. If Windows is heavily swapping or
Ollama uses all available memory, reduce context with `/context 16384` or close
other applications. Check the terminal running `ollama serve` for an out-of-memory
or model-runner error.

### A task stopped, but I want to continue

Run `/history`, find its number, then `/resume NUMBER`. Completed steps and tool
evidence are retained. `/rerun NUMBER` deliberately starts it again from scratch.

### An output created by the task is missing

Resume the incomplete task. Exact `write_file` content is retained in evidence and
missing task-created files are restored automatically. `/plan` lists restored paths
under `recovered_artifacts`. Existing files are never silently overwritten.

### The terminal prints too much JSON

Run `/trace off`. Trace is off by default in version 0.4.0, while complete JSONL
diagnostic logs remain available in `.local-agent`.

### I selected the wrong folder

Run `/workspace D:\correct\folder`. The current workspace lock is released and the
CLI reinitializes against the new folder. Instruction files passed as relative
paths must exist in that new workspace.

### I want to remove old private data

Use `/clear logs`, `/clear tasks`, or `/clear all`. These commands only touch old
records under the selected workspace's `.local-agent` directory and preserve the
active log/task. They do not delete project source files or generated output.

## Verification

```powershell
python -m unittest discover -s tests -v
python tests/live_smoke.py
python tests/live_smoke.py --tool-mode json
```

Offline tests cover parser formats/failures, schema validation, repair limits,
file reads/writes/edits, path settings, dynamic instruction files, command output,
shell exit codes and timeouts, runtime once/repeat/once workflows, premature
completion, invalid evidence, changed outputs, resume and no-progress limits.
Unavailable shell tests are skipped. The opt-in
live test uses a temporary workspace to ask the real model to read a note, execute
a PowerShell command, act on its output, and create a file. It retains the test
workspace and logs for inspection.
