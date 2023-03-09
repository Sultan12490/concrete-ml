"""QuantizedModule API."""
import copy
import re
from typing import Any, Dict, Generator, Iterable, List, Optional, Tuple, Union

import numpy
from concrete.numpy.compilation.artifacts import DebugArtifacts
from concrete.numpy.compilation.circuit import Circuit
from concrete.numpy.compilation.compiler import Compiler
from concrete.numpy.compilation.configuration import Configuration

from ..common.debugging import assert_true
from ..common.utils import (
    check_there_is_no_p_error_options_in_configuration,
    generate_proxy_function,
    manage_parameters_for_pbs_errors,
    to_tuple,
)
from .base_quantized_op import ONNXOpInputOutputType, QuantizedOp
from .quantizers import QuantizedArray, UniformQuantizer


def _raise_qat_import_error(bad_qat_ops: List[Tuple[str, str]]):
    """Raise a descriptive error if any invalid ops are present in the ONNX graph.

    Args:
        bad_qat_ops (List[Tuple[str, str]]): list of tensor names and operation types

    Raises:
        ValueError: if there were any invalid, non-quantized, tensors as inputs to non-fusable ops
    """

    raise ValueError(
        "Error occurred during quantization aware training (QAT) import: "
        "The following tensors were expected to be quantized, but the values "
        "found during calibration do not appear to be quantized. \n\n"
        + "\n".join(
            map(
                lambda info: f"* Tensor {info[0]}, input of an {info[1]} operation",
                bad_qat_ops,
            )
        )
        + "\n\nCould not determine a unique scale for the quantization! "
        "Please check the ONNX graph of this model."
    )


def _get_inputset_generator(q_inputs: Union[numpy.ndarray, Tuple[numpy.ndarray, ...]]) -> Generator:
    """Create an input set generator with proper dimensions.

    Args:
        q_inputs (Union[numpy.ndarray, Tuple[numpy.ndarray, ...]]): The quantized inputs.

    Returns:
        Generator: The input set generator with proper dimensions.
    """
    q_inputs = to_tuple(q_inputs)

    assert len(q_inputs) > 0, "Inputset cannot be empty"

    if len(q_inputs) > 1:
        return (
            tuple(numpy.expand_dims(q_input[idx], 0) for q_input in q_inputs)
            for idx in range(q_inputs[0].shape[0])
        )

    # Else, there's only a single input (q_inputs, )
    return (numpy.expand_dims(q_input, 0) for q_input in q_inputs[0])


class QuantizedModule:
    """Inference for a quantized model."""

    ordered_module_input_names: Tuple[str, ...]
    ordered_module_output_names: Tuple[str, ...]
    quant_layers_dict: Dict[str, Tuple[Tuple[str, ...], QuantizedOp]]
    input_quantizers: List[UniformQuantizer]
    output_quantizers: List[UniformQuantizer]
    fhe_circuit: Union[None, Circuit]

    def __init__(
        self,
        ordered_module_input_names: Iterable[str] = None,
        ordered_module_output_names: Iterable[str] = None,
        quant_layers_dict: Dict[str, Tuple[Tuple[str, ...], QuantizedOp]] = None,
    ):
        # Set base attributes for API consistency. This could be avoided if an abstract base class
        # is created for both Concrete-ML models and QuantizedModule
        # FIXME: https://github.com/zama-ai/concrete-ml-internal/issues/2899
        self.fhe_circuit = None
        self.input_quantizers = []
        self.output_quantizers = []
        self._onnx_model = None
        self._post_processing_params: Dict[str, Any] = {}

        # If any of the arguments are not provided, skip the init
        if not all([ordered_module_input_names, ordered_module_output_names, quant_layers_dict]):
            return

        # for mypy
        assert isinstance(ordered_module_input_names, Iterable)
        assert isinstance(ordered_module_output_names, Iterable)
        assert all([ordered_module_input_names, ordered_module_output_names, quant_layers_dict])
        self.ordered_module_input_names = tuple(ordered_module_input_names)
        self.ordered_module_output_names = tuple(ordered_module_output_names)

        num_outputs = len(self.ordered_module_output_names)
        assert_true(
            (num_outputs) == 1,
            f"{QuantizedModule.__class__.__name__} only supports a single output for now, "
            f"got {num_outputs}",
        )

        assert quant_layers_dict is not None
        self.quant_layers_dict = copy.deepcopy(quant_layers_dict)
        self.output_quantizers = self._set_output_quantizers()

    def check_model_is_compiled(self):
        """Check if the quantized module is compiled.

        Raises:
            AttributeError: If the quantized module is not compiled.
        """
        if self.fhe_circuit is None:
            raise AttributeError(
                "The quantized module is not compiled. Please run compile(...) first before "
                "executing it in FHE."
            )

    @property
    def post_processing_params(self) -> Dict[str, Any]:
        """Get the post-processing parameters.

        Returns:
            Dict[str, Any]: the post-processing parameters
        """
        return self._post_processing_params

    @post_processing_params.setter
    def post_processing_params(self, post_processing_params: Dict[str, Any]):
        """Set the post-processing parameters.

        Args:
            post_processing_params (dict): the post-processing parameters
        """
        self._post_processing_params = post_processing_params

    # pylint: disable-next=no-self-use
    def post_processing(self, values: numpy.ndarray) -> numpy.ndarray:
        """Apply post-processing to the dequantized values.

        For quantized modules, there is no post-processing step but the method is kept to make the
        API consistent for the client-server API.

        Args:
            values (numpy.ndarray): The dequantized values to post-process.

        Returns:
            numpy.ndarray: The post-processed values.
        """
        return values

    def _set_output_quantizers(self) -> List[UniformQuantizer]:
        """Get the output quantizers.

        Returns:
            List[UniformQuantizer]: List of output quantizers.
        """
        output_layers = (
            self.quant_layers_dict[output_name][1]
            for output_name in self.ordered_module_output_names
        )
        output_quantizers = list(
            QuantizedArray(
                output_layer.n_bits,
                values=None,
                value_is_float=False,
                stats=output_layer.output_quant_stats,
                params=output_layer.output_quant_params,
            ).quantizer
            for output_layer in output_layers
        )
        return output_quantizers

    @property
    def onnx_model(self):
        """Get the ONNX model.

        .. # noqa: DAR201

        Returns:
           _onnx_model (onnx.ModelProto): the ONNX model
        """
        return self._onnx_model

    @onnx_model.setter
    def onnx_model(self, value):
        self._onnx_model = value

    def __call__(self, *x: numpy.ndarray):
        return self.forward(*x)

    def forward(
        self, *qvalues: numpy.ndarray, debug: bool = False
    ) -> Union[numpy.ndarray, Tuple[numpy.ndarray, Optional[Dict[Any, Any]]]]:
        """Forward pass with numpy function only.

        Args:
            *qvalues (numpy.ndarray): numpy.array containing the quantized values.
            debug (bool): In debug mode, returns quantized intermediary values of the computation.
                          This is useful when a model's intermediary values in Concrete-ML need
                          to be compared with the intermediary values obtained in pytorch/onnx.
                          When set, the second return value is a dictionary containing ONNX
                          operation names as keys and, as values, their input QuantizedArray or
                          ndarray. The use can thus extract the quantized or float values of
                          quantized inputs.

        Returns:
            (numpy.ndarray): Predictions of the quantized model
        """
        # Make sure that the input is quantized
        invalid_inputs = tuple(
            (idx, qvalue)
            for idx, qvalue in enumerate(qvalues)
            if not issubclass(qvalue.dtype.type, numpy.integer)
        )
        assert_true(
            len(invalid_inputs) == 0,
            f"Inputs: {', '.join(f'#{val[0]} ({val[1].dtype})' for val in invalid_inputs)} are not "
            "integer types. Make sure you quantize your input before calling forward.",
            ValueError,
        )

        if debug:
            debug_value_tracker: Optional[
                Dict[str, Dict[Union[int, str], Optional[ONNXOpInputOutputType]]]
            ] = {}
            for (_, layer) in self.quant_layers_dict.values():
                layer.debug_value_tracker = debug_value_tracker
            result = self._forward(*qvalues)
            for (_, layer) in self.quant_layers_dict.values():
                layer.debug_value_tracker = None
            return result, debug_value_tracker

        return self._forward(*qvalues)

    def _forward(self, *qvalues: numpy.ndarray) -> numpy.ndarray:
        """Forward function for the FHE circuit.

        Args:
            *qvalues (numpy.ndarray): numpy.array containing the quantized values.

        Returns:
            (numpy.ndarray): Predictions of the quantized model

        """

        n_qinputs = len(self.input_quantizers)
        n_qvalues = len(qvalues)
        assert_true(
            n_qvalues == n_qinputs,
            f"Got {n_qvalues} inputs, expected {n_qinputs}",
            TypeError,
        )

        q_inputs = [
            QuantizedArray(
                self.input_quantizers[idx].n_bits,
                qvalues[idx],
                value_is_float=False,
                options=self.input_quantizers[idx].quant_options,
                stats=self.input_quantizers[idx].quant_stats,
                params=self.input_quantizers[idx].quant_params,
            )
            for idx in range(len(self.input_quantizers))
        ]

        # Init layer_results with the inputs
        layer_results: Dict[str, ONNXOpInputOutputType] = dict(
            zip(self.ordered_module_input_names, q_inputs)
        )

        bad_qat_ops: List[Tuple[str, str]] = []
        for output_name, (input_names, layer) in self.quant_layers_dict.items():
            inputs = (layer_results.get(input_name, None) for input_name in input_names)

            error_tracker: List[int] = []
            layer.error_tracker = error_tracker
            output = layer(*inputs)
            layer.error_tracker = None

            if len(error_tracker) > 0:
                # The error message contains the ONNX tensor name that
                # triggered this error
                for input_idx in error_tracker:
                    bad_qat_ops.append((input_names[input_idx], layer.__class__.op_type()))

            layer_results[output_name] = output

        if len(bad_qat_ops) > 0:
            _raise_qat_import_error(bad_qat_ops)

        outputs = tuple(
            layer_results[output_name] for output_name in self.ordered_module_output_names
        )

        assert_true(len(outputs) == 1)

        # The output of a graph must be a QuantizedArray
        assert isinstance(outputs[0], QuantizedArray)

        return outputs[0].qvalues

    def forward_in_fhe(self, *qvalues: numpy.ndarray, simulate=True) -> numpy.ndarray:
        """Forward function running in FHE or simulated mode.

        Args:
            *qvalues (numpy.ndarray): numpy.array containing the quantized values.
            simulate (bool): whether the function should be run in FHE or in simulation mode.

        Returns:
            (numpy.ndarray): Predictions of the quantized model

        """

        assert_true(
            self.fhe_circuit is not None,
            "The quantized module is not compiled. Please run compile(...) first before "
            "executing it in FHE.",
        )

        results_cnp_circuit_list = []
        for i in range(qvalues[0].shape[0]):

            # Extract the i th example from every element in the tuple qvalues
            q_value = tuple(qvalues[input][[i]] for input in range(len(qvalues)))

            # For mypy
            assert self.fhe_circuit is not None

            # Run FHE or simulation based on the simulate argument
            q_result = (
                self.fhe_circuit.simulate(*q_value)
                if simulate
                else self.fhe_circuit.encrypt_run_decrypt(*q_value)
            )
            results_cnp_circuit_list.append(q_result)
        results_cnp_circuit = numpy.concatenate(results_cnp_circuit_list, axis=0)
        return results_cnp_circuit

    def forward_and_dequant(self, *q_x: numpy.ndarray) -> numpy.ndarray:
        """Forward pass with numpy function only plus dequantization.

        Args:
            *q_x (numpy.ndarray): numpy.ndarray containing the quantized input values. Requires the
                input dtype to be int64.

        Returns:
            (numpy.ndarray): Predictions of the quantized model
        """
        q_out = self.forward(*q_x)
        return self.dequantize_output(q_out)  # type: ignore

    def quantize_input(
        self, *values: numpy.ndarray
    ) -> Union[numpy.ndarray, Tuple[numpy.ndarray, ...]]:
        """Take the inputs in fp32 and quantize it using the learned quantization parameters.

        Args:
            values (numpy.ndarray): Floating point values.

        Returns:
            Union[numpy.ndarray, Tuple[numpy.ndarray, ...]]: Quantized (numpy.int64) values.
        """
        n_q_inputs = len(self.input_quantizers)
        n_values = len(values)
        assert_true(
            n_values == n_q_inputs,
            f"Got {n_values} inputs, expected {n_q_inputs}",
            TypeError,
        )

        q_values = tuple(
            self.input_quantizers[idx].quant(values[idx]) for idx in range(len(values))
        )

        assert (
            numpy.array(q_values).dtype == numpy.int64
        ), "Inputs were not quantized to int64 values"
        return q_values[0] if len(q_values) == 1 else q_values

    def dequantize_output(self, q_values: numpy.ndarray) -> numpy.ndarray:
        """Take the last layer q_out and use its dequant function.

        Args:
            q_values (numpy.ndarray): Quantized values of the last layer.

        Returns:
            numpy.ndarray: Dequantized values of the last layer.
        """
        real_values = tuple(
            output_quantizer.dequant(q_values) for output_quantizer in self.output_quantizers
        )

        assert_true(len(real_values) == 1)

        return real_values[0]

    def set_inputs_quantization_parameters(self, *input_q_params: UniformQuantizer):
        """Set the quantization parameters for the module's inputs.

        Args:
            *input_q_params (UniformQuantizer): The quantizer(s) for the module.
        """
        n_inputs = len(self.ordered_module_input_names)
        n_values = len(input_q_params)
        assert_true(
            n_values == n_inputs,
            f"Got {n_values} inputs, expected {n_inputs}",
            TypeError,
        )

        self.input_quantizers.clear()
        self.input_quantizers.extend(copy.deepcopy(q_params) for q_params in input_q_params)

    def compile(
        self,
        inputs: Union[Tuple[numpy.ndarray, ...], numpy.ndarray],
        configuration: Optional[Configuration] = None,
        artifacts: Optional[DebugArtifacts] = None,
        show_mlir: bool = False,
        use_virtual_lib: bool = False,
        p_error: Optional[float] = None,
        global_p_error: Optional[float] = None,
        verbose: bool = False,
    ) -> Circuit:
        """Compile the module's forward function.

        Args:
            inputs (numpy.ndarray): A representative set of input values used for building
                cryptographic parameters.
            configuration (Optional[Configuration]): Options to use for compilation. Default
                to None.
            artifacts (Optional[DebugArtifacts]): Artifacts information about the
                compilation process to store for debugging.
            show_mlir (bool): Indicate if the MLIR graph should be printed during compilation.
            use_virtual_lib (bool): Indicate if the module should be compiled using the Virtual
                Library in order to simulate FHE computations. This currently requires to set
                `enable_unsafe_features` to True in the configuration. Default to False
            p_error (Optional[float]): Probability of error of a single PBS. A p_error value cannot
                be given if a global_p_error value is already set. Default to None, which sets this
                error to a default value.
            global_p_error (Optional[float]): Probability of error of the full circuit. A
                global_p_error value cannot be given if a p_error value is already set. This feature
                is not supported during Virtual Library simulation, meaning the probability is
                currently set to 0 if use_virtual_lib is True. Default to None, which sets this
                error to a default value.
            verbose (bool): Indicate if compilation information should be printed
                during compilation. Default to False.

        Returns:
            Circuit: The compiled Circuit.
        """
        inputs = to_tuple(inputs)

        ref_len = inputs[0].shape[0]
        assert_true(
            all(input.shape[0] == ref_len for input in inputs),
            "Mismatched dataset lengths",
        )

        assert not numpy.any([numpy.issubdtype(input.dtype, numpy.integer) for input in inputs]), (
            "Inputs used for compiling a QuantizedModule should only be floating points and not"
            "already-quantized values."
        )

        # concrete-numpy does not support variable *args-style functions, so compile a proxy
        # function dynamically with a suitable number of arguments
        forward_proxy, orig_args_to_proxy_func_args = generate_proxy_function(
            self._forward, self.ordered_module_input_names
        )

        compiler = Compiler(
            forward_proxy,
            {arg_name: "encrypted" for arg_name in orig_args_to_proxy_func_args.values()},
        )

        # Quantize the inputs
        q_inputs = self.quantize_input(*inputs)

        # Generate the inputset with proper dimensions
        inputset = _get_inputset_generator(q_inputs)

        # Don't let the user shoot in her foot, by having p_error or global_p_error set in both
        # configuration and in direct arguments
        check_there_is_no_p_error_options_in_configuration(configuration)

        # Find the right way to set parameters for compiler, depending on the way we want to default
        p_error, global_p_error = manage_parameters_for_pbs_errors(p_error, global_p_error)

        self.fhe_circuit = compiler.compile(
            inputset,
            configuration=configuration,
            artifacts=artifacts,
            show_mlir=show_mlir,
            virtual=use_virtual_lib,
            p_error=p_error,
            global_p_error=global_p_error,
            verbose=verbose,
        )

        return self.fhe_circuit

    def bitwidth_and_range_report(
        self,
    ) -> Optional[Dict[str, Dict[str, Union[Tuple[int, ...], int]]]]:
        """Report the ranges and bitwidths for layers that mix encrypted integer values.

        Returns:
            op_names_to_report (Dict): a dictionary with operation names as keys. For each
                operation, (e.g. conv/gemm/add/avgpool ops), a range and a bitwidth are returned.
                The range contains the min/max values encountered when computing the operation and
                the bitwidth gives the number of bits needed to represent this range.
        """

        if self.fhe_circuit is None:
            return None

        op_names_to_report: Dict[str, Dict[str, Union[Tuple[int, ...], int]]] = {}
        for (_, op_inst) in self.quant_layers_dict.values():
            # Get the value range of this tag and all its subtags
            # The potential tags for this op start with the op instance name
            # and are, sometimes, followed by a subtag starting with a period:
            # ex: abc, abc.cde, abc.cde.fgh
            # so first craft a regex to match all such tags.
            pattern = re.compile(re.escape(op_inst.op_instance_name) + "(\\..*)?")
            value_range = self.fhe_circuit.graph.integer_range(pattern)
            bitwidth = self.fhe_circuit.graph.maximum_integer_bit_width(pattern)

            # Only store the range and bit-width if there are valid ones,
            # as some ops (fusable ones) do not have tags
            if value_range is not None and bitwidth >= 0:
                op_names_to_report[op_inst.op_instance_name] = {
                    "range": value_range,
                    "bitwidth": bitwidth,
                }

        return op_names_to_report
