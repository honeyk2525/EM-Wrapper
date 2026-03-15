# EM Wrapper — GROMACS Energy Minimization GUI

![Python](https://img.shields.io/badge/Python-3.8%2B-blue?logo=python&logoColor=white)
![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20macOS-lightgrey)
![GROMACS](https://img.shields.io/badge/GROMACS-2019%2B-orange)
![License](https://img.shields.io/badge/License-MIT-green)

A cross-platform desktop GUI that wraps the GROMACS command-line pipeline for **energy minimization of protein structures**. Select one or more `.pdb` files, pick a force field, and let it run — no terminal required.

---

## Features

| | Windows (`em_windows.py`) | macOS (`em_mac.py`) |
|---|---|---|
| Multi-file batch runs | ✅ Up to 25+ files | ✅ Up to 25+ files |
| Per-job status table | ✅ File · Status · Step columns | ✅ File · Status · Step columns |
| Live log viewer | ✅ Click any job row | ✅ Click any job row |
| Cancel All | ✅ | ✅ |
| Open output folder | ✅ Per selected job | ✅ Opens in Finder |
| Final PDB collection | ✅ Auto-copies all results to `minimized_pdbs/` | ✅ Auto-copies all results to `minimized_pdbs/` |
| Force field selector | ✅ 14 fields | ✅ 14 fields |
| Settings dialog | ✅ Custom `gmx` binary path | ✅ Custom `gmx` binary path |
| Persistent config | ✅ `~/.gmx_runner_config.json` | ✅ `~/.gmx_runner_config.json` |

---

## Pipeline

Each PDB file is processed through 8 sequential GROMACS commands:

```
Input .pdb
    │
    ▼
[1] pdb2gmx      Generate topology & hydrogen atoms
    │
    ▼
[2] editconf     Define dodecahedral simulation box (1.2 nm padding)
    │
    ▼
[3] solvate      Fill box with explicit water (SPC/TIP3P)
    │
    ▼
[4] grompp       Prepare system for ion addition
    │
    ▼
[5] genion       Add 0.15 M NaCl (neutralise charge)
    │
    ▼
[6] grompp       Prepare energy minimisation run input
    │
    ▼
[7] mdrun        Run steepest-descent minimisation (≤50 000 steps, tol 1000 kJ/mol/nm)
    │
    ▼
[8] trjconv      Centre protein, export final structure
    │
    ▼
Output  <name>_run/<name>.pdb   (+ minimized_pdbs/<name>.pdb)
```

All intermediate files, the topology, and a full `run.log` are kept in the per-file workspace directory.

---

## Supported Force Fields

| Display Name | GROMACS Key | Water Model |
|---|---|---|
| CHARMM27 *(default)* | `charmm27` | TIP3P |
| AMBER03 | `amber03` | TIP3P |
| AMBER94 | `amber94` | TIP3P |
| AMBER96 | `amber96` | TIP3P |
| AMBER99 | `amber99` | TIP3P |
| AMBER99SB | `amber99sb` | TIP3P |
| AMBER99SB-ILDN | `amber99sb-ildn` | TIP3P |
| GROMOS43a1 | `gromos43a1` | SPC |
| GROMOS43a2 | `gromos43a2` | SPC |
| GROMOS45a3 | `gromos45a3` | SPC |
| GROMOS53a5 | `gromos53a5` | SPC |
| GROMOS53a6 | `gromos53a6` | SPC |
| GROMOS54a7 | `gromos54a7` | SPC |
| OPLS-AA/L | `oplsaa` | TIP3P |

---

## Requirements

- **GROMACS 2019+** — `gmx` accessible in `PATH` (or set a custom path in Settings)
- **Python 3.8+** with `tkinter` (included with standard Python on both platforms)

### Windows
- Windows 10 / 11

### macOS
- macOS (Apple Silicon recommended)
- Python installed via Homebrew or python.org

---

## Quick Start

```bash
# Clone
git clone https://github.com/honeyk2525/EM-Wrapper.git
cd EM-Wrapper

# Windows
python em_windows.py

# macOS
python3 em_mac.py
```

No additional packages required — only the Python standard library is used.

---

## Usage

### Windows — Batch Mode

1. **Launch** `em_windows.py`
2. Click **Select PDB(s)** and choose one or more `.pdb` files
3. Select a **Force Field** from the dropdown
4. Jobs start automatically; the table shows live status per file
5. Click any row in the table to view its log
6. When all jobs finish, the final minimised structures are automatically collected into a `minimized_pdbs/` folder next to your input files

### macOS — Batch Mode

1. **Launch** `em_mac.py`
2. Click **Select PDB(s)** and choose one or more `.pdb` files
3. Select a **Force Field** from the dropdown
4. Jobs run sequentially; the table shows live status per file
5. Click any row in the table to view its log
6. When all jobs finish, the final minimised structures are automatically collected into a `minimized_pdbs/` folder next to your input files

### Configuring the GROMACS Path

If `gmx` is not on your `PATH`, open **Settings** (⚙) and enter the full path to the binary:

- Windows example: `C:\Program Files\GROMACS\bin\gmx.exe`
- macOS example: `/opt/homebrew/bin/gmx`

Settings are saved to `~/.gmx_runner_config.json` and persist across sessions.

---

## Output Structure

```
/path/to/inputs/
├── protein_A.pdb                   ← original input
├── protein_A_run/
│   ├── protein_A.pdb               ← final minimised structure
│   ├── run.log                     ← full GROMACS log
│   ├── topol.top
│   ├── em.edr / em.trr / em.gro
│   └── ...
├── protein_B_run/
│   └── ...
└── minimized_pdbs/                 ← all final PDBs collected here
    ├── protein_A.pdb
    └── protein_B.pdb
```

---

## Packaging as a Standalone Executable

### Windows `.exe`

```bash
pip install pyinstaller
pyinstaller --windowed --onefile em_windows.py
# Output: dist/em_windows.exe
```

### macOS `.app`

```bash
pip3 install pyinstaller
pyinstaller --windowed --onefile em_mac.py
# Output: dist/em_mac (runnable app bundle)
```

---

## License

MIT — see [LICENSE](LICENSE) for details.
