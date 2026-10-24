"""Recheck server-resolved image snapshots without decoding or discovering files.

The materials layer owns pixel/content validation and the allowed attachment
root. This boundary accepts only its explicit ordered metadata, rejects links,
and returns bounded bytes matching the accepted hash. No Pillow or network.
"""
import hashlib
import os
from pathlib import Path
import re
import stat


MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_TOTAL_BYTES = 20 * 1024 * 1024
MAX_IMAGE_PIXELS = 16_000_000
MAX_TOTAL_PIXELS = 32_000_000
MAX_IMAGE_SIDE = 8192
MAX_IMAGES = 8
IMAGE_ERRORS = ('图片元数据无效。', '本轮图片超出容量限制。',
                '图片文件无法读取。', '图片内容与发送快照不一致。')
FIELDS = ('id', 'path', 'mime', 'sha256', 'size_bytes', 'width', 'height')


class ImageInputError(ValueError):
    pass


def metadata(images):
    """Return a private, ordered copy; never follow paths or infer attachments."""
    if images is None:
        return []
    if not isinstance(images, list):
        raise ImageInputError(IMAGE_ERRORS[0])
    if len(images) > MAX_IMAGES:
        raise ImageInputError(IMAGE_ERRORS[1])
    result, total_bytes, total_pixels = [], 0, 0
    for image in images:
        if not isinstance(image, dict):
            raise ImageInputError(IMAGE_ERRORS[0])
        value = {key: image.get(key) for key in FIELDS}
        if (not isinstance(value['id'], str) or not re.fullmatch(r'img_[0-9a-f]{32}', value['id'])
                or value['mime'] not in ('image/png', 'image/jpeg')
                or not isinstance(value['sha256'], str)
                or not re.fullmatch(r'[0-9a-f]{64}', value['sha256'])
                or not isinstance(value['path'], str) or not value['path']
                or len(value['path']) > 8192 or any(ord(c) < 32 for c in value['path'])
                or not Path(value['path']).is_absolute()
                or any(part in ('.', '..') for part in value['path'].split('/'))
                or any(type(value[key]) is not int or value[key] < 1
                       for key in ('size_bytes', 'width', 'height'))):
            raise ImageInputError(IMAGE_ERRORS[0])
        pixels = value['width'] * value['height']
        total_bytes += value['size_bytes']
        total_pixels += pixels
        if (value['size_bytes'] > MAX_IMAGE_BYTES or pixels > MAX_IMAGE_PIXELS
                or max(value['width'], value['height']) > MAX_IMAGE_SIDE
                or total_bytes > MAX_TOTAL_BYTES or total_pixels > MAX_TOTAL_PIXELS):
            raise ImageInputError(IMAGE_ERRORS[1])
        result.append(value)
    return result


def _read(image):
    descriptors = []
    try:
        parts = Path(image['path']).parts
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        current = os.open(parts[0], flags)
        descriptors.append(current)
        for part in parts[1:-1]:
            current = os.open(part, flags, dir_fd=current)
            descriptors.append(current)
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=current)
        descriptors.append(descriptor)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ImageInputError(IMAGE_ERRORS[2])
        if info.st_size != image['size_bytes'] or info.st_size > MAX_IMAGE_BYTES:
            raise ImageInputError(IMAGE_ERRORS[3])
        with os.fdopen(descriptor, 'rb', closefd=False) as handle:
            data = handle.read(MAX_IMAGE_BYTES + 1)
        if len(data) != image['size_bytes'] or hashlib.sha256(data).hexdigest() != image['sha256']:
            raise ImageInputError(IMAGE_ERRORS[3])
        return data
    except ImageInputError:
        raise
    except (OSError, ValueError, RuntimeError):
        # Do not expose an absolute attachment path or raw OS diagnostics.
        raise ImageInputError(IMAGE_ERRORS[2]) from None
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def read_images(images):
    """Every returned byte belongs to exactly one accepted ordered snapshot."""
    return [dict(image, data=_read(image)) for image in metadata(images)]
