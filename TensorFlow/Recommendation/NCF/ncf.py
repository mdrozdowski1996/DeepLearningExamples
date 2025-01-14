# Copyright (c) 2018, deepakn94, codyaustun, robieta. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# -----------------------------------------------------------------------
#
# Copyright (c) 2018, NVIDIA CORPORATION. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import sys
import time
from argparse import ArgumentParser

import tensorflow as tf
import pandas as pd
import numpy as np
import cupy as cp
import horovod.tensorflow as hvd

from mpi4py import MPI

from neumf import ncf_model_ops
from input_pipeline import DataGenerator

from logger.logger import LOGGER
from logger.autologging import log_args
from tensorflow.python.compiler.tensorrt import trt_convert as trt

def parse_args():
    """
    Parse command line arguments.
    """
    parser = ArgumentParser(description="Train a Neural Collaborative"
                                        " Filtering model")
    parser.add_argument('--data', type=str,
                        help='path to test and training data files')
    parser.add_argument('-e', '--epochs', type=int, default=40,
                        help='number of epochs to train for')
    parser.add_argument('-b', '--batch-size', type=int, default=1048576,
                        help='number of examples for each iteration')
    parser.add_argument('--valid-users-per-batch', type=int, default=5000,
                        help='Number of users tested in each evaluation batch')
    parser.add_argument('-f', '--factors', type=int, default=64,
                        help='number of predictive factors')
    parser.add_argument('--layers', nargs='+', type=int,
                        default=[256, 256, 128, 64],
                        help='size of hidden layers for MLP')
    parser.add_argument('-n', '--negative-samples', type=int, default=4,
                        help='number of negative examples per interaction')
    parser.add_argument('-l', '--learning-rate', type=float, default=0.0045,
                        help='learning rate for optimizer')
    parser.add_argument('-k', '--topk', type=int, default=10,
                        help='rank for test examples to be considered a hit')
    parser.add_argument('--seed', '-s', type=int, default=0,
                        help='manually set random seed for random number generation')
    parser.add_argument('--target', '-t', type=float, default=0.9562,
                        help='stop training early at target')
    parser.add_argument('--fp16', action='store_true', dest='amp',
                        help='enable half-precision computations using automatic mixed precision \
                              (only available in supported containers)')
    parser.add_argument('--manual-fp16', action='store_true', dest='fp16',
                        help='manually enable mixed precision using code changes')
    parser.add_argument('--xla', action='store_true',
                        help='enable TensorFlow XLA (Accelerated Linear Algebra)')
    parser.add_argument('--valid-negative', type=int, default=100,
                        help='Number of negative samples for each positive test example')
    parser.add_argument('--beta1', '-b1', type=float, default=0.25,
                        help='beta1 for Adam')
    parser.add_argument('--beta2', '-b2', type=float, default=0.5,
                        help='beta2 for Adam')
    parser.add_argument('--eps', type=float, default=1e-8,
                        help='epsilon for Adam')
    parser.add_argument('--dropout', type=float, default=0.5,
                        help='Dropout probability, if equal to 0 will not use dropout at all')
    parser.add_argument('--loss-scale', default=8192, type=int,
                        help='Loss scale value to use when manually enabling mixed precision')
    parser.add_argument('--checkpoint-dir', default='/data/checkpoints/', type=str,
                        help='Path to the store the result checkpoint file for training, \
                              or to read from for evaluation')
    parser.add_argument('--mode', choices=['train', 'test'], default='train', type=str,
                        help='Passing "test" will only run a single evaluation, \
                              otherwise full training will be performed')
    parser.add_argument('--no-neg-trick', action='store_true', dest='no_neg_trick',
                        help='do not use negative sample generation shortcut to speed up data \
                              augmentation (will increase GPU memory consumption)')
    parser.add_argument('--eval-after', type=int, default=8,
                        help='Perform evaluations only after this many epochs')
    parser.add_argument('--verbose', action='store_true',
                        help='Log the performance and accuracy after every epoch')
    parser.add_argument('--use_trt', action='store_true')
    parser.add_argument('--precision_mode', choices=['FP16', 'INT8', 'FP32'])

    return parser.parse_args()

def hvd_init():
    """
    Initialize Horovod
    """
    # Reduce logging
    os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'
    tf.logging.set_verbosity(tf.logging.ERROR)
    # Initialize horovod
    hvd.init()

    if hvd.rank() == 0:
        print('PY', sys.version)
        print('TF', tf.__version__)

def get_local_train_data(pos_train_users, pos_train_items, negative_samples):
    """
    For distributed, split up the train data and only keep the local portion
    """
    num_pos_samples = pos_train_users.shape[0]
    # Create the entire train set
    all_train_users = np.tile(pos_train_users, negative_samples+1)
    all_train_items = np.tile(pos_train_items, negative_samples+1)
    all_train_labels = np.zeros_like(all_train_users, dtype=np.float32)
    all_train_labels[:num_pos_samples] = 1.0

    # Get local training set
    split_size = all_train_users.shape[0] // hvd.size() + 1
    split_indices = np.arange(split_size, all_train_users.shape[0], split_size)
    all_train_users_splits = np.split(all_train_users, split_indices)
    all_train_items_splits = np.split(all_train_items, split_indices)
    all_train_labels_splits = np.split(all_train_labels, split_indices)
    assert len(all_train_users_splits) == hvd.size()
    local_train_users = all_train_users_splits[hvd.rank()]
    local_train_items = all_train_items_splits[hvd.rank()]
    local_train_labels = all_train_labels_splits[hvd.rank()]

    return local_train_users, local_train_items, local_train_labels

def get_local_test_data(pos_test_users, pos_test_items):
    """
    For distributed, split up the test data and only keep the local portion
    """
    split_size = pos_test_users.shape[0] // hvd.size() + 1
    split_indices = np.arange(split_size, pos_test_users.shape[0], split_size)
    test_users_splits = np.split(pos_test_users, split_indices)
    test_items_splits = np.split(pos_test_items, split_indices)
    assert len(test_users_splits) == hvd.size()
    local_test_users = test_users_splits[hvd.rank()]
    local_test_items = test_items_splits[hvd.rank()]

    return local_test_users, local_test_items

def main():
    """
    Run training/evaluation
    """
    script_start = time.time()
    hvd_init()
    mpi_comm = MPI.COMM_WORLD
    args = parse_args()

    if hvd.rank() == 0:
        log_args(args)

    if args.seed is not None:
        tf.random.set_random_seed(args.seed)
        np.random.seed(args.seed)
        cp.random.seed(args.seed)

    if args.amp:
        os.environ["TF_ENABLE_AUTO_MIXED_PRECISION"] = "1"
    if "TF_ENABLE_AUTO_MIXED_PRECISION" in os.environ \
       and os.environ["TF_ENABLE_AUTO_MIXED_PRECISION"] == "1":
        args.fp16 = False

    # directory to store/read final checkpoint
    if args.mode == 'train' and hvd.rank() == 0:
        print("Saving best checkpoint to {}".format(args.checkpoint_dir))
    elif hvd.rank() == 0:
        print("Reading checkpoint: {}".format(args.checkpoint_dir))
    if not os.path.exists(args.checkpoint_dir) and args.checkpoint_dir != '':
        os.makedirs(args.checkpoint_dir, exist_ok=True)
    final_checkpoint_path = os.path.join(args.checkpoint_dir, 'model.ckpt')

    # Load converted data and get statistics
    train_df = pd.read_pickle(args.data+'/train_ratings.pickle')
    test_df = pd.read_pickle(args.data+'/test_ratings.pickle')
    nb_users, nb_items = train_df.max() + 1

    # Extract train and test feature tensors from dataframe
    pos_train_users = train_df.iloc[:, 0].values.astype(np.int32)
    pos_train_items = train_df.iloc[:, 1].values.astype(np.int32)
    pos_test_users = test_df.iloc[:, 0].values.astype(np.int32)
    pos_test_items = test_df.iloc[:, 1].values.astype(np.int32)
    # Negatives indicator for negatives generation
    neg_mat = np.ones((nb_users, nb_items), dtype=np.bool)
    neg_mat[pos_train_users, pos_train_items] = 0

    # Get the local training/test data
    train_users, train_items, train_labels = get_local_train_data(
        pos_train_users, pos_train_items, args.negative_samples
    )
    test_users, test_items = get_local_test_data(
        pos_test_users, pos_test_items
    )

    # Create and run Data Generator in a separate thread
    data_generator = DataGenerator(
        args.seed,
        hvd.rank(),
        nb_users,
        nb_items,
        neg_mat,
        train_users,
        train_items,
        train_labels,
        args.batch_size // hvd.size(),
        args.negative_samples,
        test_users,
        test_items,
        args.valid_users_per_batch,
        args.valid_negative,
        use_neg_trick=False if args.no_neg_trick else True
        )

    # Create tensorflow session and saver
    config = tf.ConfigProto()
    config.gpu_options.allow_growth = True
    #config.gpu_options.visible_device_list = str(hvd.local_rank())
    if args.xla:
        config.graph_options.optimizer_options.global_jit_level = tf.OptimizerOptions.ON_1
    
    sess = tf.Session(config=config)

    # Input tensors
    users = tf.placeholder(tf.int32, shape=(None,), name='users')
    items = tf.placeholder(tf.int32, shape=(None,), name='items')
    labels = tf.placeholder(tf.int32, shape=(None,), name='labels')
    is_dup = tf.placeholder(tf.float32, shape=(None,), name='is_dup')
    dropout = tf.placeholder_with_default(args.dropout, shape=(), name='dropout')
    # Model ops and saver
    hit_rate, ndcg, eval_op, train_op = ncf_model_ops(
        users,
        items,
        labels,
        is_dup,
        params={
            'fp16': args.fp16,
            'val_batch_size': args.valid_negative+1,
            'top_k': args.topk,
            'learning_rate': args.learning_rate,
            'beta_1': args.beta1,
            'beta_2': args.beta2,
            'epsilon': args.eps,
            'num_users': nb_users,
            'num_items': nb_items,
            'num_factors': args.factors,
            'mf_reg': 0,
            'layer_sizes': args.layers,
            'layer_regs': [0. for i in args.layers],
            'dropout': dropout,
            'sigmoid': True,
            'loss_scale': args.loss_scale
        },
        eval_only=False if args.mode == 'train' else True
    )
    saver = tf.train.Saver()

    # Accuracy metric tensors
    hr_sum = tf.get_default_graph().get_tensor_by_name('neumf/hit_rate/total:0')
    hr_cnt = tf.get_default_graph().get_tensor_by_name('neumf/hit_rate/count:0')
    ndcg_sum = tf.get_default_graph().get_tensor_by_name('neumf/ndcg/total:0')
    ndcg_cnt = tf.get_default_graph().get_tensor_by_name('neumf/ndcg/count:0')

    # Prepare evaluation data
    data_generator.prepare_eval_data()

    # If test mode, run one eval
    if args.mode == 'test':
        saver.restore(sess, args.checkpoint_dir)
        sess.run(tf.local_variables_initializer())
        gd = sess.graph_def
        for node in gd.node:
            if node.op == 'AssignSub':
                node.op = 'Sub'
                if 'use_locking' in node.attr: del node.attr['use_locking']
            elif node.op == 'AssignAdd':
                node.op = 'Add'
                if 'use_locking' in node.attr: del node.attr['use_locking']

        frozen_graph = tf.graph_util.convert_variables_to_constants(
                           sess,
                           gd,
                           output_node_names=['neumf/group_deps'])
        sess.close()
        sess = tf.Session(config=config)
        print([n.name for n in frozen_graph.node])
        #print(frozen_graph)
        if args.use_trt:
            start_time = time.time()
            #nodes_blacklist=['neumf/hit_rate/total', 'neumf/hit_rate/count',  'neumf/ndcg/total', 'neumf/ndcg/count'],
            converter = trt.TrtGraphConverter(
                            input_graph_def=frozen_graph,
                            max_batch_size=args.batch_size,
                            nodes_blacklist=['neumf/group_deps'],
                            precision_mode=args.precision_mode.upper())
            frozen_graph = converter.convert()
            print("conversion time", time.time() - start_time)
        tf.reset_default_graph()
        print([n.name for n in frozen_graph.node])
        eval_op = tf.import_graph_def(
            frozen_graph,
            input_map={'users': users, 'items': items, 'is_dup': is_dup},
            return_elements=['neumf/group_deps'])
        print([n.name for n in tf.get_default_graph()])
        hr_sum = tf.get_default_graph().get_tensor_by_name('neumf/hit_rate/total:0')
        hr_cnt = tf.get_default_graph().get_tensor_by_name('neumf/hit_rate/count:0')
        ndcg_sum = tf.get_default_graph().get_tensor_by_name('neumf/ndcg/total:0')
        ndcg_cnt = tf.get_default_graph().get_tensor_by_name('neumf/ndcg/count:0')
        
        #print([n.name for n in tf.get_default_graph().as_graph_def().node])
        eval_start = time.time()
        
        for user_batch, item_batch, dup_batch \
            in zip(data_generator.eval_users, data_generator.eval_items, data_generator.dup_mask):
            sess.run(
                eval_op,
                feed_dict={
                    users: user_batch,
                    items: item_batch,
                    is_dup:dup_batch, dropout: 0.0
                }
            )
        eval_duration = time.time() - eval_start

        # Report results
        hit_rate_sum = sess.run(hvd.allreduce(hr_sum, average=False))
        hit_rate_cnt = sess.run(hvd.allreduce(hr_cnt, average=False))
        ndcg_sum = sess.run(hvd.allreduce(ndcg_sum, average=False))
        ndcg_cnt = sess.run(hvd.allreduce(ndcg_cnt, average=False))

        hit_rate = hit_rate_sum / hit_rate_cnt
        ndcg = ndcg_sum / ndcg_cnt

        if hvd.rank() == 0:
            LOGGER.log("Eval Time: {:.4f}, HR: {:.4f}, NDCG: {:.4f}"
                       .format(eval_duration, hit_rate, ndcg))
        return

    # Performance Metrics
    train_times = list()
    eval_times = list()
    # Accuracy Metrics
    first_to_target = None
    time_to_train = 0.0
    best_hr = 0
    best_epoch = 0
    # Manual initialize weights
    sess.run(tf.global_variables_initializer())
    # Buffers for global metrics
    global_hr_sum = np.ones(1)
    global_hr_count = np.ones(1)
    global_ndcg_sum = np.ones(1)
    global_ndcg_count = np.ones(1)
    # Buffers for local metrics
    local_hr_sum = np.ones(1)
    local_hr_count = np.ones(1)
    local_ndcg_sum = np.ones(1)
    local_ndcg_count = np.ones(1)

    # Begin training
    begin_train = time.time()
    if hvd.rank() == 0:
        LOGGER.log("Begin Training. Setup Time: {}".format(begin_train - script_start))
    for epoch in range(args.epochs):
        # Train for one epoch
        train_start = time.time()
        data_generator.prepare_train_data()
        for user_batch, item_batch, label_batch \
            in zip(data_generator.train_users_batches,
                   data_generator.train_items_batches,
                   data_generator.train_labels_batches):
            sess.run(
                train_op,
                feed_dict={
                    users: user_batch.get(),
                    items: item_batch.get(),
                    labels: label_batch.get()
                }
            )
        train_duration = time.time() - train_start
        ## Only log "warm" epochs
        if epoch >= 1:
            train_times.append(train_duration)
        # Evaluate
        if epoch > args.eval_after:
            eval_start = time.time()
            sess.run(tf.local_variables_initializer())
            for user_batch, item_batch, dup_batch \
                in zip(data_generator.eval_users,
                       data_generator.eval_items,
                       data_generator.dup_mask):
                sess.run(
                    eval_op,
                    feed_dict={
                        users: user_batch,
                        items: item_batch,
                        is_dup: dup_batch,
                        dropout: 0.0
                    }
                )
            # Compute local metrics
            local_hr_sum[0] = sess.run(hr_sum)
            local_hr_count[0] = sess.run(hr_cnt)
            local_ndcg_sum[0] = sess.run(ndcg_sum)
            local_ndcg_count[0] = sess.run(ndcg_cnt)
            # Reduce metrics across all workers
            mpi_comm.Reduce(local_hr_count, global_hr_count)
            mpi_comm.Reduce(local_hr_sum, global_hr_sum)
            mpi_comm.Reduce(local_ndcg_count, global_ndcg_count)
            mpi_comm.Reduce(local_ndcg_sum, global_ndcg_sum)
            # Calculate metrics
            hit_rate = global_hr_sum[0] / global_hr_count[0]
            ndcg = global_ndcg_sum[0] / global_ndcg_count[0]

            eval_duration = time.time() - eval_start
            ## Only log "warm" epochs
            if epoch >= 1:
                eval_times.append(eval_duration)

            if hvd.rank() == 0:
                if args.verbose:
                    log_string = "Epoch: {:02d}, Train Time: {:.4f}, Eval Time: {:.4f}, HR: {:.4f}, NDCG: {:.4f}"
                    LOGGER.log(
                        log_string.format(
                            epoch,
                            train_duration,
                            eval_duration,
                            hit_rate,
                            ndcg
                        )
                    )

                # Update summary metrics
                if hit_rate > args.target and first_to_target is None:
                    first_to_target = epoch
                    time_to_train = time.time() - begin_train
                if hit_rate > best_hr:
                    best_hr = hit_rate
                    best_epoch = epoch
                    if not args.verbose:
                        log_string = "New Best Epoch: {:02d}, Train Time: {:.4f}, Eval Time: {:.4f}, HR: {:.4f}, NDCG: {:.4f}"
                        LOGGER.log(
                            log_string.format(
                                epoch,
                                train_duration,
                                eval_duration,
                                hit_rate,
                                ndcg
                            )
                        )
                    # Save, if meets target
                    if hit_rate > args.target:
                        saver.save(sess, final_checkpoint_path)

    # Final Summary
    if hvd.rank() == 0:
        train_times = np.array(train_times)
        train_throughputs = pos_train_users.shape[0]*(args.negative_samples+1) / train_times
        eval_times = np.array(eval_times)
        eval_throughputs = pos_test_users.shape[0]*(args.valid_negative+1) / eval_times
        LOGGER.log(' ')
        LOGGER.log('Minimum Train Time per Epoch: {:.4f}'.format(np.min(train_times)))
        LOGGER.log('Average Train Time per Epoch: {:.4f}'.format(np.mean(train_times)))
        LOGGER.log('Average Train Throughput:     {:.4f}'.format(np.mean(train_throughputs)))
        LOGGER.log('Minimum Eval Time per Epoch:  {:.4f}'.format(np.min(eval_times)))
        LOGGER.log('Average Eval Time per Epoch:  {:.4f}'.format(np.mean(eval_times)))
        LOGGER.log('Average Eval Throughput:      {:.4f}'.format(np.mean(eval_throughputs)))
        LOGGER.log('First Epoch to hit:           {}'.format(first_to_target))
        LOGGER.log('Time to Train:                {:.4f}'.format(time_to_train))
        LOGGER.log('Best HR:                      {:.4f}'.format(best_hr))
        LOGGER.log('Best Epoch:                   {}'.format(best_epoch))

    sess.close()
    return

if __name__ == '__main__':
    main()
