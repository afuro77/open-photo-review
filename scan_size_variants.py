import csv
import hashlib
import importlib.util
import os
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image
from pillow_heif import register_heif_opener

register_heif_opener()
import photo_review as review

root = review.ROOT
known = {}
for group in review.read_groups().values():
    known.update({p.relative: p.digest for p in group})
files = defaultdict(list)
for directory, _, names in os.walk(root):
    for name in names:
        path = Path(directory, name)
        if path.suffix.lower() in {'.jpg', '.jpeg', '.png', '.heic', '.heif', '.orf'}:
            try:
                files[path.stem.casefold()].append((path, path.stat().st_size))
            except OSError:
                continue

def analyze(entries):
    samples = {}
    clusters = []
    for path, size in entries:
        relative = str(path.relative_to(root))
        digest = known.get(relative)
        if digest and digest in samples:
            continue
        photo = review.Photo(relative, digest or '', False)
        try:
            image = review.preview(photo, (72, 72))
            aspect = image.width / image.height
            sample = np.asarray(image.resize((28, 28), Image.Resampling.BILINEAR), dtype=np.float32)
            if sample.std(axis=(0, 1)).mean() < 12:
                continue
        except Exception:
            continue
        placed = False
        for cluster in clusters:
            if all(abs(aspect / other_aspect - 1) <= .035 and
                   np.abs(sample - other).mean() <= 4 and
                   np.quantile(np.abs(sample - other), .95) <= 13
                   for _, _, _, other_aspect, other in cluster):
                cluster.append((photo, path, size, aspect, sample))
                placed = True
                break
        if not placed:
            clusters.append([(photo, path, size, aspect, sample)])
        if digest:
            samples[digest] = True
    result = []
    for cluster in clusters:
        if len({size for _, _, size, _, _ in cluster}) < 2:
            continue
        rows = []
        for photo, path, size, _, _ in cluster:
            try:
                digest = photo.digest or photo.fingerprint()
                linked = any((path.parent / (base + ext)).exists()
                             for base in (path.stem, path.name) for ext in ('.xmp', '.aae'))
                rows.append((photo.relative, size, linked, digest))
            except OSError:
                continue
        if len({row[1] for row in rows}) >= 2:
            result.append(rows)
    return result

candidates = [group for group in files.values() if len({size for _, size in group}) > 1]
print(f'Candidate names: {len(candidates)}', flush=True)
with ThreadPoolExecutor(max_workers=10) as pool:
    results = []
    for index, matches in enumerate(pool.map(analyze, candidates), 1):
        results.extend(matches)
        if index % 1000 == 0:
            print(index, 'names processed', flush=True)
with open(review.DATA / "photo-size-variants.csv", 'w', encoding='utf-8-sig', newline='') as stream:
    writer = csv.writer(stream)
    writer.writerow(['グループ', '写真フォルダからの相対パス', 'サイズ（バイト）', '関連するXMPまたはAAE', 'SHA-256'])
    for index, group in enumerate(results, 1):
        for relative, size, linked, digest in sorted(group, key=lambda row: -row[1]):
            writer.writerow([index, relative, size, 'あり' if linked else 'なし', digest])
print(f'Groups: {len(results)}, photos: {sum(map(len, results))}', flush=True)
