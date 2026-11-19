#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
设计调性引擎 · 文字后期处理器 (do_text)

把一个核心认知落成代码：设计师的字不是"贴上去的黑体"，
而是「做旧喷漆 + 环境光受光 + 关键词变色」的"图形"。

典型用法（在 AI 生成的【无文字底图】上叠字）:

    python3 do_text.py \
        --bg 底图.png \
        --text "目标你来定" \
        --pos 148,200 \
        --size 180 \
        --light "#FF5533" \
        --accent "目标" --accent-color "#FF3B30" \
        --grain 0.35 \
        --out 成品.png

多行文字: --text 里用 \n 或 | 分隔，如 "主标题|副标题小字"，配合 --align center。
光源统一: --light-angle 决定【受光渐变方向 + 投影方向】，两者永远指向同一盏灯。

参数说明:
  --bg          背景底图（无文字），不给则只输出带透明底的字图层
  --text        要写的文字；多行用 \n 或 | 分隔
  --pos         文字左上角 x,y（逗号分隔，默认 0,0）
  --size        字号（px，默认 180）
  --weight      字重: Black/Bold/Medium/Regular/Light，默认 Black
                （集合字体里没有对应字重时自动降一级，如 PingFang 无 Black 退到 Semibold）
  --font        直接指定字体文件路径，覆盖自动查找；也可用环境变量 DO_TEXT_FONT
  --color       正文颜色 hex（默认白 #FFFFFF）
  --light       主光源色 hex（决定文字受光色 + 投影暖色），默认 #FF5533
  --light-angle 光源方向 top（默认）/ left / right；受光渐变与投影方向统一跟随
  --accent      关键词（text 的子串，可逗号分隔多个），单独上色
  --accent-color 关键词颜色 hex（默认 #FF3B30）
  --grain       做旧磨损强度 0~0.6（0=干净，0.5=重度，默认 0.15；>0.5 自动警告）
                日常/生活类封面用 0~0.15，街头促销才上 0.4~0.5
  --spacing     字距 px（默认 0）
  --line-gap    多行行距 px（默认 = 字号 * 0.15）
  --align       多行对齐 left/center/right（默认 left）
  --shadow      投影开/关 1/0（默认 1）
  --seed        随机种子（复现用，默认随机）
  --out         输出路径
"""
import argparse
import glob
import os
import re
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

GRAIN_MAX = 0.6

# Noto 单文件命名，Linux 发行版常见位置
_NOTO_DIRS = (
    "/usr/share/fonts/opentype/noto",
    "/usr/share/fonts/truetype/noto",
    "/usr/local/share/fonts",
    "/opt/homebrew/share/fonts",
    "/System/Library/Fonts",
    "/Library/Fonts",
    "~/Library/Fonts",
)

# macOS 上装了 Noto 就用 Noto，没装就在这些集合字体里挑
# PingFang 不在标准路径（在 AssetsV2 哈希目录），单独用 glob 找
_MULTI_FACE_FONTS = (
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/System/Library/Fonts/Supplemental/Songti.ttc",
)

# 字重降级链：前面的优先级高。W3/W6 是冬青黑体的写法，
# PingFang 最高只到 Semibold，所以 Black 要能落到 Semibold 或 W6。
_WEIGHT_FALLBACK = {
    "Black": ("Black", "Heavy", "Bold", "Semibold", "W6", "Medium", "Regular", "W3"),
    "Bold": ("Bold", "Semibold", "W6", "Black", "Heavy", "Medium", "Regular", "W3"),
    "Medium": ("Medium", "W6", "Semibold", "Regular", "W3"),
    "Regular": ("Regular", "W3", "Medium", "Light"),
    "Light": ("Light", "W3", "Thin", "Regular", "Ultralight"),
}

# 只认简体中文 family，避免在 .ttc 里挑到 TC/HK/MO/JP
_CN_INCLUDE = ("SC", "GB", "CN", "简体")
_CN_EXCLUDE = ("TC", "HK", "MO", "JP", "KR")


def _faces(path):
    """列出 .ttc 里每个 face 的 (index, family, style)。"""
    try:
        from fontTools.ttLib import TTCollection
        out = []
        for i, f in enumerate(TTCollection(path).fonts):
            name = f["name"]
            out.append((i, name.getDebugName(1) or "", name.getDebugName(2) or ""))
        return out
    except Exception:
        return []


def _has_fonttools():
    try:
        import fontTools  # noqa: F401
        return True
    except ImportError:
        return False


def _is_simplified(family):
    fam = (family or "").strip()
    if not fam or fam.startswith("."):
        # “.Hiragino Sans GB Interface W3” 这类私有字体不给外部用
        return False
    up = fam.upper()
    if any(x in up for x in _CN_EXCLUDE):
        return False
    return any(x in up for x in _CN_INCLUDE)


def _select_index(path, weight):
    """在集合字体里挑简体中文 + 最接近目标字重的 face。"""
    faces = _faces(path)
    if not faces:
        return 0
    if len(faces) == 1:
        return faces[0][0]

    cands = [f for f in faces if _is_simplified(f[1])] or faces
    for target in _WEIGHT_FALLBACK.get(weight, (weight, "Regular")):
        low = target.lower()
        for idx, _fam, sty in cands:
            if (sty or "").lower() == low:
                return idx
        for idx, fam, sty in cands:
            if low in (sty or "").lower() or low in (fam or "").lower():
                return idx
    return cands[0][0]


def _find_font(weight, explicit=""):
    """按优先级找一个能用的中文字体，返回 (路径, face 索引)。

    顺序：显式指定 → 环境变量 → Noto 单文件 → PingFang → macOS 集合字体 → 用户目录。
    """
    if explicit:
        path = os.path.expanduser(explicit)
        if not os.path.exists(path):
            sys.exit("指定的字体不存在: %s" % path)
        return path, _select_index(path, weight)

    env = os.environ.get("DO_TEXT_FONT", "").strip()
    if env:
        path = os.path.expanduser(env)
        if os.path.exists(path):
            return path, _select_index(path, weight)

    # 没有 fontTools 就看不到 .ttc 里的 face 列表，猜一个 index 会静默选到
    # 繁体字形（PingFang index 0 是 HK）。宁可报错，不给错字。
    if not _has_fonttools():
        sys.exit(
            "缺少 fontTools，无法在 .ttc 集合里定位简体字重。\n"
            "  pip install fonttools\n"
            "或改用 --font 直接指定字体文件（如 .ttf 单字体）"
        )

    for d in _NOTO_DIRS:
        path = os.path.join(os.path.expanduser(d), "NotoSansCJK-%s.ttc" % weight)
        if os.path.exists(path):
            return path, _select_index(path, weight)

    # PingFang 装在带哈希的资产目录里，路径随系统更新变化，只能搜
    for path in glob.glob(
        "/System/Library/AssetsV2/com_apple_MobileAsset_Font*/*/AssetData/PingFang.ttc"
    ):
        return path, _select_index(path, weight)

    for path in _MULTI_FACE_FONTS:
        if os.path.exists(path):
            return path, _select_index(path, weight)

    # 用户自己丢进字体目录的兜底
    for d in ("~/Library/Fonts", "~/.fonts", "~/.local/share/fonts"):
        for path in sorted(glob.glob(os.path.join(os.path.expanduser(d), "*.tt*"))):
            if _faces(path):
                return path, _select_index(path, weight)

    sys.exit(
        "找不到可用的中文字体。\n"
        "用 --font 指定字体文件，或设置环境变量 DO_TEXT_FONT。\n"
        "已尝试: Noto Sans CJK、PingFang、%s" % ", ".join(_MULTI_FACE_FONTS)
    )


def load_font(weight, size, explicit=""):
    path, index = _find_font(weight, explicit)
    # 集字体里字重可能降级，打出来免得误以为用的是 Black
    label = os.path.basename(path)
    for idx, fam, sty in _faces(path):
        if idx == index:
            label = ("%s %s" % (fam, sty)).strip()
            break
    print("字体: %s" % label, file=sys.stderr)
    return ImageFont.truetype(path, size, index=index)


def hex_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _rgb4(rgb):
    return tuple(list(rgb) + [255])


def render_line(text, font, color, accent_map, spacing):
    """逐字渲染一行文字，关键词单独上色，返回 RGBA 图层（透明底）。"""
    d = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    asc, desc = font.getmetrics()
    total_h = asc + desc + 12

    # 先算整行宽（文字逐个 textlength 累加，加字距）
    total_w = int(sum(d.textlength(c, font=font) for c in text)
                  + spacing * max(len(text) - 1, 0))

    img = Image.new("RGBA", (total_w + 8, total_h + 8), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    x = 4
    y = 4
    i = 0
    while i < len(text):
        # 先看当前下标是否命中某个关键词
        matched = None
        for word, col in accent_map.items():
            if text.startswith(word, i):
                matched = (word, col)
                break
        if matched:
            word, col = matched
            draw.text((x, y), word, font=font, fill=col)
            x += int(d.textlength(word, font=font)) + spacing
            i += len(word)
        else:
            c = text[i]
            draw.text((x, y), c, font=font, fill=color)
            x += int(d.textlength(c, font=font)) + spacing
            i += 1
    return img


def build_accent_map(text, accent_str, accent_color):
    """把 '--accent 目标,小安' 解析成 {词: 颜色tuple(RGBA)}。"""
    m = {}
    if accent_str:
        for w in accent_str.split(","):
            w = w.strip()
            if w and w in text:
                m[w] = _rgb4(hex_rgb(accent_color))
    return m


def render_text_block(lines, font, color, accent_map, spacing, size, line_gap, align):
    """渲染多行文字块：逐行渲染后垂直堆叠，支持 left/center/right 对齐。"""
    layers = [render_line(ln, font, color, accent_map, spacing) for ln in lines]
    max_w = max(l.width for l in layers)
    if line_gap is None:
        line_gap = int(size * 0.15)
    total_h = sum(l.height for l in layers) + line_gap * (len(layers) - 1)
    block = Image.new("RGBA", (max_w, total_h), (0, 0, 0, 0))
    y = 0
    for l in layers:
        if align == "center":
            x = (max_w - l.width) // 2
        elif align == "right":
            x = max_w - l.width
        else:
            x = 0
        block.alpha_composite(l, (x, y))
        y += l.height + line_gap
    return block


def _blob_noise(shape, scale, rng):
    """低频斑块噪声：小图随机后双线性放大，得到连续斑块而非逐像素散点。"""
    h, w = shape
    gh, gw = max(2, h // scale), max(2, w // scale)
    small = (rng.random((gh, gw)) * 255).astype(np.uint8)
    up = Image.fromarray(small).resize((w, h), Image.BICUBIC)
    n = np.asarray(up, dtype=np.float32) / 255.0
    lo, hi = float(n.min()), float(n.max())
    return (n - lo) / max(hi - lo, 1e-6)


def aging(alpha, grain, seed):
    """做旧飞白：低频斑块磨损，模拟颜料剥落（不是逐像素椒盐噪点）。

    三级：大块磨穿 → 中块减淡 → 细颗粒毛边。grain 控制磨损面积占比。
    逐像素随机会在字面上打出密集白点，看起来像脏污而非做旧，故用放大的
    低频噪声代替。
    """
    if grain <= 0:
        return alpha
    rng = np.random.default_rng(seed)
    h, w = alpha.shape
    a = alpha.astype(np.float32)

    # 大块磨穿：露出底图
    coarse = _blob_noise((h, w), 12, rng)
    a = np.where(coarse < grain * 0.50, a * 0.10, a)

    # 中块减淡：制造不均匀的旧感
    mid = _blob_noise((h, w), 5, rng)
    a = a * np.where(mid < grain * 0.55, 0.62, 1.0).astype(np.float32)

    # 细颗粒：只让笔画边缘略毛糙
    fine = _blob_noise((h, w), 2, rng)
    a = a * np.where(fine < grain * 0.30, 0.90, 1.0).astype(np.float32)

    return np.clip(a, 0, 255)


def light_gradient(rgb, light_rgb, direction="top", top=1.22, bot=0.68, tint=0.30):
    """受光渐变：光源方向决定渐变轴（顶部/左侧/右侧打亮），对侧压暗。

    与投影方向共用同一 direction，保证「光从哪来」全图一致。
    """
    h, w = rgb.shape[:2]
    rgb = rgb.astype(np.float32)
    lt = np.array(light_rgb, dtype=np.float32).reshape(1, 1, 3)
    if direction == "left":
        L = np.linspace(top, bot, w).reshape(1, w, 1).astype(np.float32)
        T = np.linspace(tint, 0.0, w).reshape(1, w, 1).astype(np.float32)
    elif direction == "right":
        L = np.linspace(bot, top, w).reshape(1, w, 1).astype(np.float32)
        T = np.linspace(0.0, tint, w).reshape(1, w, 1).astype(np.float32)
    else:  # top
        L = np.linspace(top, bot, h).reshape(h, 1, 1).astype(np.float32)
        T = np.linspace(tint, 0.0, h).reshape(h, 1, 1).astype(np.float32)
    lit = rgb * L + lt * T * 0.5
    return np.clip(lit, 0, 255).astype(np.uint8)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bg")
    ap.add_argument("--text", required=True)
    ap.add_argument("--pos", default="0,0")
    ap.add_argument("--size", type=int, default=180)
    ap.add_argument("--weight", default="Black",
                    choices=["Black", "Bold", "Medium", "Regular", "Light"])
    ap.add_argument("--font", default="", help="字体文件路径，覆盖自动查找")
    ap.add_argument("--color", default="#FFFFFF")
    ap.add_argument("--light", default="#FF5533")
    ap.add_argument("--light-angle", default="top", choices=["top", "left", "right"])
    ap.add_argument("--accent", default="")
    ap.add_argument("--accent-color", default="#FF3B30")
    ap.add_argument("--grain", type=float, default=0.15)
    ap.add_argument("--spacing", type=int, default=0)
    ap.add_argument("--line-gap", type=int, default=None)
    ap.add_argument("--align", default="left", choices=["left", "center", "right"])
    ap.add_argument("--shadow", type=int, default=1)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    # 做旧强度钳制上限，防重度磨损毁字
    args.grain = max(0.0, min(GRAIN_MAX, args.grain))
    if args.grain > 0.5:
        print("警告: --grain > 0.5 为重度做旧，文字可能被磨损到难以辨认", file=sys.stderr)

    font = load_font(args.weight, args.size, args.font)
    color = _rgb4(hex_rgb(args.color))
    accent_map = build_accent_map(args.text, args.accent, args.accent_color)

    # 多行：\n（字面量或真实换行）或 | 分隔
    lines = [ln for ln in re.split(r"\n|\|", args.text.replace("\\n", "\n")) if ln]
    text_layer = render_text_block(lines, font, color, accent_map, args.spacing,
                                   args.size, args.line_gap, args.align)
    arr = np.array(text_layer)  # h,w,4
    a = arr[..., 3]
    rgb = arr[..., :3]

    # 1) 做旧
    a2 = aging(a, args.grain, args.seed)

    # 2) 受光（方向与投影统一跟随 --light-angle）
    light_rgb = hex_rgb(args.light)
    rgb2 = light_gradient(rgb, light_rgb, direction=args.light_angle)

    # 3) 合成回透明图层
    out_arr = np.dstack([rgb2, a2.astype(np.uint8)])
    final = Image.fromarray(out_arr, "RGBA")

    # 背景合成 + 投影
    if args.bg:
        bg = Image.open(args.bg).convert("RGBA")
        # 光源方向 -> 投影偏移
        dx, dy = 0, 0
        if args.light_angle == "top":
            dx, dy = int(args.size * 0.03), int(args.size * 0.05)
        elif args.light_angle == "left":
            dx, dy = -int(args.size * 0.05), int(args.size * 0.03)
        elif args.light_angle == "right":
            dx, dy = int(args.size * 0.05), int(args.size * 0.03)

        if args.shadow:
            alpha = text_layer.split()[3]
            shadow = alpha.filter(ImageFilter.GaussianBlur(args.size * 0.02))
            shadow = shadow.point(lambda p: int(p * 0.5))
            sc = tuple(int(c * 0.35) for c in light_rgb) + (0,)
            sh_layer = Image.new("RGBA", text_layer.size, sc)
            sh_layer.putalpha(shadow)

        # 文字定位
        try:
            px, py = [int(v) for v in args.pos.split(",")]
        except Exception:
            px, py = 0, 0

        canvas = bg.copy()
        if args.shadow:
            canvas.alpha_composite(sh_layer, (px + dx, py + dy))
        canvas.alpha_composite(final, (px, py))
        canvas = canvas.convert("RGB")
    else:
        canvas = final

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    canvas.save(args.out)
    print("已生成: %s" % args.out)


if __name__ == "__main__":
    main()
