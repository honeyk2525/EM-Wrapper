#!/usr/bin/env python3
"""
GROMACS Runner — macOS Desktop Application
===========================================
A graphical wrapper around GROMACS CLI commands for running
energy minimization on one or more PDB files simultaneously.

Workflow (per file):
  1. User selects one or more .pdb files via file dialog
  2. All runs start sequentially, each in its own thread
  3. A workspace directory is created next to each PDB file
  4. MDP parameter files (ions.mdp, minim.mdp) are auto-generated
  5. GROMACS commands are executed sequentially per job:
       pdb2gmx → editconf → solvate → grompp (ions) →
       genion → grompp (em) → mdrun (em) → trjconv (center)
  6. The final centered structure is saved as <original_name>.pdb

Features:
  - Select multiple PDB files; all run sequentially
  - Job table showing per-file status and step progress
  - Click any job row to view its log in the pane below
  - Cancel All button to abort every running job
  - Open Output Folder for the selected job after completion
  - Automatic log file (run.log) saved in each workspace
  - Force-field selection dropdown (default: CHARMM27)
  - Settings dialog to configure GROMACS binary path

Requirements:
  - macOS with Apple Silicon
  - GROMACS installed and accessible via `gmx` in PATH
  - Python 3.8+ with tkinter (included with macOS Python)

Usage:
  python3 em_mac.py

Packaging (macOS .app):
  pip3 install pyinstaller
  pyinstaller --windowed --onefile em_mac.py
"""

import json
import os
import platform
import shlex
import shutil
import subprocess
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk
from pathlib import Path
from typing import Optional


# =============================================================================
# Configuration (persisted to ~/.gmx_runner_config.json)
# =============================================================================

CONFIG_PATH = Path.home() / ".gmx_runner_config.json"

DEFAULT_CONFIG = {
    "gmx_path": "gmx",   # default: rely on PATH
}


def load_config() -> dict:
    """Load settings from disk, falling back to defaults."""
    try:
        if CONFIG_PATH.is_file():
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                saved = json.load(f)
            return {**DEFAULT_CONFIG, **saved}
    except Exception:
        pass
    return dict(DEFAULT_CONFIG)


def save_config(config: dict):
    """Persist settings to disk."""
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
    except Exception:
        pass  # non-critical


# =============================================================================
# Available Force Fields
# =============================================================================

FORCE_FIELDS = {
    "CHARMM27":        "charmm27",
    "AMBER03":         "amber03",
    "AMBER94":         "amber94",
    "AMBER96":         "amber96",
    "AMBER99":         "amber99",
    "AMBER99SB":       "amber99sb",
    "AMBER99SB-ILDN":  "amber99sb-ildn",
    "GROMOS43a1":      "gromos43a1",
    "GROMOS43a2":      "gromos43a2",
    "GROMOS45a3":      "gromos45a3",
    "GROMOS53a5":      "gromos53a5",
    "GROMOS53a6":      "gromos53a6",
    "GROMOS54a7":      "gromos54a7",
    "OPLS-AA/L":       "oplsaa",
}

DEFAULT_FF = "CHARMM27"

WATER_MODELS = {
    "charmm27":       "tip3p",
    "amber03":        "tip3p",
    "amber94":        "tip3p",
    "amber96":        "tip3p",
    "amber99":        "tip3p",
    "amber99sb":      "tip3p",
    "amber99sb-ildn": "tip3p",
    "gromos43a1":     "spc",
    "gromos43a2":     "spc",
    "gromos45a3":     "spc",
    "gromos53a5":     "spc",
    "gromos53a6":     "spc",
    "gromos54a7":     "spc",
    "oplsaa":         "tip3p",
}


# =============================================================================
# MDP File Templates
# =============================================================================

IONS_MDP = """\
; ions.mdp - used as input into grompp to generate ions.tpr
; Parameters describing what to do, when to stop and what to save
integrator  = steep
emtol       = 1000.0
emstep      = 0.01
nsteps      = 50000

; Parameters describing how to find the neighbors of each atom and how to calculate the interactions
nstlist         = 1
cutoff-scheme   = Verlet
coulombtype     = PME
rcoulomb        = 1.2
rvdw            = 1.2
pbc             = xyz
"""

MINIM_MDP = """\
; minim.mdp - used as input into grompp to generate em.tpr
; Parameters describing what to do, when to stop and what to save
integrator  = steep
emtol       = 1000.0
emstep      = 0.01
nsteps      = 50000

; Parameters describing how to find the neighbors of each atom and how to calculate the interactions
nstlist         = 1
cutoff-scheme   = Verlet
coulombtype     = PME
rcoulomb        = 1.2
rvdw            = 1.2
pbc             = xyz
"""


# =============================================================================
# GROMACS Pipeline
# =============================================================================

class GromacsPipeline:
    """Encapsulates the GROMACS energy-minimization workflow."""

    TOTAL_STEPS = 8

    def __init__(
        self,
        pdb_path: Path,
        ff_key: str,
        gmx_bin: str,
        log_callback,
        status_callback,
        progress_callback,
    ):
        self.pdb_path = pdb_path
        self.pdb_name = pdb_path.stem
        self.pdb_dir = pdb_path.parent
        self.ff_dir = FORCE_FIELDS.get(ff_key, "charmm27")
        self.water_model = WATER_MODELS.get(self.ff_dir, "tip3p")
        self.ff_label = ff_key
        self.gmx = gmx_bin
        self.run_dir: Path = Path()
        self.log = log_callback
        self.set_status = status_callback
        self.set_progress = progress_callback
        self._cancelled = False
        self._log_file = None

    # ------------------------------------------------------------------
    # Workspace
    # ------------------------------------------------------------------

    def _create_workspace(self) -> Path:
        base = self.pdb_dir / f"{self.pdb_name}_run"
        if not base.exists():
            base.mkdir(parents=True)
            return base

        counter = 2
        while True:
            candidate = self.pdb_dir / f"{self.pdb_name}_run_{counter}"
            if not candidate.exists():
                candidate.mkdir(parents=True)
                return candidate
            counter += 1

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------

    def _log_and_file(self, text: str):
        self.log(text)
        if self._log_file:
            try:
                self._log_file.write(text)
                self._log_file.flush()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # MDP generation
    # ------------------------------------------------------------------

    def _write_mdp_files(self):
        (self.run_dir / "ions.mdp").write_text(IONS_MDP, encoding="utf-8")
        (self.run_dir / "minim.mdp").write_text(MINIM_MDP, encoding="utf-8")
        self._log_and_file("✓ Generated ions.mdp\n")
        self._log_and_file("✓ Generated minim.mdp\n")

    # ------------------------------------------------------------------
    # Command execution
    # ------------------------------------------------------------------

    def _run_command(self, cmd: str, stdin_text: Optional[str] = None,
                     label: str = "") -> bool:
        if self._cancelled:
            return False

        self.set_status(label or cmd)
        self._log_and_file(f"\n{'='*60}\n")
        self._log_and_file(f"▶ {cmd}\n")
        self._log_and_file(f"{'='*60}\n")

        try:
            env = os.environ.copy()

            process = subprocess.Popen(
                cmd,
                shell=True,
                cwd=str(self.run_dir),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=env,
                text=True,
                bufsize=1,
            )

            if stdin_text is not None:
                process.stdin.write(stdin_text)
                process.stdin.flush()
            process.stdin.close()

            for line in iter(process.stdout.readline, ""):
                if self._cancelled:
                    process.kill()
                    self._log_and_file("\n⚠ Cancelled by user.\n")
                    return False
                self._log_and_file(line)

            process.stdout.close()
            returncode = process.wait()

            if returncode != 0:
                self._log_and_file(
                    f"\n✗ Command failed with exit code {returncode}\n"
                )
                self.set_status(f"Error — command failed (exit {returncode})")
                return False

            self._log_and_file("✓ Done\n")
            return True

        except FileNotFoundError:
            self._log_and_file(
                f"\n✗ '{self.gmx}' not found. Check Settings → GROMACS Path.\n"
            )
            self.set_status("Error — gmx not found")
            return False
        except Exception as exc:
            self._log_and_file(f"\n✗ Unexpected error: {exc}\n")
            self.set_status(f"Error — {exc}")
            return False

    # ------------------------------------------------------------------
    # Full pipeline
    # ------------------------------------------------------------------

    def run(self) -> bool:
        name = self.pdb_name
        gmx = self.gmx

        if not shutil.which(self.gmx) and not Path(self.gmx).is_file():
            self.log(
                f"✗ GROMACS binary '{self.gmx}' not found in PATH.\n"
                f"  Set the correct path in Settings → GROMACS Path.\n"
            )
            self.set_status("Error — gmx not found")
            return False

        self.set_status("Creating workspace…")
        self.run_dir = self._create_workspace()

        self._log_file = open(
            self.run_dir / "run.log", "w", encoding="utf-8"
        )

        self._log_and_file(f"📁 Workspace: {self.run_dir}\n")
        self._log_and_file(f"🧪 Force field: {self.ff_label} ({self.ff_dir})\n")
        self._log_and_file(f"💧 Water model: {self.water_model}\n")
        self._log_and_file(f"⚙ GROMACS binary: {self.gmx}\n\n")

        dest_pdb = self.run_dir / self.pdb_path.name
        shutil.copy2(str(self.pdb_path), str(dest_pdb))
        self._log_and_file(f"✓ Copied {self.pdb_path.name} → workspace\n")

        self.set_status("Generating parameter files…")
        self._write_mdp_files()

        # Pre-quote filenames for shell safety (handles spaces, etc.)
        q = shlex.quote
        f_pdb = q(f"{name}.pdb")
        f_processed = q(f"{name}_processed.gro")
        f_newbox = q(f"{name}_newbox.gro")
        f_solv = q(f"{name}_solv.gro")
        f_solv_ions = q(f"{name}_solv_ions.gro")

        commands = [
            {
                "cmd":   f"{gmx} pdb2gmx -f {f_pdb} -o {f_processed} "
                         f"-water {self.water_model} -ff {self.ff_dir}",
                "stdin": None,
                "label": f"[1/8] pdb2gmx — generating topology ({self.ff_label})…",
            },
            {
                "cmd":   f"{gmx} editconf -f {f_processed} "
                         f"-o {f_newbox} -c -d 1.2 -bt dodecahedron",
                "stdin": None,
                "label": "[2/8] editconf — defining simulation box…",
            },
            {
                "cmd":   f"{gmx} solvate -cp {f_newbox} -cs spc216.gro "
                         f"-o {f_solv} -p topol.top",
                "stdin": None,
                "label": "[3/8] solvate — adding water…",
            },
            {
                "cmd":   f"{gmx} grompp -f ions.mdp -c {f_solv} "
                         f"-p topol.top -o ions.tpr -maxwarn 1",
                "stdin": None,
                "label": "[4/8] grompp — preparing for ion addition…",
            },
            {
                "cmd":   f"{gmx} genion -s ions.tpr -o {f_solv_ions} "
                         f"-p topol.top -pname NA -nname CL -neutral -conc 0.15",
                "stdin": "SOL\n",
                "label": "[5/8] genion — adding ions (0.15 M NaCl)…",
            },
            {
                "cmd":   f"{gmx} grompp -f minim.mdp -c {f_solv_ions} "
                         f"-p topol.top -o em.tpr",
                "stdin": None,
                "label": "[6/8] grompp — preparing energy minimization…",
            },
            {
                "cmd":   f"{gmx} mdrun -v -deffnm em",
                "stdin": None,
                "label": "[7/8] mdrun — running energy minimization…",
            },
            {
                "cmd":   f"{gmx} trjconv -s em.tpr -f em.gro "
                         f"-o centered.pdb -pbc mol -ur compact -center",
                "stdin": "Protein\nProtein\n",
                "label": "[8/8] trjconv — centering and exporting final PDB…",
            },
        ]

        for i, step in enumerate(commands, 1):
            if self._cancelled:
                self._log_and_file("\n⚠ Cancelled by user.\n")
                self.set_status("Cancelled")
                self._close_log()
                return False

            self.set_progress(i, self.TOTAL_STEPS)
            self._log_and_file(f"\n── Step {i}/{len(commands)} ──\n")
            ok = self._run_command(
                cmd=step["cmd"],
                stdin_text=step["stdin"],
                label=step["label"],
            )
            if not ok:
                self._log_and_file("\n⛔ Pipeline stopped due to error.\n")
                self._close_log()
                return False

        centered = self.run_dir / "centered.pdb"
        final_pdb = self.run_dir / self.pdb_path.name
        try:
            centered.rename(final_pdb)
            self._log_and_file(
                f"\n✓ Renamed centered.pdb → {self.pdb_path.name}\n"
            )
        except Exception as exc:
            self._log_and_file(
                f"\n⚠ Could not rename centered.pdb: {exc}\n"
            )

        self.set_progress(self.TOTAL_STEPS, self.TOTAL_STEPS)
        self.set_status("✅ Completed")
        self._log_and_file(f"\n{'='*60}\n")
        self._log_and_file("🎉 Energy minimization completed successfully!\n")
        self._log_and_file(f"📁 All output files are in:\n   {self.run_dir}\n")
        self._log_and_file(f"{'='*60}\n")
        self._close_log()
        return True

    def cancel(self):
        self._cancelled = True

    def _close_log(self):
        if self._log_file:
            try:
                self._log_file.close()
            except Exception:
                pass
            self._log_file = None


# =============================================================================
# Settings Dialog
# =============================================================================

class SettingsDialog(tk.Toplevel):
    """Modal dialog for configuring the GROMACS binary path."""

    def __init__(self, parent, cfg: dict):
        super().__init__(parent)
        self.title("Settings")
        self.cfg = cfg
        self.result = None
        self.resizable(False, False)
        self.grab_set()
        self.transient(parent)

        frame = tk.Frame(self, padx=16, pady=16)
        frame.pack(fill=tk.BOTH, expand=True)

        tk.Label(
            frame, text="GROMACS binary path:", font=("Helvetica", 12),
        ).grid(row=0, column=0, sticky=tk.W, pady=(0, 4))

        self.gmx_var = tk.StringVar(value=cfg.get("gmx_path", "gmx"))
        entry = tk.Entry(frame, textvariable=self.gmx_var, width=40,
                         font=("Menlo", 12))
        entry.grid(row=1, column=0, sticky=tk.EW, pady=(0, 4))

        tk.Label(
            frame,
            text='Default: "gmx" (uses PATH).\n'
                 "Example: /opt/homebrew/bin/gmx",
            font=("Helvetica", 10),
            fg="#666666",
            justify=tk.LEFT,
        ).grid(row=2, column=0, sticky=tk.W, pady=(0, 12))

        btn_browse = tk.Button(
            frame, text="Browse…", command=self._browse,
        )
        btn_browse.grid(row=1, column=1, padx=(6, 0))

        btn_frame = tk.Frame(frame)
        btn_frame.grid(row=3, column=0, columnspan=2, sticky=tk.E)

        tk.Button(btn_frame, text="Cancel", width=8,
                  command=self.destroy).pack(side=tk.RIGHT, padx=(6, 0))
        tk.Button(btn_frame, text="Save", width=8,
                  command=self._save).pack(side=tk.RIGHT)

    def _browse(self):
        path = filedialog.askopenfilename(title="Locate gmx binary")
        if path:
            self.gmx_var.set(path)

    def _save(self):
        self.cfg["gmx_path"] = self.gmx_var.get().strip() or "gmx"
        self.result = self.cfg
        save_config(self.cfg)
        self.destroy()


# =============================================================================
# tkinter GUI
# =============================================================================

class GromacsRunnerApp:
    """Main application window."""

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("GROMACS Runner")
        self.root.geometry("960x780")
        self.root.minsize(700, 540)

        self.config = load_config()

        # item_id -> {"pipeline", "thread", "log": list[str], "run_dir"}
        self._jobs: dict = {}
        self._active_item: Optional[str] = None  # tree row whose log is shown
        self._running_count = 0
        self._job_queue: list = []  # item_ids waiting to start

        self._build_ui()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        # === Row 1: Select PDB(s) + Force Field + Settings =============
        row1 = tk.Frame(self.root, padx=12)
        row1.pack(fill=tk.X, pady=(10, 4))

        self.btn_select = tk.Button(
            row1, text="Select PDB(s)", command=self._on_select_pdb,
            width=14, font=("Helvetica", 13, "bold"),
        )
        self.btn_select.pack(side=tk.LEFT)

        tk.Label(row1, text="  Force Field:", font=("Helvetica", 12)).pack(
            side=tk.LEFT,
        )
        self.ff_var = tk.StringVar(value=DEFAULT_FF)
        ff_menu = ttk.Combobox(
            row1,
            textvariable=self.ff_var,
            values=list(FORCE_FIELDS.keys()),
            state="readonly",
            width=18,
            font=("Helvetica", 12),
        )
        ff_menu.pack(side=tk.LEFT, padx=(4, 0))

        self.btn_settings = tk.Button(
            row1, text="⚙ Settings", command=self._open_settings,
            font=("Helvetica", 11),
        )
        self.btn_settings.pack(side=tk.RIGHT)

        # === Row 2: Cancel All + Open Folder + Status ==================
        row2 = tk.Frame(self.root, padx=12)
        row2.pack(fill=tk.X, pady=(0, 4))

        self.btn_cancel = tk.Button(
            row2, text="Cancel All", command=self._on_cancel,
            width=10, state=tk.DISABLED, font=("Helvetica", 11),
            fg="#cc3333",
        )
        self.btn_cancel.pack(side=tk.LEFT)

        self.btn_open_folder = tk.Button(
            row2, text="📂 Open Output Folder", command=self._open_folder,
            state=tk.DISABLED, font=("Helvetica", 11),
        )
        self.btn_open_folder.pack(side=tk.LEFT, padx=(8, 0))

        self.status_var = tk.StringVar(value="Waiting for files")
        self.status_label = tk.Label(
            row2, textvariable=self.status_var, anchor=tk.W,
            font=("Helvetica", 12), padx=12,
        )
        self.status_label.pack(side=tk.LEFT, fill=tk.X, expand=True)

        # === Row 3: Job table ==========================================
        tree_outer = tk.Frame(self.root, padx=12)
        tree_outer.pack(fill=tk.X, pady=(0, 4))

        vsb = ttk.Scrollbar(tree_outer, orient=tk.VERTICAL)
        self.job_tree = ttk.Treeview(
            tree_outer,
            columns=("status", "step"),
            height=7,
            yscrollcommand=vsb.set,
            selectmode="browse",
        )
        vsb.config(command=self.job_tree.yview)

        self.job_tree.heading("#0",       text="File",   anchor=tk.W)
        self.job_tree.heading("status",   text="Status", anchor=tk.W)
        self.job_tree.heading("step",     text="Step",   anchor=tk.CENTER)

        self.job_tree.column("#0",     width=280, stretch=True)
        self.job_tree.column("status", width=300, stretch=True)
        self.job_tree.column("step",   width=70,  stretch=False, anchor=tk.CENTER)

        self.job_tree.pack(side=tk.LEFT, fill=tk.X, expand=True)
        vsb.pack(side=tk.LEFT, fill=tk.Y)

        self.job_tree.bind("<<TreeviewSelect>>", self._on_job_select)

        # === Log area ==================================================
        tk.Label(
            self.root,
            text="Log  (click a job above to view its output):",
            font=("Helvetica", 10), anchor=tk.W,
        ).pack(fill=tk.X, padx=12, pady=(2, 2))

        self.output = scrolledtext.ScrolledText(
            self.root, wrap=tk.WORD, font=("Menlo", 11),
            bg="#1e1e1e", fg="#d4d4d4", insertbackground="#d4d4d4",
            state=tk.DISABLED, padx=8, pady=8,
        )
        self.output.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    def _open_settings(self):
        dlg = SettingsDialog(self.root, dict(self.config))
        self.root.wait_window(dlg)
        if dlg.result is not None:
            self.config = dlg.result

    # ------------------------------------------------------------------
    # Open output folder for the selected job
    # ------------------------------------------------------------------

    def _open_folder(self):
        run_dir = None
        if self._active_item and self._active_item in self._jobs:
            run_dir = self._jobs[self._active_item].get("run_dir")
        if not run_dir or not run_dir.is_dir():
            return
        system = platform.system()
        if system == "Darwin":
            subprocess.Popen(["open", str(run_dir)])
        elif system == "Windows":
            os.startfile(str(run_dir))
        else:
            subprocess.Popen(["xdg-open", str(run_dir)])

    # ------------------------------------------------------------------
    # Cancel all running pipelines
    # ------------------------------------------------------------------

    def _on_cancel(self):
        for job in self._jobs.values():
            pipeline = job.get("pipeline")
            if pipeline:
                pipeline.cancel()
        self.status_var.set("Cancelling all jobs…")

    # ------------------------------------------------------------------
    # Job table selection → switch log view
    # ------------------------------------------------------------------

    def _on_job_select(self, _event=None):
        sel = self.job_tree.selection()
        if not sel:
            return
        item_id = sel[0]
        self._active_item = item_id
        if item_id not in self._jobs:
            return
        chunks = self._jobs[item_id]["log"]
        self.output.configure(state=tk.NORMAL)
        self.output.delete("1.0", tk.END)
        for chunk in chunks:
            self.output.insert(tk.END, chunk)
        self.output.see(tk.END)
        self.output.configure(state=tk.DISABLED)
        # Enable open folder if this job has a finished output directory
        run_dir = self._jobs[item_id].get("run_dir")
        if run_dir and run_dir.is_dir():
            self.btn_open_folder.configure(state=tk.NORMAL)
        else:
            self.btn_open_folder.configure(state=tk.DISABLED)

    # ------------------------------------------------------------------
    # Thread-safe callbacks (one set per job, created by closures)
    # ------------------------------------------------------------------

    def _make_log_callback(self, item_id: str):
        def _cb(text: str):
            self.root.after(0, self._on_job_log, item_id, text)
        return _cb

    def _on_job_log(self, item_id: str, text: str):
        if item_id not in self._jobs:
            return
        self._jobs[item_id]["log"].append(text)
        if self._active_item == item_id:
            self.output.configure(state=tk.NORMAL)
            self.output.insert(tk.END, text)
            self.output.see(tk.END)
            self.output.configure(state=tk.DISABLED)

    def _make_status_callback(self, item_id: str):
        def _cb(text: str):
            self.root.after(0, self.job_tree.set, item_id, "status", text)
        return _cb

    def _make_progress_callback(self, item_id: str):
        def _cb(current: int, total: int):
            self.root.after(0, self.job_tree.set, item_id, "step",
                            f"{current}/{total}")
        return _cb

    # ------------------------------------------------------------------
    # PDB selection → launch all pipelines sequentially
    # ------------------------------------------------------------------

    def _on_select_pdb(self):
        filepaths = filedialog.askopenfilenames(
            title="Select PDB file(s)",
            filetypes=[("PDB files", "*.pdb"), ("All files", "*.*")],
        )
        if not filepaths:
            return

        gmx_bin = self.config.get("gmx_path", "gmx")
        ff_key = self.ff_var.get()

        new_items = []
        for filepath in filepaths:
            pdb_path = Path(filepath)
            if not pdb_path.is_file():
                continue

            item_id = self.job_tree.insert(
                "", tk.END,
                text=pdb_path.name,
                values=("Queued", "—"),
            )
            job = {"pipeline": None, "thread": None, "log": [], "run_dir": None}
            self._jobs[item_id] = job

            pipeline = GromacsPipeline(
                pdb_path=pdb_path,
                ff_key=ff_key,
                gmx_bin=gmx_bin,
                log_callback=self._make_log_callback(item_id),
                status_callback=self._make_status_callback(item_id),
                progress_callback=self._make_progress_callback(item_id),
            )
            job["pipeline"] = pipeline
            job["thread"] = threading.Thread(
                target=self._run_pipeline, args=(item_id,), daemon=True
            )
            new_items.append(item_id)

        if not new_items:
            return

        # Show the first new job's log automatically
        first = new_items[0]
        self.job_tree.selection_set(first)
        self.job_tree.see(first)
        self._active_item = first

        self._running_count += len(new_items)
        self.btn_cancel.configure(state=tk.NORMAL)
        self.btn_select.configure(state=tk.DISABLED)
        self.btn_settings.configure(state=tk.DISABLED)
        self.status_var.set(f"Running {self._running_count} job(s)…")

        self._job_queue.extend(new_items)
        self._start_next_job()

    # ------------------------------------------------------------------
    # Start the next queued job (sequential mode)
    # ------------------------------------------------------------------

    def _start_next_job(self):
        if self._job_queue:
            item_id = self._job_queue.pop(0)
            self._jobs[item_id]["thread"].start()

    # ------------------------------------------------------------------
    # Worker thread body
    # ------------------------------------------------------------------

    def _run_pipeline(self, item_id: str):
        job = self._jobs[item_id]
        pipeline = job["pipeline"]
        success = False
        try:
            success = pipeline.run()
        except Exception as exc:
            self.root.after(0, self._on_job_log, item_id,
                            f"\n✗ Fatal error: {exc}\n")
            self.root.after(0, self.job_tree.set, item_id, "status",
                            f"Error — {exc}")
        finally:
            run_dir = pipeline.run_dir if pipeline.run_dir != Path() else None
            self.root.after(0, self._on_job_done, item_id, success, run_dir)

    def _on_job_done(self, item_id: str, success: bool, run_dir: Optional[Path]):
        self._running_count = max(0, self._running_count - 1)
        job = self._jobs[item_id]
        job["run_dir"] = run_dir
        pipeline = job["pipeline"]

        if pipeline and pipeline._cancelled:
            self.job_tree.set(item_id, "status", "⚠ Cancelled")
        elif success:
            self.job_tree.set(item_id, "status", "✅ Done")
            self.job_tree.set(item_id, "step", "8/8")
        else:
            self.job_tree.set(item_id, "status", "⛔ Failed")

        # Refresh open-folder button for the currently selected row
        if self._active_item == item_id and run_dir and run_dir.is_dir():
            self.btn_open_folder.configure(state=tk.NORMAL)

        if self._running_count == 0:
            self.btn_cancel.configure(state=tk.DISABLED)
            self.btn_select.configure(state=tk.NORMAL)
            self.btn_settings.configure(state=tk.NORMAL)
            all_items = self.job_tree.get_children()
            done = sum(
                1 for iid in all_items
                if "✅" in self.job_tree.set(iid, "status")
            )
            self.status_var.set(
                f"All done — {done}/{len(all_items)} completed successfully"
            )
            self._collect_final_pdbs()
        else:
            self.status_var.set(f"Running {self._running_count} job(s)…")
            self._start_next_job()

    # ------------------------------------------------------------------
    # Collect all final PDBs into one folder
    # ------------------------------------------------------------------

    def _collect_final_pdbs(self):
        """Copy every successful job's final PDB into a shared output folder."""
        all_items = self.job_tree.get_children()
        pdbs_to_copy = []
        for iid in all_items:
            if "✅" not in self.job_tree.set(iid, "status"):
                continue
            job = self._jobs.get(iid)
            if not (job and job.get("run_dir") and job.get("pipeline")):
                continue
            pdb_path = job["pipeline"].pdb_path
            final_pdb = job["run_dir"] / pdb_path.name
            if final_pdb.is_file():
                pdbs_to_copy.append((final_pdb, pdb_path))

        if not pdbs_to_copy:
            return

        # Place the output folder next to the input PDB(s)
        parents = list({pdb.parent for _, pdb in pdbs_to_copy})
        base_dir = (
            parents[0]
            if len(parents) == 1
            else Path(os.path.commonpath([str(p) for p in parents]))
        )

        out_dir = base_dir / "minimized_pdbs"
        counter = 2
        while out_dir.exists():
            out_dir = base_dir / f"minimized_pdbs_{counter}"
            counter += 1
        out_dir.mkdir(parents=True)

        for final_pdb, orig_pdb in pdbs_to_copy:
            dest = out_dir / orig_pdb.name
            if dest.exists():
                c = 2
                while dest.exists():
                    dest = out_dir / f"{orig_pdb.stem}_{c}{orig_pdb.suffix}"
                    c += 1
            shutil.copy2(str(final_pdb), str(dest))

        done = len(pdbs_to_copy)
        total = len(all_items)
        self.status_var.set(
            f"All done — {done}/{total} completed successfully. "
            f"Final PDBs → {out_dir}"
        )
        # Log to whichever job is currently visible
        active = self._active_item or (list(self._jobs.keys())[-1] if self._jobs else None)
        if active:
            self._on_job_log(
                active,
                f"\n{'='*60}\n"
                f"📦 Collected {done} final PDB(s) → {out_dir}\n"
                f"{'='*60}\n",
            )


# =============================================================================
# Entry point
# =============================================================================

def main():
    root = tk.Tk()
    GromacsRunnerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
