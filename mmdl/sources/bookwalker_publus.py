"""Publus decoding adapted from Keiyoushi; see BOOKWALKER_LICENSE.txt (Apache-2.0)."""
import base64
import io
import json

from Crypto.Cipher import ARC4
from PIL import Image


def _rc4(data, key):
    return list(ARC4.new(bytes(key)).decrypt(bytes(data)))


class _ConfigDecoder:
    def __init__(self, data):
        header = base64.b64decode(data[:128])
        self.keys = [list(header[i:i + 32]) for i in range(0, 96, 32)]
        self.payload = list(base64.b64decode(data[128:]))
        self.filename = list(b"configuration_pack.json")

    def _mutate(self, mode):
        if mode:
            index = 3 - mode
            target = self.keys[index]
            inputs = [key for i, key in enumerate(self.keys) if i != index]
        else:
            target, inputs = self.payload, self.keys
        total = sum(map(sum, inputs)) & 255
        xor = 0
        for key in inputs:
            for value in key:
                xor ^= value
        shift = (xor >> 5) & 7
        for start in range(0, len(target), 32):
            block = target[start:start + 32]
            running_sum, running_xor = total, xor
            for i, value in enumerate(block):
                if not total & 2:
                    value = ((value & 85) << 1) | ((value >> 1) & 85)
                if not total & 4:
                    value = ((value & 51) << 2) | ((value >> 2) & 51)
                if not total & 8:
                    value = ((value & 15) << 4) | ((value >> 4) & 15)
                block[i] = value
                running_sum = (running_sum + value) & 255
                running_xor ^= value
            for pos in range(len(block)):
                for bit in range(1, 7):
                    mask = 1 << bit
                    if pos & (mask - 1) != mask - 1:
                        break
                    if running_sum & mask != mask:
                        left, right = pos - (1 << (bit - 1)), pos
                        while right > pos - (1 << (bit - 1)):
                            block[left], block[right] = block[right], block[left]
                            left -= 1
                            right -= 1
            rotation = running_xor >> 3
            rotation = rotation % len(block) if start + 32 > len(target) else rotation & 31
            for i in range(len(block)):
                source = (len(block) - rotation + i) % len(block)
                target[start + i] = block[source] if not shift else (
                    ((block[(source - 1) % len(block)] << (8 - shift)) & 255) | (block[source] >> shift)
                )

    def decode(self):
        self._mutate(0)
        key = self.keys[1] + self.filename + self.keys[2]
        box, j = list(range(256)), 0
        for i in range(256):
            j = (j + box[i] + key[i % len(key)]) & 255
            box[i], box[j] = box[j], box[i]
        self.payload = [value ^ box[i % 256] for i, value in enumerate(self.payload)]
        for start, key in (
            ((len(self.payload) | 1) - 2, self.filename + self.keys[0] + self.keys[1]),
            ((len(self.payload) - 1) & -2, self.keys[2] + self.filename + self.keys[0]),
        ):
            for i, value in enumerate(_rc4([0] * (start // 2 + 1), key)):
                self.payload[start - 2 * i] ^= value
        arrays = self.keys + [self.payload]
        for i in range(min(32, len(self.payload))):
            value = self.payload[i] ^ self.keys[0][i] ^ self.keys[1][i] ^ self.keys[2][i]
            left, right = arrays[(value & 12) >> 2], arrays[value & 3]
            left[i], right[i] = right[i], left[i]
            left, right = arrays[(value & 192) >> 6], arrays[(value & 48) >> 4]
            left[i], right[i] = right[i], left[i]
        self.keys[2] = _rc4(self.keys[2], self.keys[1] + self.keys[0] + self.filename)
        self.keys[1] = _rc4(self.keys[1], self.keys[0] + self.filename + self.keys[2])
        self.keys[0] = _rc4(self.keys[0], self.filename + self.keys[2] + self.keys[1])
        for mode in (1, 2, 3):
            self._mutate(mode)
        result = _rc4(self.payload, self.keys[2] + self.keys[1] + self.filename)
        return json.loads(bytes(result)), self.keys


def decode_config(root):
    if root.get("data") is not None:
        return _ConfigDecoder(root["data"]).decode()
    return root, []


def image_filename(page_id, keys, number):
    if not keys or not keys[0]:
        return f"{page_id}/{number}.jpeg"
    combined = [a ^ b ^ c for a, b, c in zip(*keys)]
    filename = str(number)
    parent = page_id + "/"
    chars = [0, 59] + list((parent + filename).encode("utf-16-be"))
    repeats, length = 3, len(filename) * 2 + len(chars) * 2
    while length < 256:
        repeats += 1
        length += len(chars)
    high, middle, low = 1670739, 1282576, 2237221
    i, j = 2 + len(parent.encode("utf-16-be")), 0
    for _ in range(repeats):
        while i < len(chars):
            low ^= chars[i] ^ combined[j]
            i += 1
            j = (j + 1) % len(combined)
            next_low = 435 * low
            next_middle = 435 * middle + ((low & 7) << 18) + (next_low >> 22)
            next_high = 435 * high + ((middle & 3) << 19) + ((low & 4194296) >> 3) + (next_middle >> 21)
            low, middle, high = next_low & 4194303, next_middle & 2097151, next_high & 2097151
        i = 0
    values = (
        high >> 13, (high >> 5) & 255, ((high & 31) << 3) | (middle >> 18),
        (middle >> 10) & 255, (middle >> 2) & 255, ((middle & 3) << 6) | (low >> 16),
        (low >> 8) & 255, low & 255,
    )
    digest = bytes(value ^ combined[i] for i, value in enumerate(values)).hex()
    return f"{parent}10{digest}.jpeg"


_SHIFTS = [[1,3,10],[1,5,16],[1,5,19],[1,9,29],[1,11,6],[1,11,16],[1,19,3],[1,21,20],[1,27,27],[2,5,15],[2,5,21],[2,7,7],[2,7,9],[2,7,25],[2,9,15],[2,15,17],[2,15,25],[2,21,9],[3,1,14],[3,3,26],[3,3,28],[3,3,29],[3,5,20],[3,5,22],[3,5,25],[3,7,29],[3,13,7],[3,23,25],[3,25,24],[3,27,11],[4,3,17],[4,3,27],[4,5,15],[5,3,21],[5,7,22],[5,9,7],[5,9,28],[5,9,31],[5,13,6],[5,15,17],[5,17,13],[5,21,12],[5,27,8],[5,27,21],[5,27,25],[5,27,28],[6,1,11],[6,3,17],[6,17,9],[6,21,7],[6,21,13],[7,1,9],[7,1,18],[7,1,25],[7,13,25],[7,17,21],[7,25,12],[7,25,20],[8,7,23],[8,9,23],[9,5,14],[9,5,25],[9,11,19],[9,21,16],[10,9,21],[10,9,25],[11,7,12],[11,7,16],[11,17,13],[11,21,13],[12,9,23],[13,3,17],[13,3,27],[13,5,19],[13,17,15],[14,1,15],[14,13,15],[15,1,29],[17,15,20],[17,15,23],[17,15,26]]


class _Random:
    def __init__(self, shifts, mode, seed):
        self.shifts = _SHIFTS[shifts]
        self.mode = mode
        self.seed(seed)

    def seed(self, value):
        self.state = (value & 0xffffffff) or 2463534242

    def next(self, bound):
        if bound <= 1:
            return 0
        a, b, c = self.shifts
        operations = (
            ((a, True), (b, False), (c, True)),
            ((c, True), (b, False), (a, True)),
            ((a, False), (b, True), (c, False)),
            ((c, False), (b, True), (a, False)),
            ((a, True), (c, True), (b, False)),
            ((a, False), (c, False), (b, True)),
        )[self.mode]
        while True:
            value = self.state
            for shift, left in operations:
                value = (value ^ (value << shift if left else value >> shift)) & 0xffffffff
            self.state = value
            candidate = (value - 1) & 0xffffffff
            result = candidate % bound
            if candidate - result <= ((-1 - bound) & 0xffffffff):
                return result


def _shuffle(random, size):
    result = [0] * size
    for i in range(size):
        index = random.next(i + 1)
        result[i], result[index] = result[index], i
    return result


def _edges(random, width, height, cut_x, cut_y):
    x_edges, y_edges = [0] * width, [0] * height
    remaining_x, remaining_y = width, height
    x = y = 0
    while remaining_x + remaining_y:
        choice = random.next(remaining_x + remaining_y)
        if choice < remaining_x:
            start, end = y, y + remaining_y
            if choice < cut_x:
                while start > 0 and x < y_edges[start - 1]:
                    start -= 1
                while end < height and x < y_edges[end]:
                    end += 1
                x_edges[x] = random.next(end - start) + start
                x += 1
                cut_x -= 1
            else:
                while start > 0 and x + remaining_x > y_edges[start - 1]:
                    start -= 1
                while end < height and x + remaining_x > y_edges[end]:
                    end += 1
                x_edges[x + remaining_x - 1] = random.next(end - start) + start
            remaining_x -= 1
        else:
            start, end = x, x + remaining_x
            if choice - remaining_x < cut_y:
                while start > 0 and y < x_edges[start - 1]:
                    start -= 1
                while end < width and y < x_edges[end]:
                    end += 1
                y_edges[y] = random.next(end - start) + start
                y += 1
                cut_y -= 1
            else:
                while start > 0 and y + remaining_y > x_edges[start - 1]:
                    start -= 1
                while end < width and y + remaining_y > x_edges[end]:
                    end += 1
                y_edges[y + remaining_y - 1] = random.next(end - start) + start
            remaining_y -= 1
    return x_edges, y_edges


def _permutation(p1, p2, p3, p4):
    mask = 0xffffffff
    upper1, upper2, upper3, upper4 = (value >> 16 for value in (p1, p2, p3, p4))
    selector = (upper2 ^ upper3 ^ upper4) >> 16
    random = _Random(selector // 6 % len(_SHIFTS), selector % 6, (p2 ^ p3 ^ p4) & mask)
    common = random.next(65536) | (random.next(65536) << 16)
    selector = ((upper1 ^ upper4) >> 16) ^ random.next(512)
    width, height = upper2 >> 16, upper3 >> 16
    random = _Random(selector // 6 % len(_SHIFTS), selector % 6, (p1 ^ p2 ^ common) & mask)
    grid = _shuffle(random, width * height)
    random.seed((p1 ^ p3 ^ common) & mask)
    cut_x = random.next(width + 1) if width < 4 else random.next(width - 1) + 1
    cut_y = random.next(height + 1) if height < 4 else random.next(height - 1) + 1
    alternate_x = random.next(width)
    alternate_x = 0 if width <= 0 else alternate_x if alternate_x < cut_x else alternate_x + 1
    alternate_y = random.next(height)
    alternate_y = 0 if height <= 0 else alternate_y if alternate_y < cut_y else alternate_y + 1
    random.seed((p1 ^ p4 ^ common) & mask)
    x_edges, y_edges = _edges(random, width, height, cut_x, cut_y)
    columns, rows = _shuffle(random, width), _shuffle(random, height)
    x_other, y_other = _edges(random, width, height, alternate_x, alternate_y)
    stride_x, stride_y = (width + 1) * 2, (height + 1) * 2
    result = []
    for x in range(width):
        for y in range(height):
            nx, ny = grid[x + y * width] % width, grid[x + y * width] // width
            ox = x if x < y_edges[y] else x + width + 1
            oy = y if y < x_edges[x] else y + height + 1
            ix = nx if nx < y_other[ny] else nx + width + 1
            iy = ny if ny < x_other[nx] else ny + height + 1
            result.extend((iy * stride_x + ox, ix * stride_y + oy))
    result.extend((alternate_y * stride_x + cut_x, alternate_x * stride_y + cut_y))
    for x, nx in enumerate(columns):
        ox = x if x < cut_x else x + width + 1
        ix = nx if nx < alternate_x else nx + width + 1
        result.extend((x_other[nx] * stride_x + ox, ix * stride_y + x_edges[x]))
    for y, ny in enumerate(rows):
        oy = y if y < cut_y else y + height + 1
        iy = ny if ny < alternate_y else ny + height + 1
        result.extend((iy * stride_x + y_edges[y], y_other[ny] * stride_y + oy))
    return result


def _keyed_moves(width, height, page_id, page, keys):
    value = 47 + sum(map(ord, page_id + str(page.get("No", 0)))) + sum(map(sum, keys))
    repeated = (value & 255) * 0x01010101
    selector = value % (len(_SHIFTS) * 6)
    seeds = []
    for key, field in zip(keys, ("NS", "PS", "RS")):
        packed = 0
        for i in range(0, min(len(key) & -4, 32), 4):
            packed ^= int.from_bytes(bytes(key[i:i + 4]), "big")
        seeds.append((repeated ^ packed ^ page.get(field, 0)) & 0xffffffff)
    block_w, block_h = page["BlockWidth"], page["BlockHeight"]
    cols, rows = width // block_w, height // block_h
    rest_w, rest_h = width % block_w, height % block_h
    setting = selector ^ cols ^ rows
    random = _Random(setting // 6 % len(_SHIFTS), setting % 6, seeds[0] ^ seeds[1] ^ seeds[2])
    start = random.next(65536) + random.next(65536) * 65536 + random.next(512) * 4294967296
    order = _permutation(start, (cols << 32) + seeds[0], (rows << 32) + seeds[1], (selector << 32) + seeds[2])
    offset = 0
    for count, tile_w, tile_h in (
        (cols * rows, block_w, block_h), (1, rest_w, rest_h),
        (cols, block_w, rest_h), (rows, rest_w, block_h),
    ):
        end = offset + count * 2
        if tile_w and tile_h:
            for i in range(offset, end, 2):
                first, second = order[i:i + 2]
                ox, oy = first % ((cols + 1) * 2), second % ((rows + 1) * 2)
                ix, iy = second // ((rows + 1) * 2), first // ((cols + 1) * 2)
                x = lambda n: n * block_w - ((cols + 1) * block_w - rest_w if n > cols else 0)
                y = lambda n: n * block_h - ((rows + 1) * block_h - rest_h if n > rows else 0)
                yield x(ox), y(oy), x(ix), y(iy), tile_w, tile_h
        offset = end


def _plain_moves(width, height, pattern):
    cols, rows = width // 64, height // 64
    rest_w, rest_h = width % 64, height % 64
    # Kotlin's remainder keeps the sign when small images have fewer than four tiles.
    remainder = lambda a, b: a % b if a >= 0 else -((-a) % b)
    def cut(size, factor):
        if not size:
            return 0
        value = size - factor * pattern % size
        if value % size == 0:
            value = remainder(size - 4, size)
        return value or size - 1
    cut_x, cut_y = cut(cols, 43), cut(rows, 47)
    def x_rest(row):
        if (row < cut_y) == (pattern % 2 == 1):
            size, start = cols - cut_x, cut_x
        else:
            size, start = cut_x, 0
        return (row + 67 * pattern + cut_x + 71) % size + start if size else 0
    def y_rest(col):
        if (col < cut_x) == (pattern % 2 == 1):
            size, start = cut_y, 0
        else:
            size, start = rows - cut_y, cut_y
        return (col + 53 * pattern + 59 * cut_y) % size + start if size else 0
    pos_x = lambda n: n * 64 + (rest_w if n >= cut_x else 0)
    pos_y = lambda n: n * 64 + (rest_h if n >= cut_y else 0)
    if rest_w and rest_h:
        yield cut_x * 64, cut_y * 64, cut_x * 64, cut_y * 64, rest_w, rest_h
    if rest_h:
        for col in range(cols):
            target = (col + 61 * pattern) % cols
            yield pos_x(col), cut_y * 64, pos_x(target), y_rest(target) * 64, 64, rest_h
    if rest_w:
        for row in range(rows):
            target = (row + 73 * pattern) % rows
            yield cut_x * 64, pos_y(row), x_rest(target) * 64, pos_y(target), rest_w, 64
    for col in range(cols):
        for row in range(rows):
            target_x = (col + 29 * pattern + 31 * row) % cols
            target_y = (row + 37 * pattern + 41 * target_x) % rows
            ix = target_x * 64 + (rest_w if target_x >= x_rest(target_y) else 0)
            iy = target_y * 64 + (rest_h if target_y >= y_rest(target_x) else 0)
            yield pos_x(col), pos_y(row), ix, iy, 64, 64


def restore_image(data, page_id, page, keys):
    with Image.open(io.BytesIO(data)) as opened:
        if page.get("DummyWidth") is None and opened.format == "JPEG":
            return data
        image = opened.convert("RGB")
    if page.get("DummyWidth") is not None:
        restored = Image.new("RGB", image.size)
        if keys and keys[0] and page.get("BlockWidth", 0):
            moves = _keyed_moves(*image.size, page_id, page, keys)
        else:
            pattern = sum(map(ord, f"{page_id}/{page.get('No', 0)}")) % 4 + 1
            moves = _plain_moves(*image.size, pattern)
        for ox, oy, ix, iy, width, height in moves:
            restored.paste(image.crop((ix, iy, ix + width, iy + height)), (ox, oy))
        image = restored
        width, height = page["Size"]["Width"], page["Size"]["Height"]
        if width > 0 and height > 0:
            image = image.crop((0, 0, width, height))
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=90)
    return output.getvalue()
