"""Platform-specific complete-recitation duration requirements."""

import math


SUPPORTED_PLATFORMS = {"youtube", "tiktok"}


def audio_duration_bounds(catalog, platform="youtube"):
    """Return complete recitation duration bounds, excluding ending silence."""
    if platform not in SUPPORTED_PLATFORMS:
        raise ValueError("Unsupported publishing platform")
    defaults = {
        "youtube": (catalog.get("min_audio_seconds", 30), catalog.get("max_audio_seconds", 58)),
        # Leave room for the configured ending silence in TikTok's 90s cap.
        "tiktok": (61, 88),
    }
    policies = catalog.get("platform_duration_policies", {})
    if not isinstance(policies, dict):
        raise ValueError("Invalid platform duration policies")
    policy = policies.get(platform, {})
    if not isinstance(policy, dict):
        raise ValueError("Invalid platform duration policy")
    minimum = policy.get("min_audio_seconds", defaults[platform][0])
    maximum = policy.get("max_audio_seconds", defaults[platform][1])
    try:
        minimum, maximum = float(minimum), float(maximum)
    except (TypeError, ValueError):
        raise ValueError("Platform audio duration limits must be numbers") from None
    floor, ceiling = (61, 88) if platform == "tiktok" else (30, 58)
    if (not math.isfinite(minimum) or not math.isfinite(maximum) or
            minimum < floor or maximum < minimum or maximum > ceiling):
        raise ValueError("Platform audio duration limits are outside the supported range")
    return minimum, maximum
