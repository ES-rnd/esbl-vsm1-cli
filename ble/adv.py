"""Advertisement reconstruction & ESS device filter."""

from ..config import TARGET_INDEX, TARGET_VALUE, TARGET_LENGTH


def build_raw_adv(adv) -> bytearray:
    """
        Reconstruct the raw advertisement payload
        from a bleak AdvertisementData.
    """
    raw = bytearray()

    if adv.local_name:
        nb = adv.local_name.encode()
        raw += bytes([len(nb) + 1, 0x09]) + nb

    for cid, data in adv.manufacturer_data.items():
        mfg = cid.to_bytes(2, "little") + data
        raw += bytes([len(mfg) + 1, 0xFF]) + mfg

    return raw


def is_ess_device(adv) -> bool:
    """
        ESS filter:
          - reconstructed ADV length must be exactly TARGET_LENGTH
          - byte at TARGET_INDEX must equal TARGET_VALUE
    """
    raw = build_raw_adv(adv)

    # Length check first — guards every downstream index access
    if len(raw) != TARGET_LENGTH:
        return False

    if len(raw) <= TARGET_INDEX:        # belt-and-suspenders
        return False

    return raw[TARGET_INDEX] == TARGET_VALUE
