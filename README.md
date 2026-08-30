# gemini-config

Portable configuration for Google Antigravity, Gemini CLI, and Gemini 2.5/3.0 agent workflows.

Includes global rules (`GEMINI.md`, `AGENTS.md`), portable skills (Caveman suite, Ponytail suite, A11y, MCP Builder, PDF, WebApp Testing), lifecycle hooks (`hooks.json`), and cross-platform installation scripts.

## Installation

### Windows (PowerShell)
```powershell
git clone https://github.com/danieltruong/gemini-config.git ~/gemini-config
& ~/gemini-config/install.ps1
```

### Linux / macOS
```bash
git clone https://github.com/danieltruong/gemini-config.git ~/gemini-config
bash ~/gemini-config/install.sh
```

## Structure

- **`GEMINI.md` / `AGENTS.md`**: Global rules enforcing Caveman terseness, Ponytail bias (YAGNI/stdlib), and Gemini model tiering.
- **`hooks.json` & `hooks/`**: Protojson lifecycle hooks for prompt reinforcement, commit message gating, denial circuit breaking, and docs linting.
- **`skills/`**: Standard YAML frontmatter skills for accessibility, PDF processing, MCP development, web app testing, and token optimization.
- **`agents/`**: Pre-configured subagent personas (researcher, coder, reviewer, tester, ui-designer).
- **`mcp_config.json`**: MCP server configurations (Playwright, local tools).
- **`scripts/`**: AI docs linter (`ai-docs-lint.py`), compressor (`caveman-compress.py`), and pre-push verification (`prepush.sh`).

## Verification

Run all test suites and doc linters:
```bash
python scripts/ai-docs-lint.py --all
python -m unittest discover -s hooks/tests
```
