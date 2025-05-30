# FROM: https://github.com/microsoft/FS-Mol/blob/main/fs_mol/modules/graph_readout.py

from abc import ABC, abstractmethod
from typing import List

import torch
import torch.nn as nn
from torch_scatter import scatter, scatter_softmax
from typing_extensions import Literal


class MLP(nn.Module):
    def __init__(
        self, input_dim: int, out_dim: int, hidden_layer_dims: List[int], activation=nn.ReLU()
    ):
        super().__init__()

        layers = []
        cur_hidden_dim = input_dim
        for hidden_layer_dim in hidden_layer_dims:
            layers.append(nn.Linear(cur_hidden_dim, hidden_layer_dim))
            layers.append(activation)
            cur_hidden_dim = hidden_layer_dim
        layers.append(nn.Linear(cur_hidden_dim, out_dim))
        self._layers = nn.Sequential(*layers)

    def forward(self, inputs):
        return self._layers(inputs)


class GraphReadout(nn.Module, ABC):
    def __init__(
        self,
        node_dim: int,
        out_dim: int,
    ):
        """
        Args:
            node_dim: Dimension of each node node representation.
            out_dim: Dimension of the graph representation to produce.
        """
        super().__init__()
        self._node_dim = node_dim
        self._out_dim = out_dim

    @abstractmethod
    def forward(
        self,
        node_embeddings: torch.Tensor,
        node_to_graph_id: torch.Tensor,
        num_graphs: int,
    ) -> torch.Tensor:
        """
        Args:
            node_embeddings: representations of individual graph nodes. A float tensor
                of shape [num_nodes, self.node_dim].
            node_to_graph_id: int tensor of shape [num_nodes], assigning a graph_id to each
                node.
            num_graphs: int scalar, giving the number of graphs in the batch.

        Returns:
            float tensor of shape [num_graphs, out_dim]
        """
        pass


class CombinedGraphReadout(GraphReadout):
    def __init__(
        self,
        node_dim: int,
        out_dim: int,
        num_heads: int,
        head_dim: int,
    ):
        """
        See superclass for first few parameters.

        Args:
            num_heads: Number of independent heads to use for independent weights.
            head_dim: Size of the result of each independent head.
            num_mlp_layers: Number of layers in the MLPs used to compute per-head weights and
                outputs.
        """
        super().__init__(node_dim, out_dim)
        self._num_heads = num_heads
        self._head_dim = head_dim

        # Create weighted_mean, weighted_sum, max pooling layers:
        self._weighted_mean_pooler = MultiHeadWeightedGraphReadout(
            node_dim=node_dim,
            out_dim=out_dim,
            num_heads=num_heads,
            head_dim=head_dim,
            weighting_type="weighted_mean",
        )
        self._weighted_sum_pooler = MultiHeadWeightedGraphReadout(
            node_dim=node_dim,
            out_dim=out_dim,
            num_heads=num_heads,
            head_dim=head_dim,
            weighting_type="weighted_sum",
        )
        self._max_pooler = UnweightedGraphReadout(
            node_dim=node_dim,
            out_dim=out_dim,
            pooling_type="max",
        )

        # Single linear layer to combine results:
        self._combination_layer = nn.Linear(3 * out_dim, out_dim, bias=False)

    def forward(
        self,
        node_embeddings: torch.Tensor,
        node_to_graph_id: torch.Tensor,
        num_graphs: int,
    ) -> torch.Tensor:
        mean_graph_repr = self._weighted_mean_pooler(node_embeddings, node_to_graph_id, num_graphs)
        sum_graph_repr = self._weighted_sum_pooler(node_embeddings, node_to_graph_id, num_graphs)
        max_graph_repr = self._max_pooler(node_embeddings, node_to_graph_id, num_graphs)

        # concat & non-linearity & combine:
        raw_graph_repr = torch.cat((mean_graph_repr, sum_graph_repr, max_graph_repr), dim=1)

        return self._combination_layer(nn.functional.relu(raw_graph_repr))


class MultiHeadWeightedGraphReadout(GraphReadout):
    def __init__(
        self,
        node_dim: int,
        out_dim: int,
        num_heads: int,
        head_dim: int,
        weighting_type: Literal["weighted_sum", "weighted_mean"],
        num_mlp_layers: int = 1,
    ):
        """
        See superclass for first few parameters.

        Args:
            num_heads: Number of independent heads to use for independent weights.
            head_dim: Size of the result of each independent head.
            weighting_type: Type of weighting to use, either "weighted_sum" (weights
                are in [0, 1], obtained through a logistic sigmoid) or "weighted_mean" (weights
                are in [0, 1] and sum up to 1 for each graph, obtained through a softmax).
            num_mlp_layers: Number of layers in the MLPs used to compute per-head weights and
                outputs.
        """
        super().__init__(node_dim, out_dim)
        self._num_heads = num_heads
        self._head_dim = head_dim

        if weighting_type not in (
            "weighted_sum",
            "weighted_mean",
        ):
            raise ValueError(f"Unknown weighting type {weighting_type}!")
        self._weighting_type = weighting_type

        self._scoring_module = MLP(
            input_dim=self._node_dim,
            hidden_layer_dims=[self._head_dim * num_heads] * num_mlp_layers,
            out_dim=num_heads,
        )

        self._transformation_mlp = MLP(
            input_dim=self._node_dim,
            hidden_layer_dims=[self._head_dim * num_heads] * num_mlp_layers,
            out_dim=num_heads * head_dim,
        )
        self._combination_layer = nn.Linear(num_heads * head_dim, out_dim, bias=False)

    def forward(
        self,
        node_embeddings: torch.Tensor,
        node_to_graph_id: torch.Tensor,
        num_graphs: int,
    ) -> torch.Tensor:
        # Step 1: compute scores, then normalise them according to config:
        scores = self._scoring_module(node_embeddings)  # [V, num_heads]

        if self._weighting_type == "weighted_sum":
            weights = torch.sigmoid(scores)  # [V, num_heads]
        elif self._weighting_type == "weighted_mean":
            weights = scatter_softmax(scores, index=node_to_graph_id, dim=0)  # [V, num_heads]
        else:
            raise ValueError(f"Unknown weighting type {self._weighting_type}!")

        # Step 2: compute transformed node representations:
        values = self._transformation_mlp(node_embeddings)  # [V, num_heads * head_dim]
        values = values.view(-1, self._num_heads, self._head_dim)  # [V, num_heads, head_dim]

        # Step 3: apply weights and sum up per graph:
        weighted_values = weights.unsqueeze(-1) * values  # [V, num_heads, head_dim]
        per_graph_values = torch.zeros(
            (num_graphs, self._num_heads * self._head_dim),
            device=node_embeddings.device,
        )
        per_graph_values.index_add_(
            0,
            node_to_graph_id,
            weighted_values.view(-1, self._num_heads * self._head_dim),
        )  # [num_graphs, num_heads * head_dim]

        # Step 4: go to output size:
        return self._combination_layer(per_graph_values)  # [num_graphs, out_dim]


class UnweightedGraphReadout(GraphReadout):
    def __init__(
        self,
        node_dim: int,
        out_dim: int,
        pooling_type: Literal["min", "max", "sum", "mean"],
    ):
        """
        See superclass for first few parameters.

        Args:
            pooling_type: Type of pooling to use. One of "min", "max", "sum" and "mean".
        """
        super().__init__(node_dim, out_dim)
        self._pooling_type = pooling_type

        if pooling_type not in ("min", "max", "sum", "mean"):
            raise ValueError(f"Unknown weighting type {self.pooling_type}!")

        self._combination_layer = nn.Linear(self._node_dim, out_dim, bias=False)

    def forward(
        self,
        node_embeddings: torch.Tensor,
        node_to_graph_id: torch.Tensor,
        num_graphs: int,
    ) -> torch.Tensor:
        per_graph_values = scatter(
            src=node_embeddings,
            index=node_to_graph_id,
            dim=0,
            dim_size=num_graphs,
            reduce=self._pooling_type,
        )  # [num_graphs, self.pooling_input_dim]
        return self._combination_layer(per_graph_values)  # [num_graphs, out_dim]
