#!/usr/bin/env python3
"""
Dead code removal via delta debugging with IDO compiler oracle.

Strategy:
  1. Compile original → baseline assembly hash
  2. Remove AST nodes (statements, declarations, globals)
  3. Recompile → new hash
  4. Hash identical? → node was dead code, keep removal
  5. Hash different? → node is live, revert

Phases:
  Phase 1: Top-level (global variables, forward declarations)
  Phase 2: Function bodies (whole blocks first, then individual statements)

Safety:
  - Original files are NEVER modified
  - Results are written to a separate output directory
  - Dry-run mode available
  - JSONL log of every change
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
    Executes a command and kills the ENTIRE process group on timeout.

    IDO spawns internal child processes (cfe, as, etc.). subprocess.run()
    on timeout only kills the main process — children keep running and hold
    deleted tmp files open, filling up the disk.

    start_new_session=True places the process in its own group;
    os.killpg then kills all children along with it.
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
        # SIGKILL the entire process group
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass
        # Drain pipes to avoid deadlocks
        try:
            proc.communicate(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            proc.kill()
        raise


def check_disk_space(min_free_gb=2):
    """
    Pauses if the disk is nearly full.
    Prevents WSL crashes when the VHDX fills up.
    """
    while True:
        usage = shutil.disk_usage("/")
        free_gb = usage.free / (1024 ** 3)
        if free_gb >= min_free_gb:
            return
        tqdm.write(
            f"  [WARNING] Only {free_gb:.1f} GB free! "
            f"Pausing 30s (min: {min_free_gb} GB)..."
        )
        time.sleep(30)


# --- CONFIGURATION ---
BASE_DIR      = "/home/lukas/code_generator/deadCodeRemover"
PROJECT_ROOT  = os.path.join(BASE_DIR, "IDO_Compiler")
IDO_DIR       = os.path.abspath(os.path.join(PROJECT_ROOT, "tools", "ido"))
IDO_CC        = os.path.join(IDO_DIR, "cc")

DATASET_DIR   = os.path.join(BASE_DIR, "dataset")
OPTIMIZED_DIR = os.path.join(DATASET_DIR, "Stage_1_OUT")
HEADERS_DIR    = os.path.join(DATASET_DIR, "Stage_0_headers")
INPUT_DIR     = os.path.join(DATASET_DIR, "Stage_1_IN")

GROUPS = [
    "Input_Group",
]

# Include paths for the preprocessor
INCLUDE_DIRS = [
    os.path.join(PROJECT_ROOT, "include"),
    os.path.join(PROJECT_ROOT, "src"),
    os.path.join(PROJECT_ROOT, "include", "PR"),
    os.path.join(PROJECT_ROOT, "lib", "ultralib", "include"),
    os.path.join(BASE_DIR, "csmith_install/include/csmith-2.3.0"),
]

# RAM disk for temporary files — protects the SSD
TMP_ROOT = "/dev/shm"

# objdump instead of spimdisasm — 30x faster (5ms vs 158ms)
OBJDUMP = "mips-linux-gnu-objdump"

# ASM hash cache: avoids redundant IDO calls for identical C code
_asm_cache = {}
_asm_cache_lock = None  # Initialised in the worker process


# =====================================================================
#  COMPILER ORACLE
# =====================================================================

def _get_ido_env():
    """IDO environment variables."""
    env = os.environ.copy()
    env["COMPILER_PATH"] = IDO_DIR
    env["LD_LIBRARY_PATH"] = f"{IDO_DIR}:{env.get('LD_LIBRARY_PATH', '')}"
    return env

_IDO_ENV = _get_ido_env()


def compile_to_asm_hash(c_source: str, tmp_dir: str, header_dir: str,
                    name: str = "input") -> tuple[str | None, str]:
    """
    Extended version: validates both instructions AND data sections.
    Prevents Stage 1 from stripping global data or string literals.
    """
    c_path = os.path.join(tmp_dir, f"{name}.c")
    i_path = os.path.join(tmp_dir, f"{name}.i")
    o_path = os.path.join(tmp_dir, f"{name}.o")

    def _rm(p):
        try:
            if os.path.exists(p): os.unlink(p)
        except OSError: pass

    try:
        # Cache check
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

        # 3. EXTRACT part A: instructions (.text)
        cmd_obj_d = [OBJDUMP, "-d", "-z", o_path]
        rc, stdout_text, _ = run_cmd_safely(cmd_obj_d, timeout=30)
        if rc != 0: return None, "objdump -d failed"

        asm_payload = []

        # Normalise the code section
        for line in stdout_text.decode(errors="replace").splitlines():
            m = re.match(r'^\s*[0-9a-fA-F]+:\s+[0-9a-fA-F]+\s+(.*)', line)
            if not m: continue
            s = m.group(1).strip().split('#')[0].strip()
            if not s: continue
            s = re.sub(r'addiu\s+\$?(sp|29),\s*\$?(sp|29),\s*-?[0-9a-fA-F]+', 'addiu sp,sp,OFFSET', s)
            s = re.sub(r'-?[0-9a-fA-F]+\(\$?(sp|29)\)', 'OFFSET(sp)', s)
            s = re.sub(r'-?[0-9a-fA-F]+\(\$?(fp|30)\)', 'OFFSET(fp)', s)
            s = re.sub(r'%[a-z0-9_.]+\([^)]+\)', 'SYMBOL', s)
            asm_payload.append(s)

        # 4. EXTRACT part B: data sections (.rodata, .data, .bss)
        # Query each section INDIVIDUALLY — if one is absent, objdump
        # returns rc!=0, but the other sections are still valid.
        for sec in [".rodata", ".data", ".bss"]:
            cmd_obj_s = [OBJDUMP, "-s", "-j", sec, o_path]
            _, stdout_data, _ = run_cmd_safely(cmd_obj_s, timeout=30)

            if stdout_data:
                for line in stdout_data.decode(errors="replace").splitlines():
                    m = re.match(r'^\s*[0-9a-fA-F]+\s+((?:[0-9a-fA-F]+\s*)+)', line)
                    if m:
                        asm_payload.append(f"{sec}:" + m.group(1).strip())

        _rm(o_path)

        if not asm_payload:
            return None, "empty output"

        res_hash = hashlib.md5("\n".join(asm_payload).encode()).hexdigest()
        _asm_cache[cache_key] = (res_hash, "")
        return res_hash, ""

    except Exception as e:
        return None, str(e)


# =====================================================================
#  AST HELPER FUNCTIONS
# =====================================================================

def _extract_includes(source: str) -> tuple[list[str], str]:
    """Separates #include lines from the rest of the source."""
    includes = []
    code_lines = []
    for line in source.splitlines():
        if re.match(r'^\s*#', line):
            # Normalise include paths
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
    Runs gcc -E over the source so pycparser receives
    preprocessed code without unknown types.

    Returns: (preprocessed_code, tmp_filename) or (None, "").
    """
    tmp_c = os.path.join(tmp_dir, "parse_input.c")
    tmp_i = os.path.join(tmp_dir, "parse_input.i")

    # Normalise include paths: "../headers/foo.h" → "foo.h"
    # so gcc can find them via -I
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

    # KEEP line markers (# linenum "file") for pycparser
    # so that node.coord.file is set correctly.
    # Remove everything else (e.g. #pragma, #ident).
    kept = []
    for l in preprocessed.splitlines():
        stripped = l.lstrip()
        if stripped.startswith("#"):
            # gcc -E line markers: '# 42 "parse_input.c"' or '#line 42 ...'
            if re.match(r'^#\s*\d+\s+"', stripped) or re.match(r'^#\s*line\s+', stripped):
                kept.append(l)
            # Everything else (#pragma, #ident, etc.) -> discard
            else:
                continue
        else:
            kept.append(l)
    return "\n".join(kept), "parse_input.c"


def _filter_ast_to_original(ast, original_src: str) -> None:
    """
    Removes AST nodes that originate from headers (expanded by gcc -E).
    USES LINE MARKERS (node.coord) INSTEAD OF STRING MATCHING!

    gcc -E sets #line directives which pycparser stores as the coord attribute.
    Nodes whose coord points to a file other than 'parse_input.c'
    originate from a header and are discarded.
    """
    if not ast.ext:
        return

    filtered = []
    for node in ast.ext:
        # Skip typedefs from preamble
        if isinstance(node, c_ast.Typedef):
            continue

        # Reliable filtering via internal parser coordinates
        if hasattr(node, 'coord') and node.coord is not None:
            # 'parse_input.c' is the temporary filename used by the preprocessor
            if node.coord.file and not node.coord.file.endswith("parse_input.c"):
                # Node physically originates from a header file -> discard
                continue

        filtered.append(node)

    ast.ext = filtered


def _parse_to_ast(source_with_includes: str, header_dir: str = "",
                  tmp_dir: str = "") -> c_ast.FileAST | None:
    """
    Parses C code into an AST.

    Strategy:
      1. Preprocess with gcc -E (resolves all custom types)
      2. pycparser parses the preprocessed code
      3. Filter AST to nodes from the original file
      4. Fallback: try directly with a typedef preamble
    """
    parser = c_parser.CParser()

    # --- Attempt 1: preprocess with gcc -E ---
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
                pass  # Try fallback

    # --- Attempt 2: directly with typedef preamble ---
    # Remove includes for direct processing
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
    """Generates C source from AST + original includes.
    Returns: None on RecursionError (AST too deep)."""
    gen = c_generator.CGenerator()
    try:
        code = gen.visit(ast)
    except RecursionError:
        return None
    # Remove typedefs from the preamble
    clean_lines = [l for l in code.splitlines() if not l.startswith("typedef ")]
    return "\n".join(includes) + "\n\n" + "\n".join(clean_lines)


def _count_ast_nodes(ast) -> int:
    """Counts the number of removable nodes in the AST."""
    count = 0
    # Top-level
    count += len(ast.ext) if ast.ext else 0
    # Statements in function bodies
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
    Runs delta debugging on a C file.

    Systematically removes AST nodes and checks whether the assembly changes.
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

    # --- Read original file ---
    with open(c_filepath, "r", encoding="utf-8", errors="replace") as f:
        original_src = f.read()

    includes, cleaned_src = _extract_includes(original_src)

    # --- Tmp directory (in RAM: /dev/shm) ---
    tmp_dir = tempfile.mkdtemp(dir=TMP_ROOT, prefix=f"delta_{name_no_ext}_")

    try:
        # --- Baseline: compile original ---
        baseline_code = _ast_to_source_raw(includes, cleaned_src)
        baseline_hash, baseline_err = compile_to_asm_hash(baseline_code, tmp_dir, header_dir)

        if baseline_hash is None:
            result["status"] = "error"
            result["error"] = f"Baseline does not compile: {baseline_err}"
            # DO NOT COPY: file is defective and permanently discarded
            return result

        # --- Parse AST (with gcc -E preprocessing) ---
        ast = _parse_to_ast(original_src, header_dir, tmp_dir)
        if ast is None:
            result["status"] = "error"
            result["error"] = "AST parse error"
            # COPY: file compiles cleanly but pycparser fails
            if not dry_run:
                shutil.copy2(c_filepath, output_path)
            return result

        # --- Recalculate baseline from regenerated code ---
        # pycparser's code generator produces slightly different output
        # than the original (brackets, spaces, float literals). The baseline
        # must therefore come from the REGENERATED code, not the original,
        # so we are comparing apples to apples.
        regen_code = _ast_to_source(ast, includes)
        if regen_code is None:
            result["status"] = "error"
            result["error"] = "RecursionError during code regeneration"
            # COPY: file compiles cleanly but regeneration fails
            if not dry_run:
                shutil.copy2(c_filepath, output_path)
            return result

        regen_hash, regen_err = compile_to_asm_hash(regen_code, tmp_dir, header_dir)
        if regen_hash is None:
            # Regenerated code does not compile → pycparser broke something
            # Fallback: use original baseline
            regen_hash = baseline_hash

        # If the regenerated code produces different ASM than the original,
        # delta-debugging is not safe → copy original
        if regen_hash != baseline_hash:
            result["status"] = "clean"
            if not dry_run:
                shutil.copy2(c_filepath, output_path)
            return result

        # From here: baseline_hash == regen_hash, everything consistent

        total_before = _count_ast_nodes(ast)
        changes_made = False

        # =============================================================
        # ITERATIVE MULTI-PASS
        # All phases loop until no further changes occur. Necessary because:
        #   - Phase 2b empties a while body (inner statements removed)
        #   - Phase 2 can then remove the now-empty while
        #   - Phase 3 finds new dead stores after earlier removals
        # Without multi-pass, phantom code remains!
        # =============================================================
        pass_num = 0
        max_passes = 20

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

            # === PHASE 2+2b: Statements + nested blocks ===
            for ext_node in (ast.ext or []):
                if not isinstance(ext_node, c_ast.FuncDef):
                    continue
                if not ext_node.body or not ext_node.body.block_items:
                    continue

                func_name = ext_node.decl.name
                items = ext_node.body.block_items

                # Phase 2: top-level statements in function
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

                # Phase 2b: descend into nested blocks
                if _minimize_nested(items, ast, includes, baseline_hash,
                                    tmp_dir, header_dir, func_name, result):
                    changes_made = True
                    pass_changed = True

            # === PHASE 3: Coupled removals (dead stores) ===
            # Finds local variables whose references (including in nested
            # blocks like while/if) have no ASM effect.
            # Step 1: individual variables + their references
            # Step 2: groups of variables that only reference each other
            for ext_node in (ast.ext or []):
                if not isinstance(ext_node, c_ast.FuncDef):
                    continue
                if not ext_node.body or not ext_node.body.block_items:
                    continue

                func_name = ext_node.decl.name
                items = ext_node.body.block_items
                gen = c_generator.CGenerator()

                # Generate code for each top-level item (including nested blocks)
                def _gen_item_codes():
                    codes = []
                    for item in items:
                        try:
                            codes.append(gen.visit(item))
                        except RecursionError:
                            codes.append("")
                    return codes

                item_codes = _gen_item_codes()

                # Collect local variables
                local_var_names = set()
                for item in items:
                    if isinstance(item, c_ast.Decl) and item.name:
                        local_var_names.add(item.name)

                if not local_var_names:
                    continue

                # --- Step 1: individual variable + all referencing items ---
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
                            f"({len(removed_items)} statements)")
                        changes_made = True
                        pass_changed = True
                        item_codes = _gen_item_codes()  # Refresh
                    else:
                        for idx, item in sorted(removed_items):
                            items.insert(idx, item)

                # --- Step 2: group removal ---
                # Remove all remaining local variables + everything referencing them
                # as a group (catches while-chains like sp30/sp34/sp38/...)
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
                                    f"({len(removed_items)} statements)")
                                changes_made = True
                                pass_changed = True
                            else:
                                for idx, item in sorted(removed_items):
                                    items.insert(idx, item)
                        else:
                            for idx, item in sorted(removed_items):
                                items.insert(idx, item)

            # No progress → done
            if not pass_changed:
                break

        # === RESULT ===
        if not changes_made:
            result["status"] = "clean"
            if not dry_run:
                # Copy clean files to output as well
                shutil.copy2(c_filepath, output_path)
            return result

        # Check whether only empty blocks were removed (no real reduction).
        # pycparser reformats code (extra brackets, int returns etc.)
        # which is undesirable without a genuine semantic change.
        only_empty = (
            len(result["removed_top_level"]) == 0
            and all("block (0 items)" in s for s in result["removed_statements"])
        )
        if only_empty:
            result["status"] = "clean"
            result["removed_statements"] = []
            if not dry_run:
                shutil.copy2(c_filepath, output_path)
            return result

        total_removed = len(result["removed_top_level"]) + len(result["removed_statements"])
        result["status"] = "would_minimize" if dry_run else "minimized"

        if not dry_run:
            final_code = _ast_to_source(ast, includes)

            if final_code is None:
                result["status"] = "error"
                result["error"] = "RecursionError during final code generation"
                shutil.copy2(c_filepath, output_path)
                return result

            # No safety check — same approach as Stage 4.
            # pycparser formats differently from the original, but semantics are identical.
            # The hash check was already performed for each individual node.
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(final_code)

        return result

    except RecursionError:
        result["status"] = "error"
        result["error"] = "RecursionError: AST too deeply nested (csmith/YARPGen)"
        # COPY: original copied to output so the file is not missing
        if not dry_run:
            try:
                shutil.copy2(c_filepath, output_path)
            except Exception:
                pass
        return result

    except Exception as e:
        result["status"] = "error"
        result["error"] = str(e)
        # COPY: an internal script crash must not destroy compilable files.
        if not dry_run:
            try:
                shutil.copy2(c_filepath, output_path)
            except Exception:
                pass
        return result

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)



def _minimize_nested(items: list, ast, includes: list[str],
                     baseline_hash: str, tmp_dir: str, header_dir: str,
                     func_name: str, result: dict) -> bool:
    """
    Recursive: tries to remove statements inside nested blocks.
    Descends into if/else/for/while/switch/compound blocks.
    Returns: True if at least one change was made.
    """
    any_changed = False
    for item in items:
        # If-statement: iftrue and iffalse blocks
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

        # Compound (nested block)
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
    """Tries to remove individual statements from a block.
    Returns: True if at least one statement was removed."""
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
    """Reconstructs source text without AST (used for baseline)."""
    return "\n".join(includes) + "\n\n" + cleaned_src


def _describe_node(node) -> str:
    """Returns a short description of an AST node."""
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
    # Disable core dumps
    try:
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except (ValueError, resource.error):
        pass
    # Increase recursion limit for deeply nested csmith/YARPGen expressions
    sys.setrecursionlimit(50_000)


def _get_header_dir(c_filepath: str) -> str:
    """Finds the matching header directory."""
    for group in GROUPS:
        if group in c_filepath:
            return os.path.join(HEADERS_DIR, f"{group}_headers")
    return ""


def _worker_fn(args):
    c_filepath, output_path, dry_run = args
    # Emergency brake: pause if disk is nearly full
    check_disk_space(min_free_gb=2)
    header_dir = _get_header_dir(c_filepath)
    return delta_debug_file(c_filepath, header_dir, output_path, dry_run)


def main():
    ap = argparse.ArgumentParser(
        description="Dead code removal via delta debugging with IDO compiler"
    )
    ap.add_argument(
        "--dry-run", action="store_true",
        help="Show what would happen without making changes",
    )
    ap.add_argument(
        "--group", type=str, default=None,
        help="Process only a specific group",
    )
    ap.add_argument(
        "-j", "--workers", type=int, default=None,
        help="Number of parallel workers (default: CPU count)",
    )
    ap.add_argument(
        "--diagnose", type=str, default=None,
        help="Analyse a single file",
    )
    ap.add_argument(
        "--output-dir", type=str, default=OPTIMIZED_DIR,
        help="Output directory",
    )
    ap.add_argument(
        "--overwrite", action="store_true",
        help="Reprocess already optimised files (default: skip)",
    )
    args = ap.parse_args()

    output_dir = args.output_dir
    num_workers = args.workers or multiprocessing.cpu_count()

    # --- Diagnose mode ---
    if args.diagnose:
        c_path = args.diagnose
        header_dir = _get_header_dir(c_path)

        # Gruppen-Unterordner ermitteln (wie im Batch-Modus)
        group_name = None
        for group in GROUPS:
            if group in c_path:
                group_name = group
                break

        if group_name:
            group_output = os.path.join(output_dir, group_name)
        else:
            group_output = output_dir
        os.makedirs(group_output, exist_ok=True)
        out_path = os.path.join(group_output, os.path.basename(c_path))

        print(f"=== DIAGNOSE: {c_path} ===")
        print(f"Header dir: {header_dir}")
        print(f"Output: {out_path}\n")

        res = delta_debug_file(c_path, header_dir, out_path, dry_run=False)

        print(f"Status: {res['status']}")
        if res.get('error'):
            print(f"Error: {res['error']}")
        if res['removed_top_level']:
            print(f"\nRemovable top-level nodes:")
            for name in res['removed_top_level']:
                print(f"  → {name}")
        if res['removed_statements']:
            print(f"\nRemovable statements:")
            for desc in res['removed_statements']:
                print(f"  → {desc}")

        total = len(res['removed_top_level']) + len(res['removed_statements'])
        print(f"\nTotal removable: {total}")

        # Size comparison
        if os.path.exists(out_path):
            in_size = os.path.getsize(c_path)
            out_size = os.path.getsize(out_path)
            print(f"\nInput:  {in_size} bytes")
            print(f"Output: {out_size} bytes")
            print(f"Diff:   {in_size - out_size} bytes ({in_size - out_size:+d})")
        return

    # --- Process groups ---
    groups_to_process = GROUPS
    if args.group:
        if args.group not in GROUPS:
            print(f"Error: group '{args.group}' not found.")
            sys.exit(1)
        groups_to_process = [args.group]

    os.makedirs(output_dir, exist_ok=True)

    # Collect tasks
    all_tasks = []
    skipped_existing = 0
    for group in groups_to_process:
        input_dir = os.path.join(INPUT_DIR, group)
        group_output = os.path.join(output_dir, group)

        if not os.path.isdir(input_dir):
            print(f"  [!] Not found: {input_dir}")
            continue

        os.makedirs(group_output, exist_ok=True)

        for fname in os.listdir(input_dir):
            if fname.endswith(".c"):
                c_path = os.path.join(input_dir, fname)
                out_path = os.path.join(group_output, fname)

                # Skip already processed files
                if not args.overwrite and os.path.exists(out_path):
                    skipped_existing += 1
                    continue

                all_tasks.append((c_path, out_path, args.dry_run))

    print(f"Found: {len(all_tasks) + skipped_existing} C files")
    if skipped_existing > 0:
        print(f"Skipped: {skipped_existing} (already in output)")
    print(f"To process: {len(all_tasks)}")
    print(f"Worker:   {num_workers}")
    print(f"Output:   {output_dir}")
    print(f"Method:   Delta debugging with IDO compiler oracle")
    if args.dry_run:
        print("=== DRY RUN ===\n")

    if not all_tasks:
        print("\nNo files to process.")
        return

    # --- Cleanup: stale tmp directories from previous run ---
    stale_count = 0
    tmp_root = TMP_ROOT
    try:
        for d in os.listdir(tmp_root):
            if d.startswith("delta_") and os.path.isdir(os.path.join(tmp_root, d)):
                shutil.rmtree(os.path.join(tmp_root, d), ignore_errors=True)
                stale_count += 1
        if stale_count:
            print(f"Cleaned up: {stale_count} stale tmp directories removed")
    except OSError:
        pass

    # --- Processing ---
    stats = {"clean": 0, "minimized": 0, "would_minimize": 0, "error": 0}
    total_removed = 0

    error_log_path = os.path.join(output_dir, "compile_errors.jsonl")

    # Append mode: previous logs are preserved on restart
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
                                f"{n_removed} nodes removable"
                            )

                    # Only actually minimised files go into the log
                    if status == "minimized":
                        log_file.write(json.dumps(res, ensure_ascii=False) + "\n")
                        log_file.flush()

                    # Periodic cleanup: only remove OLD tmp directories
                    # (>10 min old = safely orphaned; active workers finish in seconds)
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
                                    if age > 600:  # older than 10 minutes
                                        shutil.rmtree(dp, ignore_errors=True)
                                except OSError:
                                    pass
                        except OSError:
                            pass

                    if status == "error":
                        # Error log with timestamp
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
                print("\n\nAborted!")
                pool.terminate()
                pool.join()
                sys.exit(1)

    # --- Summary ---
    found = stats.get('would_minimize', 0) + stats.get('minimized', 0)
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"  Total files:            {len(all_tasks)}")
    print(f"  Already clean:          {stats['clean']}")
    print(f"  With dead code:         {found}")
    print(f"  Removed AST nodes:      {total_removed}")
    if args.dry_run:
        print(f"  (Dry run — nothing changed)")
    else:
        print(f"  Successfully minimised: {stats['minimized']}")
    print(f"  Errors:                 {stats['error']}")
    print(f"\nOutput: {output_dir}")
    if stats['error'] > 0:
        print(f"Error log: {error_log_path}")


if __name__ == "__main__":
    main()
