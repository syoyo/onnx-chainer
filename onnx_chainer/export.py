from __future__ import print_function

import heapq
import os

import chainer
from chainer import function_node
from chainer import variable
import numpy

try:
    from onnx import checker
    from onnx import helper
    from onnx.mapping import TENSOR_TYPE_TO_NP_TYPE
    from onnx import numpy_helper

    _available = True

    _dtype = {v: k for k, v in TENSOR_TYPE_TO_NP_TYPE.items()}

    _layers = {
        'LinearFunction': 'Gemm',
        'Reshape': 'Reshape',
        'Convolution2DFunction': 'Conv',
        'AveragePooling2D': 'AveragePool',
        'MaxPooling2D': 'MaxPool',
        'BatchNormalization': 'BatchNormalization',
        'ReLU': 'Relu',
        'Softmax': 'Softmax',
        'Add': 'Add',
        'Sub': 'Sub',
        'Mul': 'Mul',
        'Neg': 'Neg',
        'Absolute': 'Abs',
        'Div': 'Div',
    }

except (ImportError, TypeError) as e:
    print(e)
    _available = False


def _check_available():
    if not _available:
        raise ImportError(
            'ONNX is not installed on your environment. Exporting your model '
            'in ONNX format needs the onnx package.\n\n'
            '  $ pip install onnx==0.2.1\n')


def convert_parameter(parameter, param_names):
    if isinstance(parameter, chainer.Parameter):
        array = parameter.array
    elif isinstance(parameter, chainer.Variable):
        array = parameter.array
    elif isinstance(parameter, numpy.ndarray):
        array = parameter
    return numpy_helper.from_array(array, param_names[id(parameter)])


def convert_convolution_2d_function(link, input_names, param_names):
    input_names[input_names.index(id(link.W))] = param_names[id(link.W)]
    if hasattr(link, 'b'):
        input_names[input_names.index(id(link.b))] = param_names[id(link.b)]
    for i, input_name in enumerate(input_names):
        if type(input_name) is not str:
            input_names[i] = str(input_name)

    layer_name = _layers[link.__class__.__name__]
    out_names = [str(id(out())) for out in link.outputs]

    return helper.make_node(
        layer_name, input_names, out_names,
        kernel_shape=link.W.shape[2:],
        strides=(link.sy, link.sx),
        pads=(link.ph, link.pw)
    ),


def convert_linear_function(link, input_names, param_names):
    W = convert_parameter(link.W, param_names)
    input_names[input_names.index(id(link.W))] = W.name
    if hasattr(link, 'b'):
        b = convert_parameter(link.b, param_names)
        input_names[input_names.index(id(link.b))] = b.name
    for i, input_name in enumerate(input_names):
        if type(input_name) is not str:
            input_names[i] = str(input_name)

    layer_name = _layers[link.__class__.__name__]
    out_names = [str(id(out())) for out in link.outputs]

    return helper.make_node(
        layer_name, input_names, out_names,
        alpha=1.,
        beta=1.,
        broadcast=True,
        transA=False,
        transB=False,
    ),


def convert_reshape(func, input_names, param_names):
    for i, input_name in enumerate(input_names):
        if type(input_name) is not str:
            input_names[i] = str(input_name)

    layer_name = _layers[func.__class__.__name__]
    out_names = [str(id(out())) for out in func.outputs]

    return helper.make_node(
        layer_name, input_names, out_names,
        shape=func.shape
    ),


def convert_average_pooling_2d(func, input_names, param_names):
    for i, input_name in enumerate(input_names):
        if type(input_name) is not str:
            input_names[i] = str(input_name)

    layer_name = _layers[func.__class__.__name__]
    gpool = func.inputs[0].shape[2:] == (func.kh, func.kw)
    out_names = [str(id(out())) for out in func.outputs]

    if not gpool:
        return helper.make_node(
            layer_name, input_names, out_names,
            kernel_shape=(func.kh, func.kw),
            pads=(func.ph, func.pw),
            strides=(func.sy, func.sx)
        ),
    else:
        return helper.make_node(
            'Global' + layer_name, input_names, out_names),


def convert_max_pooling_2d(func, input_names, param_names):
    for i, input_name in enumerate(input_names):
        if type(input_name) is not str:
            input_names[i] = str(input_name)

    layer_name = _layers[func.__class__.__name__]
    gpool = func.inputs[0].shape[2:] == (func.kh, func.kw)
    out_names = [str(id(out())) for out in func.outputs]

    if not gpool:
        return helper.make_node(
            layer_name, input_names, out_names,
            kernel_shape=(func.kh, func.kw),
            pads=(func.ph, func.pw),
            strides=(func.sy, func.sx)
        ),
    else:
        return helper.make_node(
            'Global' + layer_name, input_names, out_names),


def convert_batch_normalization(link, input_names, param_names):
    gamma_idx = input_names.index(id(link.gamma))
    input_names[gamma_idx] = param_names[id(link.gamma)]
    beta_idx = input_names.index(id(link.beta))
    input_names[beta_idx] = param_names[id(link.beta)]
    for i, input_name in enumerate(input_names):
        if type(input_name) is not str:
            input_names[i] = str(input_name)
    input_names.append(param_names[id(link.running_mean)])
    input_names.append(param_names[id(link.running_var)])

    layer_name = _layers[link.__class__.__name__]
    unique_layer_name = os.path.dirname(input_names[1])
    out_names = [str(id(out())) for out in link.outputs]
    if chainer.config.train:
        out_names += [
            os.path.join(unique_layer_name, 'mean'),
            os.path.join(unique_layer_name, 'var'),
            os.path.join(unique_layer_name, 'saved_mean'),
            os.path.join(unique_layer_name, 'saved_var')
        ]

    return helper.make_node(
        layer_name, input_names, out_names,
        epsilon=link.eps,
        is_test=not chainer.config.train,
        momentum=link.decay,
        spatial=True,
        consumed_inputs=[False, False, False, True, True],
    ),


def convert_relu(func, input_names, param_names):
    for i, input_name in enumerate(input_names):
        if type(input_name) is not str:
            input_names[i] = str(input_name)

    layer_name = _layers[func.__class__.__name__]
    out_names = [str(id(out())) for out in func.outputs]
    return helper.make_node(layer_name, input_names, out_names),


def convert_softmax(func, input_names, param_names):
    for i, input_name in enumerate(input_names):
        if type(input_name) is not str:
            input_names[i] = str(input_name)

    layer_name = _layers[func.__class__.__name__]
    out_names = [str(id(out())) for out in func.outputs]

    return helper.make_node(
        layer_name, input_names, out_names,
        axis=func.axis
    ),


def convert_nonparametric_function(func, input_names, param_names):
    for i, input_name in enumerate(input_names):
        if type(input_name) is not str:
            input_names[i] = str(input_name)

    layer_name = _layers[func.__class__.__name__]
    out_names = [str(id(out())) for out in func.outputs]

    return helper.make_node(layer_name, input_names, out_names),


def create_node(func_name, cand, input_names, param_names, parameters,
                input_tensors):
    if func_name == 'Convolution2DFunction':
        nodes = convert_convolution_2d_function(cand, input_names, param_names)
    elif func_name == 'LinearFunction':
        nodes = convert_linear_function(cand, input_names, param_names)
    elif func_name == 'Reshape':
        nodes = convert_reshape(cand, input_names, param_names)
    elif func_name == 'AveragePooling2D':
        nodes = convert_average_pooling_2d(cand, input_names, param_names)
    elif func_name == 'MaxPooling2D':
        nodes = convert_max_pooling_2d(cand, input_names, param_names)
    elif func_name == 'BatchNormalization':
        layer_name = os.path.dirname(param_names[id(cand.gamma)])

        # Add running_mean and running_var to graph
        param_names[id(cand.running_mean)] = os.path.join(
            layer_name, 'running_mean')
        parameters.append(
            numpy_helper.from_array(
                cand.running_mean,
                param_names[id(cand.running_mean)]))
        input_tensors.append(
            helper.make_tensor_value_info(
                param_names[id(cand.running_mean)],
                _dtype[cand.running_mean.dtype],
                cand.running_mean.shape)
        )

        param_names[id(cand.running_var)] = os.path.join(
            layer_name, 'running_var')
        parameters.append(
            numpy_helper.from_array(
                cand.running_var,
                param_names[id(cand.running_var)]))
        input_tensors.append(
            helper.make_tensor_value_info(
                param_names[id(cand.running_var)],
                _dtype[cand.running_var.dtype],
                cand.running_var.shape)
        )

        nodes = convert_batch_normalization(cand, input_names, param_names)
    elif func_name == 'ReLU':
        nodes = convert_relu(cand, input_names, param_names)
    elif func_name == 'Softmax':
        nodes = convert_softmax(cand, input_names, param_names)
    elif func_name == 'Add':
        nodes = convert_nonparametric_function(cand, input_names, param_names)
    elif func_name == 'Sub':
        nodes = convert_nonparametric_function(cand, input_names, param_names)
    elif func_name == 'Mul':
        nodes = convert_nonparametric_function(cand, input_names, param_names)
    elif func_name == 'Neg':
        nodes = convert_nonparametric_function(cand, input_names, param_names)
    elif func_name == 'Div':
        nodes = convert_nonparametric_function(cand, input_names, param_names)
    elif func_name == 'Absolute':
        nodes = convert_nonparametric_function(cand, input_names, param_names)
    else:
        raise ValueError('{} is not supported.'.format(func_name))

    # A single Chainer layer could be multiple onnx layers
    # e.g., Convolution2D -> Conv + Add (for bias)
    for node in nodes:
        checker.check_node(node)
    return nodes


def export(model, args, filename=None, export_params=True,
           graph_name='Graph', save_text=False):
    """Export function for chainer.Chain in ONNX format.

    This function performs a forward computation of the given
    :class:`~chainer.Chain`, ``model``, by passing the given argments ``args``
    directly. It means, the output :class:`~chainer.Variable` object ``y`` to
    make the computational graph will be created by:

    y = model(*args)

    Args:
        model (~chainer.Chain): The model object you want to export in ONNX
            format. It should have :meth:`__call__` method because the second
            argment ``args`` is directly given to the model by the ``[]``
            accessor.
        args (list or dict): The argments which are given to the model
            directly.
        filename (str): The filename used for saving the resulting ONNX model.
            If None, nothing is saved to the disk.
        export_params (bool): If True, this function exports all the parameters
            included in the given model at the same time. If False, the
            exported ONNX model doesn't include any parameter values.
        graph_name (str): A string to be used for the ``name`` field of the
            graph in the exported ONNX model.
        save_text (bool): If True, the text format of the output ONNX model is
            also saved with ``.txt`` extention.

    Returns:
        A ONNX model object.

    """

    _check_available()

    model.to_cpu()
    args = list(args) if isinstance(args, (list, tuple)) else [args]
    for i, arg in enumerate(args):
        if not isinstance(arg, chainer.Variable):
            args[i] = chainer.Variable(arg)

    if isinstance(args, list):
        outputs = model(*args)
    elif isinstance(args, dict):
        outputs = model(**args)
    else:
        raise ValueError(
            'The \'args\' argument should be a list or dict. But a {} object '
            'was given.'.format(type(args)))

    input_tensor_ids = [id(arg) for arg in args]

    graph = []
    parameters = []
    param_names = {}
    input_tensors = []
    for name, param in model.namedparams():
        param_names[id(param)] = name
        parameters.append(
            convert_parameter(param, param_names))
        input_tensors.append(helper.make_tensor_value_info(
            name, _dtype[param.array.dtype], param.shape))

    if isinstance(outputs, dict):
        outputs = list(outputs.values())
    if not isinstance(outputs, (list, tuple)):
        outputs = (outputs,)
    output_tensor_ids = [id(output) for output in outputs]

    cands = []
    seen_edges = set()
    nodes = set()
    push_count = [0]

    def add_cand(cand):
        heapq.heappush(cands, (-cand.rank, push_count[0], cand))
        push_count[0] += 1

    for o in outputs:
        if isinstance(o, variable.Variable):
            o = o.node
        add_cand(o)
        nodes.add(o)

    while cands:
        _, _, cand = heapq.heappop(cands)
        if isinstance(cand, variable.VariableNode):
            creator = cand.creator_node
            if creator is not None and (creator, cand) not in seen_edges:
                add_cand(creator)
                seen_edges.add((creator, cand))
                nodes.add(creator)
                nodes.add(cand)
        elif isinstance(cand, function_node.FunctionNode):
            func_name = cand.__class__.__name__

            input_names = []
            for input_ in cand.inputs:
                if input_ is not cand and (input_, cand) not in seen_edges:
                    add_cand(input_)
                    seen_edges.add((input_, cand))
                    nodes.add(input_)
                    nodes.add(cand)

                # If it's a parameter
                if input_.name is not None:
                    input_names.append(id(input_.get_variable()))
                    setattr(cand, input_.name, input_.get_variable())
                else:
                    if id(input_.get_variable()) in input_tensor_ids:
                        input_id = id(input_.get_variable())
                    else:
                        input_id = id(input_)
                    input_names.append(input_id)

            for out_ in cand.outputs:
                out_ = out_()
                if out_.get_variable() is not None:
                    out_var = out_.get_variable()
                    if id(out_var) in output_tensor_ids:
                        idx = output_tensor_ids.index(id(out_var))
                        output_tensor_ids[idx] = (
                            str(id(out_)), _dtype[out_var.array.dtype],
                            out_var.shape)

            if func_name in _layers.keys():
                onnx_nodes = create_node(
                    func_name, cand, input_names, param_names, parameters,
                    input_tensors)
                graph.extend(onnx_nodes)

                # Add all the input values for the network to input_tensors
    for i, arg in enumerate(args):
        name = str(id(arg))
        input_tensors.append(helper.make_tensor_value_info(
            name, _dtype[arg.array.dtype], arg.shape))

    output_tensors = []
    for out_ in output_tensor_ids:
        output_tensors.append(helper.make_tensor_value_info(*out_))

    if not export_params:
        parameters = []

    onnx_graph = helper.make_graph(
        reversed(graph), graph_name, input_tensors, output_tensors,
        initializer=parameters)

    checker.check_graph(onnx_graph)

    model = helper.make_model(
        onnx_graph,
        producer_name='Chainer',
        producer_version=chainer.__version__)

    checker.check_model(model)

    if filename is not None:
        with open(filename, 'wb') as fp:
            fp.write(model.SerializeToString())
        if save_text:
            with open(filename + '.txt', 'w') as fp:
                print(model, file=fp)

    return model
