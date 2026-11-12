"""受限 DAP2 采集适配器。

约束表达式（CE）、响应 DDS 声明校验与 XDR 数据解码共用同一个冻结请求
（:class:`FrozenRequest`）：先按冻结的源 DDS 尺寸验证 [start:stride:stop]
（含 stop 的非负整数索引），再生成白名单变量投影；Grid 的 ARRAY 与 MAPS
按各维同步切片，响应 DDS 的约束后维长必须逐维等于请求推导长度，禁止按返回
元素总数猜测坐标轴对应。

仅支持 Byte、Int16、UInt16、Int32、UInt32、Float32、Float64、String 原子，
及其数组与 Grid；Sequence、Structure、函数与任意服务端表达式一律拒绝。

Data: 分隔符通过 DDS 文法解析定位，不按文本拆分，数据负载里出现的
``Data:`` 只作为字符串值。XDR 解码遵循项目协议：网络序；Byte/Int16/UInt16
标量占 32 位槽；数值与 Byte 向量带两个须相等的 u32 长度字、String 数组只有
一个长度字；字符串与 Byte 向量按 4 字节补零，padding 不进入值；尾部不得剩
余未解释字节。仅当 padding 恰好是整个响应的最后字节且被截断（如冻结资产
short-padding）时容忍 EOF，已存在的 padding 字节仍必须为零，截断点之后若还
有节点需要读取则照常失败。
"""
import copy
import hashlib
import re
import struct
from dataclasses import dataclass

from .io import Replay, Source

ATOMIC_TYPES = frozenset((
    'Byte', 'Int16', 'UInt16', 'Int32', 'UInt32',
    'Float32', 'Float64', 'String',
))
# 标量 32 位槽（Byte 为无符号槽），Float64/String 单独处理。
_SCALAR_SLOT = {
    'Byte': (4, '>I', (0, 255)),
    'Int16': (4, '>i', (-32768, 32767)),
    'UInt16': (4, '>I', (0, 65535)),
    'Int32': (4, '>i', (-2147483648, 2147483647)),
    'UInt32': (4, '>I', (0, 4294967295)),
    'Float32': (4, '>f', None),
}
# 向量内每个元素的打包宽度（String 为变长，单独处理）。
_VECTOR_WIDTH = {
    'Byte': 1, 'Int16': 2, 'UInt16': 2, 'Int32': 4,
    'UInt32': 4, 'Float32': 4, 'Float64': 8,
}
_IDENT = re.compile(r'[A-Za-z_][A-Za-z0-9_]*\Z')
_DATA_DELIMITER = b'\nData:\n'


class Dap2Error(Exception):
    """DAP2 采集失败基类，整次请求失败，不产生部分结果。"""


class Dap2ConstraintError(Dap2Error):
    """冻结请求构造或约束表达式校验失败。"""


class Dap2ProtocolError(Dap2Error):
    """响应 DDS/XDR 与冻结请求或协议不符。"""


class Dap2ServerError(Dap2Error):
    """服务端返回非 200 状态，错误文本须留档，不得解释为空数组。"""

    def __init__(self, status, headers, body_text, body_range):
        super().__init__(f'DAP2 服务端错误 status={status}')
        self.status = status
        self.headers = dict(headers)
        self.body_text = body_text
        self.body_range = list(body_range)


def _is_int(v):
    return type(v) is int and not isinstance(v, bool)


def _check_ident(name):
    if not isinstance(name, str) or not _IDENT.match(name):
        raise Dap2ConstraintError(f'DDS 标识符非法: {name!r}')
    return name


@dataclass(frozen=True)
class Dimension:
    """源 DDS 中的一个命名维度，size 为冻结的源尺寸。"""
    name: str
    size: int

    def __post_init__(self):
        _check_ident(self.name)
        if not _is_int(self.size) or self.size < 0:
            raise Dap2ConstraintError(f'维度尺寸非法: {self.name}={self.size!r}')


@dataclass(frozen=True)
class Slice:
    """[start:stride:stop]，stop 包含，全部为非负整数。"""
    start: int
    stride: int
    stop: int

    def __post_init__(self):
        for label, v in (('start', self.start), ('stride', self.stride), ('stop', self.stop)):
            if not _is_int(v) or v < 0:
                raise Dap2ConstraintError(f'切片 {label} 必须为非负整数: {v!r}')
        if self.stride < 1:
            raise Dap2ConstraintError('stride 必须 >= 1')
        if self.start > self.stop:
            raise Dap2ConstraintError(f'切片 start 不能大于 stop: {self}')

    def validate_against(self, dim):
        if self.stop >= dim.size:
            raise Dap2ConstraintError(
                f'切片 {self} 越界，源维度 {dim.name} 尺寸为 {dim.size}')
        if (self.stop - self.start) % self.stride:
            raise Dap2ConstraintError(
                f'切片 {self} 无法在维度 {dim.name} 上均匀取点')

    @property
    def count(self):
        return (self.stop - self.start) // self.stride + 1

    def source_index(self, k):
        return self.start + k * self.stride

    def render(self):
        return f'[{self.start}:{self.stride}:{self.stop}]'


@dataclass(frozen=True)
class VarEntry:
    """白名单中的原子标量/数组变量；dims 为空表示标量。"""
    name: str
    dims: tuple[Dimension, ...] = ()
    atom: str | None = None

    def __post_init__(self):
        _check_ident(self.name)
        if self.atom is not None and self.atom not in ATOMIC_TYPES:
            raise Dap2ConstraintError(f'不支持的原子类型: {self.atom!r}')
        if len({d.name for d in self.dims}) != len(self.dims):
            raise Dap2ConstraintError(f'变量 {self.name} 维度名重复')


@dataclass(frozen=True)
class MapSpec:
    """Grid 的一个 MAP，绑定到 ARRAY 的某个维度名。"""
    name: str
    dim: str

    def __post_init__(self):
        _check_ident(self.name)
        _check_ident(self.dim)


@dataclass(frozen=True)
class GridEntry:
    """白名单中的 Grid；maps 与 ARRAY 各维同步切片。"""
    name: str
    dims: tuple[Dimension, ...]
    maps: tuple[MapSpec, ...]
    base: str | None = None

    def __post_init__(self):
        _check_ident(self.name)
        if self.base is not None and self.base not in ATOMIC_TYPES:
            raise Dap2ConstraintError(f'不支持的 Grid 原子类型: {self.base!r}')
        dim_names = [d.name for d in self.dims]
        if len(set(dim_names)) != len(dim_names):
            raise Dap2ConstraintError(f'Grid {self.name} 维度名重复')
        if len(self.maps) != len(self.dims):
            raise Dap2ConstraintError(
                f'Grid {self.name} 的 MAP 数量 {len(self.maps)} 与维数 {len(self.dims)} 不一致')
        map_names = [m.name for m in self.maps]
        if len(set(map_names)) != len(map_names):
            raise Dap2ConstraintError(f'Grid {self.name} MAP 名重复')
        for m in self.maps:
            if m.dim not in dim_names:
                raise Dap2ConstraintError(
                    f'Grid {self.name} 的 MAP {m.name} 绑定未知维度 {m.dim}')


@dataclass(frozen=True)
class FrozenRequest:
    """一次冻结请求：CE、DDS 校验、XDR 解码共用的唯一事实来源。"""
    endpoint: str
    source_version: str
    dimensions: tuple[Dimension, ...]
    slices: dict[str, Slice]
    entries: tuple[VarEntry | GridEntry, ...]
    max_elements: int = 10000

    def __post_init__(self):
        if not isinstance(self.endpoint, str) or not self.endpoint:
            raise Dap2ConstraintError('endpoint 非法')
        if not isinstance(self.source_version, str) or not self.source_version:
            raise Dap2ConstraintError('source_version 非法')
        names = [d.name for d in self.dimensions]
        if len(set(names)) != len(names):
            raise Dap2ConstraintError('源维度名重复')
        if set(self.slices) != set(names):
            raise Dap2ConstraintError('切片集合必须与源维度一一对应')
        if not self.entries:
            raise Dap2ConstraintError('白名单变量为空')
        entry_names = [e.name for e in self.entries]
        if len(set(entry_names)) != len(entry_names):
            raise Dap2ConstraintError('白名单变量名重复')
        if not _is_int(self.max_elements) or self.max_elements <= 0:
            raise Dap2ConstraintError('max_elements 非法')
        total = 0
        for e in self.entries:
            for d in e.dims:
                if d.name not in names or self.dimensions[names.index(d.name)].size != d.size:
                    raise Dap2ConstraintError(
                        f'变量 {e.name} 引用了与源 DDS 不一致的维度 {d.name}')
                self.slices[d.name].validate_against(d)
            n = 1
            for d in e.dims:
                n *= self.slices[d.name].count
            total += n
            if n > self.max_elements or total > self.max_elements:
                raise Dap2ConstraintError(
                    f'变量 {e.name} 请求元素数超过 max_elements={self.max_elements}')

    @property
    def ce(self):
        parts = []
        for e in self.entries:
            tail = ''.join(self.slices[d.name].render() for d in e.dims)
            parts.append(e.name + tail)
        return ','.join(parts)

    @property
    def url(self):
        sep = '&' if '?' in self.endpoint else '?'
        return f'{self.endpoint}{sep}dap2.ce={self.ce}'

    def expected_dim_lengths(self, dims):
        return [(d.name, self.slices[d.name].count) for d in dims]


def request_from_manifest(manifest, endpoint='https://local.invalid/dods'):
    """从冻结清单构造冻结请求。

    清单字段：source_version、source_shape（维序按 y,x 的字典顺序）、slices、
    variables 白名单、max_elements；可选 variable_kinds 声明每个变量是
    grid/array/scalar 及其原子类型与 Grid MAP 绑定，缺省视为覆盖全部源维度
    的数组（类型以响应 DDS 为准，仍须是受支持原子）。
    """
    try:
        version = str(manifest['source_version'])
        shape = manifest['source_shape']
        raw_slices = manifest['slices']
        variables = list(manifest['variables'])
    except (KeyError, TypeError) as exc:
        raise Dap2ConstraintError(f'清单缺少冻结请求字段: {exc}') from exc
    if not isinstance(shape, dict) or not isinstance(raw_slices, dict):
        raise Dap2ConstraintError('source_shape/slices 必须为对象')
    dims = []
    slices = {}
    for name, size in shape.items():
        dim = Dimension(name, size)
        raw = raw_slices.get(name)
        if not isinstance(raw, list) or len(raw) != 3 or not all(_is_int(x) for x in raw):
            raise Dap2ConstraintError(f'维度 {name} 切片必须为 [start,stride,stop]')
        sl = Slice(*raw)
        sl.validate_against(dim)
        dims.append(dim)
        slices[name] = sl
    dims_tuple = tuple(dims)
    kinds = manifest.get('variable_kinds', {})
    if not isinstance(kinds, dict):
        raise Dap2ConstraintError('variable_kinds 必须为对象')
    entries = []
    for name in variables:
        spec = kinds.get(name, {'kind': 'array'})
        if not isinstance(spec, dict):
            raise Dap2ConstraintError(f'变量 {name} 的 kind 声明非法')
        kind = spec.get('kind')
        if kind == 'scalar':
            entries.append(VarEntry(name, (), spec.get('atom')))
        elif kind in ('array', None):
            entries.append(VarEntry(name, dims_tuple, spec.get('atom')))
        elif kind == 'grid':
            maps = tuple(
                MapSpec(mname, dname) for mname, dname in spec.get('maps', ()))
            entries.append(GridEntry(name, dims_tuple, maps, spec.get('base')))
        else:
            raise Dap2ConstraintError(f'变量 {name} 的未知 kind: {kind!r}')
    return FrozenRequest(
        endpoint=endpoint,
        source_version=version,
        dimensions=dims_tuple,
        slices=slices,
        entries=tuple(entries),
        max_elements=int(manifest.get('max_elements', 10000)),
    )


# --------------------------------------------------------------------------
# DDS 文法解析（只支持项目子集，未知结构明确报错）
# --------------------------------------------------------------------------

_PUNCT = {'{', '}', ';', ':', '[', ']', '='}


class _Tokenizer:
    def __init__(self, text):
        self.text = text
        self.pos = 0

    def next_token(self):
        text = self.text
        n = len(text)
        while self.pos < n and text[self.pos] in ' \t\r\n':
            self.pos += 1
        if self.pos >= n:
            return None
        ch = text[self.pos]
        if ch in _PUNCT:
            self.pos += 1
            return ch
        start = self.pos
        while self.pos < n and text[self.pos] not in _PUNCT and text[self.pos] not in ' \t\r\n':
            self.pos += 1
        return text[start:self.pos]

    def save(self):
        return self.pos

    def restore(self, pos):
        self.pos = pos

    def expect(self, tok):
        got = self.next_token()
        if got != tok:
            raise Dap2ProtocolError(f'DDS 语法错误：期望 {tok!r}，实际 {got!r}')
        return got

    def expect_ident(self):
        tok = self.next_token()
        if tok is None or not _IDENT.match(tok):
            raise Dap2ProtocolError(f'DDS 语法错误：期望标识符，实际 {tok!r}')
        return tok

    def expect_int(self):
        tok = self.next_token()
        if tok is None or not tok.isdigit():
            raise Dap2ProtocolError(f'DDS 维度必须为非负整数，实际 {tok!r}')
        return int(tok)

    def expect_atom(self):
        tok = self.next_token()
        if tok not in ATOMIC_TYPES:
            raise Dap2ProtocolError(f'不支持的 DDS 类型: {tok!r}')
        return tok


def _parse_dims(tk):
    dims = []
    while True:
        mark = tk.save()
        tok = tk.next_token()
        if tok != '[':
            tk.restore(mark)
            break
        name = tk.expect_ident()
        tk.expect('=')
        size = tk.expect_int()
        tk.expect(']')
        dims.append((name, size))
    return dims


def _parse_decl(tk):
    lead = tk.next_token()
    if lead in ('Structure', 'Sequence'):
        raise Dap2ProtocolError(f'不支持的 DDS 结构: {lead}')
    if lead == 'Grid':
        tk.expect('{')
        tag = tk.next_token()
        if tag != 'ARRAY':
            raise Dap2ProtocolError(f'Grid 缺少 ARRAY 声明，实际 {tag!r}')
        tk.expect(':')
        base = tk.expect_atom()
        array_name = tk.expect_ident()
        array_dims = _parse_dims(tk)
        if not array_dims:
            raise Dap2ProtocolError('Grid ARRAY 必须为数组')
        tk.expect(';')
        tag = tk.next_token()
        if tag != 'MAPS':
            raise Dap2ProtocolError(f'Grid 缺少 MAPS 声明，实际 {tag!r}')
        tk.expect(':')
        maps = []
        while True:
            tok = tk.next_token()
            if tok == '}':
                break
            if tok not in ATOMIC_TYPES:
                raise Dap2ProtocolError(f'Grid MAP 类型非法: {tok!r}')
            map_name = tk.expect_ident()
            map_dims = _parse_dims(tk)
            tk.expect(';')
            maps.append({'atom': tok, 'name': map_name, 'dims': map_dims})
        if not maps:
            raise Dap2ProtocolError('Grid 必须至少声明一个 MAP')
        grid_name = tk.expect_ident()
        tk.expect(';')
        return {'kind': 'grid', 'name': grid_name, 'base': base,
                'array': {'name': array_name, 'dims': array_dims}, 'maps': maps}
    if lead not in ATOMIC_TYPES:
        raise Dap2ProtocolError(f'不支持的 DDS 声明: {lead!r}')
    name = tk.expect_ident()
    dims = _parse_dims(tk)
    tk.expect(';')
    return {'kind': 'var', 'atom': lead, 'name': name, 'dims': dims}


def parse_dds(raw):
    """按 DDS 文法消费响应前缀，返回 (AST, DDS 文本, Data 负载起点)。

    只承认根 Dataset 闭合分号之后紧跟的 ``\\nData:\\n`` 分隔符；不扫描、不
    拆分整个 body，因此负载内部出现的 Data: 字节不会被误判为边界。
    """
    tk = _Tokenizer(raw.decode('latin1'))
    if tk.next_token() != 'Dataset':
        raise Dap2ProtocolError('响应不是 DAP2 DDS（缺少 Dataset 根）')
    tk.expect('{')
    decls = []
    while True:
        mark = tk.save()
        tok = tk.next_token()
        if tok == '}':
            break
        if tok is None:
            raise Dap2ProtocolError('DDS 提前结束')
        # 把首 token 还给 _parse_decl。
        tk.restore(mark)
        decls.append(_parse_decl(tk))
    if not decls:
        raise Dap2ProtocolError('Dataset 为空')
    tk.expect_ident()
    tk.expect(';')
    dds_end = tk.pos
    if not raw.startswith(_DATA_DELIMITER, dds_end):
        raise Dap2ProtocolError('DDS 后缺少换行 Data: 换行分隔符')
    data_start = dds_end + len(_DATA_DELIMITER)
    return decls, raw[:dds_end], data_start


def _expect_dims(actual, expected_pairs, where):
    """逐维核对名称、顺序与约束后长度，不用元素总数猜轴。"""
    if len(actual) != len(expected_pairs):
        raise Dap2ProtocolError(f'{where} 维数与冻结请求不符')
    for (aname, asize), (ename, esize) in zip(actual, expected_pairs):
        if aname != ename:
            raise Dap2ProtocolError(f'{where} 维度顺序/名称不符: {aname} != {ename}')
        if asize != esize:
            raise Dap2ProtocolError(
                f'{where} 维度 {aname} 约束后长度 {asize} 与请求推导长度 {esize} 不符')


def validate_dds(decls, request):
    """用冻结请求逐项核对响应 DDS 的变量名、类型、维度顺序与约束后维长。"""
    if [d['name'] for d in decls] != [e.name for e in request.entries]:
        raise Dap2ProtocolError('响应 DDS 变量集合或顺序与白名单不一致')
    validated = []
    for decl, entry in zip(decls, request.entries):
        if isinstance(entry, GridEntry):
            if decl['kind'] != 'grid':
                raise Dap2ProtocolError(f'{entry.name} 期望 Grid，响应声明为 {decl["kind"]}')
            if entry.base is not None and decl['base'] != entry.base:
                raise Dap2ProtocolError(
                    f'Grid {entry.name} ARRAY 类型 {decl["base"]} 与请求 {entry.base} 不符')
            if [m['name'] for m in decl['maps']] != [m.name for m in entry.maps]:
                raise Dap2ProtocolError(f'Grid {entry.name} MAP 名称或顺序不符')
            expected = request.expected_dim_lengths(entry.dims)
            _expect_dims(decl['array']['dims'], expected, f'Grid {entry.name} ARRAY')
            for map_decl, spec in zip(decl['maps'], entry.maps):
                dim = next(d for d in entry.dims if d.name == spec.dim)
                _expect_dims(map_decl['dims'],
                             request.expected_dim_lengths((dim,)),
                             f'Grid {entry.name} MAP {spec.name}')
                if map_decl['atom'] != decl['base']:
                    raise Dap2ProtocolError(
                        f'Grid {entry.name} MAP {spec.name} 类型与 ARRAY 不一致')
            validated.append(decl)
        else:
            if decl['kind'] != 'var':
                raise Dap2ProtocolError(f'{entry.name} 期望原子变量，响应声明为 {decl["kind"]}')
            if entry.atom is not None and decl['atom'] != entry.atom:
                raise Dap2ProtocolError(
                    f'变量 {entry.name} 类型 {decl["atom"]} 与请求 {entry.atom} 不符')
            if bool(entry.dims) != bool(decl['dims']):
                raise Dap2ProtocolError(f'变量 {entry.name} 标量/数组形态不符')
            _expect_dims(decl['dims'],
                         request.expected_dim_lengths(entry.dims),
                         f'变量 {entry.name}')
            validated.append(decl)
    return validated


# --------------------------------------------------------------------------
# XDR 解码（游标记录每个元素在原响应中的绝对字节范围）
# --------------------------------------------------------------------------

class _Cursor:
    def __init__(self, raw, base):
        self.raw = raw
        self.n = len(raw)
        self.pos = base

    def take(self, size, allow_eof_truncation=False):
        end = self.pos + size
        if end > self.n:
            if not allow_eof_truncation or self.n < self.pos:
                raise Dap2ProtocolError('XDR 数据提前结束')
            chunk = self.raw[self.pos:self.n]
            self.pos = self.n
            return chunk
        chunk = self.raw[self.pos:end]
        self.pos = end
        return chunk

    def u32(self):
        return struct.unpack('>I', self.take(4))[0]

    def pad(self, payload_len):
        needed = -payload_len % 4
        if not needed:
            return None
        start = self.pos
        chunk = self.take(needed, allow_eof_truncation=True)
        if any(b != 0 for b in chunk):
            raise Dap2ProtocolError('XDR padding 含非零字节，padding 不得进入值')
        return [start, start + len(chunk)] if chunk else None

    def finish(self):
        if self.pos != self.n:
            raise Dap2ProtocolError(
                f'响应尾部存在 {self.n - self.pos} 字节未解释数据，整次请求失败')


def _decode_scalar(cur, atom):
    start = cur.pos
    if atom == 'Float64':
        (value,) = struct.unpack('>d', cur.take(8))
    elif atom == 'String':
        value, full_range, value_range = _decode_string(cur)
        return value, full_range, value_range
    else:
        width, fmt, bounds = _SCALAR_SLOT[atom]
        (value,) = struct.unpack(fmt, cur.take(width))
        if bounds is not None and not (bounds[0] <= value <= bounds[1]):
            raise Dap2ProtocolError(f'{atom} 标量槽值 {value} 超出表示范围')
    return value, [start, cur.pos], None


def _decode_string(cur):
    start = cur.pos
    length = cur.u32()
    value_start = cur.pos
    raw_bytes = cur.take(length)
    try:
        value = raw_bytes.decode('utf-8')
    except UnicodeDecodeError as exc:
        raise Dap2ProtocolError(f'String 不是合法 UTF-8: {exc}') from exc
    pad = cur.pad(length)
    return value, [start, cur.pos if pad is None else pad[0]], [value_start, value_start + length]


def _index_map(dims, request, flat):
    out = {}
    counts = [request.slices[d.name].count for d in dims]
    rem = flat
    # 末维最快：先除末维。
    for d, count in reversed(list(zip(dims, counts))):
        k = rem % count
        rem //= count
        out[d.name] = request.slices[d.name].source_index(k)
    return out


def _decode_vector(cur, atom, dims, request):
    n = 1
    for _, c in request.expected_dim_lengths(dims):
        n *= c
    node_start = cur.pos
    if atom == 'String':
        # String 数组只有一个 u32 元素数字，随后逐项字符串。
        prefix = cur.u32()
        if prefix != n:
            raise Dap2ProtocolError(f'String 数组长度前缀 {prefix} 与请求元素数 {n} 不符')
        elements = []
        for k in range(n):
            value, full_range, value_range = _decode_string(cur)
            elements.append({'index': _index_map(dims, request, k),
                             'value': value, 'range': full_range})
        return {'elements': elements, 'range': [node_start, cur.pos]}
    # 数值/Byte 向量：两个 u32 长度字必须相同，且等于请求推导元素数。
    first = cur.u32()
    second = cur.u32()
    if first != n or second != n:
        raise Dap2ProtocolError(
            f'{atom} 向量双长度字 ({first},{second}) 与请求元素数 {n} 不符')
    width = _VECTOR_WIDTH[atom]
    payload_start = cur.pos
    payload = cur.take(width * n)
    elements = []
    if atom == 'Byte':
        for k, value in enumerate(payload):
            elements.append({'index': _index_map(dims, request, k), 'value': value,
                             'range': [payload_start + k, payload_start + k + 1]})
    else:
        fmt = {
            'Int16': '>h', 'UInt16': '>H', 'Int32': '>i', 'UInt32': '>I',
            'Float32': '>f', 'Float64': '>d',
        }[atom]
        for k in range(n):
            (value,) = struct.unpack_from(fmt, payload, k * width)
            elements.append({'index': _index_map(dims, request, k), 'value': value,
                             'range': [payload_start + k * width,
                                       payload_start + (k + 1) * width]})
    pad = cur.pad(len(payload))
    end = cur.pos if pad is None else pad[0]
    return {'elements': elements, 'range': [node_start, end],
            'padding_range': pad}


def _decode_var(cur, decl, entry, request):
    atom = decl['atom']
    dims = tuple(Dimension(name, size) for name, size in decl['dims'])
    start = cur.pos
    if not dims:
        value, value_range, _ = _decode_scalar(cur, atom)
        return {'name': entry.name, 'kind': 'scalar', 'atom': atom, 'dims': [],
                'elements': [{'index': {}, 'value': value, 'range': value_range}],
                'range': [start, cur.pos]}
    vec = _decode_vector(cur, atom, dims, request)
    return {'name': entry.name, 'kind': 'array', 'atom': atom,
            'dims': [{'name': n, 'length': s} for n, s in decl['dims']],
            'elements': vec['elements'], 'range': vec['range'],
            'padding_range': vec.get('padding_range')}


def _decode_grid(cur, decl, entry, request):
    start = cur.pos
    base = decl['base']
    array_dims = tuple(Dimension(n, s) for n, s in decl['array']['dims'])
    array_vec = _decode_vector(cur, base, array_dims, request)
    array_out = {
        'name': decl['array']['name'], 'atom': base,
        'dims': [{'name': n, 'length': s} for n, s in decl['array']['dims']],
        'elements': array_vec['elements'], 'range': array_vec['range'],
        'padding_range': array_vec.get('padding_range'),
    }
    maps_out = []
    for map_decl, spec in zip(decl['maps'], entry.maps):
        dim = next(d for d in entry.dims if d.name == spec.dim)
        m_dims = tuple(Dimension(n, s) for n, s in map_decl['dims'])
        m_vec = _decode_vector(cur, map_decl['atom'], m_dims, request)
        maps_out.append({
            'name': spec.name, 'atom': map_decl['atom'], 'dim': dim.name,
            'dims': [{'name': n, 'length': s} for n, s in map_decl['dims']],
            'elements': m_vec['elements'], 'range': m_vec['range'],
            'padding_range': m_vec.get('padding_range'),
        })
    return {'name': entry.name, 'kind': 'grid', 'base': base,
            'array': array_out, 'maps': maps_out, 'range': [start, cur.pos]}


def decode_response(raw, request):
    """完整解码一次响应：DDS 校验 + XDR 解码，任何不符都整次失败。"""
    decls, dds_text, data_start = parse_dds(raw)
    validated = validate_dds(decls, request)
    cur = _Cursor(raw, data_start)
    variables = []
    for decl, entry in zip(validated, request.entries):
        if isinstance(entry, GridEntry):
            variables.append(_decode_grid(cur, decl, entry, request))
        else:
            variables.append(_decode_var(cur, decl, entry, request))
    cur.finish()
    return {
        'source_version': request.source_version,
        'ce': request.ce,
        'url': request.url,
        'dds': {
            'text': dds_text.decode('latin1'),
            'sha256': hashlib.sha256(dds_text).hexdigest(),
            'bytes': len(dds_text),
            'range': [0, len(dds_text)],
        },
        'data_range': [data_start, len(raw)],
        'response': {
            'bytes': len(raw),
            'sha256': hashlib.sha256(raw).hexdigest(),
        },
        'variables': variables,
    }


# --------------------------------------------------------------------------
# 服务端重放适配器与带缓存的采集客户端
# --------------------------------------------------------------------------

class ReplayServer:
    """重放冻结 HTTP 记录：响应头原样回放，body 按任意切块经 Source 读取。"""

    def __init__(self, records, root):
        self._replay = Replay(records)
        self.root = root
        self.records = records
        self.sources = []

    @property
    def requests(self):
        return self._replay.requests

    def get(self, url, headers=None):
        position = self._replay.position
        self._replay.next('GET', url, headers)
        row = self.records[position]
        resp = row['response']
        status = resp['status']
        resp_headers = dict(resp.get('headers', {}))
        if 'asset' in resp:
            source = Source(self.root, resp['asset'])
            self.sources.append(source)
            total = len(source.raw)
        elif 'inline_body' in resp:
            inline = resp['inline_body']
            source = None
            total = len(inline.encode('utf-8') if isinstance(inline, str) else inline)
        else:
            raise ValueError('重放记录缺少 asset 或 inline_body')
        chunks = resp.get('chunk_lengths')
        if chunks is None:
            chunks = [total]
        if not isinstance(chunks, list) or not chunks or any(
                not _is_int(c) or c <= 0 for c in chunks):
            raise ValueError('chunk_lengths 必须为正整数列表')
        return _ReplayedBody(status, resp_headers, total, chunks, source, resp)


class _ReplayedBody:
    def __init__(self, status, headers, total, chunk_lengths, source, resp):
        self.status = status
        self.headers = headers
        self._total = total
        self._chunks = list(chunk_lengths)
        self._source = source
        self._resp = resp
        self._pos = 0
        self._idx = 0

    def read(self):
        if self._idx >= len(self._chunks):
            return b''
        if self._source is not None:
            length = min(self._chunks[self._idx], self._total - self._pos)
            chunk = self._source.read_at(self._pos, length)
        else:
            inline = self._resp['inline_body']
            data = inline.encode('utf-8') if isinstance(inline, str) else inline
            length = min(self._chunks[self._idx], self._total - self._pos)
            chunk = data[self._pos:self._pos + length]
        self._pos += len(chunk)
        self._idx += 1
        if self._idx == len(self._chunks) and self._pos != self._total:
            raise Dap2ProtocolError('重放切块未完整覆盖响应 body')
        return chunk


class Dap2Client:
    """带版本隔离缓存与错误留档的受限 DAP2 客户端。"""

    def __init__(self, server):
        self.server = server
        self._cache = {}
        self.errors = []

    def capture(self, request, headers=None):
        key = (request.ce, request.source_version)
        if key in self._cache:
            hit = copy.deepcopy(self._cache[key])
            hit['cached'] = True
            return hit
        body = self.server.get(request.url, headers)
        chunks = []
        while True:
            chunk = body.read()
            if not chunk:
                break
            chunks.append(chunk)
        raw = b''.join(chunks)
        if body.status != 200:
            text = raw.decode('utf-8', errors='replace')
            err = Dap2ServerError(body.status, body.headers, text, [0, len(raw)])
            self.errors.append({
                'ce': request.ce, 'source_version': request.source_version,
                'reason': 'server', 'status': body.status,
                'headers': dict(body.headers), 'body_text': text,
                'body_range': [0, len(raw)],
            })
            raise err
        try:
            result = decode_response(raw, request)
        except Dap2Error as exc:
            self.errors.append({
                'ce': request.ce, 'source_version': request.source_version,
                'reason': 'protocol', 'message': str(exc),
                'response_sha256': hashlib.sha256(raw).hexdigest(),
                'response_bytes': len(raw),
            })
            raise
        result['status'] = body.status
        result['response_headers'] = dict(body.headers)
        result['response']['range'] = [0, len(raw)]
        result['cached'] = False
        # 只有完整成功的结果进入缓存；失败 body 永不成为缓存命中。
        self._cache[key] = copy.deepcopy(result)
        return result


def capture_manifest(manifest, root, endpoint='https://local.invalid/dods', headers=None):
    """端到端便捷入口：清单 -> 冻结请求 -> 单条重放记录 -> 采集结果。"""
    request = request_from_manifest(manifest, endpoint)
    record = {
        'request': {'method': 'GET', 'url': request.url},
        'response': {
            'status': 200,
            'headers': {'content-type': 'application/x-www-form-urlencoded'},
            'asset': manifest['response'],
            'chunk_lengths': list(manifest['chunk_lengths']),
        },
    }
    server = ReplayServer([record], root)
    return Dap2Client(server).capture(request, headers)
