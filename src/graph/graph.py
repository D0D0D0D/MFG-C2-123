"""Outer fixed AgentBaseGraph for SDSRevisionEvidenceAgent."""

from __future__ import annotations

from framework.graph.agent_base_graph import AgentBaseGraph
from src.nodes.main_node import SdsRevisionWorkflowGraphNode
from src.nodes.post_process_node import PostProcessNode
from src.nodes.pre_process_node import PreProcessNode
from src.schemas.state import SdsRevisionState
from src.services.service import wire_services


class SdsRevisionEvidenceGraph(AgentBaseGraph):
    """Five-node Cat2 backbone; all mode branching stays inside ``main``."""

    def __init__(self, config: dict | None = None) -> None:
        super().__init__(config=wire_services(config))

    @property
    def name(self) -> str:
        return "SDSRevisionEvidenceAgent"

    @property
    def state_schema(self) -> type:
        return SdsRevisionState

    def register_nodes(self) -> None:
        super().register_nodes()
        self._nodes["pre_process"] = PreProcessNode(store=self.config["store"])
        self._nodes["main"] = SdsRevisionWorkflowGraphNode(config=self.config)
        self._nodes["post_process"] = PostProcessNode()

    def get_output(self, state: SdsRevisionState) -> dict:
        """Expose stable structured failure metadata at the public boundary.

        ``AgentBaseGraph.get_output()`` returns neither ``error_code`` nor
        ``error_log``, so a node that recorded *why* it failed still reaches the
        caller as a bare ``status: error``. Surfacing them here is the only
        place the caller can see them; a node-level unit test cannot prove it.
        ``error_log`` is de-duplicated because it is an ``operator.add`` channel
        and the same line can be appended by more than one node.
        """
        output = super().get_output(state)
        output.update(
            error_code=state.get("error_code"),
            error_type=state.get("error_type"),
            error_log=list(dict.fromkeys(state.get("error_log", []))),
        )
        return output


# The manifest and server use the explicit product name.  This compatibility
# alias avoids a break for local code generated during the MR-A scaffold phase.
Graph = SdsRevisionEvidenceGraph
