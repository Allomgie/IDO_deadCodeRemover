#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gen_YARPGen_split.py

YARPGen-Code-Generator für IDO 5.3 / MIPS mit Syntax-Firewall.

Beschreibung:
  Integriert YARPGen als zweite Fuzzing-Engine neben Csmith.
  YARPGen generiert komplexere C-Konstrukte (mehrere Funktionen,
  Pointer-Arithmetik), die für das Delta-Debugging interessanter sind.

Features:
  - YARPGen-Integration mit Seed-Steuerung
  - Syntax-Firewall: Blockt PC/Linux-spezifische Calls (SDL_, posix, etc.)
  - Syntax-Schutz: Verhindert kaputte Return-Statements
  - Dual-Output-Handling: init.h (Globals) + func.c (Logik)
  - IDO-Kompatibilitäts-Layer (Typ-Ersetzungen, Attribute-Entfernung)

Abhängigkeiten:
  - YARPGen (gepatchte Version für IDO 5.3 / MIPS)
    Upstream: https://github.com/intel/yarpgen
    Hinweis: Erfordert Anpassungen für Legacy-Toolchains
  - gcc, IDO 5.3 Compiler

Hinweis:
  Das gepatchte YARPGen-Binary ist nicht im Repository enthalten, da es
  proprietäre Anpassungen an die IDO-Toolchain erfordert. Der Code zeigt
  die Integrationsarchitektur.

Autor: lukas Weller
"""
import os
import subprocess
import random
import re
import shutil
from tqdm import tqdm

# --- PFADE ---
BASE_DIR      = "/home/lukas/code_generator"
PROJECT_ROOT  = os.path.join(BASE_DIR, "IDO_compiler")
IDO_DIR       = os.path.abspath(os.path.join(PROJECT_ROOT, "tools", "ido"))
IDO_CC        = os.path.join(IDO_DIR, "cc")
YARPGEN_BIN   = "/home/lukas/code_generator/yarpgen/build/yarpgen"
OUTPUT_FOLDER = os.path.join(BASE_DIR, "n64_dataset")

C_FOLDER      = os.path.join(OUTPUT_FOLDER, "C")
HEADER_FOLDER = os.path.join(OUTPUT_FOLDER, "header")
ASM_FOLDER    = os.path.join(OUTPUT_FOLDER, "ASM")

os.makedirs(C_FOLDER, exist_ok=True)
os.makedirs(HEADER_FOLDER, exist_ok=True)
os.makedirs(ASM_FOLDER, exist_ok=True)

class MipsPatternGenerator:
    def __init__(self, bin_path):
        self.bin_path = bin_path

    def sanitize_basic(self, content):
        # [Deine bisherigen Ersetzungen bleiben hier...]
        content = content.replace("_Bool", "int")
        content = content.replace("long long", "long")
        content = content.replace("ULL", "U").replace("LL", "L")
        content = re.sub(r'\d{11,}L?', '0x7FFFFFFF', content)
        
        content = re.sub(r'__attribute__\s*\(\(.*?\)\)', '', content)
        content = re.sub(r'#pragma.*', '', content)

        repl = {"uint32_t": "u32", "uint16_t": "u16", "uint8_t": "u8",
                "int32_t": "s32", "int16_t": "s16", "int8_t": "s8", "static ": ""}
        for k, v in repl.items(): content = content.replace(k, v)

        content = re.sub(r'#include.*', '', content)
        
        # --- NEU: DIE FIREWALL ---
        # 1. PC/Linux/Ghost-Call Schutz
        forbidden_patterns = [
            r'\bSDL_', r'\bPy', r'\blinux\b', r'\bposix\b', r'\bWEXITSTATUS\b', 
            r'\bsetpgid\b', r'\bsignal\(', r'\b_exit\b', r'\bgetpid\b'
        ]
        for pattern in forbidden_patterns:
            if re.search(pattern, content):
                return "" # Sofortiger Abbruch!
        
        # 2. Syntax-Schutz (Verhindert das Missing-Semicolon Problem)
        # Wenn wir "return" haben, muss die Zeile zwingend mit ; enden
        lines = content.split('\n')
        for line in lines:
            if "return" in line and not line.strip().endswith(";"):
                # Ausnahme: Funktionsdefinitionen, aber bei return sehr unwahrscheinlich
                if "{" not in line and "}" not in line:
                    return "" # Abbruch, kaputte Syntax!

        return content.strip()

    def get_code(self, seed, tmp_path):
        cmd = [self.bin_path, "-s", str(seed), "--std=c", "-o", tmp_path]
        res_yarp = subprocess.run(cmd, capture_output=True, text=True)
        
        if res_yarp.returncode != 0:
            return "", "" # Zwei leere Strings zurueckgeben
        
        init_path = os.path.join(tmp_path, "init.h")
        func_path = os.path.join(tmp_path, "func.c")
        
        if not os.path.exists(func_path) or not os.path.exists(init_path): 
            return "", ""

        with open(init_path, "r") as f:
            init_raw = f.read()

        with open(func_path, "r") as f:
            func_raw = f.read()

        # Beide separat bereinigen und zurueckgeben!
        return self.sanitize_basic(init_raw), self.sanitize_basic(func_raw)


def split_generated_code(init_code, func_code, base_filename):
    header_filename = f"{base_filename}.h"
    h_content = [
        f"#ifndef {base_filename.upper()}_H", f"#define {base_filename.upper()}_H",
        "typedef unsigned char u8; typedef unsigned short u16; typedef unsigned int u32;",
        "typedef signed char s8; typedef short s16; typedef int s32;",
        "typedef unsigned long u64; typedef long s64;",
        "",
        init_code, # Hier landen jetzt die extern Variablen!
        "\n#endif"
    ]
    c_content = [f'#include "{header_filename}"', "", func_code]
    return "\n".join(h_content), "\n".join(c_content)

def run_production(target_count=10):
    env = os.environ.copy()
    env["COMPILER_PATH"] = IDO_DIR
    env["LD_LIBRARY_PATH"] = f"{IDO_DIR}:{env.get('LD_LIBRARY_PATH', '')}"
    
    gen = MipsPatternGenerator(YARPGEN_BIN)
    success_count = 0
    attempts = 0

    # Pbar initialisieren
    pbar = tqdm(total=target_count, desc="MIPS Datensatz Generierung")

    while success_count < target_count:
        attempts += 1
        name = f"yarp_sample_{success_count + 1}"
        tmp_dir = os.path.join(BASE_DIR, f"tmp_gen_{attempts}")
        os.makedirs(tmp_dir, exist_ok=True)

        # Wir empfangen jetzt ZWEI Rueckgabewerte
        init_raw, func_raw = gen.get_code(random.randint(0, 0xFFFFFFFF), tmp_dir)
        
        if not func_raw:
            shutil.rmtree(tmp_dir)
            # tqdm.write(f"--- Versuch {attempts}: YARPGen ist intern abgestuerzt (Segfault). ---")
            continue

        # Und uebergeben auch beide an den Splitter
        h_code, c_code = split_generated_code(init_raw, func_raw, name)

        c_p = os.path.join(C_FOLDER, f"{name}.c")
        h_p = os.path.join(HEADER_FOLDER, f"{name}.h")
        with open(c_p, "w") as f: f.write(c_code)
        with open(h_p, "w") as f: f.write(h_code)

        # Stufe 1: GCC Preprocessor
        i_p = os.path.join(OUTPUT_FOLDER, f"{name}.i")
        res_gcc = subprocess.run(["gcc", "-E", "-P", "-xc", "-I", HEADER_FOLDER, c_p, "-o", i_p], capture_output=True, text=True)
        
        if res_gcc.returncode != 0 or not os.path.exists(i_p):
            tqdm.write(f"\n--- GCC FEHLER in Versuch {attempts} ---")
            # Wir zeigen nur die erste Zeile des GCC Fehlers
            tqdm.write(res_gcc.stderr.strip().split('\n')[0] if res_gcc.stderr else "Unbekannter GCC Fehler")
            
            if os.path.exists(c_p): os.remove(c_p)
            if os.path.exists(h_p): os.remove(h_p)
            shutil.rmtree(tmp_dir)
            continue

        # Stufe 2: IDO Compiler
        asm_p = os.path.join(ASM_FOLDER, f"{name}.h")
        
        # Wir lassen -o weg, da IDO es bei -S ohnehin ignoriert!
        cmd_ido = [IDO_CC, "-S", "-O2", "-mips2", "-G", "0", i_p]
        res_ido = subprocess.run(cmd_ido, env=env, capture_output=True, text=True)

        if res_ido.returncode == 0:
            success_count += 1
            pbar.update(1)
            
            # IDO legt heimlich eine .s Datei an. 
            # Wir suchen sie im Root-Ordner und im Output-Ordner und verschieben sie.
            generated_s_cwd = f"{name}.s"
            generated_s_out = os.path.join(OUTPUT_FOLDER, f"{name}.s")
            
            if os.path.exists(generated_s_cwd):
                shutil.move(generated_s_cwd, asm_p)
            elif os.path.exists(generated_s_out):
                shutil.move(generated_s_out, asm_p)
                
            if os.path.exists(i_p): os.remove(i_p)
        else:
            tqdm.write(f"\n--- IDO FEHLER in Versuch {attempts} ---")
            error_lines = res_ido.stderr.strip().split('\n')
            short_error = "\n".join(error_lines[:4])
            tqdm.write(short_error)
            
            if os.path.exists(c_p): os.remove(c_p)
            if os.path.exists(h_p): os.remove(h_p)
            if os.path.exists(i_p): os.remove(i_p)
            
            # Falls bei einem Abbruch doch eine .s Datei entstand, loeschen wir sie
            failed_s = f"{name}.s"
            if os.path.exists(failed_s): os.remove(failed_s)

        shutil.rmtree(tmp_dir)

    pbar.close()
    print(f"\n>>> ZUSAMMENFASSUNG: {target_count} Dateien erfolgreich generiert! (Benoetigte Versuche insgesamt: {attempts}) <<<")

if __name__ == "__main__":
    # Testen wir es mit einer kleinen Zahl, damit das Terminal nicht explodiert
    run_production(60000)

