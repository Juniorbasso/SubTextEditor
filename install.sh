#!/bin/sh
set -eu

SOURCE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DATA_HOME=${XDG_DATA_HOME:-"$HOME/.local/share"}
APP_DIR="$DATA_HOME/subtext-editor"
APPLICATIONS_DIR="$DATA_HOME/applications"

mkdir -p "$APP_DIR" "$APPLICATIONS_DIR"
cp "$SOURCE_DIR/app.py" "$SOURCE_DIR/icon.svg" "$SOURCE_DIR/requirements.txt" "$APP_DIR/"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/python" -m pip install -r "$APP_DIR/requirements.txt"

cat > "$APPLICATIONS_DIR/subtext-editor.desktop" <<EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=SubText Editor
GenericName=Text Editor
Comment=Native Qt text editor
Exec="$APP_DIR/.venv/bin/python" "$APP_DIR/app.py" %F
Icon=$APP_DIR/icon.svg
Terminal=false
Categories=Development;TextEditor;
StartupNotify=true
StartupWMClass=subtext-editor
Actions=NewWindow;NewFile;

[Desktop Action NewWindow]
Name=New Window
Exec="$APP_DIR/.venv/bin/python" "$APP_DIR/app.py"

[Desktop Action NewFile]
Name=New File
Exec="$APP_DIR/.venv/bin/python" "$APP_DIR/app.py" --new-file
EOF

printf 'Installed SubText Editor. Find it in the Plasma application launcher.\n'
