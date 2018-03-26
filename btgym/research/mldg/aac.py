import tensorflow as tf
import numpy as np

import sys
from logbook import Logger, StreamHandler
from btgym.research.gps.aac import GuidedAAC
from btgym.algorithms.runner.synchro import BaseSynchroRunner


class SubAAC(GuidedAAC):
    """
    Sub AAC trainer as lower-level part of meta-trainer.
    """

    def __init__(
            self,
            trial_source_target_cycle=(1, 0),
            num_episodes_per_trial=1,
            **kwargs
    ):
        super(SubAAC, self).__init__(**kwargs)
        self.current_data = None
        self.current_feed_dict = None

        # Trials sampling control:
        self.num_source_trials = trial_source_target_cycle[0]
        self.num_target_trials = trial_source_target_cycle[-1]
        self.num_episodes_per_trial = num_episodes_per_trial

        # Note that only master (test runner) is requesting trials

        self.current_source_trial = 0
        self.current_target_trial = 0
        self.current_trial_mode = 0  # source
        self.current_episode = 0

    def process(self, sess):
        """
        self.process() logic is defined by meta-trainer.
        """
        pass

    def get_sample_config(self, mode=0):
        """
        Returns environment configuration parameters for next episode to sample.

        Args:
              mode:     bool, False for slave (train data), True for master (test data)

        Returns:
            configuration dictionary of type `btgym.datafeed.base.EnvResetConfig`
        """

        new_trial = 0
        if mode:
            # Only master environment updates counters:
            if self.current_episode >= self.num_episodes_per_trial:
                # Reset episode counter:
                self.current_episode = 0

                # Request new trial:
                new_trial = 1
                # Decide on trial type (source/target):
                if self.current_source_trial >= self.num_source_trials:
                    # Time to switch to target mode:
                    self.current_trial_mode = 1
                    # Reset counters:
                    self.current_source_trial = 0
                    self.current_target_trial = 0

                if self.current_target_trial >= self.num_target_trials:
                    # Vise versa:
                    self.current_trial_mode = 0
                    self.current_source_trial = 0
                    self.current_target_trial = 0

                # Update counter:
                if self.current_trial_mode:
                    self.current_target_trial += 1
                else:
                    self.current_source_trial += 1

            self.current_episode += 1
        else:
            new_trial = 1  # slave env. gets new trial anyway

        # Compose btgym.datafeed.base.EnvResetConfig-consistent dict:
        sample_config = dict(
            episode_config=dict(
                get_new=True,
                sample_type=int(not mode),
                b_alpha=1.0,
                b_beta=1.0
            ),
            trial_config=dict(
                get_new=new_trial,
                sample_type=self.current_trial_mode,
                b_alpha=1.0,
                b_beta=1.0
            )
        )
        return sample_config


class AMLDG():
    """
    Asynchronous implementation of MLDG algorithm
    for continuous adaptation in dynamically changing environments.

    Papers:
        Da Li et al.,
         "Learning to Generalize: Meta-Learning for Domain Generalization"
         https://arxiv.org/abs/1710.03463

        Maruan Al-Shedivat et al.,
        "Continuous Adaptation via Meta-Learning in Nonstationary and Competitive Environments"
        https://arxiv.org/abs/1710.03641

    """
    def __init__(
            self,
            env,
            task,
            log_level,
            aac_class_ref=SubAAC,
            runner_config=None,
            aac_lambda=1.0,
            guided_lambda=1.0,
            rollout_length=20,
            trial_source_target_cycle=(1, 0),
            num_episodes_per_trial=1,  # one-shot adaptation
            _aux_render_modes=('action_prob', 'value_fn', 'lstm_1_h', 'lstm_2_h'),
            name='AMLDG',
            **kwargs
    ):
        try:
            self.aac_class_ref = aac_class_ref
            self.task = task
            self.name = name
            self.summary_writer = None

            StreamHandler(sys.stdout).push_application()
            self.log = Logger('{}_{}'.format(name, task), level=log_level)
            self.rollout_length = rollout_length

            if runner_config is None:
                self.runner_config = {
                    'class_ref': BaseSynchroRunner,
                    'kwargs': {},
                }
            else:
                self.runner_config = runner_config

            self.env_list = env

            assert isinstance(self.env_list, list) and len(self.env_list) == 2, \
                'Expected pair of environments, got: {}'.format(self.env_list)

            # Instantiate two sub-trainers: one for test and one for train environments:

            self.runner_config['kwargs']['data_sample_config'] = {'mode': 1}  # master
            self.runner_config['kwargs']['name'] = 'master'

            self.train_aac = aac_class_ref(
                env=self.env_list[0],  # train data will be master environment
                task=self.task,
                log_level=log_level,
                runner_config=self.runner_config,
                aac_lambda=aac_lambda,
                guided_lambda=guided_lambda,
                rollout_length=self.rollout_length,
                trial_source_target_cycle=trial_source_target_cycle,
                num_episodes_per_trial=num_episodes_per_trial,
                _use_target_policy=False,
                _use_global_network=True,
                _aux_render_modes=_aux_render_modes,
                name=self.name + '_sub_Train',
                **kwargs
            )

            self.runner_config['kwargs']['data_sample_config'] = {'mode': 0}  # master
            self.runner_config['kwargs']['name'] = 'slave'

            self.test_aac = aac_class_ref(
                env=self.env_list[-1],  # test data -> slave env.
                task=self.task,
                log_level=log_level,
                runner_config=self.runner_config,
                aac_lambda=aac_lambda,
                guided_lambda=guided_lambda,
                rollout_length=self.rollout_length,
                trial_source_target_cycle=trial_source_target_cycle,
                num_episodes_per_trial=num_episodes_per_trial,
                _use_target_policy=False,
                _use_global_network=False,
                global_step_op=self.train_aac.global_step,
                global_episode_op=self.train_aac.global_episode,
                inc_episode_op=self.train_aac.inc_episode,
                _aux_render_modes=_aux_render_modes,
                name=self.name + '_sub_Test',
                **kwargs
            )

            self.local_steps = self.train_aac.local_steps
            self.model_summary_freq = self.train_aac.model_summary_freq
            #self.model_summary_op = self.train_aac.model_summary_op

            self._make_train_op()
            self.test_aac.model_summary_op = tf.summary.merge(
                [self.test_aac.model_summary_op, self._combine_meta_summaries()],
                name='meta_model_summary'
            )

        except:
            msg = 'AMLDG.__init()__ exception occurred' + \
                  '\n\nPress `Ctrl-C` or jupyter:[Kernel]->[Interrupt] for clean exit.\n'
            self.log.exception(msg)
            raise RuntimeError(msg)

    def _make_train_op(self):
        """

        Defines:
            tensors holding training op graph for sub trainers and self;
        """
        pi = self.train_aac.local_network
        pi_prime = self.test_aac.local_network

        self.test_aac.sync = self.test_aac.sync_pi = tf.group(
            *[v1.assign(v2) for v1, v2 in zip(pi_prime.var_list, pi.var_list)]
        )

        self.global_step = self.train_aac.global_step
        self.global_episode = self.train_aac.global_episode

        self.test_aac.global_step = self.train_aac.global_step
        self.test_aac.global_episode = self.train_aac.global_episode
        self.test_aac.inc_episode = self.train_aac.inc_episode
        self.train_aac.inc_episode = None
        self.inc_step = self.train_aac.inc_step

        # Meta-loss:
        self.loss = 0.5 * self.train_aac.loss + 0.5 * self.test_aac.loss

        # Clipped gradients:
        self.train_aac.grads, _ = tf.clip_by_global_norm(
            tf.gradients(self.train_aac.loss, pi.var_list),
            40.0
        )
        #self.log.warning('self.train_aac.grads: {}'.format(len(list(self.train_aac.grads))))

        # Meta-gradient:
        grads_i, _ = tf.clip_by_global_norm(
            tf.gradients(self.train_aac.loss, pi.var_list),
            40.0
        )

        grads_i_next, _ = tf.clip_by_global_norm(
            tf.gradients(self.test_aac.loss, pi_prime.var_list),
            40.0
        )

        self.grads = []
        for g1, g2 in zip(grads_i, grads_i_next):
            if g1 is not None and g2 is not None:
                meta_g = 0.5 * g1 + 0.5 * g2
            else:
                meta_g = None

            self.grads.append(meta_g)

        #self.log.warning('self.grads_len: {}'.format(len(list(self.grads))))

        # Gradients to update local copy of pi_prime (from train data):
        train_grads_and_vars = list(zip(self.train_aac.grads, pi_prime.var_list))

        # self.log.warning('train_grads_and_vars_len: {}'.format(len(train_grads_and_vars)))

        # Meta-gradients to be sent to parameter server:
        meta_grads_and_vars = list(zip(self.grads, self.train_aac.network.var_list))

        # self.log.warning('meta_grads_and_vars_len: {}'.format(len(meta_grads_and_vars)))

        # Set global_step increment equal to observation space batch size:
        obs_space_keys = list(self.train_aac.local_network.on_state_in.keys())

        assert 'external' in obs_space_keys, \
            'Expected observation space to contain `external` mode, got: {}'.format(obs_space_keys)
        self.train_aac.inc_step = self.train_aac.global_step.assign_add(
            tf.shape(self.train_aac.local_network.on_state_in['external'])[0]
        )

        self.train_op = self.train_aac.optimizer.apply_gradients(train_grads_and_vars)

        # Optimizer for meta-update:
        #self.optimizer = tf.train.AdamOptimizer(self.train_aac.train_learn_rate, epsilon=1e-5)
        # ..non-decaying:
        self.optimizer = tf.train.AdamOptimizer(self.train_aac.opt_learn_rate, epsilon=1e-5)
        # TODO: own alpha-leran rate
        self.meta_train_op = self.optimizer.apply_gradients(meta_grads_and_vars)

        self.log.debug('meta_train_op defined')

    def _combine_meta_summaries(self):

        meta_model_summaries = [
            tf.summary.scalar("meta_grad_global_norm", tf.global_norm(self.grads)),
            tf.summary.scalar("total_meta_loss", self.loss),
        ]

        return meta_model_summaries

    def start(self, sess, summary_writer, **kwargs):
        """
        Executes all initializing operations,
        starts environment runner[s].
        Supposed to be called by parent worker just before training loop starts.

        Args:
            sess:           tf session object.
            kwargs:         not used by default.
        """
        try:
            # Copy weights from global to local:
            sess.run(self.train_aac.sync_pi)
            sess.run(self.test_aac.sync_pi)

            # Start thread_runners:
            self.train_aac._start_runners(   # master first
                sess,
                summary_writer,
                init_context=None,
                data_sample_config=self.train_aac.get_sample_config(mode=1)
            )
            self.test_aac._start_runners(
                sess,
                summary_writer,
                init_context=None,
                data_sample_config=self.test_aac.get_sample_config(mode=0)
            )

            self.summary_writer = summary_writer
            self.log.notice('Runners started.')

        except:
            msg = 'start() exception occurred' + \
                '\n\nPress `Ctrl-C` or jupyter:[Kernel]->[Interrupt] for clean exit.\n'
            self.log.exception(msg)
            raise RuntimeError(msg)

    def process(self, sess):
        """
        Meta-train/test procedure for one-shot learning. One call runs entire episode.

        Args:
            sess (tensorflow.Session):   tf session obj.

        """
        try:
            # Copy from parameter server:
            sess.run(self.train_aac.sync_pi)
            sess.run(self.test_aac.sync_pi)
            # self.log.warning('Init Sync ok.')

            # Decide on data configuration for train/test trajectories,
            # (want both data streams come from  same trial,
            # and trial type(either from source or target domain);
            # note: data_config counters get updated once per process() call
            train_data_config = self.train_aac.get_sample_config(mode=1)  # master env., samples trial
            test_data_config = self.train_aac.get_sample_config(mode=0)   # slave env, catches up with same trial

            # self.log.warning('train_data_config: {}'.format(train_data_config))
            # self.log.warning('test_data_config: {}'.format(test_data_config))

            # If this step data comes from source or target domain:
            is_target = train_data_config['trial_config']['sample_type']
            done = False

            # Collect initial train trajectory rollout:
            train_data = self.train_aac.get_data(data_sample_config=train_data_config, force_new_episode=True)
            feed_dict = self.train_aac.process_data(sess, train_data, is_train=True)

            # self.log.warning('Init Train data ok.')

            # Disable changing trials,
            # in case train episode termintaes earlier than test - we need to resample train episode from same trial:
            train_data_config['trial_config']['get_new'] = 0

            roll_num = 0

            while not done:
                # self.log.warning('Roll #{}'.format(roll_num))

                # Collect entire episode rollout by rollout:
                wirte_model_summary = \
                    self.local_steps % self.model_summary_freq == 0

                # self.log.warning(
                #     'Train data trial_num: {}'.format(
                #         np.asarray(train_data['on_policy'][0]['state']['metadata']['trial_num'])
                #     )
                # )

                train_trial_chksum = np.average(train_data['on_policy'][0]['state']['metadata']['trial_num'])

                # Update pi_prime parameters wrt collected data:
                if wirte_model_summary:
                    fetches = [self.train_op, self.train_aac.model_summary_op]
                else:
                    fetches = [self.train_op]

                fetched = sess.run(fetches, feed_dict=feed_dict)

                # self.log.warning('Train gradients ok.')

                # Collect test rollout using updated pi_prime policy:
                test_data = self.test_aac.get_data(data_sample_config=test_data_config)

                # If test episode ended?
                done = np.asarray(test_data['terminal']).any()

                # self.log.warning(
                #     'Test data trial_num: {}'.format(
                #         np.asarray(test_data['on_policy'][0]['state']['metadata']['trial_num'])
                #     )
                # )

                test_trial_chksum = np.average(test_data['on_policy'][0]['state']['metadata']['trial_num'])

                if roll_num == 0 and train_trial_chksum != test_trial_chksum:
                    # Ensure test data consistency, can correct if episode just started:
                    test_data = self.test_aac.get_data(data_sample_config=test_data_config, force_new_episode=True)
                    done = np.asarray(test_data['terminal']).any()
                    test_trial_chksum = np.average(test_data['on_policy'][0]['state']['metadata']['trial_num'])

                    self.log.warning(
                        'Test trial corrected: {}'.format(
                            np.asarray(test_data['on_policy'][0]['state']['metadata']['trial_num'])
                        )
                    )

                # self.log.warning(
                #     'roll # {}: train_trial_chksum: {}, test_trial_chksum: {}'.
                #         format(roll_num, train_trial_chksum, test_trial_chksum)
                # )

                if train_trial_chksum != test_trial_chksum:
                    # Still error? - highly probable algorithm logic bug. Issue warning.
                    msg = 'Train/test trials mismatch found!\nGot train trials: {},\nTest trials: {}'. \
                        format(
                        train_data['on_policy'][0]['state']['metadata']['trial_num'][0],
                        test_data['on_policy'][0]['state']['metadata']['trial_num'][0]
                        )
                    msg2 = 'Train data config: {}\n Test data config: {}'.format(train_data_config, test_data_config)

                    self.log.warning(msg)
                    self.log.warning(msg2)

                # Check episode type consistency; if failed - h.p. logic bug, warn:
                try:
                    assert (np.asarray(test_data['on_policy'][0]['state']['metadata']['type']) == 1).any()
                    assert (np.asarray(train_data['on_policy'][0]['state']['metadata']['type']) == 0).any()
                except AssertionError:
                    msg = 'Train/test episodes types mismatch found!\nGot train ep. type: {},\nTest ep.type: {}'. \
                        format(
                        train_data['on_policy'][0]['state']['metadata']['type'],
                        test_data['on_policy'][0]['state']['metadata']['type']
                    )
                    self.log.warning(msg)

                # self.log.warning('Test data ok.')

                if not is_target:
                    # Process test data and perform meta-train step:
                    feed_dict.update(self.test_aac.process_data(sess, test_data, is_train=True))

                    if wirte_model_summary:
                        meta_fetches = [self.meta_train_op, self.test_aac.model_summary_op, self.inc_step]
                    else:
                        meta_fetches = [self.meta_train_op, self.inc_step]

                    meta_fetched = sess.run(meta_fetches, feed_dict=feed_dict)

                    # self.log.warning('Meta-gradients ok.')
                else:
                    # Test, no updates sent to parameter server:
                    meta_fetched = [None, None]

                    # self.log.warning('Meta-test rollout ok.')

                if wirte_model_summary:
                    meta_model_summary = meta_fetched[-2]
                    model_summary = fetched[-1]

                else:
                    meta_model_summary = None
                    model_summary = None

                # Next step housekeeping:
                # copy from parameter server:
                sess.run(self.train_aac.sync_pi)
                sess.run(self.test_aac.sync_pi)
                # self.log.warning('Sync ok.')

                # Collect next train trajectory rollout:
                train_data = self.train_aac.get_data(data_sample_config=train_data_config)
                feed_dict = self.train_aac.process_data(sess, train_data, is_train=True)
                # self.log.warning('Train data ok.')

                # Write down summaries:
                self.test_aac.process_summary(sess, test_data, meta_model_summary)
                self.train_aac.process_summary(sess, train_data, model_summary)
                self.local_steps += 1
                roll_num += 1
        except:
            msg = 'process() exception occurred' + \
                  '\n\nPress `Ctrl-C` or jupyter:[Kernel]->[Interrupt] for clean exit.\n'
            self.log.exception(msg)
            raise RuntimeError(msg)