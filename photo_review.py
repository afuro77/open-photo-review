import csv
import hashlib
import json
import os
import re
import sys
import tkinter as tk
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk

from PIL import Image, ImageOps, ImageTk
from pillow_heif import register_heif_opener
from send2trash.win.modern import send2trash
from translations import set_language, tr

register_heif_opener()
def photo_root():
    if len(sys.argv) < 2:
        raise SystemExit("Usage: python photo_review.py PHOTO_FOLDER [--google-drive]")
    root = Path(sys.argv[1]).expanduser().resolve()
    if not root.is_dir():
        raise SystemExit(f"Folder not found: {root}")
    return root


ROOT = photo_root()
DRIVE_FOLDER = "--google-drive" in sys.argv[2:]
ROOT_ID = hashlib.sha256(os.path.normcase(str(ROOT)).encode("utf-8")).hexdigest()
DATA_HOME = Path(os.environ.get("OPEN_PHOTO_REVIEW_DATA", Path(os.environ["LOCALAPPDATA"]) / "OpenPhotoReview"))
DATA = DATA_HOME / ROOT_ID
STATE = DATA / "reviews.json"
SETTINGS = STATE.with_name("settings.json")
HISTORY = STATE.with_name("history.log")
RAW = {".orf", ".raw", ".dng", ".cr2", ".cr3", ".nef", ".arw", ".raf", ".rw2"}


@dataclass(frozen=True)
class Photo:
    relative: str
    digest: str
    sidecar: bool
    size: int = 0

    def path(self):
        path = (ROOT / self.relative).resolve()
        if not path.is_relative_to(ROOT):
            raise ValueError(tr('写真フォルダの外を指すパスです'))
        return path

    def fingerprint(self):
        with self.path().open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()


COPY_SUFFIX = re.compile(
    r"(?:\s*[（(]\d+[）)]|\s*[（(]\d{4}-\d{2}-\d{2}T[\d_:.+-]+[）)]"
    r"|[ _-]+(?:コピー|copy)(?:[ _-]+\d+)?|[ _-]\d+)$",
    re.IGNORECASE,
)


def copy_names(group):
    names = {Path(photo.relative).name.casefold() for photo in group}
    copies = set()
    for photo in group:
        path = Path(photo.relative)
        base = COPY_SUFFIX.sub("", path.stem)
        if base != path.stem and (base + path.suffix).casefold() in names:
            copies.add(photo)
    return copies


def read_groups(filename="photo-duplicates.csv"):
    groups = defaultdict(list)
    with (DATA / filename).open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            groups[row["グループ"]].append(Photo(
                row["写真フォルダからの相対パス"], row["SHA-256"],
                row["関連するXMPまたはAAE"] == "あり", int(row["サイズ（バイト）"])))
    return dict(groups)


def preview(photo, size):
    path = photo.path()
    if path.suffix.lower() in RAW:
        import io
        import rawpy
        with rawpy.imread(str(path)) as raw:
            try:
                thumb = raw.extract_thumb()
            except (rawpy.LibRawNoThumbnailError, rawpy.LibRawUnsupportedThumbnailError):
                image = Image.fromarray(raw.postprocess(half_size=True))
            else:
                image = (Image.open(io.BytesIO(thumb.data)) if thumb.format == rawpy.ThumbFormat.JPEG
                         else Image.fromarray(thumb.data))
    else:
        image = Image.open(path)
    with image:
        result = ImageOps.exif_transpose(image)
        result.thumbnail(size)
        return result.convert("RGB")


def recycle(photos, keepers):
    for photo in [*photos, *keepers]:
        if photo.fingerprint() != photo.digest:
            raise ValueError(tr('調査後にファイルの内容が変わりました。\n{0}', photo.relative))
    completed = []
    for photo in photos:
        try:
            path = photo.path()
            send2trash(str(path))
            if path.exists():
                raise OSError(tr('ごみ箱への移動を確認できませんでした。 {0}', photo.relative))
        except Exception as error:
            return completed, str(error)
        completed.append(photo.relative)
    return completed, None


def bulk_recycle(plan):
    completed = []
    skipped = 0
    first_error = None
    batch = []

    def record_skip(count, reason):
        nonlocal skipped, first_error
        skipped += count
        if first_error is None:
            first_error = str(reason)

    def flush():
        if not batch:
            return
        try:
            send2trash([str(path) for _, path in batch])
        except Exception as failure:
            reason = failure
        else:
            reason = tr('ごみ箱への移動を確認できませんでした。')
        for photo, path in batch:
            if path.exists():
                record_skip(1, reason)
            else:
                completed.append(photo)
        batch.clear()

    for candidates, keepers in plan:
        try:
            if any(p.fingerprint() != p.digest for p in keepers):
                raise ValueError(tr('残す写真の内容が調査後に変わりました。'))
        except (OSError, ValueError) as failure:
            record_skip(len(candidates), failure)
            continue
        for photo in candidates:
            try:
                path = photo.path()
                if any((path.parent / (name + ext)).exists()
                       for name in (path.stem, path.name) for ext in (".xmp", ".aae")):
                    raise ValueError(tr('編集情報ファイルを保護しました。 {0}', photo.relative))
                if photo.fingerprint() != photo.digest:
                    raise ValueError(tr('調査後にファイルの内容が変わりました。 {0}', photo.relative))
            except (OSError, ValueError) as failure:
                record_skip(1, failure)
                continue
            batch.append((photo, path))
            if len(batch) == 64:
                flush()
    flush()
    return completed, skipped, first_error


class ReviewApp:
    OPTIONS = {"保留": "pending", "残す": "keep", "削除候補": "delete"}
    PAGE_SIZE = 4
    EXACT = "完全に同じ写真"
    SIMILAR = "見た目が似ている写真"

    def __init__(self, window, location=None):
        self.window = window
        self.settings = {"confirm_group": True, "confirm_bulk": True, "priority": "size", "advance": True, "language": "ja"}
        if SETTINGS.is_file():
            try:
                self.settings.update(json.loads(SETTINGS.read_text(encoding="utf-8")))
            except (OSError, ValueError, TypeError):
                pass
        set_language(self.settings["language"])
        self.options = {tr(label): value for label, value in self.OPTIONS.items()}
        self.labels = {value: label for label, value in self.options.items()}
        self.restart_requested = False
        self.mode = tk.StringVar(value=self.EXACT)
        if not (DATA / "photo-duplicates.csv").is_file():
            raise ValueError(tr("先に python scan.py PHOTO_FOLDER で写真を調べてください。"))
        self.groups = read_groups()
        if not self.groups:
            self.groups = read_groups("photo-similar.csv")
            self.mode.set(self.SIMILAR)
        if not self.groups:
            raise ValueError(tr("完全一致・類似の候補は見つかりませんでした。"))
        self.keys = list(self.groups)
        self.state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
        self.settings_dialog = None
        self.pool = ThreadPoolExecutor(max_workers=4)
        window.protocol("WM_DELETE_WINDOW", self.close)
        self.index = self.page = 0
        self.images = []
        self.generation = 0
        self.busy = False

        window.title(tr('重複写真の整理'))
        icon = Path(__file__).resolve().with_name("photo-review.ico")
        if icon.is_file():
            window.iconbitmap(default=str(icon))
        window.geometry("1200x790")
        window.minsize(870, 640)
        window.configure(bg="#eef2f5")
        style = ttk.Style(window)
        style.configure(".", font=("Yu Gothic UI", 10))
        style.configure("Title.TLabel", font=("Yu Gothic UI", 22, "bold"))
        style.configure("Card.TLabel", font=("Yu Gothic UI", 10, "bold"))

        shell = ttk.Frame(window, padding=20)
        shell.pack(fill="both", expand=True)
        ttk.Label(shell, text=tr('重複写真の整理'), style="Title.TLabel").pack(anchor="w")
        modes = ttk.Frame(shell)
        modes.pack(fill="x", pady=(8, 12))
        ttk.Button(modes, text=tr('設定'), command=self.open_settings).pack(side="right")
        ttk.Button(modes, text=tr('作業ログ'), command=self.open_history).pack(side="right", padx=6)
        for mode in (self.EXACT, self.SIMILAR):
            ttk.Radiobutton(modes, text=tr(mode), value=mode, variable=self.mode,
                            command=self.switch_mode).pack(side="left", padx=(0, 18))
        self.description = ttk.Label(modes)
        self.description.pack(side="left")
        nav = ttk.Frame(shell)
        nav.pack(fill="x")
        ttk.Button(nav, text=tr('‹ 前のグループ'), command=lambda: self.step(-1)).pack(side="left")
        ttk.Button(nav, text=tr('次のグループ ›'), command=lambda: self.step(1)).pack(side="left", padx=6)
        self.jump = ttk.Entry(nav, width=8)
        self.jump.pack(side="left", padx=(12, 0))
        self.jump.bind("<Return>", lambda _: self.go())
        ttk.Button(nav, text=tr('番号へ移動'), command=self.go).pack(side="left", padx=6)
        ttk.Button(nav, text=tr('次の未確認へ'), command=self.next_pending).pack(side="left", padx=6)
        ttk.Button(nav, text=tr('一覧を再読み込み'), command=self.reload_list).pack(side="left", padx=6)
        self.summary = ttk.Label(nav)
        self.summary.pack(side="right")
        slider_row = ttk.Frame(shell)
        slider_row.pack(fill="x", pady=(5, 0))
        ttk.Label(slider_row, text=tr('グループ移動')).pack(side="left", padx=(0, 8))
        self.slider_label = ttk.Label(slider_row, width=20, anchor="e")
        self.slider_label.pack(side="right", padx=(8, 0))
        self.slider = ttk.Scale(slider_row, orient="horizontal", from_=1, to=max(2, len(self.keys)),
                                command=self.preview_slider)
        self.slider.pack(side="left", fill="x", expand=True)
        self.slider.bind("<ButtonRelease-1>", self.commit_slider)
        self.slider.bind("<KeyRelease>", self.commit_slider)
        comparison = ttk.Frame(shell)
        comparison.pack(fill="x", pady=(5, 0))
        self.size_comparison = ttk.Label(comparison, padding=(8, 3))
        self.size_comparison.pack(side="left", padx=(0, 6))
        self.name_comparison = ttk.Label(comparison, padding=(8, 3))
        self.name_comparison.pack(side="left")
        self.cards = ttk.Frame(shell)
        self.footer = ttk.Frame(shell)
        self.footer.pack(fill="x")
        actions = ttk.Frame(shell)
        actions.pack(fill="x", pady=(8, 0))
        self.quick_button = ttk.Button(actions, text=tr('おすすめを残し、ほかをごみ箱へ'),
                                       command=self.stage_group)
        self.quick_button.pack(side="left")
        self.bulk_button = ttk.Button(actions, text=tr('完全一致の重複をまとめてごみ箱へ'),
                                       command=self.confirm_bulk)
        self.bulk_button.pack(side="left", padx=8)
        self.prev_photo_button = ttk.Button(self.footer, text=tr('‹ 前の写真'), command=lambda: self.change_page(-1))
        self.prev_photo_button.pack(side="left")
        self.next_photo_button = ttk.Button(self.footer, text=tr('次の写真 ›'), command=lambda: self.change_page(1))
        self.next_photo_button.pack(side="left", padx=6)
        self.page_label = ttk.Label(self.footer)
        self.page_label.pack(side="left", padx=6)
        self.delete_button = ttk.Button(actions, command=self.confirm_recycle)
        self.delete_button.pack(side="right")
        self.result = ttk.Label(shell, foreground="#176340")
        self.result.pack(anchor="w", pady=(5, 0))
        self.cards.pack(fill="both", expand=True, pady=12)
        self.resize_job = None
        self.cards.bind("<Configure>", self.schedule_resize)
        window.update_idletasks()
        self.active_mode = self.mode.get()
        if location:
            mode, group = location
            self.mode.set(mode)
            if mode != self.EXACT:
                self.switch_mode()
            if group in self.groups:
                self.index = self.keys.index(group)
        window.update_idletasks()
        self.reload_list(quiet=True)

    def reload_list(self, quiet=False):
        if self.busy:
            return
        filename = "photo-similar.csv" if self.mode.get() == self.SIMILAR else "photo-duplicates.csv"
        try:
            groups = read_groups(filename)
            if not groups:
                raise ValueError(tr('候補が見つかりませんでした。'))
            current = self.key()
            restored = []
            for digest, entries in self.state.items():
                for relative, status in entries.items():
                    if status == "trashed":
                        photo = Photo(relative, digest, False)
                        try:
                            if photo.path().is_file():
                                restored.append(photo)
                        except (OSError, ValueError):
                            continue
            for photo in restored:
                self.state[photo.digest][photo.relative] = "pending"
            if restored:
                try:
                    self.save()
                except OSError:
                    for photo in restored:
                        self.state[photo.digest][photo.relative] = "trashed"
                    raise
        except (OSError, ValueError) as error:
            self.render()
            self.result.configure(text=tr('一覧を再読み込みできませんでした。 {0}', error))
            return
        if self.mode.get() == self.EXACT:
            self.index_exact_copies(groups)
        self.groups, self.keys = groups, list(groups)
        self.index = self.keys.index(current) if current in groups else 0
        self.page = min(self.page, (len(self.photos()) - 1) // self.PAGE_SIZE)
        self.render()
        if restored:
            self.record_history(tr('復元を確認'), (p.relative for p in restored))
            self.result.configure(text=tr('元の場所に戻った {0:,} 枚を、一覧で「保留」に戻しました。', len(restored)))
        elif not quiet:
            self.result.configure(text=tr('一覧を再読み込みしました。'))

    def record_history(self, action, paths, note=""):
        timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
        items = [str(path) for path in paths]
        try:
            HISTORY.parent.mkdir(parents=True, exist_ok=True)
            with HISTORY.open("a", encoding="utf-8") as stream:
                stream.write(tr('{0}  {1}  {2:,} 枚\n', timestamp, action, len(items)))
                for item in items:
                    stream.write(f"  {item}\n")
                if note:
                    stream.write(f"  {note}\n")
                stream.write("\n")
        except OSError as error:
            messagebox.showerror(tr('作業ログを保存できませんでした'), str(error), parent=self.window)

    def open_history(self):
        try:
            text = HISTORY.read_text(encoding="utf-8") if HISTORY.is_file() else ""
        except OSError as error:
            messagebox.showerror(tr('作業ログを開けませんでした'), str(error), parent=self.window)
            return
        dialog = tk.Toplevel(self.window)
        dialog.title(tr('作業ログ'))
        dialog.geometry("740x420")
        container = ttk.Frame(dialog, padding=12)
        container.pack(fill="both", expand=True)
        scroll = ttk.Scrollbar(container)
        scroll.pack(side="right", fill="y")
        box = tk.Text(container, wrap="char", yscrollcommand=scroll.set)
        box.pack(side="left", fill="both", expand=True)
        scroll.configure(command=box.yview)
        box.insert("1.0", text or tr('作業ログはまだありません。'))
        box.configure(state="disabled")
        box.see("end")
        dialog.bind("<Escape>", lambda _: dialog.destroy())

    def open_settings(self):
        if self.busy:
            return
        if self.settings_dialog is not None and self.settings_dialog.winfo_exists():
            self.settings_dialog.lift()
            return
        dialog = tk.Toplevel(self.window)
        self.settings_dialog = dialog
        dialog.title(tr('設定'))
        dialog.transient(self.window)
        dialog.resizable(False, False)
        body = ttk.Frame(dialog, padding=18)
        body.pack(fill="both", expand=True)
        confirm_group = tk.BooleanVar(dialog, self.settings["confirm_group"])
        confirm_bulk = tk.BooleanVar(dialog, self.settings["confirm_bulk"])
        priority = tk.StringVar(dialog, self.settings["priority"])
        advance = tk.BooleanVar(dialog, self.settings["advance"])
        language = tk.StringVar(dialog, self.settings["language"])
        languages = ttk.Frame(body)
        languages.pack(fill="x", pady=(0, 12))
        ttk.Label(languages, text=tr("言語")).pack(side="left", padx=(0, 8))
        ttk.Radiobutton(languages, text="日本語", value="ja", variable=language).pack(side="left")
        ttk.Radiobutton(languages, text="English", value="en", variable=language).pack(side="left")
        ttk.Label(body, text=tr('ごみ箱へ移す前の確認'), style="Card.TLabel").pack(anchor="w")
        ttk.Checkbutton(body, text=tr('このグループを整理するとき'), variable=confirm_group).pack(anchor="w")
        ttk.Checkbutton(body, text=tr('完全一致を一括で整理するとき'), variable=confirm_bulk).pack(anchor="w")
        ttk.Label(body, text=tr('おすすめの優先順位'), style="Card.TLabel").pack(anchor="w", pady=(12, 0))
        ttk.Radiobutton(body, text=tr('ファイルサイズ → ファイル名'), value="size",
                        variable=priority).pack(anchor="w")
        ttk.Radiobutton(body, text=tr('ファイル名 → ファイルサイズ'), value="name",
                        variable=priority).pack(anchor="w")

        ttk.Checkbutton(body, text=tr('おすすめで整理したあと、次の未確認へ進む'),
                        variable=advance).pack(anchor="w", pady=(12, 0))

        def apply():
            settings = {"confirm_group": confirm_group.get(), "confirm_bulk": confirm_bulk.get(),
                        "priority": priority.get(), "advance": advance.get(), "language": language.get()}
            try:
                SETTINGS.parent.mkdir(parents=True, exist_ok=True)
                temporary = SETTINGS.with_suffix(".tmp")
                temporary.write_text(json.dumps(settings), encoding="utf-8")
                temporary.replace(SETTINGS)
            except OSError as error:
                messagebox.showerror(tr('設定を保存できませんでした'), str(error), parent=dialog)
                return
            changed_language = settings["language"] != self.settings["language"]
            self.settings = settings
            dialog.destroy()
            if changed_language:
                self.restart_requested = True
                self.close()
            else:
                self.render()

        buttons = ttk.Frame(body)
        buttons.pack(fill="x", pady=(16, 0))
        ttk.Button(buttons, text=tr('保存'), command=apply).pack(side="right")
        ttk.Button(buttons, text=tr('キャンセル'), command=dialog.destroy).pack(side="right", padx=6)
        dialog.bind("<Escape>", lambda _: dialog.destroy())
        dialog.grab_set()

    def approve_recycle(self, photos, bulk=False):
        if not self.settings["confirm_bulk" if bulk else "confirm_group"]:
            return True
        capacity = sum(p.size for p in photos) / 1_000_000
        message = tr('{0:,} 枚（{1:,.2f} MB）をごみ箱へ移しますか？', len(photos), capacity)
        if DRIVE_FOLDER:
            message += tr('\nGoogle Drive にも反映されます。')
        shown = "\n".join(p.relative for p in photos[:6])
        if len(photos) > 6:
            shown += tr('\nほか {0:,} 枚', len(photos) - 6)
        return messagebox.askyesno(tr('ごみ箱への移動を確認'), f"{message}\n\n{shown}",
                                   default="no", parent=self.window)

    def switch_mode(self):
        if self.busy:
            self.mode.set(self.active_mode)
            return
        target = self.mode.get()
        try:
            groups = read_groups("photo-similar.csv") if target == self.SIMILAR else read_groups()
        except FileNotFoundError:
            self.mode.set(self.active_mode)
            messagebox.showinfo(tr('比較データを準備中'), tr('似ている写真の一覧は準備中です。'))
            return
        if not groups:
            self.mode.set(self.active_mode)
            messagebox.showinfo(tr('該当する写真なし'), tr('このモードの候補が見つかりませんでした。'))
            return
        if target == self.EXACT:
            self.index_exact_copies(groups)
        self.groups, self.keys = groups, list(groups)
        self.index = self.page = 0
        self.render()

    def key(self):
        return self.keys[self.index]

    def index_exact_copies(self, groups):
        self.exact_by_digest = {group[0].digest: group for group in groups.values()}

    def photos(self, key=None):
        original = self.groups[self.key() if key is None else key]
        if self.mode.get() != self.SIMILAR:
            return original
        occupied = {p.relative for p in original}
        resolved = []
        for photo in original:
            if self.status(photo) == "trashed" or not photo.path().is_file():
                candidates = [p for p in self.exact_by_digest.get(photo.digest, ()) if p.relative not in occupied]
                copies = self.available_photos(candidates)
                replacement = min(copies, key=lambda p: self.status(p) != "keep", default=None)
                if replacement:
                    photo = replacement
                    occupied.add(photo.relative)
            resolved.append(photo)
        return resolved

    def status(self, photo):
        return self.state.get(photo.digest, {}).get(photo.relative, "pending")

    def save(self):
        STATE.parent.mkdir(parents=True, exist_ok=True)
        temp = STATE.with_suffix(".tmp")
        temp.write_text(json.dumps(self.state, ensure_ascii=False), encoding="utf-8")
        temp.replace(STATE)

    def update_status(self, photo, status):
        choices = self.state.setdefault(photo.digest, {})
        previous = choices.get(photo.relative)
        choices[photo.relative] = status
        try:
            self.save()
        except OSError as error:
            if previous is None:
                choices.pop(photo.relative)
            else:
                choices[photo.relative] = previous
            messagebox.showerror(tr('振り分けの保存に失敗しました'), str(error))
            return False
        self.refresh_footer()
        return True

    def close(self):
        if self.busy:
            messagebox.showinfo(tr('処理中'), tr('ごみ箱への移動が終わるまでお待ちください。'))
            return
        self.pool.shutdown(wait=False, cancel_futures=True)
        self.window.destroy()

    def step(self, delta):
        if not self.busy:
            self.index = max(0, min(len(self.keys) - 1, self.index + delta))
            self.page = 0
            self.render()

    def go(self):
        if self.busy:
            return
        try:
            self.index = self.keys.index(self.jump.get().strip())
        except ValueError:
            messagebox.showinfo(tr('グループ番号'), tr('1〜{0:,}の番号を入力してください。', len(self.keys)))
            return
        self.page = 0
        self.render()

    def next_pending(self, quiet=False):
        if self.busy:
            return False
        for offset in range(1, len(self.keys)):
            index = (self.index + offset) % len(self.keys)
            group = self.photos(self.keys[index])
            if not any(self.status(p) == "pending" for p in group):
                continue
            available = self.available_photos(group)
            if len(available) >= 2 and any(self.status(p) == "pending" for p in available):
                self.index, self.page = index, 0
                self.result.configure(text="")
                self.render()
                return True
        if not quiet:
            self.result.configure(text=tr('ほかに確認待ちのグループはありません。'))
        return False

    def slider_index(self, value):
        return max(0, min(len(self.keys) - 1, round(float(value)) - 1))

    def preview_slider(self, value):
        target = self.slider_index(value)
        self.slider_label.configure(text=f"{self.keys[target]} / {len(self.keys):,}")

    def commit_slider(self, _event=None):
        if self.busy:
            self.slider.set(self.index + 1)
            return
        target = self.slider_index(self.slider.get())
        if target != self.index:
            self.index, self.page = target, 0
            self.render()

    def change_page(self, delta):
        if not self.busy:
            last = (len(self.photos()) - 1) // self.PAGE_SIZE
            self.page = max(0, min(last, self.page + delta))
            self.render()

    def available_photos(self, group=None):
        photos = self.photos() if group is None else group
        return [p for p in photos if self.status(p) != "trashed" and p.path().is_file()]

    def recommendation(self, available=None, group=None):
        return self.recommendation_details(available, group)[0]

    def recommendation_details(self, available=None, group=None):
        photos = self.available_photos() if available is None else available
        if not photos:
            return None, ""
        if len(photos) == 1:
            return photos[0], tr('選べる写真はこの1枚')
        copies = copy_names(photos if group is None else group)
        criteria = [
            (lambda photo: -photo.size, tr('ファイルサイズが大きい')),
            (lambda photo: photo in copies, tr('元のファイル名を優先')),
        ]
        if self.settings["priority"] == "name":
            criteria.reverse()
        criteria.extend((
            (lambda photo: not photo.sidecar, tr('編集情報ファイル付き')),
            (lambda photo: len(Path(photo.relative).parts), tr('フォルダの階層が浅い')),
            (lambda photo: photo.relative.casefold(), tr('同条件のため、保存先のパス順')),
        ))
        ranked = sorted(photos, key=lambda photo: tuple(key(photo) for key, _ in criteria))
        best, second = ranked[:2]
        reason = next((label for key, label in criteria if key(best) != key(second)), tr('同条件のため、一覧の先頭'))
        return best, reason

    def stage_group(self):
        photos = self.available_photos()
        if self.busy or len(photos) < 2:
            return
        recommended = self.recommendation(photos)
        keepers = [p for p in photos if p == recommended or self.status(p) == "keep"]
        candidates = [p for p in photos if p not in keepers]
        if not candidates:
            self.result.configure(text=tr('このグループの写真はすべて残す指定です。'))
            return
        self.start_group_recycle(candidates, keepers, recommended=recommended)

    def bulk_plan(self):
        plan = []
        for group in self.groups.values():
            active = [p for p in group if self.status(p) != "trashed"]
            if len(active) < 2:
                continue
            keepers = [p for p in active if self.status(p) == "keep"]
            if not keepers:
                eligible = [p for p in active if self.status(p) == "pending"]
                recommended = self.recommendation(eligible, active)
                if recommended:
                    keepers = [recommended]
            candidates = [p for p in active if p not in keepers and not p.sidecar]
            if keepers and candidates:
                plan.append((candidates, keepers))
        return plan

    def confirm_bulk(self):
        if self.busy or self.active_mode != self.EXACT:
            return
        plan = self.bulk_plan()
        photos = [p for candidates, _ in plan for p in candidates]
        if not photos:
            self.result.configure(text=tr('まとめて移動できる重複写真はありません。'))
            return
        if not self.approve_recycle(photos, bulk=True):
            return
        self.busy = True
        self.refresh_footer()
        self.result.configure(text=tr('完全一致を再確認して、ごみ箱へ移動中です…'))
        self.finish_bulk(self.pool.submit(bulk_recycle, plan))

    def finish_bulk(self, future):
        if not future.done():
            self.window.after(150, self.finish_bulk, future)
            return
        try:
            completed, skipped, error = future.result()
            self.record_history(tr('完全一致を一括でごみ箱へ'), (p.relative for p in completed),
                                tr('移動できなかった枚数 {0:,}。 {1}', skipped, error or '') if skipped else "")
            for photo in completed:
                self.state.setdefault(photo.digest, {})[photo.relative] = "trashed"
            if completed:
                self.save()
        except Exception as failure:
            self.busy = False
            self.render()
            messagebox.showerror(tr('一括処理を確認してください'), str(failure))
            return
        self.busy = False
        self.render()
        result = tr('{0:,} 枚をごみ箱へ移動しました。', len(completed))
        if skipped:
            result += tr(' {0:,} 枚は移動できませんでした。', skipped)
        self.result.configure(text=result)
        if error:
            messagebox.showinfo(tr('移動できなかった写真があります'), tr('{0}\n\n最初の理由  {1}', result, error))

    def refresh_footer(self):
        self.prev_photo_button.configure(state="normal" if self.page > 0 and not self.busy else "disabled")
        has_next_page = (self.page + 1) * self.PAGE_SIZE < len(self.photos())
        self.next_photo_button.configure(state="normal" if has_next_page and not self.busy else "disabled")
        self.bulk_button.configure(state="normal" if self.active_mode == self.EXACT and not self.busy else "disabled")
        quick_label = tr('おすすめを残し、ほかをごみ箱へ')
        if self.settings["advance"]:
            quick_label += tr(' → 次へ')
        self.quick_button.configure(text=quick_label,
                                    state="disabled" if self.busy or len(self.available_photos()) < 2 else "normal")
        count = sum(self.status(p) == "delete" for p in self.photos())
        self.delete_button.configure(text=tr('このグループの削除候補 {0} 枚をごみ箱へ', count),
                                     state="normal" if count and not self.busy else "disabled")

    def schedule_resize(self, _event):
        if self.resize_job is not None:
            self.window.after_cancel(self.resize_job)
        self.resize_job = self.window.after(150, self.render_resized)

    def render_resized(self):
        self.resize_job = None
        if not self.busy:
            self.render()

    def render(self):
        self.active_mode = self.mode.get()
        self.description.configure(text=(tr('画像の特徴から選んだ候補です。別の写真も含まれるため、目で確認してください。')
                                         if self.active_mode == self.SIMILAR
                                         else tr('ファイルの中身が完全に一致した写真です。')))
        self.generation += 1
        for child in self.cards.winfo_children():
            child.destroy()
        self.images.clear()
        photos = self.photos()
        available = self.available_photos(photos)
        compared = available or photos
        same_size = len({p.size for p in compared}) == 1
        same_name = len({Path(p.relative).name for p in compared}) == 1
        max_size = max(p.size for p in compared)
        for label, field, same in ((self.size_comparison, tr('サイズ'), same_size),
                                   (self.name_comparison, tr('ファイル名'), same_name)):
            label.configure(text=tr("{0}：{1}", field, tr("同じ") if same else tr("違う")),
                            background="#e0f1e6" if same else "#ffead5",
                            foreground="#165634" if same else "#814000")
        first = self.page * self.PAGE_SIZE
        visible = photos[first:first + self.PAGE_SIZE]
        self.slider.configure(to=max(2, len(self.keys)), state="normal" if len(self.keys) > 1 else "disabled")
        self.slider.set(self.index + 1)
        self.preview_slider(self.index + 1)
        self.jump.delete(0, "end")
        self.jump.insert(0, self.key())
        self.summary.configure(text=tr('グループ {0} / {1:,}\u3000·\u3000{2}枚', self.key(), len(self.keys), len(photos)))
        self.page_label.configure(text=tr('{0}〜{1} / {2}枚', first + 1, first + len(visible), len(photos)))
        columns = len(visible)
        width = max(110, self.cards.winfo_width() // columns - 32)
        height = max(80, self.cards.winfo_height() - 245)
        recommended, reason = self.recommendation_details(available)
        original_paths = {p.relative for p in self.groups[self.key()]}
        copies = copy_names(photos)
        for column, photo in enumerate(visible):
            border_color = "#2f8052" if photo == recommended else "#b6bfba"
            border_width = 3 if photo == recommended else 1
            frame = tk.Frame(self.cards, background=border_color)
            frame.place(relx=column / columns, relwidth=1 / columns, relheight=1,
                        x=4, width=-8)
            card = ttk.Frame(frame, padding=10)
            card.pack(fill="both", expand=True, padx=border_width, pady=border_width)
            if photo == recommended:
                ttk.Label(card, text=tr('★ 残すおすすめ  ·  {0}', reason), foreground="#165634",
                          background="#e0f1e6", style="Card.TLabel", padding=(6, 3),
                          wraplength=width).pack(fill="x", pady=(0, 6))
            ttk.Label(card, text=Path(photo.relative).name, style="Card.TLabel",
                      foreground="#165634" if same_name else "#814000",
                      wraplength=width).pack(anchor="w")
            if photo in copies:
                ttk.Label(card, text=tr('コピー名の候補'), foreground="#814000").pack(anchor="w")
            status = self.status(photo)
            size = tr('{0:,.2f} MB  （{1:,} バイト）', photo.size / 1000000, photo.size)
            if photo in available and not same_size:
                size += (tr('  ・ 最大より {0:,} バイト小さい', max_size - photo.size)
                         if photo.size < max_size else tr('  ・ 最大サイズ'))
            ttk.Label(card, text=size, wraplength=width,
                      foreground="#165634" if same_size else "#814000").pack(anchor="w")
            label = ttk.Label(card, text=tr('ごみ箱へ移動済み') if status == "trashed" else tr('読み込み中…'),
                              justify="center", anchor="center", wraplength=width)
            ttk.Label(card, text=photo.relative, wraplength=width).pack(anchor="w", pady=(0, 6))
            if photo.relative not in original_paths:
                ttk.Label(card, text=tr('別の保存先にある完全一致の写真'), foreground="#165634").pack(anchor="w")
            if photo.sidecar:
                ttk.Label(card, text=tr('編集情報ファイルあり'), foreground="#805700").pack(anchor="w")
            if status == "trashed":
                label.pack(side="bottom", fill="both", expand=True, pady=8)
                continue
            choice = ttk.Combobox(card, values=list(self.options), state="readonly", width=12)
            choice.set(self.labels[status])
            choice.bind("<<ComboboxSelected>>",
                        lambda _, p=photo, c=choice: self.choose(p, c))
            choice.pack(anchor="w", pady=(8, 3))
            if photo == recommended:
                saved = status == "keep"
                ttk.Button(card, text=tr('残す指定済み') if saved else tr('この写真を残す'),
                           state="disabled" if saved else "normal",
                           command=lambda p=photo: self.accept_recommendation(p)).pack(anchor="w", pady=(0, 3))
            ttk.Button(card, text=tr('元の写真を開く'), command=lambda p=photo: self.open_photo(p)).pack(anchor="w")
            label.pack(side="bottom", fill="both", expand=True, pady=8)
            future = self.pool.submit(preview, photo, (width, height))
            self.show_preview(label, future, self.generation)
        self.refresh_footer()

    def accept_recommendation(self, photo):
        if not self.busy and self.update_status(photo, "keep"):
            self.render()
            self.result.configure(text=tr('残す指定を保存しました。'))

    def choose(self, photo, choice):
        if self.busy:
            choice.set(self.labels[self.status(photo)])
            return
        if self.update_status(photo, self.options[choice.get()]):
            self.render()
        else:
            choice.set(self.labels[self.status(photo)])

    def open_photo(self, photo):
        try:
            os.startfile(photo.path())
        except (OSError, ValueError) as error:
            messagebox.showerror(tr('写真を開けませんでした'), str(error))

    def show_preview(self, label, future, generation):
        if generation != self.generation:
            future.cancel()
            return
        if not future.done():
            self.window.after(75, self.show_preview, label, future, generation)
            return
        try:
            image = ImageTk.PhotoImage(future.result(), master=self.window)
        except Exception:
            label.configure(text=tr('プレビューを表示できません。\n元の写真を開いて確認できます。'))
            return
        self.images.append(image)
        label.configure(text="", image=image)

    def confirm_recycle(self):
        photos = [p for p in self.photos() if self.status(p) == "delete"]
        keepers = [p for p in self.photos() if self.status(p) == "keep"]
        if not photos or not keepers:
            messagebox.showinfo(tr('残す写真を選択'), tr('このグループで残す写真を1枚以上選んでください。'))
            return
        self.start_group_recycle(photos, keepers)

    def start_group_recycle(self, photos, keepers, recommended=None):
        if not self.approve_recycle(photos):
            return
        self.result.configure(text="")
        self.busy = True
        self.refresh_footer()
        self.summary.configure(text=tr('ファイルを照合して、ごみ箱へ移動中…'))
        self.finish_recycle(self.pool.submit(recycle, photos, keepers), photos, recommended)

    def finish_recycle(self, future, photos, recommended=None):
        if not future.done():
            self.window.after(100, self.finish_recycle, future, photos, recommended)
            return
        try:
            completed, error = future.result()
        except Exception as failure:
            completed, error = [], str(failure)
        self.record_history(tr('グループからごみ箱へ'), completed, error or "")
        digests = {photo.relative: photo.digest for photo in photos}
        for relative in completed:
            self.state.setdefault(digests[relative], {})[relative] = "trashed"
        if completed:
            if recommended:
                self.state.setdefault(recommended.digest, {})[recommended.relative] = "keep"
            try:
                self.save()
            except OSError as failure:
                error = str(failure)
        self.busy = False
        auto_advance = completed and not error and recommended and self.settings["advance"]
        advanced = self.next_pending(quiet=True) if auto_advance else False
        if not advanced:
            self.render()
        if error:
            messagebox.showerror(tr('処理を中断しました'), tr('{0} 枚をごみ箱へ移動しました。\n\n{1}', len(completed), error))
        else:
            result = tr('{0} 枚をごみ箱へ移動しました。', len(completed))
            if auto_advance and not advanced:
                result += tr(' ほかに確認待ちのグループはありません。')
            self.result.configure(text=result)


def main():
    location = None
    while True:
        window = tk.Tk()
        try:
            app = ReviewApp(window, location=location)
        except Exception as error:
            messagebox.showerror(tr("アプリを開けませんでした"), str(error), parent=window)
            window.destroy()
            return
        window.mainloop()
        if not app.restart_requested:
            return
        location = app.active_mode, app.key()


if __name__ == "__main__":
    main()
