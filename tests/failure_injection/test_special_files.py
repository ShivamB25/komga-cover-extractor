import os
import subprocess
import sys
from pathlib import Path


def test_fifo_observation_is_rejected_without_waiting_for_writer(tmp_path: Path) -> None:
    fifo = tmp_path / 'book.cbz'
    os.mkfifo(fifo)
    script = '''import sys
from pathlib import Path
from komga_cover_extractor.infrastructure.filesystem import FilesystemError,fingerprint
try:
    fingerprint(Path(sys.argv[1]),Path(sys.argv[2]))
except FilesystemError as error:
    if error.reason != 'not_regular_file': raise
    print('rejected')
else:
    raise AssertionError('FIFO accepted')
'''
    result = subprocess.run([sys.executable, '-I', '-c', script, str(fifo), str(tmp_path)], capture_output=True, text=True, timeout=2, check=True)
    assert result.stdout == 'rejected\n'
    assert fifo.is_fifo()
