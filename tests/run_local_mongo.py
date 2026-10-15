"""Run tests with the portable server downloaded into .test-mongo."""
from pathlib import Path
import argparse
import os
import subprocess
import sys
import time
import zipfile

from pymongo import MongoClient


root = Path(__file__).resolve().parents[1]
scratch = root / '.test-mongo'
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--mongo-version', choices=['7', '8'], default='8')
args = parser.parse_args()
version = args.mongo_version
port = 27029 if version == '7' else 27028
filename = 'mongodb7.zip' if version == '7' else 'mongodb.zip'
with zipfile.ZipFile(scratch / filename) as archive:
    names = [name for name in archive.namelist() if name.endswith('/bin/mongod.exe')]
    assert len(names) == 1
    executable = Path(archive.extract(names[0], scratch))
data = scratch / ('data7' if version == '7' else 'data')
data.mkdir(exist_ok=True)
process = subprocess.Popen([
    str(executable), '--dbpath', str(data), '--port', str(port),
    '--bind_ip', '127.0.0.1', '--logpath', str(scratch / f'mongod{version}.log'),
], creationflags=subprocess.CREATE_NO_WINDOW)
uri = f'mongodb://127.0.0.1:{port}'
connection = MongoClient(uri, serverSelectionTimeoutMS=500)
try:
    for _ in range(40):
        if process.poll() is not None:
            raise RuntimeError(f'MongoDB exited; inspect .test-mongo/mongod{version}.log')
        try:
            connection.admin.command('ping')
            break
        except Exception:
            time.sleep(0.25)
    else:
        raise RuntimeError('MongoDB did not become ready')
    actual_version = connection.admin.command('buildInfo')['version']
    if not actual_version.startswith(version + '.'):
        raise RuntimeError(f'Expected MongoDB {version}, found {actual_version}')
    print(f'Running tests against MongoDB {actual_version} on port {port}', flush=True)
    result = subprocess.run(
        [sys.executable, '-m', 'pytest', '-q', 'tests'],
        cwd=root,
        env={**os.environ, 'TEST_MONGO_URI': uri},
    )
finally:
    connection.close()
    process.terminate()
    process.wait(timeout=20)
sys.exit(result.returncode)
