# C-Code Generatoren für IDO 5.3 / MIPS

Dieser Ordner enthält die Fuzzing-Engines, die den Input für die [Dead-Code Reducer Pipeline](../README.md) produzieren. Ziel ist die kontrollierte Generierung von C-Code, der garantiert mit dem IDO 5.3 Compiler (Nintendo 64 / SGI MIPS) zu validem MIPS-Assembly kompiliert.

---

## Übersicht

| Generator | Engine | Besonderheit | Output |
|-----------|--------|-------------|--------|
| `gen_csmith_split2.py` | Csmith 2.3.0 | Header/C-Split, Sandkasten-Isolation | `dataset/C/` + `dataset/header/` + `dataset/ASM/` |
| `gen_csmith_switchCase.py` | Csmith 2.3.0 + pycparser | AST-Mutation: `for`→`do-while`, Switch-Case-Injection | `dataset/C/` + `dataset/header/` + `dataset/ASM/` |
| `gen_YARPGen_split.py` | YARPGen (gepatcht) | Syntax-Firewall, Dual-Output (init.h + func.c) | `dataset/C/` + `dataset/header/` + `dataset/ASM/` |

---

## Gemeinsame Architektur

Alle drei Generatoren teilen sich ein gemeinsames Design:

1. **Generierung** – Csmith/YARPGen erzeugt rohen C-Code anhand eines Seeds
2. **Sanitizing** – Typ-Ersetzungen (`uint32_t` → `u32`), Entfernung von Keywords (`static`, `volatile`), Filterung nicht-MIPS-kompatibler Konstrukte
3. **Splitting** – Trennung in Header (Structs, externe Globals) und C-Datei (Implementation)
4. **Preprocessing** – `gcc -E` mit IDO-kompatiblen Flags (`-D_LANGUAGE_C`, `-D_MIPS_SZLONG=32`)
5. **Kompilierung** – IDO 5.3 (`cc -S -O2 -mips2 -G0`) erzeugt MIPS-Assembly
6. **Cleanup** – Isolierter Sandkasten (`tmp_<seed>/`) wird nach jedem Durchlauf zerstört

---

## Einzelne Generatoren

### `gen_csmith_split2.py` – Basis-Generator

Der Standard-Generator. Erzeugt einfache C-Funktionen mit kontrollierter Komplexität.

**Csmith-Parameter-Tuning für MIPS:**
```python
--max-funcs 1           # Nur eine Funktion pro File
--no-longlong           # Keine 64-Bit-Integer (IDO 5.3 Limitation)
--no-math64             # Keine 64-Bit-Arithmetik
--no-safe-math          # Erlaubt Overflow/Undefined Behavior
--no-arrays             # Keine Arrays (vereinfacht Splitting)
--max-block-depth 2-4   # Kontrollierte Verschachtelung
--max-expr-complexity 2-5
```

**Usage:**
```bash
python gen_csmith_split2.py
# Generiert 40.000 Samples automatisch (konfigurierbar in run_production())
```

---

### `gen_csmith_switchCase.py` – AST-Mutator

Erweitert den Basis-Generator durch **pycparser-basierte AST-Transformationen**. Nach der Csmith-Generierung wird der Code geparsed, mutiert und zurückgeneriert.

**Transformationen:**
- **For→Do-While:** `for(init; cond; next){ body }` wird zu:
  ```c
  init;
  if (cond) {
      do {
          body;
          next;
      } while (cond);
  }
  ```
- **Switch-Case-Injection:** Blöcke von 3–6 Statements werden zu Switch-Statements mit `rand_state % n` als Dispatcher

**Warum?** Diese Muster treten häufig in decompiliertem N64-Code auf. Der Reducer soll lernen, sie zu erkennen und zu vereinfachen.

**Usage:**
```bash
python gen_csmith_switchCase.py
# Generiert 60.000 Samples (langsamer als Basis wegen pycparser)
```

---

### `gen_YARPGen_split.py` – YARPGen-Integration

Integriert YARPGen als zweite Engine. YARPGen erzeugt komplexere Konstrukte (mehrere Funktionen, Pointer-Arithmetik, verschachtelte Structs), die Csmith nicht abdeckt.

**Syntax-Firewall:**
Da YARPGen für moderne x86-Compiler entwickelt wurde, filtert der Generator aktiv nicht-kompatible Konstrukte:

```python
# Blockierte Patterns (PC/Linux-spezifisch)
SDL_, Py, linux, posix, WEXITSTATUS, setpgid, signal, _exit, getpid

# Syntax-Schutz
return x  # muss mit ; enden, sonst Abbruch
```

**Dual-Output:**
YARPGen produziert zwei Dateien:
- `init.h` – Globale Variablen & Konstanten
- `func.c` – Funktionslogik

Der Generator splittet diese automatisch in unser Header/C-Schema.

> **Hinweis:** Erfordert ein gepatchtes YARPGen-Binary für IDO 5.3-Kompatibilität. Das Upstream-Binary (https://github.com/intel/yarpgen) generiert Konstrukte, die IDO 5.3 nicht verarbeiten kann (z.B. bestimmte Attribute, moderne C-Features). Der Code in dieser Datei zeigt die Integrationsarchitektur – das Binary selbst ist nicht im Repository enthalten.

**Usage:**
```bash
python gen_YARPGen_split.py
# Generiert 60.000 Samples (gepatchtes YARPGen-Binary nötig)
```

---

## Konfiguration

Pfade müssen an deine lokale Umgebung angepasst werden:

```python
# In allen drei Dateien:
BASE_DIR      = "/home/user/deadCodeRemover"
PROJECT_ROOT  = os.path.join(BASE_DIR, "IDO_compiler")  # <-- Anpassen!
IDO_DIR       = os.path.join(PROJECT_ROOT, "tools", "ido")
CSMITH_BIN    = os.path.join(BASE_DIR, "csmith_install/bin/csmith")
YARPGEN_BIN   = "/pfad/zu/yarpgen"  # <-- Anpassen!
```

**Wichtige Pfade:**
- `IDO_DIR` – Pfad zum IDO 5.3 Compiler (`cc`)
- `CSMITH_BIN` – Csmith-Executable
- `YARPGEN_BIN` – Gepatchtes YARPGen-Binary
- `INCLUDE_DIR_*` – Projekt-Header (ultralib, PR, etc.)

---

## Output-Struktur

```
n64_dataset/
├── C/               # Generierte .c-Dateien
│   ├── csmith_sample_12345.c
│   └── yarp_sample_678.c
├── header/          # Zugehörige .h-Dateien
│   ├── csmith_sample_12345.h
│   └── yarp_sample_678.h
└── ASM/             # Kompilierte MIPS-Assembly (.s)
    ├── csmith_sample_12345.h
    └── yarp_sample_678.h
```

---

## Performance

| Generator | Durchsatz | Limitierender Faktor |
|-----------|-----------|---------------------|
| `gen_csmith_split2.py` | ~500–1000 Samples/s | IDO-Kompilierung |
| `gen_csmith_switchCase.py` | ~200–400 Samples/s | pycparser AST-Mutation |
| `gen_YARPGen_split.py` | ~50–100 Samples/s | YARPGen-Start + Syntax-Check |

Alle Generatoren nutzen **Multiprocessing** (`multiprocessing.Pool`) mit `cpu_count()` Workern.

---

## Troubleshooting

**IDO findet Header nicht:**
- Prüfe `INCLUDE_DIR_1` bis `INCLUDE_DIR_4` und `CSMITH_INC`
- Header müssen via `-I` im gcc-Preprocessing sichtbar sein

**YARPGen segfaultet:**
- Normales Verhalten bei ~30% der Seeds. Der Generator fängt das ab und versucht den nächsten Seed.
- Falls >90% fehlschlagen: YARPGen-Binary ist nicht korrekt gepatcht.

**Csmith produziert leere Dateien:**
- Prüfe, ob `csmith` im PATH erreichbar ist
- `--max-funcs 1` kann bei bestimmten Seeds leere Outputs erzeugen – der Generator überspringt diese automatisch

---

## Lizenz

Siehe [../LICENSE](../LICENSE). Die Generatoren sind Teil des Dead-Code-Reducer-Projekts und stehen unter MIT-Lizenz.

**Hinweis zu Dritt-Tools:**
- Csmith steht unter BSD-Lizenz (https://github.com/csmith-project/csmith)
- YARPGen steht unter Apache 2.0 (https://github.com/intel/yarpgen)
- IDO 5.3 ist proprietäre Software von SGI/Nintendo
