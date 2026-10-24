"""Private, immutable PNG/JPEG material storage; IDs never accept file paths."""
import base64
import binascii
import contextlib
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import stat
import threading
import warnings

MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_PIXELS = 16_000_000
MAX_EDGE = 8192
MAX_MESSAGE_IMAGES = 4
MAX_ROUND_IMAGES = 8
MAX_VAULT_BYTES = 512 * 1024 * 1024
MAX_MATERIALS = 1024
ID_PATTERN = re.compile(r'img_[0-9a-f]{32}\Z')
HASH_PATTERN = re.compile(r'[0-9a-f]{64}\Z')
PUBLIC_FIELDS = ('id', 'kind', 'mime', 'name', 'size_bytes', 'width', 'height', 'status', 'source')


class MaterialCapacityError(ValueError):
    pass


def valid_id(value):
    return isinstance(value, str) and ID_PATTERN.fullmatch(value) is not None


def image_engine_available():
    try:
        from PIL import Image
        return callable(Image.open)
    except ImportError:
        return False


def image_parts(raw):
    if not raw or len(raw) > MAX_FILE_BYTES:
        raise ValueError('图片为空或超过 5 MiB，请选一张较小的 PNG/JPEG。')
    try:
        from PIL import Image, ImageOps, UnidentifiedImageError
    except ImportError:
        raise ValueError('本机图片读取组件尚未就绪，文字草稿仍保留。') from None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as image:
                kind, width, height = image.format, image.width, image.height
                if kind not in ('PNG', 'JPEG'):
                    raise ValueError('目前只接受真实的 PNG 和 JPEG 图片。')
                if (not 0 < width <= MAX_EDGE or not 0 < height <= MAX_EDGE
                        or width * height > MAX_PIXELS):
                    raise ValueError('图片尺寸超过限制：边长最多 8192，像素合计最多 1600 万。')
                if getattr(image, 'n_frames', 1) != 1:
                    raise ValueError('目前只接受静态 PNG/JPEG，暂不接动画图片。')
                image.verify()
            # verify() alone does not decode all pixel data. The bounded load
            # below rejects truncated/corrupt files before anything is ready.
            with Image.open(io.BytesIO(raw)) as image:
                image.load()
                thumbnail = ImageOps.exif_transpose(image).convert('RGBA')
                thumbnail.thumbnail((320, 320))
                buffer = io.BytesIO()
                thumbnail.save(buffer, format='PNG')
                return {'mime': 'image/png' if kind == 'PNG' else 'image/jpeg',
                        'width': width, 'height': height}, buffer.getvalue()
    except ValueError:
        raise
    except (OSError, SyntaxError, UnidentifiedImageError, Image.DecompressionBombError,
            Image.DecompressionBombWarning):
        raise ValueError('这张图片无法完整读取，请重新选择 PNG/JPEG。') from None


class ImageStore:
    def __init__(self, root):
        self.root = Path(root)
        if self.root.is_symlink():
            raise ValueError('图片材料目录不能是符号链接。')
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not self.root.is_dir() or self.root.is_symlink():
            raise ValueError('图片材料目录无效。')
        self.root.chmod(0o700)
        self.lock = threading.RLock()
        identity = self.root.stat()
        self._host_root_identity = identity.st_dev, identity.st_ino

    def capabilities(self):
        return {'enabled': image_engine_available(), 'max_file_bytes': MAX_FILE_BYTES,
                'max_message_images': MAX_MESSAGE_IMAGES, 'max_round_images': MAX_ROUND_IMAGES,
                'max_image_pixels': MAX_PIXELS, 'max_image_edge': MAX_EDGE,
                'max_vault_bytes': MAX_VAULT_BYTES, 'capacity_basis': 'stored_material_files',
                'automatic_cleanup': False}

    @contextlib.contextmanager
    def _directory(self, material_id=None):
        handles = []
        try:
            root = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            handles.append(root)
            if material_id is not None:
                if not valid_id(material_id):
                    raise ValueError('图片材料标识无效。')
                root = os.open(material_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
                handles.append(root)
            yield root
        except FileNotFoundError:
            raise KeyError('这张图片材料已找不到，原文字仍保留。') from None
        except OSError:
            raise ValueError('图片材料暂时无法完整读取或保存，不能使用符号链接或特殊文件。') from None
        finally:
            for descriptor in reversed(handles):
                os.close(descriptor)

    @staticmethod
    def _read(directory, name, limit):
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                raise ValueError('图片材料不是完整的普通文件，或已超过限制。')
            with os.fdopen(descriptor, 'rb', closefd=False) as handle:
                raw = handle.read(limit + 1)
            if len(raw) > limit or len(raw) != info.st_size:
                raise ValueError('图片材料未完整读取。')
            return raw
        finally:
            os.close(descriptor)

    def _meta(self, directory, material_id, *, raw=None):
        try:
            value = json.loads(self._read(directory, 'meta.json', 8192) if raw is None else raw)
            if (not isinstance(value, dict) or value.get('version') != 1 or value.get('id') != material_id
                    or value.get('mime') not in ('image/png', 'image/jpeg')
                    or value.get('kind') != 'image' or value.get('status') != 'ready'
                    or value.get('source') not in ('upload', 'history')
                    or not isinstance(value.get('name'), str) or len(value['name']) > 160
                    or any(ord(char) < 32 for char in value['name'])
                    or not isinstance(value.get('sha256'), str) or not HASH_PATTERN.fullmatch(value['sha256'])
                    or not isinstance(value.get('thumbnail_sha256'), str)
                    or not HASH_PATTERN.fullmatch(value['thumbnail_sha256'])):
                raise ValueError('图片材料的记录无效。')
            for key, maximum in (('size_bytes', MAX_FILE_BYTES), ('width', MAX_EDGE), ('height', MAX_EDGE)):
                if type(value.get(key)) is not int or not 0 < value[key] <= maximum:
                    raise ValueError('图片材料的大小记录无效。')
            if value['width'] * value['height'] > MAX_PIXELS:
                raise ValueError('图片材料的像素记录无效。')
            return value
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
            raise ValueError('图片材料的记录无法读取。') from None

    @contextlib.contextmanager
    def _host_directory(self, material_id):
        """Host-only read scope, anchored through every no-follow directory."""
        if not valid_id(material_id):
            raise ValueError('图片材料标识无效。')
        path = Path(os.path.abspath(self.root))
        descriptors, names = [], []
        try:
            current = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            descriptors.append(current)
            for part in (*path.parts[1:], material_id):
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
                names.append((current, part, child)); descriptors.append(child); current = child
            root = os.fstat(descriptors[-2])
            if (root.st_dev, root.st_ino) != self._host_root_identity:
                raise ValueError('图片材料目录已变化。')
            yield current
            for parent, name, child in names:
                opened, named = os.fstat(child), os.stat(name, dir_fd=parent, follow_symlinks=False)
                if (not stat.S_ISDIR(named.st_mode)
                        or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)):
                    raise ValueError('图片材料目录在读取中变化。')
        except FileNotFoundError:
            raise KeyError('这张图片材料已找不到，原文字仍保留。') from None
        except OSError:
            raise ValueError('图片材料暂时无法完整读取，不能使用符号链接或特殊文件。') from None
        finally:
            for descriptor in reversed(descriptors): os.close(descriptor)

    @staticmethod
    def _host_read(directory, name, limit, *, content=True):
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or not 0 <= before.st_size <= limit:
                raise ValueError('图片材料不是完整的普通文件。')
            raw = None
            if content:
                with os.fdopen(descriptor, 'rb', closefd=False) as stream: raw = stream.read(limit + 1)
                if len(raw) != before.st_size:
                    raise ValueError('图片材料未完整读取。')
            key = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
            if key(before) != key(os.fstat(descriptor)) or key(before) != key(os.stat(name, dir_fd=directory, follow_symlinks=False)):
                raise ValueError('图片材料在读取中变化。')
            return raw, before
        finally: os.close(descriptor)

    def _host_image(self, material_id, *, content, expected_sha256=None):
        if expected_sha256 is not None and (type(expected_sha256) is not str
                or not HASH_PATTERN.fullmatch(expected_sha256)):
            raise ValueError('图片内容校验标识无效。')
        with self.lock, self._host_directory(material_id) as directory:
            raw_meta, meta_identity = self._host_read(directory, 'meta.json', 8192)
            value = self._meta(directory, material_id, raw=raw_meta)
            if expected_sha256 is not None and expected_sha256 != value['sha256']:
                raise ValueError('图片内容版本已变化。')
            raw, original = self._host_read(directory, 'original.bin', MAX_FILE_BYTES, content=content)
            if original.st_size != value['size_bytes']:
                raise ValueError('图片材料的原图缺失或不完整。')
            if content:
                if hashlib.sha256(raw).hexdigest() != value['sha256']:
                    raise ValueError('图片内容已变化或不完整。')
                actual, _thumbnail = image_parts(raw)
                if any(actual[key] != value[key] for key in ('mime', 'width', 'height')):
                    raise ValueError('图片尺寸或格式记录与原图不一致。')
            current = os.stat('meta.json', dir_fd=directory, follow_symlinks=False)
            if (meta_identity.st_dev, meta_identity.st_ino, meta_identity.st_size, meta_identity.st_mtime_ns, meta_identity.st_ctime_ns) != (
                    current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns, current.st_ctime_ns):
                raise ValueError('图片材料的记录在读取中变化。')
            public = {key: value[key] for key in (*PUBLIC_FIELDS, 'sha256')}
        return {'image': public, 'data': raw} if content else public

    def metadata_image(self, material_id):
        """Host-only metadata + original stat. SHA is declared, not rehashed."""
        return self._host_image(material_id, content=False)

    def verified_image(self, material_id, *, expected_sha256=None):
        """Host-only verified original bytes; never an HTTP path/data URL."""
        return self._host_image(material_id, content=True, expected_sha256=expected_sha256)

    def get(self, material_id):
        if not valid_id(material_id):
            raise ValueError('图片材料标识无效。')
        with self._directory(material_id) as directory:
            value = self._meta(directory, material_id)
            descriptor = os.open('original.bin', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode) or info.st_size != value['size_bytes']:
                    raise ValueError('图片材料的原图缺失或不完整。')
            finally:
                os.close(descriptor)
        return {key: value[key] for key in PUBLIC_FIELDS}

    def metadata_for(self, ids):
        values = {}
        for material_id in dict.fromkeys(ids):
            try:
                values[material_id] = self.get(material_id)
            except (KeyError, ValueError):
                values[material_id] = {'id': material_id, 'status': 'missing'}
        return values

    def resolve(self, material_id):
        if not valid_id(material_id):
            raise ValueError('图片材料标识无效。')
        with self._directory(material_id) as directory:
            value = self._meta(directory, material_id)
            raw = self._read(directory, 'original.bin', MAX_FILE_BYTES)
            if len(raw) != value['size_bytes'] or hashlib.sha256(raw).hexdigest() != value['sha256']:
                raise ValueError('图片内容已变化或不完整，没有发给模型；请重新加入图片。')
        return {key: value[key] for key in ('id', 'mime', 'sha256', 'size_bytes', 'width', 'height')} | {
            'path': str(self.root / material_id / 'original.bin')}

    def content(self, material_id, thumbnail=False):
        if not valid_id(material_id):
            raise ValueError('图片材料标识无效。')
        with self._directory(material_id) as directory:
            value = self._meta(directory, material_id)
            raw = self._read(directory, 'thumbnail.png' if thumbnail else 'original.bin', MAX_FILE_BYTES)
            expected = value['thumbnail_sha256' if thumbnail else 'sha256']
            if hashlib.sha256(raw).hexdigest() != expected:
                raise ValueError('图片内容已变化或不完整，请重新加入图片。')
            mime = 'image/png' if thumbnail else value['mime']
        return {'material': {key: value[key] for key in PUBLIC_FIELDS},
                'data_url': 'data:' + mime + ';base64,' + base64.b64encode(raw).decode('ascii')}

    def find_by_sha256(self, digest):
        """Internal verified reuse, independent of the original source's life.

        This method is not exposed by HTTP. A historical index can keep pointing
        at an already copied original even after its native file was removed.
        A hash match alone is insufficient: verify both stored image files.
        """
        if not isinstance(digest, str) or not HASH_PATTERN.fullmatch(digest):
            raise ValueError('历史图片校验标识无效。')
        with self.lock, self._directory() as root:
            for material_id in os.listdir(root):
                if not valid_id(material_id):
                    continue
                directory = os.open(material_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                    dir_fd=root)
                try:
                    value = self._meta(directory, material_id)
                    if value['sha256'] == digest:
                        self._verify_reuse(directory, value, digest)
                        if not self._same_root(root):
                            raise ValueError('图片材料目录在读取中变化，历史图片未加入。')
                        return {key: value[key] for key in PUBLIC_FIELDS}
                finally:
                    os.close(directory)
            if not self._same_root(root):
                raise ValueError('图片材料目录在读取中变化，历史图片未加入。')
        return None

    def _verify_reuse(self, directory, value, digest):
        original = self._read(directory, 'original.bin', MAX_FILE_BYTES)
        preview = self._read(directory, 'thumbnail.png', MAX_FILE_BYTES)
        if (len(original) != value['size_bytes'] or hashlib.sha256(original).hexdigest() != digest
                or hashlib.sha256(preview).hexdigest() != value['thumbnail_sha256']):
            raise ValueError('已有同内容的历史图片材料不完整，没有新建重复副本。')

    @staticmethod
    def _write(directory, name, raw):
        descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=directory)
        with os.fdopen(descriptor, 'wb') as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())

    def _same_root(self, descriptor):
        actual = os.fstat(descriptor)
        try:
            named = os.stat(self.root, follow_symlinks=False)
        except OSError:
            return False
        return (stat.S_ISDIR(named.st_mode) and actual.st_dev == named.st_dev
                and actual.st_ino == named.st_ino)

    @staticmethod
    def _stored_bytes(directory):
        total = 0
        for name in ('original.bin', 'thumbnail.png', 'meta.json'):
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError('已有图片材料不完整，请先检查本机材料目录。')
                total += info.st_size
            finally:
                os.close(descriptor)
        return total

    def upload(self, name, encoded):
        if (not isinstance(encoded, str) or not encoded
                or len(encoded) > ((MAX_FILE_BYTES + 2) // 3) * 4):
            raise ValueError('图片为空或超过 5 MiB，请选一张较小的 PNG/JPEG。')
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise ValueError('图片上传内容无效，请重新选择文件。') from None
        return self._save_bytes(name, raw, source='upload')

    def import_bytes(self, name, raw):
        """Copy a bounded native-history image once, returning its stable ID.

        Only the server-side source reader supplies bytes. No path, client ID,
        or caller-provided digest can select or replace a material. Reuse is
        checked under the same process/file locks as publication and verifies
        the original and thumbnail before returning an existing reference.
        """
        if not isinstance(raw, bytes):
            raise ValueError('历史图片内容必须是完整的图片字节。')
        return self._save_bytes(name, raw, source='history', reuse=True)

    def _save_bytes(self, name, raw, *, source, reuse=False):
        properties, thumbnail = image_parts(raw)
        if (not isinstance(name, str) or len(name) > 160 or any(ord(char) < 32 for char in name)):
            raise ValueError('图片名称无效，最多 160 个字符。')
        name = name.strip() or ('图片.png' if properties['mime'] == 'image/png' else '图片.jpg')
        digest = hashlib.sha256(raw).hexdigest()
        with self.lock, self._directory() as root:
            try:
                lock_fd = os.open('upload.lock', os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                  0o600, dir_fd=root)
            except FileExistsError:
                lock_fd = os.open('upload.lock', os.O_RDWR | os.O_NOFOLLOW, dir_fd=root)
            temporary, temporary_fd = None, None
            try:
                if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
                    raise ValueError('图片上传锁无效。')
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
                count, total = 0, 0
                for child in os.listdir(root):
                    if not valid_id(child):
                        continue
                    count += 1
                    directory = os.open(child, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
                    try:
                        existing = self._meta(directory, child)
                        if reuse and existing['sha256'] == digest:
                            self._verify_reuse(directory, existing, digest)
                            if not self._same_root(root):
                                raise ValueError('图片材料目录在读取中变化，历史图片未加入。')
                            return {key: existing[key] for key in PUBLIC_FIELDS}
                        total += self._stored_bytes(directory)
                    finally:
                        os.close(directory)
                material_id = 'img_' + secrets.token_hex(16)
                value = dict(properties, version=1, id=material_id, kind='image', name=name,
                             size_bytes=len(raw), status='ready', source=source,
                             sha256=digest,
                             thumbnail_sha256=hashlib.sha256(thumbnail).hexdigest())
                metadata = json.dumps(value, ensure_ascii=False).encode()
                if count >= MAX_MATERIALS or total + len(raw) + len(thumbnail) + len(metadata) > MAX_VAULT_BYTES:
                    raise MaterialCapacityError('本机图片材料已达到容量上限，当前图片没有加入；原草稿仍保留。')
                if not self._same_root(root):
                    raise ValueError('图片材料目录在上传中变化，当前图片未加入；原草稿仍保留。')
                temporary = '.upload-' + secrets.token_hex(16)
                os.mkdir(temporary, mode=0o700, dir_fd=root)
                temporary_fd = os.open(temporary, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
                self._write(temporary_fd, 'original.bin', raw)
                self._write(temporary_fd, 'thumbnail.png', thumbnail)
                self._write(temporary_fd, 'meta.json', metadata)
                os.fsync(temporary_fd)
                if not self._same_root(root):
                    raise ValueError('图片材料目录在上传中变化，当前图片未加入；原草稿仍保留。')
                # All writes and publication remain anchored to the opened
                # root, even if another process changes its filesystem name.
                os.rename(temporary, material_id, src_dir_fd=root, dst_dir_fd=root)
                temporary = None
                os.fsync(root)
                return {key: value[key] for key in PUBLIC_FIELDS}
            finally:
                if temporary is not None:
                    if temporary_fd is not None:
                        for name in ('original.bin', 'thumbnail.png', 'meta.json'):
                            try:
                                os.unlink(name, dir_fd=temporary_fd)
                            except FileNotFoundError:
                                pass
                    os.rmdir(temporary, dir_fd=root)
                if temporary_fd is not None:
                    os.close(temporary_fd)
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)
