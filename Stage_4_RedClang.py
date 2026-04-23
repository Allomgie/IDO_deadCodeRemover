#!/usr/bin/env python3
"""
Stage 4: Clang-Delta Reducer — OPTIMIERTE VERSION mit TCC Fast-Reject Guard.

Hybrider Ansatz: Python-Passes zuerst (schnell, TCC-geschuetzt),
Clang-Delta nur noch fuer aggregate-to-scalar und komplexe Faelle.

Key Optimierungen:
  1. TCC Fast-Reject fuer syntaktisch sichere Python-Passes (spart IDO-Aufrufe)
  2. Globaler CompileCache pro Datei (ueber alle Passes)
  3. Zentraler Syntax-Validator VOR Compiler-Aufruf
  4. Korrigierte Python-Passes (remove-unused-function, return-void, neutralize-calls)
  5. Gatekeeper: Entscheidet, ob Clang-Delta ueberhaupt noetig ist
  6. count('\n') statt splitlines()

Input:  dataset/TokenReduced/
Output: dataset/ClangReduced/
"""

import os, sys, re, subprocess, signal, tempfile, shutil, hashlib
import json, time, argparse, multiprocessing, resource
from datetime import datetime
from tqdm import tqdm

BASE_DIR     = "/home/user/deadCodeRemover"
PROJECT_ROOT = os.path.join(BASE_DIR, "CompilerRoot")
IDO_DIR      = os.path.abspath(os.path.join(PROJECT_ROOT, "tools", "ido"))
IDO_CC       = os.path.join(IDO_DIR, "cc")

INPUT_DIR    = os.path.join(BASE_DIR, "dataset_Stage_3")
OUTPUT_DIR   = os.path.join(BASE_DIR, "dataset_Stage_4")
OBJDUMP      = "mips-linux-gnu-objdump"
TMP_ROOT     = "/dev/shm"
CLANG_DELTA  = "/usr/local/bin/clang_delta"

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
    """Prueft, ob tcc im PATH verfuegbar ist."""
    return shutil.which("tcc") is not None

def tcc_syntax_check(c_source: str, tmp_dir: str, header_dir: str) -> bool:
    """
    Schneller Syntax-Check mit TCC.
    Gibt True zurueck, wenn TCC ohne Fehler durchlaueft.
    Nutzt subprocess.call mit DEVNULL, um OS-Pipe-Overhead zu vermeiden.
    Timeout: 5 Sekunden (TCC ist extrem schnell).
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
        # STDOUT und STDERR ins Nichts leiten eliminiert den Pipe-Overhead komplett
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
#  GLOBALER COMPILE CACHE (mit TCC-Integration)
# =====================================================================

class CompileCache:
    """Zentraler Cache fuer Kompilierungsergebnisse pro Datei."""
    def __init__(self, tcc_baseline_ok: bool = False):
        self._success = {}
        self._rejected = set()
        self._tcc_rejected = set()
        self._tcc_baseline_ok = tcc_baseline_ok and _tcc_available()

        # NEU: Statistik-Zaehler
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
        Versucht einen Kandidaten zu kompilieren.
        Prueft zuerst TCC-Cache (bei sicheren Passes), dann IDO als Gold-Standard.
        """
        if not candidate or not candidate.strip():
            return None, "empty"

        key = hashlib.md5(candidate.encode()).hexdigest()

        # 1. IDO-Cache pruefen
        if key in self._rejected:
            self.stats["cache_hits"] += 1
            return None, "rejected"
        if key in self._success:
            self.stats["cache_hits"] += 1
            return self._success[key], ""

        # 2. TCC Fast-Reject (nur fuer sichere Passes & wenn Baseline ok)
        if self._tcc_baseline_ok and pass_name in TCC_SAFE_PASSES:
            if key in self._tcc_rejected:
                self.stats["cache_hits"] += 1
                return None, "tcc_rejected"

            self.stats["tcc_checks"] += 1
            if not tcc_syntax_check(candidate, tmp_dir, header_dir):
                self._tcc_rejected.add(key)
                self.stats["tcc_rejects"] += 1
                return None, "tcc_fail"

        # 3. IDO Gold-Standard Check
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
    """Schneller Heuristik-Check auf offensichtliche Syntax-Fehler."""
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
#  INFRASTRUKTUR
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
    Kompiliert C-Quelltext ueber gcc -E und IDO zu einem normalisierten ASM-Hash.
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
        if g in path: return os.path.join(DATASET_DIR, f"{g}_headers")
    return ""

def check_disk_space(min_free_gb=2):
    while True:
        if shutil.disk_usage("/").free / (1024**3) >= min_free_gb: return
        time.sleep(30)

# =====================================================================
#  HILFSFUNKTIONEN
# =====================================================================

def _find_matching(s: str, open_pos: int, open_char: str, close_char: str) -> int:
    """Findet die schließende Klammer zu open_pos. Gibt -1 zurueck wenn nicht gefunden."""
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
    """Prueft, ob simplify-if ueberhaupt anwendbar ist."""
    return bool(re.search(r'\bif\s*\(', src))

def has_comma_expressions(src: str) -> bool:
    """Prueft, ob simplify-comma anwendbar ist."""
    # Schneller Check: Gibt es Klammern mit Komma?
    pairs = _find_all_balanced(src, '(', ')')
    for start, end in pairs:
        inner = src[start+1:end]
        if ',' in inner and _split_args(inner):
            return True
    return False

def has_declarations(src: str) -> bool:
    """Prueft, ob remove-unused-var anwendbar ist."""
    return bool(re.search(r'\b(int|char|short|long|unsigned|signed|float|double|void|struct\s+\w+|union\s+\w+|enum\s+\w+)\s+\w', src))

def has_function_definitions(src: str) -> bool:
    """Prueft, ob remove-unused-function anwendbar ist."""
    # Suche nach 'name(args) {' Mustern
    return bool(re.search(r'\b\w+\s*\([^)]*\)\s*\{', src))

def has_function_calls(src: str) -> bool:
    """Prueft, ob neutralize-calls anwendbar ist."""
    calls = re.findall(r'\b([a-zA-Z_][a-zA-Z0-9_]*)\s*\(', src)
    keywords = {"if", "while", "for", "switch", "return", "sizeof"}
    return any(c not in keywords for c in calls)

def has_return_statements(src: str) -> bool:
    """Prueft, ob return-void anwendbar ist."""
    return bool(re.search(r'\breturn\b', src))

def is_worth_clang(src: str) -> tuple[bool, str]:
    """
    Gatekeeper: Entscheidet, ob Clang-Delta noetig ist.
    Gibt (needs_clang, reason) zurueck.
    """
    # Check 1: Struct/Array-Zugriffe (aggregate-to-scalar)
    if re.search(r'\.|->|\[', src):
        return True, "aggregate-to-scalar"

    # Check 2: Komplexe Funktionsaufrufe (simplify-callexpr)
    calls = re.findall(r'\b([a-zA-Z_][a-zA-Z0-9_]*)\s*\(', src)
    keywords = {"if", "while", "for", "switch", "return", "sizeof"}
    real_calls = [c for c in calls if c not in keywords]
    if real_calls:
        return True, "simplify-callexpr"

    # Check 3: Mehrere Funktionsdefinitionen (remove-unused-function)
    defs = re.findall(r'\b([a-zA-Z_][a-zA-Z0-9_]*)\s*\([^)]*\)\s*\{', src)
    if len(defs) > 1:
        return True, "remove-unused-function"

    return False, "python-sufficient"

# =====================================================================
#  PYTHON PASSES (mit TCC-Guard / pass_name)
# =====================================================================

def pass_simplify_if(src: str, baseline: str, tmp_dir: str, header_dir: str,
                     cache: CompileCache, pass_name: str = "simplify-if") -> tuple[str, bool]:
    """Python-Port von simplify-if mit aggressivem und konservativem Modus."""
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
            if close_p == -1: continue

            cond = src[open_p+1:close_p]
            after_if = src[close_p+1:].lstrip()
            if not after_if.startswith('{'):
                continue

            open_b = src.find('{', close_p)
            close_b = _find_matching(src, open_b, '{', '}')
            if close_b == -1: continue

            true_body = src[open_b+1:close_b]

            # Cache-Key fuer diesen if-Block
            ctx_key = hashlib.md5(f"if|{cond}|{true_body[:50]}".encode()).hexdigest()
            if ctx_key in fail_cache:
                continue

            after_true = src[close_b+1:].lstrip()

            if after_true.startswith("else"):
                else_start = src.find("else", close_b+1)
                after_else = src[else_start+4:].lstrip()

                if after_else.startswith('{'):
                    open_else_b = src.find('{', else_start)
                    close_else_b = _find_matching(src, open_else_b, '{', '}')
                    if close_else_b == -1: continue

                    false_body = src[open_else_b+1:close_else_b]

                    # Kandidat 1: Behalte NUR True-Zweig
                    cand_true = src[:match.start()] + true_body + src[close_else_b+1:]
                    h, _ = cache.try_candidate(cand_true, tmp_dir, header_dir, baseline, pass_name)
                    if h == baseline:
                        src = cand_true; changed = True; made_progress = True; break

                    # Kandidat 2: Behalte NUR False-Zweig
                    cand_false = src[:match.start()] + false_body + src[close_else_b+1:]
                    h, _ = cache.try_candidate(cand_false, tmp_dir, header_dir, baseline, pass_name)
                    if h == baseline:
                        src = cand_false; changed = True; made_progress = True; break
            else:
                # Aggressiv: if(cond){body} -> body
                cand_agg = src[:match.start()] + true_body + src[close_b+1:]
                h, _ = cache.try_candidate(cand_agg, tmp_dir, header_dir, baseline, pass_name)
                if h == baseline:
                    src = cand_agg; changed = True; made_progress = True; break

                # Konservativ: if(cond){body} -> cond; body
                cand_clang = src[:match.start()] + cond + ";" + true_body + src[close_b+1:]
                h, _ = cache.try_candidate(cand_clang, tmp_dir, header_dir, baseline, pass_name)
                if h == baseline:
                    src = cand_clang; changed = True; made_progress = True; break

            if not made_progress:
                fail_cache.add(ctx_key)

        if not made_progress:
            break

    return src, changed


def pass_remove_unused_var(src: str, baseline: str, tmp_dir: str, header_dir: str,
                           cache: CompileCache, pass_name: str = "remove-unused-var") -> tuple[str, bool]:
    """Python-Port von remove-unused-var. Fokus auf Komma-Deklarationen."""
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

            # Fast-Fail: Funktions-Prototypen ueberspringen
            if '(' in vars_part and '=' not in vars_part:
                continue

            var_list = _split_args(vars_part)
            if len(var_list) <= 1:
                # Single-Variable: Versuche komplett zu loeschen
                candidate = src[:match.start()] + src[match.end():]
                ctx_key = hashlib.md5(f"unused_var|{match.group(0)}".encode()).hexdigest()
                if ctx_key in fail_cache:
                    continue
                h, _ = cache.try_candidate(candidate, tmp_dir, header_dir, baseline, pass_name)
                if h == baseline:
                    src = candidate; changed = True; made_progress = True; break
                else:
                    fail_cache.add(ctx_key)
                continue

            # Multi-Variable: Entferne einzelne Elemente
            for i in range(len(var_list)):
                new_vars = var_list[:i] + var_list[i+1:]
                new_decl = type_part + " " + ", ".join(new_vars) + ";"
                candidate = src[:match.start()] + new_decl + src[match.end():]

                ctx_key = hashlib.md5(f"unused_var|{i}|{match.group(0)}".encode()).hexdigest()
                if ctx_key in fail_cache:
                    continue

                h, _ = cache.try_candidate(candidate, tmp_dir, header_dir, baseline, pass_name)
                if h == baseline:
                    src = candidate; changed = True; made_progress = True; break
                else:
                    fail_cache.add(ctx_key)

            if made_progress:
                break

        if not made_progress:
            break

    return src, changed


def pass_remove_unused_function(src: str, baseline: str, tmp_dir: str, header_dir: str,
                                cache: CompileCache, pass_name: str = "remove-unused-func") -> tuple[str, bool]:
    """Korrigierte Version: Match auf ALLE Definitionen, nicht nur static/inline."""
    if not has_function_definitions(src):
        return src, False

    changed = False
    # Sammle ALLE Funktionsdefinitionen
    func_pattern = re.compile(r'\b([a-zA-Z_][a-zA-Z0-9_]*)\s*\(')
    fail_cache = set()

    for _iteration in range(20):
        # Finde alle Definitionen
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

        # Sortiere nach Position (von hinten nach vorne)
        for func_name in sorted(defined.keys(), key=lambda n: defined[n][0], reverse=True):
            start_pos, end_pos = defined[func_name]

            # Pruefe: Wird die Funktion AUSSERHALB ihrer Definition aufgerufen?
            call_pattern = re.compile(rf'\b{re.escape(func_name)}\s*\(')
            used_elsewhere = False
            for call_match in call_pattern.finditer(src):
                if not (start_pos <= call_match.start() <= end_pos):
                    used_elsewhere = True
                    break

            if used_elsewhere:
                continue

            # Finde den Start des Rueckgabetyps
            type_start = start_pos
            while type_start > 0 and src[type_start-1] not in ';{}':
                type_start -= 1

            ctx_key = hashlib.md5(f"unused_func|{func_name}|{type_start}".encode()).hexdigest()
            if ctx_key in fail_cache:
                continue

            candidate = src[:type_start] + src[end_pos+1:]
            h, _ = cache.try_candidate(candidate, tmp_dir, header_dir, baseline, pass_name)
            if h == baseline:
                src = candidate; changed = True; made_progress = True; break
            else:
                fail_cache.add(ctx_key)

        if not made_progress:
            break

    return src, changed


def pass_simplify_comma(src: str, baseline: str, tmp_dir: str, header_dir: str,
                        cache: CompileCache, pass_name: str = "simplify-comma") -> tuple[str, bool]:
    """Python-Port von SimplifyCommaExpr."""
    if not has_comma_expressions(src):
        return src, False

    changed = False
    pattern = re.compile(r'\(')
    fail_cache = set()

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
                src = candidate; changed = True; made_progress = True; break
            else:
                fail_cache.add(ctx_key)

        if not made_progress:
            break

    return src, changed


def pass_neutralize_calls(src: str, baseline: str, tmp_dir: str, header_dir: str,
                          cache: CompileCache, pass_name: str = "neutralize-calls") -> tuple[str, bool]:
    """Korrigierte Version mit robusterem Kontext-Check."""
    if not has_function_calls(src):
        return src, False

    changed = False
    pattern = re.compile(r'\b([a-zA-Z_][a-zA-Z0-9_]*)\s*\(')
    fail_cache = set()

    for _iteration in range(20):
        matches = list(pattern.finditer(src))
        made_progress = False

        for match in reversed(matches):
            func_name = match.group(1)
            if func_name in {"main", "if", "while", "for", "switch", "sizeof"}:
                continue

            open_p = match.end() - 1
            close_p = _find_matching(src, open_p, '(', ')')
            if close_p == -1: continue

            # Kontext-Analyse
            before = src[match.start()-1] if match.start() > 0 else ''
            after = src[close_p+1] if close_p+1 < len(src) else ''

            # Bestimme erlaubte Replacements basierend auf Kontext
            replacements = []

            # Standalone-Statement: func(); -> ;
            if before in ';{}' and after == ';':
                replacements.extend([";", "(void)0;"])

            # In Zuweisung: x = func(); -> x = 0;
            if before == '=':
                replacements.extend(["0", "(void)0"])

            # In Bedingung: if(func()) -> if(0)
            if before == '(':
                replacements.extend(["0", "1"])

            # In Argumentliste: foo(func()) -> foo(0)
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
                    src = candidate; changed = True; made_progress = True; break
                else:
                    fail_cache.add(ctx_key)

            if made_progress:
                break

        if not made_progress:
            break

    return src, changed


def pass_return_void(src: str, baseline: str, tmp_dir: str, header_dir: str,
                     cache: CompileCache, pass_name: str = "return-void") -> tuple[str, bool]:
    """Korrigierte Version mit Seiteneffekt-Check."""
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
        if close_p == -1: continue

        after_args = src[close_p+1:].lstrip()
        if not after_args.startswith('{'): continue

        open_b = src.find('{', close_p)
        close_b = _find_matching(src, open_b, '{', '}')
        if close_b == -1: continue

        # Finde Rueckgabetyp
        type_start = match.start()
        while type_start > 0 and src[type_start-1] not in ';{}':
            type_start -= 1

        type_prefix = src[type_start:match.start()].strip()
        if "void" in type_prefix: continue

        body = src[open_b+1:close_b]

        # Pruefe Return-Statements
        ret_pattern = re.compile(r'\breturn\b\s*([^;]*);')

        # Sammle alle Returns
        returns = list(ret_pattern.finditer(body))
        if not returns:
            continue

        # Pruefe, ob alle Returns Seiteneffekt-frei sind
        all_safe = True
        for ret_match in returns:
            expr = ret_match.group(1).strip()
            # Heuristik: Funktionsaufrufe = Seiteneffekte
            if re.search(r'\b\w+\s*\(', expr):
                all_safe = False
                break

        ctx_key = hashlib.md5(f"ret_void|{func_name}|{type_prefix}".encode()).hexdigest()
        if ctx_key in fail_cache:
            continue

        if all_safe:
            # Aggressiv: return expr; -> ;
            new_body = ret_pattern.sub(r';', body)
        else:
            # Konservativ: return expr; -> expr;
            new_body = ret_pattern.sub(r'\1;', body)

        new_header = "void " + src[match.start():open_b+1]
        candidate = src[:type_start] + new_header + new_body + src[close_b:]

        h, _ = cache.try_candidate(candidate, tmp_dir, header_dir, baseline, pass_name)
        if h == baseline:
            src = candidate; changed = True
        else:
            fail_cache.add(ctx_key)

    return src, changed


# =====================================================================
#  CLANG-DELTA PIPELINE (nur noch fuer aggregate-to-scalar)
# =====================================================================

def preprocess_file(c_filepath, header_dir, tmp_dir):
    """gcc -E auf die C-Datei."""
    with open(c_filepath, "r") as f:
        fixed_src = re.sub(r'#include\s+"[^"]*?([^/"]+\.h)"', r'#include "\1"', f.read())
    c_path = os.path.join(tmp_dir, "src.c")
    pp_path = os.path.join(tmp_dir, "pp.c")

    with open(c_path, "w") as f: f.write(fixed_src)

    cmd = ["gcc", "-E", "-P", "-xc", "-D_LANGUAGE_C", "-D_MIPS_SZLONG=32"]
    for inc in INCLUDE_DIRS: cmd += ["-I", inc]
    if header_dir: cmd += ["-I", header_dir]
    cmd += [c_path, "-o", pp_path]

    try: rc, _, _ = run_cmd_safely(cmd, timeout=30)
    except subprocess.TimeoutExpired: return None
    try: os.unlink(c_path)
    except: pass
    if rc != 0: return None
    return pp_path


def run_clang_transform(pp_path, transformation, counter, tmp_dir):
    """Fuehrt eine clang_delta Transformation aus."""
    out_path = os.path.join(tmp_dir, "cd_out.c")
    try: os.unlink(out_path)
    except OSError: pass

    cmd = [CLANG_DELTA,
           f"--transformation={transformation}",
           f"--counter={counter}",
           f"--output={out_path}",
           pp_path]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
    except subprocess.TimeoutExpired:
        return None

    if res.returncode == 0 and os.path.exists(out_path):
        return out_path
    return None


def apply_clang_passes(pp_path, baseline_hash, tmp_dir, local_cache, verbose=False):
    """Wendet nur noch aggregate-to-scalar an."""
    changed_total = False
    current_size = os.path.getsize(pp_path)

    # Nur noch aggregate-to-scalar
    transforms = ["aggregate-to-scalar"]

    cycle_iterations = 0
    max_cycles = 5
    cycle_changed = True

    while cycle_changed and cycle_iterations < max_cycles:
        cycle_iterations += 1
        cycle_changed = False
        if verbose: print(f"\n  === Clang Cycle {cycle_iterations} ===")

        for transform in transforms:
            counter = 1
            transform_changed = False

            while True:
                out = run_clang_transform(pp_path, transform, counter, tmp_dir)
                if out is None:
                    break 

                out_size = os.path.getsize(out)
                if out_size == current_size:
                    with open(pp_path, "rb") as f1, open(out, "rb") as f2:
                        if f1.read() == f2.read():
                            try: os.unlink(out)
                            except: pass
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
                    try: os.unlink(out)
                    except: pass
                    counter += 1

            if verbose:
                if transform_changed:
                    lines = open(pp_path).read().count('\n') + 1
                    print(f"    [{transform}] reduziert -> {lines} Zeilen")
                else:
                    print(f"    [{transform}] keine Aenderung")

    if verbose: print(f"\n  Clang Pipeline stabilisiert nach {cycle_iterations} Zyklen.")
    return changed_total


def compute_asm_hash_from_pp(pp_path, tmp_dir, local_cache=None):
    """Kompiliert pre-processed Code und validiert."""
    with open(pp_path, "rb") as f:
        c_bytes = f.read()
    text_hash = hashlib.md5(c_bytes).hexdigest()

    if local_cache is not None and text_hash in local_cache:
        return local_cache[text_hash]

    o_path = os.path.join(tmp_dir, "clang_test.o")

    def _rm(p):
        try: os.unlink(p)
        except OSError: pass

    try:
        cmd = [IDO_CC, "-c", "-O2", "-mips2", "-G", "0", "-w", pp_path, "-o", o_path]
        try: rc, _, _ = run_cmd_safely(cmd, cwd=tmp_dir, env=_IDO_ENV, timeout=30)
        except subprocess.TimeoutExpired: _rm(o_path); return None
        if rc != 0: _rm(o_path); return None

        cmd_text = [OBJDUMP, "-d", "-z", o_path]
        try: rc, stdout_text, _ = run_cmd_safely(cmd_text, timeout=30)
        except subprocess.TimeoutExpired: _rm(o_path); return None
        if rc != 0: _rm(o_path); return None

        cmd_data = [OBJDUMP, "-s", "-j", ".rodata", "-j", ".data", "-j", ".bss", o_path]
        try: rc2, stdout_data, _ = run_cmd_safely(cmd_data, timeout=30)
        except subprocess.TimeoutExpired: stdout_data = b""

        _rm(o_path)

        asm_payload = []

        for line in stdout_text.decode(errors="replace").splitlines():
            m = re.match(r'^\s*[0-9a-fA-F]+:\s+[0-9a-fA-F]+\s+(.*)', line)
            if not m: continue
            s = m.group(1).strip().split('#')[0].strip()
            if not s: continue
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
            if local_cache is not None: local_cache[text_hash] = None
            return None

        h = hashlib.md5("\n".join(asm_payload).encode()).hexdigest()
        if local_cache is not None: local_cache[text_hash] = h
        return h

    except Exception:
        _rm(o_path); return None


def compute_asm_hash_from_source(c_source, tmp_dir, header_dir):
    """Kompiliert C-Quelltext mit Includes."""
    fixed = re.sub(r'#include\s+"[^"]*?([^/"]+\.h)"', r'#include "\1"', c_source)
    cache_key = hashlib.md5((fixed + (header_dir or "")).encode()).hexdigest()
    if cache_key in _asm_cache:
        return _asm_cache[cache_key], ""

    c_path = os.path.join(tmp_dir, "_src.c")
    i_path = os.path.join(tmp_dir, "_src.i")

    def _cleanup():
        for p in [c_path, i_path]:
            try: os.unlink(p)
            except OSError: pass

    try:
        with open(c_path, "w") as f: f.write(fixed)
        cmd = ["gcc", "-E", "-P", "-xc", "-D_LANGUAGE_C", "-D_MIPS_SZLONG=32"]
        for inc in INCLUDE_DIRS: cmd += ["-I", inc]
        if header_dir: cmd += ["-I", header_dir]
        cmd += [c_path, "-o", i_path]
        try: rc, _, _ = run_cmd_safely(cmd, timeout=30)
        except subprocess.TimeoutExpired: _cleanup(); return None, "gcc timeout"
        try: os.unlink(c_path)
        except: pass
        if rc != 0: _cleanup(); return None, "gcc fail"

        h = compute_asm_hash_from_pp(i_path, tmp_dir)
        try: os.unlink(i_path)
        except: pass
        if h: _asm_cache[cache_key] = h
        return h, ("" if h else "compile fail")
    except Exception as e:
        _cleanup(); return None, str(e)


# =====================================================================
#  HAUPTFUNKTION PRO DATEI
# =====================================================================

def reduce_file(c_filepath, header_dir, output_path, dry_run=False, verbose=False):
    """
    Reduziert eine einzelne C-Datei durch Python-Passes (TCC-geschuetzt)
    und optional Clang-Delta fuer aggregate-to-scalar.
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

    # Includes merken
    include_lines = []
    for line in original_src.splitlines():
        if re.match(r'^\s*#\s*include', line):
            include_lines.append(line)

    tmp_dir = tempfile.mkdtemp(dir=TMP_ROOT, prefix="cr_")
    try:
        # Baseline
        baseline, err = compute_asm_hash_from_source(original_src, tmp_dir, header_dir)
        if baseline is None:
            result["status"] = "error"
            result["error"] = f"Baseline kompiliert nicht: {err}"
            return result
        if dry_run:
            result["status"] = "would_reduce"; return result

        # TCC-Baseline Check
        tcc_baseline_ok = False
        if _tcc_available():
            tcc_baseline_ok = tcc_syntax_check(original_src, tmp_dir, header_dir)
            result["tcc_baseline_ok"] = tcc_baseline_ok
            if verbose:
                status = "OK" if tcc_baseline_ok else "FAIL (TCC deaktiviert)"
                print(f"  TCC Baseline Check: {status}")

        cache = CompileCache(tcc_baseline_ok=tcc_baseline_ok)
        result["tcc_enabled"] = tcc_baseline_ok

        if verbose:
            print(f"  Baseline: {result['original_lines']} Zeilen")

        src = original_src
        total_changed = False

        # === PYTHON PASSES (schnell, TCC-geschuetzt) ===
        if verbose: print("\n  === Python Passes ===")

        passes = [
            ("simplify-if", pass_simplify_if),
            ("remove-unused-var", pass_remove_unused_var),
            ("remove-unused-func", pass_remove_unused_function),
            ("simplify-comma", pass_simplify_comma),
            ("neutralize-calls", pass_neutralize_calls),
            ("return-void", pass_return_void),
        ]

        for pass_name, pass_func in passes:
            new_src, changed = pass_func(src, baseline, tmp_dir, header_dir, cache, pass_name)
            if changed:
                src = new_src
                total_changed = True
                if verbose:
                    lines = src.count('\n') + (1 if src and not src.endswith('\n') else 0)
                    print(f"    [{pass_name}] -> {lines} Zeilen")
            elif verbose:
                print(f"    [{pass_name}] keine Aenderung")

        needs_clang, reason = is_worth_clang(src)

        if not needs_clang:
            if verbose: print(f"\n  Gatekeeper: Clang nicht noetig ({reason})")
        else:
            if verbose: print(f"\n  Gatekeeper: Clang noetig ({reason})")

            # Pre-process fuer clang_delta
            pp_path = preprocess_file(c_filepath, header_dir, tmp_dir)
            if pp_path is None:
                result["status"] = "error"
                result["error"] = "Pre-Processing fehlgeschlagen"
                # Statistik anhaengen falls moeglich
                result["compiler_stats"] = cache.stats
                return result

            local_cache = {}
            changed = apply_clang_passes(pp_path, baseline, tmp_dir, local_cache, verbose)

            if changed:
                total_changed = True
                # Sicherheitscheck
                final_hash = compute_asm_hash_from_pp(pp_path, tmp_dir, local_cache)
                if final_hash != baseline:
                    result["status"] = "error"
                    result["error"] = f"Final hash mismatch"
                    result["compiler_stats"] = cache.stats
                    return result

                with open(pp_path) as f:
                    src = f.read()
                if include_lines:
                    src = "\n".join(include_lines) + "\n" + src

        result["reduced_lines"] = src.count('\n') + (1 if src and not src.endswith('\n') else 0)

        # NEU: Statistik an das Ergebnis anhaengen
        result["compiler_stats"] = cache.stats

        if total_changed and (result["reduced_lines"] < result["original_lines"] or len(src) < len(original_src)):
            result["status"] = "reduced"
            with open(output_path, "w") as f: f.write(src)
        else:
            result["status"] = "clean"
            shutil.copy2(c_filepath, output_path)

        return result

    except Exception as e:
        result["status"] = "error"; result["error"] = str(e)
        # Statistik anhaengen falls Cache existiert
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

def _worker_fn(args):
    c_filepath, output_path, dry_run = args
    check_disk_space(min_free_gb=2)
    return reduce_file(c_filepath, _get_header_dir(c_filepath), output_path, dry_run)

def main():
    ap = argparse.ArgumentParser(description="Stage 4: Clang-Delta Reducer - OPTIMIERT mit TCC Guard")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--group", type=str, default=None)
    ap.add_argument("-j", "--workers", type=int, default=None)
    ap.add_argument("--diagnose", type=str, default=None)
    ap.add_argument("--input-dir", type=str, default=INPUT_DIR)
    ap.add_argument("--output-dir", type=str, default=OUTPUT_DIR)
    ap.add_argument("--overwrite", action="store_true")
    args = ap.parse_args()

    if not os.path.isfile(CLANG_DELTA):
        print(f"WARNUNG: clang_delta nicht gefunden: {CLANG_DELTA}")
        print("Python-Passes werden trotzdem ausgefuehrt.")

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

        print(f"=== CLANG-REDUCE: {c} ===\n")
        res = reduce_file(c, hd, op, verbose=True)
        print(f"\nStatus: {res['status']}")
        if res.get('error'): print(f"Error: {res['error']}")
        sv = res['original_lines'] - res['reduced_lines']
        print(f"Original:  {res['original_lines']} Zeilen")
        print(f"Reduziert: {res['reduced_lines']} Zeilen")
        print(f"TCC Guard: {'aktiv' if res.get('tcc_enabled') else 'inaktiv'}")
        if res['original_lines'] > 0:
            print(f"Ersparnis: {sv} Zeilen ({sv/res['original_lines']*100:.1f}%)")

        # NEU: Compiler Diagnose Ausgabe
        if "compiler_stats" in res:
            cs = res["compiler_stats"]
            print("\n--- Compiler Diagnose ---")
            print(f"  Cache Hits: {cs['cache_hits']}")
            print(f"  TCC Checks: {cs['tcc_checks']} (davon {cs['tcc_rejects']} abgelehnt -> IDO-Aufrufe gespart!)")
            print(f"  IDO Checks: {cs['ido_checks']} (davon {cs['ido_success']} Hash OK, {cs['ido_rejects']} abgelehnt)")

        if res['status'] == 'reduced':
            print("\n--- Reduzierter Code ---")
            with open(op) as f: print(f.read())
        return

    groups = GROUPS if not args.group else [args.group]
    if args.group and args.group not in GROUPS:
        print(f"Fehler: Gruppe '{args.group}' nicht bekannt."); sys.exit(1)
    os.makedirs(output_dir, exist_ok=True)

    total_count, skipped = 0, 0
    for group in groups:
        gi, go = os.path.join(input_dir, group), os.path.join(output_dir, group)
        if not os.path.isdir(gi): continue
        os.makedirs(go, exist_ok=True)
        for e in os.scandir(gi):
            if e.name.endswith(".c") and e.is_file():
                if not args.overwrite and os.path.exists(os.path.join(go, e.name)):
                    skipped += 1
                else: total_count += 1

    print(f"Gefunden: {total_count+skipped} | Uebersprungen: {skipped} | Zu verarbeiten: {total_count}")
    print(f"TCC Guard: {'verfuegbar' if _tcc_available() else 'NICHT verfuegbar'}")
    print(f"Worker: {num_workers}")
    print(f"Input:  {input_dir}\nOutput: {output_dir}")
    if total_count == 0: print("Nichts zu tun."); return

    def _gen():
        for group in groups:
            gi, go = os.path.join(input_dir, group), os.path.join(output_dir, group)
            if not os.path.isdir(gi): continue
            for e in os.scandir(gi):
                if e.name.endswith(".c") and e.is_file():
                    op = os.path.join(go, e.name)
                    if not args.overwrite and os.path.exists(op): continue
                    yield (e.path, op, args.dry_run)

    # Cleanup
    try:
        now = time.time()
        for d in os.scandir(TMP_ROOT):
            if d.name.startswith("cr_") and d.is_dir():
                try:
                    if now - d.stat().st_mtime > 300:
                        shutil.rmtree(d.path, ignore_errors=True)
                except: pass
    except: pass

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
                        lf.write(json.dumps(res, ensure_ascii=False) + "\n"); lf.flush()
                    if res["status"] == "error":
                        ef.write(json.dumps({"timestamp": datetime.now().isoformat(),
                            "file": res["file"], "error": res.get("error")},
                            ensure_ascii=False) + "\n"); ef.flush()
            except KeyboardInterrupt:
                print("\nAbgebrochen!"); pool.terminate(); pool.join(); sys.exit(1)

    print("\n" + "=" * 60)
    print(f"  Sauber: {stats['clean']} | Reduziert: {stats['reduced']} | Fehler: {stats['error']}")
    print(f"  Zeilen gespart: {total_saved}")
    print(f"  Output: {output_dir}")

if __name__ == "__main__":
    main()