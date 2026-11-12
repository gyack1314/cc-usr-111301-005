"""受限 DAP2 客户端的冻结请求、DDS 校验、XDR 解码与缓存测试。"""
import json
import struct
import unittest
from pathlib import Path

from capture_data.dap2 import (
    Dap2Client, Dap2ConstraintError, Dap2ProtocolError, Dap2ServerError,
    Dimension, FrozenRequest, GridEntry, MapSpec, ReplayServer, Slice,
    VarEntry, capture_manifest, decode_response, parse_dds, request_from_manifest,
)

DATA = Path(__file__).resolve().parent.parent / 'data'

# --------------------------------------------------------------------------
# 测试侧 DAP2 XDR/DDS 编码器（仅用于构造响应，不参与被测实现）
# --------------------------------------------------------------------------


def u32(v):
    return struct.pack('>I', v)


def pad4(blob):
    return blob + b'\x00' * (-len(blob) % 4)


def xdr_string(s):
    raw = s.encode('utf-8')
    return u32(len(raw)) + pad4(raw)


def xdr_scalar(atom, value):
    if atom == 'Byte':
        return u32(value)
    if atom in ('UInt16', 'UInt32'):
        return u32(value)
    if atom in ('Int16', 'Int32'):
        return struct.pack('>i', value)
    if atom == 'Float32':
        return struct.pack('>f', value)
    if atom == 'Float64':
        return struct.pack('>d', value)
    if atom == 'String':
        return xdr_string(value)
    raise ValueError(atom)


def xdr_vector(atom, values):
    n = len(values)
    if atom == 'String':
        # String 数组只有一个长度字。
        return u32(n) + b''.join(xdr_string(v) for v in values)
    if atom == 'Byte':
        return u32(n) + u32(n) + pad4(bytes(values))
    width = {'Int16': 2, 'UInt16': 2, 'Int32': 4, 'UInt32': 4,
             'Float32': 4, 'Float64': 8}[atom]
    fmt = {'Int16': '>h', 'UInt16': '>H', 'Int32': '>i', 'UInt32': '>I',
           'Float32': '>f', 'Float64': '>d'}[atom]
    payload = b''.join(struct.pack(fmt, v) for v in values)
    return u32(n) + u32(n) + pad4(payload)


def dims_text(dims):
    return ''.join(f'[{name} = {size}]' for name, size in dims)


def dds_grid(name, base, array_name, array_dims, maps):
    parts = [f'Grid {{ ARRAY: {base} {array_name}{dims_text(array_dims)}; '
             f'MAPS: ']
    parts.append(' '.join(f'{atom} {mname}{dims_text(mdims)};'
                          for atom, mname, mdims in maps))
    parts.append(f' }} {name};')
    return ''.join(parts)


def dds_var(atom, name, dims):
    return f'{atom} {name}{dims_text(dims)};'


def dds_wrap(decls):
    return ('Dataset { ' + ' '.join(decls) + ' } obs;').encode('latin1')


def response_body(dds_decl_text, binary):
    return dds_decl_text + b'\nData:\n' + binary


class FrozenRequestTests(unittest.TestCase):
    def setUp(self):
        self.dims = (Dimension('y', 4), Dimension('x', 5))
        self.slices = {'y': Slice(0, 2, 2), 'x': Slice(1, 2, 3)}
        self.grid = GridEntry('g', self.dims,
                              (MapSpec('lat', 'y'), MapSpec('lon', 'x')), 'Float64')
        self.note = VarEntry('note', (), 'String')

    def test_frozen_ce_and_derived_lengths(self):
        req = FrozenRequest('https://local.invalid/dods', 'v1', self.dims,
                            self.slices, (self.grid, self.note))
        self.assertEqual(req.ce, 'g[0:2:2][1:2:3],note')
        self.assertEqual(req.url,
                         'https://local.invalid/dods?dap2.ce=g[0:2:2][1:2:3],note')
        self.assertEqual(req.expected_dim_lengths(self.dims), [('y', 2), ('x', 2)])
        # 原索引由同一冻结切片推导。
        self.assertEqual([self.slices['y'].source_index(k) for k in range(2)], [0, 2])
        self.assertEqual([self.slices['x'].source_index(k) for k in range(2)], [1, 3])

    def test_non_unit_stride_uneven_rejected(self):
        # Slice 自身只检查非负；均匀取点要对照冻结源维度尺寸验证。
        bad_slices = {'y': Slice(0, 3, 2), 'x': Slice(1, 2, 3)}
        with self.assertRaises(Dap2ConstraintError):
            FrozenRequest('u', 'v1', self.dims, bad_slices, (self.grid,))

    def test_stop_inclusive_out_of_range(self):
        with self.assertRaises(Dap2ConstraintError):
            FrozenRequest('u', 'v1', self.dims,
                          {'y': Slice(0, 1, 4), 'x': Slice(0, 1, 4)},
                          (self.grid,))

    def test_negative_and_non_int_slices(self):
        for bad in ((-1, 1, 1), (0, 0, 1), (0, True, 1), (1, 1, 0)):
            with self.assertRaises(Dap2ConstraintError):
                Slice(*bad)

    def test_unsupported_types_rejected(self):
        for bad in ('Sequence', 'Structure', 'Float128', 'Func'):
            with self.assertRaises(Dap2ConstraintError):
                VarEntry('z', (), bad)

    def test_grid_map_dim_must_bind(self):
        with self.assertRaises(Dap2ConstraintError):
            GridEntry('g', self.dims, (MapSpec('lat', 'z'),), 'Float64')
        with self.assertRaises(Dap2ConstraintError):
            GridEntry('g', self.dims, (MapSpec('lat', 'y'),), 'Float64')

    def test_max_elements_cap(self):
        with self.assertRaises(Dap2ConstraintError):
            FrozenRequest('u', 'v1', self.dims, self.slices, (self.grid,),
                          max_elements=3)

    def test_whitelist_only(self):
        req = FrozenRequest('u', 'v1', self.dims, self.slices, (self.note,))
        self.assertEqual(req.ce, 'note')


class FixtureTests(unittest.TestCase):
    def _record(self, manifest, status=200, inline=None):
        req = request_from_manifest(manifest)
        resp = {'status': status, 'headers': {'x-frozen': 'array-v1'}}
        if inline is not None:
            resp['inline_body'] = inline
        else:
            resp['asset'] = manifest['response']
            resp['chunk_lengths'] = list(manifest['chunk_lengths'])
        return req, {'request': {'method': 'GET', 'url': req.url}, 'response': resp}

    def test_grid_fixture_full_decode_and_original_indices(self):
        m = json.loads((DATA / 'normal.json').read_text(encoding='utf8'))
        req, rec = self._record(m)
        client = Dap2Client(ReplayServer([rec], DATA))
        res = client.capture(req)
        g = res['variables'][0]
        self.assertEqual(g['array']['dims'],
                         [{'name': 'y', 'length': 2}, {'name': 'x', 'length': 2}])
        self.assertEqual([e['value'] for e in g['array']['elements']],
                         [11.0, 13.0, 31.0, 33.0])
        self.assertEqual([e['index'] for e in g['array']['elements']],
                         [{'x': 1, 'y': 0}, {'x': 3, 'y': 0},
                          {'x': 1, 'y': 2}, {'x': 3, 'y': 2}])
        self.assertEqual([e['index'] for e in g['maps'][0]['elements']],
                         [{'y': 0}, {'y': 2}])
        self.assertEqual([e['value'] for e in g['maps'][1]['elements']],
                         [101.0, 103.0])
        self.assertEqual(res['variables'][1]['elements'][0]['value'], 'Data: 雨')
        # 元素字节范围落在响应 Data 负载内，且彼此不重叠。
        ranges = [tuple(e['range']) for e in g['array']['elements']]
        self.assertTrue(all(a[1] == b[0] for a, b in zip(ranges, ranges[1:])))
        self.assertTrue(all(r[0] >= res['data_range'][0] for r in ranges))
        # 服务端记录了真实请求与切块读取。
        self.assertEqual(client.server.requests[0]['method'], 'GET')
        self.assertEqual(len(client.server.sources[0].calls), len(m['chunk_lengths']))
        self.assertEqual(res['dds']['bytes'], 122)
        self.assertEqual(res['response']['sha256'], m['response']['sha256'])

    def test_dds_dimension_mismatch_fails_whole_request(self):
        m = json.loads((DATA / 'dds-request-mismatch.json').read_text(encoding='utf8'))
        req, rec = self._record(m)
        client = Dap2Client(ReplayServer([rec], DATA))
        with self.assertRaises(Dap2ProtocolError) as ctx:
            client.capture(req)
        self.assertIn('约束后长度 3 与请求推导长度 2', str(ctx.exception))
        self.assertEqual(client.errors[-1]['reason'], 'protocol')

    def test_truncated_final_padding_tolerated_but_not_consumed(self):
        m = json.loads((DATA / 'truncated-xdr-padding.json').read_text(encoding='utf8'))
        req, rec = self._record(m)
        client = Dap2Client(ReplayServer([rec], DATA))
        res = client.capture(req)
        note = res['variables'][1]['elements'][0]
        self.assertEqual(note['value'], 'Data: 雨')
        self.assertEqual(note['range'], [217, 230])  # 不含被截断的 padding
        self.assertEqual(res['response']['bytes'], 232)

    def test_data_word_inside_binary_is_not_a_delimiter(self):
        m = json.loads((DATA / 'normal.json').read_text(encoding='utf8'))
        raw = (DATA / 'grid.dods').read_bytes()
        decls, dds_text, start = parse_dds(raw)
        self.assertEqual(start, 129)
        self.assertEqual(decls[0]['name'], 'g')
        # 若按 split(b'Data:') 定位会得到两个候选，文法解析只有一个边界。
        self.assertEqual(raw.count(b'Data:'), 2)

    def test_chunked_replay_covers_entire_body(self):
        m = json.loads((DATA / 'normal.json').read_text(encoding='utf8'))
        m2 = dict(m, chunk_lengths=[37, 59, 73, 64])
        req, rec = self._record(m2)
        client = Dap2Client(ReplayServer([rec], DATA))
        res = client.capture(req)
        self.assertEqual(res['response']['bytes'], 233)
        self.assertEqual(len(client.server.sources[0].calls), 4)

    def test_underrun_chunk_plan_is_error(self):
        m = json.loads((DATA / 'normal.json').read_text(encoding='utf8'))
        m2 = dict(m, chunk_lengths=[10, 10])
        req, rec = self._record(m2)
        client = Dap2Client(ReplayServer([rec], DATA))
        with self.assertRaises(Dap2ProtocolError):
            client.capture(req)


class SyntheticTypeTests(unittest.TestCase):
    """用编码器构造非方形 Grid、非单位 stride 与全部原子类型。"""

    def setUp(self):
        # y 维 3 点（0,2,4），x 维 2 点（1,3），非方形。
        self.dims = (Dimension('y', 6), Dimension('x', 5))
        self.slices = {'y': Slice(0, 2, 4), 'x': Slice(1, 2, 3)}
        self.grid = GridEntry(
            'g', self.dims, (MapSpec('lat', 'y'), MapSpec('lon', 'x')), 'Int16')
        self.atom_cases = [
            ('Byte', [0, 255, 1, 128, 7, 9]),
            ('Int16', [-1, 32767, -32768, 0, 11, -12]),
            ('UInt16', [0, 65535, 7, 8, 9, 10]),
            ('Int32', [-1, 2147483647, 0, 4, 5, 6]),
            ('UInt32', [0, 4294967295, 3, 2, 1, 0]),
            ('Float32', [1.5, -2.25, 3.0, 4.0, 5.0, 6.0]),
            ('Float64', [11.0, 13.0, 31.0, 33.0, 7.5, -9.25]),
            ('String', ['雨', 'Data:', 'a=b&Data:\n', '', '末维最快', 'x']),
        ]

    def _build_request(self, extra_entries):
        entries = (self.grid,) + tuple(extra_entries)
        return FrozenRequest('https://local.invalid/dods', 'v1', self.dims,
                             self.slices, entries)

    def _grid_binary(self, vals, lat_vals, lon_vals):
        return (xdr_vector('Int16', vals) + xdr_vector('Int16', lat_vals)
                + xdr_vector('Int16', lon_vals))

    def test_all_atomic_arrays_and_scalars_round_trip(self):
        extra = []
        decls = [dds_grid('g', 'Int16', 'temp', [('y', 3), ('x', 2)],
                          [('Int16', 'lat', [('y', 3)]),
                           ('Int16', 'lon', [('x', 2)])])]
        binary = self._grid_binary([1, 2, 3, 4, 5, 6], [10, 30, 50], [101, 103])
        for i, (atom, vals) in enumerate(self.atom_cases):
            name = f'v_{atom.lower()}'
            extra.append(VarEntry(name, self.dims, atom))
            decls.append(dds_var(atom, name, [('y', 3), ('x', 2)]))
            binary += xdr_vector(atom, vals)
        scalar_atoms = [('Byte', 200), ('Int16', -1234), ('UInt16', 40000),
                        ('Int32', -987654), ('UInt32', 4000000000),
                        ('Float32', 2.5), ('Float64', -8.75),
                        ('String', '标量 Data: 雨')]
        for atom, val in scalar_atoms:
            name = f's_{atom.lower()}'
            extra.append(VarEntry(name, (), atom))
            decls.append(dds_var(atom, name, []))
            binary += xdr_scalar(atom, val)
        req = self._build_request(extra)
        res = decode_response(response_body(dds_wrap(decls), binary), req)
        g = res['variables'][0]
        self.assertEqual([e['index'] for e in g['array']['elements']],
                         [{'x': 1, 'y': 0}, {'x': 3, 'y': 0},
                          {'x': 1, 'y': 2}, {'x': 3, 'y': 2},
                          {'x': 1, 'y': 4}, {'x': 3, 'y': 4}])
        self.assertEqual([e['value'] for e in g['maps'][0]['elements']],
                         [10, 30, 50])
        self.assertEqual([e['index'] for e in g['maps'][1]['elements']],
                         [{'x': 1}, {'x': 3}])
        for (atom, vals), out in zip(self.atom_cases, res['variables'][1:9]):
            self.assertEqual(out['atom'], atom)
            got = [e['value'] for e in out['elements']]
            if atom == 'Float32':
                self.assertEqual(got, vals)
            else:
                self.assertEqual(got, vals)
            # String 数组只有单长度字：节点范围不允许出现双长度前缀结构误差。
            self.assertEqual(len(out['elements']), 6)
        for (atom, val), out in zip(scalar_atoms, res['variables'][9:]):
            self.assertEqual(out['elements'][0]['value'], val)
            self.assertEqual(out['elements'][0]['index'], {})

    def test_utf8_string_with_data_word_and_padding(self):
        s = 'Data: 雨'  # 9 个 UTF-8 字节 -> 3 字节 padding
        req = self._build_request([VarEntry('msg', self.dims, 'String')])
        vals = [s, 'x', 'yy', 'Data:', '雨雨', '']
        decls = [dds_grid('g', 'Int16', 'temp', [('y', 3), ('x', 2)],
                          [('Int16', 'lat', [('y', 3)]),
                           ('Int16', 'lon', [('x', 2)])]),
                 dds_var('String', 'msg', [('y', 3), ('x', 2)])]
        binary = self._grid_binary([1, 2, 3, 4, 5, 6], [10, 30, 50], [101, 103])
        binary += xdr_vector('String', vals)
        res = decode_response(response_body(dds_wrap(decls), binary), req)
        self.assertEqual([e['value'] for e in res['variables'][1]['elements']], vals)

    def test_grid_map_slice_must_stay_synchronized(self):
        # MAP 给了全尺寸 y=6 而 ARRAY 约束到 y=3：不能用元素总数掩盖。
        decls = [dds_grid('g', 'Int16', 'temp', [('y', 3), ('x', 2)],
                          [('Int16', 'lat', [('y', 6)]),
                           ('Int16', 'lon', [('x', 2)])])]
        binary = self._grid_binary([1, 2, 3, 4, 5, 6],
                                   list(range(6)), [101, 103])
        req = self._build_request([])
        with self.assertRaises(Dap2ProtocolError) as ctx:
            decode_response(response_body(dds_wrap(decls), binary), req)
        self.assertIn('MAP lat', str(ctx.exception))

    def test_total_element_count_match_cannot_save_wrong_axes(self):
        # 总元素都是 6，但维度顺序/长度对调：ARRAY 声明 [x=2][y=3]。
        decls = [dds_grid('g', 'Int16', 'temp', [('x', 2), ('y', 3)],
                          [('Int16', 'lat', [('y', 3)]),
                           ('Int16', 'lon', [('x', 2)])])]
        binary = self._grid_binary([1, 2, 3, 4, 5, 6], [10, 30, 50], [101, 103])
        req = self._build_request([])
        with self.assertRaises(Dap2ProtocolError):
            decode_response(response_body(dds_wrap(decls), binary), req)

    def test_nonzero_padding_rejected(self):
        blob = xdr_vector('Byte', [1, 2, 3])
        # 3 字节 payload 后应为 00 00 00，改成非零。
        bad = blob[:-1] + b'\x01'
        req = self._build_request([VarEntry('b', self.dims, 'Byte')])
        decls = [dds_grid('g', 'Int16', 'temp', [('y', 3), ('x', 2)],
                          [('Int16', 'lat', [('y', 3)]),
                           ('Int16', 'lon', [('x', 2)])]),
                 dds_var('Byte', 'b', [('y', 3), ('x', 2)])]
        binary = self._grid_binary([1, 2, 3, 4, 5, 6], [10, 30, 50], [101, 103]) + bad
        with self.assertRaises(Dap2ProtocolError):
            decode_response(response_body(dds_wrap(decls), binary), req)

    def test_length_prefix_mismatch_rejected(self):
        good = xdr_vector('Int32', [1, 2, 3, 4, 5, 6])
        bad = u32(5) + good[4:]  # 第一长度字与请求元素数 6 不符
        req = self._build_request([VarEntry('z', self.dims, 'Int32')])
        decls = [dds_grid('g', 'Int16', 'temp', [('y', 3), ('x', 2)],
                          [('Int16', 'lat', [('y', 3)]),
                           ('Int16', 'lon', [('x', 2)])]),
                 dds_var('Int32', 'z', [('y', 3), ('x', 2)])]
        binary = self._grid_binary([1, 2, 3, 4, 5, 6], [10, 30, 50], [101, 103]) + bad
        with self.assertRaises(Dap2ProtocolError):
            decode_response(response_body(dds_wrap(decls), binary), req)

    def test_trailing_bytes_rejected_not_treated_as_missing_values(self):
        decls = [dds_grid('g', 'Int16', 'temp', [('y', 3), ('x', 2)],
                          [('Int16', 'lat', [('y', 3)]),
                           ('Int16', 'lon', [('x', 2)])])]
        binary = self._grid_binary([1, 2, 3, 4, 5, 6], [10, 30, 50], [101, 103])
        binary += b'\x00\x00\x00\x01'  # 尾随未解释字节
        req = self._build_request([])
        with self.assertRaises(Dap2ProtocolError) as ctx:
            decode_response(response_body(dds_wrap(decls), binary), req)
        self.assertIn('未解释数据', str(ctx.exception))

    def test_truncated_payload_rejected(self):
        decls = [dds_grid('g', 'Int16', 'temp', [('y', 3), ('x', 2)],
                          [('Int16', 'lat', [('y', 3)]),
                           ('Int16', 'lon', [('x', 2)])])]
        full = self._grid_binary([1, 2, 3, 4, 5, 6], [10, 30, 50], [101, 103])
        req = self._build_request([])
        with self.assertRaises(Dap2ProtocolError):
            decode_response(response_body(dds_wrap(decls), full[:-3]), req)

    def test_wrong_variable_name_order_type_rejected(self):
        good_decls = [dds_grid('g', 'Int16', 'temp', [('y', 3), ('x', 2)],
                               [('Int16', 'lat', [('y', 3)]),
                                ('Int16', 'lon', [('x', 2)])]),
                      dds_var('String', 'note', [])]
        binary = self._grid_binary([1, 2, 3, 4, 5, 6], [10, 30, 50], [101, 103])
        binary += xdr_string('n')
        req = self._build_request([VarEntry('note', (), 'String')])
        swapped = [good_decls[1], good_decls[0]]
        with self.assertRaises(Dap2ProtocolError):
            decode_response(response_body(dds_wrap(swapped), binary), req)
        wrong_type = [dds_grid('g', 'Int32', 'temp', [('y', 3), ('x', 2)],
                               [('Int32', 'lat', [('y', 3)]),
                                ('Int32', 'lon', [('x', 2)])]),
                      dds_var('String', 'note', [])]
        with self.assertRaises(Dap2ProtocolError):
            decode_response(response_body(dds_wrap(wrong_type),
                                           xdr_vector('Int32', [1, 2, 3, 4, 5, 6])
                                           + xdr_vector('Int32', [10, 30, 50])
                                           + xdr_vector('Int32', [101, 103])
                                           + xdr_string('n')), req)

    def test_unsupported_dds_constructs_rejected(self):
        raw = b'Dataset { Sequence { Int32 z; } s; } obs;\nData:\n'
        req = self._build_request([])
        with self.assertRaises(Dap2ProtocolError):
            decode_response(raw, req)
        raw2 = b'Dataset { Structure { Int32 z; } t; } obs;\nData:\n'
        with self.assertRaises(Dap2ProtocolError):
            decode_response(raw2, req)

    def test_delimiter_must_follow_dds_grammar(self):
        # 负载里的 Data: 不能替代真正的分隔符。
        raw = b'Dataset { Int16 z; } obs; Data:\n' + u32(1)
        req = self._build_request([VarEntry('z', (), 'Int16')])
        with self.assertRaises(Dap2ProtocolError):
            decode_response(raw, req)

    def test_malformed_dds_tokens_rejected(self):
        req = self._build_request([])
        cases = [
            b'Dataset { Grid { } g; } obs;\nData:\n',          # 缺 ARRAY
            b'Dataset { Int16 z[y = -1]; } obs;\nData:\n',     # 负维长
            b'Dataset { Int16 z[y = x]; } obs;\nData:\n',      # 非整数维长
            b'Dataset { func(x) z; } obs;\nData:\n',           # 服务端函数
            b'Dataset { Int16 z } obs;\nData:\n',              # 缺分号
            b'Dataset { Int16 z; } obs;\rData:\n',             # 分隔符不符
            b'Dataset {\nData:\n',                             # DDS 提前结束
            b'NotDataset { Int16 z; } obs;\nData:\n',          # 非 Dataset 根
            b'Dataset {  } obs;\nData:\n',                     # 空 Dataset
        ]
        for raw in cases:
            with self.subTest(raw=raw):
                with self.assertRaises(Dap2ProtocolError):
                    decode_response(raw, req)


class CacheAndServerTests(unittest.TestCase):
    def setUp(self):
        self.dims = (Dimension('y', 6), Dimension('x', 5))
        self.slices = {'y': Slice(0, 2, 4), 'x': Slice(1, 2, 3)}
        self.req = FrozenRequest(
            'https://local.invalid/dods', 'v1', self.dims, self.slices,
            (VarEntry('z', self.dims, 'Int32'),))
        decl = dds_var('Int32', 'z', [('y', 3), ('x', 2)])
        self.dds = dds_wrap([decl])
        self.good = response_body(self.dds, xdr_vector('Int32', [1, 2, 3, 4, 5, 6]))

    def _record(self, body, status=200):
        return {'request': {'method': 'GET', 'url': self.req.url},
                'response': {'status': status, 'headers': {'h': 'k'},
                             'inline_body': body, 'chunk_lengths': [len(body)]}}

    def test_cache_hit_only_after_success_and_keyed_by_ce_and_version(self):
        server = ReplayServer([self._record(self.good), self._record(self.good)], '/tmp')
        client = Dap2Client(server)
        first = client.capture(self.req)
        second = client.capture(self.req)
        self.assertFalse(first['cached'])
        self.assertTrue(second['cached'])
        self.assertEqual(len(server.requests), 1)  # 第二次完全复用

    def test_failed_body_never_cached(self):
        # 双长度字第二字为 5，与请求推导元素数 6 不符；payload 恰好完整，
        # 失败点确定在长度字校验，不依赖尾部检测。
        bad = response_body(self.dds,
                            u32(6) + u32(5) + b'\x00\x00\x00\x00' * 6)
        server = ReplayServer([self._record(bad), self._record(self.good)], '/tmp')
        client = Dap2Client(server)
        with self.assertRaises(Dap2ProtocolError):
            client.capture(self.req)
        ok = client.capture(self.req)
        self.assertFalse(ok['cached'])
        again = client.capture(self.req)
        self.assertTrue(again['cached'])
        # 失败 1 次 + 成功 1 次，第三次为缓存命中，不再访问服务端。
        self.assertEqual(len(server.requests), 2)
        self.assertEqual(client.errors[0]['reason'], 'protocol')

    def test_version_change_does_not_merge_coordinates(self):
        req_v2 = FrozenRequest(
            'https://local.invalid/dods', 'v2', self.dims, self.slices,
            (VarEntry('z', self.dims, 'Int32'),))
        server = ReplayServer([
            {'request': {'method': 'GET', 'url': self.req.url},
             'response': {'status': 200, 'headers': {},
                          'inline_body': self.good, 'chunk_lengths': [len(self.good)]}},
            {'request': {'method': 'GET', 'url': req_v2.url},
             'response': {'status': 200, 'headers': {},
                          'inline_body': self.good, 'chunk_lengths': [len(self.good)]}},
        ], '/tmp')
        client = Dap2Client(server)
        r1 = client.capture(self.req)
        r2 = client.capture(req_v2)
        self.assertEqual(r1['source_version'], 'v1')
        self.assertEqual(r2['source_version'], 'v2')
        self.assertFalse(r2['cached'])
        self.assertEqual(len(server.requests), 2)

    def test_server_error_text_archived_not_empty_array(self):
        text = 'Error { code = 404; message = "no such variable"; }'
        server = ReplayServer([self._record(text, status=403)], '/tmp')
        client = Dap2Client(server)
        with self.assertRaises(Dap2ServerError) as ctx:
            client.capture(self.req)
        self.assertEqual(ctx.exception.status, 403)
        self.assertEqual(ctx.exception.body_text, text)
        archived = client.errors[0]
        self.assertEqual(archived['reason'], 'server')
        self.assertEqual(archived['body_text'], text)
        self.assertEqual(archived['body_range'], [0, len(text.encode('utf8'))])
        # 错误响应之后再请求仍然实际访问服务端。
        self.assertEqual(len(server.requests), 1)

    def test_headers_replayed_and_chunked_inline_body(self):
        rec = self._record(self.good)
        rec['response']['chunk_lengths'] = [7, 13, 1000]
        server = ReplayServer([rec], '/tmp')
        client = Dap2Client(server)
        res = client.capture(self.req)
        self.assertEqual(server.requests[0]['headers'], {})
        self.assertEqual(res['response_headers'], {'h': 'k'})
        self.assertEqual([e['value'] for e in res['variables'][0]['elements']],
                         [1, 2, 3, 4, 5, 6])


class ManifestEntryTests(unittest.TestCase):
    def test_request_from_manifest_and_capture_helper(self):
        m = json.loads((DATA / 'normal.json').read_text(encoding='utf8'))
        res = capture_manifest(m, DATA)
        self.assertEqual(res['ce'], 'g[0:2:2][1:2:3],note')
        self.assertEqual(res['source_version'], 'array-v1')
        self.assertFalse(res['cached'])

    def test_manifest_bad_slices_rejected(self):
        m = json.loads((DATA / 'normal.json').read_text(encoding='utf8'))
        m['slices']['x'] = [1, 0, 3]
        with self.assertRaises(Dap2ConstraintError):
            request_from_manifest(m)


class ScalarSlotTests(unittest.TestCase):
    def test_scalar_slot_range_enforced(self):
        dims = (Dimension('y', 2),)
        req = FrozenRequest('u', 'v1', dims, {'y': Slice(0, 1, 1)},
                            (VarEntry('b', (), 'Byte'),
                             VarEntry('w', (), 'UInt16'),
                             VarEntry('i', (), 'Int16')))

        def body(bv, wv, iv):
            dds = b'Dataset { Byte b; UInt16 w; Int16 i; } obs;\nData:\n'
            return dds + struct.pack('>I', bv) + struct.pack('>I', wv) + struct.pack('>i', iv)

        ok = decode_response(body(255, 65535, -32768), req)
        self.assertEqual([v['elements'][0]['value'] for v in ok['variables']],
                         [255, 65535, -32768])
        for bv, wv, iv, label in (
                (256, 1, 1, 'Byte'), (0, 65536, 1, 'UInt16'),
                (0, 1, -32769, 'Int16')):
            with self.subTest(label):
                with self.assertRaises(Dap2ProtocolError):
                    decode_response(body(bv, wv, iv), req)


if __name__ == '__main__':
    unittest.main()
