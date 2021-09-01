from tvm import relay
import tvm
from tvm.relay.transform.backend_operator.utils import is_function_node
from tvm.relay.transform.backend_operator.target import *
from tvm.relay.transform.optimizer.custom_fusion_pass import CustomFusionPass
from workloads.torch_workloads import get_network_from_torch
from workloads.relay_workloads import get_network_from_relay
from tvm.contrib import graph_executor as runtime
import numpy as np
import argparse
from tvm import autotvm, auto_scheduler
from tvm.relay.transform.utility.debug_helper import *
from workloads.torch_workloads import *
from e2e_perf_logger import *

import time
import os

from tvm.relay.transform.backend_operator.backend_op_lib import BackendOpLib

def setup_attrs_ours(net, net_name, hw_name, batch_size):
    net = net.with_attr(NETWORK_FUNC_ATTR, net_name)
    net = net.with_attr(HW_FUNC_ATTR, hw_name)
    net = net.with_attr(BATCH_SIZE_ATTR, batch_size)

    return net

# Setup attribute for CuDNN backend baseline
def setup_attrs_single_backend_baseline(net, net_name, hw_name, batch_size, single_backend_id):
    net = net.with_attr("CustomFusionPass", CustomFusionPass.SINGLE_BACKEND_BASELINE)

    net = net.with_attr(NETWORK_FUNC_ATTR, net_name)
    net = net.with_attr(HW_FUNC_ATTR, hw_name)
    net = net.with_attr(BATCH_SIZE_ATTR, batch_size)
    net = net.with_attr(SINGLE_BACKEND_ATTR, single_backend_id)

    return net

def measure_end_to_end_perf_tensorrt(mod, params, target_str, shape_dict, hw_name):
    from tvm.relay.op.contrib.tensorrt import partition_for_tensorrt
    mod, config = partition_for_tensorrt(mod, params)

    # Debug to check if TRT supports ops of interest
    # print(mod["main"])

    with tvm.transform.PassContext(opt_level=OPT_LEVEL.get(), config={'relay.ext.tensorrt.options': config}):
        lib = relay.build(mod, target=target_str, params=params)

    lib.export_library('compiled.so')

    dev = tvm.gpu(0)
    loaded_lib = tvm.runtime.load_module('compiled.so')
    module = tvm.contrib.graph_executor.GraphModule(loaded_lib['default'](dev))

    # Setup execution
    for input_name, input_shape in shape_dict.items():
        input_data = np.random.uniform(-1, 1, size=input_shape).astype("float32")
        module.set_input(input_name, input_data)

    ftimer = module.module.time_evaluator("run", dev, number=NUM_MEASUREMENTS_PER_REPEAT_E2E, repeat=NUM_REPEATS_E2E)
    mean_perf, std_perf = measure(ftimer, True, hw_name)

    return mean_perf, std_perf, module

def build_and_measure_autotvm(net, params, target_str, shape_dict, hw_name):
    # else:
    with autotvm.apply_history_best(get_autotvm_log_path(hw_name)):
        with tvm.transform.PassContext(opt_level=OPT_LEVEL.get()):
            lib = relay.build(net, target_str, params=params)
        print(f"We successfully built the network")
        # Create workload
        dev = tvm.device(target_str, 0)
        module = runtime.GraphModule(lib["default"](dev))

        # Setup execution
        for input_name, input_shape in shape_dict.items():
            input_data = np.random.uniform(-1, 1, size=input_shape).astype("float32")
            module.set_input(input_name, input_data)

    ftimer = module.module.time_evaluator("run", dev, number=NUM_MEASUREMENTS_PER_REPEAT_E2E, repeat=NUM_REPEATS_E2E)
    mean_perf, std_perf = measure(ftimer, True, hw_name)

    return mean_perf, std_perf, module


def measure_end_to_end_perf_autotvm(net, params, target_str, shape_dict, method_mode, net_name, hw_name, batch_size):
    assert is_function_node(net)

    if method_mode is not None:
        net = net.with_attr("CustomFusionPass", method_mode)
        net = setup_attrs_ours(net, net_name, hw_name, batch_size)

    return build_and_measure_autotvm(net, params, target_str, shape_dict, hw_name)

def measure_end_to_end_perf_cudnn(net, params, target_str, shape_dict, is_ours, net_name, hw_name, batch_size):
    assert is_function_node(net)
    single_backend_id = Target.CUDNN.id()
    net = setup_attrs_single_backend_baseline(net, net_name, hw_name, batch_size, single_backend_id)

    return build_and_measure_autotvm(net, params, target_str, shape_dict, hw_name)


def measure_end_to_end_perf_autosch(net, params, target_str, shape_dict, is_ours, hw_name):
    assert is_function_node(net)
    if is_ours:
        net = net.with_attr("CustomFusionPass", CustomFusionPass.DP)

    with auto_scheduler.ApplyHistoryBest(AUTOSCH_LOG):
        with tvm.transform.PassContext(opt_level=OPT_LEVEL.get()):
            lib = relay.build(net, target_str, params=params)

    # Create workload
    dev = tvm.device(target_str, 0)
    module = runtime.GraphModule(lib["default"](dev))

    # Setup execution
    for input_name, input_shape in shape_dict.items():
        input_data = np.random.uniform(-1, 1, size=input_shape).astype("float32")
        module.set_input(input_name, input_data)

    ftimer = module.module.time_evaluator("run", dev, number=NUM_MEASUREMENTS_PER_REPEAT_E2E, repeat=NUM_REPEATS_E2E)
    mean_perf, std_perf = measure(ftimer, True, hw_name)

    return mean_perf, std_perf, module


def verify_network_output(net, shape_dict, mod_tvm, mod_ours):
    assert is_function_node(net)

    # Create same input data for two networks
    name_to_data = {}
    for input_name, input_shape in shape_dict.items():
        input_data = np.random.uniform(-1, 1, size=input_shape).astype("float32")
        name_to_data[input_name] = input_data

    # Setup execution
    for input_name, input_data in name_to_data.items():
        mod_tvm.set_input(input_name, input_data)

    mod_tvm.run()
    out_tvm = mod_tvm.get_output(0).asnumpy()

    # Setup execution
    for input_name, input_data in name_to_data.items():
        mod_ours.set_input(input_name, input_data)

    mod_ours.run()
    out_ours = mod_ours.get_output(0).asnumpy()

    TOL = 1e-01
    print("First 10 outputs")
    print(f"TVM    : {out_tvm.flatten()[:10]}")
    # print(f"AutoTVM: {out_tvm.flatten()[:10]}")
    print(f"Ours   : {out_ours.flatten()[:10]}")
    assert np.allclose(out_tvm, out_ours, rtol=TOL, atol=TOL)

    print(f"Passed the verification of output test")
    print(f"Worst diffence : {np.abs((out_ours - out_tvm)).max():.4f}")

def args_checker(args, parser):
    is_missing_arg = not args.network
    is_missing_arg |= not args.hw
    # is_missing_arg |= not args.batch_size
    # is_missing_arg |= not args.target
    # is_missing_arg |= not args.dtype

    if is_missing_arg:
        parser.error('Make sure you input all arguments')

def get_args():
    parser = argparse.ArgumentParser()
    # Default type is string for argparse
    parser.add_argument("-n", "--network", help="name of a neural network")
    parser.add_argument("-hw", "--hw", help="target hardware")
    parser.add_argument("-bs", "--batch-size", default=1, type=int, help="batch size")
    # parser.add_argument("-t", "--target", help="target device")
    # parser.add_argument("-dt", "--dtype", help="data type")

    args = parser.parse_args()

    args_checker(args, parser)
    return args

def measure_dp_and_baselines(mod, params, shape_dict, args):
    mean_perf, std_perf, mod_dp = measure_end_to_end_perf_autotvm(mod["main"], params, args.target, shape_dict,
                                                                  CustomFusionPass.DP,
                                                                  args.network, args.hw, args.batch_size)
    print(f"[{args.network}] Performance of DP on {args.hw} (mean, std) = ({mean_perf:.4f}+-{std_perf:.4f})")
    E2EPerfLogger().log_perf(args.hw, args.network, 'DP', mean_perf, std_perf)

    mean_perf, std_perf, mod_tvm = measure_end_to_end_perf_autotvm(mod["main"], params, args.target, shape_dict,
                                                                   None,
                                                                   args.network, args.hw, args.batch_size)
    print(f"[{args.network}] Performance of AutoTVM on {args.hw} (mean, std) = ({mean_perf:.4f}+-{std_perf:.4f})")
    E2EPerfLogger().log_perf(args.hw, args.network, 'AutoTVM', mean_perf, std_perf)

    mean_perf, std_perf, mod_trt = measure_end_to_end_perf_tensorrt(mod, params, args.target, shape_dict, args.hw)
    print(f"[{args.network}] Performance of TensorRT on {args.hw} (mean, std) = ({mean_perf:.4f}+-{std_perf:.4f})")
    E2EPerfLogger().log_perf(args.hw, args.network, 'TensorRT', mean_perf, std_perf)

    mean_perf, std_perf, mod_cud = measure_end_to_end_perf_cudnn(mod["main"], params, args.target, shape_dict,
                                                                 False, args.network, args.hw, args.batch_size)
    print(f"[{args.network}] Performance of cuDNN on {args.hw} (mean, std) = ({mean_perf:.4f}+-{std_perf:.4f})")
    E2EPerfLogger().log_perf(args.hw, args.network, 'cuDNN', mean_perf, std_perf)

    # mean_perf, std_perf = measure_end_to_end_perf_autosch(mod["main"], params, 'cuda', shape_dict, False, args.hw)
    # print(f"[AutoSCH] Performance of {args.network} (mean, std) = ({mean_perf:.4f}+-{std_perf:.4f})")

    verify_network_output(mod["main"], shape_dict, mod_tvm, mod_dp)

def measure_two_level(mod, params, shape_dict, args):
    mean_perf, std_perf, mod_two_level = measure_end_to_end_perf_autotvm(mod["main"], params, args.target, shape_dict,
                                                                    CustomFusionPass.TWO_LEVEL_OPT,
                                                                    args.network, args.hw, args.batch_size)
    print(f"[{args.network}] Performance of Two-level opt on {args.hw} (mean, std) = ({mean_perf:.4f}+-{std_perf:.4f})")
    E2EPerfLogger().log_perf(args.hw, args.network, 'Two-level', mean_perf, std_perf)

    mean_perf, std_perf, mod_tvm = measure_end_to_end_perf_autotvm(mod["main"], params, args.target, shape_dict,
                                                                   None,
                                                                   args.network, args.hw, args.batch_size)
    print(f"[{args.network}] Performance of AutoTVM on {args.hw} (mean, std) = ({mean_perf:.4f}+-{std_perf:.4f})")
    E2EPerfLogger().log_perf(args.hw, args.network, 'AutoTVM', mean_perf, std_perf)

    verify_network_output(mod["main"], shape_dict, mod_tvm, mod_two_level)

def build_dp(net, params, target_str, shape_dict, net_name, hw_name, batch_size):
    net = net.with_attr("CustomFusionPass", CustomFusionPass.DP)
    net = setup_attrs_ours(net, net_name, hw_name, batch_size)

    with autotvm.apply_history_best(get_autotvm_log_path(hw_name)):
        with tvm.transform.PassContext(opt_level=OPT_LEVEL.get()):
            lib = relay.build(net, target_str, params=params)

    print(f"We successfully built the network")

"""
Measure time spent for DP algorithm (dp) and op measurement (measurement)
"""
def measure_dp_tuning_time(mod, params, shape_dict, args):
    n_trial = 1
    dp_time_arr = []
    measurement_time_arr = []

    for i in range(n_trial):
        # Delete operator_cost log
        print("Delete operator cost for measurement")
        os.system(f"rm /home/byungsoj/tvm/python/tvm/relay/transform/logs/operator_cost_{args.hw}.*")

        # Measure dp + measurement time
        start_time = time.time()
        build_dp(mod["main"], params, args.target, shape_dict, args.network, args.hw, args.batch_size)

        dp_and_measurement_time = time.time() - start_time
        print(f"[{args.network}] Elapsed time of DP + Measurement on {args.hw} = {dp_and_measurement_time:.4f}s")


        # Measure DP time
        start_time = time.time()
        build_dp(mod["main"], params, args.target, shape_dict, args.network, args.hw, args.batch_size)

        dp_time = time.time() - start_time
        print(f"[{args.network}] Elapsed time of DP on {args.hw} = {dp_time:.4f}s")

        # Get measurement time
        measurement_time = dp_and_measurement_time - dp_time
        print(f"[{args.network}] Elapsed time of Measurement on {args.hw} = {measurement_time:.4f}s")

        dp_time_arr.append(dp_time)
        measurement_time_arr.append(measurement_time)

    DPTuningTimeLogger().log_perf(args.hw, args.network, "DP", np.mean(dp_time_arr), np.std(dp_time_arr))
    DPTuningTimeLogger().log_perf(args.hw, args.network, "Op Profiling", np.mean(measurement_time_arr),
                                  np.std(measurement_time_arr))

if __name__ == "__main__":
    args = get_args()
    # Redirect output to log files
    log_dir = "e2e_measure_logs"
    setup_logging(log_dir, task_name="e2e_measure", net_name=args.network, hw_name=args.hw, batch_size=args.batch_size)

    # NasNet-A only works for opt_level 2 (not 3 due to the avgpool2d issue)
    # if args.network == "nasneta":
    #     OPT_LEVEL.set(2)

    # We can't test this because this network include batch norm.
    print(f"batch size: {args.batch_size}")

    mod, params, shape_dict, _ = get_network_from_torch(args.network, args.batch_size)
    # mod, params, shape_dict, _ = get_network_from_torch("nasneta", 1)
    # mod, params, shape_dict, _ = get_network_from_relay("conv2d", 1)
    # mod, params, shape_dict, _ = get_network_from_relay("conv2d+relu_x2", 1)
    # mod, params, shape_dict, _ = get_network_from_relay("diamond", 1)
    # mod, params, shape_dict, _ = crop_network_from_torch(args.network, 1, 290)

    # Assign build target based on a given hw
    args.target = get_build_target(args.hw)

    # measure_dp_and_baselines(mod, params, shape_dict, args)
    # measure_two_level(mod, params, shape_dict, args)
    measure_dp_tuning_time(mod, params, shape_dict, args)



