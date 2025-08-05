import os
import ast
import sys
from pathlib import Path

def validate_python_files():
    project_root = Path.cwd()
    python_files = list(project_root.glob('**/*.py'))

    # Exclude files in addons and .venv from validation
    python_files = [f for f in python_files if 'addons' not in f.parts and '.venv' not in f.parts]
    
    errors = []
    all_modules = set()

    # First, build a set of all possible module paths
    for py_file in python_files:
        if py_file.name == '__init__.py':
            module_path = str(py_file.parent.relative_to(project_root)).replace(os.sep, '.')
            all_modules.add(module_path)
        else:
            module_path = str(py_file.relative_to(project_root)).replace('.py', '').replace(os.sep, '.')
            all_modules.add(module_path)

    # Now, validate each file
    for py_file in python_files:
        rel_path = py_file.relative_to(project_root)
        try:
            with open(py_file, 'r', encoding='utf-8') as f:
                content = f.read()
                tree = ast.parse(content, filename=str(py_file))

                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        for alias in node.names:
                            if not is_importable(alias.name, all_modules):
                                errors.append(f'Unresolved import in {rel_path}: import {alias.name}')
                    elif isinstance(node, ast.ImportFrom):
                        if node.module:
                            if not is_importable(node.module, all_modules, level=node.level, current_file=py_file):
                                errors.append(f'Unresolved import in {rel_path}: from {node.module} import ...')

        except SyntaxError as e:
            errors.append(f'Syntax error in {rel_path}: {e}')
        except Exception as e:
            errors.append(f'Error processing {rel_path}: {e}')

    if errors:
        print('Validation failed with the following errors:')
        for error in errors:
            print(error)
    else:
        print('All Python files passed syntax and basic import validation.')

def is_importable(module_name, all_modules, level=0, current_file=None):
    # Check standard libraries
    if module_name in sys.builtin_module_names or module_name in sys.modules:
        return True

    # Check third-party libraries (simple check)
    try:
        __import__(module_name)
        return True
    except ImportError:
        pass

    # Check relative imports
    if level > 0:
        base_path = current_file.parent
        for _ in range(level - 1):
            base_path = base_path.parent
        
        try:
            base_module = str(base_path.relative_to(Path.cwd())).replace(os.sep, '.')
            if base_module == '.':
                base_module = ''
        except ValueError:
            base_module = '' # current_file is outside of cwd

        if base_module:
            full_module_path = f'{base_module}.{module_name}'
        else:
            full_module_path = module_name
        
        if full_module_path in all_modules:
            return True

    # Check absolute imports within the project
    return module_name in all_modules

if __name__ == '__main__':
    validate_python_files()