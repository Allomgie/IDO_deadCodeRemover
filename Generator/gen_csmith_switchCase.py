#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gen_csmith_switchCase.py

Erweiterter Csmith-Generator mit AST-Mutation für IDO 5.3 / MIPS.

Beschreibung:
  Erweitert den Basis-Csmith-Generator durch pycparser-basierte AST-
  Transformationen: For-Loops werden in Do-While + Switch-Case-Konstrukte
  umgewandelt, um MIPS-spezifische Code-Muster zu generieren.

Features:
  - Alle Features von gen_csmith.py
  - AST-Mutation via pycparser (N64ASTMutator)
  - Switch-Case-Injection mit konfigurierbarer Wahrscheinlichkeit
  - Do-While-Transformation aus For-Loops

Abhängigkeiten:
  - Csmith
  - pycparser (AST-Parsing & -Transformation)
  - gcc, IDO 5.3 Compiler

Hinweis:
  Die AST-Mutation erfordert gültigen C-Code als Input. Csmith-Output wird
  vor der Mutation durch einen Typedef-Preamble ergänzt.

Autor: Lukas Weller
"""
import os
import subprocess
import random
import re
import shutil
import multiprocessing
from tqdm import tqdm
from pycparser import c_parser, c_generator, c_ast

# --- PFADE ---
BASE_DIR      = "/home/lukas/code_generator"
PROJECT_ROOT  = os.path.join(BASE_DIR, "IDO_compiler")
IDO_DIR       = os.path.abspath(os.path.join(PROJECT_ROOT, "tools", "ido"))
IDO_CC        = os.path.join(IDO_DIR, "cc")
CSMITH_BIN    = os.path.join(BASE_DIR, "csmith_install/bin/csmith")
CSMITH_INC    = os.path.join(BASE_DIR, "csmith_install/include/csmith-2.3.0") 
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

# --- AST MUTATOR ---
class N64ASTMutator(c_ast.NodeVisitor):
    def __init__(self, switch_prob=0.80, dowhile_prob=0.50):
        self.switch_prob = switch_prob
        self.dowhile_prob = dowhile_prob
        self.switch_counter = 0

    def visit_Compound(self, node):
        if not node.block_items:
            return

        new_items = []
        batch = []

        def flush_batch():
            if len(batch) >= 3 and random.random() < self.switch_prob:
                cases = []
                for i, stmt in enumerate(batch):
                    cases.append(c_ast.Case(c_ast.Constant('int', str(i)), [stmt, c_ast.Break()]))
                
                cases.append(c_ast.Default([c_ast.Break()]))
                self.switch_counter += 1
                
                switch_expr = c_ast.BinaryOp('%', c_ast.ID('rand_state'), c_ast.Constant('int', str(len(batch))))
                switch_node = c_ast.Switch(switch_expr, c_ast.Compound(cases))
                new_items.append(switch_node)
            else:
                new_items.extend(batch)
            batch.clear()

        for child in node.block_items:
            self.visit(child)
            mutated_child = child
            
            if isinstance(child, c_ast.For) and child.cond and random.random() < self.dowhile_prob:
                do_body_items = []
                if isinstance(child.stmt, c_ast.Compound) and child.stmt.block_items:
                    do_body_items.extend(child.stmt.block_items)
                elif child.stmt:
                    do_body_items.append(child.stmt)
                    
                if child.next:
                    do_body_items.append(child.next)
                    
                do_while_node = c_ast.DoWhile(child.cond, c_ast.Compound(do_body_items))
                mutated_child = c_ast.If(child.cond, c_ast.Compound([do_while_node]), None)
                
                if child.init:
                    flush_batch()
                    new_items.append(child.init)

            if isinstance(child, (c_ast.Decl, c_ast.Return)):
                flush_batch()
                new_items.append(mutated_child)
            else:
                batch.append(mutated_child)
                if len(batch) >= random.randint(3, 6):
                    flush_batch()
        
        flush_batch() 
        node.block_items = new_items

def apply_ast_mutations(c_code):
    fake_typedefs = """
    typedef unsigned char u8; typedef unsigned short u16; typedef unsigned int u32;
    typedef signed char s8; typedef short s16; typedef int s32;
    typedef unsigned long u64; typedef long s64;
    extern u32 rand_state; 
    """
    
    parser = c_parser.CParser()
    try:
        ast = parser.parse(fake_typedefs + c_code)
        mutator = N64ASTMutator(switch_prob=0.80, dowhile_prob=0.50)
        mutator.visit(ast)
        
        generator = c_generator.CGenerator()
        mutated_code = generator.visit(ast)
        
        lines = mutated_code.split('\n')
        clean_lines = [l for l in lines if not l.startswith('typedef')]
        return '\n'.join(clean_lines).strip()
    except Exception as e:
        return c_code

# --- SPLITTER-FUNKTION ---
def split_csmith_code(raw_csmith_code, base_filename):
    header_filename = f"{base_filename}.h"
    
    header_content = [
        f"#ifndef {base_filename.upper()}_H",
        f"#define {base_filename.upper()}_H",
        '#include <ultra64.h>',
        '#include "common.h"',
        "extern u32 rand_state;", 
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

# --- GENERATOR-KLASSE ---
class MipsPatternGenerator:
    def __init__(self, csmith_path):
        self.csmith_path = csmith_path

    def get_stripped_csmith(self, seed):
        depth = str(random.randint(2, 4))
        complexity = str(random.randint(2, 5))
        blk_size = str(random.randint(3, 8))

        cmd = [self.csmith_path, "--seed", str(seed), "--max-funcs", "1", 
               "--max-block-depth", depth, "--max-expr-complexity", complexity, "--max-block-size", blk_size,
               "--no-checksum", "--no-longlong", "--no-math64", "--no-safe-math", "--no-arrays"]
        
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0: return ""
        
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
        content = re.sub(r'\n\s*\n', '\n\n', content)
        
        return content.strip()

# --- WORKER-FUNKTION FUER EINEN KERN ---
def generate_single_sample(seed):
    name = f"csmith_sample_{seed}"
    c_p = os.path.join(C_FOLDER, f"{name}.c")
    h_p = os.path.join(HEADER_FOLDER, f"{name}.h")
    asm_p = os.path.join(ASM_FOLDER, f"{name}.h")

    if os.path.exists(c_p):
        return False

    # Einzigartiger Sandkasten fuer diesen Prozess
    tmp_dir = os.path.join(OUTPUT_FOLDER, f"tmp_{seed}")
    os.makedirs(tmp_dir, exist_ok=True)
    i_p = os.path.join(tmp_dir, f"{name}.i")

    success = False
    try:
        gen = MipsPatternGenerator(CSMITH_BIN)
        raw_csmith = gen.get_stripped_csmith(seed)
        
        if not raw_csmith:
            return False
            
        # --- HIER PASSIERT DIE MAGIE: AST MUTATION ---
        raw_csmith = apply_ast_mutations(raw_csmith)
            
        header_code, c_code = split_csmith_code(raw_csmith, name)
        
        with open(h_p, "w") as f: f.write(header_code + "\n")
        with open(c_p, "w") as f: f.write(c_code + "\n")

        # --- STUFE 1: GCC Praeprozessor ---
        cmd_cpp = [
            "gcc", "-E", "-P", "-xc",
            "-D_LANGUAGE_C", "-D_MIPS_SZLONG=32",
            "-I", INCLUDE_DIR_1, 
            "-I", INCLUDE_DIR_2,
            "-I", INCLUDE_DIR_3,
            "-I", INCLUDE_DIR_4,
            "-I", HEADER_FOLDER,
            "-I", CSMITH_INC,
            c_p, "-o", i_p
        ]
        
        res_cpp = subprocess.run(cmd_cpp, capture_output=True, text=True)
        if res_cpp.returncode != 0:
            if os.path.exists(c_p): os.remove(c_p)
            if os.path.exists(h_p): os.remove(h_p)
            return False

        # --- STUFE 2: IDO Compiler (Im isolierten Sandkasten) ---
        env = os.environ.copy()
        env["COMPILER_PATH"] = IDO_DIR
        env["LD_LIBRARY_PATH"] = f"{IDO_DIR}:{env.get('LD_LIBRARY_PATH', '')}"
        
        cmd_ido = [IDO_CC, "-S", "-O2", "-mips2", "-G", "0", "-w", i_p]
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
        # Sandkasten immer sicher aufraeumen
        if os.path.exists(tmp_dir):
            shutil.rmtree(tmp_dir)

# --- MAIN ---
def run_production(target_count=60000):
    success_count = 0
    attempts = 0
    
    cpu_cores = multiprocessing.cpu_count()
    print(f"Starte AST-Mutator Multiprocessing mit {cpu_cores} Kernen...")

    pbar = tqdm(total=target_count, desc="Switch-Case MIPS Generierung", unit="file")

    with multiprocessing.Pool(processes=cpu_cores) as pool:
        while success_count < target_count:
            batch_size = cpu_cores * 20 # Kleinere Batches, da pycparser etwas laenger braucht
            seeds = [random.randint(0, 0xFFFFFFFF) for _ in range(batch_size)]
            attempts += batch_size
            
            for result in pool.imap_unordered(generate_single_sample, seeds):
                if result:
                    success_count += 1
                    pbar.update(1)
                    if success_count >= target_count:
                        break

    pbar.close()
    print(f"\n>>> ZUSAMMENFASSUNG AST-MUTATOR: {target_count} Dateien erfolgreich generiert! (Benoetigte Versuche: {attempts}) <<<")

if __name__ == "__main__":
    run_production(60000)