import ast
import os
from pathlib import Path

def get_function_defs(filepath):
    if not os.path.exists(filepath):
        return {}
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()
    tree = ast.parse(content)
    defs = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            defs[node.name] = [arg.arg for arg in node.args.args]
    return defs

def validate_main():
    errors = []
    project_root = Path.cwd()
    main_py_path = project_root / "main.py"

    with open(main_py_path, "r", encoding="utf-8") as f:
        content = f.read()
    
    tree = ast.parse(content)
    
    imports = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module_path = node.module.replace('.', '/') + '.py'
            for alias in node.names:
                imports[alias.name] = module_path

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func_name = ''
            if isinstance(node.func, ast.Name):
                func_name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                func_name = node.func.attr

            if func_name in imports:
                module_path = imports[func_name]
                defs = get_function_defs(module_path)
                
                if func_name not in defs:
                    errors.append(f"Function '{func_name}' called in main.py not found in '{module_path}'")
                    continue
                
                call_args_len = len(node.args) + len(node.keywords)
                def_args_len = len(defs[func_name])
                
                # Simple check for argument count. This doesn't handle default arguments well.
                if call_args_len != def_args_len:
                    errors.append(f"Argument count mismatch for '{func_name}' in main.py. Call has {call_args_len}, definition has {def_args_len}.")

    if errors:
        print("Function call validation failed with the following errors:")
        for error in errors:
            print(error)
    else:
        print("All function calls in main.py appear to be valid.")

if __name__ == "__main__":
    validate_main()