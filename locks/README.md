# Historical environment snapshots

These files preserve the original exact package versions, byte for byte:

- `qc-windows-py311.txt`: former `requirements-lock.txt`, checkpoint 1 QC environment.
- `warehouse-windows-py311.txt`: former `requirements-warehouse-lock.txt`, combined warehouse environment.

They are historical Windows/Python 3.11 snapshots, not Linux container locks or
complete locks for the current app. Use a separate virtual environment when
reproducing one; do not combine both snapshots. For current setup, use the named
groups in the root `pyproject.toml`.

To recreate a historical warehouse environment from the repository root:

```powershell
python -m venv .venv-historical
.\.venv-historical\Scripts\python.exe -m pip install -r locks/warehouse-windows-py311.txt
```
