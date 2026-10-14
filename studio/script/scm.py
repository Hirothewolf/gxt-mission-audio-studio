"""Bounded SA PC SCM decoder. Stops a segment at unsupported data; never resyncs."""
import json
from pathlib import Path
import struct


def definitions():
    return json.loads((Path(__file__).parents[1] / 'data' / 'sa_opcodes.json').read_text(encoding='utf-8'))


def argument(data, pos, end):
    if pos >= end:
        raise ValueError('truncated parameter')
    kind = data[pos]
    pos += 1
    sizes = {0: 0, 1: 4, 2: 2, 3: 2, 4: 1, 5: 2, 6: 4, 7: 6, 8: 6,
             9: 8, 10: 2, 11: 2, 12: 6, 13: 6, 15: 16, 16: 2, 17: 2, 18: 6, 19: 6}
    if kind == 14:
        if pos >= end:
            raise ValueError('truncated string length')
        size = data[pos]
        pos += 1
    elif kind in sizes:
        size = sizes[kind]
    else:
        raise ValueError('unsupported parameter type %02X' % kind)
    if pos + size > end:
        raise ValueError('truncated parameter value')
    raw = data[pos:pos + size]
    value = None
    if kind in (1, 4, 5):
        value = int.from_bytes(raw, 'little', signed=True)
    elif kind == 6:
        value = struct.unpack('<f', raw)[0]
    elif kind in (9, 14, 15):
        value = raw.split(b'\0')[0].decode('ascii', errors='strict')
    return {'type': kind, 'value': value}, pos + size


def decode_segment(data, start, end, commands=None):
    commands = commands or definitions()['commands']
    instructions, pos = [], start
    while pos < end:
        origin = pos
        try:
            if pos + 2 > end:
                raise ValueError('truncated opcode')
            raw_opcode = int.from_bytes(data[pos:pos + 2], 'little')
            opcode = '%04X' % (raw_opcode & 0x7fff)
            command = commands.get(opcode)
            if command is None or command.get('num_params') is None:
                raise ValueError('unsupported opcode ' + opcode)
            pos += 2
            params = []
            count = command['num_params']
            inputs = command.get('input', []) + command.get('output', [])
            variable = any(field.get('type') == 'arguments' for field in inputs)
            fixed_count = count - 1 if variable else count
            if fixed_count >= 0:
                for index in range(fixed_count):
                    if index < len(inputs) and inputs[index].get('type') == 'string128':
                        if pos + 128 > end:
                            raise ValueError('truncated debug string')
                        param = {'type': 'raw_string128', 'value': data[pos:pos + 128].split(b'\0')[0].decode('latin-1')}
                        pos += 128
                    else:
                        param, pos = argument(data, pos, end)
                        if param['type'] == 0:
                            raise ValueError('unexpected parameter terminator')
                    params.append(param)
            if count < 0 or variable:
                for _ in range(128):
                    param, pos = argument(data, pos, end)
                    if param['type'] == 0:
                        break
                    params.append(param)
                else:
                    raise ValueError('unterminated variable parameters')
            instructions.append({'offset': origin, 'end': pos, 'opcode': opcode,
                                 'name': command['name'], 'negated': bool(raw_opcode & 0x8000),
                                 'params': params})
        except (ValueError, UnicodeError, struct.error) as exc:
            return instructions, {'offset': origin, 'reason': str(exc), 'unparsed_bytes': end - origin}
    return instructions, None


def segments(data):
    chunks, pos = [], 0
    for _ in range(16):
        if data[pos:pos + 3] != b'\x02\x00\x01':
            break
        destination = struct.unpack_from('<i', data, pos + 3)[0]
        if not pos + 8 <= destination <= len(data):
            raise ValueError('invalid SCM header jump')
        chunks.append((pos, destination, data[pos + 7]))
        pos = destination
    else:
        raise ValueError('too many SCM header chunks')
    chunk = next((a for a in chunks if a[2] == 1), None)
    if not chunk:
        raise ValueError('SA mission header not found')
    header = chunk[0] + 8
    main_size, largest, count, exclusive, locals_count = struct.unpack_from('<IIHHI', data, header)
    table = header + 16
    if table + count * 4 > chunk[1] or not pos <= main_size <= len(data):
        raise ValueError('invalid SCM mission table bounds')
    offsets = list(struct.unpack_from('<%dI' % count, data, table))
    if offsets != sorted(set(offsets)) or any(x < main_size or x >= len(data) for x in offsets):
        raise ValueError('invalid SCM mission offsets')
    result = [{'id': 'MAIN_SCRIPT', 'start': pos, 'end': main_size}]
    for index, start in enumerate(offsets):
        end = offsets[index + 1] if index + 1 < len(offsets) else len(data)
        if end - start > largest:
            raise ValueError('mission exceeds SCM declared maximum')
        result.append({'id': 'MISSION_%03d' % index, 'start': start, 'end': end})
    return result


def decode(data):
    commands = definitions()['commands']
    result = []
    for segment in segments(data):
        instructions, error = decode_segment(data, segment['start'], segment['end'], commands)
        result.append(dict(segment, instructions=instructions, error=error))
    return result
