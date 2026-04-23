#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gen_csmith_split2.py

Csmith-Code-Generator für IDO 5.3 / MIPS mit Header/C-Split.

Beschreibung:
  Generiert C-Code via Csmith, splittet Structs/Globals in Header-Dateien
  und kompiliert das Ergebnis mit dem IDO 5.3 Compiler zu MIPS-Assembly.

Features:
  - Multiprocessing mit isolierten Sandkasten pro Seed
  - Automatische Header/C-Trennung (Structs/Globals → .h, Logik → .c)
  - Csmith-Parameter-Tuning für MIPS-Kompatibilität
  - IDO-Umgebungsvariablen-Setup (COMPILER_PATH, LD_LIBRARY_PATH)

Abhängigkeiten:
  - Csmith (https://github.com/csmith-project/csmith)
  - gcc (Preprocessor)
  - IDO 5.3 Compiler (MIPS)

Hinweis:
  Pfade müssen an die lokale Umgebung angepasst werden (BASE_DIR, IDO_DIR, etc.)

Autor: Lukas Weller
"""
import os
import subprocess
import random
import re
import shutil
import multiprocessing
from tqdm import tqdm

# --- PFADE ---
BASE_DIR      = "/home/lukas/code_generator"
PROJECT_ROOT  = os.path.join(BASE_DIR, "IDO_compiler")
IDO_DIR       = os.path.abspath(os.path.join(PROJECT_ROOT, "tools", "ido"))
IDO_CC        = os.path.join(IDO_DIR, "cc")
CSMITH_BIN    = os.path.join(BASE_DIR, "csmith_install/bin/csmith")
CSMITH_INC    = os.path.join(BASE_DIR, "csmith_install/include/csmith-2.3.0") # WICHTIG!
OUTPUT_FOLDER = os.path.join(BASE_DIR, "n64_dataset")

# --- UNTERORDNER ---
C_FOLDER      = os.path.join(OUTPUT_FOLDER, "C")
ASM_FOLDER    = os.path.join(OUTPUT_FOLDER, "ASM")
HEADER_FOLDER = os.path.join(OUTPUT_FOLDER, "header")

INCLUDE_DIR_1 = os.path.join(PROJECT_ROOT, "include")
INCLUDE_DIR_2 = os.path.join(PROJECT_ROOT, "src")
INCLUDE_DIR_3 = os.path.join(PROJECT_ROOT, "include", "PR")
INCLUDE_DIR_4 = os.path.join(PROJECT_ROOT, "lib", "ultralib", "include")

os.makedirs(C_FOLDER, exist_ok=True)
os.makedirs(ASM_FOLDER, exist_ok=True)
os.makedirs(HEADER_FOLDER, exist_ok=True)

# --- SPLITTER-FUNKTION ---
def split_csmith_code(raw_csmith_code, base_filename):
    header_filename = f"{base_filename}.h"
    
    header_content = [
        f"#ifndef {base_filename.upper()}_H",
        f"#define {base_filename.upper()}_H",
        '#include <ultra64.h>',
        '#include "common.h"',
        ""
    ]
    
    c_content = [
        f'#include "{header_filename}"',
        ""
    ]
    
    struct_pattern = re.compile(r"((?:struct|union)\s+[SU]\d+\s*\{[^}]*\};)", re.MULTILINE | re.DOTALL)
    structs = struct_pattern.findall(raw_csmith_code)
    for s in structs:
        header_content.append(s)
        raw_csmith_code = raw_csmith_code.replace(s, "")
        
    global_var_pattern = re.compile(r"^([a-zA-Z0-9_]+\s*\*?\s*)(g_\d+)\s*=\s*(.*?;)", re.MULTILINE | re.DOTALL)
    
    for match in global_var_pattern.finditer(raw_csmith_code):
        var_type = match.group(1).strip()
        var_name = match.group(2).strip()
        var_init = match.group(3).strip()
        
        header_content.append(f"extern {var_type} {var_name};")
        c_content.append(f"{var_type} {var_name} = {var_init}")
        raw_csmith_code = raw_csmith_code.replace(match.group(0), "")

    c_content.append(raw_csmith_code.strip())
    
    header_content.append("")
    header_content.append("#endif")
    
    return "\n".join(header_content), "\n".join(c_content)

# --- WORKER-FUNKTION FUER EINEN KERN ---
def generate_single_sample(seed):
    name = f"csmith_sample_{seed}"
    c_p = os.path.join(C_FOLDER, f"{name}.c")
    h_p = os.path.join(HEADER_FOLDER, f"{name}.h")
    asm_p = os.path.join(ASM_FOLDER, f"{name}.h")

    # Wenn Datei schon existiert, ueberspringen
    if os.path.exists(c_p):
        return False

    # Einzigartiger Sandkasten fuer diesen Prozess
    tmp_dir = os.path.join(OUTPUT_FOLDER, f"tmp_{seed}")
    os.makedirs(tmp_dir, exist_ok=True)
    i_p = os.path.join(tmp_dir, f"{name}.i")

    success = False
    try:
        # 1. Csmith ausfuehren
        depth = str(random.randint(2, 4))
        complexity = str(random.randint(2, 5))
        blk_size = str(random.randint(3, 8))

        cmd = [CSMITH_BIN, "--seed", str(seed), "--max-funcs", "1", 
               "--max-block-depth", depth, "--max-expr-complexity", complexity, "--max-block-size", blk_size,
               "--no-checksum", "--no-longlong", "--no-math64", "--no-safe-math", "--no-arrays"]
        
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            return False
            
        content = res.stdout
        content = re.sub(r'#include.*', '', content)
        content = re.sub(r'#define.*', '', content)
        content = re.sub(r'#pragma.*', '', content)
        content = re.sub(r'/\*.*?\*/', '', content, flags=re.DOTALL)
        content = re.sub(r'.*csmith_sink_.*;\n?', '', content)
        content = re.sub(r'.*__undefined.*;\n?', '', content)
        
        if "int main" in content:
            content = content.split("int main")[0]
            
        content = content.replace("uint32_t", "u32")
        content = content.replace("uint16_t", "u16")
        content = content.replace("uint8_t", "u8")
        content = content.replace("int32_t", "s32")
        content = content.replace("int16_t", "s16")
        content = content.replace("int8_t", "s8")
        content = content.replace("static ", "").replace("volatile ", "")
        raw_csmith = re.sub(r'\n\s*\n', '\n\n', content).strip()

        if not raw_csmith:
            return False

        header_code, c_code = split_csmith_code(raw_csmith, name)
        
        with open(h_p, "w") as f: f.write(header_code + "\n")
        with open(c_p, "w") as f: f.write(c_code + "\n")

        # 2. GCC Praeprozessor
        cmd_cpp = [
            "gcc", "-E", "-P", "-xc",
            "-D_LANGUAGE_C", "-D_MIPS_SZLONG=32",
            "-I", INCLUDE_DIR_1, 
            "-I", INCLUDE_DIR_2,
            "-I", INCLUDE_DIR_3,
            "-I", INCLUDE_DIR_4,
            "-I", HEADER_FOLDER,
            "-I", CSMITH_INC, # KORRIGIERT!
            c_p, "-o", i_p
        ]
        
        res_cpp = subprocess.run(cmd_cpp, capture_output=True, text=True)
        if res_cpp.returncode != 0:
            if os.path.exists(c_p): os.remove(c_p)
            if os.path.exists(h_p): os.remove(h_p)
            return False

        # 3. IDO Compiler im isolierten Sandkasten (cwd=tmp_dir)
        env = os.environ.copy()
        env["COMPILER_PATH"] = IDO_DIR
        env["LD_LIBRARY_PATH"] = f"{IDO_DIR}:{env.get('LD_LIBRARY_PATH', '')}"
        
        cmd_ido = [IDO_CC, "-S", "-O2", "-mips2", "-G", "0", "-w", i_p]
        # WICHTIG: Ausfuehrung im tmp_dir verhindert Dateikonflikte!
        res_ido = subprocess.run(cmd_ido, env=env, cwd=tmp_dir, capture_output=True, text=True)
        
        if res_ido.returncode == 0:
            generated_s = os.path.join(tmp_dir, f"{name}.s")
            if os.path.exists(generated_s):
                shutil.move(generated_s, asm_p)
                success = True

        if not success:
            if os.path.exists(c_p): os.remove(c_p)
            if os.path.exists(h_p): os.remove(h_p)

        return success

    finally:
        # Sandkasten immer aufraeumen, egal ob Erfolg oder Absturz
        if os.path.exists(tmp_dir):
            shutil.rmtree(tmp_dir)

# --- MAIN ---
def run_production(target_count=40000):
    success_count = 0
    attempts = 0
    
    # Ermittle die Anzahl der verfuegbaren CPU-Kerne
    cpu_cores = multiprocessing.cpu_count()
    print(f"Starte Multiprocessing mit {cpu_cores} Kernen...")

    pbar = tqdm(total=target_count, desc="Csmith MIPS Generierung", unit="file")

    # Multiprocessing Pool oeffnen
    with multiprocessing.Pool(processes=cpu_cores) as pool:
        # Wir fuettern die Kerne in kleinen Paketen, bis das Ziel erreicht ist
        while success_count < target_count:
            # Batch-Groesse: Genug Seeds, damit die Kerne beschaeftigt bleiben
            batch_size = cpu_cores * 50
            seeds = [random.randint(0, 0xFFFFFFFF) for _ in range(batch_size)]
            attempts += batch_size
            
            # imap_unordered verarbeitet die Ergebnisse sofort, sobald ein Kern fertig ist
            for result in pool.imap_unordered(generate_single_sample, seeds):
                if result:
                    success_count += 1
                    pbar.update(1)
                    if success_count >= target_count:
                        break

    pbar.close()
    print(f"\n>>> ZUSAMMENFASSUNG CSMITH: {target_count} Dateien erfolgreich generiert! (Benoetigte Versuche: {attempts}) <<<")

if __name__ == "__main__":
    run_production(40000)