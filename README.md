# IDO-5.3 Dead-Code Reducer Pipeline

> **Compiler-guided delta debugging for the IDO 5.3 MIPS compiler**  
> A 4-stage pipeline for automatic C code reduction while preserving binary-identical assembly output.

---

## Overview

This project implements a specialized code reducer for the **IDO 5.3 compiler** (Nintendo 64 / SGI MIPS toolchain). Unlike general-purpose reducers such as C-Reduce, this pipeline uses the compiler itself as an oracle: every transformation is validated by comparing the resulting assembly hash against a baseline.

**Core idea:** *If the compiler produces identical MIPS output, the removed code was semantically irrelevant to that specific target platform.*

### Why IDO 5.3?

IDO 5.3 is a legacy compiler with specific optimization patterns, register allocation behavior, and MIPS-II code generation. General-purpose reducers do not understand these semantics — this pipeline does.

### Background

This project grew out of a practical problem: an earlier fine-tuning run produced a model that hallucinated more than it helped. On closer inspection, a significant portion of the supposedly verified training data contained dead and phantom code — variables declared but never used, branches that could never be reached, stores immediately overwritten. All technically valid C, all noise from the model's perspective.

CReduce exists for exactly this kind of cleanup, but it is built for modern compilers. At the scale needed and with IDO 5.3 as the target, it was too slow and too imprecise. This pipeline is the result of iterating on that problem.

---

## Architecture (4 Stages)

```
Stage 0: Raw C code (input)
    ↓
Stage 1: AST-level delta debugging ─────┐
    ↓                                   │
Stage 2: Semantic expression cleanup    │
    ↓                                   ├── Compiler oracle (IDO → objdump → hash)
Stage 3: Token-level reduction ─────────┤
    ↓                                   │
Stage 4: Clang-delta hybrid ────────────┘
    ↓
Output: Minimal, compilable C code with identical MIPS assembly
```

### Stage 1 – AST Dead-Code Removal
**`Stage_1_AST.py`**

- **Parser:** pycparser with gcc -E preprocessing
- **Strategy:** Systematic removal of AST nodes (statements, declarations, globals)
- **Multi-pass:** Iterates until no further changes are possible (phantom code elimination)
- **Safety:** Original files are never modified; results are written to `dataset_Stage_1/`

**Removes:**
- Unreferenced global variables and forward declarations
- Dead statements in function bodies (including nested blocks)
- Dead stores (variables that only reference themselves)
- Empty `while`/`if` blocks after inner reduction

### Stage 2 – Semantic Cleanup
**`Stage_2_Semantics.py`**

- **Focus:** Expression-level rather than statement-level
- **Rule-based:** 13 simplification rules, each validated against the compiler oracle

**Example transformations:**
| Pattern | Result | Rule |
|---------|--------|------|
| `x + 0`, `0 + x`, `x - 0` | `x` | `add_zero` |
| `x \| 0`, `x ^ 0` | `x` | `or_xor_zero` |
| `x * 1`, `x / 1` | `x` | `mul_div_one` |
| `((u32)((u32)x))` | `((u32)x)` | `double_cast` |
| `cond ? a : a` | `a` | `ternary_same` |
| `(void*)0 == (void*)0` | `1` | `null_eq_null` |

### Stage 3 – Token-Level Reduction
**`Stage_3_CRedPython.py`**

An optimized Python replacement for C-Reduce with platform-specific optimizations:

| Feature | Description |
|---------|-------------|
| **TCC fast-reject** | Syntax check before IDO is invoked (saves ~70% of compiler calls) |
| **Global compile cache** | MD5-based cache per file across all passes |
| **Syntax validator** | Heuristic check for bracket balance and string literals |
| **14 passes** | Line removal, balanced-pair elimination, peephole optimizations |

**Pass examples:**
- `blank` / `blank_final` – Whitespace normalization
- `lines` – Binary search for line-chunk removal
- `balanced_*` – Removal of redundant brackets and empty blocks
- `peep_subexpr` – Replace complex expressions with `0`/`1`
- `peep_args` – Function argument reduction
- `ternary_modus_b/c` – Ternary collapse

### Stage 4 – Clang-Delta Hybrid
**`Stage_4_RedClang.py`**

Hybrid approach: Python passes first (fast, TCC-guarded), then selective Clang-delta:

**Python passes (TCC-guarded):**
- `simplify-if` – `if(cond){a}else{b}` → `a` or `b`
- `remove-unused-var` – Dead store elimination
- `remove-unused-func` – Function definitions without external calls
- `simplify-comma` – `(a, b, c)` → `(b, c)` etc.
- `neutralize-calls` – `foo()` → `0` / `;` depending on context
- `return-void` – Return type relaxation for unused returns

**Clang-delta (only when needed):**
- `aggregate-to-scalar` – Simplify struct/array accesses

**Gatekeeper:** Automatically determines whether Clang-delta is needed at all, based on code analysis.

---

## Technical Highlights

### Compiler Oracle
```python
# Every transformation is validated by:
C source → gcc -E → IDO CC (-O2 -mips2 -G0) → MIPS object
→ objdump -d (.text) + objdump -s (.rodata/.data/.bss)
→ Normalization (stack offsets, symbol names) → MD5 hash
→ Comparison against baseline hash
```

### Normalization
Stack offsets and symbol addresses are abstracted so that functionally identical code with different stack layouts is recognized as equivalent:
```asm
addiu $sp, $sp, -48     →  addiu sp,sp,OFFSET
-16($sp)                →  OFFSET(sp)
%got(func)($gp)         →  SYMBOL
```

### Safety Mechanisms
- **Process-group kill:** Timeout terminates IDO process groups completely (prevents zombie processes)
- **Disk space guard:** Pauses when free disk space drops below 2 GB
- **RAM disk:** Temporary files written to `/dev/shm` (protects SSDs)
- **Recursion limit:** Set to 50,000 for deeply nested csmith/YARPGen outputs
- **Dry-run mode:** Analysis without modifying any files

---

## Installation

### Prerequisites

| Component | Purpose |
|-----------|---------|
| `Python 3.8+` | Runtime |
| `pycparser` | C AST parsing (Stage 1+2) |
| `gcc` | Preprocessor |
| `ido/cc` | IDO 5.3 compiler (MIPS) |
| `mips-linux-gnu-objdump` | Disassembly and data extraction |
| `tcc` *(optional)* | Fast-reject guard (Stage 3+4) |
| `clang_delta` *(optional)* | Aggregate-to-scalar (Stage 4) |

### Setup

```bash
# Python dependencies
pip install pycparser tqdm

# IDO 5.3 compiler (not publicly available — requires own installation)
# Expected at: /home/user/deadCodeRemover/CompilerRoot/tools/ido/

# MIPS objdump (Debian/Ubuntu)
sudo apt-get install binutils-mips-linux-gnu

# TCC (optional, for fast-reject)
sudo apt-get install tcc

# clang_delta (optional, for Stage 4)
# See: https://github.com/csmith-project/clang_delta
```

### Configuration

Paths must be adjusted to your local environment:

```python
# In all stage scripts:
BASE_DIR     = "/home/user/deadCodeRemover"           # Project root
PROJECT_ROOT = os.path.join(BASE_DIR, "CompilerRoot") # IDO & headers
IDO_DIR      = os.path.join(PROJECT_ROOT, "tools", "ido")
```

**Key adjustments:**
1. `BASE_DIR` – Your working directory
2. `IDO_DIR` – Path to the IDO toolchain
3. `INCLUDE_DIRS` – Project-specific header paths
4. `GROUPS` – Your dataset groups (e.g. `["Save_00_generated"]`)
5. `CLANG_DELTA` – Path to the binary (Stage 4)

---

## Usage

### Diagnose a single file
```bash
# Stage 1: AST analysis
python Stage_1_AST.py --diagnose /path/to/input.c

# Stage 2: Semantic checks
python Stage_2_Semantics.py --diagnose /path/to/input.c

# Stage 3: Token reduction (verbose)
python Stage_3_CRedPython.py --diagnose /path/to/input.c

# Stage 4: Clang hybrid (verbose)
python Stage_4_RedClang.py --diagnose /path/to/input.c
```

### Batch processing
```bash
# Stage 1 (parallel, 8 workers)
python Stage_1_AST.py -j 8 --group Input_Group

# Stage 2 (input = output of Stage 1)
python Stage_2_Semantics.py -j 8

# Stage 3
python Stage_3_CRedPython.py -j 4  # CPU/2 recommended

# Stage 4
python Stage_4_RedClang.py -j 4
```

### Full pipeline
```bash
python Stage_1_AST.py -j $(nproc) && \
python Stage_2_Semantics.py -j $(nproc) && \
python Stage_3_CRedPython.py -j $(($(nproc)/2)) && \
python Stage_4_RedClang.py -j $(($(nproc)/2))
```

---

## Project Structure

```
deadCodeRemover/
├── CompilerRoot/               # IDO 5.3 toolchain & project headers
│   ├── tools/ido/
│   ├── include/
│   └── src/
├── dataset_Stage_0/            # Raw input
│   ├── Input_Group/
│   └── Input_Group_headers/
├── dataset_Stage_1/            # AST-optimized
├── dataset_Stage_2/            # Semantically cleaned
├── dataset_Stage_3/            # Token-reduced
├── dataset_Stage_4/            # Final (Clang hybrid)
├── Stage_1_AST.py              # AST delta debugging
├── Stage_2_Semantics.py        # Expression cleaner
├── Stage_3_CRedPython.py       # Token reducer (TCC-optimized)
├── Stage_4_RedClang.py         # Clang-delta hybrid
└── README.md
```

---

## Performance

| Stage | Average | Bottleneck | Optimization |
|-------|---------|------------|--------------|
| 1 | ~2–5s/file | IDO compile | ASM hash cache, parallelization |
| 2 | ~3–8s/file | Expression validation | TCC guard (when available) |
| 3 | ~5–15s/file | Token pass iterations | Global cache, syntax validator |
| 4 | ~3–10s/file | Clang-delta (rare) | Gatekeeper, Python-first |

**Typical reduction rates:**
- Stage 1: 15–40% line reduction (dead code)
- Stage 2: 5–15% line reduction (semantic noise)
- Stage 3: 20–50% line reduction (token level)
- Stage 4: 5–20% line reduction (structure and aggregates)

---

## Known Limitations

- **IDO-specific:** Not directly portable to other compilers (GCC/Clang use different optimization patterns)
- **pycparser limits:** No full C99 support (variable-length arrays, complex initializers)
- **TCC divergence:** TCC tolerates some constructs IDO strictly rejects, and vice versa — IDO remains the gold standard
- **RecursionError:** Extremely deep ASTs from csmith/YARPGen can exceed Python's recursion limit

---

## License & Context

This project was built within the context of the **Nintendo 64 decompilation scene**, targeting the late-90s SGI/MIPS toolchain. The IDO 5.3 compiler is proprietary software from Silicon Graphics/Nintendo and is not included in this repository.

This pipeline was developed to generate clean training data for LLM fine-tuning on MIPS-to-C decompilation. It is part of an ongoing research effort — the current approach works, but there is still plenty to improve. Feedback and contributions are welcome.
