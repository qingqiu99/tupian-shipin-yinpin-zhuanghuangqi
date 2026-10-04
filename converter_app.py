# -*- coding: utf-8 -*-
"""
视频/图片/音频 批量转换器（GUI 版）
- 视频: H.264 + AAC, MP4, 1080p, 30fps
- 图片: JPEG, 最长边 1600px
- 音频: 无损源 -> MP3 (320k 听感无损)
支持拖拽导入文件/文件夹，批量转换，自选导出目录。
"""
import os
import queue
import shutil
import traceback
import subprocess
import sys
import threading
from pathlib import Path

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# ---- 拖放方案选择 ----
# Windows 优先用 windnd(纯 Win32 WM_DROPFILES, 稳定, 支持 Unicode 路径);
# windnd 不可用时回退 tkinterdnd2(OLE/TKDND); 都没有则仅按钮添加。
try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    HAS_TKDND = True
except ImportError:
    HAS_TKDND = False

IS_WINDOWS = sys.platform.startswith("win")
try:
    import windnd
    HAS_WINDND = IS_WINDOWS
except ImportError:
    HAS_WINDND = False

if HAS_WINDND:
    TK_ROOT, DND_MODE = tk.Tk, "windnd"
elif HAS_TKDND:
    TK_ROOT, DND_MODE = TkinterDnD.Tk, "tkdnd"
else:
    TK_ROOT, DND_MODE = tk.Tk, "none"

# ================== 固定配置 ==================
def _find_ffmpeg():
    """查找 ffmpeg: 优先用软件自带的(便携模式), 再找打包资源, 最后回退已装路径和 PATH"""
    cands = []
    if getattr(sys, "frozen", False):  # PyInstaller exe 模式
        exe_dir = Path(sys.executable).parent
        cands += [exe_dir / "ffmpeg.exe", exe_dir / "ffmpeg" / "ffmpeg.exe", Path(sys._MEIPASS) / "ffmpeg" / "ffmpeg.exe"]
    here = Path(__file__).parent
    cands += [here / "ffmpeg.exe", here / "ffmpeg" / "ffmpeg.exe",
              here.parent / "ffmpeg" / "ffmpeg.exe",
              Path(r"E:\ffmpeg-master-latest-win64-gpl-shared\bin\ffmpeg.exe")]
    for c in cands:
        if c.exists():
            return str(c)
    return "ffmpeg"  # 回退到 PATH

FFMPEG = _find_ffmpeg()

VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".flv", ".wmv", ".webm", ".ts", ".m4v", ".mpg", ".mpeg"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
AUDIO_LOSSLESS = {".wav", ".flac", ".aiff", ".aif", ".ape", ".wv"}
AUDIO_LOSSY = {".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wma"}
ALL_EXTS = VIDEO_EXTS | IMAGE_EXTS | AUDIO_LOSSLESS | AUDIO_LOSSY

# 质量档位: (视频CRF, 图片质量)
PRESETS = {"高清晰度": (19, 82), "均衡(推荐)": (21, 78), "更省空间": (23, 74)}
IMAGE_MAX_EDGE = 1600
VIDEO_HEIGHT = 1080
VIDEO_FPS = 30
AUDIO_BITRATE_DEFAULT = "320"


def _config_file():
    """配置文件放在软件目录(便携, 跟着文件夹走); 写不进则退回用户目录"""
    base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
    p = base / "转换器配置.json"
    try:
        base.mkdir(parents=True, exist_ok=True)
        p.touch(exist_ok=True)
        return p
    except OSError:
        return Path.home() / "批量转换器配置.json"

# =============================================


def human(n):
    if n >= 1048576:
        return f"{n/1048576:.1f}MB"
    return f"{n/1024:.0f}KB"


class App:
    def __init__(self, root):
        self.root = root
        root.title("批量转换器 · 视频→MP4(1080p/30fps) 图片→JPEG(1600px) 音频→MP3")
        root.geometry("760x640")
        root.minsize(680, 560)
        self.files = []
        self.log_q = queue.Queue()
        self.running = False
        self.cancel = False
        self._build_ui()
        self._load_config()
        # 挂拖放钩子
        if DND_MODE == "windnd":
            self._hook_windnd()
        elif DND_MODE == "tkdnd":
            self.root.drop_target_register(DND_FILES)
            self.root.bind("<<Drop>>", self._on_drop)
        self.root.after(100, self._drain_log)

    def _hook_windnd(self):
        """windnd: 给窗口里所有控件的 hwnd 挂 WM_DROPFILES 钩子, 全窗口可拖入"""
        targets = []
        def add(w):
            targets.append(w)
            for c in w.winfo_children():
                add(c)
        add(self.root)
        for w in targets:
            try:
                windnd.hook_dropfiles(w, func=self._on_windnd_drop, force_unicode=True)
            except Exception:
                pass
        self._debug(f"windnd 钩子已挂载, 覆盖 {len(targets)} 个控件")

    # ---------- 配置记忆 ----------
    def _save_config(self):
        import json
        try:
            data = {
                "out_img": self.out_img.get(),
                "out_vid": self.out_vid.get(),
                "out_aud": self.out_aud.get(),
                "preset": self.preset_var.get(),
                "audio_bitrate": self.audio_var.get(),
            }
            _config_file().write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _load_config(self):
        import json
        try:
            data = json.loads(_config_file().read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self.out_img.set(data.get("out_img", ""))
                self.out_vid.set(data.get("out_vid", ""))
                self.out_aud.set(data.get("out_aud", ""))
                if data.get("preset") in PRESETS:
                    self.preset_var.set(data["preset"])
                if str(data.get("audio_bitrate", "")) in ("128", "192", "256", "320"):
                    self.audio_var.set(str(data["audio_bitrate"]))
        except Exception:
            pass

    # ---------- UI ----------
    def _build_ui(self):
        pad = {"padx": 10, "pady": 4}
        frm = ttk.Frame(self.root)
        frm.pack(fill="both", expand=True)

        # 拖拽提示 / 按钮
        top = ttk.LabelFrame(frm, text=" 1. 添加要转换的文件（可直接把文件或文件夹拖到窗口里任何位置）")
        top.pack(fill="x", **pad)

        self.listbox = tk.Listbox(top, height=10, selectmode="extended",
                                  font=("Microsoft YaHei UI", 9))
        self.listbox.pack(fill="both", expand=True, padx=8, pady=(8, 4))
        self.count_var = tk.StringVar(value="0 个文件")
        ttk.Label(top, textvariable=self.count_var, foreground="#666").pack(anchor="e", padx=8)

        btns = ttk.Frame(top)
        btns.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Button(btns, text="添加文件", command=self.add_files).pack(side="left", padx=(0, 6))
        ttk.Button(btns, text="添加文件夹", command=self.add_folder).pack(side="left", padx=6)
        ttk.Button(btns, text="移除选中", command=self.remove_selected).pack(side="left", padx=6)
        ttk.Button(btns, text="清空列表", command=self.clear_all).pack(side="left", padx=6)

        # 输出目录
        out = ttk.LabelFrame(frm, text=" 2. 导出位置（某类留空 = 这一类先不转换，开始时会提示）")
        out.pack(fill="x", **pad)
        self.out_img = tk.StringVar()
        self.out_vid = tk.StringVar()
        self.out_aud = tk.StringVar()
        for label, var in (("图片:", self.out_img), ("视频:", self.out_vid), ("音频:", self.out_aud)):
            row = ttk.Frame(out)
            row.pack(fill="x", padx=8, pady=4)
            ttk.Label(row, text=label, width=5).pack(side="left")
            ttk.Entry(row, textvariable=var).pack(side="left", fill="x", expand=True, padx=(0, 6))
            ttk.Button(row, text="浏览...", command=lambda v=var: self.pick_out(v)).pack(side="left")

        # 参数
        opt = ttk.LabelFrame(frm, text=" 3. 质量档位")
        opt.pack(fill="x", **pad)
        self.preset_var = tk.StringVar(value="均衡(推荐)")
        for name in PRESETS:
            ttk.Radiobutton(opt, text=name, variable=self.preset_var, value=name).pack(side="left", padx=10, pady=6)
        ttk.Label(opt, text="   音频码率:").pack(side="left", padx=(20, 4))
        self.audio_var = tk.StringVar(value=AUDIO_BITRATE_DEFAULT.replace("k", ""))
        cb = ttk.Combobox(opt, textvariable=self.audio_var, width=5, state="readonly",
                          values=("128", "192", "256", "320"))
        cb.pack(side="left")

        # 开始
        go = ttk.Frame(frm)
        go.pack(fill="x", **pad)
        self.go_btn = ttk.Button(go, text="开始转换", command=self.start)
        self.go_btn.pack(side="left", padx=(0, 6))
        self.stop_btn = ttk.Button(go, text="停止", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=6)
        self.open_btn = ttk.Button(go, text="打开导出文件夹", command=self.open_out)
        self.open_btn.pack(side="right")
        self.progress = ttk.Progressbar(go, length=220, mode="determinate")
        self.progress.pack(side="right", padx=10)

        # 日志
        logf = ttk.LabelFrame(frm, text=" 转换日志")
        logf.pack(fill="both", expand=True, **pad)
        self.log = tk.Text(logf, height=8, font=("Consolas", 9), state="disabled")
        sb = ttk.Scrollbar(logf, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        self.log.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=8)
        sb.pack(side="right", fill="y", padx=(0, 8), pady=8)

    # ---------- 添加文件 ----------
    def _on_windnd_drop(self, files):
        try:
            paths = [f if isinstance(f, str) else f.decode("mbcs", "replace") for f in files]
            self._debug(f"windnd 收到拖放: {paths[:5]}")
            self._add_paths(paths)
        except Exception:
            self._debug("windnd 处理出错:\n" + traceback.format_exc())
            self.log_write("[提示] 拖放内容处理出错, 详见 转换器调试.log")

    def _debug(self, msg):
        """拖放调试日志: 写到软件目录下的 转换器调试.log, 方便排查 exe 无控制台时的问题"""
        try:
            base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
            with open(base / "转换器调试.log", "a", encoding="utf-8") as f:
                import datetime
                f.write(f"[{datetime.datetime.now():%H:%M:%S}] {msg}\n")
        except Exception:
            pass

    def _on_drop(self, event):
        raw = getattr(event, "data", "")
        # 200ms 内相同数据的重复事件去重(根窗口和列表框都可能收到)
        import time as _t
        now = _t.time()
        if getattr(self, "_last_drop", None) and self._last_drop[0] == raw and now - self._last_drop[1] < 0.2:
            return
        self._last_drop = (raw, now)
        self._debug(f"收到拖放: {raw[:500] if raw else '(空)'}")
        try:
            paths = [p.strip("{}") for p in self.root.tk.splitlist(raw)] if raw else []
            if not paths:
                self.log_write("[提示] 拖放内容为空, 无法识别")
            else:
                self._add_paths(paths)
        except Exception:
            self._debug("拖放处理出错:\n" + traceback.format_exc())
            self.log_write("[提示] 拖放内容处理出错, 详见 转换器调试.log")

    def add_files(self):
        p = filedialog.askopenfilenames(title="选择文件",
                                        filetypes=[("媒体文件", "*.mp4 *.mkv *.avi *.mov *.flv *.wmv *.webm *.ts *.png *.jpg *.jpeg *.webp *.bmp *.tif *.wav *.flac *.aiff *.ape *.mp3 *.m4a *.ogg *.opus"), ("所有文件", "*.*")])
        self._add_paths(p)

    def add_folder(self):
        p = filedialog.askdirectory(title="选择文件夹（会包含所有子文件夹里的媒体）")
        if p:
            self._add_paths([p])

    def _add_paths(self, paths):
        added = 0
        for p in paths:
            pt = Path(p)
            if pt.is_dir():
                for f in sorted(pt.rglob("*")):
                    if f.suffix.lower() in ALL_EXTS:
                        self.files.append(f)
                        added += 1
            elif pt.is_file() and pt.suffix.lower() in ALL_EXTS:
                self.files.append(pt)
                added += 1
        self._refresh()
        if added:
            self.log_write(f"已添加 {added} 个文件")
        else:
            msg = "拖入内容里没有识别到支持的文件（支持: 视频 mp4/mkv/avi… 图片 png/jpg/webp… 音频 wav/flac/ape…）"
            self.log_write(f"[提示] {msg}")
            self._debug(f"未识别到支持文件, 拖入路径: {paths}")
            # 导入内容里包含哪些类型, 就提醒设置哪个目录
            kinds = set()
            for f in self.files:
                e = f.suffix.lower()
                kinds.add("video" if e in VIDEO_EXTS else "image" if e in IMAGE_EXTS else "audio")
            need = {"image": (self.out_img, "图片"), "video": (self.out_vid, "视频"), "audio": (self.out_aud, "音频")}
            tips = [label for k, (var, label) in need.items() if k in kinds and not var.get().strip()]
            if tips:
                self.log_write(f"[提醒] 已导入: {'/'.join(sorted(label + '文件' for label in tips))}，请在第 2 步设置导出文件夹，留空则这一类不会转换")

    def _refresh(self):
        self.listbox.delete(0, "end")
        for f in self.files:
            self.listbox.insert("end", str(f))
        self.count_var.set(f"{len(self.files)} 个文件")

    def remove_selected(self):
        sel = set(self.listbox.curselection())
        self.files = [f for i, f in enumerate(self.files) if i not in sel]
        self._refresh()

    def clear_all(self):
        self.files.clear()
        self._refresh()

    # ---------- 输出 ----------
    def pick_out(self, var):
        p = filedialog.askdirectory(title="选择导出文件夹")
        if p:
            var.set(p)
            self._save_config()

    def open_out(self):
        dirs = [v.get().strip() for v in (self.out_img, self.out_vid, self.out_aud) if v.get().strip()]
        if not dirs:
            messagebox.showinfo("提示", "还没有设置任何导出文件夹")
            return
        for d in dirs:
            os.startfile(d)

    # ---------- 转换 ----------
    def start(self):
        if not self.files:
            messagebox.showinfo("提示", "请先添加要转换的文件")
            return
        if self.running:
            messagebox.showinfo("提示", "正在转换中，请等待完成")
            return
        dirs = {"image": self.out_img.get().strip(),
                "video": self.out_vid.get().strip(),
                "audio": self.out_aud.get().strip()}
        # 按导入内容分类统计
        by_type = {"image": 0, "video": 0, "audio": 0}
        for f in self.files:
            e = f.suffix.lower()
            if e in VIDEO_EXTS:
                by_type["video"] += 1
            elif e in IMAGE_EXTS:
                by_type["image"] += 1
            else:
                by_type["audio"] += 1
        names = {"image": "图片", "video": "视频", "audio": "音频"}
        will = []      # 有目录、会转换的
        missing = []   # 导入了但没设目录、不会转换的
        for k in ("image", "video", "audio"):
            if by_type[k] == 0:
                continue
            if dirs[k]:
                will.append(f"{names[k]} {by_type[k]} 个 → {dirs[k]}")
                self.log_write(f"[导出] {names[k]} {by_type[k]} 个 -> {dirs[k]}")
            else:
                missing.append(f"{names[k]} {by_type[k]} 个（未设置{names[k]}导出文件夹）")
                self.log_write(f"[提示] 检测到 {by_type[k]} 个{names[k]}文件，但未设置{names[k]}导出文件夹，这一类将不转换")
        # 全部类型都没设目录 → 直接拦下
        if not will:
            messagebox.showwarning("无法转换",
                                   "本次导入的文件类型:\n  " + "\n  ".join(
                                       f"{names[k]} {by_type[k]} 个" for k in by_type if by_type[k])
                                   + "\n\n但对应的导出文件夹都没有设置。\n请在第 2 步设置导出文件夹后再开始。")
            return
        # 部分类型没设目录 → 弹窗确认
        if missing:
            msg = ("将转换:\n  " + "\n  ".join(will)
                   + "\n\n以下类型因未设置导出文件夹, 本次不会转换:\n  " + "\n  ".join(missing)
                   + "\n\n是否继续?")
            if not messagebox.askyesno("确认转换", msg):
                self.log_write("== 已取消 ==")
                return
        crf, iq = PRESETS[self.preset_var.get()]
        ab = self.audio_var.get() + "k"
        self._save_config()
        self.running, self.cancel = True, False
        self.go_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.progress.config(value=0, maximum=len(self.files))
        threading.Thread(target=self._worker, args=(list(self.files), dirs, crf, iq, ab), daemon=True).start()

    def stop(self):
        self.cancel = True

    def _worker(self, files, dirs, crf, iq, ab):
        done = ok = fail = copied = skipped_nodir = skipped_missing = skipped_exist = 0
        total = len(files)
        for f in files:
            if self.cancel:
                self.log_q.put("== 已手动停止 ==")
                break
            try:
                if not f.exists():
                    # exists() 偶发误判时重试一次
                    if not f.exists():
                        skipped_missing += 1
                        self.log_q.put(f"[跳过] {f}: 文件不存在（可能已被移动/删除）")
                        continue
                ext = f.suffix.lower()
                if ext in VIDEO_EXTS:
                    kind = "video"
                elif ext in IMAGE_EXTS:
                    kind = "image"
                else:
                    kind = "audio"
                out_dir = dirs.get(kind, "").strip()
                if not out_dir:
                    skipped_nodir += 1
                    continue
                odir = Path(out_dir)
                odir.mkdir(parents=True, exist_ok=True)
                if ext in VIDEO_EXTS:
                    r = self._video(f, odir, crf)
                elif ext in IMAGE_EXTS:
                    r = self._image(f, odir, iq)
                elif ext in AUDIO_LOSSLESS:
                    r = self._audio(f, odir, ab)
                else:  # 已压缩的音频: 重编码只会更糊, 直接把原件复制到导出目录
                    dst = odir / f.name
                    if dst.exists():
                        r = f"[跳过] {f.name}: 已是压缩音频且导出目录已有同名文件，不处理"
                    else:
                        try:
                            shutil.copy2(f, dst)
                            r = f"[复制] {f.name}: 已是压缩音频(mp3/m4a等)不转码，原件已复制到导出目录"
                        except Exception as ce:
                            r = f"[失败] {f.name}: 复制到导出目录出错({ce})"
                    self.log_q.put(r)
                    continue
                self.log_q.put(r)
                if r.startswith("[OK]"):
                    ok += 1
                elif r.startswith("[跳过]"):
                    skipped_exist += 1
                elif r.startswith("[复制]"):
                    copied += 1
                else:
                    fail += 1
            except Exception as e:
                self.log_q.put(f"[失败] {f.name}: {e}")
                fail += 1
            finally:
                done += 1
                self.log_q.put(("__progress__", done))
        extra = ""
        if skipped_nodir:
            extra += f", 未设置导出目录跳过 {skipped_nodir} 个"
        if skipped_missing:
            extra += f", 文件不存在跳过 {skipped_missing} 个"
        if skipped_exist:
            extra += f", 目标已存在跳过 {skipped_exist} 个"
        if copied:
            extra += f", 原件复制 {copied} 个"
        self.log_q.put(f"== 完成: 成功 {ok} 个, 失败 {fail} 个{extra} ==")
        self.log_q.put(("__progress__", total))
        self.log_q.put("__done__")

    def _video(self, src, odir, crf):
        dst = odir / (src.stem + ".mp4")
        if dst.exists():
            return f"[跳过] {src.name} -> {dst.name} 已存在"
        vf = (f"scale='min(1920,iw)':'min({VIDEO_HEIGHT},ih)':"
              f"force_original_aspect_ratio=decrease:force_divisible_by=2:flags=lanczos,"
              f"fps={VIDEO_FPS}")
        r = subprocess.run([FFMPEG, "-y", "-i", str(src), "-vf", vf,
                            "-c:v", "libx264", "-preset", "medium", "-crf", str(crf),
                            "-profile:v", "high", "-pix_fmt", "yuv420p",
                            "-c:a", "aac", "-b:a", "128k",
                            "-movflags", "+faststart", str(dst)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        if r.returncode != 0:
            dst.unlink(missing_ok=True)
            return f"[失败] {src.name}: ffmpeg 编码出错"
        return f"[OK] {src.name} -> {dst.name}  {human(src.stat().st_size)} -> {human(dst.stat().st_size)}"

    def _image(self, src, odir, quality):
        from PIL import Image
        dst = odir / (src.stem + ".jpg")
        if dst.exists():
            return f"[跳过] {src.name} -> {dst.name} 已存在"
        im = Image.open(src)
        w, h = im.size
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            bg = Image.new("RGB", im.size, (255, 255, 255))
            bg.paste(im, mask=im.split()[-1])
            im = bg
        elif im.mode != "RGB":
            im = im.convert("RGB")
        edge = max(w, h)
        if edge > IMAGE_MAX_EDGE:
            s = IMAGE_MAX_EDGE / edge
            im = im.resize((round(w * s), round(h * s)), Image.LANCZOS)
        im.save(dst, "JPEG", quality=quality, optimize=True, progressive=True, subsampling=0)
        return f"[OK] {src.name} {w}x{h} -> {im.size[0]}x{im.size[1]}  {human(src.stat().st_size)} -> {human(dst.stat().st_size)}"

    def _audio(self, src, odir, bitrate):
        dst = odir / (src.stem + ".mp3")
        if dst.exists():
            return f"[跳过] {src.name} -> {dst.name} 已存在"
        r = subprocess.run([FFMPEG, "-y", "-i", str(src), "-vn",
                            "-c:a", "libmp3lame", "-b:a", bitrate, str(dst)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        if r.returncode != 0:
            dst.unlink(missing_ok=True)
            return f"[失败] {src.name}: ffmpeg 编码出错"
        return f"[OK] {src.name} -> {dst.name}  {human(src.stat().st_size)} -> {human(dst.stat().st_size)}"

    # ---------- 日志 ----------
    def _drain_log(self):
        try:
            while True:
                item = self.log_q.get_nowait()
                if item == "__done__":
                    self.go_btn.config(state="normal")
                    self.stop_btn.config(state="disabled")
                    self.running = False
                elif isinstance(item, tuple) and item[0] == "__progress__":
                    self.progress.config(value=item[1])
                else:
                    self.log_write(item)
        except queue.Empty:
            pass
        self.root.after(100, self._drain_log)

    def log_write(self, s):
        self.log.config(state="normal")
        self.log.insert("end", s + "\n")
        self.log.see("end")
        self.log.config(state="disabled")


def main():
    root = TK_ROOT()
    App(root)
    if "--selftest" in sys.argv:
        root.after(1500, root.destroy)
    root.mainloop()


if __name__ == "__main__":
    main()
