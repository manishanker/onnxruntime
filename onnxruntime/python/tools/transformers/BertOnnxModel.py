#-------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation.  All rights reserved.
# Licensed under the MIT License.
#--------------------------------------------------------------------------

from logging import getLogger
from onnx import TensorProto, helper
from OnnxModel import OnnxModel
from fusion_reshape import FusionReshape
from fusion_layernorm import FusionLayerNormalization, FusionLayerNormalizationTF
from fusion_skiplayernorm import FusionSkipLayerNormalization, FusionBiasSkipLayerNormalization
from fusion_embedlayer import FusionEmbedLayerNormalization
from fusion_attention import FusionAttention, AttentionMask
from fusion_gelu import FusionGelu
from fusion_fastgelu import FusionFastGelu
from fusion_biasgelu import FusionBiasGelu
from fusion_gelu_approximation import FusionGeluApproximation

logger = getLogger(__name__)


class BertOptimizationOptions:
    def __init__(self, model_type):
        self.enable_gelu = True
        self.enable_layer_norm = True
        self.enable_attention = True
        self.enable_skip_layer_norm = True
        self.enable_embed_layer_norm = True
        self.enable_bias_skip_layer_norm = True
        self.enable_bias_gelu = True
        self.enable_gelu_approximation = False

        if model_type == 'gpt2':
            self.enable_skip_layer_norm = False


class BertOnnxModel(OnnxModel):
    def __init__(self, model, num_heads, hidden_size):
        assert num_heads > 0
        assert hidden_size % num_heads == 0

        super().__init__(model)
        self.num_heads = num_heads
        self.hidden_size = hidden_size
        self.bert_inputs = []

        self.attention_mask = AttentionMask(self)
        self.attention_fusion = FusionAttention(self, self.hidden_size, self.num_heads, self.attention_mask)

    def fuse_attention(self):
        self.attention_fusion.apply()

    def fuse_gelu(self):
        fusion = FusionGelu(self)
        fusion.apply()
        fusion = FusionFastGelu(self)
        fusion.apply()

    def fuse_bias_gelu(self, is_fastgelu):
        fusion = FusionBiasGelu(self, is_fastgelu)
        fusion.apply()

    def gelu_approximation(self):
        fusion = FusionGeluApproximation(self)
        fusion.apply()

    def fuse_add_bias_skip_layer_norm(self):
        fusion = FusionBiasSkipLayerNormalization(self)
        fusion.apply()

    def fuse_reshape(self):
        fusion = FusionReshape(self)
        fusion.apply()

    def fuse_embed_layer(self):
        mask_indice = self.attention_mask.mask_indice if self.attention_mask else {}
        mask_casted = self.attention_mask.mask_casted if self.attention_mask else {}
        fusion = FusionEmbedLayerNormalization(self, mask_indice, mask_casted)
        fusion.apply()
        if fusion.mask_input_name:
            self.bert_inputs.append(fusion.mask_input_name)

    def fuse_layer_norm(self):
        fusion = FusionLayerNormalization(self)
        fusion.apply()

        fusion = FusionLayerNormalizationTF(self)
        fusion.apply()

    def fuse_skip_layer_norm(self):
        fusion = FusionSkipLayerNormalization(self)
        fusion.apply()

    def get_bert_inputs(self, include_mask=True):
        return self.bert_inputs if include_mask else self.bert_inputs[:2]

    def get_bert_input_shape(self):
        graph = self.graph()
        bert_inputs = self.get_bert_inputs()
        for input in graph.input:
            if input.name in bert_inputs:
                tensor_type = input.type.tensor_type
                if (tensor_type.HasField("shape")):
                    batch_size = None
                    d = tensor_type.shape.dim[0]
                    if (d.HasField("dim_value")):
                        batch_size = d.dim_value
                    elif (d.HasField("dim_param")):
                        batch_size = str(d.dim_param)

                    sequence_length = None
                    d = tensor_type.shape.dim[1]
                    if (d.HasField("dim_value")):
                        sequence_length = d.dim_value
                    elif (d.HasField("dim_param")):
                        sequence_length = str(d.dim_param)
                    return batch_size, sequence_length

        return None, None

    def change_input_to_int32(self):
        original_opset_version = self.model.opset_import[0].version
        graph = self.graph()

        batch_size, sequence_length = self.get_bert_input_shape()
        new_graph_inputs = []

        bert_inputs = self.get_bert_inputs()
        for input in graph.input:
            if input.name in bert_inputs:
                self.remove_cast_int32(input.name)
                input_shape = [
                    batch_size if isinstance(batch_size, int) else 1,
                    sequence_length if isinstance(sequence_length, int) else 128
                ]
                int32_input = helper.make_tensor_value_info(input.name, TensorProto.INT32, input_shape)
                new_graph_inputs.append(int32_input)
            else:
                new_graph_inputs.append(input)

        graph_def = helper.make_graph(graph.node,
                                      'int32 inputs',
                                      new_graph_inputs,
                                      graph.output,
                                      initializer=graph.initializer,
                                      value_info=graph.value_info)

        self.model = helper.make_model(graph_def, producer_name='bert model optimizer')

        if isinstance(batch_size, str) or isinstance(sequence_length, str):
            self.use_dynamic_axes(batch_size if isinstance(batch_size, str) else None,
                                  sequence_length if isinstance(sequence_length, str) else None)

        # restore opset version
        self.model.opset_import[0].version = original_opset_version

    def use_dynamic_axes(self, dynamic_batch_dim='batch_size', dynamic_seq_len='max_seq_len'):
        """
        Update input and output shape to use dynamic axes.
        """
        bert_inputs = self.get_bert_inputs()
        dynamic_batch_inputs = {}
        for input in self.model.graph.input:
            for bert_input in bert_inputs:
                if bert_input == input.name:
                    dim_proto = input.type.tensor_type.shape.dim[0]
                    dim_proto.dim_param = dynamic_batch_dim
                    if dynamic_seq_len is not None:
                        dim_proto = input.type.tensor_type.shape.dim[1]
                        dim_proto.dim_param = dynamic_seq_len

        for output in self.model.graph.output:
            dim_proto = output.type.tensor_type.shape.dim[0]
            dim_proto.dim_param = dynamic_batch_dim

    def preprocess(self):
        return

    def clean_graph(self):
        output_name_to_node = self.output_name_to_node()
        nodes_to_add = []
        nodes_to_remove = []
        for node in self.nodes():
            # Before:
            #  input_ids --> Shape --> Gather(indices=0) --> Unsqueeze ------+
            #          |                                                     | 
            #          |                                                     v
            #          +----> Shape --> Gather(indices=1) --> Unsqueeze--->  Concat --> ConstantOfShape -->Cast --> EmbedLayerNormaliation/ReduceSum
            # After:
            #  input_ids --> Shape                                                  --> ConstantOfShape -->Cast --> EmbedLayerNormaliation/ReduceSum
            # TODO: merge ConstantOfShape -->Cast to ConstantOfShape (need update the data type of value)
            if node.op_type == 'EmbedLayerNormalization' or node.op_type == 'ReduceSum':
                i = 1 if node.op_type == 'EmbedLayerNormalization' else 0
                parent_nodes = self.match_parent_path(
                    node, ['Cast', 'ConstantOfShape', 'Concat', 'Unsqueeze', 'Gather', 'Shape'], [i, 0, 0, 0, 0, 0],
                    output_name_to_node)
                if parent_nodes is not None:
                    cast, constantOfShape, concat, unsqueeze, gather, shape = parent_nodes
                    if shape.input[0] == self.graph().input[0].name:
                        constantOfShape.input[0] = shape.output[0]
                        output_name_to_node = self.output_name_to_node()

            if node.op_type == 'Attention':
                # Before:
                #   input_ids --> Shape -->ConstantOfShape -->Cast --> ReduceSum --> Attention
                # After:
                #   remove this path, and remove the optional mask_index input of Attention node.
                parent_nodes = self.match_parent_path(node, ['ReduceSum', 'Cast', 'ConstantOfShape', 'Shape'],
                                                      [3, 0, 0, 0], output_name_to_node)
                if parent_nodes is not None:
                    if parent_nodes[-1].input[0] == self.graph().input[0].name:
                        attention_node = helper.make_node('Attention',
                                                          inputs=node.input[0:len(node.input) - 1],
                                                          outputs=node.output,
                                                          name=node.name + "_remove_mask")
                        attention_node.domain = "com.microsoft"
                        attention_node.attribute.extend([helper.make_attribute("num_heads", self.num_heads)])
                        nodes_to_add.append(attention_node)
                        nodes_to_remove.append(node)
        self.remove_nodes(nodes_to_remove)
        self.add_nodes(nodes_to_add)

    def postprocess(self):
        self.clean_graph()
        self.prune_graph()

    def optimize(self, options: BertOptimizationOptions = None, add_dynamic_axes=False):
        if (options is None) or options.enable_layer_norm:
            self.fuse_layer_norm()

        if (options is None) or options.enable_gelu:
            self.fuse_gelu()

        self.preprocess()

        self.fuse_reshape()

        if (options is None) or options.enable_skip_layer_norm:
            self.fuse_skip_layer_norm()

        if (options is None) or options.enable_attention:
            self.fuse_attention()

        if (options is None) or options.enable_embed_layer_norm:
            self.fuse_embed_layer()

        # Post-processing like removing extra reshape nodes.
        self.postprocess()

        # Bias fusion is done after postprocess to avoid extra Reshape between bias and Gelu/FastGelu/SkipLayerNormalization
        if (options is None) or options.enable_bias_gelu:
            # Fuse Gelu and Add Bias before it.
            self.fuse_bias_gelu(is_fastgelu=True)
            self.fuse_bias_gelu(is_fastgelu=False)

        if (options is None) or options.enable_bias_skip_layer_norm:
            # Fuse SkipLayerNormalization and Add Bias before it.
            self.fuse_add_bias_skip_layer_norm()

        if (options is not None and options.enable_gelu_approximation):
            self.gelu_approximation()

        self.remove_unused_constant()

        # Use symbolic batch dimension in input and output.
        if add_dynamic_axes:
            self.use_dynamic_axes()

        logger.info(f"opset verion: {self.model.opset_import[0].version}")

    def get_fused_operator_statistics(self):
        """
        Returns node count of fused operators.
        """
        op_count = {}
        ops = [
            'EmbedLayerNormalization', 'Attention', 'Gelu', 'FastGelu', 'BiasGelu', 'LayerNormalization',
            'SkipLayerNormalization'
        ]
        for op in ops:
            nodes = self.get_nodes_by_op_type(op)
            op_count[op] = len(nodes)
        logger.info(f"Optimized operators:{op_count}")
        return op_count

    def is_fully_optimized(self):
        """
        Returns True when the model is fully optimized.
        """
        op_count = self.get_fused_operator_statistics()
        embed = op_count['EmbedLayerNormalization']
        attention = op_count['Attention']
        gelu = op_count['Gelu'] + op_count['BiasGelu'] + op_count['FastGelu']
        layer_norm = op_count['LayerNormalization'] + op_count['SkipLayerNormalization']
        is_optimized = (embed > 0) and (attention > 0) and (attention == gelu) and (layer_norm >= 2 * attention)
        logger.info(
            f"EmbedLayer={embed}, Attention={attention}, Gelu={gelu}, LayerNormalization={layer_norm}, Successful={is_optimized}"
        )
        return is_optimized
