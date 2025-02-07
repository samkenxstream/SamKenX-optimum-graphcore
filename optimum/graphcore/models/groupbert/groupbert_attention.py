# Copyright (c) 2022 Graphcore Ltd. All rights reserved.
# Copyright 2018 The Google AI Language Team Authors and The HuggingFace Inc. team.
# Copyright (c) 2018, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# RANDOM CHANGE

import math
from typing import List, Optional, Tuple

import torch
import torch.nn as nn

from optimum.utils import logging
from transformers.modeling_utils import find_pruneable_heads_and_indices, prune_linear_layer
from transformers.models.bert.modeling_bert import BertSelfAttention


logger = logging.get_logger(__name__)


class GroupBertFusedSelfAttention(BertSelfAttention):
    def fused_qkv(self, hidden_state: torch.Tensor) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        weights = (self.query.weight, self.key.weight, self.value.weight)
        combined_weight = torch.cat(weights, dim=0)
        combined_result = hidden_state @ torch.transpose(combined_weight, -2, -1)
        biases = (self.query.bias, self.key.bias, self.value.bias)
        if all(map(lambda b: b is not None, biases)):
            combined_bias = torch.cat(biases, dim=0)
            combined_result += combined_bias
        elif any(map(lambda b: b is not None, biases)):
            raise RuntimeError(
                "Some attention layers had biases but not all. This is not supported. "
                "Please enable biases on all Query, Key and Value or none. "
                f"query.bias = {biases[0] is not None}, "
                f"key.bias = {biases[1] is not None}, "
                f"value.bias = {biases[2] is not None}"
            )
        hidden_size = hidden_state.shape[-1]
        return torch.split(combined_result, hidden_size, dim=-1)

    def forward(
        self,
        hidden_states,
        attention_mask=None,
        head_mask=None,
        encoder_hidden_states=None,
        encoder_attention_mask=None,
        past_key_value=None,
        output_attentions=False,
    ):
        if encoder_hidden_states is not None:
            raise RuntimeError("encoder_hidden_states is not supported")
        elif encoder_attention_mask is not None:
            raise RuntimeError("encoder_attention_mask is not supported")
        elif past_key_value is not None:
            raise RuntimeError("past_key_value not supported")

        # --- Change: Use fused matmul implementation ---

        mixed_query_layer, mixed_key_layer, mixed_value_layer = self.fused_qkv(hidden_states)

        query_layer = self.transpose_for_scores(mixed_query_layer)
        key_layer = self.transpose_for_scores(mixed_key_layer)
        value_layer = self.transpose_for_scores(mixed_value_layer)

        # ------------------------------------------------

        if self.is_decoder:
            # if cross_attention save Tuple(torch.Tensor, torch.Tensor) of all cross attention key/value_states.
            # Further calls to cross_attention layer can then reuse all cross-attention
            # key/value_states (first "if" case)
            # if uni-directional self-attention (decoder) save Tuple(torch.Tensor, torch.Tensor) of
            # all previous decoder key/value_states. Further calls to uni-directional self-attention
            # can concat previous decoder key/value_states to current projected key/value_states (third "elif" case)
            # if encoder bi-directional self-attention `past_key_value` is always `None`
            past_key_value = (key_layer, value_layer)

        # Take the dot product between "query" and "key" to get the raw attention scores.
        attention_scores = torch.matmul(query_layer, key_layer.transpose(-1, -2))

        if self.position_embedding_type == "relative_key" or self.position_embedding_type == "relative_key_query":
            seq_length = hidden_states.size()[1]
            position_ids_l = torch.arange(seq_length, dtype=torch.long, device=hidden_states.device).view(-1, 1)
            position_ids_r = torch.arange(seq_length, dtype=torch.long, device=hidden_states.device).view(1, -1)
            distance = position_ids_l - position_ids_r
            positional_embedding = self.distance_embedding(distance + self.max_position_embeddings - 1)
            positional_embedding = positional_embedding.to(dtype=query_layer.dtype)  # fp16 compatibility

            if self.position_embedding_type == "relative_key":
                relative_position_scores = torch.einsum("bhld,lrd->bhlr", query_layer, positional_embedding)
                attention_scores = attention_scores + relative_position_scores
            elif self.position_embedding_type == "relative_key_query":
                relative_position_scores_query = torch.einsum("bhld,lrd->bhlr", query_layer, positional_embedding)
                relative_position_scores_key = torch.einsum("bhrd,lrd->bhlr", key_layer, positional_embedding)
                attention_scores = attention_scores + relative_position_scores_query + relative_position_scores_key

        # --- Change: Use reciprocal multiply for speed ---

        attention_scores = attention_scores * (1.0 / math.sqrt(self.attention_head_size))

        # ------------------------------------------------
        # Implementation below here is from
        # https://github.com/huggingface/transformers/blob/master/src/transformers/models/bert/modeling_bert.py
        # ::BertSelfAttention

        if attention_mask is not None:
            # Apply the attention mask is (precomputed for all layers in BertModel forward() function)
            attention_scores = attention_scores + attention_mask

        # Normalize the attention scores to probabilities.
        attention_probs = nn.Softmax(dim=-1)(attention_scores)

        # This is actually dropping out entire tokens to attend to, which might
        # seem a bit unusual, but is taken from the original Transformer paper.
        attention_probs = self.dropout(attention_probs)

        # Mask heads if we want to
        if head_mask is not None:
            attention_probs = attention_probs * head_mask

        context_layer = torch.matmul(attention_probs, value_layer)

        context_layer = context_layer.permute(0, 2, 1, 3).contiguous()
        new_context_layer_shape = context_layer.size()[:-2] + (self.all_head_size,)
        context_layer = context_layer.view(*new_context_layer_shape)

        outputs = (context_layer, attention_probs) if output_attentions else (context_layer,)

        if self.is_decoder:
            outputs = outputs + (past_key_value,)
        return outputs


class GroupBertSelfOutput(nn.Module):
    """
    GroupBERT self-attention output layer. Similar to BERT, but doesn't have layer norm
    since its moved to the begining of the module.
    """

    def __init__(self, config):
        super().__init__()
        self.dense = nn.Linear(config.hidden_size, config.hidden_size)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)

    def forward(self, hidden_states: torch.Tensor, input_tensor: torch.Tensor) -> torch.Tensor:
        hidden_states = self.dense(hidden_states)
        hidden_states = self.dropout(hidden_states)
        return hidden_states + input_tensor


class GroupBertAttention(nn.Module):
    """
    GroupBERT attention module. It is similar in construction to the originalTransformer encoder layer
    used in BERT, with the only difference being the pre-norm LayerNorm configuration.
    """

    def __init__(self, config, position_embedding_type=None):
        super().__init__()
        self.LayerNorm = nn.LayerNorm(config.hidden_size, eps=config.layer_norm_eps)
        self.self = GroupBertFusedSelfAttention(config, position_embedding_type=position_embedding_type)
        self.output = GroupBertSelfOutput(config)
        self.pruned_heads = set()

    def prune_heads(self, heads):
        if len(heads) == 0:
            return
        heads, index = find_pruneable_heads_and_indices(
            heads, self.self.num_attention_heads, self.self.attention_head_size, self.pruned_heads
        )

        # Prune linear layers
        self.self.query = prune_linear_layer(self.self.query, index)
        self.self.key = prune_linear_layer(self.self.key, index)
        self.self.value = prune_linear_layer(self.self.value, index)
        self.output.dense = prune_linear_layer(self.output.dense, index, dim=1)

        # Update hyper params and store pruned heads
        self.self.num_attention_heads = self.self.num_attention_heads - len(heads)
        self.self.all_head_size = self.self.attention_head_size * self.self.num_attention_heads
        self.pruned_heads = self.pruned_heads.union(heads)

    def forward(
        self,
        hidden_states: torch.Tensor,
        attention_mask: Optional[torch.FloatTensor] = None,
        head_mask: Optional[torch.FloatTensor] = None,
        encoder_hidden_states: Optional[torch.FloatTensor] = None,
        encoder_attention_mask: Optional[torch.FloatTensor] = None,
        past_key_value: Optional[Tuple[Tuple[torch.FloatTensor]]] = None,
        output_attentions: Optional[bool] = False,
    ) -> Tuple[torch.Tensor]:
        # Prenorm
        normalised_hidden_states = self.LayerNorm(hidden_states)

        # Self-attention
        self_outputs = self.self(
            normalised_hidden_states,
            attention_mask,
            head_mask,
            encoder_hidden_states,
            encoder_attention_mask,
            past_key_value,
            output_attentions,
        )

        # Output projection and residual
        attention_output = self.output(self_outputs[0], hidden_states)

        outputs = (attention_output,) + self_outputs[1:]  # add attentions if we output them
        return outputs
