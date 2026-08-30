# Onboarding Guide for gemini-config

Follow these steps when setting up a new workstation or container environment:

## 1. Prerequisites
- Python 3.10+ on PATH
- Git configured with GitHub credentials
- Node.js (for Playwright MCP, optional)

## 2. Clone & Install
```bash
# Clone into home directory
git clone https://github.com/danieltruong/gemini-config.git ~/gemini-config

# Run installer (Windows)
pwsh -File ~/gemini-config/install.ps1

# Or run installer (POSIX)
bash ~/gemini-config/install.sh
```

## 3. Verify Installation
1. Run pre-push checks:
   ```bash
   python scripts/ai-docs-lint.py --all
   python -m unittest discover -s hooks/tests
   ```
2. Start an Antigravity / Gemini session and confirm:
   - Available skills list includes `caveman`, `ponytail`, `a11y-audit`, etc.
   - Global rules from `GEMINI.md` are active.
   - PreInvocation hooks reinforce terse communication.
