import threading
import multiprocessing
import numpy as np
import tensorflow as tf
import tensorflow.contrib.slim as slim
from random import choice
from time import sleep
from time import time
from utils import normalized_columns_initializer
import math
from tensorflow.python.framework import dtypes
from tensorflow.python.ops import random_ops
import flags

FLAGS = tf.app.flags.FLAGS


class AC_Network():
    def __init__(self, scope, nb_actions, trainer):
        with tf.variable_scope(scope):
            # Input and visual encoding layers
            self.inputs = tf.placeholder(
                shape=[None, FLAGS.resized_height, FLAGS.resized_width, FLAGS.agent_history_length], dtype=tf.float32)
            conv1_w = tf.get_variable("Conv1_W", shape=[8, 8, FLAGS.agent_history_length, 16],
                                      initializer=self.xavier_initializer())
            conv1_b = tf.Variable(tf.constant(0.01, shape=[16]), name='Conv1_b')

            self.conv1 = self.conv2d(self.inputs, conv1_w, conv1_b, strides=4, padding="VALID")
            # self.conv2 = slim.conv2d(activation_fn=tf.nn.elu,
            #                          inputs=self.conv1, num_outputs=32,
            #                          kernel_size=[4, 4], stride=[2, 2], padding='VALID')
            conv2_w = tf.get_variable("Conv2_W", shape=[4, 4, 16, 32],
                                      initializer=self.xavier_initializer())
            conv2_b = tf.Variable(tf.constant(0.01, shape=[32]), name="Conv2_b")
            self.conv2 = self.conv2d(self.conv1, conv2_w, conv2_b, strides=2, padding="VALID")
            # hidden = slim.fully_connected(slim.flatten(self.conv2), 256, activation_fn=tf.nn.elu)
            conv2_shape = self.conv2.get_shape()[1] * \
                          self.conv2.get_shape()[2] * \
                          self.conv2.get_shape()[3]
            conv2_shape = conv2_shape.value

            conv2_flat = tf.reshape(self.conv2, [-1, conv2_shape])
            fc1_w = tf.get_variable("FC1_W", shape=[conv2_shape, 256],
                                    initializer=self.xavier_initializer())
            fc1_b = tf.Variable(tf.constant(0.01, shape=[256]), name="FC1_b")
            hidden = tf.matmul(conv2_flat, fc1_w) + fc1_b
            hidden = tf.nn.elu(hidden)
            # Recurrent network for temporal dependencies
            lstm_cell = tf.nn.rnn_cell.BasicLSTMCell(256, state_is_tuple=True)
            c_init = np.zeros((1, lstm_cell.state_size.c), np.float32)
            h_init = np.zeros((1, lstm_cell.state_size.h), np.float32)
            self.state_init = [c_init, h_init]
            c_in = tf.placeholder(tf.float32, [1, lstm_cell.state_size.c])
            h_in = tf.placeholder(tf.float32, [1, lstm_cell.state_size.h])
            self.state_in = (c_in, h_in)
            rnn_in = tf.expand_dims(hidden, [0])
            step_size = tf.shape(self.inputs)[:1]
            state_in = tf.nn.rnn_cell.LSTMStateTuple(c_in, h_in)
            lstm_outputs, lstm_state = tf.nn.dynamic_rnn(
                lstm_cell, rnn_in, initial_state=state_in, sequence_length=step_size,
                time_major=False)
            lstm_c, lstm_h = lstm_state
            self.state_out = (lstm_c[:1, :], lstm_h[:1, :])
            rnn_out = tf.reshape(lstm_outputs, [-1, 256])

            # Output layers for policy and value estimations
            self.policy = slim.fully_connected(rnn_out, nb_actions,
                                               activation_fn=tf.nn.softmax,
                                               weights_initializer=normalized_columns_initializer(0.01),
                                               biases_initializer=None)
            self.value = slim.fully_connected(rnn_out, 1,
                                              activation_fn=None,
                                              weights_initializer=normalized_columns_initializer(1.0),
                                              biases_initializer=None)

            # Only the worker network need ops for loss functions and gradient updating.
            if scope != 'global':
                self.actions = tf.placeholder(shape=[None], dtype=tf.int32)
                self.actions_onehot = tf.one_hot(self.actions, nb_actions, dtype=tf.float32)
                self.target_v = tf.placeholder(shape=[None], dtype=tf.float32)
                self.advantages = tf.placeholder(shape=[None], dtype=tf.float32)

                self.responsible_outputs = tf.reduce_sum(self.policy * self.actions_onehot, [1])

                # Loss functions
                self.value_loss = 0.5 * tf.reduce_sum(tf.square(self.target_v - tf.reshape(self.value, [-1])))
                self.entropy = - tf.reduce_sum(self.policy * tf.log(self.policy))
                self.policy_loss = -tf.reduce_sum(tf.log(self.responsible_outputs) * self.advantages)
                self.loss = 0.5 * self.value_loss + self.policy_loss - self.entropy * 0.01

                # Get gradients from local network using local losses
                local_vars = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, scope)
                self.gradients = tf.gradients(self.loss, local_vars)
                self.var_norms = tf.global_norm(local_vars)
                grads, self.grad_norms = tf.clip_by_global_norm(self.gradients, 40.0)

                # Apply local gradients to global network
                global_vars = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, 'global')
                self.apply_grads = trainer.apply_gradients(zip(grads, global_vars))

        # # Create summaries to visualize weights
        # for var in tf.trainable_variables():
        #     tf.summary.histogram(var.name, var)
        # # Summarize all gradients
        # for grad, var in grads:
        #     tf.summary.histogram(var.name + '/gradient', grad)
        # Create some wrappers for simplicity

    def conv2d(self, x, W, b, strides=1, padding='SAME'):
        # Conv2D wrapper, with bias and relu activation
        x = tf.nn.conv2d(x, W, strides=[1, strides, strides, 1], padding=padding)
        x = tf.nn.bias_add(x, b)
        return tf.nn.relu(x)

    def maxpool2d(self, x, k=2, padding='SAME'):
        # MaxPool2D wrapper
        return tf.nn.max_pool(x, ksize=[1, k, k, 1], strides=[1, k, k, 1],
                              padding=padding)

    def xavier_initializer(self, uniform=True, seed=None, dtype=dtypes.float32):
        """Returns an initializer performing "Xavier" initialization for weights.
        This function implements the weight initialization from:
        Xavier Glorot and Yoshua Bengio (2010):
                 Understanding the difficulty of training deep feedforward neural
                 networks. International conference on artificial intelligence and
                 statistics.
        This initializer is designed to keep the scale of the gradients roughly the
        same in all layers. In uniform distribution this ends up being the range:
        `x = sqrt(6. / (in + out)); [-x, x]` and for normal distribution a standard
        deviation of `sqrt(3. / (in + out))` is used.
        Args:
          uniform: Whether to use uniform or normal distributed random initialization.
          seed: A Python integer. Used to create random seeds. See
            [`set_random_seed`](../../api_docs/python/constant_op.md#set_random_seed)
            for behavior.
          dtype: The data type. Only floating point types are supported.
        Returns:
          An initializer for a weight matrix.
        """
        return self.variance_scaling_initializer(factor=1.0, mode='FAN_AVG',
                                                 uniform=uniform, seed=seed, dtype=dtype)

    def variance_scaling_initializer(self, factor=2.0, mode='FAN_IN', uniform=False,
                                     seed=None, dtype=dtypes.float32):
        """Returns an initializer that generates tensors without scaling variance.
        When initializing a deep network, it is in principle advantageous to keep
        the scale of the input variance constant, so it does not explode or diminish
        by reaching the final layer. This initializer use the following formula:
        ```python
          if mode='FAN_IN': # Count only number of input connections.
            n = fan_in
          elif mode='FAN_OUT': # Count only number of output connections.
            n = fan_out
          elif mode='FAN_AVG': # Average number of inputs and output connections.
            n = (fan_in + fan_out)/2.0
            truncated_normal(shape, 0.0, stddev=sqrt(factor / n))
        ```
        * To get [Delving Deep into Rectifiers](
           http://arxiv.org/pdf/1502.01852v1.pdf), use (Default):<br/>
          `factor=2.0 mode='FAN_IN' uniform=False`
        * To get [Convolutional Architecture for Fast Feature Embedding](
           http://arxiv.org/abs/1408.5093), use:<br/>
          `factor=1.0 mode='FAN_IN' uniform=True`
        * To get [Understanding the difficulty of training deep feedforward neural
          networks](http://jmlr.org/proceedings/papers/v9/glorot10a/glorot10a.pdf),
          use:<br/>
          `factor=1.0 mode='FAN_AVG' uniform=True.`
        * To get `xavier_initializer` use either:<br/>
          `factor=1.0 mode='FAN_AVG' uniform=True`, or<br/>
          `factor=1.0 mode='FAN_AVG' uniform=False`.
        Args:
          factor: Float.  A multiplicative factor.
          mode: String.  'FAN_IN', 'FAN_OUT', 'FAN_AVG'.
          uniform: Whether to use uniform or normal distributed random initialization.
          seed: A Python integer. Used to create random seeds. See
            [`set_random_seed`](../../api_docs/python/constant_op.md#set_random_seed)
            for behavior.
          dtype: The data type. Only floating point types are supported.
        Returns:
          An initializer that generates tensors with unit variance.
        Raises:
          ValueError: if `dtype` is not a floating point type.
          TypeError: if `mode` is not in ['FAN_IN', 'FAN_OUT', 'FAN_AVG'].
        """
        if not dtype.is_floating:
            raise TypeError('Cannot create initializer for non-floating point type.')
        if mode not in ['FAN_IN', 'FAN_OUT', 'FAN_AVG']:
            raise TypeError('Unknow mode %s [FAN_IN, FAN_OUT, FAN_AVG]', mode)

        # pylint: disable=unused-argument
        def _initializer(shape, dtype=dtype, partition_info=None):
            """Initializer function."""
            if not dtype.is_floating:
                raise TypeError('Cannot create initializer for non-floating point type.')
            # Estimating fan_in and fan_out is not possible to do perfectly, but we try.
            # This is the right thing for matrix multiply and convolutions.
            if shape:
                fan_in = float(shape[-2]) if len(shape) > 1 else float(shape[-1])
                fan_out = float(shape[-1])
            else:
                fan_in = 1.0
                fan_out = 1.0
            for dim in shape[:-2]:
                fan_in *= float(dim)
                fan_out *= float(dim)
            if mode == 'FAN_IN':
                # Count only number of input connections.
                n = fan_in
            elif mode == 'FAN_OUT':
                # Count only number of output connections.
                n = fan_out
            elif mode == 'FAN_AVG':
                # Average number of inputs and output connections.
                n = (fan_in + fan_out) / 2.0
            if uniform:
                # To get stddev = math.sqrt(factor / n) need to adjust for uniform.
                limit = math.sqrt(3.0 * factor / n)
                return random_ops.random_uniform(shape, -limit, limit,
                                                 dtype, seed=seed)
            else:
                # To get stddev = math.sqrt(factor / n) need to adjust for truncated.
                trunc_stddev = math.sqrt(1.3 * factor / n)
                return random_ops.truncated_normal(shape, 0.0, trunc_stddev, dtype,
                                                   seed=seed)

        # pylint: enable=unused-argument

        return _initializer