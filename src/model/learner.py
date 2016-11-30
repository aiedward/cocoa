'''
Main learning loop.
'''

import os
import time
import tensorflow as tf
from lib import logstats
from vocab import is_entity
import resource
import numpy as np
from encdec import get_prediction

def memory():
    usage=resource.getrusage(resource.RUSAGE_SELF)
    return (usage[2]*resource.getpagesize()) / 1000000.0

def add_learner_arguments(parser):
    parser.add_argument('--optimizer', default='sgd', help='Optimization method')
    parser.add_argument('--grad-clip', type=int, default=5, help='Min and max values of gradients')
    parser.add_argument('--learning-rate', type=float, default=0.1, help='Learning rate')
    parser.add_argument('--max-epochs', type=int, default=10, help='Number of training epochs')
    parser.add_argument('--num-per-epoch', type=int, default=None, help='Number of examples per epoch')
    parser.add_argument('--print-every', type=int, default=1, help='Number of examples between printing training loss')
    parser.add_argument('--init-from', help='Initial parameters')
    parser.add_argument('--checkpoint', default='.', help='Directory to save learned models')
    parser.add_argument('--gpu', type=int, default=0, help='Use GPU or not')

optim = {'adagrad': tf.train.AdagradOptimizer,
         'sgd': tf.train.GradientDescentOptimizer,
         'adam': tf.train.AdamOptimizer,
        }

class Learner(object):
    def __init__(self, data, model, evaluator, batch_size=1, verbose=False):
        self.data = data  # DataGenerator object
        self.model = model
        self.vocab = data.mappings['vocab']
        if type(model).__name__ == 'BasicEncoderDecoder':
            self._run_batch = self._run_batch_basic
        elif type(model).__name__ == 'GraphEncoderDecoder':
            self._run_batch = self._run_batch_graph
        self.batch_size = batch_size
        self.evaluator = evaluator
        self.verbose = verbose

    def test_loss(self, sess, test_data, num_batches):
        '''
        Return the cross-entropy loss.
        '''
        summary_map = {}
        for i in xrange(num_batches):
            dialogue_batch = test_data.next()
            self._run_batch(dialogue_batch, sess, summary_map, test=True)
        return summary_map['loss']['mean']

    # TODO: don't need graphs in the parameters
    def _get_feed_dict(self, batch, encoder_init_state=None, graph_data=None, graphs=None, copy=False, checklists=None, copied_nodes=None):
        # NOTE: We need to do the processing here instead of in preprocess because the
        # graph is dynamic; also the original batch data should not be modified.
        if copy:
            targets = graphs.copy_targets(batch['targets'], self.vocab.size)
        else:
            targets = batch['targets']

        encoder_args = {'inputs': batch['encoder_inputs'],
                'last_inds': batch['encoder_inputs_last_inds'],
                'init_state': encoder_init_state,
                }
        decoder_args = {'inputs': batch['decoder_inputs'],
                'last_inds': batch['decoder_inputs_last_inds'],
                }
        kwargs = {'encoder': encoder_args,
                'decoder': decoder_args,
                'targets': targets,
                }

        if graph_data is not None:
            encoder_args['entities'] = graph_data['encoder_entities']
            decoder_args['entities'] = graph_data['decoder_entities']
            encoder_args['utterances'] = graph_data['utterances']
            kwargs['graph_embedder'] = graph_data
            decoder_args['checklists'] = checklists
            decoder_args['copied_nodes'] = copied_nodes

        feed_dict = self.model.get_feed_dict(**kwargs)
        return feed_dict

    def _print_batch(self, batch, preds, loss):
        encoder_tokens = batch['encoder_tokens']
        encoder_inputs = batch['encoder_inputs']
        decoder_inputs = batch['decoder_inputs']
        decoder_tokens = batch['decoder_tokens']
        targets = batch['targets']
        # Go over each example in the batch
        print '-------------- batch ----------------'
        for i in xrange(encoder_inputs.shape[0]):
            if len(decoder_tokens[i]) == 0:
                continue
            print i
            print 'RAW INPUT:', encoder_tokens[i]
            print 'RAW TARGET:', decoder_tokens[i]
            print '----------'
            print 'ENC INPUT:', self.data.textint_map.int_to_text(encoder_inputs[i], 'encoding')
            print 'DEC INPUT:', self.data.textint_map.int_to_text(decoder_inputs[i], 'decoding')
            print 'TARGET:', self.data.textint_map.int_to_text(targets[i], 'target')
            print 'PRED:', self.data.textint_map.int_to_text(preds[i], 'target')
            print 'LOSS:', loss[i]
            break

    def _run_batch_graph(self, dialogue_batch, sess, summary_map, test=False):
        '''
        Run truncated RNN through a sequence of batch examples with knowledge graphs.
        '''
        encoder_init_state = None
        utterances = None
        graphs = dialogue_batch['graph']
        for i, batch in enumerate(dialogue_batch['batch_seq']):
            graph_data = graphs.get_batch_data(batch['encoder_tokens'], batch['decoder_tokens'], utterances)
            checklists = graphs.get_checklists(batch['targets'], self.vocab)
            copied_nodes = graphs.get_copied_nodes(batch['targets'], self.vocab)
            feed_dict = self._get_feed_dict(batch, encoder_init_state, graph_data, graphs, self.data.copy, checklists, copied_nodes)
            if test:
                logits, final_state, utterances, loss, seq_loss = sess.run(
                        [self.model.decoder.output_dict['logits'],
                         self.model.decoder.output_dict['final_state'],
                         self.model.decoder.output_dict['utterances'],
                         self.model.loss, self.model.seq_loss], feed_dict=feed_dict)
            else:
                _, logits, final_state, utterances, loss, seq_loss, gn = sess.run(
                        [self.train_op,
                         self.model.decoder.output_dict['logits'],
                         self.model.decoder.output_dict['final_state'],
                         self.model.decoder.output_dict['utterances'],
                         self.model.loss,
                         self.model.seq_loss,
                         self.grad_norm], feed_dict=feed_dict)
            # NOTE: final_state = (rnn_state, attn, context)
            encoder_init_state = final_state[0]

            if self.verbose:
                preds = get_prediction(logits)
                preds = graphs.copy_preds(preds, self.data.mappings['vocab'].size)
                self._print_batch(batch, preds, seq_loss)

            logstats.update_summary_map(summary_map, {'loss': loss})
            if not test:
                logstats.update_summary_map(summary_map, {'grad_norm': gn})

    def _run_batch_basic(self, dialogue_batch, sess, summary_map, test=False):
        '''
        Run truncated RNN through a sequence of batch examples.
        '''
        encoder_init_state = None
        for batch in dialogue_batch['batch_seq']:
            feed_dict = self._get_feed_dict(batch, encoder_init_state)
            if test:
                logits, final_state, loss, seq_loss = sess.run([
                    self.model.decoder.output_dict['logits'],
                    self.model.decoder.output_dict['final_state'],
                    self.model.loss, self.model.seq_loss], feed_dict=feed_dict)
            else:
                _, logits, final_state, loss, seq_loss, gn = sess.run([
                    self.train_op,
                    self.model.decoder.output_dict['logits'],
                    self.model.decoder.output_dict['final_state'],
                    self.model.loss, self.model.seq_loss, self.grad_norm], feed_dict=feed_dict)
            encoder_init_state = final_state

            if self.verbose:
                preds = get_prediction(logits)
                self._print_batch(batch, preds, seq_loss)

            logstats.update_summary_map(summary_map, {'loss': loss})
            if not test:
                logstats.update_summary_map(summary_map, {'grad_norm': gn})

    def learn(self, args, config, ckpt=None, split='train'):
        assert args.optimizer in optim.keys()
        optimizer = optim[args.optimizer](args.learning_rate)

        # Gradient
        grads_and_vars = optimizer.compute_gradients(self.model.loss)
        if args.grad_clip > 0:
            min_grad, max_grad = -1.*args.grad_clip, args.grad_clip
            clipped_grads_and_vars = [(tf.clip_by_value(grad, min_grad, max_grad), var) for grad, var in grads_and_vars]
        else:
            clipped_grads_and_vars = grads_and_vars
        # TODO: clip has problem with indexedslices, don't use
        #self.clipped_grads = [grad for grad, var in clipped_grads_and_vars]
        #self.grads = [grad for grad, var in grads_and_vars]
        self.grad_norm = tf.global_norm([grad for grad, var in grads_and_vars])
        self.clipped_grad_norm = tf.global_norm([grad for grad, var in clipped_grads_and_vars])

        # Optimize
        self.train_op = optimizer.apply_gradients(clipped_grads_and_vars)

        # Training loop
        train_data = self.data.generator(split, self.batch_size)
        num_per_epoch = train_data.next()
        step = 0
        saver = tf.train.Saver()
        save_path = os.path.join(args.checkpoint, 'tf_model.ckpt')
        best_saver = tf.train.Saver(max_to_keep=1)
        best_checkpoint = args.checkpoint+'-best'
        if not os.path.isdir(best_checkpoint):
            os.mkdir(best_checkpoint)
        best_save_path = os.path.join(best_checkpoint, 'tf_model.ckpt')
        best_loss = float('inf')

        # Testing
        with tf.Session(config=config) as sess:
            tf.initialize_all_variables().run()
            if args.init_from:
                saver.restore(sess, ckpt.model_checkpoint_path)
            summary_map = {}
            for epoch in xrange(args.max_epochs):
                print '================== Epoch %d ==================' % (epoch+1)
                for i in xrange(num_per_epoch):
                    start_time = time.time()
                    self._run_batch(train_data.next(), sess, summary_map, test=False)
                    end_time = time.time()
                    logstats.update_summary_map(summary_map, \
                            {'time(s)/batch': end_time - start_time, \
                             'memory(MB)': memory()})
                    step += 1
                    if step % args.print_every == 0 or step % num_per_epoch == 0:
                        print '{}/{} (epoch {}) {}'.format(i+1, num_per_epoch, epoch+1, logstats.summary_map_to_str(summary_map))
                        summary_map = {}  # Reset
                step = 0

                # Save model after each epoch
                print 'Save model checkpoint to', save_path
                saver.save(sess, save_path, global_step=epoch)

                # Evaluate on dev
                for split, test_data, num_batches in self.evaluator.dataset():
                    print '================== Eval %s ==================' % split
                    print '================== Perplexity =================='
                    start_time = time.time()
                    loss = self.test_loss(sess, test_data, num_batches)
                    print 'loss=%.4f time(s)=%.4f' % (loss, time.time() - start_time)
                    print '================== Sampling =================='
                    start_time = time.time()
                    bleu, ent_recall = self.evaluator.test_bleu(sess, test_data, num_batches)
                    print 'bleu=%.4f entity_recall=%.4f time(s)=%.4f' % (bleu, ent_recall, time.time() - start_time)
                    if split == 'dev' and loss < best_loss:
                        print 'New best model'
                        best_loss = loss
                        best_saver.save(sess, best_save_path)
                        logstats.add('best model', {'bleu': bleu, 'entity_recall': ent_recall, 'loss': loss})
