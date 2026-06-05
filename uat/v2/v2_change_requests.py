"""v2 Change Requests — not available in SDK 10.11.1.

Change request methods (create_change_request, list_change_requests, etc.) are
absent from the Client in SDK 10.11.1. All steps are skipped until the SDK
version is updated to one that includes them.
"""

from uat.common import TestContext


def run(ctx: TestContext) -> None:
    for step in (
        "create_change_request",
        "get_change_request",
        "list_change_requests",
        "close_change_request",
    ):
        ctx.skip(step, "not available in SDK 10.11.1")
