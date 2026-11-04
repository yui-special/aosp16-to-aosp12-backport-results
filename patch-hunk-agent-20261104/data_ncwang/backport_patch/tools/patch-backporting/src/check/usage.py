"""Usage lookup is intentionally provider-neutral.

DeepSeek does not expose the retired OpenAI ``/v1/usage`` endpoint used by the
old implementation. Per-run token usage remains available in API responses;
account-level billing must be read from the DeepSeek console.
"""


def get_usage(api_key):
    raise RuntimeError(
        "DeepSeek usage is available in the DeepSeek console; "
        "the legacy OpenAI usage endpoint is disabled"
    )
