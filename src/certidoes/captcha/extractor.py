"""Challenge extractor: iframe bbox -> modal crop -> 3x3 tile split.
No DOM access inside cross-origin iframe; geometry via iframe rect + gap detection.
Page coords returned in CSS viewport px (Playwright mouse space).
"""
from PIL import Image, ImageChops


def clamp_clip(bbox, vp_w=1280, vp_h=900):
    x, y, w, h = bbox["x"], bbox["y"], bbox["w"], bbox["h"]
    x = max(0, int(round(x)))
    y = max(0, int(round(y)))
    w = min(int(round(w)), vp_w - x)
    h = min(int(round(h)), vp_h - y)
    return {"x": x, "y": y, "width": w, "height": h}


def _bright_bands(series, frac=0.55, min_run=4):
    """Indices where value > frac*max, grouped into runs >= min_run."""
    if not series:
        return []
    mx = max(series) or 1
    runs, start = [], None
    for i, v in enumerate(series):
        if v > frac * mx:
            if start is None:
                start = i
        else:
            if start is not None and i - start >= min_run:
                runs.append((start, i))
            start = None
    if start is not None and len(series) - start >= min_run:
        runs.append((start, len(series)))
    return runs


def _snap_dividers_min(gl, W, H, vertical, lo, hi, expected, tol_frac=0.06):
    """Divider columns/rows: bright across the WHOLE span (min-profile peaks).
    vertical=True -> scan x over y in [lo,hi); else scan y over x in [lo,hi)."""
    if vertical:
        prof = [min(gl[x, yy] for yy in range(lo, hi)) for x in range(W)]
        span = hi - lo
    else:
        prof = [min(gl[xx, yy] for xx in range(lo, hi)) for yy in range(H)]
        span = hi - lo
    mx = max(prof) or 1
    runs, start = [], None
    for i, v in enumerate(prof):
        if v > 0.55 * mx:
            if start is None:
                start = i
        else:
            if start is not None and 2 <= i - start <= max(16, span * 0.05):
                runs.append((start, i))
            start = None
    if start is not None and 2 <= len(prof) - start <= max(16, span * 0.05):
        runs.append((start, len(prof)))
    tol = span * tol_frac
    out = []
    for e in expected:
        cands = [(abs((r[0] + r[1]) / 2 - e), r) for r in runs]
        if cands:
            d, r = min(cands)
            if d <= tol:
                out.append(((r[0] + r[1]) / 2, r[1] - r[0]))
    return out


def find_grid(crop: Image.Image):
    """Find 3x3 tile grid inside modal crop. Returns (gx, gy, gw, gh) in crop px.
    Teal header anchors top; white gap dividers snap columns/rows."""
    rgb = crop.convert("RGB")
    W, H = rgb.size
    px = rgb.load()
    # teal header rows: high G+B, low R across a good share of the row.
    # Scan the top 40% and take the LAST teal row: white text lines inside
    # the header must not end the scan early.
    def is_header_row(y):
        n = 0
        step = max(1, W // 60)
        total = 0
        for x in range(0, W, step):
            r, g, b = px[x, y]
            total += 1
            if g > 100 and b > 100 and r < 120:
                n += 1
        return n / max(1, total) > 0.35
    last_teal = -1
    for y in range(0, int(H * 0.4)):
        if is_header_row(y):
            last_teal = y
    gy0 = last_teal + int(W * 0.015) if last_teal > 5 else int(H * 0.16)
    pad = int(W * 0.025)
    gx0, gw0 = pad, W - 2 * pad
    if W > 450:
        # shortcut: the wide canvas has a 90px footer; revisit if hCaptcha changes this layout.
        area = rgb.crop((pad, gy0, W - pad, H - 90))
        background = max(area.getcolors(area.width * area.height))[1]
        diff = ImageChops.difference(area, Image.new("RGB", area.size, background)).convert("L")
        panel = diff.point(lambda value: 255 if value == 0 else 0).getbbox()
        if panel:
            left, top, right, bottom = panel
            panel_diff = diff.crop((left + 3, top + 3, right - 3, bottom - 3))
            content = panel_diff.point(lambda value: 255 if value > 10 else 0).getbbox()
            if content:
                x0, y0, x1, y1 = content
                width, height = x1 - x0, y1 - y0
                if W * 0.4 < width < W * 0.9 and 0.8 < height / width < 1.2:
                    return (pad + left + 3 + x0, gy0 + top + 3 + y0, width, height)
    g = crop.convert("L")
    gl = g.load()
    # vertical dividers: columns bright across whole grid height (min-profile)
    y0, y1 = gy0, min(H, gy0 + gw0)
    dv = _snap_dividers_min(gl, W, H, True, y0, y1,
                            [gx0 + gw0 / 3.0, gx0 + 2 * gw0 / 3.0])
    if len(dv) == 2:
        (d1, w1), (d2, w2) = dv
        pitch = d2 - d1
        gap = (w1 + w2) / 2
        gx = int(round(d1 - pitch + gap / 2))
        cell = pitch - gap
        gw = int(round(3 * cell + 2 * gap))
    else:
        gx, gw, cell = gx0, gw0, gw0 / 3.0
    # horizontal dividers: rows bright across whole grid width
    x0, x1 = gx, min(W, gx + gw)
    dh = _snap_dividers_min(gl, W, H, False, x0, x1,
                            [gy0 + gw / 3.0, gy0 + 2 * gw / 3.0])
    if len(dh) == 2:
        (e1, h1), (e2, h2) = dh
        pitch2 = e2 - e1
        gap2 = (h1 + h2) / 2
        gy = int(round(e1 - pitch2 + gap2 / 2))
        gh = int(round(3 * (pitch2 - gap2) + 2 * gap2))
    else:
        gy, gh = gy0, gw
    gx, gy = max(0, gx), max(0, gy)
    gw = min(gw, W - gx)
    gh = min(gh, H - gy)
    return (gx, gy, gw, gh)


def split_tiles(crop: Image.Image, grid):
    gx, gy, gw, gh = grid
    cw, ch = gw / 3.0, gh / 3.0
    tiles = []
    for r in range(3):
        for c in range(3):
            x0 = int(gx + c * cw)
            y0 = int(gy + r * ch)
            tiles.append(crop.crop((x0, y0, int(x0 + cw), int(y0 + ch))))
    return tiles


def split_header(crop: Image.Image, grid_top: int):
    """Split header into (text_img, ref_img_or_None).
    Reference image (when present) sits as a square in the header's right area;
    detected by non-teal content. Text-only headers return None ref."""
    rgb = crop.convert("RGB")
    W, _ = rgb.size
    header = crop.crop((0, 0, crop.width, grid_top))
    hw = header.height
    # candidate ref square: right side, side = header height
    side = min(hw, W // 3)
    rx0 = W - side
    px = rgb.load()
    content, total, step = 0, 0, max(1, side // 24)
    for y in range(0, hw, step):
        for x in range(rx0, W, step):
            r, g, b = px[x, y]
            total += 1
            teal = g > 100 and b > 100 and r < 120
            bright = max(r, g, b) >= 200  # white text / light chrome
            if not teal and not bright:
                content += 1
    if total and content / total > 0.15:
        ref = crop.crop((rx0, 0, W, hw))
        text = crop.crop((0, 0, rx0, grid_top))
        return text, ref
    return header, None


def tile_centers_page(bbox, clip, grid):
    """Center of each tile in page CSS px. clip = clamped screenshot clip."""
    gx, gy, gw, gh = grid
    scale_x = clip["width"] / clip.get("shot_w", clip["width"])
    centers = []
    for r in range(3):
        for c in range(3):
            # crop px -> page css px (dsf=1 assumed; corrected by caller if needed)
            px = clip["x"] + gx + (c + 0.5) * (gw / 3.0)
            py = clip["y"] + gy + (r + 0.5) * (gh / 3.0)
            centers.append((round(px, 1), round(py, 1)))
    return centers
