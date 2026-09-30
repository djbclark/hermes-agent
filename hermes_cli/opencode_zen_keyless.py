"""FORK(opencode-zen-keyless): the keyless OpenCode Zen free tier.

Upstream removed this tier in v2026.9.24 (their relay started 403ing anonymous
traffic outside the official client), but the fork keeps it: the fork's
no-spend policy (``hermes_cli/runtime_provider.py::_enforce_opencode_zen_free_only``)
routes ``*-free`` slugs anonymously and ``model_cost_guard`` treats them as
free. Everything the fork still needs lives in this module so the next upstream
merge cannot conflict with it:

- ``OPENCODE_ZEN_FREE_KEYLESS_PLACEHOLDER`` — sentinel api_key for anonymous runs
- ``opencode_zen_free_headers()`` — ``Authorization: ""`` so the placeholder
  never ships as a bearer (the relay 401s any unrecognized bearer)
- ``opencode_zen_free_runtime()`` — keyless runtime dict for a free slug picked
  under any OpenCode-family provider (Go selections heal to the Zen relay)

Membership = the verified static floor (``models_catalog_static._PROVIDER_MODELS["opencode-free"]``)
plus any SWR disk-cache entry. Upstream's live-fetch healing (``GET /zen/v1/models``)
went away with their provider table entry and is deliberately NOT restored.
"""

from typing import Optional

OPENCODE_ZEN_FREE_KEYLESS_PLACEHOLDER = "opencode-zen-free-keyless"

# ``-free``-suffixed slugs that are KEYED (Go-subscription) models, NOT anonymous-servable —
# excluded from the keyless catalog despite the suffix (ox-alpha-free is Ox Alpha's Go twin).
_OPENCODE_FREE_KEYED_SUFFIX_MODELS = frozenset({"ox-alpha-free"})


def opencode_zen_free_headers() -> dict:
    """Client default_headers for anonymous Zen free-tier requests. ``Authorization: ""`` overrides the
    OpenAI SDK's ``Bearer <api_key>`` so the placeholder never reaches the wire (the relay 401s any
    unknown bearer). Attribution headers mirror the opencode provider profile."""
    try:
        from hermes_cli import __version__ as _v
    except Exception:
        _v = "0"
    return {
        "Authorization": "",
        "HTTP-Referer": "https://hermes-agent.nousresearch.com",
        "X-Title": "Hermes Agent",
        "User-Agent": f"HermesAgent/{_v}"}


def _opencode_free_known_model_slugs() -> set:
    """Lowercased keyless free-tier slugs known right now WITHOUT network I/O: static floor ∪ SWR
    disk-cache entry. The healing path runs during model resolution and must never block on a fetch."""
    from hermes_cli.models import _load_provider_models_cache
    from hermes_cli.models_catalog_static import _PROVIDER_MODELS
    known = {m.lower() for m in _PROVIDER_MODELS.get("opencode-free", [])}
    try:
        entry = _load_provider_models_cache().get("opencode-free") or {}
        known.update(str(m).lower() for m in entry.get("models", []) or [])
    except Exception:
        pass
    return known


def opencode_zen_free_runtime(provider_id: Optional[str], model_id: Optional[str]) -> Optional[dict]:
    """Keyless runtime entry for an OpenCode Zen free-tier model, or None. Fires when ``provider_id``
    is ``opencode-free`` (EVERY model on it routes anonymously) or when any other OpenCode-family
    provider selected a model in the known keyless catalog (static floor ∪ cached entries),
    healing a free-model pick made under Zen/Go whose keys the free tier rejects."""
    from hermes_cli.models import (
        normalize_opencode_base_url, normalize_opencode_model_id, opencode_model_api_mode,
        opencode_provider_family)
    family = opencode_provider_family(provider_id)
    if family is None:
        return None
    normalized = normalize_opencode_model_id(provider_id, model_id)
    if family != "opencode-free" and normalized.strip().lower() not in _opencode_free_known_model_slugs():
        return None
    api_mode = opencode_model_api_mode("opencode-zen", normalized)
    base_url = normalize_opencode_base_url("opencode-zen", api_mode, "https://opencode.ai/zen/v1")
    return {
        "provider": family,
        "api_mode": api_mode,
        "base_url": base_url,
        "api_key": OPENCODE_ZEN_FREE_KEYLESS_PLACEHOLDER,
        "default_headers": opencode_zen_free_headers(),
        "source": "opencode-zen-free-keyless"}


def merge_keyless_default_headers(api_key: Optional[str], headers: Optional[dict]) -> Optional[dict]:
    """FORK hook used at client-construction sites: when ``api_key`` is the keyless placeholder,
    merge the anonymous headers over ``headers`` so the placeholder never ships as a bearer."""
    if api_key != OPENCODE_ZEN_FREE_KEYLESS_PLACEHOLDER:
        return headers
    return {**(headers or {}), **opencode_zen_free_headers()}
