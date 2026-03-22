# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the App

```bash
python em_windows.py
```

No package installation required — only the Python standard library is used (`tkinter`, `subprocess`, `threading`, `json`, `pathlib`, `shutil`).

## Packaging as Standalone Executable

```bash
pip install pyinstaller
pyinstaller --windowed --onefile em_windows.py
# Output: dist/em_windows.exe
```

## Architecture

The codebase is a single self-contained file (`em_windows.py`) containing three classes and a small config module:

**`GromacsPipeline`** — Runs the 8-step GROMACS energy minimization workflow in a background thread. Takes three callbacks (`log_callback`, `status_callback`, `progress_callback`) that use `root.after(0, ...)` for thread-safe tkinter updates. Writes MDP files (`ions.mdp`, `minim.mdp`) into the workspace, then executes `pdb2gmx → editconf → solvate → grompp → genion → grompp → mdrun → trjconv` via `subprocess.Popen(shell=True)`. Cancellation is cooperative: `_cancelled` flag is checked before each step and while reading stdout lines.

**`GromacsRunnerApp`** — The main tkinter window. Manages a `_jobs` dict keyed by `ttk.Treeview` item IDs, mapping each to `{"pipeline", "thread", "log": list[str], "run_dir"}`. Jobs are queued in `_job_queue` and dispatched one-at-a-time via `_start_next_job()` (sequential execution). After all jobs finish, `_collect_final_pdbs()` copies final PDBs into a `minimized_pdbs/` folder next to the inputs.

**`SettingsDialog`** — Modal `tk.Toplevel` for editing the `gmx` binary path. Saves to `~/.gmx_runner_config.json`.

## Config

Persistent config is stored at `~/.gmx_runner_config.json` (only `gmx_path`). Default is `"gmx"` (PATH lookup).

## GROMACS Pipeline Details

- `ions.mdp` and `minim.mdp` are generated fresh in each run workspace (hardcoded templates in `IONS_MDP` / `MINIM_MDP` constants).
- `genion` receives `"SOL\n"` via stdin; `trjconv` receives `"Protein\nProtein\n"`.
- Each PDB gets its own workspace: `<name>_run/` (or `<name>_run_2/` etc. if it already exists).
- `FORCE_FIELDS` maps display names to GROMACS keys; `WATER_MODELS` maps those keys to `tip3p`/`spc`.
