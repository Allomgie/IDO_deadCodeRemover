# IDO-5.3 Dead-Code Reducer Pipeline

> **Compiler-gesteuertes Delta-Debugging für den IDO 5.3 MIPS-Compiler**  
> Eine 4-Stage Pipeline zur automatischen Reduktion von C-Code unter Beibehaltung binäridentischer Assembly-Ausgabe.

---

## Überblick

Dieses Projekt implementiert einen spezialisierten Code-Reducer für den **IDO 5.3 Compiler** (Nintendo 64 / SGI MIPS toolchain). Im Gegensatz zu allgemeinen Reducern wie C-Reduce nutzt diese Pipeline den Compiler selbst als Orakel: Jede Transformation wird validiert, indem der resultierende Assembly-Hash mit der Baseline verglichen wird.

**Kernidee:** *Wenn der Compiler den gleichen MIPS-Code erzeugt, war der entfernte Code semantisch irrelevant für die Zielplattform.*

### Warum IDO 5.3?

IDO 5.3 ist ein legacy Compiler mit spezifischen Optimierungsmustern, Register-Allokation und MIPS-II Code-Generierung. Allgemeine Reducer verstehen diese Semantik nicht – diese Pipeline schon.

---

## Architektur (4 Stages)

```
Stage 0: Roher C-Code (Input)
    ↓
Stage 1: AST-Level Delta Debugging ─────┐
    ↓                                   │
Stage 2: Semantische Expression-Clean   │
    ↓                                   ├── Compiler-Orakel (IDO → objdump → Hash)
Stage 3: Token-Level Reduktion ─────────┤
    ↓                                   │
Stage 4: Clang-Delta Hybrid ────────────┘
    ↓
Output: Minimaler, compilierbarer C-Code mit identischem MIPS-Assembly
```

### Stage 1 – AST Dead-Code Removal
**`Stage_1_AST.py`**

- **Parser:** pycparser mit gcc-E Preprocessing
- **Strategie:** Systematisches Entfernen von AST-Knoten (Statements, Deklarationen, Globals)
- **Multi-Pass:** Iteriert bis keine Änderungen mehr möglich (Phantom-Code-Elimination)
- **Sicherheit:** Originaldateien werden nie verändert; Ergebnisse landen in `dataset_Stage_1/`

**Entfernt:**
- Unreferenzierte globale Variablen & Forward-Deklarationen
- Tote Statements in Funktionskörpern (inkl. verschachtelte Blöcke)
- Dead Stores (Variablen, die nur sich selbst referenzieren)
- Leere `while`-/`if`-Blöcke nach innerer Reduktion

### Stage 2 – Semantische Bereinigung
**`Stage_2_Semantics.py`**

- **Fokus:** Expression-Level statt Statement-Level
- **Regel-basiert:** 13 Vereinfachungsregeln mit Delta-Debugging-Validierung

**Beispiel-Transformationen:**
| Pattern | Ergebnis | Regel |
|---------|----------|-------|
| `x + 0`, `0 + x`, `x - 0` | `x` | `add_zero` |
| `x \| 0`, `x ^ 0` | `x` | `or_xor_zero` |
| `x * 1`, `x / 1` | `x` | `mul_div_one` |
| `((u32)((u32)x))` | `((u32)x)` | `double_cast` |
| `cond ? a : a` | `a` | `ternary_same` |
| `(void*)0 == (void*)0` | `1` | `null_eq_null` |

### Stage 3 – Token-Level Reduktion
**`Stage_3_CRedPython.py`**

Ein optimierter Python-Ersatz für C-Reduce mit plattformspezifischen Optimierungen:

| Feature | Beschreibung |
|---------|--------------|
| **TCC Fast-Reject** | Syntax-Check vor IDO-Aufruf (spart ~70% Compiler-Calls) |
| **Globaler Compile-Cache** | MD5-basierte Cache pro Datei über alle Passes |
| **Syntax-Validator** | Heuristische Prüfung auf Klammer-Balance, String-Literals |
| **14 Passes** | Zeilen-Entfernung, Balanced-Pair-Elimination, Peephole-Optimierungen |

**Pass-Beispiele:**
- `blank` / `blank_final` – Whitespace-Normalisierung
- `lines` – Binäre Suche für Zeilen-Chunk-Entfernung
- `balanced_*` – Entfernen redundanter Klammern, leerer Blöcke
- `peep_subexpr` – Ersetzen komplexer Ausdrücke durch `0`/`1`
- `peep_args` – Funktionsargument-Reduktion
- `ternary_modus_b/c` – Ternary-Kollaps

### Stage 4 – Clang-Delta Hybrid
**`Stage_4_RedClang.py`**

Hybrider Ansatz: Python-Passes zuerst (schnell, TCC-geschützt), dann selektiver Clang-Delta-Einsatz:

**Python-Passes (TCC-geschützt):**
- `simplify-if` – `if(cond){a}else{b}` → `a` oder `b`
- `remove-unused-var` – Dead-Store-Elimination
- `remove-unused-func` – Funktionsdefinitionen ohne externe Calls
- `simplify-comma` – `(a, b, c)` → `(b, c)` etc.
- `neutralize-calls` – `foo()` → `0` / `;` je nach Kontext
- `return-void` – Rückgabetyp-Relaxation bei ungenutzten Returns

**Clang-Delta (nur wenn nötig):**
- `aggregate-to-scalar` – Struct/Array-Zugriffe vereinfachen

**Gatekeeper:** Automatische Entscheidung, ob Clang-Delta überhaupt benötigt wird (basierend auf Code-Analyse).

---

## Technische Highlights

### Compiler-Orakel
```python
# Jede Transformation wird validiert durch:
C-Source → gcc -E → IDO CC (-O2 -mips2 -G0) → MIPS-Object
→ objdump -d (.text) + objdump -s (.rodata/.data/.bss)
→ Normalisierung (Stack-Offsets, Symbol-Namen) → MD5-Hash
→ Vergleich mit Baseline-Hash
```

### Normalisierung
Stack-Offsets und Symbol-Adressen werden abstrahiert, um funktional identischen Code mit verschiedenen Stack-Layouts als gleich zu erkennen:
```asm
addiu $sp, $sp, -48     →  addiu sp,sp,OFFSET
-16($sp)                →  OFFSET(sp)
%got(func)($gp)         →  SYMBOL
```

### Sicherheitsmechanismen
- **Process-Group-Kill:** Timeout beendet IDO-Prozessgruppen komplett (verhindert Zombie-Prozesse)
- **Disk-Space-Guard:** Pausiert bei <2GB freiem Speicher
- **RAM-Disk:** Temporäre Dateien in `/dev/shm` (SSD-Schonung)
- **Recursion-Limit:** 50.000 für tief verschachtelte csmith/YARPGen-Outputs
- **Dry-Run-Modus:** Analyse ohne Dateiänderungen

---

## Installation

### Voraussetzungen

| Komponente | Zweck |
|------------|-------|
| `Python 3.8+` | Runtime |
| `pycparser` | C-AST Parsing (Stage 1+2) |
| `gcc` | Preprocessor |
| `ido/cc` | IDO 5.3 Compiler (MIPS) |
| `mips-linux-gnu-objdump` | Disassembly & Daten-Extraktion |
| `tcc` *(optional)* | Fast-Reject Guard (Stage 3+4) |
| `clang_delta` *(optional)* | Aggregate-to-Scalar (Stage 4) |

### Setup

```bash
# Python-Abhängigkeiten
pip install pycparser tqdm

# IDO 5.3 Compiler (nicht öffentlich verfügbar – eigene Installation nötig)
# Erwartet unter: /home/user/deadCodeRemover/CompilerRoot/tools/ido/

# MIPS objdump (Debian/Ubuntu)
sudo apt-get install binutils-mips-linux-gnu

# TCC (optional, für Fast-Reject)
sudo apt-get install tcc

# clang_delta (optional, für Stage 4)
# Siehe: https://github.com/csmith-project/clang_delta
```

### Konfiguration

Pfade müssen an deine Umgebung angepasst werden:

```python
# In allen Stage-Scripts:
BASE_DIR     = "/home/user/deadCodeRemover"          # Projekt-Root
PROJECT_ROOT = os.path.join(BASE_DIR, "CompilerRoot") # IDO & Headers
IDO_DIR      = os.path.join(PROJECT_ROOT, "tools", "ido")
```

**Wichtige Anpassungen:**
1. `BASE_DIR` – Dein Arbeitsverzeichnis
2. `IDO_DIR` – Pfad zur IDO-Toolchain
3. `INCLUDE_DIRS` – Projekt-spezifische Header-Pfade
4. `GROUPS` – Deine Dataset-Gruppen (z.B. `["Save_00_generated"]`)
5. `CLANG_DELTA` – Pfad zur Binary (Stage 4)

---

## Verwendung

### Einzelne Datei diagnostizieren
```bash
# Stage 1: AST-Analyse
python Stage_1_AST.py --diagnose /pfad/zu/input.c

# Stage 2: Semantische Checks
python Stage_2_Semantics.py --diagnose /pfad/zu/input.c

# Stage 3: Token-Reduktion (verbose)
python Stage_3_CRedPython.py --diagnose /pfad/zu/input.c

# Stage 4: Clang-Hybrid (verbose)
python Stage_4_RedClang.py --diagnose /pfad/zu/input.c
```

### Batch-Verarbeitung
```bash
# Stage 1 (parallel, 8 Worker)
python Stage_1_AST.py -j 8 --group Input_Group

# Stage 2 (Input = Output von Stage 1)
python Stage_2_Semantics.py -j 8

# Stage 3
python Stage_3_CRedPython.py -j 4  # CPU/2 empfohlen

# Stage 4
python Stage_4_RedClang.py -j 4
```

### Pipeline-Kette
```bash
# Komplette Pipeline in einem Rutsch
python Stage_1_AST.py -j $(nproc) && \
python Stage_2_Semantics.py -j $(nproc) && \
python Stage_3_CRedPython.py -j $(($(nproc)/2)) && \
python Stage_4_RedClang.py -j $(($(nproc)/2))
```

---

## Projektstruktur

```
deadCodeRemover/
├── CompilerRoot/               # IDO 5.3 Toolchain & Projekt-Headers
│   ├── tools/ido/
│   ├── include/
│   └── src/
├── dataset_Stage_0/            # Roh-Input
│   ├── Input_Group/
│   └── Input_Group_headers/
├── dataset_Stage_1/            # AST-optimiert
├── dataset_Stage_2/            # Semantisch bereinigt
├── dataset_Stage_3/            # Token-reduziert
├── dataset_Stage_4/            # Final (Clang-Hybrid)
├── Stage_1_AST.py              # AST Delta Debugging
├── Stage_2_Semantics.py        # Expression-Cleaner
├── Stage_3_CRedPython.py       # Token-Reducer (TCC-optimiert)
├── Stage_4_RedClang.py         # Clang-Delta Hybrid
└── README.md                   # Diese Datei
```

---

## Performance-Charakteristiken

| Stage | Durchschnitt | Bottleneck | Optimierung |
|-------|-------------|------------|-------------|
| 1 | ~2-5s/Datei | IDO Compile | ASM-Hash-Cache, Parallelisierung |
| 2 | ~3-8s/Datei | Expression-Validierung | TCC-Guard (wenn verfügbar) |
| 3 | ~5-15s/Datei | Token-Pass-Iterationen | Globaler Cache, Syntax-Validator |
| 4 | ~3-10s/Datei | Clang-Delta (selten) | Gatekeeper, Python-First |

**Typische Reduktionsraten:**
- Stage 1: 15-40% Zeilen (Dead Code)
- Stage 2: 5-15% Zeilen (semantische Noise)
- Stage 3: 20-50% Zeilen (Token-Ebene)
- Stage 4: 5-20% Zeilen (Struktur + Aggregate)

---

## Grenzen & Bekannte Einschränkungen

- **IDO-spezifisch:** Nicht direkt auf andere Compiler übertragbar (GCC/Clang verwenden andere Optimierungsmuster)
- **pycparser-Grenzen:** Keine vollständige C99-Unterstützung (Variable-Length Arrays, komplexe Initialisierer)
- **TCC-Abweichungen:** TCC toleriert manche Konstrukte, die IDO strikt ablehnt (und umgekehrt) – daher IDO als Gold-Standard
- **RecursionError:** Extrem tiefe ASTs (csmith-Generatoren) können das Python-Recursion-Limit sprengen

---

## Lizenz & Herkunft

Dieses Projekt entstand im Kontext der **Nintendo 64 Decompilation Scene** und ist spezialisiert auf die Werkzeugkette der late-90er SGI/MIPS-Entwicklung. Der IDO 5.3 Compiler ist proprietäre Software von Silicon Graphics / Nintendo und nicht in diesem Repository enthalten.

---

## Verwendung

Dieses Repository dient als **technisches Portfolio** und demonstriert:
- Compiler-Instrumentierung & Binary-Analyse
- Delta-Debugging-Algorithmen
- AST-Manipulation & Code-Transformation
- Parallele Verarbeitung & Caching-Strategien
- Legacy-Toolchain-Integration

Dieses Projekt wurde verwendet, um einen Dataset zum KI-Fine Tuning zu bereinigen.


