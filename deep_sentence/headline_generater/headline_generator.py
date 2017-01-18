from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import random

import numpy as np
from six.moves import xrange  # pylint: disable=redefined-builtin
import tensorflow as tf
from tensorflow.python.framework import ops

PAD_ID = 0
GO_ID = 0
EOS_ID = 2
UNK_ID = 3

class HeadlineGenerator:

  def __init__(self, vocab_size, size, max_gradient_norm,
        batch_size, learning_rate, learning_rate_decay_factor, num_layers=1,
        use_lstm=True, num_samples=2048, forward_only=False, dtype=tf.float32):

    self.encoder_size = 300
    self.decoder_size = 30
    self.vocab_size = vocab_size
    self.batch_size = batch_size
    self.learning_rate = tf.Variable(float(learning_rate), trainable=False, dtype=dtype)
    self.learning_rate_decay_op = self.learning_rate.assign(self.learning_rate * learning_rate_decay_factor)
    self.global_step = tf.Variable(0, trainable=False)

    # If we use sampled softmax, we need an output projection.
    output_projection = None
    softmax_loss_function = None
    # Sampled softmax only makes sense if we sample less than vocabulary size.
    if 0 < num_samples < self.vocab_size:
      w_t = tf.get_variable("proj_w", [self.vocab_size, size], dtype=dtype)
      w = tf.transpose(w_t)
      b = tf.get_variable("proj_b", [self.vocab_size], dtype=dtype)
      output_projection = (w, b)

      def sampled_loss(labels, inputs):
        labels = tf.reshape(labels, [-1, 1])
        # We need to compute the sampled_softmax_loss using 32bit floats to
        # avoid numerical instabilities.
        local_w_t = tf.cast(w_t, tf.float32)
        local_b = tf.cast(b, tf.float32)
        local_inputs = tf.cast(inputs, tf.float32)
        return tf.cast(tf.nn.sampled_softmax_loss(
                        weights=local_w_t,
                        biases=local_b,
                        inputs=local_inputs,
                        labels=labels,
                        num_sampled=num_samples,
                        num_classes=self.vocab_size), dtype)
      softmax_loss_function = sampled_loss

    # Create the internal multi-layer cell for our RNN.
    single_cell = tf.nn.rnn_cell.GRUCell(size)
    if use_lstm:
      single_cell = tf.nn.rnn_cell.BasicLSTMCell(size)
    cell = single_cell
    if num_layers > 1:
      cell = tf.nn.rnn_cell.MultiRNNCell([single_cell] * num_layers)

    # The seq2seq function: we use embedding for the input and attention.
    def seq2seq_f(encoder_inputs, decoder_inputs, do_decode):
      return tf.nn.seq2seq.embedding_attention_seq2seq(
          encoder_inputs=encoder_inputs,
          decoder_inputs=decoder_inputs,
          cell=cell,
          num_encoder_symbols=vocab_size,
          num_decoder_symbols=vocab_size,
          embedding_size=size,
          output_projection=output_projection,
          feed_previous=do_decode,
          dtype=dtype)

    # Feeds for inputs.
    MAX_ENCODER_INPUTS_SIZE = 300
    MAX_DECODER_INPUTS_SIZE = 30
    self.encoder_inputs = [tf.placeholder(tf.int32, shape=[None], name="encoder{0}".format(i)) for i in xrange(MAX_ENCODER_INPUTS_SIZE)]
    self.decoder_inputs = [tf.placeholder(tf.int32, shape=[None], name="decoder{0}".format(i)) for i in xrange(MAX_DECODER_INPUTS_SIZE+1)]
    self.target_weights = [tf.placeholder(dtype, shape=[None], name="weight{0}".format(i)) for i in xrange(MAX_DECODER_INPUTS_SIZE+1)]

    # Our targets are decoder inputs shifted by one.
    targets = [self.decoder_inputs[i + 1] for i in xrange(len(self.decoder_inputs) - 1)]

    # Training outputs and losses.
    if forward_only:
      self.output, _ = seq2seq_f(self.encoder_inputs, self.decoder_inputs, True)
      per_example_loss=False
      if per_example_loss:
        self.loss = tf.nn.seq2seq.sequence_loss_by_example(
              self.output[:MAX_DECODER_INPUTS_SIZE],
              targets,
              self.target_weights[:MAX_DECODER_INPUTS_SIZE],
              softmax_loss_function=softmax_loss_function)
      else:
        loss = tf.nn.seq2seq.sequence_loss(
              self.output[:MAX_DECODER_INPUTS_SIZE],
              targets,
              self.target_weights[:MAX_DECODER_INPUTS_SIZE],
              softmax_loss_function=softmax_loss_function)

      # If we use output projection, we need to project outputs for decoding.
      if output_projection is not None:
          self.output = [tf.matmul(output, output_projection[0]) + output_projection[1]
              for output in self.output]
    else:
      self.output, _ = seq2seq_f(self.encoder_inputs, self.decoder_inputs, True)
      per_example_loss=False
      if per_example_loss:
        self.loss = tf.nn.seq2seq.sequence_loss_by_example(
              self.output[:MAX_DECODER_INPUTS_SIZE],
              targets,
              self.target_weights[:MAX_DECODER_INPUTS_SIZE],
              softmax_loss_function=softmax_loss_function)
      else:
        loss = tf.nn.seq2seq.sequence_loss(
              self.output[:MAX_DECODER_INPUTS_SIZE],
              targets,
              self.target_weights[:MAX_DECODER_INPUTS_SIZE],
              softmax_loss_function=softmax_loss_function)

    # Gradients and SGD update operation for training the model.
    params = tf.trainable_variables()
    if not forward_only:
      opt = tf.train.GradientDescentOptimizer(self.learning_rate)
      gradients = tf.gradients(self.loss, params)
      clipped_gradients, self.gradient_norm = tf.clip_by_global_norm(gradients, max_gradient_norm)
      self.update = opt.apply_gradients(zip(clipped_gradients, params), global_step=self.global_step)

    self.saver = tf.train.Saver(tf.global_variables())


  def get_batch(self, data):
    """Get a random batch of data from the specified bucket, prepare for step.
    To feed data in step(..) it must be a list of batch-major vectors, while
    data here contains single length-major cases. So the main logic of this
    function is to re-index data cases to be in the proper format for feeding.
    Args:
      data: a tuple of size len(self.buckets) in which each element contains
        lists of pairs of input and output data that we use to create a batch.
      bucket_id: integer, which bucket to get the batch for.
    Returns:
      The triple (encoder_inputs, decoder_inputs, target_weights) for
      the constructed batch that has the proper format to call step(...) later.
    """
    encoder_size, decoder_size = self.encoder_size, self.decoder_size
    encoder_inputs, decoder_inputs = [], []

    # Get a random batch of encoder and decoder inputs from data,
    # pad them if needed, reverse encoder inputs and add GO to decoder.
    for _ in xrange(self.batch_size):
      encoder_input, decoder_input = random.choice(data)

      # Encoder inputs are padded and then reversed.
      encoder_pad = [PAD_ID] * (self.encoder_size - len(encoder_input))
      encoder_inputs.append(list(reversed(encoder_input + encoder_pad)))

      # Decoder inputs get an extra "GO" symbol, and are padded then.
      decoder_pad_size = self.decoder_size - len(decoder_input) - 1
      decoder_inputs.append([GO_ID] + decoder_input + [PAD_ID] * decoder_pad_size)

    # Now we create batch-major vectors from the data selected above.
    batch_encoder_inputs, batch_decoder_inputs, batch_weights = [], [], []

    # Batch encoder inputs are just re-indexed encoder_inputs.
    for length_idx in xrange(self.encoder_size):
      batch_encoder_inputs.append(
          np.array([encoder_inputs[batch_idx][length_idx]
                    for batch_idx in xrange(self.batch_size)], dtype=np.int32))
    # Batch decoder inputs are re-indexed decoder_inputs, we create weights.
    for length_idx in xrange(self.decoder_size):
      batch_decoder_inputs.append(
          np.array([decoder_inputs[batch_idx][length_idx]
                    for batch_idx in xrange(self.batch_size)], dtype=np.int32))
      # Create target_weights to be 0 for targets that are padding.
      batch_weight = np.ones(self.batch_size, dtype=np.float32)
      for batch_idx in xrange(self.batch_size):
        # We set weight to 0 if the corresponding target is a PAD symbol.
        # The corresponding target is decoder_input shifted by 1 forward.
        if length_idx < decoder_size - 1:
          target = decoder_inputs[batch_idx][length_idx + 1]
        if length_idx == decoder_size - 1 or target == PAD_ID:
          batch_weight[batch_idx] = 0.0
      batch_weights.append(batch_weight)
    return batch_encoder_inputs, batch_decoder_inputs, batch_weights


  def step(self, session, encoder_inputs, decoder_inputs, target_weights, forward_only):
    """Run a step of the model feeding the given inputs.
    Args:
      session: tensorflow session to use.
      encoder_inputs: list of numpy int vectors to feed as encoder inputs.
      decoder_inputs: list of numpy int vectors to feed as decoder inputs.
      target_weights: list of numpy float vectors to feed as target weights.
      forward_only: whether to do the backward step or only forward.
    Returns:
      A triple consisting of gradient norm (or None if we did not do backward),
      average perplexity, and the outputs.
    Raises:
      ValueError: if length of encoder_inputs, decoder_inputs, or
        target_weights disagrees with bucket size for the specified bucket_id.
    """
    # Check if the sizes match.
    if len(encoder_inputs) != self.encoder_size:
      raise ValueError("Encoder length must be equal to the one in bucket,"
                       " %d != %d." % (len(encoder_inputs), self.encoder_size))
    if len(decoder_inputs) != self.decoder_size:
      raise ValueError("Decoder length must be equal to the one in bucket,"
                       " %d != %d." % (len(decoder_inputs), self.decoder_size))
    if len(target_weights) != self.decoder_size:
      raise ValueError("Weights length must be equal to the one in bucket,"
                       " %d != %d." % (len(target_weights), self.decoder_size))

    # Input feed: encoder inputs, decoder inputs, target_weights, as provided.
    input_feed = {}
    for l in xrange(self.encoder_size):
      input_feed[self.encoder_inputs[l].name] = encoder_inputs[l]
    for l in xrange(self.decoder_size):
      input_feed[self.decoder_inputs[l].name] = decoder_inputs[l]
      input_feed[self.target_weights[l].name] = target_weights[l]

    # Since our targets are decoder inputs shifted by one, we need one more.
    last_target = self.decoder_inputs[self.decoder_size].name
    input_feed[last_target] = np.zeros([self.batch_size], dtype=np.int32)

    # Output feed: depends on whether we do a backward step or not.
    if not forward_only:
      output_feed = [self.updates[bucket_id],  # Update Op that does SGD.
                     self.gradient_norms[bucket_id],  # Gradient norm.
                     self.losses[bucket_id]]  # Loss for this batch.
    else:
      output_feed = [self.losses[bucket_id]]  # Loss for this batch.
      for l in xrange(self.decoder_size):  # Output logits.
        output_feed.append(self.outputs[bucket_id][l])

    outputs = session.run(output_feed, input_feed)
    if not forward_only:
      return outputs[1], outputs[2], None  # Gradient norm, loss, no outputs.
    else:
      return None, outputs[0], outputs[1:]  # No gradient norm, loss, outputs.
