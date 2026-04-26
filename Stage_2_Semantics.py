#!/usr/bin/env python3
"""
Semantic code cleanup via expression-level delta debugging.

Second stage script: operates on structurally optimised code
(Dataset A → Dataset B) and removes semantic noise that leaves
no trace in the assembly output.

Targets:
  - Identity operations: x + 0, x | 0, x * 1, x << 0, x >> 0
  - Redundant casts: ((u32) ((u32) x)) → ((u32) x)
  - Self-divisions/identities: x / x → 1 (when no side effects)
  - Comma operator noise: (a, b) → b when a is side-effect-free
  - Trivial constant comparisons: (&g) == (&g) → 1
  - Nested identical casts: (u32)(u32)x → (u32)x
  - Dead expression branches in ternary: cond ? a : a → a

Strategy:
  1. Parse AST (same as Stage 1)
  2. Collect all expression nodes
  3. For each: attempt simplification
  4. Compile, compare normalised ASM hash
  5. Hash identical? → keep simplification

Difference from Stage 1:
  Stage 1: Removes nodes (block_items, ext)
  Stage 2: Replaces expression nodes with simpler variants
"""

import os
import sys
import re
import copy
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


# --- CONFIGURATION ---
BASE_DIR      = "/home/lukas/code_generator/deadCodeRemover"
PROJECT_ROOT  = os.path.join(BASE_DIR, "IDO_Compiler")
IDO_DIR       = os.path.abspath(os.path.join(PROJECT_ROOT, "tools", "ido"))
IDO_CC        = os.path.join(IDO_DIR, "cc")


DATASET_DIR   = os.path.join(BASE_DIR, "dataset")
INPUT_DIR     = os.path.join(DATASET_DIR, "Stage_2_IN")       
OUTPUT_DIR    = os.path.join(DATASET_DIR, "Stage_2_OUT") 
HEADERS_DIR    = os.path.join(DATASET_DIR, "Stage_0_headers")

GROUPS = [
    "Input_Group",
]

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
#  SAFE SUBPROCESS EXECUTION
# =====================================================================

def run_cmd_safely(cmd, cwd=None, env=None, timeout=30):
    """Process-Group-aware subprocess execution."""
    try:
        proc = subprocess.Popen(
            cmd, cwd=cwd, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True
        )
        stdout, stderr = proc.communicate(timeout=timeout)
        return proc.returncode, stdout, stderr
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass
        try:
            proc.communicate(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            proc.kill()
        raise


def check_disk_space(min_free_gb=2):
    """Emergency brake when disk is full."""
    while True:
        usage = shutil.disk_usage("/")
        free_gb = usage.free / (1024 ** 3)
        if free_gb >= min_free_gb:
            return
        tqdm.write(f"  [WARNING] Only {free_gb:.1f} GB free! Pausing 30s...")
        time.sleep(30)


# =====================================================================
#  COMPILER ORACLE
# =====================================================================

def _get_ido_env():
    env = os.environ.copy()
    env["COMPILER_PATH"] = IDO_DIR
    env["LD_LIBRARY_PATH"] = f"{IDO_DIR}:{env.get('LD_LIBRARY_PATH', '')}"
    return env

_IDO_ENV = _get_ido_env()


def compile_to_asm_hash(c_source: str, tmp_dir: str, header_dir: str,
                        name: str = "input") -> tuple[str | None, str]:
    """
    Improved version for Stage 2:
    Validates instructions (.text) AND raw data (.rodata, .data, .bss).
    """
    c_path = os.path.join(tmp_dir, f"{name}.c")
    i_path = os.path.join(tmp_dir, f"{name}.i")
    o_path = os.path.join(tmp_dir, f"{name}.o")

    def _rm(path):
        try:
            if os.path.exists(path): os.unlink(path)
        except OSError: pass

    try:
        # Preprocessing-Fix & Cache-Check
        fixed_source = re.sub(r'#include\s+"[^"]*?([^/"]+\.h)"', r'#include "\1"', c_source)
        cache_key = hashlib.md5(fixed_source.encode()).hexdigest()
        if cache_key in _asm_cache:
            return _asm_cache[cache_key]

        with open(c_path, "w", encoding="utf-8") as f:
            f.write(fixed_source)

        # 1. gcc -E
        cmd_cpp = ["gcc", "-E", "-P", "-xc", "-D_LANGUAGE_C", "-D_MIPS_SZLONG=32"]
        for inc in INCLUDE_DIRS: cmd_cpp += ["-I", inc]
        if header_dir: cmd_cpp += ["-I", header_dir]
        cmd_cpp += [c_path, "-o", i_path]
        rc, _, _ = run_cmd_safely(cmd_cpp, timeout=30)
        _rm(c_path)
        if rc != 0: return None, "gcc -E failed"

        # 2. IDO CC
        cmd_ido = [IDO_CC, "-c", "-O2", "-mips2", "-G", "0", "-w", i_path, "-o", o_path]
        rc, _, _ = run_cmd_safely(cmd_ido, cwd=tmp_dir, env=_IDO_ENV, timeout=30)
        _rm(i_path)
        if rc != 0: return None, "IDO failed"

        # 3. Extract A: instructions (.text)
        cmd_obj_d = [OBJDUMP, "-d", "-z", o_path]
        rc, stdout_text, _ = run_cmd_safely(cmd_obj_d, timeout=30)
        if rc != 0: return None, "objdump -d failed"

        # 4. Extract B: raw data (.rodata, .data, .bss)
        cmd_obj_s = [OBJDUMP, "-s", "-j", ".rodata", "-j", ".data", "-j", ".bss", o_path]
        rc2, stdout_data, _ = run_cmd_safely(cmd_obj_s, timeout=30)
        _rm(o_path)

        asm_payload = []

        # Normalise code section (same as Stage 1/3)
        for line in stdout_text.decode(errors="replace").splitlines():
            m = re.match(r'^\s*[0-9a-fA-F]+:\s+[0-9a-fA-F]+\s+(.*)', line)
            if not m: continue
            s = m.group(1).strip().split('#')[0].strip()
            if not s: continue
            # Stack normalisation
            s = re.sub(r'addiu\s+\$?(sp|29),\s*\$?(sp|29),\s*-?[0-9a-fA-F]+', 'addiu sp,sp,OFFSET', s)
            s = re.sub(r'-?[0-9a-fA-F]+\(\$?(sp|29)\)', 'OFFSET(sp)', s)
            s = re.sub(r'-?[0-9a-fA-F]+\(\$?(fp|30)\)', 'OFFSET(fp)', s)
            s = re.sub(r'%[a-z0-9_.]+\([^)]+\)', 'SYMBOL', s)
            asm_payload.append(s)

        # Append hex dump of data sections
        if rc2 == 0 and stdout_data:
            for line in stdout_data.decode(errors="replace").splitlines():
                m = re.match(r'^\s*[0-9a-fA-F]+\s+((?:[0-9a-fA-F]+\s*)+)', line)
                if m:
                    asm_payload.append("DATA:" + m.group(1).strip())

        if not asm_payload:
            return None, "No output generated"

        result_hash = hashlib.md5("\n".join(asm_payload).encode()).hexdigest()
        _asm_cache[cache_key] = (result_hash, "")
        return result_hash, ""

    except Exception as e:
        return None, str(e)


# =====================================================================
#  AST HELPERS
# =====================================================================

def _preprocess_for_parsing(c_source: str, header_dir: str, tmp_dir: str) -> str | None:
    tmp_c = os.path.join(tmp_dir, "parse_input.c")
    tmp_i = os.path.join(tmp_dir, "parse_input.i")

    fixed_source = re.sub(
        r'#include\s+"[^"]*?([^/"]+\.h)"',
        r'#include "\1"',
        c_source
    )
    with open(tmp_c, "w", encoding="utf-8") as f:
        f.write(fixed_source)

    cmd = ["gcc", "-E", "-xc", "-D_LANGUAGE_C", "-D_MIPS_SZLONG=32",
           "-D__attribute__(x)=", "-D__extension__="]
    for inc in INCLUDE_DIRS:
        cmd += ["-I", inc]
    if header_dir:
        cmd += ["-I", header_dir]
    cmd += [tmp_c, "-o", tmp_i]

    try:
        rc, _, _ = run_cmd_safely(cmd, timeout=30)
    except subprocess.TimeoutExpired:
        return None
    if rc != 0:
        return None

    with open(tmp_i, "r", encoding="utf-8", errors="replace") as f:
        preprocessed = f.read()

    lines = [l for l in preprocessed.splitlines() if not l.lstrip().startswith("#")]
    return "\n".join(lines)


def _filter_ast_to_original(ast, original_src: str):
    if not ast.ext:
        return

    filtered = []
    for node in ast.ext:
        # 1. Keep function definitions whose name appears in the original
        if isinstance(node, c_ast.FuncDef):
            if node.decl.name in original_src:
                filtered.append(node)

        # 2. Keep declarations ONLY if they are NOT extern declarations
        #    from headers. Extern declarations belong in the header,
        #    not in the .c file.
        elif isinstance(node, c_ast.Decl):
            if node.name and node.name in original_src:
                # Skip extern declarations from headers
                if node.storage and 'extern' in node.storage:
                    continue
                # Skip typedefs (also come from headers)
                if isinstance(node.type, c_ast.TypeDecl) and \
                   hasattr(node, 'storage') and node.storage and \
                   'typedef' in node.storage:
                    continue
                filtered.append(node)

    ast.ext = filtered


def _parse_to_ast(source: str, header_dir: str, tmp_dir: str):
    preprocessed = _preprocess_for_parsing(source, header_dir, tmp_dir)
    if not preprocessed:
        return None
    parser = c_parser.CParser()
    try:
        ast = parser.parse(preprocessed)
        _filter_ast_to_original(ast, source)
        return ast
    except (RecursionError, Exception):
        return None


def _extract_includes(source: str) -> tuple[list[str], str]:
    includes = []
    code_lines = []
    for line in source.splitlines():
        if re.match(r'^\s*#', line):
            fixed = re.sub(r'#include\s+"[^"]*?([^/"]+\.h)"',
                           r'#include "\1"', line)
            includes.append(fixed)
        else:
            code_lines.append(line)
    return includes, "\n".join(code_lines)


def _ast_to_source(ast, includes: list[str]) -> str | None:
    gen = c_generator.CGenerator()
    try:
        code = gen.visit(ast)
    except RecursionError:
        return None
    clean_lines = [l for l in code.splitlines() if not l.startswith("typedef ")]
    return "\n".join(includes) + "\n\n" + "\n".join(clean_lines)


# =====================================================================
#  SEMANTIC SIMPLIFICATION RULES
# =====================================================================
#
# Each rule is a (name, simplify) pair:
#   simplify(node):  Returns the simplified node, or None if the rule does not apply.
#
# The delta debugger tests each rule on every matching node
# and keeps the simplification if the ASM hash remains identical.

def _is_zero_const(node) -> bool:
    """Is this a zero constant (0, 0U, 0x0, 0.0f)?"""
    if not isinstance(node, c_ast.Constant):
        return False
    val = node.value.strip().lower().rstrip('ulf')
    try:
        if '.' in val:
            return float(val) == 0.0
        if val.startswith('0x'):
            return int(val, 16) == 0
        return int(val) == 0
    except ValueError:
        return False


def _is_one_const(node) -> bool:
    """Is this a one constant?"""
    if not isinstance(node, c_ast.Constant):
        return False
    val = node.value.strip().lower().rstrip('ulf')
    try:
        if '.' in val:
            return float(val) == 1.0
        if val.startswith('0x'):
            return int(val, 16) == 1
        return int(val) == 1
    except ValueError:
        return False


def _nodes_equal(a, b) -> bool:
    """Structural comparison of two AST nodes via code generation."""
    if type(a) is not type(b):
        return False
    try:
        gen = c_generator.CGenerator()
        return gen.visit(a) == gen.visit(b)
    except Exception:
        return False


def _has_side_effect(node) -> bool:
    """
    Heuristic: does this expression potentially have side effects?
    (function calls, assignments, ++/--, pointer dereferences)
    """
    if isinstance(node, (c_ast.FuncCall, c_ast.Assignment)):
        return True
    if isinstance(node, c_ast.UnaryOp) and node.op in ('p++', 'p--', '++', '--', '*'):
        return True
    # Recurse into children
    for _, child in node.children():
        if _has_side_effect(child):
            return True
    return False


# ---------- Regel 1: x + 0, 0 + x, x - 0 → x ----------
def _rule_add_zero(node):
    if not isinstance(node, c_ast.BinaryOp):
        return None
    if node.op == '+':
        if _is_zero_const(node.right):
            return node.left
        if _is_zero_const(node.left):
            return node.right
    elif node.op == '-':
        if _is_zero_const(node.right):
            return node.left
    return None


# ---------- Regel 2: x | 0, 0 | x, x ^ 0, 0 ^ x → x ----------
def _rule_or_xor_zero(node):
    if not isinstance(node, c_ast.BinaryOp):
        return None
    if node.op in ('|', '^'):
        if _is_zero_const(node.right):
            return node.left
        if _is_zero_const(node.left):
            return node.right
    return None


# ---------- Regel 3: x * 1, 1 * x, x / 1 → x ----------
def _rule_mul_div_one(node):
    if not isinstance(node, c_ast.BinaryOp):
        return None
    if node.op == '*':
        if _is_one_const(node.right):
            return node.left
        if _is_one_const(node.left):
            return node.right
    elif node.op == '/':
        if _is_one_const(node.right):
            return node.left
    return None


# ---------- Regel 4: x << 0, x >> 0 → x ----------
def _rule_shift_zero(node):
    if not isinstance(node, c_ast.BinaryOp):
        return None
    if node.op in ('<<', '>>'):
        if _is_zero_const(node.right):
            return node.left
    return None


# ---------- Rule 5: x & 0xFFFFFFFF → x (for u32 masks) ----------
def _rule_and_fullmask(node):
    if not isinstance(node, c_ast.BinaryOp):
        return None
    if node.op != '&':
        return None
    for side, other in ((node.right, node.left), (node.left, node.right)):
        if isinstance(side, c_ast.Constant):
            val = side.value.strip().lower().rstrip('ulf')
            try:
                n = int(val, 16) if val.startswith('0x') else int(val)
                if n == 0xFFFFFFFF or n == -1:
                    return other
            except ValueError:
                pass
    return None


# ---------- Rule 6: double cast ((T)(T)x) → (T)x ----------
def _rule_double_cast(node):
    if not isinstance(node, c_ast.Cast):
        return None
    inner = node.expr
    if isinstance(inner, c_ast.Cast):
        gen = c_generator.CGenerator()
        try:
            outer_type = gen.visit(node.to_type)
            inner_type = gen.visit(inner.to_type)
            if outer_type == inner_type:
                return inner  # Double cast → one is enough
        except Exception:
            pass
    return None


# ---------- Rule 7: remove cast to same type ----------
# (s32)(s32_var) — only safe if we know var is already s32
# Difficult without type inference, therefore omitted


# ---------- Rule 8: comma operator (a, b) → b when a is side-effect-free ----------
def _rule_comma_drop_left(node):
    if not isinstance(node, c_ast.ExprList):
        return None
    if len(node.exprs) < 2:
        return None
    # Check whether all but the last expression are side-effect-free
    effects_free_prefix = []
    for expr in node.exprs[:-1]:
        if _has_side_effect(expr):
            return None
        effects_free_prefix.append(expr)
    if effects_free_prefix:
        # All side-effect-free prefixes → drop them
        return node.exprs[-1]
    return None


# ---------- Rule 9: ternary cond ? a : a → a ----------
def _rule_ternary_same(node):
    if not isinstance(node, c_ast.TernaryOp):
        return None
    if _nodes_equal(node.iftrue, node.iffalse):
        if _has_side_effect(node.cond):
            # Condition has side effects → would need to keep condition + iftrue
            # Too complex to handle safely, skip
            return None
        return node.iftrue
    return None


# ---------- Rule 10: (void *) 0 == (void *) 0 → 1 ----------
def _rule_null_eq_null(node):
    if not isinstance(node, c_ast.BinaryOp):
        return None
    if node.op not in ('==', '!='):
        return None

    def _is_null(n):
        if isinstance(n, c_ast.Cast) and isinstance(n.expr, c_ast.Constant):
            return _is_zero_const(n.expr)
        if isinstance(n, c_ast.Constant):
            return _is_zero_const(n)
        return False

    if _is_null(node.left) and _is_null(node.right):
        result = "1" if node.op == '==' else "0"
        return c_ast.Constant(type='int', value=result)
    return None


# ---------- Rule 11: (&x) == (&x) → 1 ----------
def _rule_addr_eq_self(node):
    if not isinstance(node, c_ast.BinaryOp):
        return None
    if node.op not in ('==', '!='):
        return None
    # Both sides must be UnaryOp with '&'
    if (isinstance(node.left, c_ast.UnaryOp) and node.left.op == '&' and
        isinstance(node.right, c_ast.UnaryOp) and node.right.op == '&'):
        if _nodes_equal(node.left.expr, node.right.expr):
            result = "1" if node.op == '==' else "0"
            return c_ast.Constant(type='int', value=result)
    return None


# ---------- Rule 12: x == x / x != x → 1 / 0 (only without side effects) ----------
def _rule_self_compare(node):
    if not isinstance(node, c_ast.BinaryOp):
        return None
    if node.op not in ('==', '!=', '<=', '>='):
        return None
    if _has_side_effect(node.left) or _has_side_effect(node.right):
        return None
    if _nodes_equal(node.left, node.right):
        # x == x → 1, x != x → 0, x <= x → 1, x >= x → 1
        if node.op in ('==', '<=', '>='):
            return c_ast.Constant(type='int', value='1')
        else:  # !=
            return c_ast.Constant(type='int', value='0')
    return None


# ---------- Rule 13: redundant unary plus ----------
def _rule_unary_plus(node):
    if isinstance(node, c_ast.UnaryOp) and node.op == '+':
        return node.expr
    return None


# ---------- Rule 14: -(-(x)) → x ----------
def _rule_double_negate(node):
    if isinstance(node, c_ast.UnaryOp) and node.op == '-':
        if isinstance(node.expr, c_ast.UnaryOp) and node.expr.op == '-':
            return node.expr.expr
    return None


# ---------- Rule 15: !(!x) → x (complex in boolean context, syntactic only) ----------
# Omitted because !!x is not always == x


# List of all rules
RULES = [
    ("add_zero", _rule_add_zero),
    ("or_xor_zero", _rule_or_xor_zero),
    ("mul_div_one", _rule_mul_div_one),
    ("shift_zero", _rule_shift_zero),
    ("and_fullmask", _rule_and_fullmask),
    ("double_cast", _rule_double_cast),
    ("comma_drop", _rule_comma_drop_left),
    ("ternary_same", _rule_ternary_same),
    ("null_eq_null", _rule_null_eq_null),
    ("addr_eq_self", _rule_addr_eq_self),
    ("self_compare", _rule_self_compare),
    ("unary_plus", _rule_unary_plus),
    ("double_negate", _rule_double_negate),
]


# =====================================================================
#  AST MUTATION AND DELTA DEBUGGING
# =====================================================================

def _find_parent_and_attr(root, target):
    """
    Finds the parent and attribute name for a target node.
    Returns: (parent, attr_name, index_or_None)
    """
    stack = [root]
    while stack:
        node = stack.pop()
        for attr in dir(node):
            if attr.startswith('_'):
                continue
            try:
                value = getattr(node, attr, None)
            except Exception:
                continue

            if value is target:
                return (node, attr, None)

            if isinstance(value, list):
                for i, item in enumerate(value):
                    if item is target:
                        return (node, attr, i)
                    if isinstance(item, c_ast.Node):
                        stack.append(item)
            elif isinstance(value, c_ast.Node):
                stack.append(value)
    return (None, None, None)


def _collect_expression_nodes(ast):
    """
    Collects all expression nodes in the AST (BFS).
    Declarations (Decl.type etc.) are skipped.
    """
    nodes = []
    # Uses a visitor that only traverses bodies, not type nodes
    
    class Collector(c_ast.NodeVisitor):
        def __init__(self):
            self.nodes = []

        def generic_visit(self, node):
            # Collect expression-relevant nodes
            if isinstance(node, (
                c_ast.BinaryOp, c_ast.UnaryOp, c_ast.Cast,
                c_ast.TernaryOp, c_ast.ExprList,
            )):
                self.nodes.append(node)
            # Continue traversal
            for _, child in node.children():
                self.visit(child)

        def visit_Decl(self, node):
            # Do not traverse into declaration types, only into init
            if node.init:
                self.visit(node.init)

        def visit_Typedef(self, node):
            pass  # Skip typedefs

    c = Collector()
    c.visit(ast)
    return c.nodes


def _try_simplify(ast, target_node, simplified_node, includes, tmp_dir,
                  header_dir, baseline_hash):
    """
    Tries to replace target_node with simplified_node.
    Returns: True if hash is identical, False otherwise.
    """
    parent, attr, idx = _find_parent_and_attr(ast, target_node)
    if parent is None:
        return False

    # Replace
    if idx is None:
        try:
            setattr(parent, attr, simplified_node)
        except Exception:
            return False
    else:
        lst = getattr(parent, attr)
        lst[idx] = simplified_node

    test_code = _ast_to_source(ast, includes)
    if test_code is None:
        # Revert
        if idx is None:
            setattr(parent, attr, target_node)
        else:
            getattr(parent, attr)[idx] = target_node
        return False

    test_hash, _ = compile_to_asm_hash(test_code, tmp_dir, header_dir)

    if test_hash == baseline_hash:
        return True  # Simplification accepted
    else:
        # Revert
        if idx is None:
            setattr(parent, attr, target_node)
        else:
            getattr(parent, attr)[idx] = target_node
        return False


def semantic_clean_file(c_filepath: str, header_dir: str,
                        output_path: str, dry_run: bool = False) -> dict:
    """
    Main function: applies simplification rules to all expressions.
    """
    result = {
        "file": c_filepath,
        "status": "clean",
        "simplifications": [],  # Liste von (rule_name, count)
        "error": None,
    }

    filename = os.path.basename(c_filepath)
    name_no_ext = os.path.splitext(filename)[0]

    with open(c_filepath, "r", encoding="utf-8", errors="replace") as f:
        original_src = f.read()

    includes, _ = _extract_includes(original_src)
    tmp_dir = tempfile.mkdtemp(dir=TMP_ROOT, prefix=f"sem_{name_no_ext}_")

    try:
        # 1. COMPILER CHECK: does the code compile at all?
        baseline_hash, baseline_err = compile_to_asm_hash(
            original_src, tmp_dir, header_dir
        )
        if baseline_hash is None:
            result["status"] = "error"
            result["error"] = f"Baseline compilation failed: {baseline_err}"
            # DO NOT COPY: file is defective and permanently discarded.
            return result

        # 2. PARSER CHECK: can pycparser read the code?
        ast = _parse_to_ast(original_src, header_dir, tmp_dir)
        if ast is None:
            result["status"] = "error"
            result["error"] = "AST parse failed"
            # COPY: file compiles cleanly but pycparser fails.
            if not dry_run:
                shutil.copy2(c_filepath, output_path)
            return result

        # Verify: regenerated code compiles to the same assembly
        regen_code = _ast_to_source(ast, includes)
        if regen_code is None:
            result["status"] = "error"
            result["error"] = "Code regeneration failed"
            # COPY: file compiles cleanly but regeneration fails.
            if not dry_run:
                shutil.copy2(c_filepath, output_path)
            return result

        regen_hash, _ = compile_to_asm_hash(regen_code, tmp_dir, header_dir)
        if regen_hash != baseline_hash:
            # pycparser modified the code → not safe to process
            result["status"] = "clean"
            if not dry_run:
                shutil.copy2(c_filepath, output_path)
            return result

        # === Iterative fixpoint: apply rules until none apply ===
        rule_counts = {name: 0 for name, _ in RULES}
        changed = True
        iterations = 0
        max_iterations = 50  # Safety limit against infinite loops

        while changed and iterations < max_iterations:
            iterations += 1
            changed = False

            # Collect expressions fresh each iteration (AST changes)
            expr_nodes = _collect_expression_nodes(ast)

            for node in expr_nodes:
                # Try all rules
                for rule_name, rule_fn in RULES:
                    try:
                        simplified = rule_fn(node)
                    except Exception:
                        continue

                    if simplified is None:
                        continue
                    if simplified is node:
                        continue

                    # Test
                    if _try_simplify(ast, node, simplified, includes,
                                     tmp_dir, header_dir, baseline_hash):
                        rule_counts[rule_name] += 1
                        changed = True
                        break  # Node was replaced, move to next node

        # Result
        total_simplifications = sum(rule_counts.values())
        if total_simplifications == 0:
            result["status"] = "clean"
            if not dry_run:
                shutil.copy2(c_filepath, output_path)
            return result

        result["simplifications"] = [
            (name, count) for name, count in rule_counts.items() if count > 0
        ]
        result["status"] = "would_simplify" if dry_run else "simplified"

        if not dry_run:
            final_code = _ast_to_source(ast, includes)
            if final_code is None:
                result["status"] = "error"
                result["error"] = "Final code regeneration failed"
                shutil.copy2(c_filepath, output_path)
                return result

            # Safety check
            final_hash, _ = compile_to_asm_hash(final_code, tmp_dir, header_dir)
            if final_hash != baseline_hash:
                result["status"] = "error"
                result["error"] = "Final hash mismatch"
                shutil.copy2(c_filepath, output_path)
                return result

            with open(output_path, "w", encoding="utf-8") as f:
                f.write(final_code)

        return result

    except RecursionError:
        result["status"] = "error"
        result["error"] = "RecursionError"
        # COPY: file is valid, just too deeply nested for Python.
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


# =====================================================================
#  WORKER & MAIN
# =====================================================================

def _worker_init():
    import resource
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except (ValueError, resource.error):
        pass
    sys.setrecursionlimit(50_000)


def _get_header_dir(c_filepath: str) -> str:
    for group in GROUPS:
        if group in c_filepath:
            return os.path.join(HEADERS_DIR, f"{group}_headers")
    return ""


def _worker_fn(args):
    c_filepath, output_path, dry_run = args
    check_disk_space(min_free_gb=2)
    header_dir = _get_header_dir(c_filepath)
    return semantic_clean_file(c_filepath, header_dir, output_path, dry_run)


def main():
    ap = argparse.ArgumentParser(
        description="Semantic code cleanup via expression-level delta debugging (Stage 2)"
    )
    ap.add_argument("--dry-run", action="store_true",
                    help="Analyse only, do not write output")
    ap.add_argument("--group", type=str, default=None,
                    help="Process only a specific group")
    ap.add_argument("-j", "--workers", type=int, default=None,
                    help="Number of parallel workers")
    ap.add_argument("--diagnose", type=str, default=None,
                    help="Analyse a single file")
    ap.add_argument("--input-dir", type=str, default=INPUT_DIR,
                    help="Input directory")
    ap.add_argument("--output-dir", type=str, default=OUTPUT_DIR,
                    help="Output directory")
    ap.add_argument("--overwrite", action="store_true",
                    help="Reprocess already processed files")
    args = ap.parse_args()

    input_dir = args.input_dir
    output_dir = args.output_dir
    num_workers = args.workers or multiprocessing.cpu_count()

    # Diagnose mode
    if args.diagnose:
        c_path = args.diagnose
        header_dir = _get_header_dir(c_path)
        out_path = os.path.join(output_dir, os.path.basename(c_path))

        print(f"=== DIAGNOSE: {c_path} ===")
        print(f"Header dir: {header_dir}")
        print(f"Output: {out_path}\n")

        res = semantic_clean_file(c_path, header_dir, out_path, dry_run=True)

        print(f"Status: {res['status']}")
        if res.get('error'):
            print(f"Error: {res['error']}")
        if res['simplifications']:
            print(f"\nSimplifications:")
            for name, count in res['simplifications']:
                print(f"  {name}: {count}")
            print(f"\nTotal: {sum(c for _, c in res['simplifications'])}")
        return

    # Groups
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
        group_input = os.path.join(input_dir, group)
        group_output = os.path.join(output_dir, group)

        if not os.path.isdir(group_input):
            print(f"  [!] Not found: {group_input}")
            continue

        os.makedirs(group_output, exist_ok=True)

        for fname in os.listdir(group_input):
            if fname.endswith(".c"):
                c_path = os.path.join(group_input, fname)
                out_path = os.path.join(group_output, fname)
                if not args.overwrite and os.path.exists(out_path):
                    skipped_existing += 1
                    continue
                all_tasks.append((c_path, out_path, args.dry_run))

    print(f"Found: {len(all_tasks) + skipped_existing} files")
    if skipped_existing > 0:
        print(f"Skipped: {skipped_existing}")
    print(f"To process: {len(all_tasks)}")
    print(f"Worker:   {num_workers}")
    print(f"Input:    {input_dir}")
    print(f"Output:   {output_dir}")
    print(f"Method:   Expression-level delta debugging (semantic cleanup)")
    if args.dry_run:
        print("=== DRY RUN ===\n")

    if not all_tasks:
        print("\nNothing to do.")
        return

    # Startup cleanup
    tmp_root = TMP_ROOT
    stale = 0
    try:
        for d in os.listdir(tmp_root):
            if d.startswith("sem_") and os.path.isdir(os.path.join(tmp_root, d)):
                shutil.rmtree(os.path.join(tmp_root, d), ignore_errors=True)
                stale += 1
        if stale:
            print(f"Cleaned up: {stale} stale tmp directories")
    except OSError:
        pass

    stats = {"clean": 0, "simplified": 0, "would_simplify": 0, "error": 0}
    total_simplifications = 0
    rule_totals = {}
    error_log_path = os.path.join(output_dir, "semantic_errors.jsonl")

    with open(os.path.join(output_dir, "semantic_clean.jsonl"), "a") as log_file, \
         open(error_log_path, "a") as error_log:
        with multiprocessing.Pool(num_workers, initializer=_worker_init) as pool:
            try:
                it = pool.imap_unordered(_worker_fn, all_tasks, chunksize=1)
                processed = 0

                for res in tqdm(it, total=len(all_tasks), desc="Semantic cleanup"):
                    processed += 1
                    stats[res["status"]] = stats.get(res["status"], 0) + 1

                    n_simp = sum(c for _, c in res.get("simplifications", []))
                    if n_simp > 0:
                        total_simplifications += n_simp
                        for name, count in res["simplifications"]:
                            rule_totals[name] = rule_totals.get(name, 0) + count
                        if args.dry_run:
                            tqdm.write(
                                f"  {os.path.basename(res['file'])}: "
                                f"{n_simp} simplifications"
                            )
                        log_file.write(json.dumps(res, ensure_ascii=False) + "\n")
                        log_file.flush()

                    if res["status"] == "error":
                        error_log.write(json.dumps({
                            "timestamp": datetime.now().isoformat(),
                            "file": res["file"],
                            "filename": os.path.basename(res["file"]),
                            "error": res.get("error", "unknown"),
                        }, ensure_ascii=False) + "\n")
                        error_log.flush()
                        tqdm.write(f"  [!] {os.path.basename(res['file'])}: "
                                   f"{res.get('error', '?')}")

                    # Periodic cleanup (>10 min old)
                    if processed % 200 == 0:
                        try:
                            now = time.time()
                            for d in os.listdir(tmp_root):
                                if not d.startswith("sem_"):
                                    continue
                                dp = os.path.join(tmp_root, d)
                                if not os.path.isdir(dp):
                                    continue
                                try:
                                    if now - os.path.getmtime(dp) > 600:
                                        shutil.rmtree(dp, ignore_errors=True)
                                except OSError:
                                    pass
                        except OSError:
                            pass

            except KeyboardInterrupt:
                print("\n\nAborted!")
                pool.terminate()
                pool.join()
                sys.exit(1)

    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"  Total files:            {len(all_tasks)}")
    print(f"  Already clean:          {stats['clean']}")
    print(f"  Simplified:             {stats.get('simplified', 0) + stats.get('would_simplify', 0)}")
    print(f"  Total simplifications:  {total_simplifications}")
    print(f"  Errors:                 {stats['error']}")
    if rule_totals:
        print(f"\n  Rule breakdown:")
        for name in sorted(rule_totals, key=lambda n: -rule_totals[n]):
            print(f"    {name:20s}: {rule_totals[name]}")
    print(f"\nOutput: {output_dir}")
    if stats['error'] > 0:
        print(f"Error log: {error_log_path}")


if __name__ == "__main__":
    main()
