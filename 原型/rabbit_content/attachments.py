"""Validate bounded, path-free image references without a content store."""
import re


MAX_MESSAGE_IMAGES = 4


MAX_HISTORY_IMAGES = 512


MAX_NATIVE_ATTACHMENTS = 514


NATIVE_SOURCE_FIELDS = ('attachment_ordinal', 'content_index', 'field', 'token', 'fingerprint',
                        'kind', 'mime', 'size_bytes', 'state', 'reason', 'omitted_count')


def validate_attachment_ids(value, *, maximum=MAX_MESSAGE_IMAGES):
    """Only stable local references enter the store; never paths or image data."""
    if value is None:
        return []
    if (not isinstance(value, list) or len(value) > maximum
            or any(not isinstance(item, str) or not re.fullmatch(r'img_[0-9a-f]{32}', item) for item in value)
            or len(set(value)) != len(value)):
        raise ValueError('图片引用需要有效标识，每条消息最多 ' + str(maximum) + ' 张，不能重复。')
    return list(value)


def validate_native_attachments(value):
    """Keep bounded, path-free native positions, including failed originals."""
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_NATIVE_ATTACHMENTS:
        raise ValueError('原生图片描述超过历史读取上限。')
    records = []
    for item in value:
        if (not isinstance(item, dict)
                or type(item.get('attachment_ordinal')) is not int or not 0 <= item['attachment_ordinal'] < MAX_NATIVE_ATTACHMENTS
                or type(item.get('content_index')) is not int or not -1 <= item['content_index'] < 4096
                or item.get('field') not in ('content', 'api_content')
                or not isinstance(item.get('token'), str) or not re.fullmatch(r'natimg_[0-9a-f]{64}', item['token'])
                or not isinstance(item.get('fingerprint'), str) or not re.fullmatch(r'[0-9a-f]{64}', item['fingerprint'])
                or not isinstance(item.get('kind'), str) or not re.fullmatch(r'[a-z_]{1,64}', item['kind'])
                or item.get('mime') not in ('', 'image/png', 'image/jpeg')
                or type(item.get('size_bytes')) is not int or not 0 <= item['size_bytes'] <= 5 * 1024 * 1024
                or item.get('state') not in ('embedded', 'unavailable')
                or type(item.get('omitted_count')) is not int or not -1 <= item['omitted_count'] <= 8192
                or item.get('material_state') not in ('ready', 'unavailable')
                or any(v is not None and (not isinstance(v, str) or not re.fullmatch(r'[a-z_]{0,128}', v))
                       for v in (item.get('reason'), item.get('material_reason')))):
            raise ValueError('原生图片描述无效。')
        attachment_id = item.get('attachment_id')
        if item['state'] == 'embedded' and (item['kind'] not in ('input_image', 'image_url', 'legacy_image_data_url')
                or item['mime'] not in ('image/png', 'image/jpeg') or item['size_bytes'] <= 0
                or item['content_index'] < 0 or item.get('reason') not in ('', None)):
            raise ValueError('原生图片字节描述无效。')
        if item['material_state'] == 'ready':
            if (item['state'] != 'embedded' or not attachment_id
                    or item.get('material_reason') is not None or not validate_attachment_ids([attachment_id])):
                raise ValueError('原生图片材料引用无效。')
        elif attachment_id is not None:
            raise ValueError('缺失的原生图片不能带可用材料标识。')
        records.append({key: item.get(key) for key in NATIVE_SOURCE_FIELDS +
                        ('attachment_id', 'material_state', 'material_reason')})
    return records
