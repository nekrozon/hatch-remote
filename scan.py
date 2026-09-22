"""Find the Hatch Rest over BLE and print its address."""
import asyncio

from bleak import BleakScanner

HATCH_MFR_ID = 1076


async def main():
    print("scanning 10s... (allow Bluetooth for your terminal if macOS prompts)")
    found = await BleakScanner.discover(timeout=10.0, return_adv=True)
    for dev, adv in found.values():
        is_hatch = HATCH_MFR_ID in adv.manufacturer_data
        tag = "  <-- HATCH" if is_hatch else ""
        if is_hatch or (dev.name and "hatch" in dev.name.lower()):
            print(f"{dev.address}  name={dev.name!r}  rssi={adv.rssi}  mfr={list(adv.manufacturer_data)}{tag}")
    print(f"({len(found)} devices seen)")


asyncio.run(main())
