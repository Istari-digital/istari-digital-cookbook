"""v3 Remotes — list sending and receiving remote connections."""

from uat.common import TestContext


def run(ctx: TestContext) -> None:

    @ctx.step("list_sending_remotes — enumerate outbound remote connections")
    def sending():
        return ctx.v3.list_sending_remotes()

    if sending and sending.items:
        @ctx.step("get_sending_remote — fetch a sending remote by id")
        def _():
            remote_id = sending.items[0].id
            assert isinstance(remote_id, str)
            return ctx.v3.get_sending_remote(remote_id=remote_id)
    else:
        ctx.skip("get_sending_remote", "list_sending_remotes failed or returned no remotes")

    @ctx.step("list_receiving_remotes — enumerate inbound remote connections")
    def receiving():
        return ctx.v3.list_receiving_remotes()

    if receiving and receiving.items:
        @ctx.step("get_receiving_remote — fetch a receiving remote by id")
        def _r():
            remote_id = receiving.items[0].id
            assert isinstance(remote_id, str)
            return ctx.v3.get_receiving_remote(remote_id=remote_id)
    else:
        ctx.skip("get_receiving_remote", "list_receiving_remotes failed or returned no remotes")
