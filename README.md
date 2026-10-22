# SubText Editor

SubText is a small **native Qt desktop editor** for Fedora and KDE Plasma. It is an independent prototype informed by REA's static inspection of the locally installed Sublime Text package. It contains no Sublime Text binaries, source, or branded artwork.

## Run from this folder

Fedora needs Python 3 and a virtual environment. Install PySide6 into the project-local environment, then launch the editor:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
./run.sh
```

Open files from a terminal with `./run.sh file.py another.txt`, or use **File → Open File** in the application.

## Add it to the Plasma application launcher

```sh
./install.sh
```

The installer copies the app under `~/.local/share/subtext-editor`, creates an isolated Python environment there, installs PySide6, and registers a `.desktop` entry under `~/.local/share/applications`. It does not need administrator access.

## Current editor features

- Native Qt window, project file tree, and multiple document tabs.
- Split View displays the active document in two synchronized editor panes.
- Open, save, save as, save all, recent files, and revert; manually closing a changed tab asks before discarding it.
- Untitled tabs use a preview of the first line, and closing the application restores the previous session, including unsaved buffers.
- Reopen and save files with UTF-8, UTF-16, Windows-1252, and ISO-8859-1 encodings; printing and separate editor windows.
- Python syntax colors, line numbers, find next/previous, and go to line.
- Quick Open for open tabs and a command palette.
- Tools menu with native build output, cancellation and result navigation; user build definitions use a supported subset of `.sublime-build` JSON.
- Built-in and user snippets with numbered placeholders, plus recording, replaying and saving basic text-edit macros.
- `Ctrl+K`, `Ctrl+B` toggles the project sidebar.

This is an initial desktop prototype. It does not load Sublime Text packages or Package Control, and it does not yet implement Sublime Text's full syntax engine, project/workspace model, multi-cursor editing, or native behavior in every detail. Macro recording currently captures text edits, and build definitions support a subset of Sublime Text's build-system options.

## Run the functional checks

The initial File menu checks exercise Split View, the filename suggestion from untitled text, and byte-level output for Save with Encoding:

```sh
QT_QPA_PLATFORM=offscreen python3 -m unittest discover -s tests -v
```

The application source is licensed under MIT. PySide6 and Qt remain separately licensed dependencies; see the [Qt for Python licensing information](https://doc.qt.io/qtforpython-6/commercial/index.html).
