"""OpenRouter vision solver: header crop + 9 tiles -> tile indices to click.
Reads OPENROUTER_API_KEY from env. Model passed via --model (user benchmarks).
"""
import base64
import io
import json
import os
import re

import requests
from PIL import Image

API = "https://openrouter.ai/api/v1/chat/completions"


def _b64(img: Image.Image, scale=2) -> str:
    if scale != 1:
        img = img.resize((img.width * scale, img.height * scale), Image.LANCZOS)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=90)
    return base64.b64encode(buf.getvalue()).decode()


PROMPT = (
    "Solve the image challenge by reading the task header. Each grid image is "
    "explicitly labelled Tile 1 through Tile 9 in row-major order. Reply ONLY "
    "with a JSON array of the labels of ALL matching tiles, e.g. [1,5,6,7]. "
    "Never count the header or reference as a tile. Inspect the objects, not watermarks."
)


PROMPT_REF = (
    PROMPT + " Use the separately labelled Reference image to interpret the task."
)


def _completion(content, model, timeout, reasoning_effort, usage):
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY not set")
    # Avoid reserving credits for the provider's 65k-token output default.
    body = {"model": model, "messages": [{"role": "user", "content": content}],
            "max_tokens": 4096, "usage": {"include": True}}
    if reasoning_effort:
        body["reasoning"] = {"effort": reasoning_effort}
    response = requests.post(API, headers={"Authorization": f"Bearer {key}",
                                          "Content-Type": "application/json"},
                             json=body, timeout=timeout)
    response.raise_for_status()
    result = response.json()
    if usage is not None:
        usage.update(result.get("usage") or {})
    return result["choices"][0]["message"]["content"]


def solve(header_img: Image.Image, tiles, model: str, timeout=90, ref_img=None,
          reasoning_effort=None, usage=None) -> list[int]:
    if ref_img is None:
        content = [{"type": "text", "text": PROMPT},
                   {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + _b64(header_img)}},
                   {"type": "text", "text": "Tiles 1-9:"}]
    else:
        content = [{"type": "text", "text": PROMPT_REF},
                   {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + _b64(header_img)}},
                   {"type": "text", "text": "Reference item:"},
                   {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + _b64(ref_img)}},
                   {"type": "text", "text": "Tiles (numbered 1-9 row-major in reply):"}]
    for number, t in enumerate(tiles, 1):
        content.append({"type": "text", "text": f"Tile {number}:"})
        content.append({"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + _b64(t)}})
    text = _completion(content, model, timeout, reasoning_effort, usage)
    m = re.search(r"\[[\d,\s]*\]", text)
    if not m:
        raise ValueError(f"no JSON array in model reply: {text[:200]}")
    idx = [int(x) for x in json.loads(m.group(0)) if 1 <= int(x) <= 9]
    return sorted(set(idx))


def solve_drag(image: Image.Image, model: str, reasoning_effort=None, usage=None):
    content = [{"type": "text", "text": (
        'Solve this drag-and-drop CAPTCHA following the instruction in the image. '
        'Return ONLY JSON {"start":[x,y],"end":[x,y]}. Start is the center of the '
        'movable piece; end is the center of its correct destination. Coordinates '
        'are normalized to 0-1000 in each axis of the ENTIRE screenshot, including '
        'the header and buttons. Do not return tile numbers.')},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + _b64(image)}}]
    text = _completion(content, model, 90, reasoning_effort, usage)
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise ValueError("No drag coordinates in model reply")
    action = json.loads(match.group())
    for name in ("start", "end"):
        point = action.get(name)
        if (not isinstance(point, list) or len(point) != 2
                or any(type(value) not in (int, float) or not 0 <= value <= 1000 for value in point)):
            raise ValueError("Invalid drag coordinates")
        if not 120 <= point[1] / 1000 * image.height <= image.height - 90:
            raise ValueError("Drag point is outside the challenge image")
    if action["start"] == action["end"]:
        raise ValueError("Drag destination must differ from its source")
    return action
