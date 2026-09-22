"""Minimal Hatch Rest (1st gen, BLE) client + CLI.

Protocol borrowed from pyhatchbabyrest; this version keeps one connection open
instead of reconnecting per command.

  python hatch.py status
  python hatch.py on | off
  python hatch.py color R G B      # 0-255 each
  python hatch.py brightness N     # 0-255
  python hatch.py volume N         # 0-255
  python hatch.py sound NAME       # see SOUNDS
"""
import asyncio
import os
import sys
from enum import IntEnum

from bleak import BleakClient, BleakScanner

CHAR_TX = "02240002-5efd-47eb-9c1a-de53f7a2b232"
CHAR_FEEDBACK = "02260002-5efd-47eb-9c1a-de53f7a2b232"
HATCH_MFR_ID = 1076
COLOR_GRADIENT = (254, 254, 254)  # rainbow mode


class Sound(IntEnum):
    none = 0
    stream = 2
    noise = 3
    dryer = 4
    ocean = 5
    wind = 6
    rain = 7
    bird = 9
    crickets = 10
    brahms = 11
    twinkle = 13
    rockabye = 14


SOUNDS = [s.name for s in Sound]


class Hatch:
    def __init__(self, address: str | None = None):
        self.address = address or os.environ.get("HATCH_ADDR")
        self.client: BleakClient | None = None
        self.power = False
        self.color = (0, 0, 0)
        self.brightness = 0
        self.sound = Sound.none
        self.volume = 0

    async def connect(self, timeout: float = 15.0):
        if self.address:
            dev = await BleakScanner.find_device_by_address(self.address, timeout=timeout)
        else:
            dev = await BleakScanner.find_device_by_filter(
                lambda d, adv: HATCH_MFR_ID in adv.manufacturer_data, timeout=timeout
            )
        if dev is None:
            raise RuntimeError("Hatch not found (is it powered? set HATCH_ADDR?)")
        self.address = dev.address
        self.client = BleakClient(dev)
        await self.client.connect()
        await self.refresh()
        return self

    async def disconnect(self):
        if self.client:
            await self.client.disconnect()
            self.client = None

    @property
    def connected(self) -> bool:
        return bool(self.client and self.client.is_connected)

    async def _ensure(self):
        if not self.connected:
            await self.connect()

    async def _send(self, cmd: str):
        await self._ensure()
        await self.client.write_gatt_char(CHAR_TX, bytearray(cmd, "utf-8"), response=True)
        await asyncio.sleep(1.5)  # feedback char lags ~1s behind writes
        await self.refresh()

    async def refresh(self):
        await self._ensure()
        raw = await self.client.read_gatt_char(CHAR_FEEDBACK)
        b = list(raw)
        # layout: ... 'C' r g b bright 'S' sound vol 'P' flags ...
        assert b[5] == 0x43 and b[10] == 0x53 and b[13] == 0x50, f"unexpected feedback: {raw.hex()}"
        self.color = tuple(b[6:9])
        self.brightness = b[9]
        try:
            self.sound = Sound(b[11])
        except ValueError:
            self.sound = Sound.none
        self.volume = b[12]
        self.power = not (b[14] & 0b11000000)

    def state(self) -> dict:
        return {
            "connected": self.connected,
            "address": self.address,
            "power": self.power,
            "color": list(self.color),
            "brightness": self.brightness,
            "sound": self.sound.name,
            "volume": self.volume,
        }

    # ---- commands ----
    async def power_on(self):
        await self._send("SI01")

    async def power_off(self):
        await self._send("SI00")

    async def set_sound(self, sound: Sound | int | str):
        if isinstance(sound, str):
            sound = Sound[sound]
        await self._send(f"SN{int(sound):02x}")

    async def set_volume(self, volume: int):
        await self._send(f"SV{_u8(volume):02x}")

    async def set_color(self, r: int, g: int, b: int, brightness: int | None = None):
        br = self.brightness if brightness is None else brightness
        await self._send(f"SC{_u8(r):02x}{_u8(g):02x}{_u8(b):02x}{_u8(br):02x}")

    async def set_brightness(self, brightness: int):
        await self.set_color(*self.color, brightness=brightness)


def _u8(v) -> int:
    return max(0, min(255, int(v)))


async def _cli(argv: list[str]):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return
    cmd, *args = argv
    h = await Hatch().connect()
    try:
        match cmd:
            case "status":
                pass
            case "on":
                await h.power_on()
            case "off":
                await h.power_off()
            case "color":
                await h.set_color(*map(int, args[:3]))
            case "brightness":
                await h.set_brightness(int(args[0]))
            case "volume":
                await h.set_volume(int(args[0]))
            case "sound":
                await h.set_sound(args[0])
            case "raw":  # escape hatch: python hatch.py raw SI01
                await h._send(args[0])
            case _:
                print(f"unknown command {cmd!r}\n{__doc__}")
                return
        print(h.state())
    finally:
        await h.disconnect()


if __name__ == "__main__":
    asyncio.run(_cli(sys.argv[1:]))
