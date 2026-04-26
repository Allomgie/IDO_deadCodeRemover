#!/usr/bin/env python3
"""
Stage 4: Clang-delta reducer — OPTIMISED VERSION with TCC fast-reject guard.

Hybrid approach: Python passes first (fast, TCC-guarded),
Clang-delta only for aggregate-to-scalar and complex cases.

Key optimisations:
  1. TCC fast-reject for syntactically safe Python passes (saves IDO calls)
  2. Global CompileCache per file (across all passes)
  3. Central syntax validator BEFORE compiler call
  4. Corrected Python passes (remove-unused-function, return-void, neutralize-calls)
  5. Gatekeeper: decides whether Clang-delta is needed at all
  6. count('\n') instead of splitlines()
  7. FIXED: pass_simplify_if now preserves block braces
  8. FIXED: header handling like Stage 3 (never attach includes to PP code)
  9. FIXED: multi-pass for Python passes
  10. FIXED: pass_simplify_comma no longer matches every bracket

Input:  dataset/TokenReduced/
Output: dataset/ClangReduced/
"""

import os, sys, re, subprocess, signal, tempfile, shutil, hashlib
import json, time, argparse, multiprocessing, resource
from datetime import datetime
from tqdm import tqdm

BASE_DIR     = "/home/lukas/code_generator/deadCodeRemover"
PROJECT_ROOT = os.path.join(BASE_DIR, "IDO_Compiler")
IDO_DIR      = os.path.abspath(os.path.join(PROJECT_ROOT, "tools", "ido"))
IDO_CC       = os.path.join(IDO_DIR, "cc")

DATASET_DIR  = os.path.join(BASE_DIR, "dataset")
INPUT_DIR    = os.path.join(DATASET_DIR, "Stage_4_IN")
OUTPUT_DIR   = os.path.join(DATASET_DIR, "Stage_4_OUT")
HEADERS_DIR    = os.path.join(DATASET_DIR, "Stage_0_headers")

OBJDUMP      = "mips-linux-gnu-objdump"
TMP_ROOT     = "/dev/shm"
CLANG_DELTA  = "/usr/local/bin/clang_delta"

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
MAX_PASSES = 3

# =====================================================================
#  TCC FAST-REJECT GUARD
# =====================================================================

TCC_SAFE_PASSES = {
    "simplify-if", "remove-unused-var", "remove-unused-func",
    "simplify-comma", "blank", "balanced_parens_only",
    "balanced_curly_empty", "balanced_parens_zero",
    "balanced_remove_curly", "balanced_remove_parens",
    "balanced_remove_square", "peep_while", "regex_atoms",
    "ternary_modus_b", "ternary_modus_c",
}

def _tcc_available() -> bool:
    """Checks whether tcc is available in PATH."""
    return shutil.which("tcc") is not None

def tcc_syntax_check(c_source: str, tmp_dir: str, header_dir: str) -> bool:
    """
    Fast syntax check using TCC.
    Returns True if TCC passes without errors.
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
    """Central cache for compilation results per file."""
    def __init__(self, tcc_baseline_ok: bool = False):
        self._success = {}
        self._rejected = set()
        self._tcc_rejected = set()
        self._tcc_baseline_ok = tcc_baseline_ok and _tcc_available()

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
        Checks TCC cache first (for safe passes), then IDO as gold standard.
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

# =====================================================================
#  SYNTAX-VALIDATOR
# =====================================================================

_SYNTAX_KILLERS = [
    re.compile(r'[,;]\s*[;,)]'),
    re.compile(r'\(\s*[,;]'),
    re.compile(r'[,;]\s*\)'),
    re.compile(r'\{\s*\}\s*\w'),
    re.compile(r'\b(if|while|for|switch)\s*[^\s(]'),
]

def is_syntactically_plausible(src: str) -> bool:
    """Fast heuristic check for obvious syntax errors."""
    counts = {'(': 0, ')': 0, '{': 0, '}': 0, '[': 0, ']': 0}
    for ch in src:
        if ch in counts:
            counts[ch] += 1
    if counts['('] != counts[')'] or counts['{'] != counts['}'] or counts['['] != counts[']']:
        return False

    in_string = False
    escaped = False
    for ch in src:
        if escaped:
            escaped = False
            continue
        if ch == '\\':
            escaped = True
            continue
        if ch == '"' and not in_string:
            in_string = True
        elif ch == '"' and in_string:
            in_string = False
    if in_string:
        return False

    for pattern in _SYNTAX_KILLERS:
        if pattern.search(src):
            return False
    return True

# =====================================================================
#  INFRASTRUCTURE
# =====================================================================

def run_cmd_safely(cmd, cwd=None, env=None, timeout=30):
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
    if not is_syntactically_plausible(c_source):
        return None, "syntax_fail"

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

        cmd = [OBJDUMP, "-d", "-z", o_path]
        try: rc, stdout_text, _ = run_cmd_safely(cmd, timeout=30)
        except subprocess.TimeoutExpired: _cleanup(); return None, "objdump timeout"
        if rc != 0: _rm(o_path); _cleanup(); return None, "objdump fail"

        cmd = [OBJDUMP, "-s", "-j", ".rodata", "-j", ".data", "-j", ".bss", o_path]
        try: rc2, stdout_data, _ = run_cmd_safely(cmd, timeout=30)
        except subprocess.TimeoutExpired: stdout_data = b""
        _rm(o_path)

        asm = []
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

        if stdout_data:
            for line in stdout_data.decode(errors="replace").splitlines():
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

def _get_header_dir(path):
    for g in GROUPS:
        if g in path: return os.path.join(HEADERS_DIR, f"{g}_headers")
    return ""

def check_disk_space(min_free_gb=2):
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

# =====================================================================
#  FAST-FAIL GUARDS
# =====================================================================

def has_if_blocks(src: str) -> bool:
    """Checks whether simplify-if is applicable at all."""
    return bool(re.search(r'\bif\s*\(', src))

def has_comma_expressions(src: str) -> bool:
    """Checks whether simplify-comma is applicable."""
    # Quick check: are there brackets containing commas?
    pairs = _find_all_balanced(src, '(', ')')
    for start, end in pairs:
        inner = src[start+1:end]
        if ',' in inner and _split_args(inner):
            return True
    return False

def has_declarations(src: str) -> bool:
    """Checks whether remove-unused-var is applicable."""
    return bool(re.search(r'\b(int|char|short|long|unsigned|signed|float|double|void|struct\s+\w+|union\s+\w+|enum\s+\w+)\s+\w', src))

def has_function_definitions(src: str) -> bool:
    """Checks whether remove-unused-function is applicable."""
    return bool(re.search(r'\b\w+\s*\([^)]*\)\s*\{', src))

def has_function_calls(src: str) -> bool:
    """Checks whether neutralize-calls is applicable."""
    calls = re.findall(r'\b([a-zA-Z_][a-zA-Z0-9_]*)\s*\(', src)
    keywords = {"if", "while", "for", "switch", "return", "sizeof"}
    return any(c not in keywords for c in calls)

def has_return_statements(src: str) -> bool:
    """Checks whether return-void is applicable."""
    return bool(re.search(r'\breturn\b', src))

def is_worth_clang(src: str) -> tuple[bool, str]:
    """
    Gatekeeper: decides whether Clang-delta is needed.
    Returns (needs_clang, reason).
    """
    # Check 1: struct/array accesses (aggregate-to-scalar)
    if re.search(r'\.|->|\[', src):
        return True, "aggregate-to-scalar"

    # Check 2: complex function calls (simplify-callexpr)
    calls = re.findall(r'\b([a-zA-Z_][a-zA-Z0-9_]*)\s*\(', src)
    keywords = {"if", "while", "for", "switch", "return", "sizeof"}
    real_calls = [c for c in calls if c not in keywords]
    if real_calls:
        return True, "simplify-callexpr"

    # Check 3: multiple function definitions (remove-unused-function)
    defs = re.findall(r'\b([a-zA-Z_][a-zA-Z0-9_]*)\s*\([^)]*\)\s*\{', src)
    if len(defs) > 1:
        return True, "remove-unused-function"

    return False, "python-sufficient"

# =====================================================================
#  PYTHON PASSES (with TCC guard / pass_name)
# =====================================================================

def pass_simplify_if(src: str, baseline: str, tmp_dir: str, header_dir: str,
                     cache: CompileCache, pass_name: str = "simplify-if") -> tuple[str, bool]:
    """Python port of simplify-if. Preserves curly braces to avoid
    destroying block semantics (especially with multiple statements)."""
    if not has_if_blocks(src):
        return src, False

    changed = False
    pattern = re.compile(r'\bif\s*\(')
    fail_cache = set()

    for _iteration in range(20):
        matches = list(pattern.finditer(src))
        made_progress = False

        for match in reversed(matches):
            open_p = match.end() - 1
            close_p = _find_matching(src, open_p, '(', ')')
            if close_p == -1:
                continue

            cond = src[open_p+1:close_p]
            after_if = src[close_p+1:].lstrip()
            if not after_if.startswith('{'):
                continue

            open_b = src.find('{', close_p)
            close_b = _find_matching(src, open_b, '{', '}')
            if close_b == -1:
                continue

            # Cache key for this if-block
            ctx_key = hashlib.md5(f"if|{cond}|{src[open_b:close_b+1]}".encode()).hexdigest()
            if ctx_key in fail_cache:
                continue

            after_true = src[close_b+1:].lstrip()

            if after_true.startswith("else"):
                else_start = src.find("else", close_b+1)
                after_else = src[else_start+4:].lstrip()

                if after_else.startswith('{'):
                    open_else_b = src.find('{', else_start)
                    close_else_b = _find_matching(src, open_else_b, '{', '}')
                    if close_else_b == -1:
                        continue

                    # Candidate 1: keep ONLY the true branch (WITH braces)
                    cand_true = src[:match.start()] + src[open_b:close_b+1] + src[close_else_b+1:]
                    h, _ = cache.try_candidate(cand_true, tmp_dir, header_dir, baseline, pass_name)
                    if h == baseline:
                        src = cand_true
                        changed = True
                        made_progress = True
                        break

                    # Candidate 2: keep ONLY the false branch (WITH braces)
                    cand_false = src[:match.start()] + src[open_else_b:close_else_b+1] + src[close_else_b+1:]
                    h, _ = cache.try_candidate(cand_false, tmp_dir, header_dir, baseline, pass_name)
                    if h == baseline:
                        src = cand_false
                        changed = True
                        made_progress = True
                        break
            else:
                # Aggressive: if(cond){body} -> {body}
                cand_agg = src[:match.start()] + src[open_b:close_b+1] + src[close_b+1:]
                h, _ = cache.try_candidate(cand_agg, tmp_dir, header_dir, baseline, pass_name)
                if h == baseline:
                    src = cand_agg
                    changed = True
                    made_progress = True
                    break

                # Conservative: if(cond){body} -> cond; {body}
                cand_clang = src[:match.start()] + cond + ";" + src[open_b:close_b+1] + src[close_b+1:]
                h, _ = cache.try_candidate(cand_clang, tmp_dir, header_dir, baseline, pass_name)
                if h == baseline:
                    src = cand_clang
                    changed = True
                    made_progress = True
                    break

            if not made_progress:
                fail_cache.add(ctx_key)

        if not made_progress:
            break

    return src, changed


def pass_remove_unused_var(src: str, baseline: str, tmp_dir: str, header_dir: str,
                           cache: CompileCache, pass_name: str = "remove-unused-var") -> tuple[str, bool]:
    """Python port of remove-unused-var. Focus on comma declarations."""
    if not has_declarations(src):
        return src, False

    changed = False
    type_pattern = r'\b(int|char|short|long|unsigned|signed|float|double|void|struct\s+[a-zA-Z0-9_]+|union\s+[a-zA-Z0-9_]+|enum\s+[a-zA-Z0-9_]+)\s+'
    decl_pattern = re.compile(type_pattern + r'([^;]+);')
    fail_cache = set()

    for _iteration in range(20):
        matches = list(decl_pattern.finditer(src))
        made_progress = False

        for match in reversed(matches):
            type_part = match.group(1).strip()
            vars_part = match.group(2).strip()

            # Fast-fail: skip function prototypes
            if '(' in vars_part and '=' not in vars_part:
                continue

            var_list = _split_args(vars_part)
            if len(var_list) <= 1:
                # Single variable: try to remove completely
                candidate = src[:match.start()] + src[match.end():]
                ctx_key = hashlib.md5(f"unused_var|{match.group(0)}".encode()).hexdigest()
                if ctx_key in fail_cache:
                    continue
                h, _ = cache.try_candidate(candidate, tmp_dir, header_dir, baseline, pass_name)
                if h == baseline:
                    src = candidate
                    changed = True
                    made_progress = True
                    break
                else:
                    fail_cache.add(ctx_key)
                continue

            # Multi-variable: remove individual elements
            for i in range(len(var_list)):
                new_vars = var_list[:i] + var_list[i+1:]
                new_decl = type_part + " " + ", ".join(new_vars) + ";"
                candidate = src[:match.start()] + new_decl + src[match.end():]

                ctx_key = hashlib.md5(f"unused_var|{i}|{match.group(0)}".encode()).hexdigest()
                if ctx_key in fail_cache:
                    continue

                h, _ = cache.try_candidate(candidate, tmp_dir, header_dir, baseline, pass_name)
                if h == baseline:
                    src = candidate
                    changed = True
                    made_progress = True
                    break
                else:
                    fail_cache.add(ctx_key)

            if made_progress:
                break

        if not made_progress:
            break

    return src, changed


def pass_remove_unused_function(src: str, baseline: str, tmp_dir: str, header_dir: str,
                                cache: CompileCache, pass_name: str = "remove-unused-func") -> tuple[str, bool]:
    """Corrected version: matches ALL definitions, not just static/inline."""
    if not has_function_definitions(src):
        return src, False

    changed = False
    func_pattern = re.compile(r'\b([a-zA-Z_][a-zA-Z0-9_]*)\s*\(')
    fail_cache = set()

    for _iteration in range(20):
        defined = {}
        for match in func_pattern.finditer(src):
            name = match.group(1)
            if name in {"if", "while", "for", "switch", "sizeof", "main"}:
                continue
            open_p = match.end() - 1
            close_p = _find_matching(src, open_p, '(', ')')
            if close_p == -1:
                continue
            after_args = src[close_p+1:].lstrip()
            if after_args.startswith('{'):
                open_b = src.find('{', close_p)
                close_b = _find_matching(src, open_b, '{', '}')
                if close_b != -1:
                    defined[name] = (match.start(), close_b)

        if len(defined) <= 1:
            break

        made_progress = False

        for func_name in sorted(defined.keys(), key=lambda n: defined[n][0], reverse=True):
            start_pos, end_pos = defined[func_name]

            call_pattern = re.compile(rf'\b{re.escape(func_name)}\s*\(')
            used_elsewhere = False
            for call_match in call_pattern.finditer(src):
                if not (start_pos <= call_match.start() <= end_pos):
                    used_elsewhere = True
                    break

            if used_elsewhere:
                continue

            type_start = start_pos
            while type_start > 0 and src[type_start-1] not in ';{}':
                type_start -= 1

            ctx_key = hashlib.md5(f"unused_func|{func_name}|{type_start}".encode()).hexdigest()
            if ctx_key in fail_cache:
                continue

            candidate = src[:type_start] + src[end_pos+1:]
            h, _ = cache.try_candidate(candidate, tmp_dir, header_dir, baseline, pass_name)
            if h == baseline:
                src = candidate
                changed = True
                made_progress = True
                break
            else:
                fail_cache.add(ctx_key)

        if not made_progress:
            break

    return src, changed


def pass_simplify_comma(src: str, baseline: str, tmp_dir: str, header_dir: str,
                        cache: CompileCache, pass_name: str = "simplify-comma") -> tuple[str, bool]:
    """Python port of SimplifyCommaExpr. Only matches comma expressions."""
    if not has_comma_expressions(src):
        return src, False

    changed = False
    fail_cache = set()

    # Match brackets that contain commas but are NOT control structures
    # Negative lookbehind: no if/for/while/switch/return directly before
    pattern = re.compile(r'(?<![a-zA-Z_0-9])\(')

    for _iteration in range(10):
        matches = list(pattern.finditer(src))
        made_progress = False

        for match in reversed(matches):
            start_p = match.start()
            end_p = _find_matching(src, start_p, '(', ')')
            if end_p == -1:
                continue

            inner = src[start_p + 1:end_p]
            parts = _split_args(inner)

            if len(parts) < 2:
                continue

            new_inner = ", ".join(parts[1:])
            candidate = src[:start_p + 1] + new_inner + src[end_p:]

            ctx_key = hashlib.md5(f"comma|{inner[:50]}".encode()).hexdigest()
            if ctx_key in fail_cache:
                continue

            h, _ = cache.try_candidate(candidate, tmp_dir, header_dir, baseline, pass_name)
            if h == baseline:
                src = candidate
                changed = True
                made_progress = True
                break
            else:
                fail_cache.add(ctx_key)

        if not made_progress:
            break

    return src, changed


def pass_neutralize_calls(src: str, baseline: str, tmp_dir: str, header_dir: str,
                          cache: CompileCache, pass_name: str = "neutralize-calls") -> tuple[str, bool]:
    """Corrected version with more robust context check and macro blacklist."""
    if not has_function_calls(src):
        return src, False

    changed = False
    pattern = re.compile(r'\b([a-zA-Z_][a-zA-Z0-9_]*)\s*\(')
    fail_cache = set()

    # YARPGen / CSmith macros that are not real function calls
    macro_blacklist = {"min", "max", "safe_add", "safe_sub", "safe_mul", "safe_div",
                       "safe_mod", "safe_lshift", "safe_rshift", "safe_unary_minus",
                       "safe_mod_func_uint32_t_u_u"}

    for _iteration in range(20):
        matches = list(pattern.finditer(src))
        made_progress = False

        for match in reversed(matches):
            func_name = match.group(1)
            if func_name in {"main", "if", "while", "for", "switch", "sizeof"}:
                continue
            if func_name in macro_blacklist:
                continue

            open_p = match.end() - 1
            close_p = _find_matching(src, open_p, '(', ')')
            if close_p == -1:
                continue

            before = src[match.start()-1] if match.start() > 0 else ''
            after = src[close_p+1] if close_p+1 < len(src) else ''

            replacements = []

            if before in ';{}' and after == ';':
                replacements.extend([";", "(void)0;"])
            if before == '=':
                replacements.extend(["0", "(void)0"])
            if before == '(':
                replacements.extend(["0", "1"])
            if before == ',':
                replacements.extend(["0"])

            if not replacements:
                continue

            for repl in replacements:
                candidate = src[:match.start()] + repl + src[close_p+1:]

                ctx_key = hashlib.md5(f"call|{func_name}|{repl}|{before}|{after}".encode()).hexdigest()
                if ctx_key in fail_cache:
                    continue

                h, _ = cache.try_candidate(candidate, tmp_dir, header_dir, baseline, pass_name)
                if h == baseline:
                    src = candidate
                    changed = True
                    made_progress = True
                    break
                else:
                    fail_cache.add(ctx_key)

            if made_progress:
                break

        if not made_progress:
            break

    return src, changed


def pass_return_void(src: str, baseline: str, tmp_dir: str, header_dir: str,
                     cache: CompileCache, pass_name: str = "return-void") -> tuple[str, bool]:
    """Corrected version with side-effect check."""
    if not has_return_statements(src):
        return src, False

    changed = False
    func_pattern = re.compile(r'\b([a-zA-Z_][a-zA-Z0-9_]*)\s*\(')
    fail_cache = set()

    matches = list(func_pattern.finditer(src))
    for match in reversed(matches):
        func_name = match.group(1)
        if func_name in {"if", "while", "for", "switch", "main"}:
            continue

        open_p = match.end() - 1
        close_p = _find_matching(src, open_p, '(', ')')
        if close_p == -1:
            continue

        after_args = src[close_p+1:].lstrip()
        if not after_args.startswith('{'):
            continue

        open_b = src.find('{', close_p)
        close_b = _find_matching(src, open_b, '{', '}')
        if close_b == -1:
            continue

        type_start = match.start()
        while type_start > 0 and src[type_start-1] not in ';{}':
            type_start -= 1

        type_prefix = src[type_start:match.start()].strip()
        if "void" in type_prefix:
            continue

        body = src[open_b+1:close_b]

        ret_pattern = re.compile(r'\breturn\b\s*([^;]*);')
        returns = list(ret_pattern.finditer(body))
        if not returns:
            continue

        all_safe = True
        for ret_match in returns:
            expr = ret_match.group(1).strip()
            if re.search(r'\b\w+\s*\(', expr):
                all_safe = False
                break

        ctx_key = hashlib.md5(f"ret_void|{func_name}|{type_prefix}".encode()).hexdigest()
        if ctx_key in fail_cache:
            continue

        if all_safe:
            new_body = ret_pattern.sub(r';', body)
        else:
            new_body = ret_pattern.sub(r'\1;', body)

        new_header = "void " + src[match.start():open_b+1]
        candidate = src[:type_start] + new_header + new_body + src[close_b:]

        h, _ = cache.try_candidate(candidate, tmp_dir, header_dir, baseline, pass_name)
        if h == baseline:
            src = candidate
            changed = True
        else:
            fail_cache.add(ctx_key)

    return src, changed


# =====================================================================
#  CLANG-DELTA PIPELINE (only for aggregate-to-scalar)
# =====================================================================

def preprocess_file(c_filepath, header_dir, tmp_dir):
    """Runs gcc -E on the C file."""
    with open(c_filepath, "r") as f:
        fixed_src = re.sub(r'#include\s+"[^"]*?([^/"]+\.h)"', r'#include "\1"', f.read())
    c_path = os.path.join(tmp_dir, "src.c")
    pp_path = os.path.join(tmp_dir, "pp.c")

    with open(c_path, "w") as f:
        f.write(fixed_src)

    cmd = ["gcc", "-E", "-P", "-xc", "-D_LANGUAGE_C", "-D_MIPS_SZLONG=32"]
    for inc in INCLUDE_DIRS:
        cmd += ["-I", inc]
    if header_dir:
        cmd += ["-I", header_dir]
    cmd += [c_path, "-o", pp_path]

    try:
        rc, _, _ = run_cmd_safely(cmd, timeout=30)
    except subprocess.TimeoutExpired:
        return None
    try:
        os.unlink(c_path)
    except:
        pass
    if rc != 0:
        return None
    return pp_path


def run_clang_transform(pp_path, transformation, counter, tmp_dir, header_dir=""):
    """Runs a clang_delta transformation."""
    out_path = os.path.join(tmp_dir, "cd_out.c")
    try:
        os.unlink(out_path)
    except OSError:
        pass

    # Append compiler flags after '--' so clang_delta can find headers
    cmd = [CLANG_DELTA,
           f"--transformation={transformation}",
           f"--counter={counter}",
           f"--output={out_path}",
           pp_path,
           "--",
           "-D_LANGUAGE_C", "-D_MIPS_SZLONG=32"]
    for inc in INCLUDE_DIRS:
        cmd.extend(["-I", inc])
    if header_dir:
        cmd.extend(["-I", header_dir])

    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
    except subprocess.TimeoutExpired:
        return None

    if res.returncode == 0 and os.path.exists(out_path):
        return out_path
    return None


def apply_clang_passes(pp_path, baseline_hash, tmp_dir, header_dir, local_cache, verbose=False):
    """Applies only aggregate-to-scalar."""
    changed_total = False
    current_size = os.path.getsize(pp_path)

    transforms = ["aggregate-to-scalar"]

    cycle_iterations = 0
    max_cycles = 5
    cycle_changed = True

    while cycle_changed and cycle_iterations < max_cycles:
        cycle_iterations += 1
        cycle_changed = False
        if verbose:
            print(f"\n  === Clang Cycle {cycle_iterations} ===")

        for transform in transforms:
            counter = 1
            transform_changed = False

            while True:
                out = run_clang_transform(pp_path, transform, counter, tmp_dir, header_dir)
                if out is None:
                    break

                out_size = os.path.getsize(out)
                if out_size == current_size:
                    with open(pp_path, "rb") as f1, open(out, "rb") as f2:
                        if f1.read() == f2.read():
                            try:
                                os.unlink(out)
                            except:
                                pass
                            counter += 1
                            continue

                h = compute_asm_hash_from_pp(out, tmp_dir, local_cache)

                if h == baseline_hash:
                    shutil.move(out, pp_path)
                    current_size = out_size
                    transform_changed = True
                    cycle_changed = True
                    changed_total = True
                else:
                    try:
                        os.unlink(out)
                    except:
                        pass
                    counter += 1

            if verbose:
                if transform_changed:
                    lines = open(pp_path).read().count('\n') + 1
                    print(f"    [{transform}] reduced -> {lines} lines")
                else:
                    print(f"    [{transform}] no change")

    if verbose:
        print(f"\n  Clang pipeline stabilised after {cycle_iterations} cycles.")
    return changed_total


def compute_asm_hash_from_pp(pp_path, tmp_dir, local_cache=None):
    """Compiles preprocessed code and validates."""
    with open(pp_path, "rb") as f:
        c_bytes = f.read()
    text_hash = hashlib.md5(c_bytes).hexdigest()

    if local_cache is not None and text_hash in local_cache:
        return local_cache[text_hash]

    o_path = os.path.join(tmp_dir, "clang_test.o")

    def _rm(p):
        try:
            os.unlink(p)
        except OSError:
            pass

    try:
        cmd = [IDO_CC, "-c", "-O2", "-mips2", "-G", "0", "-w", pp_path, "-o", o_path]
        try:
            rc, _, _ = run_cmd_safely(cmd, cwd=tmp_dir, env=_IDO_ENV, timeout=30)
        except subprocess.TimeoutExpired:
            _rm(o_path)
            return None
        if rc != 0:
            _rm(o_path)
            return None

        cmd_text = [OBJDUMP, "-d", "-z", o_path]
        try:
            rc, stdout_text, _ = run_cmd_safely(cmd_text, timeout=30)
        except subprocess.TimeoutExpired:
            _rm(o_path)
            return None
        if rc != 0:
            _rm(o_path)
            return None

        cmd_data = [OBJDUMP, "-s", "-j", ".rodata", "-j", ".data", "-j", ".bss", o_path]
        try:
            rc2, stdout_data, _ = run_cmd_safely(cmd_data, timeout=30)
        except subprocess.TimeoutExpired:
            stdout_data = b""

        _rm(o_path)

        asm_payload = []

        for line in stdout_text.decode(errors="replace").splitlines():
            m = re.match(r'^\s*[0-9a-fA-F]+:\s+[0-9a-fA-F]+\s+(.*)', line)
            if not m:
                continue
            s = m.group(1).strip().split('#')[0].strip()
            if not s:
                continue
            s = re.sub(r'addiu\s+\$?sp,\s*\$?sp,\s*-?\d+', 'addiu sp,sp,OFFSET', s)
            s = re.sub(r'-?\d+\(\$?sp\)', 'OFFSET(sp)', s)
            s = re.sub(r'-?\d+\(\$?fp\)', 'OFFSET(fp)', s)
            s = re.sub(r'%[a-z0-9_.]+\([^)]*\)', 'SYMBOL', s)
            asm_payload.append(s)

        if rc2 == 0 and stdout_data:
            for line in stdout_data.decode(errors="replace").splitlines():
                m = re.match(r'^\s*[0-9a-fA-F]+\s+((?:[0-9a-fA-F]+\s*)+)', line)
                if m:
                    asm_payload.append("DATA:" + m.group(1).strip())

        if not asm_payload:
            if local_cache is not None:
                local_cache[text_hash] = None
            return None

        h = hashlib.md5("\n".join(asm_payload).encode()).hexdigest()
        if local_cache is not None:
            local_cache[text_hash] = h
        return h

    except Exception:
        _rm(o_path)
        return None


def compute_asm_hash_from_source(c_source, tmp_dir, header_dir):
    """Compiles C source with includes."""
    fixed = re.sub(r'#include\s+"[^"]*?([^/"]+\.h)"', r'#include "\1"', c_source)
    cache_key = hashlib.md5((fixed + (header_dir or "")).encode()).hexdigest()
    if cache_key in _asm_cache:
        return _asm_cache[cache_key], ""

    c_path = os.path.join(tmp_dir, "_src.c")
    i_path = os.path.join(tmp_dir, "_src.i")

    def _cleanup():
        for p in [c_path, i_path]:
            try:
                os.unlink(p)
            except OSError:
                pass

    try:
        with open(c_path, "w") as f:
            f.write(fixed)
        cmd = ["gcc", "-E", "-P", "-xc", "-D_LANGUAGE_C", "-D_MIPS_SZLONG=32"]
        for inc in INCLUDE_DIRS:
            cmd += ["-I", inc]
        if header_dir:
            cmd += ["-I", header_dir]
        cmd += [c_path, "-o", i_path]
        try:
            rc, _, _ = run_cmd_safely(cmd, timeout=30)
        except subprocess.TimeoutExpired:
            _cleanup()
            return None, "gcc timeout"
        try:
            os.unlink(c_path)
        except:
            pass
        if rc != 0:
            _cleanup()
            return None, "gcc fail"

        h = compute_asm_hash_from_pp(i_path, tmp_dir)
        try:
            os.unlink(i_path)
        except:
            pass
        if h:
            _asm_cache[cache_key] = h
        return h, ("" if h else "compile fail")
    except Exception as e:
        _cleanup()
        return None, str(e)




def pass_min_max_identity(src: str, baseline: str, tmp_dir: str, header_dir: str,
                          cache: CompileCache, pass_name: str = "min_max_identity") -> tuple[str, bool]:
    """
    min(x, x) -> x, max(x, x) -> x.
    Operates at text level BEFORE macro expansion.
    """
    changed = False
    pattern = re.compile(r'\b(min|max)\s*\(')
    fail_cache = set()

    while True:
        matches = list(pattern.finditer(src))
        made_progress = False

        for match in reversed(matches):
            open_p = match.end() - 1
            close_p = _find_matching(src, open_p, '(', ')')
            if close_p == -1:
                continue

            args_str = src[open_p+1:close_p]
            args = _split_args(args_str)
            if len(args) != 2:
                continue

            # Normalise whitespace for comparison
            arg1_norm = re.sub(r'\s+', '', args[0])
            arg2_norm = re.sub(r'\s+', '', args[1])

            if arg1_norm != arg2_norm:
                continue

            ctx_key = hashlib.md5(f"minmax_id|{match.start()}|{arg1_norm}".encode()).hexdigest()
            if ctx_key in fail_cache:
                continue

            # min(x, x) -> x  (keep the first argument)
            candidate = src[:match.start()] + args[0] + src[close_p+1:]

            h, _ = cache.try_candidate(candidate, tmp_dir, header_dir, baseline, pass_name)
            if h == baseline:
                src = candidate
                changed = True
                made_progress = True
                break
            else:
                fail_cache.add(ctx_key)

        if not made_progress:
            break

    return src, changed


def pass_ternary_identical(src: str, baseline: str, tmp_dir: str, header_dir: str,
                           cache: CompileCache, pass_name: str = "ternary_identical") -> tuple[str, bool]:
    """
    cond ? a : a -> a  (simple cases only, no nested ternaries).
    """
    changed = False
    fail_cache = set()

    # Match cond ? expr1 : expr2 where expr1/expr2 contain no ?
    pattern = re.compile(r'([^?]+)\?([^?:]+):([^?;\)]+)')

    while True:
        matches = list(pattern.finditer(src))
        made_progress = False

        for match in reversed(matches):
            true_branch = match.group(2).strip()
            false_branch = match.group(3).strip()

            if re.sub(r'\s+', '', true_branch) != re.sub(r'\s+', '', false_branch):
                continue

            ctx_key = hashlib.md5(f"tern_id|{match.start()}|{true_branch}".encode()).hexdigest()
            if ctx_key in fail_cache:
                continue

            candidate = src[:match.start()] + true_branch + src[match.end():]

            h, _ = cache.try_candidate(candidate, tmp_dir, header_dir, baseline, pass_name)
            if h == baseline:
                src = candidate
                changed = True
                made_progress = True
                break
            else:
                fail_cache.add(ctx_key)

        if not made_progress:
            break

    return src, changed


def pass_dead_store(src: str, baseline: str, tmp_dir: str, header_dir: str,
                    cache: CompileCache, pass_name: str = "dead_store") -> tuple[str, bool]:
    """
    Removes assignments 'var = expr;' when:
      - var does not appear in expr (no self-reference)
      - var is never read after the assignment
      - the line is not a declaration (e.g. int x = 5;)

    This is the killer pass for YARPGen code with unused global assignments.
    """
    changed = False
    type_keywords = re.compile(
        r'^(\s*)(int|char|short|long|unsigned|signed|float|double|void|'
        r'struct|union|enum|static|extern|const|volatile|register|inline|'
        r'typedef|auto|restrict)\b'
    )

    # Pattern: identifier = expr;  (no ==, +=, etc.)
    assign_pattern = re.compile(r'(?<![a-zA-Z0-9_])([a-zA-Z_][a-zA-Z0-9_]*)\s*=[^=;]+;')

    while True:
        matches = list(assign_pattern.finditer(src))
        made_progress = False

        for match in reversed(matches):
            var_name = match.group(1)
            full_match = match.group(0)

            # 1. Check whether the line is a declaration
            line_start = src.rfind('\n', 0, match.start()) + 1
            line = src[line_start:match.end()]
            if type_keywords.match(line):
                continue

            # 2. Check whether var appears in the RHS (self-reference)
            rhs_start = match.start() + match.group(0).index('=') + 1
            rhs = src[rhs_start:match.end() - 1]  # without trailing ';'
            if re.search(rf'\b{re.escape(var_name)}\b', rhs):
                continue

            # 3. Check whether var is still read after the assignment
            rest = src[match.end():]
            if re.search(rf'\b{re.escape(var_name)}\b', rest):
                continue

            # Candidate: remove entire line
            candidate = src[:line_start] + src[match.end():]

            h, _ = cache.try_candidate(candidate, tmp_dir, header_dir, baseline, pass_name)
            if h == baseline:
                src = candidate
                changed = True
                made_progress = True
                break

        if not made_progress:
            break

    return src, changed

# =====================================================================
#  MAIN FUNCTION PER FILE
# =====================================================================

def reduce_file(c_filepath, header_dir, output_path, dry_run=False, verbose=False):
    """
    Reduces a single C file through Python passes (TCC-guarded)
    and optionally Clang-delta for aggregate-to-scalar.
    """
    global _asm_cache
    if len(_asm_cache) > 10000:
        _asm_cache.clear()

    result = {"file": c_filepath, "status": "clean",
              "original_lines": 0, "reduced_lines": 0, "error": None,
              "tcc_enabled": False, "tcc_baseline_ok": False}

    with open(c_filepath, "r", encoding="utf-8", errors="replace") as f:
        original_src = f.read()

    original_src = re.sub(r'#include\s+"[^"]*?([^/"]+\.h)"', r'#include "\1"', original_src)
    result["original_lines"] = original_src.count('\n') + (1 if original_src and not original_src.endswith('\n') else 0)

    tmp_dir = tempfile.mkdtemp(dir=TMP_ROOT, prefix="cr_")
    try:
        baseline, err = compute_asm_hash_from_source(original_src, tmp_dir, header_dir)
        if baseline is None:
            result["status"] = "error"
            result["error"] = f"Baseline does not compile: {err}"
            return result
        if dry_run:
            result["status"] = "would_reduce"
            return result

        tcc_baseline_ok = False
        if _tcc_available():
            tcc_baseline_ok = tcc_syntax_check(original_src, tmp_dir, header_dir)
            result["tcc_baseline_ok"] = tcc_baseline_ok
            if verbose:
                status = "OK" if tcc_baseline_ok else "FAIL (TCC disabled)"
                print(f"  TCC Baseline Check: {status}")

        cache = CompileCache(tcc_baseline_ok=tcc_baseline_ok)
        result["tcc_enabled"] = tcc_baseline_ok

        if verbose:
            print(f"  Baseline: {result['original_lines']} lines")

        src = original_src
        total_changed = False

        # === PYTHON PASSES (fast, TCC-guarded) ===
        # FIXED: multi-pass instead of single pass
        passes = [
            ("min_max_identity", pass_min_max_identity),
            ("ternary_identical", pass_ternary_identical),
            ("simplify-if", pass_simplify_if),
            ("remove-unused-var", pass_remove_unused_var),
            ("remove-unused-func", pass_remove_unused_function),
            ("simplify-comma", pass_simplify_comma),
            ("neutralize-calls", pass_neutralize_calls),
            ("return-void", pass_return_void),
            ("dead_store", pass_dead_store),
        ]

        max_python_passes = 5
        for pass_round in range(max_python_passes):
            round_changed = False
            if verbose:
                print(f"\n  === Python Passes Round {pass_round + 1} ===")

            for pass_name, pass_func in passes:
                new_src, changed = pass_func(src, baseline, tmp_dir, header_dir, cache, pass_name)
                if changed:
                    src = new_src
                    total_changed = True
                    round_changed = True
                    if verbose:
                        lines = src.count('\n') + (1 if src and not src.endswith('\n') else 0)
                        print(f"    [{pass_name}] -> {lines} lines")
                elif verbose:
                    print(f"    [{pass_name}] no change")

            if not round_changed:
                if verbose:
                    print(f"  Python passes stable after {pass_round + 1} rounds.")
                break

        needs_clang, reason = is_worth_clang(src)

        if not needs_clang:
            if verbose:
                print(f"\n  Gatekeeper: Clang not needed ({reason})")
        else:
            if verbose:
                print(f"\n  Gatekeeper: Clang needed ({reason})")

            # FIXED: clang_delta works directly on original source with include paths
            src_copy = os.path.join(tmp_dir, "src_for_clang.c")
            with open(src_copy, "w") as f:
                f.write(src)

            pp_path = src_copy
            local_cache = {}
            changed = apply_clang_passes(pp_path, baseline, tmp_dir, header_dir, local_cache, verbose)

            if changed:
                total_changed = True
                with open(pp_path) as f:
                    src = f.read()
                if verbose:
                    print("  Clang on original source succeeded")
            else:
                if verbose:
                    print("  Clang on original source failed, keeping original.")

        result["reduced_lines"] = src.count('\n') + (1 if src and not src.endswith('\n') else 0)
        result["compiler_stats"] = cache.stats

        if total_changed and (result["reduced_lines"] < result["original_lines"] or len(src) < len(original_src)):
            result["status"] = "reduced"
            with open(output_path, "w") as f:
                f.write(src)
        else:
            result["status"] = "clean"
            shutil.copy2(c_filepath, output_path)

        return result

    except Exception as e:
        result["status"] = "error"
        result["error"] = str(e)
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
    try:
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except:
        pass

def _worker_fn(args):
    c_filepath, output_path, dry_run = args
    check_disk_space(min_free_gb=2)
    return reduce_file(c_filepath, _get_header_dir(c_filepath), output_path, dry_run)

def main():
    ap = argparse.ArgumentParser(description="Stage 4: Clang-delta reducer — optimised with TCC guard")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--group", type=str, default=None)
    ap.add_argument("-j", "--workers", type=int, default=None)
    ap.add_argument("--diagnose", type=str, default=None)
    ap.add_argument("--input-dir", type=str, default=INPUT_DIR)
    ap.add_argument("--output-dir", type=str, default=OUTPUT_DIR)
    ap.add_argument("--overwrite", action="store_true")
    args = ap.parse_args()

    if not os.path.isfile(CLANG_DELTA):
        print(f"WARNING: clang_delta not found: {CLANG_DELTA}")
        print("Python passes will still run.")

    input_dir, output_dir = args.input_dir, args.output_dir
    num_workers = args.workers or max(1, multiprocessing.cpu_count() // 2)

    if args.diagnose:
        c = args.diagnose
        hd = _get_header_dir(c)
        os.makedirs(output_dir, exist_ok=True)
        g = next((g for g in GROUPS if g in c), "")
        if g:
            od = os.path.join(output_dir, g)
            os.makedirs(od, exist_ok=True)
            op = os.path.join(od, os.path.basename(c))
        else:
            op = os.path.join(output_dir, os.path.basename(c))

        print(f"=== CLANG-REDUCE: {c} ===\n")
        res = reduce_file(c, hd, op, verbose=True)
        print(f"\nStatus: {res['status']}")
        if res.get('error'):
            print(f"Error: {res['error']}")
        sv = res['original_lines'] - res['reduced_lines']
        print(f"Original:  {res['original_lines']} lines")
        print(f"Reduced:   {res['reduced_lines']} lines")
        print(f"TCC Guard: {'active' if res.get('tcc_enabled') else 'inactive'}")
        if res['original_lines'] > 0:
            print(f"Saved:     {sv} lines ({sv/res['original_lines']*100:.1f}%)")

        if "compiler_stats" in res:
            cs = res["compiler_stats"]
            print("\n--- Compiler diagnostics ---")
            print(f"  Cache Hits: {cs['cache_hits']}")
            print(f"  TCC Checks: {cs['tcc_checks']} ({cs['tcc_rejects']} rejected → IDO calls saved)")
            print(f"  IDO Checks: {cs['ido_checks']} ({cs['ido_success']} hash OK, {cs['ido_rejects']} rejected)")

        if res['status'] == 'reduced':
            print("\n--- Reduced code ---")
            with open(op) as f:
                print(f.read())
        return

    groups = GROUPS if not args.group else [args.group]
    if args.group and args.group not in GROUPS:
        print(f"Error: group '{args.group}' not found.")
        sys.exit(1)
    os.makedirs(output_dir, exist_ok=True)

    total_count, skipped = 0, 0
    for group in groups:
        gi, go = os.path.join(input_dir, group), os.path.join(output_dir, group)
        if not os.path.isdir(gi):
            continue
        os.makedirs(go, exist_ok=True)
        for e in os.scandir(gi):
            if e.name.endswith(".c") and e.is_file():
                if not args.overwrite and os.path.exists(os.path.join(go, e.name)):
                    skipped += 1
                else:
                    total_count += 1

    print(f"Found: {total_count+skipped} | Skipped: {skipped} | To process: {total_count}")
    print(f"TCC Guard: {'available' if _tcc_available() else 'NOT available'}")
    print(f"Worker: {num_workers}")
    print(f"Input:  {input_dir}\nOutput: {output_dir}")
    if total_count == 0:
        print("Nothing to do.")
        return

    def _gen():
        for group in groups:
            gi, go = os.path.join(input_dir, group), os.path.join(output_dir, group)
            if not os.path.isdir(gi):
                continue
            for e in os.scandir(gi):
                if e.name.endswith(".c") and e.is_file():
                    op = os.path.join(go, e.name)
                    if not args.overwrite and os.path.exists(op):
                        continue
                    yield (e.path, op, args.dry_run)

    try:
        now = time.time()
        for d in os.scandir(TMP_ROOT):
            if d.name.startswith("cr_") and d.is_dir():
                try:
                    if now - d.stat().st_mtime > 300:
                        shutil.rmtree(d.path, ignore_errors=True)
                except:
                    pass
    except:
        pass

    stats = {"clean": 0, "reduced": 0, "error": 0, "clang_skipped": 0}
    total_saved = 0
    lp = os.path.join(output_dir, "clang_reduce_log.jsonl")
    ep = os.path.join(output_dir, "clang_reduce_errors.jsonl")

    with open(lp, "a") as lf, open(ep, "a") as ef:
        with multiprocessing.Pool(num_workers, initializer=_worker_init) as pool:
            try:
                for res in tqdm(pool.imap_unordered(_worker_fn, _gen(), chunksize=1),
                                total=total_count, desc="Clang-Reduce"):
                    stats[res["status"]] = stats.get(res["status"], 0) + 1
                    sv = res["original_lines"] - res["reduced_lines"]
                    if sv > 0 and res["status"] == "reduced":
                        total_saved += sv
                        lf.write(json.dumps(res, ensure_ascii=False) + "\n")
                        lf.flush()
                    if res["status"] == "error":
                        ef.write(json.dumps({"timestamp": datetime.now().isoformat(),
                            "file": res["file"], "error": res.get("error")},
                            ensure_ascii=False) + "\n")
                        ef.flush()
            except KeyboardInterrupt:
                print("\nAborted!")
                pool.terminate()
                pool.join()
                sys.exit(1)

    print("\n" + "=" * 60)
    print(f"  Clean: {stats['clean']} | Reduced: {stats['reduced']} | Errors: {stats['error']}")
    print(f"  Lines saved: {total_saved}")
    print(f"  Output: {output_dir}")

if __name__ == "__main__":
    main()
