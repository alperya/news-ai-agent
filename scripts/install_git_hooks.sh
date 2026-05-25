#!/usr/bin/env bash
# Install git pre-push hook so checks also run when pushing directly from terminal.
# Run once after cloning: bash scripts/install_git_hooks.sh

set -e
HOOK=".git/hooks/pre-push"

cat > "$HOOK" << 'EOF'
#!/usr/bin/env bash
# Pre-push guard: mirrors .claude/hooks/pre_push_check.py for terminal pushes.
REMOTE="$1"
URL="$2"

PYTHON=$(command -v python3 || command -v python)
if [ -z "$PYTHON" ]; then
  echo "⚠️  python3 not found, skipping pre-push checks."
  exit 0
fi

# Fake the JSON stdin that Claude Code hook normally provides
echo "{\"tool_input\": {\"command\": \"git push $REMOTE $URL\"}}" \
  | "$PYTHON" .claude/hooks/pre_push_check.py
EOF

chmod +x "$HOOK"
echo "✅ Git pre-push hook installed at $HOOK"
echo "   Checks will now run on every 'git push' from the terminal too."
