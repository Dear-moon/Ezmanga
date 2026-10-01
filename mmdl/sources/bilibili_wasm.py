"""Go/JS host bindings used by Bilibili's protocol modules."""
import math
import os
from pathlib import Path
import struct
import time

import wasmtime


UNDEFINED = object()
CALLBACK_SOURCE = Path(__file__).with_name("bilibili_callback.js").read_text(encoding="utf-8")


class GoWasm:
    def __init__(self, filename, *, fetch=None):
        self.store = wasmtime.Store()
        module = wasmtime.Module.from_file(self.store.engine, str(filename))
        self.global_object = {
            "Object": lambda: {},
            "Array": lambda size: [UNDEFINED] * int(size),
            "Uint8Array": lambda size: bytearray(int(size)) if isinstance(size, (int, float)) else bytearray(size),
            "fs": {"constants": dict.fromkeys([
                "O_WRONLY", "O_RDWR", "O_CREAT", "O_TRUNC", "O_APPEND", "O_EXCL", "O_DIRECTORY"], -1)},
            "document": {"cookie": ""},
        }
        if fetch is not None:
            self.global_object["fetch"] = fetch
        self.global_object["Object"].defineProperty = self.define_property
        self.go = {"_makeFuncWrapper": self.make_function, "_pendingEvent": None}
        self.values = [math.nan, 0, None, True, False, self.global_object, self.go]
        self.references = {id(self.global_object): 5, id(self.go): 6}
        self.refcounts = [math.inf] * len(self.values)
        self.free_ids = []
        self.timers = {}
        self.next_timer = 0
        imports = [wasmtime.Func(self.store, entry.type, self.callback(entry.name)) for entry in module.imports]
        instance = wasmtime.Instance(self.store, module, imports)
        self.exports = instance.exports(self.store)
        self.memory = self.exports["mem"]
        self.write(4096, b"wasm\0")
        self.write(4104, struct.pack("<QQQ", 4096, 0, 0))
        self.exports["run"](self.store, 1, 4104)

    def callback(self, name):
        return lambda sp: self.handle(name, sp)

    def read(self, offset, length):
        return bytes(self.memory.read(self.store, offset, offset + length))

    def write(self, offset, data):
        self.memory.write(self.store, data, offset)

    def u64(self, offset):
        return struct.unpack("<Q", self.read(offset, 8))[0]

    def put64(self, offset, value):
        self.write(offset, struct.pack("<Q", value))

    def slice(self, offset):
        return self.read(self.u64(offset), self.u64(offset + 8))

    def string(self, offset):
        return self.slice(offset).decode("utf-8")

    def value(self, offset):
        number = struct.unpack("<d", self.read(offset, 8))[0]
        if number == 0:
            return UNDEFINED
        if not math.isnan(number):
            return number
        return self.values[struct.unpack("<I", self.read(offset, 4))[0]]

    def put_value(self, offset, value):
        if value is UNDEFINED:
            self.put64(offset, 0)
            return
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value != 0:
            if math.isnan(value):
                self.write(offset, struct.pack("<II", 0, 0x7ff80000))
            else:
                self.write(offset, struct.pack("<d", value))
            return
        if value is None:
            index = 2
        elif value is True:
            index = 3
        elif value is False:
            index = 4
        elif isinstance(value, (int, float)):
            index = 1
        else:
            identity = id(value)
            if identity not in self.references:
                if self.free_ids:
                    index = self.free_ids.pop()
                    self.values[index] = value
                    self.refcounts[index] = 0
                else:
                    index = len(self.values)
                    self.values.append(value)
                    self.refcounts.append(0)
                self.references[identity] = index
            index = self.references[identity]
            self.refcounts[index] += 1
        flag = 2 if isinstance(value, str) else 4 if callable(value) else 1
        if value is None or isinstance(value, (bool, int, float)):
            flag = 0
        self.write(offset, struct.pack("<II", index, 0x7ff80000 | flag))

    def args(self, offset):
        pointer, count = self.u64(offset), self.u64(offset + 8)
        return [self.value(pointer + 8 * index) for index in range(count)]

    def get(self, value, name):
        if isinstance(value, dict):
            return value.get(name, UNDEFINED)
        if name == "length":
            return len(value)
        if callable(value):
            return getattr(value, name)
        raise NotImplementedError(f"Bilibili WASM property: {name}")

    def define_property(self, target, name, descriptor):
        target[name] = descriptor["value"]
        return target

    def make_function(self, identifier):
        def function(*args):
            event = {"id": identifier, "this": self.global_object, "args": list(args)}
            self.go["_pendingEvent"] = event
            self.exports["resume"](self.store)
            return event["result"]
        # The image module reads the official callback source as protocol metadata.
        function.toString = lambda: CALLBACK_SOURCE
        return function

    def handle(self, name, sp):
        if name == "runtime.wasmExit":
            code = struct.unpack("<i", self.read(sp + 8, 4))[0]
            raise RuntimeError(f"Bilibili WASM exited: {code}")
        elif name == "runtime.wasmWrite":
            # Runtime diagnostics may contain short-lived resource URLs.
            return
        elif name == "runtime.getRandomData":
            self.write(self.u64(sp + 8), os.urandom(self.u64(sp + 16)))
        elif name == "runtime.nanotime1":
            self.put64(sp + 8, time.monotonic_ns())
        elif name == "runtime.walltime":
            now = time.time_ns()
            self.put64(sp + 8, now // 1_000_000_000)
            self.write(sp + 16, struct.pack("<I", now % 1_000_000_000))
        elif name == "runtime.resetMemoryDataView":
            return
        elif name == "syscall/js.finalizeRef":
            index = struct.unpack("<I", self.read(sp + 8, 4))[0]
            self.refcounts[index] -= 1
            if self.refcounts[index] == 0:
                del self.references[id(self.values[index])]
                self.values[index] = UNDEFINED
                self.free_ids.append(index)
        elif name == "runtime.scheduleTimeoutEvent":
            self.next_timer += 1
            self.timers[self.next_timer] = self.u64(sp + 8)
            self.write(sp + 16, struct.pack("<I", self.next_timer))
        elif name == "runtime.clearTimeoutEvent":
            del self.timers[struct.unpack("<I", self.read(sp + 8, 4))[0]]
        elif name == "syscall/js.stringVal":
            self.put_value(sp + 24, self.string(sp + 8))
        elif name == "syscall/js.valueGet":
            self.put_value(sp + 32, self.get(self.value(sp + 8), self.string(sp + 16)))
        elif name == "syscall/js.valueSet":
            self.value(sp + 8)[self.string(sp + 16)] = self.value(sp + 32)
        elif name == "syscall/js.valueIndex":
            self.put_value(sp + 24, self.value(sp + 8)[self.u64(sp + 16)])
        elif name == "syscall/js.valueSetIndex":
            self.value(sp + 8)[self.u64(sp + 16)] = self.value(sp + 24)
        elif name == "syscall/js.valueLength":
            self.put64(sp + 16, len(self.value(sp + 8)))
        elif name == "syscall/js.valueInstanceOf":
            value, constructor = self.value(sp + 8), self.value(sp + 16)
            expected = list if constructor is self.global_object["Array"] else bytearray
            self.write(sp + 24, bytes([isinstance(value, expected)]))
        elif name == "syscall/js.valueCall":
            result = self.get(self.value(sp + 8), self.string(sp + 16))(*self.args(sp + 32))
            sp = self.exports["getsp"](self.store)
            self.put_value(sp + 56, result)
            self.write(sp + 64, b"\x01")
        elif name == "syscall/js.valueNew":
            result = self.value(sp + 8)(*self.args(sp + 16))
            sp = self.exports["getsp"](self.store)
            self.put_value(sp + 40, result)
            self.write(sp + 48, b"\x01")
        elif name == "syscall/js.valuePrepareString":
            encoded = self.value(sp + 8).encode("utf-8")
            self.put_value(sp + 16, encoded)
            self.put64(sp + 24, len(encoded))
        elif name == "syscall/js.valueLoadString":
            self.write(self.u64(sp + 16), self.value(sp + 8)[:self.u64(sp + 24)])
        elif name == "syscall/js.copyBytesToJS":
            destination, source = self.value(sp + 8), self.slice(sp + 16)
            count = min(len(destination), len(source))
            destination[:count] = source[:count]
            self.put64(sp + 40, count)
            self.write(sp + 48, b"\x01")
        elif name == "syscall/js.copyBytesToGo":
            source = self.value(sp + 32)
            count = min(self.u64(sp + 16), len(source))
            self.write(self.u64(sp + 8), source[:count])
            self.put64(sp + 40, count)
            self.write(sp + 48, b"\x01")
        else:
            raise NotImplementedError(f"Bilibili WASM import: {name}")
