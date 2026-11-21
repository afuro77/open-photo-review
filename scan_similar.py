import csv
import importlib.util
import os
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image

import photo_review as review

root = review.ROOT
known = {}
representatives = set()
for group in review.read_groups().values():
    representatives.add(group[0].relative)
    known.update({p.relative: p.digest for p in group})
entries = []
for directory, _, names in os.walk(root):
    for name in names:
        path = Path(directory, name)
        if path.suffix.lower() in {'.jpg', '.jpeg', '.png', '.heic', '.heif', '.orf', '.tif', '.gif'}:
            relative = str(path.relative_to(root))
            digest = known.get(relative)
            if not digest or relative in representatives:
                entries.append(review.Photo(relative, digest or '', False))

def describe(photo):
    try:
        image = review.preview(photo, (96, 96))
        gray = np.asarray(image.convert('L').resize((9, 8)), dtype=np.int16)
        color = np.asarray(image.resize((24, 24)), dtype=np.int16)
        if color.std(axis=(0, 1)).mean() < 10:
            return None
        signature = int.from_bytes(np.packbits(gray[:, 1:] > gray[:, :-1]).tobytes(), 'little')
        return photo, signature, image.width / image.height, color
    except Exception:
        return None

def compatible(left, right):
    _, lh, la, lc = left
    _, rh, ra, rc = right
    return (lh ^ rh).bit_count() <= 7 and abs(la / ra - 1) <= .055 and np.abs(lc - rc).mean() <= 14

print(f'Photos to fingerprint: {len(entries)}', flush=True)
index = defaultdict(list)
clusters = []
with ThreadPoolExecutor(max_workers=10) as pool:
    for count, item in enumerate(pool.map(describe, entries), 1):
        if count % 2500 == 0:
            print(count, 'fingerprinted', flush=True)
        if item is None:
            continue
        _, signature, _, _ = item
        keys = [(band, (signature >> (16 * band)) & 65535) for band in range(4)]
        nearby = set()
        for band, key in keys:
            nearby.update(index.get((band, key), ()))
            for bit in range(16):
                nearby.update(index.get((band, key ^ (1 << bit)), ()))
        for group_id in sorted(nearby):
            if all(compatible(item, member) for member in clusters[group_id]):
                clusters[group_id].append(item)
                break
        else:
            group_id = len(clusters)
            clusters.append([item])
            for key in keys:
                index[key].append(group_id)

temp = review.DATA / "photo-similar.tmp"
if Path(review.DATA / "photo-size-variants.csv").exists():
    with open(review.DATA / "photo-size-variants.csv", encoding='utf-8-sig', newline='') as stream:
        same_name = defaultdict(list)
        for row in csv.DictReader(stream):
            same_name[row['グループ']].append(row)
else:
    same_name = {}
seen = set()
total = 0
with temp.open('w', encoding='utf-8-sig', newline='') as stream:
    writer = csv.writer(stream)
    writer.writerow(['グループ', '写真フォルダからの相対パス', 'サイズ（バイト）', '関連するXMPまたはAAE', 'SHA-256'])
    def emit(rows):
        global total
        identity = frozenset(row[3] for row in rows)
        if len(identity) < 2 or identity in seen:
            return
        seen.add(identity)
        total += 1
        for row in sorted(rows, key=lambda row: (-row[1], not row[2], row[0].casefold())):
            writer.writerow([total, row[0], row[1], 'あり' if row[2] else 'なし', row[3]])
    for rows in same_name.values():
        emit([(row['写真フォルダからの相対パス'], int(row['サイズ（バイト）']), row['関連するXMPまたはAAE'] == 'あり', row['SHA-256']) for row in rows])
    for cluster in clusters:
        if len(cluster) < 2:
            continue
        rows = []
        for photo, _, _, _ in cluster:
            try:
                path = photo.path()
                linked = any((path.parent / (base + ext)).exists()
                             for base in (path.stem, path.name) for ext in ('.xmp', '.aae'))
                rows.append((photo.relative, path.stat().st_size, linked, photo.digest or photo.fingerprint()))
            except OSError:
                continue
        emit(rows)
temp.replace(review.DATA / "photo-similar.csv")
print('Similar groups:', total, flush=True)
