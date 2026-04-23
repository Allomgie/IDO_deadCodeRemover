#!/usr/bin/env python3
"""
Dead-Code-Entfernung via Delta Debugging mit IDO-Compiler-Orakel.

Strategie:
  1. Kompiliere Original → Baseline Assembly-Hash
  2. Entferne AST-Knoten (Statements, Deklarationen, Globals)
  3. Kompiliere erneut → Neuer Hash
  4. Hash identisch? → Knoten war Dead Code, entfernen
  5. Hash verschieden? → Knoten ist live, zurücksetzen

Phasen:
  Phase 1: Top-Level (globale Variablen, Forward-Deklarationen)
  Phase 2: Funktionskörper (ganze Blöcke zuerst, dann einzelne Statements)

Sicherheit:
  - Originaldateien werden NIE verändert
  - Ergebnisse landen in einem separaten Optimized/-Ordner
  - Dry-Run-Modus
  - JSONL-Log jeder Änderung
"""

import os
import sys
import re
import subprocess
import signal
import tempfile
import shutil
import hashlib
import json
import time
import argparse
import multiprocessing
from datetime import datetime
from tqdm import tqdm
from pycparser import c_parser, c_generator, c_ast


# =====================================================================
#  SAFE SUBPROCESS EXECUTION
# =====================================================================

def run_cmd_safely(cmd, cwd=None, env=None, timeout=30):
    """
    Führt einen Befehl aus und beendet bei Timeout die KOMPLETTE Prozessgruppe.
    
    IDO spawnt intern Kindprozesse (cfe, as, etc.). subprocess.run() killt
    bei Timeout nur den Hauptprozess — die Kinder laufen weiter und halten
    gelöschte tmp-Dateien offen, was die Festplatte füllt.
    
    start_new_session=True packt den Prozess in eine eigene Gruppe,
    os.killpg killt dann alle Kinder mit.
    """
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True
        )
        stdout, stderr = proc.communicate(timeout=timeout)
        return proc.returncode, stdout, stderr

    except subprocess.TimeoutExpired:
        # SIGKILL an die gesamte Prozessgruppe
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass
        # Pipes leeren um Deadlocks zu vermeiden
        try:
            proc.communicate(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            proc.kill()
        raise


def check_disk_space(min_free_gb=2):
    """
    Pausiert falls die Festplatte fast voll ist.
    Verhindert WSL-Abstürze bei vollem VHDX.
    """
    while True:
        usage = shutil.disk_usage("/")
        free_gb = usage.free / (1024 ** 3)
        if free_gb >= min_free_gb:
            return
        tqdm.write(
            f"  [WARNUNG] Nur noch {free_gb:.1f} GB frei! "
            f"Pausiere 30s (min: {min_free_gb} GB)..."
        )
        time.sleep(30)


# --- KONFIGURATION ---
BASE_DIR      = "/home/user/deadCodeRemover"
PROJECT_ROOT  = os.path.join(BASE_DIR, "CompilerRoot")
IDO_DIR       = os.path.abspath(os.path.join(PROJECT_ROOT, "tools", "ido"))
IDO_CC        = os.path.join(IDO_DIR, "cc")

DATASET_DIR   = os.path.join(BASE_DIR, "dataset_Stage_0")
OPTIMIZED_DIR = os.path.join(DATASET_DIR, "dataset_Stage_1")

GROUPS = [
    "Input_Group",
]

# Include-Pfade für den Präprozessor
INCLUDE_DIRS = [
    os.path.join(PROJECT_ROOT, "include"),
    os.path.join(PROJECT_ROOT, "src"),
    os.path.join(PROJECT_ROOT, "include", "PR"),
    os.path.join(PROJECT_ROOT, "lib", "ultralib", "include"),
    os.path.join(BASE_DIR, "csmith_install/include/csmith-2.3.0"),
]

# RAM-Disk für temporäre Dateien — schont die SSD
TMP_ROOT = "/dev/shm"

# objdump statt spimdisasm — 30x schneller (5ms vs 158ms)
OBJDUMP = "mips-linux-gnu-objdump"

# ASM-Hash-Cache: Vermeidet doppelte IDO-Aufrufe für identischen C-Code
_asm_cache = {}
_asm_cache_lock = None  # Wird im Worker initialisiert


# =====================================================================
#  COMPILER-ORAKEL
# =====================================================================

def _get_ido_env():
    """IDO-Umgebungsvariablen."""
    env = os.environ.copy()
    env["COMPILER_PATH"] = IDO_DIR
    env["LD_LIBRARY_PATH"] = f"{IDO_DIR}:{env.get('LD_LIBRARY_PATH', '')}"
    return env

_IDO_ENV = _get_ido_env()


def compile_to_asm_hash(c_source: str, tmp_dir: str, header_dir: str,
                    name: str = "input") -> tuple[str | None, str]:
    """
    Erweiterte Version: Prüft Instruktionen UND Daten-Sektionen.
    Verhindert, dass Stage 1 globale Daten oder Strings wegkürzt.
    """
    c_path = os.path.join(tmp_dir, f"{name}.c")
    i_path = os.path.join(tmp_dir, f"{name}.i")
    o_path = os.path.join(tmp_dir, f"{name}.o")

    def _rm(p):
        try:
            if os.path.exists(p): os.unlink(p)
        except OSError: pass

    try:
        # Cache-Check
        fixed_source = re.sub(r'#include\s+"[^"]*?([^/"]+\.h)"', r'#include "\1"', c_source)
        cache_key = hashlib.md5(fixed_source.encode()).hexdigest()
        if cache_key in _asm_cache:
            return _asm_cache[cache_key]

        with open(c_path, "w", encoding="utf-8") as f:
            f.write(fixed_source)

        # 1. Preprocess
        cmd_cpp = ["gcc", "-E", "-P", "-xc", "-D_LANGUAGE_C", "-D_MIPS_SZLONG=32"]
        for inc in INCLUDE_DIRS: cmd_cpp += ["-I", inc]
        if header_dir: cmd_cpp += ["-I", header_dir]
        cmd_cpp += [c_path, "-o", i_path]
        rc, _, _ = run_cmd_safely(cmd_cpp, timeout=30)
        _rm(c_path)
        if rc != 0: return None, "gcc -E failed"

        # 2. IDO Compile
        cmd_ido = [IDO_CC, "-c", "-O2", "-mips2", "-G", "0", "-w", i_path, "-o", o_path]
        rc, _, _ = run_cmd_safely(cmd_ido, cwd=tmp_dir, env=_IDO_ENV, timeout=30)
        _rm(i_path)
        if rc != 0: return None, "IDO fail"

        # 3. EXTRAKTION TEIL A: Instruktionen (.text)
        cmd_obj_d = [OBJDUMP, "-d", "-z", o_path]
        rc, stdout_text, _ = run_cmd_safely(cmd_obj_d, timeout=30)
        if rc != 0: return None, "objdump -d failed"

        # 4. EXTRAKTION TEIL B: Daten (.rodata, .data, .bss)
        cmd_obj_s = [OBJDUMP, "-s", "-j", ".rodata", "-j", ".data", "-j", ".bss", o_path]
        rc2, stdout_data, _ = run_cmd_safely(cmd_obj_s, timeout=30)
        # rc2 kann ungleich 0 sein, wenn Sektionen fehlen -> ignorieren wir
        _rm(o_path)

        asm_payload = []

        # Normalisierung des Code-Teils
        for line in stdout_text.decode(errors="replace").splitlines():
            m = re.match(r'^\s*[0-9a-fA-F]+:\s+[0-9a-fA-F]+\s+(.*)', line)
            if not m: continue
            s = m.group(1).strip().split('#')[0].strip()
            if not s: continue
            # Deine Stack-Normalisierung
            s = re.sub(r'addiu\s+\$?(sp|29),\s*\$?(sp|29),\s*-?[0-9a-fA-F]+', 'addiu sp,sp,OFFSET', s)
            s = re.sub(r'-?[0-9a-fA-F]+\(\$?(sp|29)\)', 'OFFSET(sp)', s)
            s = re.sub(r'-?[0-9a-fA-F]+\(\$?(fp|30)\)', 'OFFSET(fp)', s)
            s = re.sub(r'%[a-z0-9_.]+\([^)]+\)', 'SYMBOL', s)
            asm_payload.append(s)

        # Rohdaten-Teil hinzufügen
        if rc2 == 0 and stdout_data:
            for line in stdout_data.decode(errors="replace").splitlines():
                m = re.match(r'^\s*[0-9a-fA-F]+\s+((?:[0-9a-fA-F]+\s*)+)', line)
                if m:
                    asm_payload.append("DATA:" + m.group(1).strip())

        if not asm_payload:
            return None, "empty output"

        res_hash = hashlib.md5("\n".join(asm_payload).encode()).hexdigest()
        _asm_cache[cache_key] = (res_hash, "")
        return res_hash, ""

    except Exception as e:
        return None, str(e)


# =====================================================================
#  AST-HILFSFUNKTIONEN
# =====================================================================

def _extract_includes(source: str) -> tuple[list[str], str]:
    """Trennt #include-Zeilen vom Rest des Quelltexts."""
    includes = []
    code_lines = []
    for line in source.splitlines():
        if re.match(r'^\s*#', line):
            # Include-Pfade normalisieren
            fixed = re.sub(
                r'#include\s+"[.][.]/[^"]*?([^/"]+\.h)"',
                r'#include "\1"',
                line
            )
            includes.append(fixed)
        else:
            code_lines.append(line)
    return includes, "\n".join(code_lines)


def _preprocess_for_parsing(c_source: str, header_dir: str,
                            tmp_dir: str) -> tuple[str | None, str]:
    """
    Lässt gcc -E über den Quelltext laufen, damit pycparser
    vorverarbeiteten Code ohne unbekannte Types bekommt.
    
    Rückgabe: (vorverarbeiteter_code, tmp_dateiname) oder (None, "").
    """
    tmp_c = os.path.join(tmp_dir, "parse_input.c")
    tmp_i = os.path.join(tmp_dir, "parse_input.i")

    # Include-Pfade normalisieren: "../headers/foo.h" → "foo.h"
    # damit gcc sie über -I findet
    fixed_source = re.sub(
        r'#include\s+"[^"]*?([^/"]+\.h)"',
        r'#include "\1"',
        c_source
    )

    with open(tmp_c, "w", encoding="utf-8") as f:
        f.write(fixed_source)

    cmd = [
        "gcc", "-E", "-xc",
        "-D_LANGUAGE_C", "-D_MIPS_SZLONG=32",
        "-D__attribute__(x)=", "-D__extension__=",
    ]
    for inc in INCLUDE_DIRS:
        cmd += ["-I", inc]
    if header_dir:
        cmd += ["-I", header_dir]
    cmd += [tmp_c, "-o", tmp_i]

    try:
        rc, _, _ = run_cmd_safely(cmd, timeout=30)
    except subprocess.TimeoutExpired:
        return None, ""
    if rc != 0:
        return None, ""

    with open(tmp_i, "r", encoding="utf-8", errors="replace") as f:
        preprocessed = f.read()

    # Entferne # line-Direktiven für pycparser
    lines = [l for l in preprocessed.splitlines() if not l.lstrip().startswith("#")]
    return "\n".join(lines), "parse_input.c"


def _filter_ast_to_original(ast, original_src: str) -> None:
    """
    Entfernt AST-Knoten, die aus Headers stammen (expandiert durch gcc -E).
    Behält nur Knoten die aus der originalen .c-Datei kommen.
    
    Heuristik: Behalte FuncDef-Knoten und Decl-Knoten, deren Name
    im originalen Quelltext vorkommt.
    """
    if not ast.ext:
        return

    # Sammle alle Bezeichner die im Original vorkommen
    original_names = set(re.findall(r'\b([a-zA-Z_]\w*)\b', original_src))

    # Filtere: Behalte nur Knoten die relevant sind
    filtered = []
    for node in ast.ext:
        # FuncDef: Behalten wenn der Name im Original vorkommt
        if isinstance(node, c_ast.FuncDef):
            if node.decl.name in original_names:
                filtered.append(node)
            continue

        # Decl (globale Variablen, Forward-Decls): 
        # Behalten wenn der Name im Original vorkommt
        if isinstance(node, c_ast.Decl):
            if node.name and node.name in original_names:
                filtered.append(node)
            continue

        # Typedefs: Nur Standard-Types behalten (aus unserer Preamble)
        if isinstance(node, c_ast.Typedef):
            # Skip — werden beim Code-Generieren ohnehin gefiltert
            continue

        # Alles andere (Pragmas etc.): Skip
    
    ast.ext = filtered


def _parse_to_ast(source_with_includes: str, header_dir: str = "",
                  tmp_dir: str = "") -> c_ast.FileAST | None:
    """
    Parst C-Code zu einem AST.
    
    Strategie:
      1. gcc -E vorverarbeiten (löst alle Custom Types auf)
      2. pycparser parst den vorverarbeiteten Code
      3. AST auf Knoten aus der Originaldatei filtern
      4. Fallback: Direkt mit Typedef-Preamble versuchen
    """
    parser = c_parser.CParser()

    # --- Versuch 1: Mit gcc -E vorverarbeiten ---
    if tmp_dir and header_dir is not None:
        preprocessed, _ = _preprocess_for_parsing(
            source_with_includes, header_dir, tmp_dir
        )
        if preprocessed:
            try:
                ast = parser.parse(preprocessed)
                _filter_ast_to_original(ast, source_with_includes)
                return ast
            except RecursionError:
                return None
            except Exception:
                pass  # Fallback versuchen

    # --- Versuch 2: Direkt mit Typedef-Preamble ---
    # Includes entfernen für direkte Verarbeitung
    lines_no_includes = [l for l in source_with_includes.splitlines()
                         if not re.match(r'^\s*#', l)]
    cleaned = "\n".join(lines_no_includes)

    preambles = [
        ("typedef signed char s8; typedef unsigned char u8; "
         "typedef signed short s16; typedef unsigned short u16; "
         "typedef signed int s32; typedef unsigned int u32; "
         "typedef signed long long s64; typedef unsigned long long u64; "
         "typedef float f32; typedef double f64; "
         "typedef unsigned int size_t; typedef unsigned int uint; "
         "typedef int bool; "),
        ("typedef signed char s8; typedef unsigned char u8; "
         "typedef signed short s16; typedef unsigned short u16; "
         "typedef signed int s32; typedef unsigned int u32; "
         "typedef signed long long s64; typedef unsigned long long u64; "
         "typedef float f32; typedef double f64; "
         "typedef int bool; "),
    ]
    for preamble in preambles:
        try:
            return parser.parse(preamble + cleaned)
        except RecursionError:
            return None
        except Exception:
            continue
    return None


def _ast_to_source(ast, includes: list[str]) -> str | None:
    """Generiert C-Quelltext aus AST + originalen Includes.
    Rückgabe: None bei RecursionError (AST zu tief)."""
    gen = c_generator.CGenerator()
    try:
        code = gen.visit(ast)
    except RecursionError:
        return None
    # Typedefs aus der Preamble entfernen
    clean_lines = [l for l in code.splitlines() if not l.startswith("typedef ")]
    return "\n".join(includes) + "\n\n" + "\n".join(clean_lines)


def _count_ast_nodes(ast) -> int:
    """Zählt die Anzahl entfernbarer Knoten im AST."""
    count = 0
    # Top-Level
    count += len(ast.ext) if ast.ext else 0
    # Statements in Funktionskörpern
    for node in (ast.ext or []):
        if isinstance(node, c_ast.FuncDef) and node.body and node.body.block_items:
            count += len(node.body.block_items)
    return count


# =====================================================================
#  DELTA DEBUGGING
# =====================================================================

def delta_debug_file(c_filepath: str, header_dir: str,
                     output_path: str, dry_run: bool = False) -> dict:
    """
    Führt Delta Debugging auf einer C-Datei durch.
    
    Entfernt systematisch AST-Knoten und prüft ob sich das Assembly ändert.
    """
    result = {
        "file": c_filepath,
        "status": "clean",
        "removed_top_level": [],
        "removed_statements": [],
        "error": None,
    }

    filename = os.path.basename(c_filepath)
    name_no_ext = os.path.splitext(filename)[0]

    # --- Originaldatei lesen ---
    with open(c_filepath, "r", encoding="utf-8", errors="replace") as f:
        original_src = f.read()

    includes, cleaned_src = _extract_includes(original_src)

    # --- Tmp-Verzeichnis (in RAM: /tmp) ---
    tmp_dir = tempfile.mkdtemp(dir=TMP_ROOT, prefix=f"delta_{name_no_ext}_")

    try:
        # --- Baseline: Original kompilieren ---
        baseline_code = _ast_to_source_raw(includes, cleaned_src)
        baseline_hash, baseline_err = compile_to_asm_hash(baseline_code, tmp_dir, header_dir)

        if baseline_hash is None:
            result["status"] = "error"
            result["error"] = f"Baseline kompiliert nicht: {baseline_err}"
            return result

        # --- AST parsen (mit gcc -E Vorverarbeitung) ---
        ast = _parse_to_ast(original_src, header_dir, tmp_dir)
        if ast is None:
            result["status"] = "error"
            result["error"] = "AST Parse-Fehler"
            return result

        # --- Baseline NEU berechnen vom regenerierten Code ---
        # pycparser's Code-Generator erzeugt minimal anderen Code als das
        # Original (Klammern, Spaces, Float-Literale). Deshalb muss die
        # Baseline vom REGENERIERTEN Code kommen, nicht vom Original.
        # So vergleichen wir Äpfel mit Äpfeln.
        regen_code = _ast_to_source(ast, includes)
        if regen_code is None:
            result["status"] = "error"
            result["error"] = "RecursionError bei Code-Regenerierung"
            return result

        regen_hash, regen_err = compile_to_asm_hash(regen_code, tmp_dir, header_dir)
        if regen_hash is None:
            # Regenerierter Code kompiliert nicht → pycparser hat was kaputt gemacht
            # Fallback: Original-Baseline verwenden
            regen_hash = baseline_hash

        # Wenn der regenerierte Code anderen ASM erzeugt als das Original,
        # können wir nicht sicher Delta-Debuggen → Original kopieren
        if regen_hash != baseline_hash:
            result["status"] = "clean"
            if not dry_run:
                shutil.copy2(c_filepath, output_path)
            return result

        # Ab hier: baseline_hash == regen_hash, alles konsistent

        total_before = _count_ast_nodes(ast)
        changes_made = False

        # =============================================================
        # ITERATIVER MULTI-PASS
        # Alle Phasen laufen in einer Schleife bis keine Änderungen
        # mehr passieren. Nötig weil:
        #   - Phase 2b leert einen while-Body (innere Statements weg)
        #   - Dann kann Phase 2 den jetzt leeren while entfernen
        #   - Phase 3 findet neue Dead Stores nach Entfernungen
        # Ohne Multi-Pass bleibt Phantom-Code stehen!
        # =============================================================
        pass_num = 0
        max_passes = 5

        while pass_num < max_passes:
            pass_num += 1
            pass_changed = False

            # === PHASE 1: Top-Level (globale Vars, Forward-Decls) ===
            if ast.ext:
                i = len(ast.ext) - 1
                while i >= 0:
                    node = ast.ext[i]
                    if isinstance(node, (c_ast.FuncDef, c_ast.Typedef)):
                        i -= 1
                        continue
                    removed = ast.ext.pop(i)
                    test_code = _ast_to_source(ast, includes)
                    if test_code is None:
                        ast.ext.insert(i, removed)
                        i -= 1
                        continue
                    test_hash, _ = compile_to_asm_hash(test_code, tmp_dir, header_dir)
                    if test_hash == baseline_hash:
                        name = removed.name if isinstance(removed, c_ast.Decl) else \
                               getattr(removed, 'name', removed.__class__.__name__)
                        result["removed_top_level"].append(name or removed.__class__.__name__)
                        changes_made = True
                        pass_changed = True
                    else:
                        ast.ext.insert(i, removed)
                    i -= 1

            # === PHASE 2+2b: Statements + verschachtelte Blöcke ===
            for ext_node in (ast.ext or []):
                if not isinstance(ext_node, c_ast.FuncDef):
                    continue
                if not ext_node.body or not ext_node.body.block_items:
                    continue

                func_name = ext_node.decl.name
                items = ext_node.body.block_items

                # Phase 2: Top-Level Statements in Funktion
                i = len(items) - 1
                while i >= 0:
                    removed = items.pop(i)
                    test_code = _ast_to_source(ast, includes)
                    if test_code is None:
                        items.insert(i, removed)
                        i -= 1
                        continue
                    test_hash, _ = compile_to_asm_hash(test_code, tmp_dir, header_dir)
                    if test_hash == baseline_hash:
                        desc = _describe_node(removed)
                        result["removed_statements"].append(f"{func_name}: {desc}")
                        changes_made = True
                        pass_changed = True
                    else:
                        items.insert(i, removed)
                    i -= 1

                # Phase 2b: In verschachtelte Blöcke hineingehen
                if _minimize_nested(items, ast, includes, baseline_hash,
                                    tmp_dir, header_dir, func_name, result):
                    changes_made = True
                    pass_changed = True

            # === PHASE 3: Gekoppelte Entfernungen (Dead Stores) ===
            # Sucht lokale Variablen deren Referenzen (auch in verschachtelten
            # Blöcken wie while/if) keinen ASM-Effekt haben.
            # Schritt 1: Einzelne Variablen + Referenzen entfernen
            # Schritt 2: Gruppen von Variablen die nur sich gegenseitig referenzieren
            for ext_node in (ast.ext or []):
                if not isinstance(ext_node, c_ast.FuncDef):
                    continue
                if not ext_node.body or not ext_node.body.block_items:
                    continue

                func_name = ext_node.decl.name
                items = ext_node.body.block_items
                gen = c_generator.CGenerator()

                # Code jedes Top-Level Items generieren (inkl. verschachtelte Blöcke)
                def _gen_item_codes():
                    codes = []
                    for item in items:
                        try:
                            codes.append(gen.visit(item))
                        except RecursionError:
                            codes.append("")
                    return codes

                item_codes = _gen_item_codes()

                # Lokale Variablen sammeln
                local_var_names = set()
                for item in items:
                    if isinstance(item, c_ast.Decl) and item.name:
                        local_var_names.add(item.name)

                if not local_var_names:
                    continue

                # --- Schritt 1: Einzelne Variable + alle referenzierenden Items ---
                for var_name in list(local_var_names):
                    ref_indices = []
                    for idx, code in enumerate(item_codes):
                        if re.search(r'\b' + re.escape(var_name) + r'\b', code):
                            ref_indices.append(idx)

                    if len(ref_indices) < 2:
                        continue

                    removed_items = []
                    for idx in sorted(ref_indices, reverse=True):
                        if idx < len(items):
                            removed_items.append((idx, items.pop(idx)))

                    test_code = _ast_to_source(ast, includes)
                    if test_code is None:
                        for idx, item in sorted(removed_items):
                            items.insert(idx, item)
                        continue

                    test_hash, _ = compile_to_asm_hash(test_code, tmp_dir, header_dir)
                    if test_hash == baseline_hash:
                        result["removed_statements"].append(
                            f"{func_name}: Dead Store '{var_name}' "
                            f"({len(removed_items)} Statements)")
                        changes_made = True
                        pass_changed = True
                        item_codes = _gen_item_codes()  # Aktualisieren
                    else:
                        for idx, item in sorted(removed_items):
                            items.insert(idx, item)

                # --- Schritt 2: Gruppen-Entfernung ---
                # Alle verbleibenden lokalen Variablen + alles was sie referenziert
                # als Gruppe entfernen (fängt while-Ketten mit sp30/sp34/sp38/... ab)
                item_codes = _gen_item_codes()
                remaining_vars = set()
                for item in items:
                    if isinstance(item, c_ast.Decl) and item.name:
                        remaining_vars.add(item.name)

                if len(remaining_vars) >= 2:
                    group_indices = set()
                    for var_name in remaining_vars:
                        for idx, code in enumerate(item_codes):
                            if re.search(r'\b' + re.escape(var_name) + r'\b', code):
                                group_indices.add(idx)

                    if group_indices:
                        removed_items = []
                        for idx in sorted(group_indices, reverse=True):
                            if idx < len(items):
                                removed_items.append((idx, items.pop(idx)))

                        test_code = _ast_to_source(ast, includes)
                        if test_code is not None:
                            test_hash, _ = compile_to_asm_hash(test_code, tmp_dir, header_dir)
                            if test_hash == baseline_hash:
                                var_list = ", ".join(sorted(remaining_vars))
                                result["removed_statements"].append(
                                    f"{func_name}: Dead Store Group [{var_list}] "
                                    f"({len(removed_items)} Statements)")
                                changes_made = True
                                pass_changed = True
                            else:
                                for idx, item in sorted(removed_items):
                                    items.insert(idx, item)
                        else:
                            for idx, item in sorted(removed_items):
                                items.insert(idx, item)

            # Kein Fortschritt → fertig
            if not pass_changed:
                break

        # === ERGEBNIS ===
        if not changes_made:
            result["status"] = "clean"
            if not dry_run:
                # Auch saubere Dateien in den Output kopieren
                shutil.copy2(c_filepath, output_path)
            return result

        total_removed = len(result["removed_top_level"]) + len(result["removed_statements"])
        result["status"] = "would_minimize" if dry_run else "minimized"

        if not dry_run:
            final_code = _ast_to_source(ast, includes)

            if final_code is None:
                result["status"] = "error"
                result["error"] = "RecursionError beim finalen Code-Generieren"
                shutil.copy2(c_filepath, output_path)
                return result

            # Sicherheitscheck: Kompiliert das Ergebnis noch?
            final_hash, _ = compile_to_asm_hash(final_code, tmp_dir, header_dir)
            if final_hash != baseline_hash:
                result["status"] = "error"
                result["error"] = "Finaler Hash stimmt nicht (sollte nicht passieren)"
                shutil.copy2(c_filepath, output_path)
                return result

            with open(output_path, "w", encoding="utf-8") as f:
                f.write(final_code)

        return result

    except RecursionError:
        result["status"] = "error"
        result["error"] = "RecursionError: AST zu tief verschachtelt (csmith/YARPGen)"
        # Original in Output kopieren damit die Datei nicht fehlt
        try:
            shutil.copy2(c_filepath, output_path)
        except Exception:
            pass
        return result

    except Exception as e:
        result["status"] = "error"
        result["error"] = str(e)
        return result

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _minimize_nested(items: list, ast, includes: list[str],
                     baseline_hash: str, tmp_dir: str, header_dir: str,
                     func_name: str, result: dict) -> bool:
    """
    Rekursiv: Versucht Statements innerhalb von verschachtelten Blöcken zu entfernen.
    Geht in if/else/for/while/switch/compound Blöcke hinein.
    Rückgabe: True wenn mindestens eine Änderung gemacht wurde.
    """
    any_changed = False
    for item in items:
        # If-Statement: iftrue und iffalse Blöcke
        if isinstance(item, c_ast.If):
            if item.iftrue and isinstance(item.iftrue, c_ast.Compound) and item.iftrue.block_items:
                if _try_remove_from_block(
                    item.iftrue.block_items, ast, includes,
                    baseline_hash, tmp_dir, header_dir, func_name, result
                ):
                    any_changed = True
                if _minimize_nested(
                    item.iftrue.block_items, ast, includes,
                    baseline_hash, tmp_dir, header_dir, func_name, result
                ):
                    any_changed = True
            if item.iffalse and isinstance(item.iffalse, c_ast.Compound) and item.iffalse.block_items:
                if _try_remove_from_block(
                    item.iffalse.block_items, ast, includes,
                    baseline_hash, tmp_dir, header_dir, func_name, result
                ):
                    any_changed = True
                if _minimize_nested(
                    item.iffalse.block_items, ast, includes,
                    baseline_hash, tmp_dir, header_dir, func_name, result
                ):
                    any_changed = True

        # For/While/DoWhile: Body
        elif isinstance(item, (c_ast.For, c_ast.While, c_ast.DoWhile)):
            body = item.stmt if hasattr(item, 'stmt') else None
            if body and isinstance(body, c_ast.Compound) and body.block_items:
                if _try_remove_from_block(
                    body.block_items, ast, includes,
                    baseline_hash, tmp_dir, header_dir, func_name, result
                ):
                    any_changed = True
                if _minimize_nested(
                    body.block_items, ast, includes,
                    baseline_hash, tmp_dir, header_dir, func_name, result
                ):
                    any_changed = True

        # Switch: Cases
        elif isinstance(item, c_ast.Switch):
            if item.stmt and isinstance(item.stmt, c_ast.Compound) and item.stmt.block_items:
                if _try_remove_from_block(
                    item.stmt.block_items, ast, includes,
                    baseline_hash, tmp_dir, header_dir, func_name, result
                ):
                    any_changed = True
                for case_item in item.stmt.block_items:
                    if isinstance(case_item, (c_ast.Case, c_ast.Default)):
                        if case_item.stmts:
                            if _try_remove_from_block(
                                case_item.stmts, ast, includes,
                                baseline_hash, tmp_dir, header_dir,
                                func_name, result
                            ):
                                any_changed = True
                            if _minimize_nested(
                                case_item.stmts, ast, includes,
                                baseline_hash, tmp_dir, header_dir,
                                func_name, result
                            ):
                                any_changed = True

        # Compound (verschachtelter Block)
        elif isinstance(item, c_ast.Compound) and item.block_items:
            if _try_remove_from_block(
                item.block_items, ast, includes,
                baseline_hash, tmp_dir, header_dir, func_name, result
            ):
                any_changed = True
            if _minimize_nested(
                item.block_items, ast, includes,
                baseline_hash, tmp_dir, header_dir, func_name, result
            ):
                any_changed = True

    return any_changed


def _try_remove_from_block(block_items: list, ast, includes: list[str],
                           baseline_hash: str, tmp_dir: str, header_dir: str,
                           func_name: str, result: dict) -> bool:
    """Versucht einzelne Statements aus einem Block zu entfernen.
    Rückgabe: True wenn mindestens ein Statement entfernt wurde."""
    any_removed = False
    i = len(block_items) - 1
    while i >= 0:
        removed = block_items.pop(i)

        test_code = _ast_to_source(ast, includes)

        if test_code is None:
            block_items.insert(i, removed)
            i -= 1
            continue

        test_hash, _ = compile_to_asm_hash(test_code, tmp_dir, header_dir)

        if test_hash == baseline_hash:
            desc = _describe_node(removed)
            result["removed_statements"].append(f"{func_name}: {desc}")
            any_removed = True
        else:
            block_items.insert(i, removed)

        i -= 1
    return any_removed


def _ast_to_source_raw(includes: list[str], cleaned_src: str) -> str:
    """Baut den Quelltext zusammen ohne AST (für Baseline)."""
    return "\n".join(includes) + "\n\n" + cleaned_src


def _describe_node(node) -> str:
    """Gibt eine kurze Beschreibung eines AST-Knotens."""
    if isinstance(node, c_ast.Decl):
        return f"Decl: {node.name or '?'}"
    if isinstance(node, c_ast.FuncCall):
        name = getattr(node.name, 'name', '?') if node.name else '?'
        return f"Call: {name}()"
    if isinstance(node, c_ast.Assignment):
        return f"Assign: {node.op}"
    if isinstance(node, c_ast.Return):
        return "return"
    if isinstance(node, c_ast.If):
        return "if-block"
    if isinstance(node, c_ast.For):
        return "for-loop"
    if isinstance(node, c_ast.While):
        return "while-loop"
    if isinstance(node, c_ast.Switch):
        return "switch"
    if isinstance(node, c_ast.Compound):
        n = len(node.block_items) if node.block_items else 0
        return f"block ({n} items)"
    return node.__class__.__name__


# =====================================================================
#  WORKER & MAIN
# =====================================================================

def _worker_init():
    import signal
    import resource
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    # Core-Dumps deaktivieren
    try:
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except (ValueError, resource.error):
        pass
    # Rekursionslimit erhöhen für tief verschachtelte csmith/YARPGen-Ausdrücke
    sys.setrecursionlimit(50_000)


def _get_header_dir(c_filepath: str) -> str:
    """Findet den passenden Header-Ordner."""
    for group in GROUPS:
        if group in c_filepath:
            return os.path.join(DATASET_DIR, f"{group}_headers")
    return ""


def _worker_fn(args):
    c_filepath, output_path, dry_run = args
    # Notfall-Bremse: Pausiere wenn Festplatte fast voll
    check_disk_space(min_free_gb=2)
    header_dir = _get_header_dir(c_filepath)
    return delta_debug_file(c_filepath, header_dir, output_path, dry_run)


def main():
    ap = argparse.ArgumentParser(
        description="Dead-Code-Entfernung via Delta Debugging mit IDO-Compiler"
    )
    ap.add_argument(
        "--dry-run", action="store_true",
        help="Zeigt nur was passieren würde",
    )
    ap.add_argument(
        "--group", type=str, default=None,
        help="Nur eine bestimmte Gruppe verarbeiten",
    )
    ap.add_argument(
        "-j", "--workers", type=int, default=None,
        help="Anzahl paralleler Worker (Default: CPU-Kerne)",
    )
    ap.add_argument(
        "--diagnose", type=str, default=None,
        help="Einzelne Datei analysieren",
    )
    ap.add_argument(
        "--output-dir", type=str, default=OPTIMIZED_DIR,
        help=f"Ausgabe-Verzeichnis (Default: {OPTIMIZED_DIR})",
    )
    ap.add_argument(
        "--overwrite", action="store_true",
        help="Bereits optimierte Dateien erneut verarbeiten (Default: überspringen)",
    )
    args = ap.parse_args()

    output_dir = args.output_dir
    num_workers = args.workers or multiprocessing.cpu_count()

    # --- Diagnose-Modus ---
    if args.diagnose:
        c_path = args.diagnose
        header_dir = _get_header_dir(c_path)
        out_path = os.path.join(output_dir, os.path.basename(c_path))

        print(f"=== DIAGNOSE: {c_path} ===")
        print(f"Header-Dir: {header_dir}")
        print(f"Output: {out_path}\n")

        res = delta_debug_file(c_path, header_dir, out_path, dry_run=True)

        print(f"Status: {res['status']}")
        if res.get('error'):
            print(f"Error: {res['error']}")
        if res['removed_top_level']:
            print(f"\nEntfernbare Top-Level Knoten:")
            for name in res['removed_top_level']:
                print(f"  → {name}")
        if res['removed_statements']:
            print(f"\nEntfernbare Statements:")
            for desc in res['removed_statements']:
                print(f"  → {desc}")

        total = len(res['removed_top_level']) + len(res['removed_statements'])
        print(f"\nGesamt entfernbar: {total}")
        return

    # --- Gruppen verarbeiten ---
    groups_to_process = GROUPS
    if args.group:
        if args.group not in GROUPS:
            print(f"Fehler: Gruppe '{args.group}' nicht bekannt.")
            sys.exit(1)
        groups_to_process = [args.group]

    os.makedirs(output_dir, exist_ok=True)

    # Tasks sammeln
    all_tasks = []
    skipped_existing = 0
    for group in groups_to_process:
        input_dir = os.path.join(DATASET_DIR, group)
        group_output = os.path.join(output_dir, group)

        if not os.path.isdir(input_dir):
            print(f"  [!] Nicht gefunden: {input_dir}")
            continue

        os.makedirs(group_output, exist_ok=True)

        for fname in os.listdir(input_dir):
            if fname.endswith(".c"):
                c_path = os.path.join(input_dir, fname)
                out_path = os.path.join(group_output, fname)

                # Bereits verarbeitete Dateien überspringen
                if not args.overwrite and os.path.exists(out_path):
                    skipped_existing += 1
                    continue

                all_tasks.append((c_path, out_path, args.dry_run))

    print(f"Gefunden: {len(all_tasks) + skipped_existing} C-Dateien")
    if skipped_existing > 0:
        print(f"Übersprungen: {skipped_existing} (bereits im Output vorhanden)")
    print(f"Zu verarbeiten: {len(all_tasks)}")
    print(f"Worker:   {num_workers}")
    print(f"Output:   {output_dir}")
    print(f"Methode:  Delta Debugging mit IDO-Compiler-Orakel")
    if args.dry_run:
        print("=== DRY RUN ===\n")

    if not all_tasks:
        print("\nKeine Dateien zu verarbeiten.")
        return

    # --- Aufräumen: Verwaiste tmp-Verzeichnisse vom letzten Lauf ---
    stale_count = 0
    tmp_root = TMP_ROOT
    try:
        for d in os.listdir(tmp_root):
            if d.startswith("delta_") and os.path.isdir(os.path.join(tmp_root, d)):
                shutil.rmtree(os.path.join(tmp_root, d), ignore_errors=True)
                stale_count += 1
        if stale_count:
            print(f"Aufgeräumt: {stale_count} verwaiste tmp-Verzeichnisse gelöscht")
    except OSError:
        pass

    # --- Verarbeitung ---
    stats = {"clean": 0, "minimized": 0, "would_minimize": 0, "error": 0}
    total_removed = 0

    error_log_path = os.path.join(output_dir, "compile_errors.jsonl")

    # append-Modus: vorherige Logs bleiben erhalten bei Neustart
    with open(os.path.join(output_dir, "delta_debug.jsonl"), "a") as log_file, \
         open(error_log_path, "a") as error_log:
        with multiprocessing.Pool(num_workers, initializer=_worker_init) as pool:
            try:
                it = pool.imap_unordered(_worker_fn, all_tasks, chunksize=1)
                processed_count = 0

                for res in tqdm(it, total=len(all_tasks), desc="Delta Debugging"):
                    processed_count += 1
                    status = res["status"]
                    stats[status] = stats.get(status, 0) + 1

                    n_removed = len(res.get("removed_top_level", [])) + \
                                len(res.get("removed_statements", []))

                    if n_removed > 0:
                        total_removed += n_removed
                        if args.dry_run:
                            tqdm.write(
                                f"  {os.path.basename(res['file'])}: "
                                f"{n_removed} Knoten entfernbar"
                            )
                        log_file.write(json.dumps(res, ensure_ascii=False) + "\n")
                        log_file.flush()

                    # Periodischer Cleanup: Nur ALTE tmp-Verzeichnisse löschen
                    # (>10 Min alt = sicher verwaist, aktive Worker brauchen max Sekunden)
                    if processed_count % 200 == 0:
                        try:
                            now = time.time()
                            for d in os.listdir(tmp_root):
                                if not d.startswith("delta_"):
                                    continue
                                dp = os.path.join(tmp_root, d)
                                if not os.path.isdir(dp):
                                    continue
                                try:
                                    age = now - os.path.getmtime(dp)
                                    if age > 600:  # Älter als 10 Minuten
                                        shutil.rmtree(dp, ignore_errors=True)
                                except OSError:
                                    pass
                        except OSError:
                            pass

                    if status == "error":
                        # Fehler-Log mit Zeitstempel
                        error_entry = {
                            "timestamp": datetime.now().isoformat(),
                            "file": res["file"],
                            "filename": os.path.basename(res["file"]),
                            "error": res.get("error", "unknown"),
                        }
                        error_log.write(json.dumps(error_entry, ensure_ascii=False) + "\n")
                        error_log.flush()

                        tqdm.write(
                            f"  [!] {os.path.basename(res['file'])}: "
                            f"{res.get('error', '?')}"
                        )

            except KeyboardInterrupt:
                print("\n\nAbgebrochen!")
                pool.terminate()
                pool.join()
                sys.exit(1)

    # --- Zusammenfassung ---
    found = stats.get('would_minimize', 0) + stats.get('minimized', 0)
    print("\n" + "=" * 60)
    print("ZUSAMMENFASSUNG")
    print("=" * 60)
    print(f"  Dateien gesamt:         {len(all_tasks)}")
    print(f"  Bereits sauber:         {stats['clean']}")
    print(f"  Mit Dead Code:          {found}")
    print(f"  Entfernte AST-Knoten:   {total_removed}")
    if args.dry_run:
        print(f"  (Dry-Run — nichts verändert)")
    else:
        print(f"  Erfolgreich minimiert:  {stats['minimized']}")
    print(f"  Fehler:                 {stats['error']}")
    print(f"\nOutput: {output_dir}")
    if stats['error'] > 0:
        print(f"Fehler-Log: {error_log_path}")


if __name__ == "__main__":
    main()