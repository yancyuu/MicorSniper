#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN_DIR="${CRAW_BIN_DIR:-$HOME/.local/bin}"
TARGET="$BIN_DIR/craw"

mkdir -p "$BIN_DIR"

cd "$REPO_ROOT"
poetry install

cat > "$TARGET" <<EOF
#!/usr/bin/env bash
set -euo pipefail
cd "$REPO_ROOT"
exec poetry run craw "\$@"
EOF

chmod +x "$TARGET"

echo "Installed craw CLI: $TARGET"
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *)
    echo "Add this to your shell profile if craw is not found:"
    echo "  export PATH=\"$BIN_DIR:\$PATH\""
    ;;
esac
echo "Try: craw --help"
