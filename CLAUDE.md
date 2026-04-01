# Claude Code — Project Instructions

## Branch Policy
Always commit and push directly to `main`. Never create feature branches or pull requests.

## Commit Style
- Commit message should be concise and describe what changed
- Always include CHANGELOG.md + README.md updates in the same commit (the post-commit hook handles this automatically via `scripts/hooks/post-commit`)

## Running Tests
```bash
source venv/bin/activate
python -m pytest tests/
```

## Python Environment
- Use the project venv at `venv/` (Python 3.12, Tk 9.0)
- Do not use system Python 3.9 — it has Tk 8.5 which is incompatible with customtkinter 5.2+
