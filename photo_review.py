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
from pathlib import Path
from tkinter import messagebox, ttk

from PIL import Image, ImageOps, ImageTk
from pillow_heif import register_heif_opener
from send2trash.win.modern import send2trash

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
            raise ValueError("写真フォルダの外を指すパスです")
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
            raise ValueError(f"調査後にファイルの内容が変わりました。\n{photo.relative}")
    completed = []
    for photo in photos:
        try:
            path = photo.path()
            send2trash(str(path))
            if path.exists():
                raise OSError(f"ごみ箱への移動を確認できませんでした。 {photo.relative}")
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
            reason = "ごみ箱への移動を確認できませんでした。"
        for photo, path in batch:
            if path.exists():
                record_skip(1, reason)
            else:
                completed.append(photo)
        batch.clear()

    for candidates, keepers in plan:
        try:
            if any(p.fingerprint() != p.digest for p in keepers):
                raise ValueError("残す写真の内容が調査後に変わりました。")
        except (OSError, ValueError) as failure:
            record_skip(len(candidates), failure)
            continue
        for photo in candidates:
            try:
                path = photo.path()
                if any((path.parent / (name + ext)).exists()
                       for name in (path.stem, path.name) for ext in (".xmp", ".aae")):
                    raise ValueError(f"編集情報ファイルを保護しました。 {photo.relative}")
                if photo.fingerprint() != photo.digest:
                    raise ValueError(f"調査後にファイルの内容が変わりました。 {photo.relative}")
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
    LABELS = {value: label for label, value in OPTIONS.items()}
    PAGE_SIZE = 4
    EXACT = "完全に同じ写真"
    SIMILAR = "見た目が似ている写真"

    def __init__(self, window):
        self.window = window
        self.mode = tk.StringVar(value=self.EXACT)
        self.groups = read_groups()
        if not self.groups:
            self.groups = read_groups("photo-similar.csv")
            self.mode.set(self.SIMILAR)
        if not self.groups:
            raise ValueError("完全一致・類似の候補は見つかりませんでした。")
        self.keys = list(self.groups)
        self.state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
        self.settings = {"confirm_group": True, "confirm_bulk": True, "priority": "size"}
        if SETTINGS.is_file():
            try:
                self.settings.update(json.loads(SETTINGS.read_text(encoding="utf-8")))
            except (OSError, ValueError, TypeError):
                pass
        self.settings_dialog = None
        self.pool = ThreadPoolExecutor(max_workers=4)
        window.protocol("WM_DELETE_WINDOW", self.close)
        self.index = self.page = 0
        self.images = []
        self.generation = 0
        self.busy = False

        window.title("重複写真の整理")
        window.geometry("1200x790")
        window.minsize(870, 640)
        window.configure(bg="#eef2f5")
        style = ttk.Style(window)
        style.configure(".", font=("Yu Gothic UI", 10))
        style.configure("Title.TLabel", font=("Yu Gothic UI", 22, "bold"))
        style.configure("Card.TLabel", font=("Yu Gothic UI", 10, "bold"))

        shell = ttk.Frame(window, padding=20)
        shell.pack(fill="both", expand=True)
        ttk.Label(shell, text="重複写真の整理", style="Title.TLabel").pack(anchor="w")
        modes = ttk.Frame(shell)
        modes.pack(fill="x", pady=(8, 12))
        ttk.Button(modes, text="設定", command=self.open_settings).pack(side="right")
        for mode in (self.EXACT, self.SIMILAR):
            ttk.Radiobutton(modes, text=mode, value=mode, variable=self.mode,
                            command=self.switch_mode).pack(side="left", padx=(0, 18))
        self.description = ttk.Label(modes)
        self.description.pack(side="left")
        nav = ttk.Frame(shell)
        nav.pack(fill="x")
        ttk.Button(nav, text="‹ 前のグループ", command=lambda: self.step(-1)).pack(side="left")
        ttk.Button(nav, text="次のグループ ›", command=lambda: self.step(1)).pack(side="left", padx=6)
        self.jump = ttk.Entry(nav, width=8)
        self.jump.pack(side="left", padx=(12, 0))
        self.jump.bind("<Return>", lambda _: self.go())
        ttk.Button(nav, text="番号へ移動", command=self.go).pack(side="left", padx=6)
        ttk.Button(nav, text="次の未確認へ", command=self.next_pending).pack(side="left", padx=6)
        self.summary = ttk.Label(nav)
        self.summary.pack(side="right")
        comparison = ttk.Frame(shell)
        comparison.pack(fill="x", pady=(5, 0))
        self.size_comparison = ttk.Label(comparison, padding=(8, 3))
        self.size_comparison.pack(side="left", padx=(0, 6))
        self.name_comparison = ttk.Label(comparison, padding=(8, 3))
        self.name_comparison.pack(side="left")
        if DRIVE_FOLDER:
            ttk.Label(comparison, text="ごみ箱への移動は Google Drive にも反映されます", foreground="#814000").pack(side="right")
        self.cards = ttk.Frame(shell)
        self.footer = ttk.Frame(shell)
        self.footer.pack(fill="x")
        actions = ttk.Frame(shell)
        actions.pack(fill="x", pady=(8, 0))
        self.quick_button = ttk.Button(actions, text="おすすめを残し、ほかをごみ箱へ",
                                       command=self.stage_group)
        self.quick_button.pack(side="left")
        self.bulk_button = ttk.Button(actions, text="完全一致の重複をまとめてごみ箱へ",
                                       command=self.confirm_bulk)
        self.bulk_button.pack(side="left", padx=8)
        ttk.Button(self.footer, text="‹ 前の写真", command=lambda: self.change_page(-1)).pack(side="left")
        ttk.Button(self.footer, text="次の写真 ›", command=lambda: self.change_page(1)).pack(side="left", padx=6)
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
        window.update_idletasks()
        self.render()

    def open_settings(self):
        if self.busy:
            return
        if self.settings_dialog is not None and self.settings_dialog.winfo_exists():
            self.settings_dialog.lift()
            return
        dialog = tk.Toplevel(self.window)
        self.settings_dialog = dialog
        dialog.title("設定")
        dialog.transient(self.window)
        dialog.resizable(False, False)
        body = ttk.Frame(dialog, padding=18)
        body.pack(fill="both", expand=True)
        confirm_group = tk.BooleanVar(dialog, self.settings["confirm_group"])
        confirm_bulk = tk.BooleanVar(dialog, self.settings["confirm_bulk"])
        priority = tk.StringVar(dialog, self.settings["priority"])
        ttk.Label(body, text="ごみ箱へ移す前の確認", style="Card.TLabel").pack(anchor="w")
        ttk.Checkbutton(body, text="このグループを整理するとき", variable=confirm_group).pack(anchor="w")
        ttk.Checkbutton(body, text="完全一致を一括で整理するとき", variable=confirm_bulk).pack(anchor="w")
        ttk.Label(body, text="おすすめの優先順位", style="Card.TLabel").pack(anchor="w", pady=(12, 0))
        ttk.Radiobutton(body, text="ファイルサイズ → ファイル名", value="size",
                        variable=priority).pack(anchor="w")
        ttk.Radiobutton(body, text="ファイル名 → ファイルサイズ", value="name",
                        variable=priority).pack(anchor="w")

        def apply():
            settings = {"confirm_group": confirm_group.get(), "confirm_bulk": confirm_bulk.get(),
                        "priority": priority.get()}
            try:
                SETTINGS.parent.mkdir(parents=True, exist_ok=True)
                temporary = SETTINGS.with_suffix(".tmp")
                temporary.write_text(json.dumps(settings), encoding="utf-8")
                temporary.replace(SETTINGS)
            except OSError as error:
                messagebox.showerror("設定を保存できませんでした", str(error), parent=dialog)
                return
            self.settings = settings
            dialog.destroy()
            self.render()

        buttons = ttk.Frame(body)
        buttons.pack(fill="x", pady=(16, 0))
        ttk.Button(buttons, text="保存", command=apply).pack(side="right")
        ttk.Button(buttons, text="キャンセル", command=dialog.destroy).pack(side="right", padx=6)
        dialog.bind("<Escape>", lambda _: dialog.destroy())
        dialog.grab_set()

    def approve_recycle(self, photos, bulk=False):
        if not self.settings["confirm_bulk" if bulk else "confirm_group"]:
            return True
        capacity = sum(p.size for p in photos) / 1_000_000
        message = f"{len(photos):,} 枚（{capacity:,.2f} MB）をごみ箱へ移しますか？"
        if DRIVE_FOLDER:
            message += "\nGoogle Drive にも反映されます。"
        shown = "\n".join(p.relative for p in photos[:6])
        if len(photos) > 6:
            shown += f"\nほか {len(photos) - 6:,} 枚"
        return messagebox.askyesno("ごみ箱への移動を確認", f"{message}\n\n{shown}",
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
            messagebox.showinfo("比較データを準備中", "似ている写真の一覧は準備中です。")
            return
        if not groups:
            self.mode.set(self.active_mode)
            messagebox.showinfo("該当する写真なし", "このモードの候補が見つかりませんでした。")
            return
        self.groups, self.keys = groups, list(groups)
        self.index = self.page = 0
        self.render()

    def key(self):
        return self.keys[self.index]

    def photos(self):
        return self.groups[self.key()]

    def status(self, photo):
        return self.state.get(photo.digest, {}).get(photo.relative, "pending")

    def save(self):
        STATE.parent.mkdir(parents=True, exist_ok=True)
        temp = STATE.with_suffix(".tmp")
        temp.write_text(json.dumps(self.state, ensure_ascii=False), encoding="utf-8")
        temp.replace(STATE)

    def update_status(self, photo, status):
        self.state.setdefault(photo.digest, {})[photo.relative] = status
        try:
            self.save()
        except OSError as error:
            messagebox.showerror("振り分けの保存に失敗しました", str(error))
        self.refresh_footer()

    def close(self):
        if self.busy:
            messagebox.showinfo("処理中", "ごみ箱への移動が終わるまでお待ちください。")
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
            messagebox.showinfo("グループ番号", f"1〜{len(self.keys):,}の番号を入力してください。")
            return
        self.page = 0
        self.render()

    def next_pending(self):
        if self.busy:
            return
        for offset in range(1, len(self.keys) + 1):
            index = (self.index + offset) % len(self.keys)
            if any(self.status(p) == "pending" for p in self.groups[self.keys[index]]):
                self.index, self.page = index, 0
                self.render()
                return
        messagebox.showinfo("確認完了", "保留の写真はありません。")

    def change_page(self, delta):
        if not self.busy:
            last = (len(self.photos()) - 1) // self.PAGE_SIZE
            self.page = max(0, min(last, self.page + delta))
            self.render()

    def available_photos(self):
        return [p for p in self.photos() if self.status(p) != "trashed" and p.path().is_file()]

    def recommendation(self, available=None, group=None):
        photos = self.available_photos() if available is None else available
        copies = copy_names(photos if group is None else group)
        def rank(photo):
            size, copy = -photo.size, photo in copies
            priority = (copy, size) if self.settings["priority"] == "name" else (size, copy)
            return (*priority, not photo.sidecar, len(Path(photo.relative).parts), photo.relative.casefold())

        return min(photos, key=rank, default=None)

    def stage_group(self):
        photos = self.available_photos()
        if self.busy or len(photos) < 2:
            return
        recommended = self.recommendation(photos)
        keepers = [p for p in photos if p == recommended or self.status(p) == "keep"]
        candidates = [p for p in photos if p not in keepers]
        if not candidates:
            self.result.configure(text="このグループの写真はすべて残す指定です。")
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
            self.result.configure(text="まとめて移動できる重複写真はありません。")
            return
        if not self.approve_recycle(photos, bulk=True):
            return
        self.busy = True
        self.refresh_footer()
        self.result.configure(text="完全一致を再確認して、ごみ箱へ移動中です…")
        self.finish_bulk(self.pool.submit(bulk_recycle, plan))

    def finish_bulk(self, future):
        if not future.done():
            self.window.after(150, self.finish_bulk, future)
            return
        try:
            completed, skipped, error = future.result()
            for photo in completed:
                self.state.setdefault(photo.digest, {})[photo.relative] = "trashed"
            if completed:
                self.save()
        except Exception as failure:
            self.busy = False
            self.render()
            messagebox.showerror("一括処理を確認してください", str(failure))
            return
        self.busy = False
        self.render()
        result = f"{len(completed):,} 枚をごみ箱へ移動しました。"
        if skipped:
            result += f" {skipped:,} 枚は移動できませんでした。"
        self.result.configure(text=result)
        if error:
            messagebox.showinfo("移動できなかった写真があります", f"{result}\n\n最初の理由  {error}")

    def refresh_footer(self):
        self.bulk_button.configure(state="normal" if self.active_mode == self.EXACT and not self.busy else "disabled")
        self.quick_button.configure(state="disabled" if self.busy or len(self.available_photos()) < 2 else "normal")
        count = sum(self.status(p) == "delete" for p in self.photos())
        self.delete_button.configure(text=f"このグループの削除候補 {count} 枚をごみ箱へ",
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
        self.description.configure(text=("画像の特徴から選んだ候補です。別の写真も含まれるため、目で確認してください。"
                                         if self.active_mode == self.SIMILAR
                                         else "ファイルの中身が完全に一致した写真です。"))
        self.generation += 1
        for child in self.cards.winfo_children():
            child.destroy()
        self.images.clear()
        photos = self.photos()
        same_size = len({p.size for p in photos}) == 1
        same_name = len({Path(p.relative).name for p in photos}) == 1
        max_size = max(p.size for p in photos)
        for label, field, same in ((self.size_comparison, "サイズ", same_size),
                                   (self.name_comparison, "ファイル名", same_name)):
            label.configure(text=f"{field}：{'同じ' if same else '違う'}",
                            background="#e0f1e6" if same else "#ffead5",
                            foreground="#165634" if same else "#814000")
        first = self.page * self.PAGE_SIZE
        visible = photos[first:first + self.PAGE_SIZE]
        self.jump.delete(0, "end")
        self.jump.insert(0, self.key())
        self.summary.configure(text=f"グループ {self.key()} / {len(self.keys):,}　·　{len(photos)}枚")
        self.page_label.configure(text=f"{first + 1}〜{first + len(visible)} / {len(photos)}枚")
        columns = len(visible)
        width = max(110, self.cards.winfo_width() // columns - 32)
        height = max(80, self.cards.winfo_height() - 245)
        recommended = self.recommendation()
        copies = copy_names(photos)
        for column, photo in enumerate(visible):
            card = ttk.Frame(self.cards, padding=10, relief="solid", borderwidth=1)
            card.place(relx=column / columns, relwidth=1 / columns, relheight=1,
                       x=4, width=-8)
            ttk.Label(card, text=Path(photo.relative).name, style="Card.TLabel",
                      foreground="#165634" if same_name else "#814000",
                      wraplength=width).pack(anchor="w")
            if photo in copies:
                ttk.Label(card, text="コピー名の候補", foreground="#814000").pack(anchor="w")
            status = self.status(photo)
            size = f"{photo.size / 1_000_000:,.2f} MB  （{photo.size:,} バイト）"
            if not same_size:
                size += (f"  ・ 最大より {max_size - photo.size:,} バイト小さい"
                         if photo.size < max_size else "  ・ 最大サイズ")
            ttk.Label(card, text=size, wraplength=width,
                      foreground="#165634" if same_size else "#814000").pack(anchor="w")
            if photo == recommended:
                ttk.Label(card, text="★ 残すおすすめ", foreground="#176340",
                          style="Card.TLabel").pack(anchor="w")
            label = ttk.Label(card, text="ごみ箱へ移動済み" if status == "trashed" else "読み込み中…",
                              justify="center", anchor="center", wraplength=width)
            ttk.Label(card, text=photo.relative, wraplength=width).pack(anchor="w", pady=(0, 6))
            if photo.sidecar:
                ttk.Label(card, text="編集情報ファイルあり", foreground="#805700").pack(anchor="w")
            if status == "trashed":
                label.pack(side="bottom", fill="both", expand=True, pady=8)
                continue
            choice = ttk.Combobox(card, values=list(self.OPTIONS), state="readonly", width=12)
            choice.set(self.LABELS[status])
            choice.bind("<<ComboboxSelected>>",
                        lambda _, p=photo, c=choice: self.choose(p, c))
            choice.pack(anchor="w", pady=(8, 3))
            if photo == recommended:
                ttk.Button(card, text="おすすめを残す",
                           command=lambda p=photo, c=choice: self.accept_recommendation(p, c)).pack(anchor="w", pady=(0, 3))
            ttk.Button(card, text="元の写真を開く", command=lambda p=photo: self.open_photo(p)).pack(anchor="w")
            label.pack(side="bottom", fill="both", expand=True, pady=8)
            future = self.pool.submit(preview, photo, (width, height))
            self.show_preview(label, future, self.generation)
        self.refresh_footer()

    def accept_recommendation(self, photo, choice):
        if not self.busy:
            choice.set("残す")
            self.update_status(photo, "keep")

    def choose(self, photo, choice):
        if self.busy:
            choice.set(self.LABELS[self.status(photo)])
            return
        self.update_status(photo, self.OPTIONS[choice.get()])

    def open_photo(self, photo):
        try:
            os.startfile(photo.path())
        except (OSError, ValueError) as error:
            messagebox.showerror("写真を開けませんでした", str(error))

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
            label.configure(text="プレビューを表示できません。\n元の写真を開いて確認できます。")
            return
        self.images.append(image)
        label.configure(text="", image=image)

    def confirm_recycle(self):
        photos = [p for p in self.photos() if self.status(p) == "delete"]
        keepers = [p for p in self.photos() if self.status(p) == "keep"]
        if not photos or not keepers:
            messagebox.showinfo("残す写真を選択", "このグループで残す写真を1枚以上選んでください。")
            return
        self.start_group_recycle(photos, keepers)

    def start_group_recycle(self, photos, keepers, recommended=None):
        if not self.approve_recycle(photos):
            return
        self.result.configure(text="")
        self.busy = True
        self.refresh_footer()
        self.summary.configure(text="ファイルを照合して、ごみ箱へ移動中…")
        self.finish_recycle(self.pool.submit(recycle, photos, keepers), photos, recommended)

    def finish_recycle(self, future, photos, recommended=None):
        if not future.done():
            self.window.after(100, self.finish_recycle, future, photos, recommended)
            return
        try:
            completed, error = future.result()
        except Exception as failure:
            completed, error = [], str(failure)
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
        self.render()
        if error:
            messagebox.showerror("処理を中断しました", f"{len(completed)} 枚をごみ箱へ移動しました。\n\n{error}")
        else:
            self.result.configure(text=f"{len(completed)} 枚をごみ箱へ移動しました。")


def main():
    window = tk.Tk()
    try:
        if not (DATA / "photo-duplicates.csv").is_file():
            raise ValueError("先に python scan.py PHOTO_FOLDER で写真を調べてください。")
        window.update_idletasks()
        ReviewApp(window)
    except Exception as error:
        messagebox.showerror("アプリを開けませんでした", str(error), parent=window)
        window.destroy()
        return
    window.mainloop()


if __name__ == "__main__":
    main()

