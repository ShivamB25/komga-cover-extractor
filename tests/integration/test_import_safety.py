import subprocess
import sys
from pathlib import Path

import komga_cover_extractor


def test_all_package_modules_import_without_configured_effects(tmp_path: Path) -> None:
    package = Path(komga_cover_extractor.__file__).parent
    modules = []
    for path in sorted(package.rglob('*.py')):
        relative = path.relative_to(package).with_suffix('')
        parts = relative.parts[:-1] if relative.name == '__init__' else relative.parts
        modules.append('.'.join(('komga_cover_extractor', *parts)))
    script = '''import argparse,importlib,sys,threading
from pathlib import Path
sys.dont_write_bytecode = True

def forbidden(*args,**kwargs):
    raise AssertionError('import initiated a configured effect')

def audit(event,args):
    if event in {'socket.connect','socket.bind','subprocess.Popen','os.mkdir','os.remove','os.rename','os.chdir'}:
        forbidden()
sys.addaudithook(audit)
threading.Thread.start=forbidden
argparse.ArgumentParser.parse_args=forbidden
for name in sys.argv[1:]:
    importlib.import_module(name)
'''
    process = subprocess.run([sys.executable, '-I', '-c', script, *modules], cwd=tmp_path, env={'HOME': str(tmp_path)}, text=True, capture_output=True, timeout=30, check=False)
    assert process.returncode == 0, process.stderr
    assert process.stdout == '' and process.stderr == ''
    assert not list(tmp_path.iterdir())
