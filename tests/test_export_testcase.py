import os

import chainer
import chainer.functions as F
import chainer.links as L
import numpy as np
import onnx
import pytest

from onnx_chainer import export_testcase


@pytest.fixture(scope='function')
def model():
    return chainer.Sequential(
        L.Convolution2D(None, 16, 5, 1, 2),
        F.relu,
        L.Convolution2D(16, 8, 5, 1, 2),
        F.relu,
        L.Convolution2D(8, 5, 5, 1, 2),
        F.relu,
        L.Linear(None, 100),
        F.relu,
        L.Linear(100, 10)
    )


@pytest.fixture(scope='function')
def x():
    return np.zeros((1, 3, 28, 28), dtype=np.float32)


@pytest.mark.parametrize('in_names,out_names',
                         [(None, None), (['x'], ['y'])])
def test_export_testcase(
        tmpdir, model, x, desable_experimental_warning, in_names, out_names):
    # Just check the existence of pb files
    path = tmpdir.mkdir('test_export_testcase').dirname
    export_testcase(model, (x,), path,
                    input_names=in_names, output_names=out_names)

    assert os.path.isfile(os.path.join(path, 'model.onnx'))
    input_pb_path = os.path.join(path, 'test_data_set_0', 'input_0.pb')
    assert os.path.isfile(input_pb_path)
    input_tensor = onnx.load_tensor(input_pb_path)
    assert input_tensor.name == (in_names[0] if in_names else 'Input_0')
    output_pb_path = os.path.join(path, 'test_data_set_0', 'output_0.pb')
    assert os.path.isfile(output_pb_path)
    output_tensor = onnx.load_tensor(output_pb_path)
    assert output_tensor.name == (
        out_names[0] if out_names else 'LinearFunction_1')


def test_output_grad(tmpdir, model, x, desable_experimental_warning):
    path = tmpdir.mkdir('test_export_testcase_with_grad').dirname
    export_testcase(model, (x,), path, output_grad=True, train=True)

    model_filename = os.path.join(path, 'model.onnx')
    assert os.path.isfile(model_filename)
    assert os.path.isfile(os.path.join(path, 'test_data_set_0', 'input_0.pb'))
    assert os.path.isfile(os.path.join(path, 'test_data_set_0', 'output_0.pb'))

    onnx_model = onnx.load(model_filename)
    initializer_names = {i.name for i in onnx_model.graph.initializer}

    # 10 gradient files should be there
    for i in range(10):
        tensor_filename = os.path.join(
            path, 'test_data_set_0', 'gradient_{}.pb'.format(i))
        assert os.path.isfile(tensor_filename)
        tensor = onnx.load_tensor(tensor_filename)
        assert tensor.name.startswith('param_')
        assert tensor.name in initializer_names
    assert not os.path.isfile(
        os.path.join(path, 'test_data_set_0', 'gradient_10.pb'))
