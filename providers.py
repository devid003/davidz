"""Image / video generators: Claude, Grok (xAI) and Pollinations.

No logos or watermarks are added anywhere:
- Pollinations: always sends nologo=true.
- Grok: the xAI Imagine API returns clean media.
- Claude: Anthropic has no image/video model, so Claude writes SVG artwork that we render
  ourselves (nothing is stamped on it). Video = several SVG frames, smoothed and encoded to MP4.
"""
import asyncio
import base64
import io
import os
import re
import time
import urllib.parse

import aiohttp
from dotenv import load_dotenv

load_dotenv()

POLLINATIONS_TOKEN = os.getenv("POLLINATIONS_TOKEN", "")
XAI_API_KEY = os.getenv("XAI_API_KEY", "")
GROK_IMAGE_MODEL = os.getenv("GROK_IMAGE_MODEL", "grok-imagine-image-2.0")
GROK_VIDEO_MODEL = os.getenv("GROK_VIDEO_MODEL", "grok-imagine-video-1.5")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5")

PROVIDERS = ("claude", "grok", "pollinations")
VIDEO_PROVIDERS = ("claude", "grok")
LABELS = {"claude": "Claude", "grok": "Grok", "pollinations": "Pollinations"}

NO_MARKS = "No logos, no watermarks, no signatures, no captions or text unless the prompt asks for text."


def secrets_list() -> list[str]:
    return [POLLINATIONS_TOKEN, XAI_API_KEY, ANTHROPIC_API_KEY]


# ---------------------------------------------------------------- Pollinations
async def pollinations_image(prompt: str) -> bytes:
    if POLLINATIONS_TOKEN:
        url = "https://gen.pollinations.ai/image/" + urllib.parse.quote(prompt)
        params = {"width": 1024, "height": 1024, "key": POLLINATIONS_TOKEN, "nologo": "true"}
    else:
        url = "https://image.pollinations.ai/prompt/" + urllib.parse.quote(prompt)
        params = {"width": 1024, "height": 1024, "nologo": "true"}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as s:
        async with s.get(url, params=params) as r:
            if r.status != 200:
                raise RuntimeError(f"Pollinations returned {r.status}")
            return await r.read()


# ------------------------------------------------------------------------ Grok
def _xai_headers() -> dict:
    if not XAI_API_KEY:
        raise RuntimeError("Grok is not set up yet (XAI_API_KEY is missing).")
    return {"Authorization": f"Bearer {XAI_API_KEY}", "Content-Type": "application/json"}


async def grok_image(prompt: str) -> bytes:
    headers = _xai_headers()
    body = {"model": GROK_IMAGE_MODEL, "prompt": prompt, "response_format": "b64_json"}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=180)) as s:
        async with s.post("https://api.x.ai/v1/images/generations", headers=headers, json=body) as r:
            if r.status != 200:
                raise RuntimeError(f"Grok returned {r.status} (the prompt may have been rejected)")
            item = (await r.json())["data"][0]
        if item.get("b64_json"):
            return base64.b64decode(item["b64_json"])
        async with s.get(item["url"]) as img:
            if img.status != 200:
                raise RuntimeError("Could not download the Grok image")
            return await img.read()


async def grok_video(prompt: str, duration: int = 8, aspect_ratio: str = "16:9") -> bytes:
    headers = _xai_headers()
    body = {
        "model": GROK_VIDEO_MODEL,
        "prompt": prompt,
        "duration": duration,
        "aspect_ratio": aspect_ratio,
    }
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=900)) as s:
        async with s.post("https://api.x.ai/v1/videos/generations", headers=headers, json=body) as r:
            if r.status != 200:
                raise RuntimeError(f"Grok returned {r.status} (the prompt may have been rejected)")
            request_id = (await r.json())["request_id"]

        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            await asyncio.sleep(5)
            async with s.get(f"https://api.x.ai/v1/videos/{request_id}", headers=headers) as r:
                if r.status != 200:
                    raise RuntimeError(f"Grok status check returned {r.status}")
                data = await r.json()
            status = data.get("status")
            if status == "done":
                async with s.get(data["video"]["url"]) as v:
                    if v.status != 200:
                        raise RuntimeError("Could not download the Grok video")
                    return await v.read()
            if status in ("failed", "expired"):
                raise RuntimeError(f"Grok video {status}")
        raise RuntimeError("Grok video took too long")


# ---------------------------------------------------------------------- Claude
async def _claude(system: str, user: str, max_tokens: int) -> str:
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("Claude is not set up yet (ANTHROPIC_API_KEY is missing).")
    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    body = {
        "model": CLAUDE_MODEL,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=300)) as s:
        async with s.post("https://api.anthropic.com/v1/messages", headers=headers, json=body) as r:
            if r.status != 200:
                raise RuntimeError(f"Claude returned {r.status}")
            data = await r.json()
    return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")


SVG_RE = re.compile(r"<svg\b.*?</svg>", re.S | re.I)

IMAGE_SYSTEM = (
    "You are an illustrator that answers ONLY with one complete SVG document and nothing else "
    "(no markdown fences, no explanation). Use viewBox=\"0 0 1024 1024\" and xmlns. "
    "Use gradients, layered shapes, filters and rich detail so it looks polished. "
    "No external images, fonts or links. " + NO_MARKS
)

VIDEO_SYSTEM = (
    "You are an animator. Answer ONLY with a sequence of complete SVG documents, one per animation "
    "frame, in time order, and nothing else (no markdown fences, no explanation). "
    "Every frame uses viewBox=\"0 0 1024 576\" and xmlns, keeps the same scene, palette and layout, and changes "
    "only what moves, so playback looks smooth. Do not use SMIL/CSS animation: each frame is a still. "
    "No external images, fonts or links. " + NO_MARKS
)


def _render_svg(svg: str, width: int, height: int) -> bytes:
    import cairosvg  # imported lazily so the bot still starts if it is missing

    return cairosvg.svg2png(bytestring=svg.encode("utf-8"), output_width=width, output_height=height)


async def claude_image(prompt: str) -> bytes:
    text = await _claude(IMAGE_SYSTEM, f"Draw this: {prompt}", 12000)
    m = SVG_RE.search(text)
    if not m:
        raise RuntimeError("Claude did not return a drawing, please try again")
    try:
        return await asyncio.to_thread(_render_svg, m.group(0), 1024, 1024)
    except Exception:
        raise RuntimeError("Claude's drawing could not be rendered, please try again")


def _encode_video(svgs: list[str], fps: int = 12, hold: int = 6) -> bytes:
    """Render SVG frames, cross-fade between them for smooth motion, encode MP4."""
    import imageio
    import numpy as np
    from PIL import Image

    imgs = []
    for svg in svgs:
        png = _render_svg(svg, 1024, 576)
        imgs.append(Image.open(io.BytesIO(png)).convert("RGB"))
    frames = []
    for a, b in zip(imgs, imgs[1:]):
        for i in range(hold):
            frames.append(np.asarray(Image.blend(a, b, i / hold)))
    frames.append(np.asarray(imgs[-1]))

    path = f"/tmp/claude_{time.time_ns()}.mp4"
    try:
        imageio.mimwrite(path, frames, fps=fps, codec="libx264", pixelformat="yuv420p", macro_block_size=1, quality=7)
        with open(path, "rb") as f:
            return f.read()
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


async def claude_video(prompt: str, frames: int = 8) -> bytes:
    text = await _claude(
        VIDEO_SYSTEM,
        f"Animate this in exactly {frames} frames: {prompt}",
        16000,
    )
    svgs = SVG_RE.findall(text)
    if len(svgs) < 2:
        raise RuntimeError("Claude did not return enough frames, please try again")
    try:
        return await asyncio.to_thread(_encode_video, svgs)
    except Exception:
        raise RuntimeError("Claude's animation could not be encoded, please try again")


# ------------------------------------------------------------------- dispatch
async def generate_image(provider: str, prompt: str) -> bytes:
    if provider == "claude":
        return await claude_image(prompt)
    if provider == "grok":
        return await grok_image(prompt)
    return await pollinations_image(prompt)


async def generate_video(provider: str, prompt: str, duration: int = 8) -> bytes:
    if provider == "claude":
        return await claude_video(prompt)
    if provider == "grok":
        return await grok_video(prompt, duration)
    raise RuntimeError("Video is only available with Claude or Grok.")
