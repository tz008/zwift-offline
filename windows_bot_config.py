"""Read and edit Solo Blob settings without importing the running server."""

import json
import math
import os
from pathlib import Path
import tempfile


# Keep these public UI defaults aligned with solo_blob.DEFAULT_CONFIG. The
# server/protobuf modules are deliberately not imported by the desktop page.
BOT_DEFAULTS = {
    'enabled': False,
    'riders': 8,
    'scatter': True,
    'rotate': True,
    'bot_gestures': True,
    'row_gap': 1.8,
    'lateral_spacing': 0.95,
    'scatter_width': 3.0,
    'shuffle_interval': 25,
    'pull_interval': 60,
}
NUMERIC_FIELDS = {
    'row_gap': ('前后排间距', 0.5, 10.0),
    'lateral_spacing': ('横向间距', 0.5, 3.0),
    # The simulator clamps scatter_width / 2 to 1.0 .. 3.2 metres.
    'scatter_width': ('散开宽度', 2.0, 6.4),
    'shuffle_interval': ('位置交换间隔', 5.0, None),
    'pull_interval': ('领骑轮换间隔', 5.0, None),
}
BOOLEAN_FIELDS = ('enabled', 'scatter', 'rotate', 'bot_gestures')


class BotConfigError(ValueError):
    def __init__(self, message, field=None):
        super().__init__(message)
        self.field = field


def bot_config_path(directory):
    return Path(directory) / 'storage' / 'solo_blob.txt'


def solo_player_id(directory):
    """Match run_standalone's first existing profile, including its order."""
    storage = Path(directory) / 'storage'
    try:
        names = os.listdir(storage)
    except FileNotFoundError:
        return 1
    except OSError as exc:
        raise BotConfigError('无法读取单机玩家资料 %s：%s' % (storage, exc)) from exc
    for name in names:
        path = storage / name
        if os.path.isdir(path) and os.path.exists(path / 'profile.bin'):
            try:
                return int(name) or 1
            except ValueError:
                continue
    return 1


def _reject_constant(value):
    raise ValueError('不支持 %s；请输入有限数值' % value)


def _read_config(path):
    try:
        text = path.read_text(encoding='utf-8-sig')
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError) as exc:
        raise BotConfigError('无法读取机器人配置 %s：%s' % (path, exc)) from exc
    try:
        config = json.loads(text, parse_constant=_reject_constant)
    except (ValueError, RecursionError) as exc:
        raise BotConfigError('机器人配置文件格式有误 %s：%s' % (path, exc)) from exc
    if not isinstance(config, dict):
        raise BotConfigError('机器人配置必须是 JSON 对象：%s' % path)
    return config


def validate_bot_config(config, *, strict=True, fields=None):
    """Keep old finite values readable; apply UI limits only to edited fields."""
    values = dict(BOT_DEFAULTS)
    values.update({key: config[key] for key in BOT_DEFAULTS if key in config})
    fields = set(BOT_DEFAULTS if fields is None else fields) if strict else set()
    for key in BOOLEAN_FIELDS:
        if type(values[key]) is not bool:
            raise BotConfigError('机器人开关、队形和行为选项必须为布尔值。', key)
    count = values['riders']
    if isinstance(count, str) and count.strip().isdecimal():
        try:
            count = int(count.strip())
        except ValueError:
            count = None
    if type(count) is not int or 'riders' in fields and not 1 <= count <= 30:
        raise BotConfigError('机器人人数必须是 1–30 之间的整数。', 'riders')
    values['riders'] = count
    for key, (label, lower, upper) in NUMERIC_FIELDS.items():
        try:
            if isinstance(values[key], bool):
                raise ValueError()
            value = float(values[key])
        except (TypeError, ValueError, OverflowError):
            value = float('nan')
        if not math.isfinite(value):
            raise BotConfigError('%s必须为有限数值。' % label, key)
        if key in fields and (value < lower or upper is not None and value > upper):
            limit = '%g–%g' % (lower, upper) if upper is not None else '不小于 %g' % lower
            raise BotConfigError('%s必须为%s的有限数值。' % (label, limit), key)
        values[key] = value
    if values['enabled'] and values['scatter'] and fields.intersection(('scatter', 'riders', 'scatter_width', 'lateral_spacing')):
        if values['lateral_spacing'] > values['scatter_width'] / 2:
            raise BotConfigError('自然散开时，横向间距不能超过散开宽度的一半。请减小横向间距或增大散开宽度。',
                                 'lateral_spacing')
    return values


def load_bot_config(directory):
    raw = _read_config(bot_config_path(directory))
    return dict(raw, **validate_bot_config(raw, strict=False))


def save_bot_config(directory, config, *, edited_fields=None):
    """Merge against the current file so unrelated hand-edited keys survive."""
    path = bot_config_path(directory)
    raw = _read_config(path)
    editable = {key: config[key] for key in BOT_DEFAULTS if key in config}
    # First check structure, then validate only fields the editor actually uses.
    validate_bot_config(raw, strict=False)
    fields = editable.keys() if edited_fields is None else edited_fields
    values = validate_bot_config(dict(raw, **editable), fields=fields)
    result = dict(raw, **values, player_id=solo_player_id(directory))
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='\n',
                                         dir=path.parent, prefix='solo_blob.', suffix='.tmp',
                                         delete=False) as stream:
            temporary = Path(stream.name)
            # SoloBlobManager uses the Windows system encoding when reading.
            json.dump(result, stream, ensure_ascii=True, indent=2, allow_nan=False)
            stream.write('\n')
        os.replace(temporary, path)
    except (OSError, ValueError, RecursionError) as exc:
        raise BotConfigError('无法保存机器人配置 %s：%s' % (path, exc)) from exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass  # Preserve the original save error if cleanup also fails.
    return result
