"""v2 Agents — list agents, pools, and status history (read-only)."""

from uat.common import TestContext


def run(ctx: TestContext) -> None:

    @ctx.step("list_agents — paginate registered agents")
    def agents_page():
        return ctx.client.list_agents(size=10)

    @ctx.step("list_agent_pools — paginate agent pools")
    def pools_page():
        return ctx.client.list_agent_pools(size=10)

    # If at least one agent exists, fetch its status history
    agent = None
    if agents_page and agents_page.items:
        agent = agents_page.items[0]

    if not agent:
        ctx.skip("get_agent", "no agents registered on platform")
        ctx.skip("list_agent_status_history", "no agents registered on platform")
        return

    @ctx.step("get_agent — fetch an agent by id")
    def fetched():
        return ctx.client.get_agent(agent.id)

    @ctx.step("list_agent_status_history — fetch status history for an agent")
    def status_history():
        return ctx.client.list_agent_status_history(agent.id, size=10)
