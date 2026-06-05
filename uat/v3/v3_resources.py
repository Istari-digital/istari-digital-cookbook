"""v3 Resources — create, get, list resources (Model and Artifact types).

Documented at: docs.istaridigital.com/developers/SDK/v3/quick-start
"""

from istari_digital_client.v3.models.resource_type_dto import ResourceTypeDto
from uat.common import TestContext


def run(ctx: TestContext) -> None:

    @ctx.step("create_resource (MODEL) — upload a file as a v3 Model resource")
    def resource():
        return ctx.v3.create_resource(
            path=ctx.data("dummy.txt"),
            resource_type=ResourceTypeDto.MODEL,
            display_name="UAT v3 Resource",
            description="Created by UAT runner",
        )

    if resource:
        ctx.register("v3_resource", resource)

    @ctx.step("get_resource — fetch a v3 resource by id")
    def fetched():
        assert resource, "depends on create_resource"
        return ctx.v3.get_resource(resource.resource_id)

    @ctx.step("create_resource (ARTIFACT) — upload a file as a v3 Artifact resource")
    def artifact():
        return ctx.v3.create_resource(
            path=ctx.data("dummy.txt"),
            resource_type=ResourceTypeDto.ARTIFACT,
            display_name="UAT v3 Artifact",
        )

    if artifact:
        ctx.track("v3_resource", artifact.resource_id)
