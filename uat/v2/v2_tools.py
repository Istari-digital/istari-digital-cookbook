"""v2 Tools & Functions — list tools, functions, modules (read-only)."""

from uat.common import TestContext


def run(ctx: TestContext) -> None:

    @ctx.step("list_functions — list available job functions")
    def functions_page():
        return ctx.client.list_functions(size=20)

    @ctx.step("list_tools — list registered tools")
    def tools_page():
        return ctx.client.list_tools(size=20)

    @ctx.step("list_tool_versions — list all tool versions")
    def tool_versions():
        return ctx.client.list_tool_versions(size=20)

    @ctx.step("list_modules — list available modules")
    def modules_page():
        return ctx.client.list_modules(size=20)

    # Fetch function detail using its UUID id (not name)
    fn = None
    if functions_page and getattr(functions_page, "items", None):
        fn = functions_page.items[0]

    if fn:
        fn_id = getattr(fn, "id", None)
        if fn_id:
            @ctx.step("get_function — fetch a function by id")
            def function_detail():
                return ctx.client.get_function(fn_id)
