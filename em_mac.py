#!/usr/bin/env python3
"""
GROMACS Runner — macOS Desktop Application
===========================================
A graphical wrapper around GROMACS CLI commands for running
energy minimization on a selected PDB file.

Workflow:
  1. User selects a .pdb file via file dialog
  2. A workspace directory is created next to the PDB file
  3. MDP parameter files (ions.mdp, minim.mdp) are auto-generated
  4. GROMACS commands are executed sequentially:
       pdb2gmx → editconf → solvate → grompp (ions) →
       genion → grompp (em) → mdrun (em) → trjconv (center)
  5. The final centered structure is saved as <original_name>.pdb

Features:
  - Force-field selection dropdown (default: CHARMM27)
  - Visual progress bar with step counter
  - Cancel button to abort a running pipeline
  - Open output folder in Finder after completion
  - Automatic log file (run.log) saved in workspace
  - Settings dialog to configure GROMACS binary path

Requirements:
  - macOS with Apple Silicon
  - GROMACS installed and accessible via `gmx` in PATH
  - Python 3.8+ with tkinter (included with macOS Python)

Usage:
  python3 gmx_runner.py

Packaging (macOS .app):
  pip3 install pyinstaller
  pyinstaller --windowed --onefile gmx_runner.py
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
            # Merge with defaults so new keys are always present
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

# These are the standard GROMACS force-field directory names.
# The -ff flag accepts them directly, so no interactive prompt is needed.
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

# Appropriate water model for each force field
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

    TOTAL_STEPS = 8  # number of GROMACS commands

    def __init__(
        self,
        pdb_path: Path,
        ff_key: str,
        gmx_bin: str,
        log_callback,
        status_callback,
        progress_callback,
    ):
        """
        Args:
            pdb_path:          Absolute path to the user-selected .pdb file.
            ff_key:            Display name of the force field (key in FORCE_FIELDS).
            gmx_bin:           Path to the gmx binary (e.g. "gmx" or "/opt/homebrew/bin/gmx").
            log_callback:      Callable(str) — appends text to the GUI output area.
            status_callback:   Callable(str) — updates the GUI status label.
            progress_callback: Callable(int, int) — updates the progress bar (current, total).
        """
        self.pdb_path = pdb_path
        self.pdb_name = pdb_path.stem                 # e.g. "PROT"
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
        self._log_file = None   # file handle for run.log

    # ------------------------------------------------------------------
    # Workspace
    # ------------------------------------------------------------------

    def _create_workspace(self) -> Path:
        """
        Create a unique run directory next to the PDB file.
        Naming: PROT_run → PROT_run_2 → PROT_run_3 → …
        """
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
        """Write to both the GUI and the log file."""
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
        """Write ions.mdp and minim.mdp inside the run directory."""
        (self.run_dir / "ions.mdp").write_text(IONS_MDP, encoding="utf-8")
        (self.run_dir / "minim.mdp").write_text(MINIM_MDP, encoding="utf-8")
        self._log_and_file("✓ Generated ions.mdp\n")
        self._log_and_file("✓ Generated minim.mdp\n")

    # ------------------------------------------------------------------
    # Command execution
    # ------------------------------------------------------------------

    def _run_command(self, cmd: str, stdin_text: Optional[str] = None,
                     label: str = "") -> bool:
        """
        Execute a single shell command inside the run directory.

        Args:
            cmd:        The command string to execute.
            stdin_text: Optional text to pipe into the process's stdin
                        (for interactive prompts like group selection).
            label:      Human-readable label for the status bar.

        Returns:
            True if the command succeeded (returncode == 0), False otherwise.
        """
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
        """Execute the complete energy-minimization workflow."""
        name = self.pdb_name
        gmx = self.gmx

        # --- Pre-flight: verify gmx is reachable --------------------------
        if not shutil.which(self.gmx) and not Path(self.gmx).is_file():
            self.log(
                f"✗ GROMACS binary '{self.gmx}' not found in PATH.\n"
                f"  Set the correct path in Settings → GROMACS Path.\n"
            )
            self.set_status("Error — gmx not found")
            return False

        # --- Step 0: Create workspace ----------------------------------
        self.set_status("Creating workspace…")
        self.run_dir = self._create_workspace()

        # Open the log file for the duration of the run
        self._log_file = open(
            self.run_dir / "run.log", "w", encoding="utf-8"
        )

        self._log_and_file(f"📁 Workspace: {self.run_dir}\n")
        self._log_and_file(f"🧪 Force field: {self.ff_label} ({self.ff_dir})\n")
        self._log_and_file(f"💧 Water model: {self.water_model}\n")
        self._log_and_file(f"⚙ GROMACS binary: {self.gmx}\n\n")

        # Copy PDB into workspace
        dest_pdb = self.run_dir / self.pdb_path.name
        shutil.copy2(str(self.pdb_path), str(dest_pdb))
        self._log_and_file(f"✓ Copied {self.pdb_path.name} → workspace\n")

        # Write MDP parameter files
        self.set_status("Generating parameter files…")
        self._write_mdp_files()

        # --- GROMACS commands ------------------------------------------
        #
        # Interactive input:
        #   • pdb2gmx  — force field set via -ff flag (no prompt)
        #   • genion   — sends "SOL\n" to select the solvent group by name
        #   • trjconv  — sends "Protein\nSystem\n" to select groups by name

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
                         f"-p topol.top -o ions.tpr",
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

        # --- Rename centered.pdb → original PDB name ------------------
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

        # --- Done! -----------------------------------------------------
        self.set_progress(self.TOTAL_STEPS, self.TOTAL_STEPS)
        self.set_status("✅ Completed — energy minimization finished")
        self._log_and_file(f"\n{'='*60}\n")
        self._log_and_file("🎉 Energy minimization completed successfully!\n")
        self._log_and_file(f"📁 All output files are in:\n   {self.run_dir}\n")
        self._log_and_file(f"{'='*60}\n")
        self._close_log()
        return True

    def cancel(self):
        """Signal the pipeline to stop after the current command."""
        self._cancelled = True

    def _close_log(self):
        """Close the log file handle."""
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

    def __init__(self, parent, config: dict):
        super().__init__(parent)
        self.title("Settings")
        self.config = config
        self.result = None  # set to updated config on OK
        self.resizable(False, False)
        self.grab_set()  # modal

        # Make the dialog appear centred over the parent
        self.transient(parent)

        # --- Widgets ---
        frame = tk.Frame(self, padx=16, pady=16)
        frame.pack(fill=tk.BOTH, expand=True)

        tk.Label(
            frame, text="GROMACS binary path:", font=("Helvetica", 12),
        ).grid(row=0, column=0, sticky=tk.W, pady=(0, 4))

        self.gmx_var = tk.StringVar(value=config.get("gmx_path", "gmx"))
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

        # --- OK / Cancel ---
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
        self.config["gmx_path"] = self.gmx_var.get().strip() or "gmx"
        self.result = self.config
        save_config(self.config)
        self.destroy()


# =============================================================================
# tkinter GUI
# =============================================================================

class GromacsRunnerApp:
    """Main application window."""

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("GROMACS Runner")
        self.root.geometry("860x680")
        self.root.minsize(640, 480)

        self.config = load_config()
        self.pipeline: Optional[GromacsPipeline] = None
        self._running = False
        self._last_run_dir: Optional[Path] = None

        self._build_ui()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        """Lay out all widgets."""

        # === Row 1: Select PDB + Force Field + Settings ================
        row1 = tk.Frame(self.root, padx=12, pady=(10, 4))
        row1.pack(fill=tk.X)

        self.btn_select = tk.Button(
            row1, text="Select PDB", command=self._on_select_pdb,
            width=12, font=("Helvetica", 13, "bold"),
        )
        self.btn_select.pack(side=tk.LEFT)

        # Force-field dropdown
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

        # Settings button (gear icon)
        self.btn_settings = tk.Button(
            row1, text="⚙ Settings", command=self._open_settings,
            font=("Helvetica", 11),
        )
        self.btn_settings.pack(side=tk.RIGHT)

        # === Row 2: Cancel + Open Folder + Status ======================
        row2 = tk.Frame(self.root, padx=12, pady=(0, 4))
        row2.pack(fill=tk.X)

        self.btn_cancel = tk.Button(
            row2, text="Cancel", command=self._on_cancel,
            width=8, state=tk.DISABLED, font=("Helvetica", 11),
            fg="#cc3333",
        )
        self.btn_cancel.pack(side=tk.LEFT)

        self.btn_open_folder = tk.Button(
            row2, text="📂 Open Output Folder", command=self._open_folder,
            state=tk.DISABLED, font=("Helvetica", 11),
        )
        self.btn_open_folder.pack(side=tk.LEFT, padx=(8, 0))

        self.status_var = tk.StringVar(value="Waiting for file")
        self.status_label = tk.Label(
            row2, textvariable=self.status_var, anchor=tk.W,
            font=("Helvetica", 12), padx=12,
        )
        self.status_label.pack(side=tk.LEFT, fill=tk.X, expand=True)

        # === Row 3: Progress bar =======================================
        row3 = tk.Frame(self.root, padx=12, pady=(0, 6))
        row3.pack(fill=tk.X)

        self.progress_var = tk.DoubleVar(value=0.0)
        self.progress_bar = ttk.Progressbar(
            row3, variable=self.progress_var,
            maximum=100, mode="determinate",
        )
        self.progress_bar.pack(fill=tk.X)

        self.progress_label = tk.Label(
            row3, text="Step 0/8", font=("Helvetica", 10), anchor=tk.E,
        )
        self.progress_label.pack(anchor=tk.E)

        # === Output area ===============================================
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
        """Show the settings dialog."""
        if self._running:
            messagebox.showinfo("Busy", "Cannot change settings while running.")
            return
        dlg = SettingsDialog(self.root, dict(self.config))
        self.root.wait_window(dlg)
        if dlg.result is not None:
            self.config = dlg.result

    # ------------------------------------------------------------------
    # Open output folder in Finder
    # ------------------------------------------------------------------

    def _open_folder(self):
        """Open the last run directory in the system file manager."""
        if self._last_run_dir and self._last_run_dir.is_dir():
            system = platform.system()
            if system == "Darwin":
                subprocess.Popen(["open", str(self._last_run_dir)])
            elif system == "Windows":
                os.startfile(str(self._last_run_dir))
            else:
                subprocess.Popen(["xdg-open", str(self._last_run_dir)])

    # ------------------------------------------------------------------
    # Cancel
    # ------------------------------------------------------------------

    def _on_cancel(self):
        """Ask the pipeline to stop."""
        if self.pipeline:
            self.pipeline.cancel()
            self.status_var.set("Cancelling…")

    # ------------------------------------------------------------------
    # Logging helpers (thread-safe via root.after)
    # ------------------------------------------------------------------

    def _append_log(self, text: str):
        self.output.configure(state=tk.NORMAL)
        self.output.insert(tk.END, text)
        self.output.see(tk.END)
        self.output.configure(state=tk.DISABLED)

    def _log_from_thread(self, text: str):
        self.root.after(0, self._append_log, text)

    def _set_status_from_thread(self, text: str):
        self.root.after(0, self.status_var.set, text)

    def _set_progress_from_thread(self, current: int, total: int):
        """Update progress bar and step label from background thread."""
        pct = (current / total) * 100 if total else 0
        self.root.after(0, self.progress_var.set, pct)
        self.root.after(0, self.progress_label.configure,
                        {"text": f"Step {current}/{total}"})

    # ------------------------------------------------------------------
    # PDB selection + pipeline launch
    # ------------------------------------------------------------------

    def _on_select_pdb(self):
        if self._running:
            return

        filepath = filedialog.askopenfilename(
            title="Select a PDB file",
            filetypes=[("PDB files", "*.pdb"), ("All files", "*.*")],
        )
        if not filepath:
            return

        pdb_path = Path(filepath)
        if not pdb_path.is_file():
            self.status_var.set("Error — selected file does not exist")
            return

        # Clear previous output
        self.output.configure(state=tk.NORMAL)
        self.output.delete("1.0", tk.END)
        self.output.configure(state=tk.DISABLED)

        # Reset progress
        self.progress_var.set(0)
        self.progress_label.configure(text="Step 0/8")

        self._append_log(f"Selected: {pdb_path}\n\n")
        self.status_var.set("Starting pipeline…")

        # Disable / enable buttons
        self._running = True
        self.btn_select.configure(state=tk.DISABLED)
        self.btn_settings.configure(state=tk.DISABLED)
        self.btn_cancel.configure(state=tk.NORMAL)
        self.btn_open_folder.configure(state=tk.DISABLED)

        gmx_bin = self.config.get("gmx_path", "gmx")
        ff_key = self.ff_var.get()

        self.pipeline = GromacsPipeline(
            pdb_path=pdb_path,
            ff_key=ff_key,
            gmx_bin=gmx_bin,
            log_callback=self._log_from_thread,
            status_callback=self._set_status_from_thread,
            progress_callback=self._set_progress_from_thread,
        )

        thread = threading.Thread(target=self._run_pipeline, daemon=True)
        thread.start()

    def _run_pipeline(self):
        success = False
        try:
            success = self.pipeline.run()
        except Exception as exc:
            self._log_from_thread(f"\n✗ Fatal error: {exc}\n")
            self._set_status_from_thread(f"Error — {exc}")
        finally:
            self._running = False
            self._last_run_dir = self.pipeline.run_dir if self.pipeline else None

            def _restore_ui():
                self.btn_select.configure(state=tk.NORMAL)
                self.btn_settings.configure(state=tk.NORMAL)
                self.btn_cancel.configure(state=tk.DISABLED)
                if self._last_run_dir and self._last_run_dir.is_dir():
                    self.btn_open_folder.configure(state=tk.NORMAL)

            self.root.after(0, _restore_ui)


# =============================================================================
# Entry point
# =============================================================================

def main():
    root = tk.Tk()
    GromacsRunnerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
