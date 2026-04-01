#!/bin/bash
# check_ci.sh — Called by Claude Code PostToolUse hook after git push.
# Waits for GitHub Actions to complete and reports results back to Claude.
# If any workflow failed, dumps the error logs so Claude can fix them inline.

# Read the tool result JSON from stdin
TOOL_JSON=$(cat)

# Only proceed if the command was a git push and it succeeded
CMD=$(echo "$TOOL_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('tool_input',{}).get('command',''))" 2>/dev/null)
EXIT_CODE=$(echo "$TOOL_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('tool_response',{}).get('exit_code', d.get('exit_code', 1)))" 2>/dev/null)

# Exit silently if this wasn't a git push or it failed
if ! echo "$CMD" | grep -q "git push"; then exit 0; fi
if [ "$EXIT_CODE" != "0" ]; then exit 0; fi

REPO=$(gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null)
if [ -z "$REPO" ]; then exit 0; fi

echo "=== CI Check: waiting 35s for workflows to start... ==="
sleep 35

# Poll up to ~90s for all runs to leave queued/in_progress state
for i in $(seq 1 9); do
    PENDING=$(gh run list --repo "$REPO" --limit 5 --json status \
        --jq '[.[] | select(.status == "in_progress" or .status == "queued" or .status == "waiting")] | length' 2>/dev/null)
    if [ "${PENDING:-1}" = "0" ]; then break; fi
    echo "CI still running... ($((i * 10))s)"
    sleep 10
done

# Summary
echo ""
echo "=== CI Results for $REPO ==="
gh run list --repo "$REPO" --limit 5 \
    --json conclusion,status,name \
    --jq '.[] | "  \(if .conclusion != null then .conclusion else .status end | ascii_upcase)  \(.name)"' 2>/dev/null

# Dump logs for any failed runs
FAILED_IDS=$(gh run list --repo "$REPO" --limit 5 \
    --json conclusion,databaseId \
    --jq '.[] | select(.conclusion == "failure") | .databaseId' 2>/dev/null)

if [ -n "$FAILED_IDS" ]; then
    echo ""
    echo "=== Failure Logs ==="
    for id in $FAILED_IDS; do
        gh run view "$id" --repo "$REPO" --log-failed 2>/dev/null \
            | grep -v "^[[:space:]]*$" \
            | tail -60
        echo "---"
    done
    echo ""
    echo "ACTION REQUIRED: CI failures detected — review logs above and fix before ending session."
else
    echo ""
    echo "All workflows passed."
fi
