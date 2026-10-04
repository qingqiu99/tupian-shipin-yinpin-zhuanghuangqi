# -*- coding: utf-8 -*-
"""
视频图片批量转换器（省空间 + 保清晰度）
- 视频: H.264 + AAC, MP4, 1080p, 30fps
- 图片: JPEG, 最长边 1600px, 质量 78

用法:
  1. 双击 转换器.bat，把文件/文件夹拖进窗口
  2. 或命令行: python convert.py 文件或文件夹 [更多文件或文件夹...]
输出统一放在源文件同级的 converted/ 文件夹里（已存在同名文件会跳过）。
"""
import os
import subprocess
import sys
from pathlib import Path

# ================== 可调参数 ==================
FFMPEG = r"E:\ffmpeg-master-latest-win64-gpl-shared\bin\ffmpeg.exe"  # 找不到会回退到 PATH 里的 ffmpeg

# --- 视频 ---
VIDEO_CRF = 21        # 质量: 18~23, 越小越清晰越大。21 是"清晰优先省空间"的甜点值
VIDEO_PRESET = "medium"  # slow 更省 3~5% 体积但慢一倍; 求快可改 veryfast
VIDEO_FPS = 30        # 目标帧率(源帧率更低时不补帧也无妨, fps=30 会统一为30)
VIDEO_HEIGHT = 1080   # 目标分辨率(短边超1080才缩, 不会放大)
AUDIO_BITRATE = "128k"   # 人声/普通内容 128k 足够; 音乐类可改 192k

# --- 图片 ---
IMAGE_MAX_EDGE = 1600   # 最长边, 超出才缩, 不会放大
IMAGE_QUALITY = 78      # JPEG 质量 0~100
IMAGE_SUBSAMPLING = 0   # 0=4:4:4 最清晰(文字/截图推荐); 2=4:2:0 体积再省15~20%(纯照片可用)
# =============================================

VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".flv", ".wmv", ".webm", ".ts", ".m4v", ".mpg", ".mpeg"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}


def convert_video(src: Path, out_dir: Path):
    dst = out_dir / (src.stem + ".mp4")
    if dst.exists():
        print(f"[跳过] 已存在: {dst.name}")
        return
    vf = (
        f"scale='min(1920,iw)':'min({VIDEO_HEIGHT},ih)':"
        f"force_original_aspect_ratio=decrease:force_divisible_by=2:flags=lanczos,"
        f"fps={VIDEO_FPS}"
    )
    cmd = [
        FFMPEG, "-y", "-i", str(src),
        "-vf", vf,
        "-c:v", "libx264", "-preset", VIDEO_PRESET, "-crf", str(VIDEO_CRF),
        "-profile:v", "high", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", AUDIO_BITRATE,
        "-movflags", "+faststart",
        "-map_metadata", "0",
        str(dst),
    ]
    print(f"[视频] {src.name} -> {dst.name}")
    r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        dst.unlink(missing_ok=True)
        print(f"  [失败] {r.stderr.strip().splitlines()[-1] if r.stderr.strip() else '未知错误'}")
    else:
        old, new = src.stat().st_size, dst.stat().st_size
        print(f"  {old/1048576:.1f}MB -> {new/1048576:.1f}MB ({new/old*100:.0f}%)")


def convert_image(src: Path, out_dir: Path):
    from PIL import Image
    dst = out_dir / (src.stem + ".jpg")
    if dst.exists():
        print(f"[跳过] 已存在: {dst.name}")
        return
    try:
        im = Image.open(src)
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            bg = Image.new("RGB", im.size, (255, 255, 255))  # 透明区域铺白底
            bg.paste(im, mask=im.split()[-1])
            im = bg
        elif im.mode != "RGB":
            im = im.convert("RGB")
        w, h = im.size
        edge = max(w, h)
        if edge > IMAGE_MAX_EDGE:
            scale = IMAGE_MAX_EDGE / edge
            im = im.resize((round(w * scale), round(h * scale)), Image.LANCZOS)
        im.save(dst, "JPEG", quality=IMAGE_QUALITY, optimize=True, progressive=True, subsampling=IMAGE_SUBSAMPLING)
        old, new = src.stat().st_size, dst.stat().st_size
        print(f"[图片] {src.name} {w}x{h} -> {im.size[0]}x{im.size[1]}  {old/1024:.0f}KB -> {new/1024:.0f}KB ({new/old*100:.0f}%)")
    except Exception as e:
        print(f"  [失败] {src.name}: {e}")


def collect(args):
    files = []
    for a in args:
        p = Path(a)
        if p.is_dir():
            files += [f for f in p.rglob("*") if f.suffix.lower() in VIDEO_EXTS | IMAGE_EXTS]
        elif p.is_file() and p.suffix.lower() in VIDEO_EXTS | IMAGE_EXTS:
            files.append(p)
    return files


def main():
    global FFMPEG
    args = sys.argv[1:]
    if not args:
        print("把要转换的文件或文件夹拖到本窗口后回车（多个用空格分隔，带空格的路径加英文引号）:")
        line = input("> ").strip()
        args = [x.strip('"') for x in line.split('" "') if x.strip()] or line.split()
    if not args:
        print("没有输入任何路径。")
        return
    if not Path(FFMPEG).exists():
        FFMPEG = "ffmpeg"  # 回退到 PATH
    files = collect(args)
    if not files:
        print("未找到可转换的视频/图片文件。")
        return
    print(f"共 {len(files)} 个文件\n")
    for f in files:
        out_dir = f.parent / "converted"
        out_dir.mkdir(exist_ok=True)
        if f.suffix.lower() in VIDEO_EXTS:
            convert_video(f, out_dir)
        else:
            convert_image(f, out_dir)
    print("\n全部完成，输出在各源目录的 converted/ 文件夹。")
    try:
        input("按回车退出...")
    except EOFError:
        pass


if __name__ == "__main__":
    main()
