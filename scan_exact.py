import csv
import hashlib
import os
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from photo_review import DATA, ROOT

root = ROOT
DATA.mkdir(parents=True, exist_ok=True)
exts = set('jpg jpeg heic heif png gif tif tiff bmp webp orf dng raw cr2 cr3 nef arw raf rw2 avif'.split())
sizes = defaultdict(list)
errors = []
companions = set()
def walk_error(e):
    errors.append((e.filename, str(e)))
for folder, _, names in os.walk(root, onerror=walk_error):
    for name in names:
        path = Path(folder, name)
        suffix = path.suffix.lower()[1:]
        if suffix in ('xmp', 'aae'):
            companions.add(str(path.with_suffix('')).casefold())
        if suffix in exts:
            try:
                sizes[path.stat().st_size].append(path)
            except OSError as e:
                errors.append((str(path), str(e)))

def digest(path, partial):
    try:
        with path.open('rb') as f:
            h = hashlib.sha256(f.read(4096)).digest() if partial else hashlib.file_digest(f, 'sha256').digest()
        return path, h, None
    except OSError as e:
        return path, None, str(e)

candidates = [(size, path) for size, paths in sizes.items() if len(paths) > 1 for path in paths]
print(f'photos={sum(map(len, sizes.values()))}, size candidates={len(candidates)}', flush=True)
def scan(items, partial, executor):
    groups = defaultdict(list)
    paths = (path for _, path in items)
    for index, ((size, _), (path, h, error)) in enumerate(zip(items, executor.map(lambda path: digest(path, partial), paths)), 1):
        if error:
            errors.append((str(path), error))
        else:
            groups[size, h].append(path)
        if index % 5000 == 0:
            print(f'partial={partial} {index}/{len(items)}', flush=True)
    return {key: paths for key, paths in groups.items() if len(paths) > 1}
with ThreadPoolExecutor(max_workers=12) as executor:
    matches = scan(candidates, True, executor)
    print(f'sample candidates={sum(map(len, matches.values()))}', flush=True)
    exact = scan([(key[0], path) for key, paths in matches.items() for path in paths], False, executor)

def has_sidecar(path):
    return str(path.with_suffix('')).casefold() in companions or str(path).casefold() in companions
def rank(path):
    rel = path.relative_to(root)
    return (not has_sidecar(path), len(rel.parts), str(rel).casefold())
total = amount = sidecars = 0
with open(DATA / "photo-duplicates.tmp", 'w', encoding='utf-8-sig', newline='') as f:
    writer = csv.writer(f)
    writer.writerow(['グループ', '写真フォルダからの相対パス', 'サイズ（バイト）', '関連するXMPまたはAAE', 'SHA-256'])
    for index, ((size, h), paths) in enumerate(sorted(exact.items(), key=lambda item: (-item[0][0], item[0][1])), 1):
        for pos, path in enumerate(sorted(paths, key=rank)):
            linked = has_sidecar(path)
            writer.writerow([index, str(path.relative_to(root)), size, 'あり' if linked else 'なし', h.hex()])
            if pos:
                total += 1
                amount += size
                sidecars += linked
(DATA / "photo-duplicates.tmp").replace(DATA / "photo-duplicates.csv")
(DATA / "photo-scan-errors.csv").unlink(missing_ok=True)
if errors:
    with open(DATA / "photo-scan-errors.csv", 'w', encoding='utf-8-sig', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['Path', 'Error'])
        writer.writerows(errors)
print(f'groups={len(exact)} duplicate_copies={total} bytes={amount} duplicate_sidecars={sidecars} errors={len(errors)}', flush=True)
