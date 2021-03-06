"""
 Copyright (c) 2019-2020 Intel Corporation
 Licensed under the Apache License, Version 2.0 (the "License");
 you may not use this file except in compliance with the License.
 You may obtain a copy of the License at
      http://www.apache.org/licenses/LICENSE-2.0
 Unless required by applicable law or agreed to in writing, software
 distributed under the License is distributed on an "AS IS" BASIS,
 WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 See the License for the specific language governing permissions and
 limitations under the License.
"""

import json
import shlex
import signal
import subprocess
import sys
import threading
import time
from enum import Enum, auto
from pathlib import Path

import os
import pytest
import tempfile
import torch

# pylint: disable=redefined-outer-name
from examples.common.optimizer import get_default_weight_decay
from examples.common.sample_config import SampleConfig
from examples.common.utils import get_name, is_staged_quantization
from nncf.compression_method_api import CompressionLevel
from nncf.config import NNCFConfig
from tests.conftest import EXAMPLES_DIR, PROJECT_ROOT, TEST_ROOT


class Command:
    def __init__(self, cmd, path=None):
        self.cmd = cmd
        self.process = None
        self.exec_time = -1
        self.output = []  # store output here
        self.kwargs = {}
        self.timeout = False
        self.path = path

        # set system/version dependent "start_new_session" analogs
        if sys.platform == "win32":
            self.kwargs.update(creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        elif sys.version_info < (3, 2):  # assume posix
            self.kwargs.update(preexec_fn=os.setsid)
        else:  # Python 3.2+ and Unix
            self.kwargs.update(start_new_session=True)

    def kill_process_tree(self, pid):
        try:
            if sys.platform != "win32":
                os.killpg(pid, signal.SIGKILL)
            else:
                subprocess.call(['taskkill', '/F', '/T', '/PID', str(pid)])
        except OSError as err:
            print(err)

    def run(self, timeout=3600, assert_returncode_zero=True):

        def target():
            start_time = time.time()
            self.process = subprocess.Popen(self.cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, shell=True,
                                            bufsize=1, cwd=self.path, **self.kwargs)
            self.timeout = False

            self.output = []
            for line in self.process.stdout:
                line = line.decode('utf-8')
                self.output.append(line)
                sys.stdout.write(line)

            sys.stdout.flush()
            self.process.stdout.close()

            self.process.wait()
            self.exec_time = time.time() - start_time

        thread = threading.Thread(target=target)
        thread.start()

        thread.join(timeout)
        if thread.is_alive():
            try:
                print("Error: process taking too long to complete--terminating" + ", [ " + self.cmd + " ]")
                self.kill_process_tree(self.process.pid)
                self.exec_time = timeout
                self.timeout = True
                thread.join()
            except OSError as e:
                print(self.process.pid, "Exception when try to kill task by PID, " + e.strerror)
                raise
        returncode = self.process.wait()
        print("Process returncode = " + str(returncode))
        if assert_returncode_zero:
            assert returncode == 0, "Process exited with a non-zero exit code {}; output:{}".format(
                returncode,
                "".join(self.output))
        return returncode

    def get_execution_time(self):
        return self.exec_time


class ConfigFactory:
    """Allows to modify config file before test run"""

    def __init__(self, base_config, config_path):
        self.config = base_config
        self.config_path = str(config_path)

    def serialize(self):
        with open(self.config_path, 'w') as f:
            json.dump(self.config, f)
        return self.config_path

    def __getitem__(self, item):
        return self.config[item]

    def __setitem__(self, key, value):
        self.config[key] = value


def create_command_line(args, sample_type):
    python_path = PROJECT_ROOT.as_posix()
    executable = EXAMPLES_DIR.joinpath(sample_type, 'main.py').as_posix()
    cli_args = " ".join(key if val is None else "{} {}".format(key, val) for key, val in args.items())
    return "PYTHONPATH={path} {python_exe} {main_py} {args}".format(
        path=python_path, main_py=executable, args=cli_args, python_exe=sys.executable
    )


SAMPLE_TYPES = ["classification", "semantic_segmentation", "object_detection"]

DATASETS = {
    "classification": ["mock_32x32", "mock_32x32", "mock_32x32", "mock_32x32"],
    "semantic_segmentation": ["camvid", "camvid"],
    "object_detection": ["voc"],
}

CONFIGS = {
    "classification": [TEST_ROOT.joinpath("data", "configs", "squeezenet1_1_cifar10_rb_sparsity_int8.json"),
                       TEST_ROOT.joinpath("data", "configs", "resnet18_cifar100_bin_xnor.json"),
                       TEST_ROOT.joinpath("data", "configs", "resnet18_cifar10_staged_quant.json"),
                       TEST_ROOT.joinpath("data", "configs", "resnet18_pruning_magnitude.json")],
    "semantic_segmentation": [TEST_ROOT.joinpath("data", "configs", "unet_camvid_int8.json"),
                              TEST_ROOT.joinpath("data", "configs", "unet_camvid_rb_sparsity.json")],
    "object_detection": [TEST_ROOT.joinpath("data", "configs", "ssd300_vgg_voc_int8.json")]
}

BATCHSIZE_PER_GPU = {
    "classification": [256, 256, 256, 128],
    "semantic_segmentation": [2, 2],
    "object_detection": [128],
}

DATASET_PATHS = {
    "classification": {
        x: lambda dataset_root: dataset_root if dataset_root else os.path.join(
            tempfile.gettempdir(), x) for x in DATASETS["classification"]
    },
    "semantic_segmentation": {
        DATASETS["semantic_segmentation"][0]: lambda dataset_root: TEST_ROOT.joinpath("data", "mock_datasets",
                                                                                      "camvid"),
        DATASETS["semantic_segmentation"][0]: lambda dataset_root: TEST_ROOT.joinpath("data", "mock_datasets", "camvid")
    },
    "object_detection": {
        DATASETS["object_detection"][0]: lambda dataset_root: TEST_ROOT.joinpath("data", "mock_datasets", "voc")
    },
}

CONFIG_PARAMS = list()
for sample_type in SAMPLE_TYPES:
    for tpl in list(zip(CONFIGS[sample_type], DATASETS[sample_type], BATCHSIZE_PER_GPU[sample_type])):
        CONFIG_PARAMS.append((sample_type,) + tpl)


@pytest.fixture(params=CONFIG_PARAMS,
                ids=["-".join([p[0], p[1].name, p[2], str(p[3])]) for p in CONFIG_PARAMS])
def config(request, dataset_dir):
    sample_type, config_path, dataset_name, batch_size = request.param
    dataset_path = DATASET_PATHS[sample_type][dataset_name](dataset_dir)

    with config_path.open() as f:
        jconfig = json.load(f)

    if "checkpoint_save_dir" in jconfig.keys():
        del jconfig["checkpoint_save_dir"]

    jconfig["dataset"] = dataset_name

    return {
        "sample_type": sample_type,
        'nncf_config': jconfig,
        "model_name": jconfig["model"],
        "dataset_path": dataset_path,
        "batch_size": batch_size,
    }


@pytest.fixture(scope="module")
def case_common_dirs(tmp_path_factory):
    return {
        "checkpoint_save_dir": str(tmp_path_factory.mktemp("models"))
    }


@pytest.mark.parametrize(" multiprocessing_distributed",
                         (True, False),
                         ids=['distributed', 'dataparallel'])
def test_pretrained_model_eval(config, tmp_path, multiprocessing_distributed):
    config_factory = ConfigFactory(config['nncf_config'], tmp_path / 'config.json')
    args = {
        "--mode": "test",
        "--data": config["dataset_path"],
        "--config": config_factory.serialize(),
        "--log-dir": tmp_path,
        "--batch-size": config["batch_size"] * torch.cuda.device_count(),
        "--workers": 0,  # Workaround for the PyTorch MultiProcessingDataLoader issue
        "--dist-url": "tcp://127.0.0.1:8987"
    }

    if multiprocessing_distributed:
        args["--multiprocessing-distributed"] = None

    runner = Command(create_command_line(args, config["sample_type"]))
    runner.run()


@pytest.mark.parametrize(
    "multiprocessing_distributed", [
        pytest.param(True, marks=pytest.mark.dependency(name=["train_distributed"])),
        pytest.param(False, marks=pytest.mark.dependency(name=["train_dataparallel"]))],
    ids=['distributed', 'dataparallel'])
def test_pretrained_model_train(config, tmp_path, multiprocessing_distributed, case_common_dirs):
    checkpoint_save_dir = os.path.join(case_common_dirs["checkpoint_save_dir"],
                                       "distributed" if multiprocessing_distributed else "data_parallel")
    config_factory = ConfigFactory(config['nncf_config'], tmp_path / 'config.json')
    args = {
        "--mode": "train",
        "--data": config["dataset_path"],
        "--config": config_factory.serialize(),
        "--log-dir": tmp_path,
        "--batch-size": config["batch_size"] * torch.cuda.device_count(),
        "--workers": 0,  # Workaround for the PyTorch MultiProcessingDataLoader issue
        "--epochs": 2,
        "--checkpoint-save-dir": checkpoint_save_dir,
        "--dist-url": "tcp://127.0.0.1:8989"
    }

    if multiprocessing_distributed:
        args["--multiprocessing-distributed"] = None

    runner = Command(create_command_line(args, config["sample_type"]))
    runner.run()
    last_checkpoint_path = os.path.join(checkpoint_save_dir, get_name(config_factory.config) + "_last.pth")
    assert os.path.exists(last_checkpoint_path)
    assert torch.load(last_checkpoint_path)['compression_level'] in (CompressionLevel.FULL, CompressionLevel.PARTIAL)


@pytest.mark.parametrize(
    "multiprocessing_distributed", [
        pytest.param(True, marks=pytest.mark.dependency(depends=["train_distributed"])),
        pytest.param(False, marks=pytest.mark.dependency(depends=["train_dataparallel"]))],
    ids=['distributed', 'dataparallel'])
def test_trained_model_eval(config, tmp_path, multiprocessing_distributed, case_common_dirs):
    config_factory = ConfigFactory(config['nncf_config'], tmp_path / 'config.json')
    ckpt_path = os.path.join(case_common_dirs["checkpoint_save_dir"],
                             "distributed" if multiprocessing_distributed else "data_parallel",
                             get_name(config_factory.config) + "_last.pth")
    args = {
        "--mode": "test",
        "--data": config["dataset_path"],
        "--config": config_factory.serialize(),
        "--log-dir": tmp_path,
        "--batch-size": config["batch_size"] * torch.cuda.device_count(),
        "--workers": 0,  # Workaround for the PyTorch MultiProcessingDataLoader issue
        "--weights": ckpt_path,
        "--dist-url": "tcp://127.0.0.1:8987"
    }

    if multiprocessing_distributed:
        args["--multiprocessing-distributed"] = None

    runner = Command(create_command_line(args, config["sample_type"]))
    runner.run()


def get_resuming_checkpoint_path(config_factory, multiprocessing_distributed, checkpoint_save_dir):
    return os.path.join(checkpoint_save_dir,
                        "distributed" if multiprocessing_distributed else "data_parallel",
                        get_name(config_factory.config) + "_last.pth")


@pytest.mark.parametrize(
    "multiprocessing_distributed", [
        pytest.param(True, marks=pytest.mark.dependency(depends=["train_distributed"])),
        pytest.param(False, marks=pytest.mark.dependency(depends=["train_dataparallel"]))],
    ids=['distributed', 'dataparallel'])
def test_resume(config, tmp_path, multiprocessing_distributed, case_common_dirs):
    checkpoint_save_dir = os.path.join(str(tmp_path), "models")
    config_factory = ConfigFactory(config['nncf_config'], tmp_path / 'config.json')
    ckpt_path = get_resuming_checkpoint_path(config_factory, multiprocessing_distributed,
                                             case_common_dirs["checkpoint_save_dir"])
    if "max_iter" in config_factory.config:
        config_factory.config["max_iter"] += 2
    args = {
        "--mode": "train",
        "--data": config["dataset_path"],
        "--config": config_factory.serialize(),
        "--log-dir": tmp_path,
        "--batch-size": config["batch_size"] * torch.cuda.device_count(),
        "--workers": 0,  # Workaround for the PyTorch MultiProcessingDataLoader issue
        "--epochs": 3,
        "--checkpoint-save-dir": checkpoint_save_dir,
        "--resume": ckpt_path,
        "--dist-url": "tcp://127.0.0.1:8986"
    }

    if multiprocessing_distributed:
        args["--multiprocessing-distributed"] = None

    runner = Command(create_command_line(args, config["sample_type"]))
    runner.run()
    last_checkpoint_path = os.path.join(checkpoint_save_dir, get_name(config_factory.config) + "_last.pth")
    assert os.path.exists(last_checkpoint_path)
    assert torch.load(last_checkpoint_path)['compression_level'] in (CompressionLevel.FULL, CompressionLevel.PARTIAL)


@pytest.mark.parametrize(
    "multiprocessing_distributed", [
        pytest.param(True, marks=pytest.mark.dependency(depends=["train_distributed"])),
        pytest.param(False, marks=pytest.mark.dependency(depends=["train_dataparallel"]))],
    ids=['distributed', 'dataparallel'])
def test_export_with_resume(config, tmp_path, multiprocessing_distributed, case_common_dirs):
    config_factory = ConfigFactory(config['nncf_config'], tmp_path / 'config.json')
    ckpt_path = get_resuming_checkpoint_path(config_factory, multiprocessing_distributed,
                                             case_common_dirs["checkpoint_save_dir"])

    onnx_path = os.path.join(str(tmp_path), "model.onnx")
    args = {
        "--mode": "test",
        "--config": config_factory.serialize(),
        "--resume": ckpt_path,
        "--to-onnx": onnx_path
    }

    runner = Command(create_command_line(args, config["sample_type"]))
    runner.run()
    assert os.path.exists(onnx_path)


def test_export_with_pretrained(tmp_path):
    config = SampleConfig()
    config.update({
        "model": "resnet18",
        "dataset": "imagenet",
        "input_info": {
            "sample_size": [2, 3, 299, 299]
        },
        "num_classes": 1000,
        "compression": {"algorithm": "magnitude_sparsity"}
    })
    config_factory = ConfigFactory(config, tmp_path / 'config.json')

    onnx_path = os.path.join(str(tmp_path), "model.onnx")
    args = {
        "--mode": "test",
        "--config": config_factory.serialize(),
        "--pretrained": '',
        "--to-onnx": onnx_path
    }

    runner = Command(create_command_line(args, "classification"))
    runner.run()
    assert os.path.exists(onnx_path)


@pytest.mark.parametrize(('algo', 'ref_weight_decay'),
                         (('rb_sparsity', 0),
                          ('const_sparsity', 1e-4),
                          ('magnitude_sparsity', 1e-4),
                          ('quantization', 1e-4)))
def test_get_default_weight_decay(algo, ref_weight_decay):
    config = NNCFConfig()
    config.update({"compression": {"algorithm": algo}})
    assert ref_weight_decay == get_default_weight_decay(config)


def test_cpu_only_mode_produces_cpu_only_model(config, tmp_path, mocker):
    config_factory = ConfigFactory(config['nncf_config'], tmp_path / 'config.json')
    args = {
        "--data": config["dataset_path"],
        "--config": config_factory.serialize(),
        "--log-dir": tmp_path,
        "--batch-size": config["batch_size"] * torch.cuda.device_count(),
        "--workers": 0,  # Workaround for the PyTorch MultiProcessingDataLoader issue
        "--epochs": 1,
        "--cpu-only": None
    }

    # to prevent starting a not closed mlflow session due to memory leak of config and SafeMLFLow happens with a
    # mocked train function
    mocker.patch("examples.common.utils.SafeMLFLow")
    command_line = " ".join(key if val is None else "{} {}".format(key, val) for key, val in args.items())
    if config["sample_type"] == "classification":
        import examples.classification.main as sample
        if is_staged_quantization(config['nncf_config']):
            mocker.patch("examples.classification.staged_quantization_worker.train_epoch_staged")
            mocker.patch("examples.classification.staged_quantization_worker.validate")
            import examples.classification.staged_quantization_worker as staged_worker
            staged_worker.validate.return_value = (0, 0)
        else:
            mocker.patch("examples.classification.main.train_epoch")
            mocker.patch("examples.classification.main.validate")
            sample.validate.return_value = (0, 0)
    elif config["sample_type"] == "semantic_segmentation":
        import examples.semantic_segmentation.main as sample
        import examples.semantic_segmentation.train
        mocker.spy(examples.semantic_segmentation.train.Train, "__init__")
    elif config["sample_type"] == "object_detection":
        import examples.object_detection.main as sample
        mocker.patch("examples.object_detection.main.train")

    sample.main(shlex.split(command_line))

    # pylint: disable=no-member
    if config["sample_type"] == "classification":
        if is_staged_quantization(config['nncf_config']):
            import examples.classification.staged_quantization_worker as staged_worker
            model_to_be_trained = staged_worker.train_epoch_staged.call_args[0][2]  # model
        else:
            model_to_be_trained = sample.train_epoch.call_args[0][1]  # model
    elif config["sample_type"] == "semantic_segmentation":
        model_to_be_trained = examples.semantic_segmentation.train.Train.__init__.call_args[0][1]  # model
    elif config["sample_type"] == "object_detection":
        model_to_be_trained = sample.train.call_args[0][0]  # net

    for p in model_to_be_trained.parameters():
        assert not p.is_cuda


class SampleType(Enum):
    CLASSIFICATION = auto()
    SEMANTIC_SEGMENTATION = auto()
    OBJECT_DETECTION = auto()


class TestCaseDescriptor:
    config_name: str
    config_path: Path
    sample_type: SampleType
    dataset_dir: Path
    dataset_name: str
    is_real_dataset: bool = False
    batch_size: int
    batch_size_init: int = None
    num_weights_to_init: int

    def batch(self, batch_size: int):
        self.batch_size = batch_size
        return self

    def batch_for_init(self, batch_size_init: int):
        self.batch_size_init = batch_size_init
        return self

    def config(self, config_name: str):
        self.config_path = TEST_ROOT.joinpath("data", "configs", "hawq", config_name)
        self.config_name = config_name
        return self

    def sample(self, sample_type: SampleType):
        self.sample_type = sample_type
        return self

    def real_dataset(self, dataset_name: str):
        self.dataset_name = dataset_name
        self.is_real_dataset = True
        return self

    def mock_dataset(self, dataset_name: str):
        self.dataset_dir = TEST_ROOT.joinpath("data", "mock_datasets", dataset_name)
        return self

    def num_weights(self, n: int):
        self.num_weights_to_init = n
        return self

    def __str__(self):
        return '_'.join([self.config_name, ])


TEST_CASE_DESCRIPTORS = [
    TestCaseDescriptor().
        config("inception_v3_cifar10_mixed_int.json").
        sample(SampleType.CLASSIFICATION).real_dataset('cifar10').batch(2).num_weights(95),
    TestCaseDescriptor().
        config("inception_v3_cifar10_mixed_int_staged.json").
        sample(SampleType.CLASSIFICATION).real_dataset('cifar10').batch(2).num_weights(95),
    TestCaseDescriptor().
        config("resnet18_cifar10_mixed_int.json").
        sample(SampleType.CLASSIFICATION).real_dataset('cifar10').batch(2).num_weights(21),
    TestCaseDescriptor().
        config("resnet18_cifar10_mixed_int_staged.json").
        sample(SampleType.CLASSIFICATION).real_dataset('cifar10').batch(2).num_weights(21),
    TestCaseDescriptor().
        config("resnet18_cifar10_mixed_int.json").
        sample(SampleType.CLASSIFICATION).real_dataset('cifar10').batch(3).num_weights(21).batch_for_init(2),
    TestCaseDescriptor().
        config("resnet18_cifar10_mixed_int_staged.json").
        sample(SampleType.CLASSIFICATION).real_dataset('cifar10').batch(3).num_weights(21).batch_for_init(2),
    TestCaseDescriptor().
        config("ssd300_vgg_voc_mixed_int.json").
        sample(SampleType.OBJECT_DETECTION).mock_dataset('voc').batch(2).num_weights(35),
    TestCaseDescriptor().
        config("ssd300_vgg_voc_mixed_int.json").
        sample(SampleType.OBJECT_DETECTION).mock_dataset('voc').batch(3).num_weights(35).batch_for_init(2),
    TestCaseDescriptor().
        config("unet_camvid_mixed_int.json").
        sample(SampleType.SEMANTIC_SEGMENTATION).mock_dataset('camvid').batch(2).num_weights(23),
    TestCaseDescriptor().
        config("icnet_camvid_mixed_int.json").
        sample(SampleType.SEMANTIC_SEGMENTATION).mock_dataset('camvid').batch(2).num_weights(64),
    TestCaseDescriptor().
        config("unet_camvid_mixed_int.json").
        sample(SampleType.SEMANTIC_SEGMENTATION).mock_dataset('camvid').batch(3).num_weights(23).batch_for_init(2),
]


@pytest.fixture(params=TEST_CASE_DESCRIPTORS, ids=[str(d) for d in TEST_CASE_DESCRIPTORS])
def hawq_config(request, dataset_dir):
    desc: TestCaseDescriptor = request.param
    if desc.is_real_dataset:
        desc.dataset_dir = Path(
            dataset_dir if dataset_dir else os.path.join(tempfile.gettempdir(), desc.dataset_name))
    return desc


def test_hawq_init(hawq_config, tmp_path, mocker):
    args = {
        "--data": str(hawq_config.dataset_dir),
        "--config": str(hawq_config.config_path),
        "--log-dir": tmp_path,
        "--batch-size": hawq_config.batch_size,
        "--workers": 0,  # Workaround for the PyTorch MultiProcessingDataLoader issue
    }
    batch_size_for_init = hawq_config.batch_size_init
    if batch_size_for_init:
        args["--batch-size-init"] = batch_size_for_init
    command_line = " ".join(f'{key} {val}' for key, val in args.items())
    # to prevent starting a not closed mlflow session due to memory leak of config and SafeMLFLow happens with a
    # mocked train function
    mocker.patch("examples.common.utils.SafeMLFLow")
    if hawq_config.sample_type == SampleType.CLASSIFICATION:
        import examples.classification.main as sample
        mocker.patch("examples.classification.staged_quantization_worker.train_staged")
        mocker.patch("examples.classification.main.train")
    elif hawq_config.sample_type == SampleType.SEMANTIC_SEGMENTATION:
        import examples.semantic_segmentation.main as sample
        mocker.patch("examples.semantic_segmentation.main.train")
    elif hawq_config.sample_type == SampleType.OBJECT_DETECTION:
        import examples.object_detection.main as sample
        mocker.patch("examples.object_detection.main.train")
    from nncf.quantization.init_precision import HAWQPrecisionInitializer
    set_chosen_config_spy = mocker.spy(HAWQPrecisionInitializer, "set_chosen_config")
    from nncf.quantization.hessian_trace import HessianTraceEstimator
    hessian_trace_estimator_spy = mocker.spy(HessianTraceEstimator, "__init__")

    sample.main(shlex.split(command_line))

    bitwidth_list = set_chosen_config_spy.call_args[0][1]
    assert len(bitwidth_list) == hawq_config.num_weights_to_init
    # with default compression ratio = 1.5 should be always a mixed precision model
    assert len(set(bitwidth_list)) > 1

    init_data_loader = hessian_trace_estimator_spy.call_args[0][5]
    expected_batch_size = batch_size_for_init if batch_size_for_init else hawq_config.batch_size
    assert init_data_loader.batch_size == expected_batch_size
