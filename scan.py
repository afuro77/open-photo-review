import subprocess
import sys
from pathlib import Path


def main():
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python scan.py PHOTO_FOLDER")
    folder = Path(sys.argv[1]).expanduser().resolve()
    if not folder.is_dir():
        raise SystemExit(f"Folder not found: {folder}")
    scripts = Path(__file__).resolve().parent
    for phase in ("scan_exact.py", "scan_size_variants.py", "scan_similar.py"):
        subprocess.run([sys.executable, str(scripts / phase), str(folder)], check=True)


if __name__ == "__main__":
    main()
