"""Conservative FFmpeg detail filters for mobile video exports.

This is deterministic CPU processing, not AI super-resolution. Keep values mild
for high-motion footage to avoid halos, ringing, and loss of fine texture.
"""

DENOISE_FILTERS = {
    "off": None,
    "low": "hqdn3d=1.2:1.0:2.0:1.5",
    "medium": "hqdn3d=2.0:1.5:3.0:2.25",
}
SHARPEN_FILTERS = {
    "off": None,
    "low": "unsharp=5:5:0.45:5:5:0.0",
    "medium": "unsharp=5:5:0.70:5:5:0.0",
}


def build_detail_filters(*, sharpen: str = "low", denoise: str = "low") -> list[str]:
    """Return validated, low-memory FFmpeg filters in denoise-then-sharpen order."""
    if sharpen not in SHARPEN_FILTERS:
        raise ValueError("锐化档位无效。")
    if denoise not in DENOISE_FILTERS:
        raise ValueError("降噪档位无效。")
    return [
        item for item in (DENOISE_FILTERS[denoise], SHARPEN_FILTERS[sharpen])
        if item
    ]
