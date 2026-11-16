"""人脸素材处理.

服务端要的是「同一个人的脸」，同时不希望同一张图片被重复使用。
做法：取账号已上传的人脸照，做不可见级别的像素扰动后重新编码。

扰动量很小（几个像素、每个通道 ±5），肉眼和特征提取都不受影响，
但文件哈希会变。不做这一步的话，重复图片可能被判为盗用素材。
"""

from __future__ import annotations

import io
import logging
import random

from PIL import Image

from tsn.core.errors import EngineError

log = logging.getLogger("tsn.engine.face")

MIN_PIXELS = 5
MAX_PIXELS = 15
CHANNEL_DELTA = 5
JPEG_QUALITY = 95


def perturb(image_bytes: bytes, *, rng: random.Random | None = None, num_pixels: int | None = None) -> bytes:
    """加随机像素扰动后重新编码为 JPEG."""
    if not image_bytes:
        raise EngineError("人脸图片为空")

    rng = rng or random.Random()
    count = num_pixels if num_pixels is not None else rng.randint(MIN_PIXELS, MAX_PIXELS)

    try:
        image = Image.open(io.BytesIO(image_bytes))
        if image.mode != "RGB":
            image = image.convert("RGB")
        width, height = image.size
        if width < 40 or height < 40:
            raise EngineError(f"人脸图片过小: {width}x{height}")

        pixels = image.load()
        for _ in range(count):
            x = rng.randint(10, width - 10)
            y = rng.randint(10, height - 10)
            current = list(pixels[x, y])
            for _ in range(rng.randint(1, 3)):
                ch = rng.randint(0, 2)
                current[ch] = max(0, min(255, current[ch] + rng.randint(-CHANNEL_DELTA, CHANNEL_DELTA)))
            pixels[x, y] = tuple(current)

        out = io.BytesIO()
        image.save(out, format="JPEG", quality=JPEG_QUALITY)
        return out.getvalue()
    except EngineError:
        raise
    except Exception as exc:  # noqa: BLE001  图片格式问题不可穷举
        log.warning("人脸扰动失败，回退原图: %s", exc)
        return image_bytes


def pick_face(files: list[bytes], *, rng: random.Random | None = None) -> bytes:
    """从多张素材里随机挑一张（同一人多张照片时轮流用更自然）."""
    if not files:
        raise EngineError("没有可用的人脸素材；请先在官方 App 上传人脸照片")
    rng = rng or random.Random()
    return rng.choice(files)
