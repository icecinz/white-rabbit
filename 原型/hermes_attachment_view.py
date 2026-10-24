"""Bind native image path annotations to this chat's known material ledger.

This is display/source mapping only. A path-looking string is never a file
access grant; the resolver must prove an exact application-owned image source.
No RPC or filesystem read belongs here. It does not prove model recognition.
"""
from copy import deepcopy
import re

from hermes_material_cache import cache_root
from rabbit_runtime.hermes.message_view import split_display_images, reference_value as _reference_value


_NAME = re.compile(r'[0-9a-f]{64}\.(?:png|jpg)\Z')


def references(message, *, profile_home, workspace_id, chat_id, resolver):
    if (type(message) is not dict or message.get('role') != 'user'
            or type(message.get('text')) is not str or not callable(resolver)):
        return None
    split = split_display_images(message['text'])
    text, paths = split['text'], []
    prefix = str(cache_root(profile_home, workspace_id, chat_id) / 'images') + '/'
    lines = text.split('\n')
    while lines and lines[-1].startswith('@image:'):
        value = lines[-1][len('@image:'):]
        path = value[1:-1] if len(value) >= 2 and value[0] in ('`', '"', "'") and value[-1] == value[0] else value
        if (len(paths) == 4 or not path.startswith(prefix) or not _NAME.fullmatch(path[len(prefix):])
                or value != _reference_value(path)):
            return None
        paths.append(path); lines.pop()
    if not paths:
        return None
    paths.reverse()
    images = resolver(paths)
    if images is None:
        return None
    if (type(images) is not list or len(images) != len(paths)
            or any(image.get('path') != path for image, path in zip(images, paths))):
        return None
    if split['images'] and (len(split['images']) != len(images) or any(
            any(part[key] != image[key] for key in ('sha256', 'size_bytes', 'mime'))
            for part, image in zip(split['images'], images))):
        return None
    return {'text': '\n'.join(lines), 'images': images}


def projection(message, row, **scope):
    found = references(message, **scope)
    if found is None:
        return None
    projected = {key: deepcopy(value) for key, value in row.items() if key not in ('text', 'native_images')}
    projected['text'] = found['text']
    projected['native_images'] = [{'image_index': index, 'display_omitted': True,
        **{key: image[key] for key in ('sha256', 'size_bytes', 'mime')}}
        for index, image in enumerate(found['images'])]
    return projected
