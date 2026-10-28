"""Capture and solve one 3x3 hCaptcha image challenge on a Playwright page."""
import io
import os
import random
import re
import sys
import time

from PIL import Image, ImageChops, ImageStat

from .extractor import clamp_clip, find_grid, split_header, split_tiles, tile_centers_page
from .solver import solve, solve_drag


IFRAME_JS = """() => {
  const out = [];
  document.querySelectorAll('iframe').forEach((f) => {
    const r = f.getBoundingClientRect();
    if (!(f.src || '').includes('hcaptcha')) return;
    const style = getComputedStyle(f);
    const visible = r.width > 50 && r.height > 50 && r.right > 0 && r.bottom > 0
      && r.left < innerWidth && r.top < innerHeight
      && style.display !== 'none' && style.visibility !== 'hidden' && Number(style.opacity) > 0;
    out.push({x: r.x, y: r.y, w: r.width, h: r.height, visible,
              challenge: f.src.includes('frame=challenge')});
  });
  return out;
}"""
STATE_JS = """(successSelector) => ({
  tok: ((document.querySelector('[name="h-captcha-response"]') || {value: ''}).value || '').length,
  done: !!(successSelector && document.querySelector(successSelector)),
  frames: document.querySelectorAll('iframe[src*="hcaptcha"]').length
})"""
VIEWPORT_JS = "() => ({width: innerWidth, height: innerHeight})"


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def page_state(page, success_selector=None):
    try:
        return page.evaluate(STATE_JS, success_selector)
    except Exception:
        return {"tok": 0, "done": False, "frames": -1, "ctx_dead": True}


def vis_modal(page):
    return [x for x in page.evaluate(IFRAME_JS) if x["visible"] and x["challenge"]]


def captcha_state(page, submitted_token=""):
    try:
        frames = page.evaluate(IFRAME_JS)
        state = page.evaluate("""() => ({
          ready: !!window.hcaptcha && typeof window.hcaptcha.getResponse === 'function',
          responses: [...new Set([...document.querySelectorAll('[data-hcaptcha-widget-id]')]
            .map(e => e.getAttribute('data-hcaptcha-widget-id')).filter(Boolean))].map(id => {
              try { return window.hcaptcha?.getResponse(id) || ''; } catch { return ''; }
            })
        })""")
        tokens = state["responses"]
        return {"api_ready": state["ready"], "frames": frames,
                "widget_token_lengths": [len(token) for token in tokens],
                "submitted_token_length": len(submitted_token),
                "submitted_matches_widgets": [bool(submitted_token) and token == submitted_token for token in tokens]}
    except Exception as error:
        return {"probe_error": type(error).__name__}


def modal_clip(page, box):
    viewport = page.evaluate(VIEWPORT_JS)
    return clamp_clip(box, viewport["width"], viewport["height"])


def click(page, x, y):
    page.mouse.move(x + random.uniform(-3, 3), y + random.uniform(-3, 3), steps=12)
    page.wait_for_timeout(random.randint(150, 250))
    page.mouse.click(x, y)
    page.wait_for_timeout(random.randint(250, 400))


def wait_modal(page, timeout_s=5):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        m = vis_modal(page)
        if m:
            return m
        page.wait_for_timeout(500)
    return []


def shot(page, path, clip, tries=2):
    """Screenshot with a fresh iframe clip after a failed attempt."""
    last = None
    for attempt in range(tries):
        try:
            page.screenshot(path=path, clip=clip, timeout=8000)
            return clip
        except Exception as e:
            last = e
            if attempt + 1 < tries:
                page.wait_for_timeout(500)
                boxes = vis_modal(page)
                if not boxes:
                    break
                clip = modal_clip(page, boxes[0])
    log(f"screenshot failed: {str(last)[:120]}")
    return None


def grid_difference(before, after, grid):
    if before.size != after.size:
        return 0
    gx, gy, gw, gh = grid
    area = (gx, gy, gx + gw, gy + gh)
    diff = ImageChops.difference(before.crop(area).convert("RGB"),
                                 after.crop(area).convert("RGB"))
    return sum(ImageStat.Stat(diff).mean) / 3


def tiles_ready(crop, grid):
    # Both the compact and canvas layouts use nearly uniform placeholders.
    stats = (ImageStat.Stat(tile.convert("RGB")) for tile in split_tiles(crop, grid))
    return all(max(stat.stddev) >= 2
               for stat in stats)


def drag_ready(crop):
    # shortcut: these bounds match the observed 520px canvas; revisit for other drag layouts.
    scene = crop.crop((10, 130, crop.width - 10, crop.height - 90)).convert("RGB")
    return max(ImageStat.Stat(scene).stddev) > 2


def is_drag_challenge(page, clip):
    for frame in page.frames:
        if "hcaptcha" not in frame.url:
            continue
        box = frame.frame_element().bounding_box()
        if box and abs(box["x"] - clip["x"]) < 3 and abs(box["y"] - clip["y"]) < 3:
            prompt = frame.evaluate("() => document.querySelector('.challenge-prompt')?.innerText || ''")
            return bool(re.search(r"\b(drag|arrast\w*)\b", prompt, re.I))
    return False


def solve_round(page, out_dir, rnd, model, reasoning_effort=None,
                success_selector=None):
    """One extract->solve->click round. Returns (done_bool, info_dict)."""
    info = {"round": rnd}
    t0 = time.monotonic()
    boxes = wait_modal(page)
    info["modal_wait_s"] = round(time.monotonic() - t0, 1)
    if not boxes:
        return False, {**info, "why": "no-modal"}
    clip = modal_clip(page, boxes[0])
    dragging = is_drag_challenge(page, clip)
    info["task"] = "drag" if dragging else "grid"
    shot_path = os.path.join(out_dir, f"r{rnd}-modal.png")
    t0 = time.monotonic()
    deadline = t0 + 8
    while True:
        clip = shot(page, shot_path, clip)
        if not clip:
            return False, {**info, "why": "shot-fail"}
        crop = Image.open(shot_path).copy()
        grid = (0, 0, crop.width, crop.height) if dragging else find_grid(crop)
        ready = drag_ready(crop) if dragging else tiles_ready(crop, grid)
        if ready:
            break
        if time.monotonic() >= deadline:
            return False, {**info, "why": "tiles-not-ready"}
        page.wait_for_timeout(300)
    info["capture_s"] = round(time.monotonic() - t0, 1)
    t0 = time.monotonic()
    if not dragging:
        text_img, ref_img = split_header(crop, grid[1])
        text_img.save(os.path.join(out_dir, f"r{rnd}-text.png"))
        info["has_ref"] = ref_img is not None
        if ref_img is not None:
            ref_img.save(os.path.join(out_dir, f"r{rnd}-ref.png"))
        tiles = split_tiles(crop, grid)
        for i, t in enumerate(tiles):
            t.save(os.path.join(out_dir, f"r{rnd}-tile{i+1}.png"))
    info["extract_s"] = round(time.monotonic() - t0, 1)
    t0 = time.monotonic()
    usage = {}
    try:
        idx = (solve_drag(crop, model, reasoning_effort=reasoning_effort, usage=usage)
               if dragging else solve(text_img, tiles, model, ref_img=ref_img,
                                      reasoning_effort=reasoning_effort, usage=usage))
    except Exception as e:
        info["solve_s"] = round(time.monotonic() - t0, 1)
        info["solve_error"] = str(e)[:200]
        if usage:
            info["model_usage"] = usage
        return False, info
    if usage:
        info["model_usage"] = usage
    info["model_drag" if dragging else "model_picks"] = idx
    info["solve_s"] = round(time.monotonic() - t0, 1)
    if not idx:
        return False, {**info, "why": "empty-picks"}
    t0 = time.monotonic()
    if dragging:
        def coordinates(point):
            return (clip["x"] + point[0] / 1000 * clip["width"],
                    clip["y"] + point[1] / 1000 * clip["height"])

        start, end = coordinates(idx["start"]), coordinates(idx["end"])
        page.mouse.move(*start, steps=12)
        page.mouse.down()
        try:
            page.wait_for_timeout(250)
            page.mouse.move(*end, steps=30)
            page.wait_for_timeout(250)
        finally:
            page.mouse.up()
    for n in ([] if dragging else idx):
        boxes = wait_modal(page, timeout_s=10)
        if not boxes:
            info["stopped_at"] = n
            break
        clip = modal_clip(page, boxes[0])
        if (clip["width"], clip["height"]) != crop.size:
            fresh = os.path.join(out_dir, f"r{rnd}-geometry.png")
            clip = shot(page, fresh, clip)
            if not clip:
                info["stopped_at"] = n
                break
            crop = Image.open(fresh)
            grid = find_grid(crop)
        centers = tile_centers_page(boxes[0], clip, grid)
        x, y = centers[n - 1]
        click(page, x, y)
    if "stopped_at" in info:
        return False, {**info, "why": "interrupted-clicks"}
    boxes = vis_modal(page)
    if boxes:
        clip = modal_clip(page, boxes[0])
        ax = clip["x"] + clip["width"] - 58
        ay = clip["y"] + clip["height"] - 38
        # settle: let widget register all toggles before submitting (race fix)
        page.wait_for_timeout(2500)
        click(page, ax, ay)
        info["submitted"] = True
    else:
        info["submitted"] = False
    info["click_s"] = round(time.monotonic() - t0, 1)
    # poll: challenge verdict (token / caller success stage / new round) up to 15s
    t0 = time.monotonic()
    st = page_state(page, success_selector)
    deadline = time.monotonic() + 15
    candidate = None
    while time.monotonic() < deadline and not (st["tok"] > 0 or st["done"]):
        page.wait_for_timeout(500)
        st = page_state(page, success_selector)
        if st["tok"] > 0 or st["done"]:
            info["next_round_ready"] = True
            break
        try:
            boxes = vis_modal(page)
            if not boxes:
                continue
            fresh = Image.open(io.BytesIO(page.screenshot(
                clip=modal_clip(page, boxes[0]), timeout=2500)))
            # shortcut: full-grid changes mark a new round; use frame state if tiles refresh individually.
            if grid_difference(crop, fresh, grid) > 40:
                if candidate is not None and grid_difference(candidate, fresh, grid) < 10:
                    info["next_round_ready"] = True
                    break
                candidate = fresh
            else:
                candidate = None
        except Exception as e:
            info.setdefault("verdict_probe_error", str(e)[:120])
    info["state_after"] = st
    info["verdict_s"] = round(time.monotonic() - t0, 1)
    return bool(st["tok"] > 0 or st["done"]), info
