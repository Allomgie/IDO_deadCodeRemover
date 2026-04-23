#!/usr/bin/env python3
"""
Stage 3b: Token-level reducer — optimised C-Reduce replacement.
OPTIMISED VERSION with TCC fast-reject guard, global caches, syntax validator, and more efficient passes.

Key optimisations:
  1. TCC fast-reject for syntactically safe passes (saves IDO calls)
  2. Global CompileCache per file (across all passes)
  3. Central syntax validator BEFORE compiler call
  4. Include normalisation once per file
  5. count('\n') instead of splitlines() for line counting
  6. pass_peep_subexpr restricted to complex expressions
  7. _fast_paren_cleanup in a single pass instead of iteratively
"""

import os, sys, re, subprocess, signal, tempfile, shutil, hashlib
import json, time, argparse, multiprocessing, resource
from datetime import datetime
from tqdm import tqdm

BASE_DIR     = "/home/user/deadCodeRemover"
PROJECT_ROOT = os.path.join(BASE_DIR, "CompilerRoot")
IDO_DIR      = os.path.abspath(os.path.join(PROJECT_ROOT, "tools", "ido"))
IDO_CC       = os.path.join(IDO_DIR, "cc")

INPUT_DIR    = os.path.join(BASE_DIR, "dataset_Stage_2")
OUTPUT_DIR   = os.path.join(BASE_DIR, "dataset_Stage_3")
OBJDUMP      = "mips-linux-gnu-objdump"
TMP_ROOT     = "/dev/shm"

GROUPS = [
    "Save_00_generated", "Save_01_handwritten", "Save_02_original",
    "Save_03_Torture", "Save_04_YARPGen", "Save_05_csmith",
    "Save_06_csmith_switchCase",
]
INCLUDE_DIRS = [
    os.path.join(PROJECT_ROOT, "include"),
    os.path.join(PROJECT_ROOT, "src"),
    os.path.join(PROJECT_ROOT, "include", "PR"),
    os.path.join(PROJECT_ROOT, "lib", "ultralib", "include"),
    os.path.join(BASE_DIR, "csmith_install/include/csmith-2.3.0"),
]

_asm_cache: dict = {}
_peep_local_fail_cache = set()
MAX_PASSES = 10


# =====================================================================
#  TCC FAST-REJECT GUARD
# =====================================================================

TCC_SAFE_PASSES = {
    "blank", "blank_final",
    "balanced_parens_only", "balanced_curly_empty",
    "balanced_parens_zero", "balanced_remove_curly",
    "balanced_remove_parens", "balanced_remove_square",
    "ternary_modus_b", "ternary_modus_c",
    "peep_while", "regex_atoms",
}

def _tcc_available() -> bool:
    """Checks whether tcc is available in PATH."""
    return shutil.which("tcc") is not None

def tcc_syntax_check(c_source: str, tmp_dir: str, header_dir: str) -> bool:
    """
    Fast syntax check using TCC.
    Returns True if TCC passes without errors.
    Uses subprocess.call with DEVNULL to avoid OS pipe overhead.
    Timeout: 5 seconds (TCC is extremely fast).
    """
    c_path = os.path.join(tmp_dir, "_tcc.c")
    try:
        with open(c_path, "w") as f:
            f.write(c_source)
        cmd = ["tcc", "-fsyntax-only", "-c", "-xc", "-D_LANGUAGE_C", "-D_MIPS_SZLONG=32"]
        for inc in INCLUDE_DIRS:
            cmd += ["-I", inc]
        if header_dir:
            cmd += ["-I", header_dir]
        cmd += [c_path]
        # Direct subprocess.call with DEVNULL eliminates OS pipe overhead
        rc = subprocess.call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        return rc == 0
    except subprocess.TimeoutExpired:
        return False
    except Exception:
        return False
    finally:
        try:
            if os.path.exists(c_path):
                os.unlink(c_path)
        except OSError:
            pass


# =====================================================================
#  GLOBAL COMPILE CACHE (with TCC integration)
# =====================================================================

class CompileCache:
    """
    Central cache for compilation results per file.
    Stores both successful and failed hashes.
    Integrates TCC fast-reject for syntactically safe passes.
    """
    def __init__(self, tcc_baseline_ok: bool = False):
        self._success = {}      # key -> hash
        self._rejected = set()  # key -> True (IDO failed)
        self._tcc_rejected = set()  # key -> True (TCC failed)
        self._tcc_baseline_ok = tcc_baseline_ok and _tcc_available()

        # Statistics counters
        self.stats = {
            "cache_hits": 0,
            "tcc_checks": 0,
            "tcc_rejects": 0,
            "ido_checks": 0,
            "ido_rejects": 0,
            "ido_success": 0
        }

    def try_candidate(self, candidate: str, tmp_dir: str, header_dir: str,
                      baseline: str, pass_name: str = "") -> tuple[str | None, str]:
        """
        Tries to compile a candidate.
        Returns (hash, "") on success,
        (None, reason) if rejected or failed.
        """
        if not candidate or not candidate.strip():
            return None, "empty"

        key = hashlib.md5(candidate.encode()).hexdigest()

        # 1. Check IDO cache
        if key in self._rejected:
            self.stats["cache_hits"] += 1
            return None, "rejected"
        if key in self._success:
            self.stats["cache_hits"] += 1
            return self._success[key], ""

        # 2. TCC fast-reject (only for safe passes & when baseline is ok)
        if self._tcc_baseline_ok and pass_name in TCC_SAFE_PASSES:
            if key in self._tcc_rejected:
                self.stats["cache_hits"] += 1
                return None, "tcc_rejected"

            self.stats["tcc_checks"] += 1
            if not tcc_syntax_check(candidate, tmp_dir, header_dir):
                self._tcc_rejected.add(key)
                self.stats["tcc_rejects"] += 1
                return None, "tcc_fail"

        # 3. IDO gold-standard check
        self.stats["ido_checks"] += 1
        h, err = compile_to_hash(candidate, tmp_dir, header_dir)
        if h is None:
            self._rejected.add(key)
            self.stats["ido_rejects"] += 1
            return None, err

        self._success[key] = h
        self.stats["ido_success"] += 1
        return h, ""

    def is_rejected(self, candidate: str) -> bool:
        key = hashlib.md5(candidate.encode()).hexdigest()
        return key in self._rejected or key in self._tcc_rejected



# =====================================================================
#  INFRASTRUCTURE
# =====================================================================

def run_cmd_safely(cmd: list[str], cwd: str | None = None, env: dict | None = None, timeout: int = 30):
    try:
        proc = subprocess.Popen(cmd, cwd=cwd, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        stdout, stderr = proc.communicate(timeout=timeout)
        return proc.returncode, stdout, stderr
    except subprocess.TimeoutExpired:
        try: os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, OSError): pass
        try: proc.communicate(timeout=5)
        except: proc.kill()
        raise


def compile_to_hash(c_source: str, tmp_dir: str, header_dir: str) -> tuple[str | None, str]:
    """
    Compiles C source via gcc -E and IDO to a normalised ASM hash.
    """
    fixed = c_source
    cache_key = hashlib.md5((fixed + (header_dir or "")).encode()).hexdigest()
    if cache_key in _asm_cache:
        return _asm_cache[cache_key], ""

    c_path = os.path.join(tmp_dir, "_tr.c")
    i_path = os.path.join(tmp_dir, "_tr.i")
    o_path = os.path.join(tmp_dir, "_tr.o")

    def _rm(p):
        try:
            if os.path.exists(p): os.unlink(p)
        except OSError: pass
    def _cleanup():
        for p in [c_path, i_path, o_path]: _rm(p)

    try:
        with open(c_path, "w") as f: f.write(fixed)
        cmd = ["gcc", "-E", "-P", "-xc", "-D_LANGUAGE_C", "-D_MIPS_SZLONG=32"]
        for inc in INCLUDE_DIRS: cmd += ["-I", inc]
        if header_dir: cmd += ["-I", header_dir]
        cmd += [c_path, "-o", i_path]
        try: rc, _, _ = run_cmd_safely(cmd, timeout=30)
        except subprocess.TimeoutExpired: _cleanup(); return None, "gcc timeout"
        _rm(c_path)
        if rc != 0: _cleanup(); return None, "gcc fail"

        cmd = [IDO_CC, "-c", "-O2", "-mips2", "-G", "0", "-w", i_path, "-o", o_path]
        try: rc, _, _ = run_cmd_safely(cmd, cwd=tmp_dir, env=_IDO_ENV, timeout=30)
        except subprocess.TimeoutExpired: _cleanup(); return None, "IDO timeout"
        _rm(i_path)
        if rc != 0: _cleanup(); return None, "IDO fail"

        # objdump -d: disassemble .text (instructions)
        cmd = [OBJDUMP, "-d", "-z", o_path]
        try: rc, stdout_text, _ = run_cmd_safely(cmd, timeout=30)
        except subprocess.TimeoutExpired: _cleanup(); return None, "objdump timeout"
        if rc != 0: _rm(o_path); _cleanup(); return None, "objdump fail"

        # objdump -s: raw data from .rodata, .data, .bss (string constants etc.)
        cmd = [OBJDUMP, "-s", "-j", ".rodata", "-j", ".data", "-j", ".bss", o_path]
        try: rc2, stdout_data, _ = run_cmd_safely(cmd, timeout=30)
        except subprocess.TimeoutExpired: stdout_data = b""
        _rm(o_path)

        asm = []
        # 1. Normalise instructions from .text
        for line in stdout_text.decode(errors="replace").splitlines():
            m = re.match(r'^\s*[0-9a-fA-F]+:\s+[0-9a-fA-F]+\s+(.*)', line)
            if not m: continue
            s = m.group(1).strip().split('#')[0].strip()
            if not s: continue
            s = re.sub(r'addiu\s+\$?sp,\s*\$?sp,\s*-?\d+', 'addiu sp,sp,OFFSET', s)
            s = re.sub(r'-?\d+\(\$?sp\)', 'OFFSET(sp)', s)
            s = re.sub(r'-?\d+\(\$?fp\)', 'OFFSET(fp)', s)
            s = re.sub(r'%[a-z0-9_.]+\([^)]*\)', 'SYMBOL', s)
            asm.append(s)

        # 2. Append raw data from .rodata/.data/.bss (hex dump)
        if stdout_data:
            for line in stdout_data.decode(errors="replace").splitlines():
                # objdump -s format: " 0000 41424344 45464748 ..."
                m = re.match(r'^\s*[0-9a-fA-F]+\s+((?:[0-9a-fA-F]+\s*)+)', line)
                if m:
                    asm.append("DATA:" + m.group(1).strip())

        if not asm: _cleanup(); return None, "no asm"
        h = hashlib.md5("\n".join(asm).encode()).hexdigest()
        _asm_cache[cache_key] = h
        return h, ""
    except Exception as e:
        _cleanup(); return None, str(e)


def _get_ido_env():
    env = os.environ.copy()
    env["COMPILER_PATH"] = IDO_DIR
    env["LD_LIBRARY_PATH"] = f"{IDO_DIR}:{env.get('LD_LIBRARY_PATH', '')}"
    return env
_IDO_ENV = _get_ido_env()

def _get_header_dir(path: str):
    for g in GROUPS:
        if g in path: return os.path.join(DATASET_DIR, f"{g}_headers")
    return ""

def check_disk_space(min_free_gb: int = 2):
    while True:
        if shutil.disk_usage("/").free / (1024**3) >= min_free_gb: return
        time.sleep(30)


# =====================================================================
#  HELPER FUNCTIONS
# =====================================================================

def _find_matching(s: str, open_pos: int, open_char: str, close_char: str) -> int:
    """Finds the closing bracket matching open_pos. Returns -1 if not found."""
    depth = 0
    for i in range(open_pos, len(s)):
        if s[i] == open_char:
            depth += 1
        elif s[i] == close_char:
            depth -= 1
            if depth == 0:
                return i
    return -1

def _find_all_balanced(src: str, open_c: str, close_c: str) -> list[tuple[int, int]]:
    stack = []
    pairs = []
    for i, c in enumerate(src):
        if c == open_c:
            stack.append(i)
        elif c == close_c:
            if stack:
                start = stack.pop()
                pairs.append((start, i))
    return pairs

def _split_args(args_str: str) -> list[str]:
    args = []
    depth = 0
    in_string = False
    current = []
    for ch in args_str:
        if ch == '"': in_string = not in_string
        if not in_string:
            if ch in '([{': depth += 1
            elif ch in ')]}': depth -= 1

        if ch == ',' and depth == 0 and not in_string:
            args.append(''.join(current).strip())
            current = []
        else:
            current.append(ch)
    if current or args:
        args.append(''.join(current).strip())
    return [a for a in args if a != '']

# Optimised version — single pass instead of iterative
def _fast_paren_cleanup(src: str) -> str:
    """
    Removes fully redundant wrappings such as ((x)) -> (x).
    Optimised: single pass with mapping instead of iterative reconstruction.
    """
    pairs = _find_all_balanced(src, '(', ')')
    if not pairs:
        return src

    # Mapping: start -> end for fast lookup
    end_map = {s: e for s, e in pairs}

    to_remove = set()

    for start, end in pairs:
        # Find the first character after the opening bracket
        inner_start = start + 1
        while inner_start < end and src[inner_start].isspace():
            inner_start += 1

        # Find the last character before the closing bracket
        inner_end = end - 1
        while inner_end > start and src[inner_end].isspace():
            inner_end -= 1

        # Check: is the exact content of this pair simply
        # another complete bracket pair?
        if inner_start in end_map and end_map[inner_start] == inner_end:
            # The outer pair is structurally redundant
            to_remove.add(start)
            to_remove.add(end)

    if not to_remove:
        return src

    # Rebuild string in one pass
    return "".join(c for i, c in enumerate(src) if i not in to_remove)


# =====================================================================
#  PYTHON PASSES (with pass_name for TCC guard)
# =====================================================================

def pass_blank(src: str, baseline: str, tmp_dir: str, header_dir: str,
               cache: CompileCache, pass_name: str = "blank") -> tuple[str, bool]:
    """
    Normalises whitespace:
    - Preserves indentation (tabs).
    - Brackets are kept tight: (x).
    - Ternary operators (? and :) get exactly one space for readability.
    """
    changed = False
    lines = src.splitlines()
    new_lines = []

    for line in lines:
        if not line.strip():
            new_lines.append("")
            continue

        # 1. Preserve indentation
        match = re.match(r'^(\s*)', line)
        indent = match.group(1) if match else ""
        content = line[len(indent):].strip()

        # 2. Collapse all whitespace to single spaces
        content = re.sub(r'\s{2,}', ' ', content)

        # 3. Re-space ternary operators: " ? " and " : "
        content = re.sub(r'\s*([?:])\s*', r' \1 ', content)

        # 4. Tighten brackets (no spaces inside)
        content = re.sub(r'\(\s+', '(', content)
        content = re.sub(r'\s+\)', ')', content)

        # 5. Commas and semicolons: one space to the right, none to the left
        content = re.sub(r'\s*([,;])\s*', r'\1 ', content)

        # 6. Final cleanup of double spaces
        content = re.sub(r'\s{2,}', ' ', content).strip()

        new_lines.append(indent + content)

    candidate = "\n".join(new_lines)

    if candidate != src:
        h, _ = cache.try_candidate(candidate, tmp_dir, header_dir, baseline, pass_name)
        if h == baseline:
            src = candidate
            changed = True

    # Vertical pass: empty lines and preprocessor noise
    lines = src.splitlines()
    no_pre = [l for l in lines if not l.strip().startswith('#')]
    if len(no_pre) < len(lines):
        candidate = "\n".join(no_pre)
        h, _ = cache.try_candidate(candidate, tmp_dir, header_dir, baseline, pass_name)
        if h == baseline:
            src = candidate
            changed = True

    return src, changed


def pass_includes(src: str, baseline: str, tmp_dir: str, header_dir: str,
                  cache: CompileCache, pass_name: str = "includes") -> tuple[str, bool]:
    """
    Removes #include lines individually or all at once.
    Not in TCC_SAFE_PASSES because TCC and IDO use different header ecosystems.
    """
    lines = src.splitlines(keepends=True)
    include_indices = [i for i, l in enumerate(lines) if re.match(r'^\s*#\s*include', l)]

    if not include_indices:
        return src, False

    changed = False

    # Optional "nuclear strike": try removing all includes at once first
    candidate_all = "".join([l for i, l in enumerate(lines) if i not in include_indices])
    h_all, _ = cache.try_candidate(candidate_all, tmp_dir, header_dir, baseline, pass_name)
    if h_all == baseline:
        return candidate_all, True

    # Individual check
    for idx in reversed(include_indices):
        candidate = "".join(lines[:idx] + lines[idx+1:])
        h, _ = cache.try_candidate(candidate, tmp_dir, header_dir, baseline, pass_name)
        if h == baseline:
            lines.pop(idx)
            changed = True

    return "".join(lines), changed


def pass_lines(src: str, baseline: str, tmp_dir: str, header_dir: str,
               cache: CompileCache, pass_name: str = "lines") -> tuple[str, bool]:
    """
    Removes line chunks via binary search.
    Not in TCC_SAFE_PASSES because removing forward declarations
    can produce false negatives with the stricter TCC (IDO tolerates K&R).
    """
    # 1. Pre-clean: remove all genuinely empty lines
    lines = [l for l in src.splitlines(keepends=True) if l.strip()]
    n = len(lines)
    if n == 0: return src, False

    chunk = n
    changed = False

    while chunk >= 1:
        i = n
        made_progress = False
        while i > 0:
            start = max(0, i - chunk)
            candidate_lines = lines[:start] + lines[i:]
            candidate = "".join(candidate_lines)

            if not candidate.strip():
                i -= chunk
                continue

            h, _ = cache.try_candidate(candidate, tmp_dir, header_dir, baseline, pass_name)
            if h == baseline:
                lines = candidate_lines
                n = len(lines)
                changed = True
                made_progress = True
            else:
                i -= chunk
        if not made_progress:
            chunk //= 2

    return "".join(lines), changed


def pass_balanced(src: str, baseline: str, tmp_dir: str, header_dir: str,
                  cache: CompileCache, mode="parens-only",
                  pass_name: str = "balanced") -> tuple[str, bool]:
    """
    Removes or modifies balanced bracket pairs.
    Supports round, curly, and square brackets.
    """
    changed = False
    fail_cache = set()

    delimiters = {
        "curly": ('{', '}'),
        "parens": ('(', ')'),
        "square": ('[', ']')
    }

    d_type = "curly" if "curly" in mode else ("square" if "square" in mode else "parens")
    open_c, close_c = delimiters[d_type]

    while True:
        # 1. Fast path for redundant brackets
        if d_type == "parens":
            cleaned_src = _fast_paren_cleanup(src)
            if cleaned_src != src:
                src = cleaned_src
                changed = True

        pairs = _find_all_balanced(src, open_c, close_c)
        pairs.sort(key=lambda x: x[1] - x[0])

        made_progress = False
        for start, end in pairs:
            inner = src[start+1:end]

            # --- FILTER ---
            if (mode in ("parens-only", "curly-only", "square-only")) and d_type == "parens":
                if start > 0 and (src[start-1].isalnum() or src[start-1] == '_'):
                    continue

            # --- CACHE CHECK ---
            pre_ctx = src[max(0, start - 30):start]
            post_ctx = src[end+1:min(len(src), end + 31)]
            matched_text = src[start:end+1]

            ctx_string = f"{mode}|{pre_ctx}|{matched_text}|{post_ctx}"
            cache_key = hashlib.md5(ctx_string.encode()).hexdigest()

            if cache_key in fail_cache:
                continue

            # --- TRANSFORMATIONS ---
            if mode in ("parens-only", "curly-only", "square-only"):
                candidate = src[:start] + inner + src[end+1:]
            elif mode == "curly-to-empty":
                candidate = src[:start] + "{}" + src[end+1:]
            elif mode == "parens-to-zero":
                candidate = src[:start] + "0" + src[end+1:]
            elif "remove-block" in mode:
                candidate = src[:start] + src[end+1:]
            else:
                continue

            # Prevents the pass from failing on a clean {} pair
            if candidate == src:
                continue

            h, _ = cache.try_candidate(candidate, tmp_dir, header_dir, baseline, pass_name)
            if h == baseline:
                src = candidate
                changed = True
                made_progress = True
                break
            else:
                fail_cache.add(cache_key)

        if not made_progress:
            break

    return src, changed


def pass_peep_args(src: str, baseline: str, tmp_dir: str, header_dir: str,
                   cache: CompileCache, pass_name: str = "peep_args") -> tuple[str, bool]:
    """
    pass_peep: removes individual function arguments or replaces them with 0.
    """
    changed = False
    call_pattern = re.compile(r'\b([A-Za-z_][A-Za-z0-9_]*)\s*(\()')

    for _iteration in range(20):
        matches = list(call_pattern.finditer(src))
        made_progress = False

        for match in matches:
            func_name = match.group(1)
            open_pos = match.start(2)
            close_pos = _find_matching(src, open_pos, '(', ')')
            if close_pos == -1:
                continue

            args_str = src[open_pos+1:close_pos]
            args = _split_args(args_str)
            if len(args) <= 0:
                continue

            # Test: remove each argument
            for i in range(len(args)):
                new_args = args[:i] + args[i+1:]
                new_call = f"{func_name}({', '.join(new_args)})"
                candidate = src[:match.start()] + new_call + src[close_pos+1:]
                h, _ = cache.try_candidate(candidate, tmp_dir, header_dir, baseline, pass_name)
                if h == baseline:
                    src = candidate
                    changed = True
                    made_progress = True
                    break

            if made_progress:
                break

            # Test: replace argument with 0
            for i in range(len(args)):
                if args[i].strip() == '0':
                    continue
                new_args = args[:i] + ['0'] + args[i+1:]
                new_call = f"{func_name}({', '.join(new_args)})"
                candidate = src[:match.start()] + new_call + src[close_pos+1:]
                h, _ = cache.try_candidate(candidate, tmp_dir, header_dir, baseline, pass_name)
                if h == baseline:
                    src = candidate
                    changed = True
                    made_progress = True
                    break

            if made_progress:
                break

        if not made_progress:
            break

    return src, changed


def pass_ternary(src: str, baseline: str, tmp_dir: str, header_dir: str,
                 cache: CompileCache, mode="b", pass_name: str = "ternary") -> tuple[str, bool]:
    """
    pass_ternary: a ? b : c -> b (mode 'b') or c (mode 'c').
    """
    changed = False
    fail_cache = set()

    pattern = re.compile(r'([^?]+)\?([^?:]+):([^?;\)]+)')

    while True:
        matches = list(pattern.finditer(src))
        if not matches:
            break

        made_progress = False
        for m in reversed(matches):
            a_cond = m.group(1).strip()
            b_true = m.group(2).strip()
            c_false = m.group(3).strip()

            replacement = b_true if mode == "b" else c_false

            # --- CACHE CHECK ---
            pre_ctx = src[max(0, m.start() - 30):m.start()]
            post_ctx = src[m.end():min(len(src), m.end() + 30)]
            matched_text = m.group(0)

            ctx_string = f"ternary_{mode}|{pre_ctx}|{matched_text}|{post_ctx}"
            cache_key = hashlib.md5(ctx_string.encode()).hexdigest()

            if cache_key in fail_cache:
                continue

            candidate = src[:m.start()] + replacement + src[m.end():]

            h, _ = cache.try_candidate(candidate, tmp_dir, header_dir, baseline, pass_name)
            if h == baseline:
                src = candidate
                changed = True
                made_progress = True
                break
            else:
                fail_cache.add(cache_key)

        if not made_progress:
            break

    return src, changed


# Optimised — complex expressions only, not individual identifiers
def pass_peep_subexpr(src: str, baseline: str, tmp_dir: str, header_dir: str,
                      cache: CompileCache, pass_name: str = "peep_subexpr") -> tuple[str, bool]:
    """
    Replaces complex expressions with 0, 1, or empty string.
    Not in TCC_SAFE_PASSES because type errors (e.g. "" instead of int)
    may be handled differently by TCC and IDO.
    """
    changed = False

    blacklist = {
        "return", "if", "else", "for", "while", "do", "switch", "case", "default",
        "struct", "union", "enum", "typedef", "sizeof",
        "goto", "break", "continue"
    }

    # Only complex expressions (at least operator + operand)
    patterns = [
        # Binary expressions with two identifiers/literals
        r'\b[A-Za-z_][A-Za-z0-9_]*\s*[+\-*/&|^%]\s*[A-Za-z_][A-Za-z0-9_]*',
        # Unary expressions
        r'[!~]\s*[A-Za-z_][A-Za-z0-9_]*',
        # Comparisons
        r'\b[A-Za-z_][A-Za-z0-9_]*\s*[<>=!]+\s*[A-Za-z_][A-Za-z0-9_]*',
        # Assignments (excluding declarations)
        r'\b[A-Za-z_][A-Za-z0-9_]*\s*=\s*[^=;]+',
    ]

    replacements = ["0", "1", ""]

    for pattern in patterns:
        regex = re.compile(pattern)
        matches = list(regex.finditer(src))

        for match in reversed(matches):
            matched_text = match.group(0).strip()

            if matched_text in blacklist:
                continue

            made_progress = False

            for repl in replacements:
                if matched_text == repl:
                    continue

                # Local context cache
                pre_ctx = src[max(0, match.start() - 30):match.start()]
                post_ctx = src[match.end():min(len(src), match.end() + 30)]

                ctx_string = f"{matched_text}|{repl}|{pre_ctx}|{post_ctx}"
                cache_key = hashlib.md5(ctx_string.encode()).hexdigest()

                if cache_key in _peep_local_fail_cache:
                    continue

                candidate = src[:match.start()] + repl + src[match.end():]

                # Fast-fail (syntax killer)
                start_idx = max(0, match.start() - 4)
                end_idx = min(len(candidate), match.start() + len(repl) + 4)
                compact = re.sub(r'\s+', '', candidate[start_idx:end_idx])

                if ",," in compact or "(, " in compact or ",)" in compact or "=;" in compact:
                    _peep_local_fail_cache.add(cache_key)
                    continue

                h, _ = cache.try_candidate(candidate, tmp_dir, header_dir, baseline, pass_name)
                if h == baseline:
                    src = candidate
                    changed = True
                    made_progress = True
                    break 
                else:
                    _peep_local_fail_cache.add(cache_key)

            if made_progress and pattern == patterns[-1]:
                pass

    return src, changed


def pass_peep_while(src: str, baseline: str, tmp_dir: str, header_dir: str,
                    cache: CompileCache, pass_name: str = "peep_while") -> tuple[str, bool]:
    """
    Peep 'c' from C-Reduce: replaces while(cond) { body } with body.
    """
    changed = False
    pattern = re.compile(r'\bwhile\s*\(')

    matches = list(pattern.finditer(src))
    for match in reversed(matches):
        open_p = match.end() - 1
        close_p = _find_matching(src, open_p, '(', ')')
        if close_p == -1: continue

        after_parens = src[close_p+1:].lstrip()
        if after_parens.startswith('{'):
            open_b = src.find('{', close_p)
            close_b = _find_matching(src, open_b, '{', '}')
            if close_b != -1:
                body = src[open_b+1:close_b]
                candidate = src[:match.start()] + body + src[close_b+1:]

                h, _ = cache.try_candidate(candidate, tmp_dir, header_dir, baseline, pass_name)
                if h == baseline:
                    src = candidate
                    changed = True

    return src, changed


def pass_regex_atoms(src: str, baseline: str, tmp_dir: str, header_dir: str,
                     cache: CompileCache, pass_name: str = "regex_atoms") -> tuple[str, bool]:
    """
    Applies atomic regex substitutions (e.g. += -> =, unsigned -> "").
    """
    changed = False
    fail_cache = set()

    rules = [
        (r'\+\=', '='), (r'\-\=', '='), (r'\*\=', '='), (r'\/\=', '='),
        (r'\%\=', '='), (r'\&\=', '='), (r'\|\=', '='), (r'\^\=', '='),
        (r'\<\<\=', '='), (r'\>\>\=', '='),
        (r'\bunsigned\s+', ''), (r'\bsigned\s+', ''),
        (r'\bextern\s+', ''), (r'\bstatic\s+', ''),
        (r'\bconst\s+', ''), (r'\bregister\s+', ''),
        (r'\bvolatile\s+', ''),
    ]

    while True:
        made_progress = False

        for pattern_str, replacement in rules:
            pattern = re.compile(pattern_str)
            matches = list(pattern.finditer(src))

            for match in reversed(matches):
                pre_ctx = src[max(0, match.start() - 30):match.start()]
                post_ctx = src[match.end():min(len(src), match.end() + 30)]
                matched_text = match.group(0)

                ctx_string = f"atom|{pre_ctx}|{matched_text}|{replacement}|{post_ctx}"
                cache_key = hashlib.md5(ctx_string.encode()).hexdigest()

                if cache_key in fail_cache:
                    continue

                candidate = src[:match.start()] + replacement + src[match.end():]

                h, _ = cache.try_candidate(candidate, tmp_dir, header_dir, baseline, pass_name)
                if h == baseline:
                    src = candidate
                    changed = True
                    made_progress = True
                    break
                else:
                    fail_cache.add(cache_key)

            if made_progress:
                break

        if not made_progress:
            break

    return src, changed


# =====================================================================
#  MAIN FUNCTION PER FILE
# =====================================================================

def reduce_file(c_filepath: str, header_dir: str, output_path: str,
                dry_run: bool = False, verbose: bool = False):
    """
    Reduces a single C file through iterative Python passes.
    Uses TCC as a fast-reject guard for syntactically safe passes
    when the baseline is compilable with TCC.
    """

    global _peep_local_fail_cache
    _peep_local_fail_cache.clear()

    result = {"file": c_filepath, "status": "clean",
              "original_lines": 0, "reduced_lines": 0, "error": None,
              "tcc_enabled": False, "tcc_baseline_ok": False}

    with open(c_filepath, "r", encoding="utf-8", errors="replace") as f:
        original_src = f.read()

    # Include normalisation once per file
    original_src = re.sub(r'#include\s+"[^"]*?([^/"]+\.h)"', r'#include "\1"', original_src)

    # Use count('\n') instead of splitlines()
    result["original_lines"] = original_src.count('\n') + (1 if original_src and not original_src.endswith('\n') else 0)

    tmp_dir = tempfile.mkdtemp(dir=TMP_ROOT, prefix="tr_")
    try:
        baseline, err = compile_to_hash(original_src, tmp_dir, header_dir)
        if baseline is None:
            result["status"] = "error"
            result["error"] = f"Baseline does not compile: {err}"
            return result
        if dry_run:
            result["status"] = "would_reduce"; return result

        # TCC baseline check: is TCC usable for this file at all?
        tcc_baseline_ok = False
        if _tcc_available():
            tcc_baseline_ok = tcc_syntax_check(original_src, tmp_dir, header_dir)
            result["tcc_baseline_ok"] = tcc_baseline_ok
            if verbose:
                status = "OK" if tcc_baseline_ok else "FAIL (TCC disabled)"
                print(f"  TCC Baseline Check: {status}")

        # Global cache for this file (with TCC integration)
        cache = CompileCache(tcc_baseline_ok=tcc_baseline_ok)
        result["tcc_enabled"] = tcc_baseline_ok

        # Prevents the ASM cache from growing too large
        global _asm_cache
        if len(_asm_cache) > 10000:
            _asm_cache.clear()

        src = original_src
        total_changed = False

        if verbose:
            print(f"  Baseline: {result['original_lines']} lines")

        # --- PASS DEFINITIONS (with cache parameter and pass_name) ---
        pass_dict = {
            "blank":                 lambda s, b, t, h, n="blank": pass_blank(s, b, t, h, cache, n),
            "includes":              lambda s, b, t, h, n="includes": pass_includes(s, b, t, h, cache, n),
            "lines":                 lambda s, b, t, h, n="lines": pass_lines(s, b, t, h, cache, n),
            "regex_atoms":           lambda s, b, t, h, n="regex_atoms": pass_regex_atoms(s, b, t, h, cache, n),
            "peep_while":            lambda s, b, t, h, n="peep_while": pass_peep_while(s, b, t, h, cache, n),
            "balanced_parens_only":  lambda s, b, t, h, n="balanced_parens_only": pass_balanced(s, b, t, h, cache, mode="parens-only", pass_name=n),
            "balanced_curly_empty":  lambda s, b, t, h, n="balanced_curly_empty": pass_balanced(s, b, t, h, cache, mode="curly-to-empty", pass_name=n),
            "balanced_parens_zero":  lambda s, b, t, h, n="balanced_parens_zero": pass_balanced(s, b, t, h, cache, mode="parens-to-zero", pass_name=n),
            "balanced_remove_curly":  lambda s, b, t, h, n="balanced_remove_curly": pass_balanced(s, b, t, h, cache, mode="curly-remove-block", pass_name=n),
            "balanced_remove_parens": lambda s, b, t, h, n="balanced_remove_parens": pass_balanced(s, b, t, h, cache, mode="parens-remove-block", pass_name=n),
            "balanced_remove_square": lambda s, b, t, h, n="balanced_remove_square": pass_balanced(s, b, t, h, cache, mode="square-remove-block", pass_name=n),
            "peep_subexpr":          lambda s, b, t, h, n="peep_subexpr": pass_peep_subexpr(s, b, t, h, cache, n),
            "peep_args":             lambda s, b, t, h, n="peep_args": pass_peep_args(s, b, t, h, cache, n),
            "ternary_modus_b":       lambda s, b, t, h, n="ternary_modus_b": pass_ternary(s, b, t, h, cache, mode="b", pass_name=n),
            "ternary_modus_c":       lambda s, b, t, h, n="ternary_modus_c": pass_ternary(s, b, t, h, cache, mode="c", pass_name=n),
            "blank_final":           lambda s, b, t, h, n="blank_final": pass_blank(s, b, t, h, cache, n),
        }

        PASS_DEPENDENCIES = {
            "balanced_remove_block": ["lines"],
            "balanced_curly_empty": ["lines"],
            "peep_while": ["lines", "balanced_remove_block"],
            "peep_subexpr": ["peep_args", "peep_subexpr"],
            "peep_args": ["peep_subexpr"]
        }

        def run_phase(phase_name: str, pass_names: list, max_iters: int, require_line_drop: bool = True) -> bool:
            """
            Runs a phase of passes.
            require_line_drop=True: repeats only when actual lines disappear.
            require_line_drop=False: repeats on ANY text change.
            """
            nonlocal src, total_changed
            phase_made_progress = False

            if verbose: print(f"\n  === {phase_name} ===")
            dirty_passes = set(pass_names)

            for i in range(max_iters):
                iter_progress = False
                for p_name in pass_names:

                    if p_name not in dirty_passes:
                        if verbose: print(f"    [{p_name}] skipped (not dirty)")
                        continue

                    # Use count('\n') instead of splitlines()
                    lines_before = src.count('\n') + (1 if src and not src.endswith('\n') else 0)
                    new_src, changed = pass_dict[p_name](src, baseline, tmp_dir, header_dir, p_name)

                    if changed:
                        lines_after = new_src.count('\n') + (1 if new_src and not new_src.endswith('\n') else 0)
                        src = new_src
                        total_changed = True

                        if require_line_drop:
                            if lines_after < lines_before:
                                iter_progress = True
                                phase_made_progress = True
                        else:
                            iter_progress = True
                            phase_made_progress = True

                        if verbose:
                            print(f"    [{p_name}] {lines_before} -> {lines_after} (-{lines_before - lines_after})")

                        if p_name in PASS_DEPENDENCIES:
                            for dependent_pass in PASS_DEPENDENCIES[p_name]:
                                if dependent_pass in pass_names:
                                    dirty_passes.add(dependent_pass)

                    elif verbose:
                        print(f"    [{p_name}] no change")
                        dirty_passes.discard(p_name)

                if not dirty_passes:
                    if verbose: print(f"  {phase_name} finished early (no dirty passes remaining).")
                    break

                if not iter_progress:
                    if verbose: print(f"  {phase_name} finished after {i+1} iterations (no defined progress).")
                    break
            return phase_made_progress

        # =================================================================
        #  FAST-TRACK REDUCTION
        # =================================================================

        # Phase 0: One-Shots
        run_phase("Phase 0 (One-Shots)", ["includes", "blank"], max_iters=1)

        # Phase 1: Enabler (Strikt auf Zeilen)
        run_phase("Phase 1 (Enabler)", [
            "regex_atoms", "balanced_parens_only", "balanced_parens_zero", 
            "ternary_modus_b", "ternary_modus_c", "blank"
        ], max_iters=2, require_line_drop=True)

        # Phase 2: structure (strict line-level)
        run_phase("Phase 2 (Struktur)", [
            "lines", 
            "balanced_curly_empty", 
            "balanced_remove_curly", 
            "balanced_remove_parens", 
            "balanced_remove_square", 
            "peep_while"
        ], max_iters=4, require_line_drop=True)

        # Phase 3a: micro-expressions (iterative, until nothing changes)
        run_phase("Phase 3a (Sub-Expressions)", [
            "peep_subexpr"
        ], max_iters=10, require_line_drop=False)

        run_phase("Phase 3b (Arguments)", ["peep_args"], max_iters=1, require_line_drop=False)

        # Phase 4: cleanup
        run_phase("Phase 4 (Cleanup)", [
            "balanced_parens_only", 
            "balanced_parens_zero",
            "balanced_remove_curly",
            "blank_final"
        ], max_iters=1)

        # --- END OF REDUCTION LOOP ---

        # Use count('\n') instead of splitlines()
        result["reduced_lines"] = src.count('\n') + (1 if src and not src.endswith('\n') else 0)

        # Append statistics to result
        result["compiler_stats"] = cache.stats

        if total_changed and (result["reduced_lines"] < result["original_lines"] or len(src) < len(original_src)):
            result["status"] = "reduced"
            with open(output_path, "w", encoding="utf-8") as f: f.write(src)
        else:
            result["status"] = "clean"
            shutil.copy2(c_filepath, output_path)
        return result

    except Exception as e:
        result["status"] = "error"
        result["error"] = str(e)
        # Append statistics if cache exists
        if 'cache' in locals():
            result["compiler_stats"] = cache.stats
        return result
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# =====================================================================
#  WORKER & MAIN
# =====================================================================

def _worker_init():
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    try: resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except: pass

def _worker_fn(args: tuple):
    c_filepath, output_path, dry_run = args
    check_disk_space(min_free_gb=2)
    return reduce_file(c_filepath, _get_header_dir(c_filepath), output_path, dry_run)

def main():
    ap = argparse.ArgumentParser(description="Stage 3b: Token-level reducer (Python-only) — optimised with TCC guard")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--group", type=str, default=None)
    ap.add_argument("-j", "--workers", type=int, default=None)
    ap.add_argument("--diagnose", type=str, default=None)
    ap.add_argument("--input-dir", type=str, default=INPUT_DIR)
    ap.add_argument("--output-dir", type=str, default=OUTPUT_DIR)
    ap.add_argument("--overwrite", action="store_true")
    args = ap.parse_args()

    input_dir, output_dir = args.input_dir, args.output_dir
    num_workers = args.workers or max(1, multiprocessing.cpu_count() // 2)

    if args.diagnose:
        c = args.diagnose
        hd = _get_header_dir(c)
        os.makedirs(output_dir, exist_ok=True)
        g = next((g for g in GROUPS if g in c), "")
        if g:
            od = os.path.join(output_dir, g); os.makedirs(od, exist_ok=True)
            op = os.path.join(od, os.path.basename(c))
        else: op = os.path.join(output_dir, os.path.basename(c))

        print(f"=== TOKEN-REDUCE: {c} ===\n")
        res = reduce_file(c, hd, op, verbose=True)
        print(f"\nStatus: {res['status']}")
        if res.get('error'): print(f"Error: {res['error']}")
        sv = res['original_lines'] - res['reduced_lines']
        print(f"Original:  {res['original_lines']} lines")
        print(f"Reduced:   {res['reduced_lines']} lines")
        print(f"TCC Guard: {'active' if res.get('tcc_enabled') else 'inactive'}")
        if res['original_lines'] > 0:
            print(f"Saved:     {sv} lines ({sv/res['original_lines']*100:.1f}%)")

        # Compiler diagnostics output
        if "compiler_stats" in res:
            cs = res["compiler_stats"]
            print("\n--- Compiler diagnostics ---")
            print(f"  Cache Hits: {cs['cache_hits']}")
            print(f"  TCC Checks: {cs['tcc_checks']} ({cs['tcc_rejects']} rejected → IDO calls saved)")
            print(f"  IDO Checks: {cs['ido_checks']} ({cs['ido_success']} hash OK, {cs['ido_rejects']} rejected)")

        if res['status'] == 'reduced':
            print("\n--- Reduced code ---")
            with open(op) as f: print(f.read())
        return

    groups = GROUPS if not args.group else [args.group]
    if args.group and args.group not in GROUPS:
        print(f"Error: group '{args.group}' not found."); sys.exit(1)
    os.makedirs(output_dir, exist_ok=True)

    total_count, skipped = 0, 0
    for group in groups:
        gi = os.path.join(input_dir, group)
        go = os.path.join(output_dir, group)
        if not os.path.isdir(gi): continue
        os.makedirs(go, exist_ok=True)
        for e in os.scandir(gi):
            if e.name.endswith(".c") and e.is_file():
                if not args.overwrite and os.path.exists(os.path.join(go, e.name)):
                    skipped += 1
                else: total_count += 1

    print(f"Found: {total_count+skipped} | Skipped: {skipped} | To process: {total_count}")
    print(f"TCC Guard: {'available' if _tcc_available() else 'NOT available'}")
    print(f"Input:  {input_dir}\nOutput: {output_dir}")
    if total_count == 0: print("Nothing to do."); return

    def _gen():
        for group in groups:
            gi = os.path.join(input_dir, group)
            go = os.path.join(output_dir, group)
            if not os.path.isdir(gi): continue
            for e in os.scandir(gi):
                if e.name.endswith(".c") and e.is_file():
                    op = os.path.join(go, e.name)
                    if not args.overwrite and os.path.exists(op): continue
                    yield (e.path, op, args.dry_run)

    # Startup cleanup
    try:
        now = time.time()
        for d in os.scandir(TMP_ROOT):
            if d.name.startswith("tr_") and d.is_dir():
                try:
                    if now - d.stat().st_mtime > 300:
                        shutil.rmtree(d.path, ignore_errors=True)
                except: pass
    except: pass

    stats = {"clean": 0, "reduced": 0, "error": 0}
    total_saved = 0
    lp = os.path.join(output_dir, "token_reduce_log.jsonl")
    ep = os.path.join(output_dir, "token_reduce_errors.jsonl")

    with open(lp, "a") as lf, open(ep, "a") as ef:
        with multiprocessing.Pool(num_workers, initializer=_worker_init) as pool:
            try:
                iterator = pool.imap_unordered(_worker_fn, _gen(), chunksize=1)
                for n, res in enumerate(tqdm(iterator, total=total_count, desc="Token-Reduce"), 1):
                    stats[res["status"]] = stats.get(res["status"], 0) + 1
                    sv = res["original_lines"] - res["reduced_lines"]
                    if sv > 0 and res["status"] == "reduced":
                        total_saved += sv
                        lf.write(json.dumps(res, ensure_ascii=False) + "\n"); lf.flush()
                    if res["status"] == "error":
                        ef.write(json.dumps({"timestamp": datetime.now().isoformat(),
                            "file": res["file"], "error": res.get("error")},
                            ensure_ascii=False) + "\n"); ef.flush()

                    if n % 100 == 0:
                        try:
                            now = time.time()
                            for d in os.scandir(TMP_ROOT):
                                if d.name.startswith("tr_") and d.is_dir():
                                    try:
                                        if now - d.stat().st_mtime > 300:
                                            shutil.rmtree(d.path, ignore_errors=True)
                                    except OSError: pass
                        except OSError: pass

            except KeyboardInterrupt:
                print("\nAborted!"); pool.terminate(); pool.join(); sys.exit(1)

    print("\n" + "=" * 60)
    print(f"  Clean: {stats['clean']} | Reduced: {stats['reduced']} | Errors: {stats['error']}")
    print(f"  Lines saved: {total_saved}")
    print(f"  Output: {output_dir}")

if __name__ == "__main__":
    main()