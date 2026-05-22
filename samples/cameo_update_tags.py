"""
Cameo update_tags — modify tagged values on Cameo elements.

Workflow:
  1. Upload a Cameo model (.mdzip or .mdxml)
  2. Run @istari:extract to get element IDs from blocks.json / requirements.json
  3. Run @istari:update_tags with a list of element updates
  4. Output is a new revision of the same .mdzip (original file_id preserved)

Notes:
  - `updates` is a list. Each item needs `element_id` + a non-empty `tags` dict.
    Empty tags → validation failure.
  - `tags` is flat: {"name": "value"}. Not nested, not a list.
  - `element_id` comes from extract artifacts (blocks.json for blocks,
    requirements.json for requirements). Run @istari:extract first if you
    don't have IDs.
  - tool_version options: "2024x Refresh2", "2022x Refresh2", "2021x Refresh2".
    A mismatch → 404.
  - For TWC-hosted models use @istari:twc_update_tags instead, and supply
    teamwork_cloud link + twc_auth_login.
"""

import os
import time

from dotenv import load_dotenv
from istari_digital_client import Client, Configuration, JobStatusName

load_dotenv()
client = Client(Configuration(
    registry_url=os.getenv("ISTARI_DIGITAL_REGISTRY_URL"),
    registry_auth_token=os.getenv("ISTARI_DIGITAL_REGISTRY_AUTH_TOKEN"),
))

# 1. Upload Cameo model (.mdzip or .mdxml)
model = client.add_model("/path/to/MyModel.mdzip", display_name="My Cameo Model")

# 2. Extract first — produces blocks.json / requirements.json containing element IDs
extract = client.add_job(
    model_id=str(model.id),
    function="@istari:extract",
    tool_name="dassault_cameo",
    tool_version="2024x Refresh2",
    operating_system="Windows Server 2022",
)

TERMINAL = {JobStatusName.COMPLETED, JobStatusName.FAILED, JobStatusName.CANCELED}
while True:
    extract = client.get_job(str(extract.id))
    if extract.status.name in TERMINAL:
        break
    time.sleep(10)

# 3. Grab element_id from blocks.json or requirements.json artifact
#    (find artifact → read contents → pick id field)
model = client.get_model(str(model.id))
blocks_artifact = next(
    a for a in model.artifacts
    if a.file.revisions[0].name.endswith("blocks.json")
)
blocks = client.read_contents(blocks_artifact.file_revision.token)
# inspect blocks → copy desired element's "id"
element_id = "_2022x_2_abc123..."  # replace with real ID

# 4. Run update_tags
job = client.add_job(
    model_id=str(model.id),
    function="@istari:update_tags",
    tool_name="dassault_cameo",
    tool_version="2024x Refresh2",
    operating_system="Windows Server 2022",
    parameters={
        "updates": [
            {
                "element_id": element_id,
                "tags": {  # flat dict: tag_name → value
                    "hyperlinkText": "FIA 2025",
                    "owner": "rbillings",
                },
            },
            # more elements as needed
        ]
    },
)
print(f"Job {job.id} -> {job.status.name.value}")
