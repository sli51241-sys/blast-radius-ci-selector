"""Fetch only the pinned upstream SQLGlot repository during image construction."""
import io
from pathlib import Path
import tarfile
import urllib.request

COMMIT = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
url = f"https://github.com/tobymao/sqlglot/archive/{COMMIT}.tar.gz"
archive = urllib.request.urlopen(url, timeout=90).read()
with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as package:
    package.extractall("/opt", filter="data")
Path(f"/opt/sqlglot-{COMMIT}").rename("/opt/sqlglot")
assert Path("/opt/sqlglot/sqlglot/parser.py").is_file(), "incomplete pinned source"
print(f"Fetched SQLGlot {COMMIT}")
